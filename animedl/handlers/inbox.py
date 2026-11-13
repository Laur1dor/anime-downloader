"""Save owner-supplied Telegram videos outside the indexed libraries."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import re
import shutil
import tempfile

from aiogram import F, Router
from aiogram.filters import Filter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ..admin_ops import library_ready

router = Router()
_lock = asyncio.Lock()
_api_root = Path("/var/lib/telegram-bot-api")
log = logging.getLogger(__name__)


def video_of(message):
    document = message.document
    return message.video or message.video_note or (
        document if document and (document.mime_type or "").startswith("video/") else None
    )


def owner_message(message, cfg):
    return (message.chat.type == "private" and message.chat.id == cfg.admin_id
            and message.from_user is not None and message.from_user.id == cfg.admin_id)


class OwnerVideo(Filter):
    async def __call__(self, message, cfg):
        return bool(cfg.media_root and owner_message(message, cfg) and video_of(message))


def inbox_dir(root):
    if not library_ready(root):
        raise OSError("NAS is unavailable")
    target = root / "TelegramInbox"
    if target.is_symlink():
        raise OSError("Inbox is a symlink")
    target.mkdir(exist_ok=True)
    return target


def publish(source, target, message_id, media):
    source = Path(source).resolve(strict=True)
    if not source.is_relative_to(_api_root.resolve(strict=True)) or not source.is_file():
        raise OSError("Unexpected Telegram file location")
    original = getattr(media, "file_name", None) or source.name
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(original).stem).strip(" .")[:48] or "video"
    suffix = Path(original).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
        suffix = ".mp4"
    final = target / f"{message_id}-{stem}{suffix}"
    size = source.stat().st_size
    expected = getattr(media, "file_size", None)
    if not size or (expected is not None and expected != size):
        raise OSError("Incomplete Telegram file")
    if final.exists() or final.is_symlink():
        if final.is_file() and not final.is_symlink() and final.stat().st_size == size:
            return final
        raise OSError("Inbox filename already exists")
    with tempfile.NamedTemporaryFile(dir=target, prefix=".incoming-", delete=False) as temp:
        staged = Path(temp.name)
        try:
            with source.open("rb") as src:
                shutil.copyfileobj(src, temp, length=1024 * 1024)
            temp.close()
            if staged.stat().st_size != size:
                raise OSError("Incomplete NAS copy")
            staged.replace(final)
        finally:
            staged.unlink(missing_ok=True)
    return final


@router.message(OwnerVideo())
async def offer(message):
    await message.reply("Сохранить это видео на NAS в папку TelegramInbox?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Сохранить на NAS", callback_data="nas_inbox:save"),
            InlineKeyboardButton(text="Отмена", callback_data="nas_inbox:cancel"),
        ]]))


@router.callback_query(F.data.in_({"nas_inbox:save", "nas_inbox:cancel"}))
async def save(callback, bot, cfg):
    prompt = callback.message
    original = getattr(prompt, "reply_to_message", None)
    if (callback.from_user.id != cfg.admin_id or not cfg.media_root
            or not original or not owner_message(original, cfg)
            or prompt.chat.type != "private" or prompt.chat.id != cfg.admin_id
            or not video_of(original)):
        await callback.answer("Эта кнопка недоступна.", show_alert=True)
        return
    if _lock.locked():
        await callback.answer("Одно видео уже сохраняется. Попробуй чуть позже.")
        return
    if callback.data == "nas_inbox:cancel":
        await callback.answer()
        await prompt.edit_text("Сохранение отменено.")
        return
    async with _lock:
        await callback.answer("Сохраняю видео…")
        try:
            if not cfg.tg_api_base_url or not cfg.tg_api_local_mode:
                raise OSError("Local Bot API required")
            await asyncio.to_thread(inbox_dir, cfg.media_root)
            media = video_of(original)
            file = await bot.get_file(media.file_id, request_timeout=7200)
            if not file.file_path or not Path(file.file_path).is_absolute():
                raise OSError("Missing local Telegram file path")
            target = await asyncio.to_thread(inbox_dir, cfg.media_root)
            final = await asyncio.to_thread(publish, file.file_path, target, original.message_id, media)
        except Exception as exc:
            log.warning("NAS inbox save failed: %s", type(exc).__name__)
            await prompt.reply("Не удалось сохранить видео. Проверь доступность NAS и свободное место, затем нажми кнопку снова.")
            return
        await prompt.edit_text(f"Сохранено на NAS: TelegramInbox/{final.name}", parse_mode=None)
