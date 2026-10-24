"""Создание Bot (с поддержкой локального Bot API сервера) и отправка видео."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Awaitable, Callable

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramRetryAfter, TelegramNetworkError, TelegramServerError
from aiogram.types import FSInputFile, Message

from .config import Config
from .httpx_session import HttpxSession

log = logging.getLogger(__name__)


async def _send_with_flood_retry(factory: Callable[[], Awaitable[Message]],
                                 attempts: int = 5, *, retry_transport: bool = True) -> Message:
    """Выполнить отправку, переживая flood-control Telegram (429 retry_after)."""
    for i in range(attempts):
        try:
            return await factory()
        except TelegramRetryAfter as e:
            wait = e.retry_after + 1
            if i == attempts - 1:
                raise
            log.warning("Telegram flood-control: жду %dс и повторяю (%d/%d)",
                        wait, i + 1, attempts)
            await asyncio.sleep(wait)
        except (TelegramNetworkError, TelegramServerError):
            if not retry_transport or i >= min(attempts - 1, 2):
                raise
            log.warning("Temporary Telegram transport failure; retry %d/3", i + 2)
            await asyncio.sleep(2 ** (i + 1))
    raise RuntimeError("unreachable")


def build_bot(cfg: Config) -> Bot:
    if cfg.tg_api_base_url:
        # Локальный Bot API сервер (localhost): DPI не мешает, стримим файлы до 2 ГБ.
        api = TelegramAPIServer.from_base(cfg.tg_api_base_url.rstrip("/").removesuffix("/bot"),
                                         is_local=cfg.tg_api_local_mode)
        session = AiohttpSession(api=api)
        log.info("Использую локальный Bot API сервер: %s", cfg.tg_api_base_url)
    else:
        # Облачный api.telegram.org через httpx — обходит DPI-троттлинг, где aiohttp режется.
        session = HttpxSession()
        log.info("Использую облачный Bot API через httpx-транспорт.")
    return Bot(
        token=cfg.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode="HTML"),
    )


async def send_video_file(
    bot: Bot, chat_id: int, path: Path, *,
    width: int, height: int, duration: int, caption: str = "",
    local_file_root: Path | None = None,
) -> Message:
    """Отправить файл с диска, явно передав размеры (чтобы Telegram не искажал пропорции)."""
    video = FSInputFile(path)
    api = getattr(getattr(bot, "session", None), "api", None)
    if local_file_root is not None and getattr(api, "is_local", False) is True:
        try:
            root = Path(local_file_root).resolve(strict=True)
            resolved = Path(path).resolve(strict=True)
            if root.is_dir() and resolved.is_file() and resolved.is_relative_to(root):
                # The API container mounts this directory read-only at the same
                # absolute path. Avoid a second multi-GB HTTP upload spool.
                video = resolved.as_uri()
        except (OSError, RuntimeError, ValueError):
            # Other users and files outside the shared allowlist use HTTP upload.
            pass
    return await _send_with_flood_retry(lambda: bot.send_video(
        chat_id=chat_id,
        video=video,
        width=width or None,
        height=height or None,
        duration=duration or None,
        caption=caption,
        supports_streaming=True,
        request_timeout=3600,
    ), retry_transport=False)


async def send_video_by_id(
    bot: Bot, chat_id: int, file_id: str, caption: str = ""
) -> Message:
    """Мгновенная пересылка ранее загруженного видео по file_id (из кэша)."""
    return await _send_with_flood_retry(
        lambda: bot.send_video(chat_id=chat_id, video=file_id, caption=caption))


def extract_file_id(msg: Message) -> str:
    if msg.video:
        return msg.video.file_id
    if msg.document:
        return msg.document.file_id
    return ""
