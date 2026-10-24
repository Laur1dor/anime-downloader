"""Optional episode names from Jikan, keyed only by an exact MyAnimeList ID."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .http import new_client
from .models import episode_name

log = logging.getLogger(__name__)
_lock = asyncio.Lock()
_last_request = 0.0
_failures: dict[tuple[str, int], float] = {}
_SUCCESS_TTL = 24 * 3600
_EMPTY_TTL = 3600
_FAILURE_TTL = 600
_MAX_PAGES = 5


def _parse_mal_page(markup: str, mal_id: int) -> dict[int, str]:
    """Read only episode-table links whose entry and displayed number agree."""
    soup = BeautifulSoup(markup, "lxml")
    names = {}
    for cell in soup.select("td.episode-title.fs12"):
        row = cell.find_parent("tr")
        number_cell = row.select_one("td.episode-number[data-raw]") if row else None
        link = cell.find("a", href=True, recursive=False)
        if number_cell is None or link is None:
            continue
        parsed = urlparse(urljoin("https://myanimelist.net", link["href"]))
        match = re.fullmatch(r"/anime/(\d+)/[^/]+/episode/(\d+)/?", parsed.path)
        if parsed.scheme != "https" or parsed.hostname != "myanimelist.net" or not match:
            continue
        entry_id, number = map(int, match.groups())
        if entry_id != mal_id or number <= 0 or number_cell.get("data-raw") != str(number):
            continue
        name = episode_name(link.get_text(" ", strip=True), number)
        if name != f"Серия {number}":
            names[number] = name
    return names


def _read_cache(path: Path, mal_id: int) -> tuple[dict[int, str], float]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("mal_id") != mal_id:
            return {}, 0
        names = {}
        for key, title in data["names"].items():
            number = int(key)
            if number > 0 and isinstance(title, str):
                name = episode_name(title, number)
                if name != f"Серия {number}":
                    names[number] = name
        return names, float(data["expires_at"])
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {}, 0


def _write_cache(path: Path, mal_id: int, names: dict[int, str], ttl: int) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps({
            "mal_id": mal_id, "provider": "jikan-or-myanimelist", "fetched_at": time.time(),
            "expires_at": time.time() + ttl, "names": names,
        }, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    except OSError as exc:
        log.debug("Episode metadata cache unavailable: %s", type(exc).__name__)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


async def get_episode_names(mal_id: int, cache_dir: Path) -> dict[int, str]:
    """Return exact-ID English episode names; outages never fail a download.

    Source episode names take precedence at the caller. No title searches or
    season-number arithmetic are performed here: episode numbers are exactly
    those listed for this MAL entry. Cached names survive provider outages.
    """
    if type(mal_id) is not int or mal_id <= 0:
        return {}
    path = Path(cache_dir) / "episode-metadata" / f"mal-{mal_id}.json"
    key = (str(path), mal_id)
    stale, expires = _read_cache(path, mal_id)
    if expires > time.time() or _failures.get(key, 0) > time.monotonic():
        return stale
    # Serialize requests globally and recheck cache after waiting, avoiding
    # simultaneous duplicate lookups and respecting Jikan's rate limit.
    async with _lock:
        stale, expires = _read_cache(path, mal_id)
        if expires > time.time() or _failures.get(key, 0) > time.monotonic():
            return stale
        names: dict[int, str] = {}
        complete = False
        try:
            async with new_client(timeout=6.0, trust_env=False) as client:
                for page in range(1, _MAX_PAGES + 1):
                    global _last_request
                    delay = 1.05 - (time.monotonic() - _last_request)
                    if delay > 0:
                        await asyncio.sleep(delay)
                    _last_request = time.monotonic()
                    response = await client.get(
                        f"https://api.jikan.moe/v4/anime/{mal_id}/episodes",
                        params={"page": page},
                    )
                    response.raise_for_status()
                    payload = response.json()
                    entries = payload["data"]
                    if not isinstance(entries, list):
                        raise ValueError("Invalid episode list")
                    for entry in entries:
                        number, title = entry.get("mal_id"), entry.get("title")
                        if type(number) is not int or number <= 0 or not isinstance(title, str):
                            continue
                        name = episode_name(title, number)
                        if name != f"Серия {number}":
                            names[number] = name
                    pagination = payload["pagination"]
                    has_next = pagination["has_next_page"]
                    if type(has_next) is not bool:
                        raise ValueError("Invalid pagination")
                    if not has_next:
                        complete = True
                        break
        except Exception as exc:
            # Do not log URLs, response bodies, user data or credentials.
            log.info("Optional episode metadata unavailable: %s", type(exc).__name__)
        if not complete or not names:
            try:
                # MAL uses the configured metadata/network route (WARP on the
                # server); Jikan above stays direct. Never alter global routes.
                async with new_client(timeout=6.0, trust_env=True) as client:
                    response = await client.get(f"https://myanimelist.net/anime/{mal_id}/_/episode")
                    response.raise_for_status()
                    fallback = _parse_mal_page(response.text, mal_id)
                    if fallback:
                        # Direct MAL episode listings are limited to one page.
                        # Keep partial results on the shorter retry interval.
                        names = {**fallback, **names}
                        complete = len(fallback) < 100
            except Exception as exc:
                log.info("Optional MAL episode metadata unavailable: %s", type(exc).__name__)
        if complete:
            _failures.pop(key, None)
            if not names and stale:
                _write_cache(path, mal_id, stale, _EMPTY_TTL)
                return stale
            _write_cache(path, mal_id, names, _SUCCESS_TTL if names else _EMPTY_TTL)
            return names
        merged = {**stale, **names}
        _failures[key] = time.monotonic() + _FAILURE_TTL
        _write_cache(path, mal_id, merged, _FAILURE_TTL)
        return merged
