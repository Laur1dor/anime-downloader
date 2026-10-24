"""/start, /help, /cancel и общие утилиты."""
from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from ..config import Config
from ..keyboards import admin_reply_kb

router = Router()

HELP = (
    "🎬 <b>AnimeDownloader</b>\n\n"
    "Пришли ссылку на аниме или «Клан Сопрано» — выберешь озвучку, серии и качество, "
    "я скачаю и пришлю видео сюда.\n\n"
    "<b>Поддерживаются:</b>\n"
    "• animego.me / animego.org (Kodik 720p, AniBoom до 1080p)\n"
    "• animego.online\n"
    "• yummyanime.tv\n"
    "• yummyani.me / old.yummyani.me\n"
    "• watch-the-sopranos-online.org — сезон или весь сериал, озвучка Гоблина\n\n"
    "Команды:\n"
    "• /search название — поиск; можно просто отправить название\n"
    "• /search_stats — статистика поиска для владельца\n"
    "• /cancel — отменить текущий сценарий\n"
    "• /cache_stats — статистика кэша\n"
    "• /cache_clear — очистить кэш\n"
)


@router.message(Command("start"))
async def cmd_start(message: Message, cfg: Config) -> None:
    kb = None
    if message.from_user and message.from_user.id == cfg.admin_id:
        kb = admin_reply_kb()  # админу — постоянная клавиатура с перезагрузкой/скоростью
    await message.answer(HELP, reply_markup=kb)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено. Пришли новую ссылку, когда будешь готов.")
