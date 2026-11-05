"""Поиск по названию с переходом в существующий сценарий загрузки."""
from __future__ import annotations

import asyncio
import html
import logging
import time

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
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
_selecting: set[int] = set()
PAGE_SIZE = 5
RESULTS_STATE = "search:results"


async def _edit_menu(operation):
    try:
        return await operation
    except TelegramBadRequest as exc:
        if 'message is not modified' not in exc.message.lower():
            raise


def results_view(results, token, page, *, partial=False):
    pages = max(1, (len(results) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    start = page * PAGE_SIZE
    visible = results[start:start + PAGE_SIZE]
    lines = [f"🔎 Выбери произведение и источник · {page + 1}/{pages}", ""]
    for index, hit in enumerate(visible, start + 1):
        lines.append(f"<b>{index}. {html.escape(hit.title[:180])}</b>\nИсточник: {html.escape(hit.site)}\n")
    lines.append("Нажми номер ниже или отправь его сообщением.")
    if partial:
        lines.append("Часть источников недоступна; показаны доступные результаты и кэш.")
    rows = [[InlineKeyboardButton(text=str(i + 1), callback_data=f"find:{token}:{i}")
             for i in range(start, start + len(visible))]]
    arrows = []
    if page:
        arrows.append(InlineKeyboardButton(text="←", callback_data=f"search:page:{token}:{page - 1}"))
    if page + 1 < pages:
        arrows.append(InlineKeyboardButton(text="→", callback_data=f"search:page:{token}:{page + 1}"))
    if arrows:
        rows.append(arrows)
    rows.append([InlineKeyboardButton(text="« Новый поиск", callback_data=f"search:new:{token}"),
                 InlineKeyboardButton(text="✖ Отмена", callback_data=f"search:cancel:{token}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows), page


@router.message(Command("search"))
@router.message(StateFilter(None), F.text, ~F.text.startswith("/"), ~F.text.contains("http"))
@router.message(StateFilter(RESULTS_STATE), F.text, ~F.text.startswith("/"), ~F.text.contains("http"), ~F.text.regexp(r"^\s*\d+\s*$"))
async def on_search(message: Message, state: FSMContext, catalog: CatalogStore) -> None:
    if not message.from_user:
        return
    if await state.get_state() not in (None, RESULTS_STATE) or get_job(message.chat.id):
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
        await state.set_state("search:loading")
        status = await message.answer("🔎 Ищу по названию…")
        await state.update_data(search_message_id=status.message_id)
        cached = catalog.search(query, limit=20)
        errors = {}
        hits = []
        try:
            response = await asyncio.wait_for(search_sources(query, limit=20), timeout=25)
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
        results = list(merged.values())[:20]
        # A /cancel while the network was running must not resurrect this menu.
        if await state.get_state() != "search:loading":
            return
        emit("search_results", uid, choice=str(len(results)))
        if not results:
            catalog.event(uid, "no_results")
            suffix = ("\nЧасть источников сейчас недоступна. Можно прислать прямую ссылку."
                      if errors else "\nПопробуй другое название или пришли ссылку.")
            await status.edit_text("Ничего не найдено." + suffix)
            await state.clear()
            return
        token = catalog.store_results(uid, results)
        await state.update_data(search_token=token, search_page=0, search_partial=bool(errors))
        await state.set_state(RESULTS_STATE)
        text, keyboard, _ = results_view(results, token, 0, partial=bool(errors))
        await status.edit_text(text, reply_markup=keyboard)
    finally:
        _active.discard(uid)
        if await state.get_state() == "search:loading":
            await state.clear()


@router.callback_query(F.data.startswith("find:"))
async def on_result(cq: CallbackQuery, state: FSMContext, catalog: CatalogStore) -> None:
    if not cq.message or not cq.from_user:
        await cq.answer()
        return
    if await state.get_state() not in (None, RESULTS_STATE) or get_job(cq.message.chat.id):
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
    await _open_result(cq.from_user.id, token, int(raw), cq.message, state, catalog, target=cq.message)


async def _open_result(uid, token, index, message, state, catalog, *, target=None):
    if uid in _selecting:
        return
    _selecting.add(uid)
    try:
        data = await state.get_data()
        if data.get('search_token') and data['search_token'] != token:
            return
        hit = catalog.get_result(uid, token, index)
        if hit is None:
            return
        await state.set_state("search:opening")
        # A different result must not inherit the previous title's voice,
        # episode range or destination folder when returning to the list.
        await state.set_data({k: v for k, v in data.items() if k.startswith('search_')})
        restore = {'search_token': token, 'search_page': data.get('search_page', index // PAGE_SIZE)}
        if target is not None and getattr(target, 'message_id', None):
            restore['search_message_id'] = target.message_id
        await state.update_data(**restore)
        if target is None:
            target = await message.bot.edit_message_text("🔎 Открываю…", chat_id=message.chat.id,
                                                       message_id=data['search_message_id'])
        catalog.event(uid, "selection")
        await open_title(message, state, hit.url, catalog, status=target)
    finally:
        _selecting.discard(uid)
        if await state.get_state() == "search:opening":
            await state.set_state(RESULTS_STATE)


@router.message(StateFilter(RESULTS_STATE), F.text.regexp(r"^\s*\d+\s*$"))
async def on_result_number(message: Message, state: FSMContext, catalog: CatalogStore):
    data = await state.get_data()
    raw = message.text.strip()
    token = data.get('search_token', '')
    if not catalog.get_results(message.from_user.id, token):
        await state.clear()
        await message.bot.edit_message_text("Поиск устарел. Напиши название заново.",
            chat_id=message.chat.id, message_id=data['search_message_id'], reply_markup=None)
        return
    if len(raw) > 3 or catalog.get_result(message.from_user.id, token, int(raw) - 1) is None:
        text, keyboard, _ = results_view(catalog.get_results(message.from_user.id, token), token,
                                        data.get('search_page', 0), partial=data.get('search_partial', False))
        await _edit_menu(message.bot.edit_message_text("Такого номера нет. Выбери номер из списка.\n\n" + text,
            chat_id=message.chat.id, message_id=data['search_message_id'], reply_markup=keyboard))
        return
    await _open_result(message.from_user.id, token, int(raw) - 1, message, state, catalog)


@router.callback_query(F.data.startswith("search:"))
async def on_search_navigation(cq: CallbackQuery, state: FSMContext, catalog: CatalogStore):
    data = await state.get_data()
    parts = cq.data.split(':')
    if len(parts) not in (3, 4) or not cq.message:
        await cq.answer()
        return
    _, action, token, *raw_page = parts
    results = catalog.get_results(cq.from_user.id, token)
    if not results or token != data.get('search_token'):
        await cq.answer("Поиск устарел. Отправь название заново.", show_alert=True)
        return
    if get_job(cq.message.chat.id) or await state.get_state() == "search:opening":
        await cq.answer("Дождись завершения текущего действия.", show_alert=True)
        return
    await cq.answer()
    if action in ('cancel', 'new'):
        catalog.discard_results(cq.from_user.id, token)
        await state.clear()
        await _edit_menu(cq.message.edit_text("Поиск отменён." if action == 'cancel' else "🔎 Напиши название для нового поиска.",
                                   reply_markup=None))
    elif action == 'page' and len(raw_page) == 1 and raw_page[0].isdigit() and len(raw_page[0]) <= 3:
        text, keyboard, page = results_view(results, token, int(raw_page[0]),
                                           partial=data.get('search_partial', False))
        await state.set_state(RESULTS_STATE)
        await state.update_data(search_page=page)
        # Clicking the same back button twice can produce an unchanged message.
        await _edit_menu(cq.message.edit_text(text, reply_markup=keyboard))


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
