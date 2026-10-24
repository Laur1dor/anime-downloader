import struct
import asyncio
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx

from animedl.artwork import _image_extension, create_episode_thumbnail, download_poster, extract_poster_url


def png():
    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b""))


# Minimal marker fixture for structural validation (not a decoder fixture).
JPEG = (b"\xff\xd8\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
        b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00\x01\xff\xd9")


class ArtworkTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name) / "Series"
        self.addCleanup(self.tmp.cleanup)

    def client(self, response):
        actual_client = httpx.AsyncClient
        def factory(**kwargs):
            self.assertEqual(kwargs["timeout"], 8.0)
            self.assertFalse(kwargs["trust_env"])
            return actual_client(transport=httpx.MockTransport(response), **kwargs)
        return patch("animedl.artwork.new_client", side_effect=factory)

    async def test_png_atomic_publish_and_source_referer(self):
        self.directory.mkdir()
        old = self.directory / "poster.jpg"
        old.write_bytes(b"previous poster")
        def reply(request):
            self.assertEqual(request.headers["referer"], "https://source.test/anime")
            return httpx.Response(200, headers={"Content-Type": "image/png"}, content=png())
        with self.client(reply):
            self.assertTrue(await download_poster("https://cdn.test/cover.jpg", self.directory, referer="https://source.test/anime"))
        self.assertEqual((self.directory / "poster.png").read_bytes(), png())
        self.assertFalse(old.exists())
        self.assertEqual([p.name for p in self.directory.iterdir()], ["poster.png"])

    async def test_missing_or_invalid_url_skips_network(self):
        with patch("animedl.artwork.new_client") as client:
            for url in ("", "file:///poster.jpg", "//cdn.test/poster.jpg"):
                self.assertFalse(await download_poster(url, self.directory))
            client.assert_not_called()

    async def test_one_download_publishes_series_and_season_cover(self):
        calls = []
        def reply(request):
            calls.append(request)
            return httpx.Response(200, headers={"Content-Type": "image/png"}, content=png())
        season = self.directory / "Season 01"
        with self.client(reply):
            self.assertTrue(await download_poster("https://cdn.test/cover", self.directory, season_dir=season))
        self.assertEqual(len(calls), 1)
        self.assertEqual((season / "poster.png").read_bytes(), png())
        self.assertEqual((self.directory / "poster.png").read_bytes(), png())

    async def test_html_or_invalid_mime_never_published(self):
        for mime, data in (("image/png", b"<html>forbidden</html>"), ("text/html", png()), ("image/jpeg", png())):
            with self.client(lambda request: httpx.Response(200, headers={"Content-Type": mime}, content=data)):
                self.assertFalse(await download_poster("https://cdn.test/cover", self.directory))
        self.assertFalse(self.directory.exists())

    async def test_failed_download_preserves_previous_poster(self):
        self.directory.mkdir()
        old = self.directory / "poster.png"
        old.write_bytes(png())
        with self.client(lambda request: httpx.Response(403)):
            self.assertFalse(await download_poster("https://cdn.test/cover", self.directory))
        self.assertEqual(old.read_bytes(), png())

    async def test_declared_or_streamed_oversize_rejected(self):
        for headers in ({"Content-Length": "999"}, {}):
            with self.client(lambda request: httpx.Response(200, headers={"Content-Type": "image/png", **headers}, content=png())):
                with patch("animedl.artwork.MAX_POSTER_BYTES", 32):
                    self.assertFalse(await download_poster("https://cdn.test/cover", self.directory))
        self.assertFalse(self.directory.exists())

    async def test_replace_failure_cleans_temporary_file(self):
        with self.client(lambda request: httpx.Response(200, headers={"Content-Type": "image/png"}, content=png())):
            with patch("animedl.artwork.os.replace", side_effect=OSError("read only")):
                self.assertFalse(await download_poster("https://cdn.test/cover", self.directory))
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_timeout_is_nonfatal(self):
        def reply(request):
            raise httpx.ReadTimeout("timeout", request=request)
        with self.client(reply):
            self.assertFalse(await download_poster("https://cdn.test/cover", self.directory))

    def test_reject_corrupt_png_and_fake_jpeg_webp(self):
        self.assertEqual(_image_extension(png(), "image/png; charset=binary"), "png")
        broken = bytearray(png())
        broken[45] ^= 1
        self.assertEqual(_image_extension(bytes(broken), "image/png"), "")
        self.assertEqual(_image_extension(b"\xff\xd8fake\xff\xd9", "image/jpeg"), "")
        self.assertEqual(_image_extension(b"RIFF\x00\x00\x00\x00WEBP", "image/webp"), "")

    def test_extract_full_og_poster_and_normalize_urls(self):
        source = "https://old.yummyani.me/catalog/item/title"
        self.assertEqual(extract_poster_url('<meta property="og:image" content="//static.yani.tv/posters/full/123.jpg">', source),
                         "https://static.yani.tv/posters/full/123.jpg")
        self.assertEqual(extract_poster_url('<div class="anime-poster"><img src="/preview.jpg" data-original="/full.jpg"></div>', source),
                         "https://old.yummyani.me/full.jpg")
        self.assertEqual(extract_poster_url('<meta property="og:image" content="javascript:bad"><div class="inner-page__img"><img src="/cover.png"></div>', source),
                         "https://old.yummyani.me/cover.png")
        self.assertEqual(extract_poster_url('<img src="/advertisement.jpg">', source), "")

    async def test_thumbnail_command_short_seek_atomic_and_existing_skip(self):
        self.directory.mkdir()
        video = self.directory / "Anime S01E01.mp4"
        video.write_bytes(b"video placeholder")
        destination = video.with_name(video.stem + "-thumb.jpg")
        async def spawn(*args, **kwargs):
            self.assertEqual(args[args.index("-ss") + 1], "1.000")
            self.assertEqual(args[args.index("-i") + 1], str(video))
            self.assertEqual(args[args.index("-frames:v") + 1], "1")
            self.assertEqual(args[args.index("-threads") + 1], "1")
            self.assertFalse(destination.exists())
            Path(args[-1]).write_bytes(JPEG)
            return Mock(returncode=0, wait=AsyncMock(return_value=0))
        with patch("animedl.artwork.asyncio.create_subprocess_exec", side_effect=spawn) as subprocess:
            self.assertTrue(await create_episode_thumbnail(video, 10))
            self.assertTrue(await create_episode_thumbnail(video, 10))
            self.assertEqual(subprocess.call_count, 1)
        self.assertEqual(destination.read_bytes(), JPEG)
        self.assertEqual(len(list(self.directory.iterdir())), 2)

    async def test_missing_ffmpeg_and_invalid_output_are_nonfatal(self):
        self.directory.mkdir()
        video = self.directory / "Anime.mp4"
        video.touch()
        with patch("animedl.artwork.asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
            self.assertFalse(await create_episode_thumbnail(video))
        for code in (0, 1):
            with patch("animedl.artwork.asyncio.create_subprocess_exec", return_value=Mock(returncode=code, wait=AsyncMock(return_value=code))):
                self.assertFalse(await create_episode_thumbnail(video))
        self.assertEqual(list(self.directory.iterdir()), [video])

    async def test_thumbnail_timeout_kills_child_and_cleans_temp(self):
        self.directory.mkdir()
        video = self.directory / "Anime.mp4"
        video.touch()
        proc = Mock(returncode=None, wait=AsyncMock(side_effect=[asyncio.TimeoutError, -9]))
        proc.kill.side_effect = lambda: setattr(proc, "returncode", -9)
        with patch("animedl.artwork.asyncio.create_subprocess_exec", return_value=proc):
            self.assertFalse(await create_episode_thumbnail(video, 24 * 60))
        proc.kill.assert_called_once()
        self.assertEqual(list(self.directory.iterdir()), [video])


if __name__ == "__main__":
    unittest.main()
