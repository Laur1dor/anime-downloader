"""Состояния сценария скачивания."""
from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class DownloadFlow(StatesGroup):
    choosing_season = State()
    choosing_voice = State()
    choosing_provider = State()
    choosing_episodes = State()
    waiting_range = State()      # ждём ввод диапазона/номера текстом
    choosing_quality = State()
    choosing_delivery = State()
    choosing_category = State()
    choosing_library_details = State()
    confirm = State()
    processing = State()
