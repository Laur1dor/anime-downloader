"""Экстрактор Kodik.

Проверенный вживую поток (июль 2026):
  1. GET embed-страницы (kodikplayer.com/seria/{id}/{hash}/{q}) БЕЗ чужого Referer,
     чтобы сервер сгенерировал самоссылочные подписи (d=kodikplayer.com, ref="").
  2. Из HTML достаём urlParams (d, d_sign, pd, pd_sign, ref, ref_sign) и из пути id/hash/type.
  3. POST /ftor с этими параметрами -> JSON { links: { "360": [{src}], "480": ..., "720": ... } }.
  4. Каждый src (если без "//") расшифровывается: сдвиг букв на +18 в своём алфавите,
     затем base64. Итог — HLS-манифест (.m3u8) на CDN.

Подписи и ссылки CDN короткоживущие, поэтому extract() надо вызывать
непосредственно перед скачиванием.
"""
from __future__ import annotations

import base64
import json
import logging
import re
from urllib.parse import parse_qs, urlparse

from ..http import UA, new_client
from ..models import VideoVariant
from .base import ExtractError, PlayerExtractor

log = logging.getLogger(__name__)

_KODIK_HOSTS = ("kodik.info", "kodikplayer.com", "aniqit.com", "kodik.biz", "kodik.cc")
_PATH_RE = re.compile(r"/(seria|video|film|season|serial)/(\d+)/([0-9a-f]+)")
_URLPARAMS_RE = re.compile(r"urlParams\s*=\s*'([^']+)'")


def kodik_decode(src: str) -> str:
    """Расшифровать src из ответа /ftor. Зеркалит JS плеера."""
    if "//" in src:
        return src
    def shift(m: re.Match) -> str:
        ch = m.group(0)
        code = ord(ch) + 18
        boundary = 90 if ch <= "Z" else 122
        return chr(code if boundary >= code else code - 26)
    decoded = re.sub(r"[a-zA-Z]", shift, src)
    decoded += "=" * (-len(decoded) % 4)
    return base64.b64decode(decoded).decode("utf-8", "replace")


class KodikExtractor(PlayerExtractor):
    name = "Kodik"

    def can_handle(self, embed_url: str) -> bool:
        host = urlparse(self._norm(embed_url)).hostname or ""
        return any(host == h or host.endswith("." + h) for h in _KODIK_HOSTS)

    @staticmethod
    def _norm(url: str) -> str:
        return "https:" + url if url.startswith("//") else url

    async def extract(self, embed_url: str) -> list[VideoVariant]:
        url = self._norm(embed_url)
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.hostname}"

        m = _PATH_RE.search(parsed.path)
        if not m:
            raise ExtractError(f"Kodik: не разобрал путь embed: {parsed.path}")
        vtype, vid, vhash = m.group(1), m.group(2), m.group(3)
        # для /season/ нужен номер эпизода из query (?episode=N)
        episode = parse_qs(parsed.query).get("episode", [None])[0]

        # Важно: НЕ передаём Referer чужого сайта — иначе подписи не совпадут и /ftor -> 500.
        async with new_client() as client:
            r = await client.get(url)
            if r.status_code != 200:
                raise ExtractError(f"Kodik: embed вернул {r.status_code}")
            mm = _URLPARAMS_RE.search(r.text)
            if not mm:
                raise ExtractError("Kodik: не нашёл urlParams на embed-странице")
            up = json.loads(mm.group(1))

            data = {
                "d": up["d"], "d_sign": up["d_sign"],
                "pd": up["pd"], "pd_sign": up["pd_sign"],
                "ref": up.get("ref", ""), "ref_sign": up["ref_sign"],
                "bad_user": "false", "cdn_is_working": "true",
                "type": vtype, "hash": vhash, "id": vid,
            }
            if episode is not None:
                data["episode"] = episode
            headers = {
                "User-Agent": UA,
                "Referer": url,
                "Origin": origin,
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded",
            }
            rp = await client.post(f"{origin}/ftor", data=data, headers=headers)
            if not rp.headers.get("content-type", "").startswith("application/json"):
                raise ExtractError(f"Kodik: /ftor вернул {rp.status_code} (не JSON)")
            payload = rp.json()

        links = payload.get("links") or {}
        variants: list[VideoVariant] = []
        for q, arr in links.items():
            if not arr:
                continue
            try:
                quality = int(re.sub(r"\D", "", str(q)) or 0)
            except ValueError:
                quality = 0
            src = kodik_decode(arr[0]["src"])
            if src.startswith("//"):
                src = "https:" + src
            kind = "hls" if ".m3u8" in src or "hls" in src else "mp4"
            variants.append(VideoVariant(
                quality=quality, url=src, kind=kind,
                headers={"Referer": origin + "/", "User-Agent": UA},
            ))
        variants.sort(key=lambda v: v.quality, reverse=True)
        if not variants:
            raise ExtractError("Kodik: /ftor не вернул ссылок")
        log.info("Kodik: %s -> качества %s", vid, [v.quality for v in variants])
        return variants
