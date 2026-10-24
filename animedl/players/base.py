"""Интерфейс экстрактора плеера: embed-url -> список VideoVariant по качествам."""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import VideoVariant


class ExtractError(Exception):
    """Не удалось достать видео из плеера."""


class PlayerExtractor(ABC):
    name: str = "base"

    @abstractmethod
    def can_handle(self, embed_url: str) -> bool:
        ...

    @abstractmethod
    async def extract(self, embed_url: str) -> list[VideoVariant]:
        """Вернуть варианты видео (разные качества). Пусто -> не смог."""
        ...
