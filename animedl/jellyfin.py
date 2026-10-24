"""Optional notification to rescan a Jellyfin library after publishing media."""
from __future__ import annotations

import httpx

from .config import Config


async def refresh_library(cfg: Config) -> bool:
    """Return whether a scan was requested; HTTP errors are handled by the caller."""
    if not cfg.jellyfin_url or not cfg.jellyfin_api_key:
        return False
    async with httpx.AsyncClient(timeout=8.0, trust_env=False) as client:
        response = await client.post(
            cfg.jellyfin_url.rstrip("/") + "/Library/Refresh",
            headers={"Authorization": (
                'MediaBrowser Client="AnimeDownloader", Device="Bot", '
                'DeviceId="anime-bot", Version="1.0", '
                f'Token="{cfg.jellyfin_api_key}"'
            )},
        )
        response.raise_for_status()
    return True
