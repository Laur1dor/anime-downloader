"""Relay ordinary player fetches to yt-dlp without buffering an entire video."""
from __future__ import annotations

import asyncio
import base64
import re
import secrets
import threading
from dataclasses import replace
from urllib.parse import urljoin, urlparse

from aiohttp import web

MAX_RESPONSE_BYTES = 16 * 1024 * 1024

_FETCH = """async ({url,range,limit}) => {
 const controller = new AbortController();
 const timer = setTimeout(() => controller.abort(), 45000);
 try {
  const response = await fetch(url, {signal: controller.signal,
    headers: range ? {'Range':range} : {}});
  if (!response.ok) return {status:response.status};
  const reader = response.body.getReader(); const chunks=[]; let size=0;
  while (true) { const {done,value}=await reader.read(); if(done) break;
   size+=value.length;
   if(size>limit) {await reader.cancel();throw Error('Media fragment exceeds relay limit');}
   chunks.push(value);
  }
  let binary='';
  for(const chunk of chunks) for(let i=0;i<chunk.length;i+=32768)
   binary+=String.fromCharCode(...chunk.subarray(i,i+32768));
  return {status:response.status,body:btoa(binary),
    type:response.headers.get('content-type'),
    range:response.headers.get('content-range')};
 } finally {clearTimeout(timer);}
}"""


class PlaylistRelay:
    def __init__(self, frame):
        self.frame = frame
        self.urls: dict[str, str] = {}
        self.tokens: dict[str, str] = {}
        self.base = ""
        self.slots = asyncio.Semaphore(2)

    def register(self, url: str) -> str:
        if urlparse(url).scheme not in ("https", "http"):
            raise ValueError("Unsupported HLS resource scheme")
        token = self.tokens.get(url)
        if token is None:
            token = secrets.token_urlsafe(24)
            self.tokens[url] = token
            self.urls[token] = url
        # Extension ensures yt-dlp recognizes the generic HLS entrypoint.
        return self.base + "/" + token + (".m3u8" if ".m3u8" in urlparse(url).path else ".bin")

    def rewrite(self, text: str, source: str) -> str:
        lines = []
        for line in text.splitlines():
            key_format = re.search(r'KEYFORMAT="([^"]+)"', line)
            if key_format and key_format[1] != "identity":
                raise ValueError("Protected media is unsupported")
            if line.startswith("#EXT-X-KEY") and "METHOD=" in line and "METHOD=AES-128" not in line and "METHOD=NONE" not in line:
                raise ValueError("Protected media is unsupported")
            if line.startswith("#"):
                line = re.sub(r'URI="([^"]+)"', lambda m: 'URI="' + self.register(urljoin(source, m[1])) + '"', line)
            elif line.strip():
                line = self.register(urljoin(source, line.strip()))
            lines.append(line)
        return "\n".join(lines) + "\n"

    async def handle(self, request):
        token = request.match_info["resource"].split(".")[0]
        url = self.urls.get(token)
        if url is None:
            raise web.HTTPNotFound()
        async with self.slots:
            try:
                result = await asyncio.wait_for(self.frame.evaluate(_FETCH, {
                    "url": url, "range": request.headers.get("Range"),
                    "limit": MAX_RESPONSE_BYTES,
                }), timeout=50)
                if result["status"] not in (200, 206):
                    return web.Response(status=result["status"])
                body = base64.b64decode(result["body"], validate=True)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ValueError("Media fragment exceeds relay limit")
                headers = {}
                if body.lstrip().startswith(b"#EXTM3U"):
                    body = self.rewrite(body.decode("utf-8-sig"), url).encode()
                    headers["Content-Type"] = "application/vnd.apple.mpegurl"
                elif result.get("type"):
                    headers["Content-Type"] = result["type"]
                if result.get("range"):
                    headers["Content-Range"] = result["range"]
                return web.Response(body=body, status=result["status"], headers=headers)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Neither signed URLs nor browser exception details enter logs.
                raise web.HTTPBadGateway(text="Browser media request failed") from None


async def download_browser_hls(variant, out_base, progress, cancel_check=None):
    from ..players.alloha import AllohaExtractor
    from .downloader import _run_ytdlp, DownloadCancelled, DownloadError

    stopped = threading.Event()

    def cancelled():
        return stopped.is_set() or bool(cancel_check and cancel_check())

    async def consume(frame, variants):
        selected = next((v for v in variants if v.quality == variant.quality), None)
        if selected is None:
            raise DownloadError("Alloha: выбранное качество больше недоступно")
        # Stop the player's background prefetch; fetch() remains the ordinary
        # page implementation with the same live playback session.
        await frame.locator("video").evaluate_all("nodes => nodes.forEach(v => v.pause())")
        relay = PlaylistRelay(frame)
        app = web.Application()
        app.router.add_get("/{resource}", relay.handle)
        runner = web.AppRunner(app, access_log=None, shutdown_timeout=2)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        relay.base = f"http://127.0.0.1:{port}"
        local = replace(selected, url=relay.register(selected.url), headers={}, embed_url="", direct=True)
        worker = asyncio.create_task(asyncio.to_thread(_run_ytdlp, local, out_base, progress, cancelled))
        try:
            while not worker.done():
                if cancelled():
                    stopped.set()
                    raise DownloadCancelled()
                await asyncio.wait({worker}, timeout=0.5)
            return worker.result()
        finally:
            stopped.set()
            await runner.cleanup()
            # The blocking yt-dlp worker must see failed reads/cancellation
            # before its live browser is closed, including outer-task cancel.
            if not worker.done():
                try:
                    await asyncio.wait_for(asyncio.shield(worker), timeout=55)
                except Exception:
                    pass

    return await AllohaExtractor()._browser_variants(variant.embed_url, consume=consume)
