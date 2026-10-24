"""Поиск ffmpeg/ffprobe и обёртки над ними."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

# Возможные места, где winget ставит ffmpeg на Windows.
_WINGET_LINKS = Path(os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links"))
_WINGET_PKGS = Path(os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages"))


def _resolve(name: str) -> str:
    # 1) PATH
    found = shutil.which(name)
    if found:
        return found
    # 2) явный override из .env
    env_dir = os.environ.get("FFMPEG_DIR", "").strip()
    if env_dir:
        cand = Path(env_dir) / (name + ".exe")
        if cand.exists():
            return str(cand)
    # 3) winget Links shim
    cand = _WINGET_LINKS / (name + ".exe")
    if cand.exists():
        return str(cand)
    # 4) поиск в winget Packages (Gyan.FFmpeg/*/bin)
    if _WINGET_PKGS.exists():
        for exe in _WINGET_PKGS.glob(f"Gyan.FFmpeg*/**/bin/{name}.exe"):
            return str(exe)
    # 5) как есть — пусть упадёт с понятной ошибкой при запуске
    return name


FFMPEG = _resolve("ffmpeg")
FFPROBE = _resolve("ffprobe")


def ffmpeg_dir() -> str:
    """Директория ffmpeg — чтобы отдать yt-dlp через ffmpeg_location."""
    p = Path(FFMPEG)
    return str(p.parent) if p.parent != Path(".") else ""


async def ffprobe_media(path: Path) -> dict:
    """Вернуть {width, height, duration} первого видеопотока."""
    cmd = [
        FFPROBE, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height:format=duration",
        "-of", "json", str(path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await proc.communicate()
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    if proc.returncode != 0:
        log.warning("ffprobe failed: %s", err.decode("utf-8", "replace")[:200])
        return {"width": 0, "height": 0, "duration": 0}
    data = json.loads(out.decode("utf-8", "replace"))
    stream = (data.get("streams") or [{}])[0]
    duration = float((data.get("format") or {}).get("duration") or 0)
    return {
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "duration": int(round(duration)),
    }


async def remux_to_mp4(src: Path, dst: Path) -> bool:
    """Быстрый remux в mp4 без перекодирования (для .ts/.mkv HLS-выхлопа)."""
    cmd = [
        FFMPEG, "-y", "-i", str(src),
        "-c", "copy", "-movflags", "+faststart",
        "-map", "0:v:0", "-map", "0:a:0?", str(dst),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        log.warning("remux failed: %s", err.decode("utf-8", "replace")[:300])
        return False
    return True
