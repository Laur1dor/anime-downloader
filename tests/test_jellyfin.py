import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from animedl.jellyfin import refresh_library


class JellyfinTests(unittest.IsolatedAsyncioTestCase):
    async def test_unconfigured_integration_makes_no_request(self):
        with patch("animedl.jellyfin.httpx.AsyncClient") as client:
            for url, key in (("", ""), ("http://jellyfin.test", ""), ("", "test-token")):
                cfg = SimpleNamespace(jellyfin_url=url, jellyfin_api_key=key)
                self.assertFalse(await refresh_library(cfg))
            client.assert_not_called()

    async def test_posts_refresh_directly_with_header_and_timeout(self):
        requests = []
        transport = httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(204))
        actual_client = httpx.AsyncClient
        def factory(**kwargs):
            self.assertEqual(kwargs, {"timeout": 8.0, "trust_env": False})
            return actual_client(transport=transport, **kwargs)
        cfg = SimpleNamespace(jellyfin_url="http://jellyfin.test/base/", jellyfin_api_key="test-token")
        with patch("animedl.jellyfin.httpx.AsyncClient", side_effect=factory):
            self.assertTrue(await refresh_library(cfg))
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].method, "POST")
        self.assertEqual(str(requests[0].url), "http://jellyfin.test/base/Library/Refresh")
        self.assertEqual(requests[0].headers["Authorization"],
            'MediaBrowser Client="AnimeDownloader", Device="Bot", DeviceId="anime-bot", '
            'Version="1.0", Token="test-token"')
        self.assertNotIn("test-token", str(requests[0].url))

    async def test_http_failure_is_raised_for_caller_to_report(self):
        actual_client = httpx.AsyncClient
        transport = httpx.MockTransport(lambda request: httpx.Response(401))
        cfg = SimpleNamespace(jellyfin_url="http://jellyfin.test", jellyfin_api_key="test-token")
        with patch("animedl.jellyfin.httpx.AsyncClient", side_effect=lambda **kw: actual_client(transport=transport, **kw)):
            with self.assertRaises(httpx.HTTPStatusError):
                await refresh_library(cfg)


if __name__ == "__main__":
    unittest.main()
