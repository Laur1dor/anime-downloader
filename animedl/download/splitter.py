"""Split oversized uploads on keyframes without reducing their quality."""
from __future__ import annotations

import asyncio
import math
import shutil
import tempfile
from pathlib import Path
from typing import Callable

from .downloader import DownloadCancelled, DownloadResult
from .fftools import FFMPEG, ffprobe_media


async def _segment(source: Path, folder: Path, seconds: float,
                   cancel_check: Callable[[], bool] | None) -> list[Path]:
    folder.mkdir()
    proc = await asyncio.create_subprocess_exec(
        FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(source), "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
        "-f", "segment", "-segment_time", f"{seconds:.6f}",
        "-reset_timestamps", "1", "-segment_format", "mp4",
        "-segment_format_options", "movflags=+faststart",
        str(folder / "part-%05d.mp4"),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    tail = bytearray()

    async def drain() -> None:
        assert proc.stderr is not None
        while block := await proc.stderr.read(4096):
            tail.extend(block)
            del tail[:-8192]

    reader = asyncio.create_task(drain())
    try:
        while proc.returncode is None:
            if cancel_check and cancel_check():
                raise DownloadCancelled()
            try:
                await asyncio.wait_for(asyncio.shield(reader), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            await proc.wait()
        await reader
        if proc.returncode:
            raise ValueError("Не удалось разделить видео: " + tail.decode("utf-8", "replace")[-1000:])
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        await reader
    paths = sorted(folder.glob("part-*.mp4"))
    if not paths or any(p.stat().st_size == 0 for p in paths):
        raise ValueError("ffmpeg не создал корректные части видео")
    return paths


async def split_for_telegram(
    result: DownloadResult, max_bytes: int, *,
    cancel_check: Callable[[], bool] | None = None,
) -> list[DownloadResult]:
    """Return independently playable MP4s, each at most ``max_bytes``.

    The original is never changed. Generated files live in a unique sibling
    directory; callers own them after success. On failure/cancellation all
    generated files are removed. Splitting has finite depth and part limits;
    a GOP larger than the cap raises rather than silently dropping frames.
    """
    if max_bytes <= 0:
        raise ValueError("Лимит размера должен быть положительным")
    if cancel_check and cancel_check():
        raise DownloadCancelled()
    actual_size = result.path.stat().st_size
    if actual_size <= max_bytes:
        return [result]
    info = await ffprobe_media(result.path)
    duration = info.get("duration") or result.duration
    if not duration or not math.isfinite(duration) or duration <= 0:
        raise ValueError("Не удалось определить длительность видео для разделения")
    workspace = Path(tempfile.mkdtemp(prefix=result.path.stem + "-parts-", dir=result.path.parent))
    index = 0
    accepted: list[Path] = []

    async def divide(source: Path, seconds: float, depth: int) -> None:
        nonlocal index
        if cancel_check and cancel_check():
            raise DownloadCancelled()
        if depth >= 12 or index >= 1024:
            raise ValueError("Видео нельзя разделить в заданный лимит без перекодирования (слишком большой интервал между ключевыми кадрами)")
        index += 1
        children = await _segment(source, workspace / f"pass-{index:04d}", seconds, cancel_check)
        oversized = [p for p in children if p.stat().st_size > max_bytes]
        # A sole segment means no usable keyframe was reached. Retry with
        # shorter intervals, but never recurse indefinitely on the same GOP.
        for child in children:
            if child in oversized:
                await divide(child, max(0.001, seconds / 2), depth + 1)
                child.unlink()
            else:
                accepted.append(child)
                if len(accepted) > 1024:
                    raise ValueError("Слишком много частей видео")

    try:
        await divide(result.path, max(0.001, duration * max_bytes / actual_size * 0.8), 0)
        parts: list[DownloadResult] = []
        for number, path in enumerate(accepted, 1):
            if cancel_check and cancel_check():
                raise DownloadCancelled()
            details = await ffprobe_media(path)
            if not details.get("width"):
                raise ValueError("Созданная часть видео не читается")
            destination = workspace / f"part-{number:03d}.mp4"
            path.replace(destination)
            parts.append(DownloadResult(
                path=destination, width=details["width"], height=details["height"],
                duration=details["duration"], size_bytes=destination.stat().st_size,
                meta=dict(result.meta, part_number=number, part_count=len(accepted)),
            ))
        for folder in workspace.glob("pass-*"):
            folder.rmdir()
        return parts
    except BaseException:
        shutil.rmtree(workspace, ignore_errors=True)
        raise
