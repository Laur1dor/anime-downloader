"""Интерфейс парсера сайта. Новый сайт = новый подкласс + регистрация домена."""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import EpisodeRef, PlayerButton, TitleInfo


class ParseError(Exception):
    """Не удалось разобрать страницу — вероятно, сайт изменил структуру."""


class SiteParser(ABC):
    key: str = "base"          # короткий идентификатор парсера (входит в hash кэша)
    domains: tuple[str, ...] = ()

    @abstractmethod
    async def fetch_title(self, url: str) -> TitleInfo:
        """По ссылке на тайтл вернуть название, anime_id и список серий."""
        ...

    @abstractmethod
    async def fetch_episode_players(
        self, title: TitleInfo, episode: EpisodeRef
    ) -> list[PlayerButton]:
        """Вернуть все кнопки (озвучка×плеер) для конкретной серии."""
        ...
