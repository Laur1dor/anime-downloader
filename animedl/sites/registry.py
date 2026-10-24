"""Определение парсера по домену ссылки."""
from __future__ import annotations

from urllib.parse import urlparse

from .animego import AnimeGoParser
from .base import SiteParser
from .yummyanime import YummyAnimeParser
from .sopranos import SopranosParser

# Зарегистрированные парсеры. Добавление сайта = импорт нового класса сюда.
_PARSERS: list[SiteParser] = [
    AnimeGoParser(),
    YummyAnimeParser(),
    SopranosParser(),
]

# Домены, которые движок узнаёт, но парсер ещё не реализован (для понятной ошибки).
_KNOWN_UNIMPLEMENTED: dict[str, str] = {}


class UnsupportedSite(Exception):
    pass


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def get_parser(url: str) -> SiteParser:
    host = _host(url)
    for p in _PARSERS:
        for d in p.domains:
            d = d.lower().removeprefix("www.")
            if host == d or host.endswith("." + d):
                return p
    if host in _KNOWN_UNIMPLEMENTED:
        raise UnsupportedSite(
            f"Сайт {host} ({_KNOWN_UNIMPLEMENTED[host]}) пока не поддержан. "
            f"Поддержан: animego.me / animego.org."
        )
    raise UnsupportedSite(f"Неизвестный сайт: {host or url}")
