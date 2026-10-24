"""Служебные команды: статистика и очистка кэша."""
from __future__ import annotations

import logging
import html
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from ..admin_ops import (delete_media_title, list_media_library, category_ready,
                         read_bandwidth, restart_containers,
                         write_bandwidth)
from ..cache.store import CacheStore
from ..config import Config
from ..models import MEDIA_CATEGORIES
from ..observability import AuditStore
from ..keyboards import LIBRARY_BTN, RELOAD_BTN, SPEED_BTN

log = logging.getLogger(__name__)
router = Router()


def _is_admin(message: Message, cfg: Config) -> bool:
    return bool(message.from_user and message.from_user.id == cfg.admin_id)


@router.message(Command("stats"))
async def cmd_audit_stats(message: Message, cfg: Config, audit: AuditStore) -> None:
    if not _is_admin(message, cfg):
        return
    if audit is None:
        await message.answer("Журнал аудита недоступен. Проверь диск и логи сервиса.")
        return
    stats = await audit.report()
    lines = [f"📊 Аудит за последние {audit.retention_days} дн.", stats["source"],
             f"Пользователей: {stats['users']} · действий/событий: {stats['total']}",
             f"Ошибок: {stats['errors']}", "", "Обработка серий (включая повторы):"]
    lines += [f"{html.escape(status)}: {count} · {size / 1024**3:.2f} ГБ" for status, count, size in stats["jobs"]]
    lines += ["", "Действия:"] + [f"{html.escape(action)}: {count}" for action, count in stats["actions"]]
    lines += ["", "Чаще загружают:"] + [f"{html.escape((title or 'Без названия')[:75])}: {count}" for title, count in stats["titles"]]
    lines += ["", "Плееры / качество / результат:"] + [f"{html.escape(provider[:30])} / {quality or '—'}p / {html.escape(status)}: {count}"
        for provider, quality, status, count in stats["providers"]]
    lines += ["", "Пользователи: серии / неудачи:"] + [f"{uid}: {count} / {failures or 0}" for uid, count, failures in stats["top_users"]]
    lines += ["", "Типы ошибок:"] + [f"{html.escape(cls[:50])}{' HTTP ' + str(http) if http else ''}: {count}" for cls, http, count in stats["failures"]]
    lines += ["", f"Хранилище: {stats['mirror']}"]
    if audit.dsn:
        lines.append(f"Ожидают записи в PostgreSQL: {stats['pending']} · вытеснено до записи: {stats['dropped']}")
    await message.answer("\n".join(lines))


@router.message(Command("errors"))
async def cmd_audit_errors(message: Message, cfg: Config, audit: AuditStore) -> None:
    if not _is_admin(message, cfg):
        return
    if audit is None:
        await message.answer("Журнал аудита недоступен. Проверь диск и логи сервиса.")
        return
    rows, source = await audit.error_report()
    if not rows:
        await message.answer("В сохранённом журнале нет сгруппированных ошибок.")
        return
    lines = ["⚠ Последние группы ошибок (UTC):", source]
    for fingerprint, cls, location, count, ts, http in rows:
        stamp = datetime.fromtimestamp(ts, timezone.utc).strftime("%d.%m %H:%M")
        lines.append(f"{html.escape(cls or 'Error')}{' HTTP ' + str(http) if http else ''} ×{count} · {stamp}\n"
                     f"{html.escape(location or '')}\nГруппа: {fingerprint}")
    await message.answer("\n\n".join(lines))


@router.message(Command("cache_stats"))
async def cmd_cache_stats(message: Message, cache: CacheStore) -> None:
    count, total = cache.stats()
    await message.answer(
        f"📦 В кэше записей: <b>{count}</b>\n"
        f"Суммарный объём отправленного: <b>{total/1024/1024:.0f} МБ</b>\n"
        f"(повторные запросы этих файлов уходят мгновенно по file_id)"
    )


@router.message(Command("cache_clear"))
async def cmd_cache_clear(message: Message) -> None:
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗑 Да, очистить", callback_data="cache_clear_yes"),
        InlineKeyboardButton(text="Отмена", callback_data="cache_clear_no"),
    ]])
    await message.answer("Очистить кэш file_id? Файлы в Telegram останутся, "
                         "но повторные запросы будут скачиваться заново.", reply_markup=kb)


@router.callback_query(lambda c: c.data == "cache_clear_yes")
async def cb_clear_yes(cq: CallbackQuery, cache: CacheStore) -> None:
    n = cache.clear()
    await cq.message.edit_text(f"Кэш очищен ({n} записей удалено).")
    await cq.answer()


@router.callback_query(lambda c: c.data == "cache_clear_no")
async def cb_clear_no(cq: CallbackQuery) -> None:
    await cq.message.edit_text("Очистка отменена.")
    await cq.answer()


# ---------- перезагрузка контейнеров (только админ) ----------

@router.message(F.text == RELOAD_BTN)
@router.message(Command("reload"))
async def cmd_reload(message: Message, cfg: Config) -> None:
    if not _is_admin(message, cfg):
        return
    if not cfg.reload_containers:
        await message.answer("⚠ Не задан список контейнеров (RELOAD_CONTAINERS в .env).")
        return
    await message.answer("♻️ Перезагружаю контейнеры: "
                         + ", ".join(cfg.reload_containers) + " …")
    results = await restart_containers(cfg.reload_containers)
    # если среди контейнеров есть свой (bot) — процесс может умереть до этой строки;
    # docker поднимет его по restart-policy
    await message.answer("Результат перезагрузки:\n" + "\n".join(results))


