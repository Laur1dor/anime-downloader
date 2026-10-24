import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.types import FSInputFile

from animedl.telegram_client import build_bot, send_video_file


class LocalFileTransportTests(unittest.IsolatedAsyncioTestCase):
    async def upload(self, path, root=None, local=True):
        bot = SimpleNamespace(session=SimpleNamespace(api=SimpleNamespace(is_local=local)),
                              send_video=AsyncMock(return_value="sent"))
        await send_video_file(bot, 1, path, width=640, height=360, duration=10,
                              local_file_root=root)
        return bot.send_video.call_args.kwargs["video"]

    async def test_opted_in_local_file_uses_uri_with_encoded_spaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "episode space.mp4"
            path.write_bytes(b"small actual file")
            self.assertEqual(await self.upload(path, root), path.resolve().as_uri())
            self.assertIn("%20", await self.upload(path, root))

    async def test_cloud_or_no_allowlist_always_uses_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "video.mp4"
            path.write_bytes(b"video")
            self.assertIsInstance(await self.upload(path, root, local=False), FSInputFile)
            self.assertIsInstance(await self.upload(path, root=None), FSInputFile)

    async def test_outside_or_prefix_sibling_cannot_use_file_uri(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "work"
            sibling = Path(tmp) / "work-extra"
            root.mkdir()
            sibling.mkdir()
            path = sibling / "video.mp4"
            path.write_bytes(b"video")
            self.assertIsInstance(await self.upload(path, root), FSInputFile)
            traversal = root / ".." / "work-extra" / "video.mp4"
            self.assertIsInstance(await self.upload(traversal, root), FSInputFile)

    async def test_symlink_outside_allowlist_never_uses_uri(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "work"
            root.mkdir()
            outside = Path(tmp) / "private.mp4"
            outside.write_bytes(b"private")
            link = root / "link.mp4"
            try:
                link.symlink_to(outside)
            except OSError:
                self.skipTest("symlink permission unavailable; enforced by Linux CI")
            self.assertIsInstance(await self.upload(link, root), FSInputFile)

    async def test_missing_path_falls_back_without_falsely_claiming_local_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsInstance(await self.upload(Path(tmp) / "missing.mp4", Path(tmp)), FSInputFile)

    async def test_build_bot_preserves_local_api_mode(self):
        cfg = SimpleNamespace(tg_api_base_url="http://telegram-bot-api:8081",
                              tg_api_local_mode=True, bot_token="123456:abcdef")
        bot = build_bot(cfg)
        try:
            self.assertTrue(bot.session.api.is_local)
        finally:
            await bot.session.close()
