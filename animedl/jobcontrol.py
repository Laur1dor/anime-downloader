"""Управление активной задачей скачивания: пауза / продолжение / отмена."""
from __future__ import annotations

import asyncio


class JobControl:
    def __init__(self) -> None:
        self.cancelled = False
        self._resume = asyncio.Event()
        self._resume.set()  # по умолчанию — не на паузе

    def pause(self) -> None:
        self._resume.clear()

    def resume(self) -> None:
        self._resume.set()

    def cancel(self) -> None:
        self.cancelled = True
        self._resume.set()  # разблокируем ожидание паузы, чтобы отмена сработала

    @property
    def is_paused(self) -> bool:
        return not self._resume.is_set()

    async def wait_if_paused(self) -> None:
        await self._resume.wait()


# Глобальный лимит одновременных задач (защита машины в public-режиме).
_SEM: asyncio.Semaphore | None = None
_SEM_LIMIT = 1


def configure_concurrency(limit: int) -> None:
    global _SEM_LIMIT
    _SEM_LIMIT = max(1, limit)


def global_semaphore() -> asyncio.Semaphore:
    global _SEM
    if _SEM is None:
        _SEM = asyncio.Semaphore(_SEM_LIMIT)
    return _SEM


def is_busy() -> bool:
    sem = global_semaphore()
    return sem.locked() or sem._value <= 0  # noqa: SLF001


# Активные задачи по chat_id (у нас фактически один админ, но так честнее).
ACTIVE: dict[int, JobControl] = {}


def start_job(chat_id: int) -> JobControl:
    ctrl = JobControl()
    ACTIVE[chat_id] = ctrl
    return ctrl


def get_job(chat_id: int) -> JobControl | None:
    return ACTIVE.get(chat_id)


def end_job(chat_id: int) -> None:
    ACTIVE.pop(chat_id, None)
