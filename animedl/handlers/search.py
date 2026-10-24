"""Поиск по названию с переходом в существующий сценарий загрузки."""
from __future__ import annotations

import asyncio
import html
import logging
import time

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from ..catalog import CatalogStore
from ..config import Config
from ..observability import capture, emit
from ..jobcontrol import get_job
from ..search_sources import search_sources
from .flow import open_title

router = Router()
log = logging.getLogger(__name__)
_recent: dict[int, float] = {}
_active: set[int] = set()


@router.message(Command("search"))
@router.message(StateFilter(None), F.text, ~F.text.startswith("/"), ~F.text.contains("http"))
async def on_search(message: Message, state: FSMContext, catalog: CatalogStore) -> None:
    if not message.from_user:
        return
    if await state.get_state() is not None or get_job(message.chat.id):
        await message.answer("Заверши текущий выбор или нажми /cancel перед новым поиском.")
        return
    text = (message.text or "").strip()
    query = text.partition(" ")[2].strip() if text.startswith("/search") else text
    if not 2 <= len(query) <= 100:
        await message.answer("Напиши название длиной от 2 до 100 символов: /search Необъятный океан")
        return
    uid = message.from_user.id
    now = time.monotonic()
    if uid in _active or now - _recent.get(uid, -100) < 5:
        await message.answer("Подожди несколько секунд перед следующим поиском.")
        return
    if len(_active) >= 2:
        await message.answer("Поиск занят. Попробуй через несколько секунд.")
        return
    for user, stamp in list(_recent.items()):
        if now - stamp > 60:
            _recent.pop(user, None)
    _recent[uid] = now
    _active.add(uid)
    try:
        catalog.event(uid, "search")
        emit("search", uid)
        status = await message.answer("🔎 Ищу по названию…")
        cached = catalog.search(query, limit=12)
        errors = {}
        hits = []
        try:
            response = await asyncio.wait_for(search_sources(query, limit=12), timeout=25)
            hits, errors = response.hits, response.errors
        except Exception as exc:
            capture(exc, "search_error", uid)
            log.warning("Search failed: %s", type(exc).__name__)
            errors = {"поиск": type(exc).__name__}
        for provider in errors:
            emit("search_provider_error", uid, "error", provider=provider)
            catalog.event(uid, "provider_error", site=provider)
        hits = [catalog.remember(hit) for hit in hits]
        # Keep distinct seasons/sources; their names alone are never an identity.
        merged = {hit.url: hit for hit in hits}
        for hit in cached:
            merged.setdefault(hit.url, hit)
        results = list(merged.values())[:12]
        emit("search_results", uid, choice=str(len(results)))
        if not results:
            catalog.event(uid, "no_results")
            suffix = ("\nЧасть источников сейчас недоступна. Можно прислать прямую ссылку."
                      if errors else "\nПопробуй другое название или пришли ссылку.")
            await status.edit_text("Ничего не найдено." + suffix)
            return
        token = catalog.store_results(uid, results)
        rows = [[InlineKeyboardButton(text=f"{i + 1}. {hit.title[:65]} · {hit.site}",
                                     callback_data=f"find:{token}:{i}")]
                for i, hit in enumerate(results)]
        lines = [f"{i + 1}. <b>{html.escape(hit.title[:180])}</b> · {html.escape(hit.site)}"
                 for i, hit in enumerate(results)]
        note = "\nНекоторые источники недоступны; показаны доступные результаты и кэш." if errors else ""
        await status.edit_text("Выбери произведение и источник:\n\n" + "\n".join(lines) + note,
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    finally:
        _active.discard(uid)


@router.callback_query(F.data.startswith("find:"))
async def on_result(cq: CallbackQuery, state: FSMContext, catalog: CatalogStore) -> None:
    if not cq.message or not cq.from_user:
        await cq.answer()
        return
    if await state.get_state() is not None or get_job(cq.message.chat.id):
        await cq.answer("Сначала заверши текущий выбор или /cancel", show_alert=True)
        return
    try:
        _, token, raw = cq.data.split(":")
        hit = catalog.get_result(cq.from_user.id, token, int(raw))
    except (ValueError, TypeError):
        hit = None
    if hit is None:
        await cq.answer("Результат устарел. Повтори поиск.", show_alert=True)
        return
    await cq.answer()
    # Enter the state before network awaits, preventing a double click from opening two flows.
    await state.set_state("search:opening")
    try:
        catalog.event(cq.from_user.id, "selection")
        await open_title(cq.message, state, hit.url, catalog)
    finally:
        if await state.get_state() == "search:opening":
            await state.clear()


@router.message(Command("search_stats"))
async def on_stats(message: Message, cfg: Config, catalog: CatalogStore) -> None:
    if not message.from_user or message.from_user.id != cfg.admin_id:
        return
    stats = catalog.stats()
    await message.answer(
        "Поиск за последние 7 дней:\n"
        f"Пользователей: {stats['users']}\nПоисков: {stats['searches']}\n"
        f"Выборов результата: {stats['selections']}\nБез результатов: {stats['no_results']}\n"
        f"Ошибок источников: {stats['provider_errors']}"
    )
