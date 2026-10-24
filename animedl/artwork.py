"""Bounded source-site artwork download for a local media-library series."""
from __future__ import annotations

import asyncio
import logging
import os
import struct
import tempfile
import zlib
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .download.fftools import FFMPEG
from .http import new_client

log = logging.getLogger(__name__)
MAX_POSTER_BYTES = 8 * 1024 * 1024
POSTER_TIMEOUT = 20.0
_MIME_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


def extract_poster_url(raw_html: str, source_url: str) -> str:
    """Extract title artwork, resolving relative and protocol-relative URLs."""
    soup = BeautifulSoup(raw_html, "lxml")
    selectors = (
        ('meta[property="og:image"]', ("content",)),
        ('meta[name="twitter:image"]', ("content",)),
        ('.anime-poster img, .full-poster img, .inner-page__img img, .poster img',
         ("data-original", "data-src", "src")),
        ('[itemprop="image"] link[href]', ("href",)),
        ('img[itemprop="image"]', ("data-original", "data-src", "src")),
    )
    for selector, attributes in selectors:
        for element in soup.select(selector):
            for attribute in attributes:
                value = str(element.get(attribute) or "").strip()
                if not value:
                    continue
                resolved = urljoin(source_url, value)
                parsed = urlparse(resolved)
                if parsed.scheme in ("http", "https") and parsed.hostname:
                    return resolved
    return ""


def _image_extension(data: bytes, mime: str) -> str:
    """Check MIME and container structure; never publish HTML/error responses."""
    ext = _MIME_EXT.get(mime.split(";", 1)[0].strip().lower(), "")
    if ext == "png" and data.startswith(b"\x89PNG\r\n\x1a\n"):
        offset, chunks, has_data = 8, 0, False
        while offset + 12 <= len(data):
            length = int.from_bytes(data[offset:offset + 4], "big")
            end = offset + 12 + length
            if end > len(data):
                return ""
            kind = data[offset + 4:offset + 8]
            payload = data[offset + 8:end - 4]
            crc = int.from_bytes(data[end - 4:end], "big")
            if zlib.crc32(kind + payload) & 0xffffffff != crc:
                return ""
            if chunks == 0:
                if kind != b"IHDR" or length != 13:
                    return ""
                width, height = struct.unpack(">II", payload[:8])
                if not width or not height:
                    return ""
            has_data |= kind == b"IDAT" and length > 0
            if kind == b"IEND":
                return ext if length == 0 and end == len(data) and has_data else ""
            chunks += 1
            offset = end
    elif ext == "jpg" and data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9"):
        # Parse marker lengths until scan data; require a frame with dimensions.
        offset, frame = 2, False
        while offset + 4 <= len(data):
            if data[offset] != 0xff:
                return ""
            while offset < len(data) and data[offset] == 0xff:
                offset += 1
            if offset >= len(data):
                return ""
            marker = data[offset]
            offset += 1
            length = int.from_bytes(data[offset:offset + 2], "big")
            if length < 2 or offset + length > len(data) - 2:
                return ""
            if marker in (0xc0, 0xc1, 0xc2):
                if length < 8 or not int.from_bytes(data[offset + 3:offset + 5], "big") or not int.from_bytes(data[offset + 5:offset + 7], "big"):
                    return ""
                frame = True
            if marker == 0xda:
                return ext if frame and offset + length < len(data) - 2 else ""
            offset += length
    elif ext == "webp" and len(data) >= 20 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        if int.from_bytes(data[4:8], "little") + 8 != len(data):
            return ""
        offset, image = 12, False
        while offset + 8 <= len(data):
            kind = data[offset:offset + 4]
            length = int.from_bytes(data[offset + 4:offset + 8], "little")
            end = offset + 8 + length
            if end > len(data):
                return ""
            payload = data[offset + 8:end]
            if kind == b"VP8 ":
                image |= length >= 10 and payload[3:6] == b"\x9d\x01\x2a" and bool(int.from_bytes(payload[6:8], "little") & 0x3fff) and bool(int.from_bytes(payload[8:10], "little") & 0x3fff)
            elif kind == b"VP8L":
                image |= length >= 5 and payload[0] == 0x2f
            offset = end + (length % 2)
        return ext if image and offset == len(data) else ""
    return ""


