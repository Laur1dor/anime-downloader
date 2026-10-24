"""Парсер YummyAnime.

Домены:
  * yummyanime.tv      — движок DLE. Тайтл: /{id}-{slug}.html
  * old.yummyani.me    — движок «yani». Тайтл: /catalog/item/{slug}

Оба отдают плеер через AJAX. DLE использует Kodik, а yani API также
отдаёт отдельные ссылки Alloha для каждой серии и озвучки.
Внутри серила:
  * <option data-translation-type="voice"> — озвучки (title, media_id, media_hash);
    каждая озвучка — свой под-серил /serial/{media_id}/{media_hash}.
  * в под-сериле озвучки <option data-hash> — серии, каждая = /seria/{id}/{hash},
    что напрямую скармливается общему KodikExtractor.

Так переиспользуется готовый Kodik-экстрактор: сайт-парсер лишь строит матрицу
озвучка × серия → seria-embed.
"""
from __future__ import annotations

import asyncio
import html as html_lib
import json
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from ..http import new_client
from ..artwork import extract_poster_url
from ..models import EpisodeRef, PlayerButton, TitleInfo, explicit_season, library_title_suggestion, anime_category_from_html, episode_name, source_mal_id
from .base import ParseError, SiteParser

log = logging.getLogger(__name__)

_KODIK_SERIAL_RE = re.compile(r"kodik\w*\.\w+/serial/\d+/[0-9a-f]+")


@dataclass
class _Voice:
    title: str
    media_id: str
    media_hash: str
    episodes: dict[int, tuple[str, str]] = field(default_factory=dict)  # num -> (seria_id, hash)
    episode_titles: dict[int, str] = field(default_factory=dict)
    provider: str = "Kodik"
    embed_urls: dict[int, str] = field(default_factory=dict)


def _parse_voices(html: str) -> list[_Voice]:
    soup = BeautifulSoup(html, "lxml")
    voices: list[_Voice] = []
    seen: set[str] = set()
    for o in soup.select('option[data-translation-type="voice"]'):
        mid = o.get("data-media-id")
        mhash = o.get("data-media-hash")
        title = (o.get("data-title") or o.get_text(strip=True) or "—").strip()
        if not mid or not mhash or mid in seen:
            continue
        seen.add(mid)
        voices.append(_Voice(title=title, media_id=mid, media_hash=mhash))
    return voices


def _parse_episodes(html: str) -> dict[int, tuple[str, str]]:
    soup = BeautifulSoup(html, "lxml")
    eps: dict[int, tuple[str, str]] = {}
    for o in soup.select("option[data-hash]"):
        if o.get("data-translation-type"):
            continue
        sid, shash = o.get("data-id"), o.get("data-hash")
        if not sid or not shash:
            continue
        try:
            num = int(o.get("value") or 0)
        except ValueError:
            continue
        if num and num not in eps:
            eps[num] = (sid, shash)
    return eps


def _parse_episode_titles(html: str) -> dict[int, str]:
    """Keep named Kodik episodes; ordinary numbered options are not names."""
    soup = BeautifulSoup(html, "lxml")
    titles: dict[int, str] = {}
    for option in soup.select("option[data-hash][data-id]"):
        if option.get("data-translation-type"):
            continue
        try:
            number = int(option.get("value") or 0)
        except ValueError:
            continue
        if number <= 0:
            continue
        for raw in (option.get("data-title"), option.get_text(" ", strip=True)):
            name = episode_name(raw or "", number)
            if name != f"Серия {number}":
                titles.setdefault(number, name)
                break
    return titles


def _parse_api_players(entries: list) -> tuple[list[tuple[str, str]], list[_Voice]]:
    """Keep each provider's real episode URLs and voice, without shifting episode 0."""
    kodik: dict[str, str] = {}
    films: dict[str, _Voice] = {}
    alloha: dict[str, _Voice] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        data = entry.get("data") or {}
        if not isinstance(data, dict):
            continue
        player = str(data.get("player") or "").lower()
        dub = str(data.get("dubbing") or "—").strip()
        iframe = entry.get("iframe_url") or ""
        if not isinstance(iframe, str) or not iframe:
            continue
        iframe = "https:" + iframe if iframe.startswith("//") else iframe
        host = (urlparse(iframe).hostname or "").lower()
        if "kodik" in player and "kodik" in host:
            # Standalone Kodik films have no serial/episode <option> elements.
            # Keep their actual embed instead of trying to enumerate a series.
            if re.match(r"^/(?:video|film)/\d+/[0-9a-f]+(?:/|$)", urlparse(iframe).path):
                voice = films.setdefault(dub, _Voice(dub, "", "", provider="Kodik"))
                voice.embed_urls.setdefault(1, iframe)
                voice.episodes.setdefault(1, ("", ""))
            else:
                kodik.setdefault(dub, iframe.split("?")[0])
        elif ("alloha" in player or "aloha" in player) and "alloha" in host:
            try:
                number = int(str(entry.get("number")))
            except (TypeError, ValueError):
                continue
            # Recaps/specials labelled 0 must not shift regular season episodes.
            if number <= 0:
                continue
            voice = alloha.setdefault(dub, _Voice(dub, "", "", provider="Alloha"))
            voice.embed_urls.setdefault(number, iframe)
            voice.episodes.setdefault(number, ("", ""))
    return list(kodik.items()), [*films.values(), *alloha.values()]


