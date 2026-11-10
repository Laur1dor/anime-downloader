"""/start, /help, /cancel и общие утилиты."""
from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, ChatMemberUpdated

from ..config import Config
from ..keyboards import admin_reply_kb
from ..jobcontrol import get_job
from ..observability import emit
from ..catalog import CatalogStore

router = Router()


@router.my_chat_member()
async def on_bot_membership(event: ChatMemberUpdated) -> None:
    if event.chat.type == "private" and event.new_chat_member.status in ("kicked", "left"):
        ctrl = get_job(event.chat.id)
        if ctrl:
            ctrl.stop_forbidden()
            emit("batch_stopped", event.chat.id, "cancelled", reason="telegram_forbidden")

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
    "• /feedback текст — обратная связь владельцу\n"
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
async def cmd_cancel(message: Message, state: FSMContext, catalog: CatalogStore | None = None) -> None:
    data = await state.get_data()
    if catalog and message.from_user and data.get('search_token'):
        catalog.discard_results(message.from_user.id, data['search_token'])
    await state.clear()
    if data.get('search_message_id') and not get_job(message.chat.id):
        await message.bot.edit_message_text("Поиск и выбор отменены. Пришли ссылку или название.",
            chat_id=message.chat.id, message_id=data['search_message_id'], reply_markup=None)
    else:
        await message.answer("Отменено. Пришли новую ссылку, когда будешь готов.")
