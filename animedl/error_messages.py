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
    op = details.get('operation')
    if reason == 'warning_overflow':
        return ('Очередь предупреждений переполнялась',
                f"Часть предупреждений не поместилась в очередь. Счётчик потерь: {details.get('dropped_warnings', 0)}.",
                'Проверь источник потока предупреждений. Потери учитываются в /stats; тишина не считается подтверждением исправления.')
    optional = {
        'episode_thumbnail': ('Не удалось подготовить миниатюру серии', 'Основное видео может быть сохранено; предупреждение относится к изображению.'),
        'poster_fetch': ('Не удалось получить постер', 'Предупреждение относится к обложке, а не к доставке видео.'),
        'jellyfin_refresh': ('Не подтверждено обновление библиотеки Jellyfin', 'Файл мог сохраниться; запрос сканирования библиотеки не подтверждён.'),
        'voice_enumeration': ('Не удалось проверить серии одной озвучки', 'Список доступных серий этой озвучки может быть неполным.'),
        'title_parse': ('Не удалось определить произведение и озвучки', 'Выбор загрузки не был подготовлен для этого источника.'),
        'quality_menu': ('Не удалось подготовить выбор качества', 'Доступные качества для выбранных серий и озвучки не подтверждены.'),
        'search_provider': ('Не удалось проверить один источник поиска', 'Другие источники и сохранённые результаты могут работать.'),
        'postgres_mirror': ('PostgreSQL временно недоступен', 'События остаются в ограниченном локальном журнале; бот продолжает работать.'),
        'telegram_polling': ('Прервалось получение сообщений из Telegram', 'Бот временно не мог получить новые команды. Это не доказывает сбой уже начатого скачивания.'),
    }
    if op in optional:
        heading, impact = optional[op]
        return heading, impact, 'Статус изменится после успешной проверки этой же операции. Подробности — /errors.'
    if reason == "callback_expired":
        return ("Telegram не принял подтверждение кнопки",
                "Истёк срок ответа на нажатие. Сам по себе этот ответ не означает сбой скачивания.",
                "Проверь текущее состояние задачи и итоговое сообщение бота.")
    if details.get('severity') == 'warning' or reason == 'logged_warning' or (action == 'logging_warning' and reason != 'logged_error'):
        return ('Предупреждение при работе бота',
                'Код зарегистрировал предупреждение. Оно само по себе не означает провал загрузки.',
                'Проверь итог задачи. Если для этой операции нет проверки восстановления, проблема останется открытой в /errors.')
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


def event_context(details):
    parts = [str(details[k])[:100] for k in ('title','provider','voice') if details.get(k)]
    if details.get('season') is not None:
        parts.append(f"сезон {details['season']}")
    if details.get('episode') is not None:
        parts.append(f"серия {details['episode']}")
    return html.escape(' · '.join(parts))


def format_error_alert(action, details, *, scope="") -> str:
    heading, impact, advice = error_explanation(details, action)
    lines = ["⚠ " + heading]
    if scope:
        lines.append(scope)
    context = event_context(details)
    if context:
        lines.append(context)
    component = details.get('component', '')
    for prefix, label in (('animedl.download','подготовка видео'),('animedl.artwork','изображения'),
                          ('animedl.telegram_client','отправка Telegram'),('animedl.sites','разбор сайта'),
                          ('animedl.handlers','обработка команды'),('aiogram','связь с Telegram'),
                          ('animedl.admin_ops','служебное действие'),('animedl','обработка бота')):
        if component.startswith(prefix):
            lines.append('Компонент: ' + label)
            break
    lines.extend(("", impact, advice))
    if details.get("stage"):
        from .observability import _STAGES
        if details["stage"] in _STAGES:
            lines.append("Этап: " + _STAGES[details["stage"]])
    lines.extend(("", "Код: <code>" + html.escape(str(details.get("fingerprint", "—"))) + "</code>",
                  "Технические подробности: /errors"))
    return "\n".join(lines)
