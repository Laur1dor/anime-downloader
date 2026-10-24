"""Alloha: use its normal browser player and verify each offered HLS quality."""
from __future__ import annotations

import asyncio
import html
import json
import re
from urllib.parse import parse_qs, urlparse

from ..http import UA
from ..models import VideoVariant
from .base import ExtractError, PlayerExtractor

_BROWSER_LOCK = asyncio.Semaphore(1)


def manifest_heights(text: str) -> list[int]:
    if not text.lstrip().startswith("#EXTM3U"):
        return []
    return sorted({int(h) for h in re.findall(r"RESOLUTION=\d+x(\d+)", text)}, reverse=True)


def verify_selection(markup: str, url: str) -> None:
    match = re.search(r"const\s+fileList\s*=\s*JSON\.parse\('((?:\\.|[^'\\])*)'\)", markup)
    if not match:
        raise ExtractError("Alloha: нет данных о выбранной серии.")
    try:
        active = json.loads(match[1].replace("\\'", "'"))["active"]
        query = parse_qs(urlparse(url).query)
        for param, field in (("translation", "id_translation"), ("season", "seasons"), ("episode", "episode")):
            if query.get(param) and str(active.get(field)) != query[param][0]:
                raise ExtractError("Alloha: плеер вернул другую серию, сезон или озвучку.")
    except (ValueError, KeyError, TypeError) as exc:
        raise ExtractError("Alloha: повреждены данные выбранной серии.") from exc


class AllohaExtractor(PlayerExtractor):
    name = "Alloha"

    def can_handle(self, embed_url: str) -> bool:
        url = "https:" + embed_url if embed_url.startswith("//") else embed_url
        return urlparse(url).hostname == "alloha.yani.tv"

    async def extract(self, embed_url: str) -> list[VideoVariant]:
        url = "https:" + embed_url if embed_url.startswith("//") else embed_url
        try:
            async with _BROWSER_LOCK:
                return await asyncio.wait_for(self._browser_variants(url), timeout=40)
        except ExtractError:
            raise
        except asyncio.TimeoutError as exc:
            raise ExtractError("Alloha: истекло время проверки качества.") from exc
        except Exception as exc:
            # Browser exceptions can contain signed URLs: never expose their text.
            raise ExtractError(f"Alloha: не удалось открыть плеер ({type(exc).__name__}).") from exc

    async def _browser_variants(self, url: str, consume=None):
        from playwright.async_api import async_playwright

        origin = "https://alloha.yani.tv"
        headers = {"User-Agent": UA, "Referer": origin + "/", "Origin": origin}
        variants: dict[int, VideoVariant] = {}
        ready = asyncio.Event()
        changed = asyncio.Event()
        pending: set[asyncio.Task] = set()
        parent = "https://old.yummyani.me/animedl-player"

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, args=["--no-sandbox"])
            try:
                page = await browser.new_page(user_agent=UA)
                # A normal iframe parent is required by the player. Only this
                # local response is supplied; all player/media requests are real.
                await page.route(parent, lambda route: route.fulfill(
                    content_type="text/html", body=f'<iframe width="1280" height="720" src="{html.escape(url, quote=True)}"></iframe>'))

                async def inspect_response(response):
                    if response.status != 200 or ".m3u8" not in urlparse(response.url).path:
                        return
                    try:
                        for height in manifest_heights(await response.text()):
                            variants[height] = VideoVariant(height, response.url, kind="ytdlp", headers=headers, direct=True, embed_url=url)
                            ready.set()
                            changed.set()
                    except Exception:
                        return

                def on_response(response):
                    task = asyncio.create_task(inspect_response(response))
                    pending.add(task)
                    task.add_done_callback(pending.discard)

                page.on("response", on_response)
                await page.goto(parent, wait_until="domcontentloaded", timeout=15000)
                await asyncio.wait_for(ready.wait(), timeout=20)
                frame = next((f for f in page.frames if urlparse(f.url).hostname == "alloha.yani.tv"), None)
                if frame is None:
                    raise ExtractError("Alloha: плеер не открылся.")
                verify_selection(await frame.content(), url)
                controls = frame.locator('[data-allplay="quality"]:visible')

                async def quality_menu():
                    if await controls.count():
                        return
                    item = frame.get_by_role("menuitem", name=re.compile(r"^Качество"))
                    if not await item.is_visible():
                        # After changing quality the button's text becomes HD.
                        await frame.locator('button[data-allplay="settings"]').first.click(timeout=5000)
                    if not await controls.count():
                        await item.click(timeout=5000)
                    await controls.first.wait_for(timeout=5000)

                await quality_menu()
                offered = sorted({int(x) for x in await controls.evaluate_all("nodes => nodes.map(n => n.value)")}, reverse=True)
                for height in offered:
                    if height in variants:
                        continue
                    changed.clear()
                    await frame.locator(f'[data-allplay="quality"][value="{height}"]').click(timeout=3000)
                    while height not in variants:
                        await asyncio.wait_for(changed.wait(), timeout=5)
                        changed.clear()
                    # Selecting a quality returns to the main settings menu.
                    if height != offered[-1]:
                        await quality_menu()
                if not variants:
                    raise ExtractError("Alloha: нет проверенных вариантов качества.")
                if consume is not None:
                    return await consume(frame, [variants[h] for h in sorted(variants, reverse=True)])
                return [variants[h] for h in sorted(variants, reverse=True)]
            finally:
                for task in pending:
                    task.cancel()
                await browser.close()
