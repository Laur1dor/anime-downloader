import asyncio
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from animedl.download import splitter
from animedl.download.downloader import DownloadCancelled, DownloadResult


class SplitterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.path = self.folder / "video.mp4"
        self.path.write_bytes(b"x" * 1000)
        self.result = DownloadResult(self.path, 320, 180, 10, 1000, {"voice": "original"})
        self.probe = patch.object(splitter, "ffprobe_media", AsyncMock(
            return_value={"width": 320, "height": 180, "duration": 10}))
        self.probe.start()
        self.addCleanup(self.probe.stop)

    async def test_small_input_is_not_copied_or_probed(self):
        parts = await splitter.split_for_telegram(self.result, 1000)
        self.assertEqual(parts, [self.result])
        splitter.ffprobe_media.assert_not_awaited()

    async def test_uses_actual_size_and_recursively_splits_large_part(self):
        calls = []

        async def segment(source, folder, seconds, cancel_check):
            calls.append(seconds)
            folder.mkdir()
            sizes = [600, 400] if len(calls) == 1 else [300, 300]
            children = []
            for index, size in enumerate(sizes):
                path = folder / f"part-{index:05}.mp4"
                path.write_bytes(bytes([index]) * size)
                children.append(path)
            return children

        self.result.size_bytes = 1  # A stale cached size must not bypass splitting.
        with patch.object(splitter, "_segment", side_effect=segment):
            parts = await splitter.split_for_telegram(self.result, 500)
        self.assertEqual([p.size_bytes for p in parts], [300, 300, 400])
        self.assertEqual([p.meta["part_number"] for p in parts], [1, 2, 3])
        self.assertEqual(parts[0].meta["voice"], "original")
        self.assertTrue(all(p.meta["part_count"] == 3 for p in parts))
        self.assertLess(calls[1], calls[0])
        self.assertEqual(self.path.stat().st_size, 1000)

    async def test_failed_split_cleans_all_outputs_and_keeps_original(self):
        with patch.object(splitter, "_segment", AsyncMock(side_effect=ValueError("bad codec"))):
            with self.assertRaisesRegex(ValueError, "bad codec"):
                await splitter.split_for_telegram(self.result, 500)
        self.assertEqual(list(self.folder.iterdir()), [self.path])

    async def test_single_oversized_gop_has_finite_retries_and_cleanup(self):
        async def segment(source, folder, seconds, cancel_check):
            folder.mkdir()
            child = folder / "part-00000.mp4"
            child.write_bytes(b"x" * 1000)
            return [child]

        with patch.object(splitter, "_segment", side_effect=segment) as process:
            with self.assertRaisesRegex(ValueError, "ключевыми"):
                await splitter.split_for_telegram(self.result, 500)
        self.assertEqual(process.call_count, 12)
        self.assertEqual(list(self.folder.iterdir()), [self.path])

    async def test_cancellation_cleans_generated_files(self):
        async def segment(source, folder, seconds, cancel_check):
            folder.mkdir()
            (folder / "partial.mp4").write_bytes(b"unfinished")
            raise asyncio.CancelledError()

        with patch.object(splitter, "_segment", side_effect=segment):
            with self.assertRaises(asyncio.CancelledError):
                await splitter.split_for_telegram(self.result, 500)
        self.assertEqual(list(self.folder.iterdir()), [self.path])

    async def test_pre_cancelled_does_not_start_process(self):
        with self.assertRaises(DownloadCancelled):
            await splitter.split_for_telegram(self.result, 500, cancel_check=lambda: True)
        splitter.ffprobe_media.assert_not_awaited()

    async def test_running_ffmpeg_is_killed_and_waited_on_cancel(self):
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def read(size):
            started.set()
            await stopped.wait()
            return b""

        process = SimpleNamespace(returncode=None, stderr=SimpleNamespace(read=read))

        def kill():
            process.returncode = -9
            stopped.set()

        process.kill = Mock(side_effect=kill)
        process.wait = AsyncMock(return_value=-9)
        with patch.object(asyncio, "create_subprocess_exec", AsyncMock(return_value=process)):
            task = asyncio.create_task(splitter._segment(
                self.path, self.folder / "running", 1, None))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()

    async def test_ffmpeg_error_has_bounded_details(self):
        process = SimpleNamespace(returncode=1, stderr=SimpleNamespace(
            read=AsyncMock(side_effect=[b"x" * 10000, b""])))
        with patch.object(asyncio, "create_subprocess_exec", AsyncMock(return_value=process)):
            with self.assertRaises(ValueError) as error:
                await splitter._segment(self.path, self.folder / "bad", 1, None)
        self.assertLess(len(str(error.exception)), 1100)

    async def test_invalid_duration_and_limit_are_rejected(self):
        with self.assertRaises(ValueError):
            await splitter.split_for_telegram(self.result, 0)
        splitter.ffprobe_media.return_value = {"duration": 0}
        self.result.duration = 0
        with self.assertRaisesRegex(ValueError, "длительность"):
            await splitter.split_for_telegram(self.result, 500)


@unittest.skipUnless(shutil.which(splitter.FFMPEG), "ffmpeg is not installed")
class RealSplitterTests(unittest.IsolatedAsyncioTestCase):
    async def test_mp4_parts_are_playable_with_original_audio_and_resolution(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sample.mp4"
            subprocess.run([
                splitter.FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
                "testsrc2=size=320x180:rate=24:duration=8", "-f", "lavfi", "-i",
                "sine=frequency=440:duration=8", "-c:v", "libx264", "-crf", "18",
                "-g", "24", "-keyint_min", "24", "-sc_threshold", "0",
                "-c:a", "aac", "-shortest", str(path),
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            info = await splitter.ffprobe_media(path)
            source = DownloadResult(path, info["width"], info["height"], info["duration"], path.stat().st_size)
            cap = source.size_bytes // 3
            parts = await splitter.split_for_telegram(source, cap)
            self.assertGreater(len(parts), 1)
            self.assertTrue(all(p.size_bytes <= cap for p in parts))
            for part in parts:
                self.assertEqual((part.width, part.height), (320, 180))
                # Decoding validates independent video/audio playback, not just headers.
                subprocess.run([splitter.FFMPEG, "-v", "error", "-xerror", "-i", str(part.path),
                                "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"],
                               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            self.assertLess(abs(sum(p.duration for p in parts) - source.duration), len(parts))
