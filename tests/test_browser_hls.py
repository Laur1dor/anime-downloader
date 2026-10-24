import base64
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import web

from animedl.download.browser_hls import PlaylistRelay, MAX_RESPONSE_BYTES


class BrowserHLSTests(unittest.IsolatedAsyncioTestCase):
    def test_rewrites_nested_playlists_segments_keys_and_maps(self):
        relay = PlaylistRelay(None)
        relay.base = "http://127.0.0.1:1234"
        source = "https://cdn.example/signed/master.m3u8?secret=token"
        output = relay.rewrite('#EXTM3U\n#EXT-X-MEDIA:URI="audio.m3u8"\n#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n#EXT-X-MAP:URI="init.mp4"\n../segment.ts?secret=media\n', source)
        self.assertNotIn("secret", output)
        self.assertNotIn("cdn.example", output)
        self.assertEqual(len(relay.urls), 4)
        self.assertIn("https://cdn.example/segment.ts?secret=media", relay.urls.values())
        self.assertEqual(relay.register(source), relay.register(source))

    def test_refuses_non_http_resources_and_protected_media(self):
        relay = PlaylistRelay(None)
        for value in ("file:///etc/passwd", "data:text/plain,key"):
            with self.assertRaises(ValueError):
                relay.register(value)
        with self.assertRaises(ValueError):
            relay.rewrite('#EXT-X-KEY:METHOD=SAMPLE-AES,URI="key"', "https://cdn.example/a.m3u8")

    async def test_fragment_request_is_bounded_and_forwards_range(self):
        frame = SimpleNamespace(evaluate=AsyncMock(return_value={
            "status": 206, "body": base64.b64encode(b"media").decode(),
            "range": "bytes 0-4/10", "type": "video/mp2t"}))
        relay = PlaylistRelay(frame)
        token = relay.register("https://cdn.example/fragment.ts").split("/")[-1]
        request = SimpleNamespace(match_info={"resource": token}, headers={"Range": "bytes=0-4"})
        response = await relay.handle(request)
        self.assertEqual(response.status, 206)
        self.assertEqual(response.body, b"media")
        self.assertEqual(frame.evaluate.call_args.args[1]["limit"], MAX_RESPONSE_BYTES)
        self.assertEqual(frame.evaluate.call_args.args[1]["range"], "bytes=0-4")

    async def test_error_does_not_expose_signed_url(self):
        frame = SimpleNamespace(evaluate=AsyncMock(side_effect=RuntimeError("secret=token")))
        relay = PlaylistRelay(frame)
        token = relay.register("https://cdn.example/a.ts?secret=token").split("/")[-1]
        with self.assertRaises(web.HTTPBadGateway) as caught:
            await relay.handle(SimpleNamespace(match_info={"resource": token}, headers={}))
        self.assertNotIn("secret", caught.exception.text)
