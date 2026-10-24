"""Sopranos catalogue: published season links and Dooplay's player AJAX."""
from __future__ import annotations

import re
import json
from urllib.parse import urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from ..artwork import extract_poster_url
from ..http import new_client
from ..models import EpisodeRef, PlayerButton, TitleInfo
from .base import ParseError, SiteParser
from .sopranos_session import fetch_page


_HOST = "watch-the-sopranos-online.org"
_EPISODE = re.compile(r"^/episodes/(\d+)-sezon-(\d+)-episode/?$")
_SEASON = re.compile(r"^/seasons/(\d+)-season(?:-?\d+)?/?$")


def _local_url(href: str, base: str) -> str | None:
    parsed = urlparse(urljoin(base, href))
    if parsed.scheme not in ("http", "https") or parsed.hostname not in (_HOST, "www." + _HOST):
        return None
    return urlunparse(("https", _HOST, parsed.path, "", "", ""))


def parse_season_links(html: str, base: str) -> dict[int, str]:
    """Use real navigation URLs (season 2 and 6 have irregular slugs)."""
    seasons: dict[int, str] = {}
    soup = BeautifulSoup(html, "lxml")
    for anchor in soup.select('a[href*="/seasons/"]'):
        url = _local_url(anchor.get("href", ""), base)
        match = _SEASON.fullmatch(urlparse(url).path) if url else None
        if match:
            seasons.setdefault(int(match[1]), url)
    return dict(sorted(seasons.items()))


def parse_episodes(html: str, base: str, *, season: int) -> list[EpisodeRef]:
    """Only episode cards, excluding next/previous links and the spin-off film."""
    soup = BeautifulSoup(html, "lxml")
    episodes: dict[int, EpisodeRef] = {}
    for card in soup.select("ul.episodios > li"):
        anchor = card.select_one(".episodiotitle a[href]") or card.select_one("a[href]")
        url = _local_url(anchor.get("href", ""), base) if anchor else None
        match = _EPISODE.fullmatch(urlparse(url).path) if url else None
        if not match or int(match[1]) != season:
            continue
        number = int(match[2])
        date = card.select_one(".date")
        episodes.setdefault(number, EpisodeRef(
            number=number, db_id=url, season=season,
            title=anchor.get_text(" ", strip=True),
            released=date.get_text(strip=True) if date else "",
        ))
    return [episodes[number] for number in sorted(episodes)]


class SopranosParser(SiteParser):
    key = "sopranos"
    domains = (_HOST,)

    @staticmethod
    async def _page(client, url: str) -> str:
        html = await fetch_page(client, url)
        soup = BeautifulSoup(html, "lxml")
        if not soup.select_one("#single"):
            raise ParseError("Сайт «Клан Сопрано» не отдал каталог: возможно, требуется проверка в браузере.")
        return html

    async def fetch_title(self, url: str) -> TitleInfo:
        canonical = _local_url(url, "https://" + _HOST)
        path = urlparse(canonical).path if canonical else ""
        ep_match, season_match = _EPISODE.fullmatch(path), _SEASON.fullmatch(path)
        if not canonical or (path != "/" and not ep_match and not season_match):
            raise ParseError("Пришли ссылку на серию или сезон «Клана Сопрано».")
        selected_season = int(ep_match[1] if ep_match else season_match[1]) if (ep_match or season_match) else None
        async with new_client(trust_env=False) as client:
            html = await self._page(client, canonical)
            soup = BeautifulSoup(html, "lxml")
            show_name = soup.select_one('#single[itemtype$="TVSeries"] > meta[itemprop="name"]')
            name = show_name.get("content", "").strip() if show_name else "Клан Сопрано"
            season_links = parse_season_links(html, canonical)
            if not season_links:
                raise ParseError("Сайт «Клан Сопрано»: не найден список сезонов.")
            all_episodes: list[EpisodeRef] = []
            poster = ""
            for season, season_url in season_links.items():
                # An episode page already contains the entire current season.
                season_html = html if selected_season == season else await self._page(client, season_url)
                episodes = parse_episodes(season_html, season_url, season=season)
                if not episodes:
                    raise ParseError(f"Сайт «Клан Сопрано»: нет списка серий сезона {season}.")
                all_episodes.extend(episodes)
                # Episode OG images are screenshots, not series posters.
                if not poster and (not ep_match or selected_season != season):
                    poster = extract_poster_url(season_html, season_url)
        return TitleInfo(site=self.key, url=canonical, anime_id="the-sopranos", title=name,
                         episodes=all_episodes, season=selected_season,
                         preferred_voice="Гоблин", poster=poster, media_category="series")

    async def fetch_episode_players(self, title: TitleInfo, episode: EpisodeRef) -> list[PlayerButton]:
        url = _local_url(episode.db_id, title.url)
        if not url or not _EPISODE.fullmatch(urlparse(url).path):
            raise ParseError("Сайт «Клан Сопрано»: некорректная ссылка серии.")
        async with new_client(trust_env=False) as client:
            html = await self._page(client, url)
            soup = BeautifulSoup(html, "lxml")
            options = soup.select("li.dooplay_player_option[data-post][data-nume][data-type]")
            options.sort(key=lambda option: option.get("data-name", "").casefold() != "гоблин")
            ajax_match = re.search(r"var\s+dtAjax\s*=\s*", html)
            ajax_url = None
            if ajax_match:
                try:
                    ajax_data = json.JSONDecoder().raw_decode(html[ajax_match.end():])[0]
                    ajax_url = _local_url(ajax_data.get("url", ""), url)
                except (ValueError, AttributeError):
                    pass
            buttons = []
            for option in options:
                voice = option.get("data-name", "") or option.get_text(" ", strip=True)
                iframe = soup.select_one("#dooplay_player_response iframe[src]") if "on" in option.get("class", []) else None
                embed = iframe.get("src", "") if iframe else ""
                if not embed and ajax_url:
                    response = await client.post(ajax_url, data={
                        "action": "doo_player_ajax", "post": option["data-post"],
                        "nume": option["data-nume"], "type": option["data-type"],
                    }, headers={"Referer": url, "X-Requested-With": "XMLHttpRequest"})
                    if response.status_code == 200:
                        frame = BeautifulSoup(response.text, "lxml").select_one("iframe[src]")
                        if frame:
                            embed = frame.get("src", "")
                        else:
                            try:
                                payload = response.json()
                                embed = payload.get("embed_url", "")
                                if "<iframe" in embed:
                                    frame = BeautifulSoup(embed, "lxml").select_one("iframe[src]")
                                    embed = frame.get("src", "") if frame else ""
                            except (ValueError, AttributeError, TypeError):
                                pass
                parsed = urlparse(urljoin(url, embed))
                if embed and parsed.scheme in ("http", "https") and parsed.hostname:
                    provider = "PLPlayer" if parsed.hostname == "plplayer.online" else parsed.hostname
                    buttons.append(PlayerButton(voice=voice, provider=provider,
                                                embed_url=urljoin(url, embed), provider_id=option["data-nume"]))
                elif voice.casefold() == "гоблин":
                    raise ParseError("Сайт «Клан Сопрано»: плеер Гоблина не отдал ссылку на видео.")
        if not buttons or not any(button.voice.casefold() == "гоблин" for button in buttons):
            raise ParseError("На странице «Клана Сопрано» отсутствует плеер Гоблина. Скачивание пока недоступно.")
        return buttons
