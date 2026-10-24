"""Инлайн-клавиатуры. callback_data кодирует только индексы (лимит 64 байта)."""
from __future__ import annotations

from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, ReplyKeyboardMarkup)
from aiogram.utils.keyboard import InlineKeyboardBuilder

RELOAD_BTN = "🔄 Перезагрузка"
SPEED_BTN = "⚙ Скорость"
LIBRARY_BTN = "📁 Библиотека"


def admin_reply_kb() -> ReplyKeyboardMarkup:
    """Постоянная клавиатура у поля ввода — только для админа."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=RELOAD_BTN), KeyboardButton(text=SPEED_BTN)],
                  [KeyboardButton(text=LIBRARY_BTN)]],
        resize_keyboard=True, is_persistent=True,
    )


def seasons_kb(seasons: list[int], selected: int | None = None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for season in seasons:
        label = f"Сезон {season}" + (" · из ссылки" if season == selected else "")
        kb.button(text=label, callback_data=f"season:{season}")
    kb.button(text="📥 Весь сериал", callback_data="season:all")
    kb.button(text="✖ Отмена", callback_data="cancel")
    kb.adjust(2)
    return kb.as_markup()


def voices_kb(voices: list[str], preferred: str = "", *, has_seasons: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for i, v in enumerate(voices):
        kb.button(text=v + (" · по умолчанию" if v == preferred else ""), callback_data=f"voice:{i}")
    if has_seasons:
        kb.button(text="« Сезоны", callback_data="back:season")
    kb.button(text="✖ Отмена", callback_data="cancel")
    kb.adjust(1)
    return kb.as_markup()


def providers_kb(items: list[tuple[str, int]]) -> InlineKeyboardMarkup:
    """items: (provider_title, max_quality_hint or 0)."""
    kb = InlineKeyboardBuilder()
    for i, (prov, q) in enumerate(items):
        label = f"{prov}" + (f" (до {q}p)" if q else "")
        kb.button(text=label, callback_data=f"prov:{i}")
    kb.button(text="« Назад", callback_data="back:voice")
    kb.button(text="✖ Отмена", callback_data="cancel")
    kb.adjust(1)
    return kb.as_markup()


def episodes_kb(total: int, *, whole_show: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    scope = "сериал" if whole_show else "сезон"
    kb.button(text=f"📥 Весь {scope} ({total} серий)", callback_data="ep:all")
    if not whole_show:
        kb.button(text="▶️ Сезон с серии…", callback_data="ep:from")
    if not whole_show:
        kb.button(text="🔢 Диапазон / номер", callback_data="ep:range")
    kb.button(text="« Назад", callback_data="back:prov")
    kb.button(text="✖ Отмена", callback_data="cancel")
    kb.adjust(1)
    return kb.as_markup()


def job_kb(is_paused: bool) -> InlineKeyboardMarkup:
    row = []
    if is_paused:
        row.append(InlineKeyboardButton(text="▶ Продолжить", callback_data="job:resume"))
    else:
        row.append(InlineKeyboardButton(text="⏸ Пауза", callback_data="job:pause"))
    row.append(InlineKeyboardButton(text="⛔ Стоп", callback_data="job:cancel"))
    return InlineKeyboardMarkup(inline_keyboard=[row])


def quality_kb(qualities: list[int]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for q in qualities:
        kb.button(text=f"{q}p", callback_data=f"q:{q}")
    kb.button(text="« Назад", callback_data="back:ep")
    kb.button(text="✖ Отмена", callback_data="cancel")
    kb.adjust(3)
    return kb.as_markup()


def confirm_kb(*, edit_library: bool = False) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton(text="▶ Начать", callback_data="go"),
        InlineKeyboardButton(text="✖ Отмена", callback_data="cancel"),
    ]]
    if edit_library:
        rows.append([InlineKeyboardButton(text="✏ Название и сезон", callback_data="library:details:edit")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def delivery_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for mode, label in (("telegram", "Telegram"), ("library", "Только медиатека"), ("both", "Telegram + медиатека")):
        kb.button(text=label, callback_data=f"delivery:{mode}")
    kb.button(text="✖ Отмена", callback_data="cancel")
    kb.adjust(1)
    return kb.as_markup()


def library_details_kb(has_season: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if has_season:
        kb.button(text="Подтвердить сезон и название", callback_data="library:details:confirm")
    kb.button(text="✖ Отмена", callback_data="cancel")
    kb.adjust(1)
    return kb.as_markup()


def category_kb(selected: str) -> InlineKeyboardMarkup:
    from .models import MEDIA_CATEGORIES
    kb = InlineKeyboardBuilder()
    for category, (label, _) in MEDIA_CATEGORIES.items():
        kb.button(text=("✓ " if category == selected else "") + label, callback_data=f"category:{category}")
    kb.button(text="✖ Отмена", callback_data="cancel")
    kb.adjust(2)
    return kb.as_markup()
