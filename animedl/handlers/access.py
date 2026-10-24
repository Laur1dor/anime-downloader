"""Мидлварь доступа: пропускаем только админа, остальным — отказ."""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

log = logging.getLogger(__name__)


class AccessMiddleware(BaseMiddleware):
    """Гейт доступа. В режиме "admin" пропускает только админа; в "public" — всех."""

    def __init__(self, admin_id: int, public: bool = False):
        self.admin_id = admin_id
        self.public = public

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if not self.public and user is not None and user.id != self.admin_id:
            log.warning("Отказ в доступе: user_id=%s", user.id)
            if isinstance(event, Message):
                await event.answer("⛔ У вас нет доступа к этому боту.")
            elif isinstance(event, CallbackQuery):
                await event.answer("⛔ Нет доступа", show_alert=True)
            return None
        return await handler(event, data)


# обратная совместимость
AdminOnlyMiddleware = AccessMiddleware
