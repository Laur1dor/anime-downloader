"""Общие модели данных, которыми обмениваются слои sites/players/download."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

MEDIA_CATEGORIES = {
    "anime": ("Аниме", "Anime"),
    "series": ("Сериалы", "Series"),
    "anime_movies": ("Аниме-фильмы", "AnimeMovies"),
    "movies": ("Фильмы", "Movies"),
}


def is_movie(category: str) -> bool:
    if category not in MEDIA_CATEGORIES:
        raise ValueError("Неизвестная категория медиатеки")
    return category in ("anime_movies", "movies")


def anime_category_from_html(markup: str) -> str:
    """Only explicit source type metadata identifies films, never episode count."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(markup, "lxml")
    for element in soup.select("li, tr, div, span, dt"):
        text = element.get_text(" ", strip=True)
        if re.fullmatch(r"(?:тип|формат)\s*:?", text, re.I):
            sibling = element.find_next_sibling()
            text += " " + (sibling.get_text(" ", strip=True) if sibling else "")
        if len(text) <= 80 and re.match(r"^(?:тип|формат)\s*:?\s*(?:фильм|полнометражный|movie)\b", text, re.I):
            return "anime_movies"
    return "anime"


@dataclass
class EpisodeRef:
    number: int          # номер серии для человека
    db_id: str           # внутренний id серии на сайте (для /player/videos/{db_id})
    title: str = ""
    released: str = ""
    season: int | None = None


@dataclass
class PlayerButton:
    """Одна кнопка (озвучка × плеер × embed) для конкретной серии."""
    voice: str           # data-translation-title, напр. "AniLiberty"
    provider: str        # data-provider-title, напр. "Kodik" / "AniBoom" / "CVH"
    embed_url: str       # data-player (может начинаться с //)
    provider_id: str = ""

    @property
    def url(self) -> str:
        u = self.embed_url
        if u.startswith("//"):
            return "https:" + u
        return u


@dataclass
class TitleInfo:
    site: str            # ключ парсера, напр. "animego"
    url: str
    anime_id: str
    title: str
    episodes: list[EpisodeRef] = field(default_factory=list)
    poster: str = ""
    season: int | None = None
    preferred_voice: str = ""
    media_category: str = "anime"
    library_series_title: str = ""  # common title verified against source season metadata
    mal_id: int | None = None


@dataclass
class VideoVariant:
    """Готовый к скачиванию источник конкретного качества."""
    quality: int         # 360/480/720/1080 ...
    url: str             # прямая ссылка (m3u8/mp4) либо embed-страница для kind="ytdlp"
    kind: str = "hls"    # "hls" | "mp4" | "ytdlp"
    headers: dict = field(default_factory=dict)  # заголовки для скачивания (Referer и т.п.)
    format_id: str = ""  # для kind="ytdlp": конкретный формат yt-dlp (может быть пустым)
    direct: bool = False  # selected sources require the server's home connection
    embed_url: str = ""  # original normal player, for browser-bound media sessions


_SEASON_RE = re.compile(
    r"\b(?:season|сезон)\s*[:#№]?\s*(\d{1,3})\b|"
    r"\b(\d{1,3})(?:st|nd|rd|th|-?й)?\s+(?:season|сезон)\b", re.I)


def explicit_season(title: str) -> int | None:
    """Accept only an explicit season label; bare title numbers are ambiguous."""
    numbers = {int(a or b) for a, b in _SEASON_RE.findall(title)}
    return next(iter(numbers)) if len(numbers) == 1 and 1 <= next(iter(numbers)) <= 999 else None


def episode_name(title: str, number: int) -> str:
    """Keep actual source names, excluding generated episode-number labels."""
    import html
    value = " ".join(html.unescape(title or "").split())
    if not value or re.fullmatch(r"\d+|(?:серия|эпизод|episode|ep\.?)\s*\d+|\d+\s*(?:серия|эпизод)", value, re.I):
        return f"Серия {number}"
    return value[:250]


def source_mal_id(markup: str) -> int | None:
    """Use only explicit catalogue links from the source, never name search."""
    from bs4 import BeautifulSoup
    from urllib.parse import urlparse
    ids = set()
    for link in BeautifulSoup(markup, 'lxml').select('a[href]'):
        url = urlparse(link['href'])
        if url.hostname in ('myanimelist.net', 'www.myanimelist.net', 'shikimori.io', 'shikimori.one', 'shikimori.me'):
            match = re.match(r'^/(?:anime|animes)/(\d+)(?:[/-]|$)', url.path)
            if match:
                ids.add(int(match[1]))
    return next(iter(ids)) if len(ids) == 1 else None


def library_title_suggestion(title: str) -> str:
    if explicit_season(title) is None:
        return title
    clean = _SEASON_RE.sub("", title).strip(" .-–—:()[]")
    return " ".join(clean.split()) or title


def library_selection_suggestion(title: str) -> tuple[str, int | None, str]:
    season = explicit_season(title)
    if season is not None:
        return library_title_suggestion(title), season, ""
    # A subtitle is only an editable proposal. The owner must confirm its meaning.
    if " — " in title:
        series, subtitle = title.rsplit(" — ", 1)
        if series.strip() and subtitle.strip():
            return series.strip(), None, subtitle.strip()
    return title, None, ""


def validate_library_details(title: str | None, season: int | None, season_name: str = "", category: str = "anime") -> None:
    if not title or not title.strip() or len(title) > 150 or any(ord(c) < 32 for c in title):
        raise ValueError("Укажи название сериала")
    if is_movie(category):
        return
    if season is not None and (type(season) is not int or not 1 <= season <= 999):
        raise ValueError("Номер сезона должен быть от 1 до 999")
    if not isinstance(season_name, str) or len(season_name) > 120 or any(ord(c) < 32 for c in season_name):
        raise ValueError("Некорректное название сезона")
    if season is None and not season_name.strip():
        raise ValueError("Укажи название или номер сезона")


def make_cache_hash(site: str, anime_id: str, episode: int,
                    voice: str, provider: str, quality: int) -> str:
    raw = f"{site}|{anime_id}|{episode}|{voice}|{provider}|{quality}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
