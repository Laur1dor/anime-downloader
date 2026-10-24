"""Bounded catalogue search through sources available directly from the bot host.

Search never needs environment proxy settings or catalogue API keys. A catalogue
failure is reported separately and does not discard working source results.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .http import new_client


@dataclass(frozen=True)
class SearchHit:
    site: str
    url: str
    title: str
    aliases: tuple[str, ...] = ()
    poster: str = ""
    mal_id: int | None = None


@dataclass
class SearchResponse:
    hits: list[SearchHit] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)


def _safe_url(raw: str, base: str, domains: set[str], path_pattern: str) -> str:
    value = urljoin(base, raw)
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        return ""
    if (parsed.scheme != "https" or parsed.hostname not in domains
            or parsed.username or parsed.password or port not in (None, 443)
            or not re.fullmatch(path_pattern, parsed.path)):
        return ""
    return value.split("?", 1)[0].split("#", 1)[0]


def _positive_id(value) -> int | None:
    try:
        number = int(value)
        return number if number > 0 else None
    except (TypeError, ValueError):
        return None


def parse_yummy_results(payload: dict, limit: int = 12) -> list[SearchHit]:
    rows = payload.get("response")
    if not isinstance(rows, list):
        raise ValueError("Invalid Yummy search response")
    hits = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        slug = str(row.get("anime_url") or "")
        url = _safe_url("/catalog/item/" + slug, "https://old.yummyani.me",
                        {"old.yummyani.me"}, r"/catalog/item/[\w-]+")
        title = str(row.get("title") or "").strip()
        if not url or not title:
            continue
        poster = row.get("poster") if isinstance(row.get("poster"), dict) else {}
        ids = row.get("remote_ids") if isinstance(row.get("remote_ids"), dict) else {}
        hits.append(SearchHit("yummyanime", url, title,
                              poster=urljoin("https://old.yummyani.me", str(poster.get("big") or "")) if poster.get("big") else "",
                              mal_id=_positive_id(ids.get("myanimelist_id"))))
        if len(hits) >= limit:
            break
    return hits


def parse_animego_results(markup: str, limit: int = 12) -> list[SearchHit]:
    soup = BeautifulSoup(markup, "lxml")
    hits, seen = [], set()
    for card in soup.select(".ani-grid__item"):
        anchor = card.select_one(".ani-grid__item-title a[href]")
        if not anchor:
            continue
        url = _safe_url(anchor.get("href", ""), "https://animego.me",
                        {"animego.me"}, r"/anime/[a-z0-9-]+-\d+")
        title = anchor.get_text(" ", strip=True)
        if not url or not title or url in seen:
            continue
        seen.add(url)
        alternative = card.select_one(".ani-grid__item-body .fw-lighter")
        alias = alternative.get_text(" ", strip=True) if alternative else ""
        image = card.select_one("img[src]")
        hits.append(SearchHit("animego", url, title,
                              aliases=(alias,) if alias and alias != title else (),
                              poster=urljoin("https://animego.me", image.get("src", "")) if image else ""))
        if len(hits) >= limit:
            break
    return hits


async def search_sources(query: str, limit: int = 12) -> SearchResponse:
    query = " ".join(query.split())[:120]
    limit = max(1, min(int(limit), 20))
    if len(query) < 2:
        return SearchResponse()
    errors: dict[str, str] = {}
    async with new_client(trust_env=False, timeout=6.0) as client:
        async def source(site: str, text: str) -> list[SearchHit]:
            try:
                if site == "yummyanime":
                    response = await client.get("https://old.yummyani.me/api/search", params={"q": text, "limit": limit, "offset": 0})
                    response.raise_for_status()
                    return parse_yummy_results(response.json(), limit)
                response = await client.get("https://animego.me/search/all", params={"q": text})
                response.raise_for_status()
                return parse_animego_results(response.text, limit)
            except Exception as exc:
                errors[site] = type(exc).__name__
                return []

        async def catalogue() -> list[dict]:
            try:
                response = await client.get("https://shikimori.io/api/animes", params={"search": query, "limit": 3})
                response.raise_for_status()
                rows = response.json()
                if not isinstance(rows, list):
                    raise ValueError("Invalid Shikimori search response")
                return [row for row in rows if isinstance(row, dict)]
            except Exception as exc:
                errors["shikimori"] = type(exc).__name__
                return []

        yummy, animego, catalogue_rows = await asyncio.gather(source("yummyanime", query), source("animego", query), catalogue())
        # A Russian catalogue alternative can find a source that only indexes
        # Russian titles. It generates suggestions, never assigns an identity.
        variants = list(dict.fromkeys(str(row.get("russian") or "").strip() for row in catalogue_rows))
        variants = [value for value in variants if value and value.casefold() != query.casefold()][:2]
        pending = [(site, variant) for site, hits in (("yummyanime", yummy), ("animego", animego))
                   if not hits for variant in variants]
        expanded = await asyncio.gather(*(source(site, variant) for site, variant in pending))
        aliases_by_id = {}
        for row in catalogue_rows:
            identity = _positive_id(row.get("id"))
            if identity:
                aliases_by_id[identity] = tuple(dict.fromkeys(str(row.get(key) or "").strip() for key in ("name", "russian") if row.get(key)))
        merged, seen = [], set()
        # Interleave providers so a successful first site cannot fill every slot.
        for group in (list(sum(zip(yummy, animego), ())) + yummy[len(animego):] + animego[len(yummy):], *expanded):
            for hit in group:
                if hit.url in seen:
                    continue
                seen.add(hit.url)
                aliases = tuple(dict.fromkeys((*hit.aliases, *aliases_by_id.get(hit.mal_id, ()))))
                merged.append(SearchHit(hit.site, hit.url, hit.title, aliases, hit.poster, hit.mal_id))
        return SearchResponse(merged[:limit], errors)
