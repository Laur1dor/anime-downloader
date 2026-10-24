"""Парсер AnimeGO (animego.me / animego.org — общий движок).

Проверенные эндпоинты (июль 2026):
  * Страница тайтла: /anime/{slug}-{anime_id}
  * Список серий + плееры первой серии: GET /player/{anime_id}  (JSON, data.content = HTML)
  * Плееры конкретной серии:            GET /player/videos/{episode_db_id}
Все AJAX-запросы требуют заголовок X-Requested-With: XMLHttpRequest и Referer тайтла.

В content каждая кнопка плеера — элемент с data-атрибутами:
  data-translation-title  — название озвучки (напр. "AniLiberty")
  data-provider-title     — плеер (Kodik / AniBoom / CVH)
  data-provider           — числовой id провайдера
  data-player             — embed-url (часто начинается с //)
"""
from __future__ import annotations

import json
import logging
import re
import httpx

from bs4 import BeautifulSoup

from ..http import new_client
from ..artwork import extract_poster_url
from ..models import EpisodeRef, PlayerButton, TitleInfo, explicit_season, anime_category_from_html, source_mal_id
from .base import ParseError, SiteParser

log = logging.getLogger(__name__)

_ID_RE = re.compile(r"/anime/[a-z0-9\-]+-(\d+)")


class AnimeGoParser(SiteParser):
    key = "animego"
    domains = ("animego.me", "animego.org", "www.animego.me", "www.animego.org")

    # базовый домен для API берём из ссылки пользователя (поддержка зеркал)
    @staticmethod
    def _base(url: str) -> str:
        m = re.match(r"(https?://[^/]+)", url)
        return m.group(1) if m else "https://animego.me"

    async def fetch_title(self, url: str) -> TitleInfo:
        base = self._base(url)
        m = _ID_RE.search(url)
        if not m:
            raise ParseError("AnimeGO: не нашёл id аниме в ссылке")
        anime_id = m.group(1)

        async with new_client() as client:
            r = await client.get(url)
            if r.status_code != 200:
                raise ParseError(f"AnimeGO: страница тайтла вернула {r.status_code}")
            soup = BeautifulSoup(r.text, "lxml")
            h1 = soup.select_one("h1")
            title = h1.get_text(strip=True) if h1 else f"anime {anime_id}"
            poster = extract_poster_url(r.text, url)
            media_category = anime_category_from_html(r.text)

            # список серий берём из /player/{anime_id}
            content = await self._get_player_content(client, base, url, f"/player/{anime_id}")
            episodes = self._parse_episodes(content)
            # The player often has numbers only; the release schedule has names.
            names = self._schedule_names(r.text)
            schedule = soup.select_one('[data-infinite-scroll-url-template-value]')
            template = schedule.get('data-infinite-scroll-url-template-value', '') if schedule else ''
            if template.startswith(f'/anime/{anime_id}/') and '__page__' in template and schedule.get('data-infinite-scroll-has-more-value') == 'true':
                try:
                    for page in range(1, min(len(episodes) + 1, 100)):
                        response = await client.get(base + template.replace('__page__', str(page)),
                            headers={'X-Requested-With': 'XMLHttpRequest', 'Referer': url})
                        response.raise_for_status()
                        data = response.json().get('data') or {}
                        names.update(self._schedule_names(data.get('content') or ''))
                        if data.get('endPage') or not data.get('content'):
                            break
                except (httpx.HTTPError, ValueError, TypeError):
                    log.warning('AnimeGO: названия из расписания временно недоступны')
            for episode in episodes:
                episode.title = episode.title or names.get(episode.number, '')

        if not episodes:
            # фильм/полнометражка без списка серий: плееры лежат в /player/{anime_id}
            # напрямую, поэтому помечаем серию маркером (см. fetch_episode_players).
            episodes = [EpisodeRef(number=1, db_id="__title__", title=title)]
        log.info("AnimeGO: '%s' id=%s серий=%d", title, anime_id, len(episodes))
        return TitleInfo(site=self.key, url=url, anime_id=anime_id,
                         title=title, episodes=episodes, season=explicit_season(title), poster=poster, media_category=media_category, mal_id=source_mal_id(r.text))

    async def fetch_episode_players(
        self, title: TitleInfo, episode: EpisodeRef
    ) -> list[PlayerButton]:
        base = self._base(title.url)
        # фильм (одна серия-маркер) -> плееры в /player/{anime_id}; сериал -> /player/videos/{db_id}
        path = (f"/player/{title.anime_id}" if episode.db_id == "__title__"
                else f"/player/videos/{episode.db_id}")
        async with new_client() as client:
            content = await self._get_player_content(client, base, title.url, path)
        buttons = self._parse_buttons(content)
        log.info("AnimeGO: серия %s -> %d кнопок", episode.number, len(buttons))
        return buttons

    # --- helpers ---

    @staticmethod
    async def _get_player_content(client, base: str, referer: str, path: str) -> str:
        headers = {"X-Requested-With": "XMLHttpRequest", "Referer": referer, "Accept": "*/*"}
        r = await client.get(base + path, headers=headers)
        if r.status_code != 200:
            raise ParseError(f"AnimeGO: {path} вернул {r.status_code}")
        ct = r.headers.get("content-type", "")
        if "application/json" in ct:
            try:
                data = r.json()
            except json.JSONDecodeError as e:
                raise ParseError(f"AnimeGO: {path} — битый JSON") from e
            # структура: {status, data: {content: "..."}}
            content = (data.get("data") or {}).get("content")
            if content is None:
                content = data.get("content", "")
            return content or ""
        return r.text

    @staticmethod
    def _schedule_names(content: str) -> dict[int, str]:
        soup = BeautifulSoup(content, 'lxml')
        names = {}
        for item in soup.select('[data-episode][data-number]'):
            previous = item.find_previous_sibling('div')
            if previous is None or 'fw-bold' not in previous.get('class', []):
                continue
            name = previous.get_text(' ', strip=True)
            try:
                number = int(item['data-number'])
            except (ValueError, TypeError):
                continue
            if name and number > 0:
                names[number] = name
        return names

    @staticmethod
    def _parse_episodes(content: str) -> list[EpisodeRef]:
        soup = BeautifulSoup(content, "lxml")
        eps: list[EpisodeRef] = []
        seen: set[str] = set()
        for el in soup.select("[data-episode]"):
            db_id = el.get("data-episode")
            num = el.get("data-episode-number")
            if not db_id or db_id in seen:
                continue
            seen.add(db_id)
            try:
                number = int(num) if num else len(eps) + 1
            except ValueError:
                number = len(eps) + 1
            eps.append(EpisodeRef(
                number=number,
                db_id=db_id,
                title=el.get("data-episode-title", "") or "",
                released=el.get("data-episode-released", "") or "",
            ))
        eps.sort(key=lambda e: e.number)
        return eps

    @staticmethod
    def _parse_buttons(content: str) -> list[PlayerButton]:
        soup = BeautifulSoup(content, "lxml")
        buttons: list[PlayerButton] = []
        for el in soup.select("[data-player]"):
            embed = el.get("data-player")
            if not embed:
                continue
            buttons.append(PlayerButton(
                voice=(el.get("data-translation-title") or "").strip() or "—",
                provider=(el.get("data-provider-title") or "").strip() or "—",
                embed_url=embed,
                provider_id=el.get("data-provider") or "",
            ))
        return buttons