# ---------- лимит скорости (только админ) ----------

@router.message(F.text == SPEED_BTN)
async def speed_info(message: Message, cfg: Config) -> None:
    if not _is_admin(message, cfg):
        return
    cur = read_bandwidth(cfg.rate_file)
    txt = (f"⚙ <b>Лимит скорости</b>\n"
           f"Сейчас: <b>{'без лимита' if cur == 0 else str(cur) + ' Мбит/с'}</b>\n\n"
           f"Изменить: <code>/speed 100</code> (Мбит/с), <code>/speed 0</code> — снять лимит.")
    await message.answer(txt)


@router.message(F.text == LIBRARY_BTN)
@router.message(Command("library"))
async def toggle_library(message: Message, cfg: Config) -> None:
    if not _is_admin(message, cfg):
        return
    if not cfg.library_dir and not cfg.media_root:
        await message.answer("⚠ Медиатека не настроена (LIBRARY_DIR в .env не задан).")
        return
    connected = category_ready(cfg, "anime")
    categories = "\n".join(f"{'✅' if category_ready(cfg, key) else '⚠'} {label}"
                             for key, (label, _) in MEDIA_CATEGORIES.items())
    await message.answer(
        "📁 Медиатека: " + ("папка подключена." if connected else "папка недоступна, проверь подключение и маркер .animedl-library.")
        + "\nДоставка выбирается для каждой загрузки после качества: Telegram, медиатека или оба. "
        "В медиатеку может сохранять только владелец бота.\n" + categories)



@router.message(Command("lib"))
async def cmd_lib(message: Message, cfg: Config) -> None:
    if not _is_admin(message, cfg):
        return
    if not cfg.library_dir and not cfg.media_root:
        await message.answer("⚠ Медиатека не настроена (LIBRARY_DIR в .env).")
        return
    items = list_media_library(cfg)
    if not items:
        await message.answer("📁 Медиатека пуста.")
        return
    total = sum(s for _, s, _ in items)
    rows = []
    for i, (name, size, n) in enumerate(items):
        category, _, title = name.partition("/")
        rows.append([InlineKeyboardButton(
            text=f"🗑 {MEDIA_CATEGORIES[category][0]}: {title[:30]} — {n} файлов, {size/1024/1024:.0f} МБ",
            callback_data=f"libdel:{i}")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await message.answer(
        f"📁 <b>Медиатека</b>: {len(items)} тайтлов, {total/1024/1024/1024:.1f} ГБ.\n"
        f"Нажми на тайтл, чтобы удалить:", reply_markup=kb)


@router.callback_query(F.data.startswith("libdel:"))
async def cb_libdel(cq: CallbackQuery, cfg: Config) -> None:
    if not cq.from_user or cq.from_user.id != cfg.admin_id:
        await cq.answer("Нет доступа", show_alert=True)
        return
    idx = int(cq.data.split(":")[1])
    items = list_media_library(cfg)
    if idx >= len(items):
        await cq.answer("Список изменился, открой /lib заново")
        return
    name = items[idx][0]
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"libdelok:{idx}"),
        InlineKeyboardButton(text="Отмена", callback_data="libdelno"),
    ]])
    await cq.message.edit_text(f"Удалить из медиатеки: <b>{name}</b>?", reply_markup=kb)
    await cq.answer()


@router.callback_query(F.data.startswith("libdelok:"))
async def cb_libdelok(cq: CallbackQuery, cfg: Config) -> None:
    if not cq.from_user or cq.from_user.id != cfg.admin_id:
        await cq.answer("Нет доступа", show_alert=True)
        return
    idx = int(cq.data.split(":")[1])
    items = list_media_library(cfg)
    if idx >= len(items):
        await cq.message.edit_text("Список изменился, открой /lib заново.")
        await cq.answer()
        return
    name = items[idx][0]
    ok = delete_media_title(cfg, name)
    await cq.message.edit_text(f"{'🗑 Удалено' if ok else '⚠ Не удалось удалить'}: <b>{name}</b>")
    await cq.answer()


@router.callback_query(F.data == "libdelno")
async def cb_libdelno(cq: CallbackQuery) -> None:
    await cq.message.edit_text("Удаление отменено.")
    await cq.answer()


@router.message(Command("speed"))
async def cmd_speed(message: Message, cfg: Config) -> None:
    if not _is_admin(message, cfg):
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        cur = read_bandwidth(cfg.rate_file)
        await message.answer(f"Текущий лимит: {'без лимита' if cur == 0 else str(cur)+' Мбит/с'}.\n"
                             f"Использование: <code>/speed 100</code> или <code>/speed 0</code>.")
        return
    mbit = int(parts[1])
    write_bandwidth(cfg.rate_file, mbit)
    # применяем: рестартим bot-api контейнер(ы) — entrypoint перечитает rate-файл
    api_containers = [c for c in cfg.reload_containers if "api" in c]
    applied = ""
    if api_containers:
        res = await restart_containers(api_containers)
        applied = "\nПрименяю: " + ", ".join(res)
    await message.answer(
        f"✅ Лимит отдачи: <b>{'снят' if mbit == 0 else str(mbit) + ' Мбит/с'}</b>." + applied
    )
