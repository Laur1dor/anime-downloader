"""A short-lived, direct browser session for the site's normal verification."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlparse

from ..http import UA
from .base import ParseError

_LOCK = asyncio.Lock()
_HOST = "watch-the-sopranos-online.org"


def _cookie_path() -> Path:
    return Path(os.environ.get("SOPRANOS_COOKIE_PATH", "/app/data/sopranos-session.json"))


def _apply_cookies(client, cookies: list[dict]) -> None:
    for cookie in cookies:
        domain = cookie.get("domain", "").lstrip(".")
        if domain == _HOST:
            client.cookies.set(cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie.get("path", "/"))


async def fetch_page(client, url: str) -> str:
    if urlparse(url).hostname != _HOST:
        raise ParseError("Некорректный адрес сайта «Клан Сопрано».")
    try:
        _apply_cookies(client, json.loads(_cookie_path().read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    response = await client.get(url)
    if response.status_code == 200 and 'id="single"' in response.text:
        return response.text
    if response.status_code not in (200, 403):
        raise ParseError(f"Сайт «Клан Сопрано»: страница вернула {response.status_code}.")
    async with _LOCK:
        # Another request may have refreshed the session while we were waiting.
        try:
            _apply_cookies(client, json.loads(_cookie_path().read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError, TypeError):
            pass
        response = await client.get(url)
        if response.status_code == 200 and 'id="single"' in response.text:
            return response.text
        return await _browser_page(client, url)


async def _browser_page(client, url: str) -> str:
    try:
        from playwright.async_api import async_playwright
        async with asyncio.timeout(75):
            async with async_playwright() as runtime:
                clean_env = {k: v for k, v in os.environ.items() if k.lower() not in (
                    "http_proxy", "https_proxy", "all_proxy")}
                browser = await runtime.chromium.launch(
                    headless=True, args=["--no-proxy-server", "--disable-dev-shm-usage"], env=clean_env)
                try:
                    context = await browser.new_context(user_agent=UA, locale="ru-RU", timezone_id="Europe/Moscow")
                    page = await context.new_page()
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    # Only click the site's visible ordinary confirmation button.
                    # Never compute its hidden tokens or emulate its fingerprint.
                    if await page.locator("#single").count() == 0:
                        candidates = page.get_by_text("Я не робот", exact=True)
                        for candidate in await candidates.all():
                            if await candidate.is_visible():
                                await candidate.click(timeout=10000)
                                break
                    await page.locator("#single").wait_for(state="attached", timeout=30000)
                    html = await page.content()
                    cookies = [c for c in await context.cookies() if c["domain"].lstrip(".") == _HOST]
                    _apply_cookies(client, cookies)
                    path = _cookie_path()
                    path.parent.mkdir(parents=True, exist_ok=True)
                    temporary = path.with_suffix(".tmp")
                    temporary.write_text(json.dumps(cookies), encoding="utf-8")
                    temporary.chmod(0o600)
                    os.replace(temporary, path)
                    return html
                finally:
                    await browser.close()
    except Exception as exc:
        raise ParseError("Сайт «Клан Сопрано»: автоматическая проверка не завершилась; попробуй позже.") from exc
