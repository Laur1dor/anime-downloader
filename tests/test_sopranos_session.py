import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from animedl.sites.sopranos_session import fetch_page


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_authenticated_http_does_not_launch_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            cookies = Path(directory) / "session.json"
            cookies.write_text('[{"name":"antibot","value":"test","domain":"watch-the-sopranos-online.org","path":"/"}]')
            def respond(request):
                self.assertIn("antibot=test", request.headers["cookie"])
                return httpx.Response(200, text='<div id="single">catalogue</div>')
            with patch("animedl.sites.sopranos_session._cookie_path", return_value=cookies), patch(
                    "animedl.sites.sopranos_session._browser_page", AsyncMock()) as browser:
                async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                    await fetch_page(client, "https://watch-the-sopranos-online.org/")
                browser.assert_not_awaited()

    async def test_expired_session_uses_browser_then_new_http_session(self):
        async def verify(client, url):
            client.cookies.set("antibot", "renewed", domain="watch-the-sopranos-online.org")
            return '<div id="single">catalogue</div>'
        def respond(request):
            return httpx.Response(200, text='<div id="single">catalogue</div>') if "antibot=renewed" in request.headers.get("cookie", "") else httpx.Response(403)
        with tempfile.TemporaryDirectory() as directory, patch(
                "animedl.sites.sopranos_session._cookie_path", return_value=Path(directory)/"missing"), patch(
                "animedl.sites.sopranos_session._browser_page", AsyncMock(side_effect=verify)) as browser:
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                await fetch_page(client, "https://watch-the-sopranos-online.org/")
                await fetch_page(client, "https://watch-the-sopranos-online.org/")
            browser.assert_awaited_once()
