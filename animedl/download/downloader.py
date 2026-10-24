"""Скачивание VideoVariant в mp4 через yt-dlp + ffmpeg, с прогрессом и ffprobe."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from ..models import VideoVariant
from .fftools import ffmpeg_dir, ffprobe_media

log = logging.getLogger(__name__)


class DownloadError(Exception):
    pass


class DownloadCancelled(Exception):
    """Скачивание прервано пользователем (отмена)."""


@dataclass
class Progress:
    percent: float = 0.0
    downloaded: int = 0
    total: int = 0
    status: str = "queued"   # queued | downloading | processing | done | error


@dataclass
class DownloadResult:
    path: Path
    width: int
    height: int
    duration: int
    size_bytes: int
    meta: dict = field(default_factory=dict)


def _format_selector(variant: VideoVariant) -> str:
    if variant.kind == "ytdlp":
        q = variant.quality or 1080
        if variant.format_id:
            return f"{variant.format_id}+bestaudio/best/{variant.format_id}"
        return f"bv*[height<={q}]+ba/b[height<={q}]/best"
    # прямой m3u8/mp4 — единый поток
    return "best/bestvideo+bestaudio"


def _run_ytdlp(variant: VideoVariant, out_base: Path, progress: Progress,
               cancel_check: Callable[[], bool] | None = None) -> Path:
    import yt_dlp

    def hook(d: dict) -> None:
        if cancel_check and cancel_check():
            raise DownloadCancelled()
        if d.get("status") == "downloading":
            progress.status = "downloading"
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            progress.downloaded, progress.total = done, total
            progress.percent = (done / total * 100.0) if total else progress.percent
        elif d.get("status") == "finished":
            progress.status = "processing"
            progress.percent = 100.0

    opts = {
        "outtmpl": str(out_base) + ".%(ext)s",
        "format": _format_selector(variant),
        "merge_output_format": "mp4",
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
        "noplaylist": True,
        "http_headers": variant.headers or {},
        "hls_use_mpegts": False,
        "concurrent_fragment_downloads": 4,
        "socket_timeout": 45,
        "skip_unavailable_fragments": False,
        "retries": 5,
        "fragment_retries": 10,
        "progress_hooks": [hook],
        "overwrites": True,
    }
    ff = ffmpeg_dir()
    if variant.direct:
        opts["proxy"] = ""
    if urlparse(variant.url).hostname == "127.0.0.1":
        # The browser relay already bounds remote requests. Fail quickly when
        # cancellation closes it instead of spawning long fragment retries.
        opts.update(concurrent_fragment_downloads=2, retries=1, fragment_retries=1)
    if ff:
        opts["ffmpeg_location"] = ff

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([variant.url])
    except Exception as e:  # noqa: BLE001
        # yt-dlp оборачивает исключение из хука; распознаём отмену
        if isinstance(e, DownloadCancelled) or "DownloadCancelled" in repr(e):
            for c in out_base.parent.glob(out_base.name + ".*"):
                c.unlink(missing_ok=True)
            raise DownloadCancelled() from e
        raise

    # находим итоговый файл
    candidates = sorted(out_base.parent.glob(out_base.name + ".*"))
    mp4 = [c for c in candidates if c.suffix == ".mp4"]
    if mp4:
        return mp4[0]
    if candidates:
        return candidates[0]
    raise DownloadError("yt-dlp завершился, но файл не найден")


async def download_variant(
    variant: VideoVariant, out_base: Path, progress: Progress | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> DownloadResult:
    progress = progress or Progress()
    progress.status = "downloading"
    out_base.parent.mkdir(parents=True, exist_ok=True)

    try:
        if variant.embed_url:
            # Browser-bound Alloha sessions need their normal page fetch logic
            # for every segment, not just a captured manifest URL.
            from .browser_hls import download_browser_hls
            path = await download_browser_hls(variant, out_base, progress, cancel_check)
        else:
            path = await asyncio.to_thread(_run_ytdlp, variant, out_base, progress, cancel_check)
    except DownloadCancelled:
        progress.status = "error"
        raise
    except Exception as e:  # noqa: BLE001
        progress.status = "error"
        raise DownloadError(f"Ошибка скачивания: {e}") from e

    progress.status = "processing"
    info = await ffprobe_media(path)
    size = path.stat().st_size
    progress.status = "done"
    progress.percent = 100.0
    log.info("Скачано %s (%.1f МБ, %dx%d, %dс)",
             path.name, size / 1024 / 1024, info["width"], info["height"], info["duration"])
    return DownloadResult(
        path=path, width=info["width"], height=info["height"],
        duration=info["duration"], size_bytes=size,
    )