# DLE-движки (controller.php?mod=kodik-player -> Kodik-серил): yummyanime.tv и animego.online.
_DLE_HOSTS = ("yummyanime.tv", "animego.online")


def _is_dle(host: str) -> bool:
    return any(h in host for h in _DLE_HOSTS)


class YummyAnimeParser(SiteParser):
    key = "yummyanime"
    # Один движок обслуживает несколько сайтов (yummyanime.tv/yummyani.me/old +
    # animego.online — тот же DLE-движок, что yummyanime.tv).
    domains = ("yummyanime.tv", "yummyani.me", "old.yummyani.me", "www.yummyanime.tv",
               "animego.online", "www.animego.online")

    def __init__(self) -> None:
        self._cache: dict[str, list[_Voice]] = {}  # ключ = url тайтла
        self._categories: dict[str, str] = {}
        self._season_details: dict[str, tuple[str, int | None]] = {}
        self._mal_ids: dict[str, int | None] = {}
        self._api_voices: dict[str, list[_Voice]] = {}

    @staticmethod
    def _base(url: str) -> str:
        m = re.match(r"(https?://[^/]+)", url)
        return m.group(1) if m else "https://yummyanime.tv"

    @staticmethod
    def _site_key(url: str) -> str:
        host = urlparse(url).hostname or ""
        return "animego_online" if "animego.online" in host else "yummyanime"

    @staticmethod
    def _anime_id(url: str) -> str:
        host = urlparse(url).hostname or ""
        if _is_dle(host):
            m = re.search(r"/(\d+)-", url)
            return m.group(1) if m else url.rstrip("/").split("/")[-1]
        # old.yummyani.me: /catalog/item/{slug} — id = slug
        return url.rstrip("/").split("/")[-1]

    async def _enumerate_voices(self, client, base: str, url: str) -> tuple[str, list[tuple[str, str]], str]:
        """Вернуть (название тайтла, [(озвучка, kodik_url)], постер) — по-разному на движках."""
        host = urlparse(base).hostname or ""

        if _is_dle(host):
            # DLE: controller.php?mod=kodik-player -> общий Kodik-серил с озвучками
            m = re.search(r"/(\d+)-", url)
            if not m:
                raise ParseError("Не нашёл id в ссылке")
            ep = f"{base}/engine/ajax/controller.php?mod=kodik-player&url=1&action=iframe&id={m.group(1)}"
            r = await client.get(ep, headers={"X-Requested-With": "XMLHttpRequest", "Referer": url})
            try:
                serial = (r.json() or {}).get("data") or ""
            except json.JSONDecodeError:
                serial = ""
            if not serial:
                raise ParseError("Пустой ответ kodik-player (нет озвучек Kodik?)")
            serial = "https:" + serial if serial.startswith("//") else serial
            rr = await client.get(serial)
            shost = urlparse(serial).scheme + "://" + urlparse(serial).hostname
            title = self._html_title(rr.text)
            voices = [(v.title, f"{shost}/serial/{v.media_id}/{v.media_hash}/720p")
                      for v in _parse_voices(rr.text)]
            poster = ""
            try:
                page = await client.get(url)
                if page.status_code == 200:
                    poster = extract_poster_url(page.text, url)
                    self._categories[url] = anime_category_from_html(page.text)
                    title = self._source_title(page.text) or title
                    self._season_details[url] = self._source_season_details(page.text, title)
                    self._mal_ids[url] = source_mal_id(page.text)
            except Exception:
                log.debug("YummyAnime: постер страницы недоступен")
            return title, voices, poster

        # old.yummyani.me: React SPA, API /api/anime/{internal_id}/videos
        r = await client.get(url)
        self._categories[url] = anime_category_from_html(r.text)
        title = self._source_title(r.text) or self._html_title(r.text)
        self._season_details[url] = self._source_season_details(r.text, title)
        self._mal_ids[url] = source_mal_id(r.text)
        mid = re.search(r'data-id="(\d+)"', r.text)
        if not mid:
            raise ParseError("YummyAnime(old): не нашёл internal id на странице")
        api = f"{base}/api/anime/{mid.group(1)}/videos"
        ra = await client.get(api, headers={"X-Requested-With": "XMLHttpRequest", "Referer": url})
        try:
            entries = (ra.json() or {}).get("response") or []
        except json.JSONDecodeError:
            raise ParseError("YummyAnime(old): битый ответ API videos")
        if not isinstance(entries, list):
            raise ParseError("YummyAnime(old): неожиданный формат API videos")
        voice_urls, direct_voices = _parse_api_players(entries)
        self._api_voices[url] = direct_voices
        return title, voice_urls, extract_poster_url(r.text, url)

    @staticmethod
    def _source_season_details(raw: str, title: str) -> tuple[str, int | None]:
        """Read explicit alternative-title season labels, never bare title digits alone."""
        soup = BeautifulSoup(raw, "lxml")
        labels = [title]
        for element in soup.select(".alt-names-list li"):
            text = element.get_text(" ", strip=True)
            if len(text) <= 250 and explicit_season(text) is not None:
                labels.append(text)
        numbers = {explicit_season(label) for label in labels if explicit_season(label) is not None}
        if len(numbers) != 1:
            return title, explicit_season(title)
        season = numbers.pop()
        common = library_title_suggestion(title)
        # An explicit English alternate name corroborates 'Russian title 3'.
        common = re.sub(r"\s+" + str(season) + r"$", "", common).strip() or title
        return common, season

    @staticmethod
    def _source_title(raw: str) -> str:
        """Keep arc subtitles from the source H1 instead of splitting on every dash."""
        soup = BeautifulSoup(raw, "lxml")
        h1 = soup.select_one("h1")
        if h1 and h1.get_text(strip=True):
            return h1.get_text(" ", strip=True)
        meta = soup.select_one('meta[property="og:title"]')
        if meta and meta.get("content"):
            title = html_lib.unescape(str(meta["content"]))
            # Strip only the site SEO suffix, never a subtitle such as '— Бедствие'.
            return re.split(r"\s+[—–|-]\s+смотреть\b", title, maxsplit=1, flags=re.I)[0].strip()
        return ""

    @staticmethod
    def _html_title(raw: str) -> str:
        m = re.search(r"<title>(.*?)</title>", raw, re.S)
        if not m:
            return ""
        # берём часть до разделителя и чистим HTML-сущности
        title = html_lib.unescape(m.group(1)).strip()
        return re.split(r"\s+[—–|-]\s+", title)[0].strip() or title

    async def fetch_title(self, url: str) -> TitleInfo:
        base = self._base(url)
        async with new_client() as client:
            title_name, voice_urls, poster = await self._enumerate_voices(client, base, url)
            voices = [_Voice(title=t, media_id="", media_hash="") for t, _ in voice_urls]
            urls = [u for _, u in voice_urls]
            sem = asyncio.Semaphore(8)

            async def load(v: _Voice, vu: str) -> None:
                async with sem:
                    try:
                        rr = await client.get(vu)
                        v.episodes = _parse_episodes(rr.text)
                        v.episode_titles = _parse_episode_titles(rr.text)
                    except Exception as e:  # noqa: BLE001
                        log.warning("YummyAnime: не загрузил озвучку %s: %s", v.title, e)

            await asyncio.gather(*(load(v, u) for v, u in zip(voices, urls)))
            voices.extend(self._api_voices.get(url, []))

        voices = [v for v in voices if v.episodes]
        if not voices:
            raise ParseError("YummyAnime: не удалось получить серии ни для одной озвучки")

        anime_id = self._anime_id(url)
        self._cache[url] = voices          # ключ = url (id может совпасть у разных сайтов)

        all_nums = sorted({n for v in voices for n in v.episodes})
        names: dict[int, str] = {}
        for voice in voices:
            for number, name in voice.episode_titles.items():
                names.setdefault(number, name)
        episodes = [EpisodeRef(number=n, db_id=str(n), title=names.get(n, f"{n} серия")) for n in all_nums]
        title_name = title_name or f"Anime {anime_id}"
        common_title, source_season = self._season_details.get(url, (title_name, explicit_season(title_name)))
        log.info("%s: '%s' озвучек=%d серий=%d", self._site_key(url), title_name,
                 len(voices), len(episodes))
        return TitleInfo(site=self._site_key(url), url=url, anime_id=anime_id,
                         title=title_name, episodes=episodes, season=source_season, poster=poster,
                         library_series_title=common_title if source_season is not None else "",
                         media_category=self._categories.get(url, "anime"), mal_id=self._mal_ids.get(url))

    async def fetch_episode_players(
        self, title: TitleInfo, episode: EpisodeRef
    ) -> list[PlayerButton]:
        voices = self._cache.get(title.url)
        if voices is None:
            # кэш утерян — перечитываем тайтл
            await self.fetch_title(title.url)
            voices = self._cache.get(title.url, [])
        buttons: list[PlayerButton] = []
        for v in voices:
            direct = v.embed_urls.get(episode.number)
            if direct:
                buttons.append(PlayerButton(voice=v.title, provider=v.provider, embed_url=direct))
                continue
            pair = v.episodes.get(episode.number)
            if not pair:
                continue
            sid, shash = pair
            buttons.append(PlayerButton(
                voice=v.title, provider="Kodik",
                embed_url=f"//kodikplayer.com/seria/{sid}/{shash}/720p",
            ))
        return buttons
