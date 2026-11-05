"""Safe, plain-language descriptions; never include exception text or media URLs."""
from __future__ import annotations

import errno
import html

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import AnswerCallbackQuery


def error_reason(exc) -> str | None:
    if isinstance(exc, TelegramBadRequest) and isinstance(exc.method, AnswerCallbackQuery):
        text = exc.message.lower()
        if "query is too old" in text or "query id is invalid" in text:
            return "callback_expired"
    if isinstance(exc, TelegramForbiddenError):
        if "blocked by the user" in exc.message.lower() or "blocked by user" in exc.message.lower():
            return "bot_blocked"
        if "user is deactivated" in exc.message.lower():
            return "recipient_deactivated"
        return "telegram_forbidden"
    if isinstance(exc, OSError):
        if exc.errno in (errno.ENOSPC, errno.EDQUOT):
            return "disk_full"
        if exc.errno in (errno.EACCES, errno.EROFS):
            return "storage_permissions"
    return None


def error_explanation(details, action="") -> tuple[str, str, str]:
    """What happened, what is known about its impact, and the next useful step."""
    reason = details.get("reason")
    cls = details.get("error_class") or ""
    cause = details.get("cause_class") or cls
    if reason == "callback_expired":
        return ("Telegram не принял подтверждение кнопки",
                "Истёк срок ответа на нажатие. Сам по себе этот ответ не означает сбой скачивания.",
                "Проверь текущее состояние задачи и итоговое сообщение бота.")
    if reason in ("bot_blocked", "recipient_deactivated", "telegram_forbidden") or cls == "TelegramForbiddenError":
        why = {"bot_blocked": "Получатель заблокировал бота.",
               "recipient_deactivated": "Аккаунт получателя удалён или деактивирован."}.get(reason,
               "Telegram запретил боту писать в этот чат; точная причина не сохранена.")
        return ("Telegram запретил отправку в чат", why + " Эта отправка не выполнена.",
                "Получателю нужно проверить доступ к боту, снять блокировку, если она есть, и отправить /start.")
    if reason == "disk_full":
        return ("На сервере закончилось место", "Файл не удалось записать.",
                "Освободи место на диске загрузок и повтори неудавшиеся серии.")
    if reason == "storage_permissions":
        return ("Нет доступа к папке хранения", "Бот не смог записать файл.",
                "Проверь подключение хранилища и права записи в папку.")
    if action == "quality_probe_error":
        return ("Не удалось проверить качество у плеера",
                "Качества этого плеера не подтверждены. Это ещё не означает, что загрузка целиком не удалась.",
                "Проверь итог загрузки; при необходимости повтори проверку качества позже.")
    if action == "source_error":
        return ("Не удалось скачать видео с источника", "Эта попытка скачивания не удалась.",
                "Повтори неудавшиеся серии позже или выбери другой доступный источник.")
    if action == "library_error":
        return ("Не удалось сохранить видео в медиатеку", "Сохранение этой серии завершилось ошибкой.",
                "Проверь доступность папки, свободное место и права записи; затем повтори серию.")
    if cls == "TelegramBadRequest":
        return ("Telegram отклонил запрос бота",
                "Не удалось выполнить запрос. По этому событию нельзя определить итог скачивания.",
                "Проверь итоговое сообщение задачи. Если проблема повторяется, передай код ошибки из /errors.")
    if cls in ("TelegramNetworkError", "TelegramServerError"):
        return ("Сбой связи с Telegram", "Не удалось подтвердить выполнение запроса.",
                "Проверь чат перед повтором: уже принятые видео могли отправиться.")
    if "Timeout" in cause or cause in ("ConnectError", "ConnectionError", "ReadError"):
        return ("Не удалось дождаться ответа сервиса", "Одна из операций не завершилась вовремя.",
                "Проверь итог задачи и повтори только неудавшиеся серии.")
    if details.get("http_status"):
        return ("Сервис отклонил запрос", "Удалённый сервис вернул ошибку доступа или обработки.",
                "Проверь итог задачи; если сбой повторяется, попробуй другой источник.")
    return ("Не удалось выполнить действие бота",
            "Возникла ошибка. По этому событию нельзя определить итог всей загрузки.",
            "Проверь сообщение о завершении; для разбора используй код ошибки из /errors.")


def format_error_alert(action, details, *, scope="") -> str:
    heading, impact, advice = error_explanation(details, action)
    lines = ["⚠ " + heading]
    if scope:
        lines.append(scope)
    context = [str(details[k]) for k in ("title", "provider") if details.get(k)]
    if details.get("episode") is not None:
        context.append(f"серия {details['episode']}")
    if context:
        lines.append(html.escape(" · ".join(context)))
    lines.extend(("", impact, advice))
    if details.get("stage"):
        from .observability import _STAGES
        if details["stage"] in _STAGES:
            lines.append("Этап: " + _STAGES[details["stage"]])
    lines.extend(("", "Код: <code>" + html.escape(str(details.get("fingerprint", "—"))) + "</code>",
                  "Технические подробности: /errors"))
    return "\n".join(lines)