async def download_poster(poster_url: str, series_dir: Path, *, referer: str = "", season_dir: Path | None = None) -> bool:
    """Publish poster.jpg/png/webp atomically; missing/failed artwork is nonfatal.

    Validation checks the image container, not full image decoding. Network and
    filesystem failures return False; task cancellation still propagates.
    """
    parsed = urlparse(poster_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    temporary: Path | None = None
    try:
        headers = {"Accept": "image/jpeg,image/png,image/webp"}
        if referer:
            headers["Referer"] = referer
        async with asyncio.timeout(POSTER_TIMEOUT):
            async with new_client(timeout=8.0, headers=headers, trust_env=False) as client:
                async with client.stream("GET", poster_url) as response:
                    response.raise_for_status()
                    mime = response.headers.get("content-type", "")
                    if mime.split(";", 1)[0].strip().lower() not in _MIME_EXT:
                        return False
                    declared = response.headers.get("content-length")
                    if declared and int(declared) > MAX_POSTER_BYTES:
                        return False
                    data = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                        if len(data) + len(chunk) > MAX_POSTER_BYTES:
                            return False
                        data.extend(chunk)
        extension = _image_extension(bytes(data), mime)
        if not extension:
            return False
        destinations = [Path(series_dir)]
        if season_dir is not None:
            destinations.append(Path(season_dir))
        for destination in destinations:
            destination.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=destination, prefix=".poster-", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination / f"poster.{extension}")
            temporary = None
            # Remove obsolete variants only after the new source image is published.
            for other in _MIME_EXT.values():
                if other != extension:
                    (destination / f"poster.{other}").unlink(missing_ok=True)
        return True
    except Exception as exc:  # artwork must never fail the episode delivery
        log.warning("Source poster download failed (%s)", type(exc).__name__)
        return False
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _valid_thumbnail(path: Path) -> bool:
    return (path.is_file() and 0 < path.stat().st_size <= MAX_POSTER_BYTES
            and _image_extension(path.read_bytes(), "image/jpeg") == "jpg")


async def create_episode_thumbnail(video_path: Path, duration: int = 0) -> bool:
    """Create a Jellyfin '<episode>-thumb.jpg' from one frame, nonfatally."""
    video_path = Path(video_path)
    destination = video_path.with_name(video_path.stem + "-thumb.jpg")
    temporary: Path | None = None
    proc = None
    try:
        if _valid_thumbnail(destination):
            return True
        if not video_path.is_file():
            return False
        # A known short episode seeks near its beginning; an unknown clip starts
        # at zero so even sub-second files can yield a frame.
        seek = min(60.0, max(0.0, duration * 0.1))
        with tempfile.NamedTemporaryFile(dir=video_path.parent, prefix=".thumb-", suffix=".jpg", delete=False) as handle:
            temporary = Path(handle.name)
        proc = await asyncio.create_subprocess_exec(
            FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-threads", "1", "-ss", f"{seek:.3f}", "-i", str(video_path),
            "-map", "0:v:0", "-frames:v", "1", "-an", "-sn",
            "-vf", "scale=min(640\\,iw):-2", "-filter_threads", "1",
            "-threads", "1", "-q:v", "3", "-f", "image2", str(temporary),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=20.0)
        if proc.returncode != 0 or not _valid_thumbnail(temporary):
            return False
        # Another simultaneous delivery may have published a valid thumb.
        if not _valid_thumbnail(destination):
            os.replace(temporary, destination)
            temporary = None
        return True
    except Exception as exc:
        log.warning("Episode thumbnail failed (%s)", type(exc).__name__)
        return False
    finally:
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
