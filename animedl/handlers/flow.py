"""Ссылка → озвучка → серии → качество → подтверждение → загрузка."""
from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup

from ..cache.store import CacheStore
from ..catalog import CatalogStore
from ..admin_ops import category_ready
from ..config import Config
from ..observability import capture
from ..fsm.states import DownloadFlow
from ..jobcontrol import (end_job, get_job, global_semaphore, is_busy, start_job)
from ..keyboards import (category_kb, confirm_kb, delivery_kb, library_details_kb, episodes_kb, job_kb, providers_kb, quality_kb,
                         seasons_kb, voices_kb)
from ..models import MEDIA_CATEGORIES, is_movie, EpisodeRef, PlayerButton, TitleInfo, library_selection_suggestion, validate_library_details
from ..players.base import ExtractError
from ..services import (EpisodeResult, group_voices, process_episode, providers_for_voice,
                        qualities_for_buttons, validate_delivery)
from ..sites.base import ParseError
from ..sites.registry import UnsupportedSite, get_parser

log = logging.getLogger(__name__)
router = Router()


# ---------- helpers ----------

def _btn_from_dict(d: dict) -> PlayerButton:
    return PlayerButton(voice=d["voice"], provider=d["provider"],
                        embed_url=d["embed_url"], provider_id=d.get("provider_id", ""))


def _title_from_state(data: dict) -> TitleInfo:
    eps = [EpisodeRef(number=e["number"], db_id=e["db_id"],
                      title=e.get("title", ""), released=e.get("released", ""), season=e.get("season"))
           for e in data["episodes"]]
    return TitleInfo(site=data["site"], url=data["url"], anime_id=data["anime_id"],
                     title=data["title"], episodes=eps, season=data.get("season"), poster=data.get("poster", ""),
                     preferred_voice=data.get("preferred_voice", ""), media_category=data.get("media_category", "anime"),
                     library_series_title=data.get("source_series_title", ""), mal_id=data.get("mal_id"))


def parse_selection(text: str, total: int) -> list[int]:
    text = text.replace(" ", "")
    result: set[int] = set()
    for part in text.split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            a, b = int(a), int(b)
            if a > b:
                a, b = b, a
            result.update(range(a, b + 1))
        else:
            result.add(int(part))
    result = {n for n in result if 1 <= n <= total}
    if not result:
        raise ValueError("пусто")
    return sorted(result)


# ---------- шаг 1: ссылка ----------

@router.message(F.text.contains("http"))
async def on_link(message: Message, state: FSMContext, catalog: CatalogStore | None = None) -> None:
    url = message.text.strip().split()[0]
    await open_title(message, state, url, catalog)


async def open_title(message: Message, state: FSMContext, url: str,
                     catalog: CatalogStore | None = None) -> None:
    try:
        parser = get_parser(url)
    except UnsupportedSite as e:
        await message.answer(f"⚠ {e}")
        return

    status = await message.answer("🔎 Определяю тайтл и озвучки…")
    try:
        title = await parser.fetch_title(url)
        if catalog is not None:
            catalog.remember(title)
        seasons = sorted({ep.season for ep in title.episodes if ep.season is not None})
        if seasons:
            await state.update_data(
                url=url, site=title.site, anime_id=title.anime_id, title=title.title,
                season=title.season, poster=title.poster, preferred_voice=title.preferred_voice, media_category=title.media_category,
                source_series_title=title.library_series_title, mal_id=title.mal_id,
                seasons=seasons,
                all_episodes=[{"number": e.number, "db_id": e.db_id, "title": e.title,
                               "released": e.released, "season": e.season} for e in title.episodes],
            )
            await state.set_state(DownloadFlow.choosing_season)
            import html
            await status.edit_text(f"🎬 <b>{html.escape(title.title)}</b>\nВыбери сезон или весь сериал:",
                                   reply_markup=seasons_kb(seasons, title.season))
            return
        ref_ep = title.episodes[0]
        buttons = await parser.fetch_episode_players(title, ref_ep)
    except (ParseError, UnsupportedSite) as e:
        capture(e, "title_parse_error")
        await status.edit_text(f"⚠ {e}")
        return
    except Exception as e:  # noqa: BLE001
        capture(e, "title_parse_error", message.from_user.id if message.from_user else None)
        log.exception("Ошибка разбора тайтла")
        await status.edit_text(f"⚠ Не удалось разобрать страницу: {e}")
        return

    if not buttons:
        await status.edit_text("⚠ Для этого тайтла пока нет доступных плееров "
                               "(возможно, серия ещё не вышла).")
        return

    voices = group_voices(buttons)
    await state.set_state(DownloadFlow.choosing_voice)
    await state.update_data(
        url=url, site=title.site, anime_id=title.anime_id, title=title.title, season=title.season, poster=title.poster,
        preferred_voice=title.preferred_voice, media_category=title.media_category, seasons=[], whole_show=False,
        source_series_title=title.library_series_title, mal_id=title.mal_id,
        episodes=[{"number": e.number, "db_id": e.db_id, "title": e.title,
                   "released": e.released, "season": e.season} for e in title.episodes],
        ref_buttons=[{"voice": b.voice, "provider": b.provider,
                      "embed_url": b.embed_url, "provider_id": b.provider_id}
                     for b in buttons],
        voices=list(voices.keys()),
    )
    await status.edit_text(
        f"🎬 <b>{title.title}</b>\n"
        f"Серий доступно: {len(title.episodes)}\n\n"
        f"Выбери озвучку:",
        reply_markup=voices_kb(list(voices.keys())),
    )


@router.callback_query(DownloadFlow.choosing_season, F.data.startswith("season:"))
async def on_season(cq: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    raw = cq.data.split(":", 1)[1]
    try:
        season = None if raw == "all" else int(raw)
    except ValueError:
        await cq.answer("Неверный сезон")
        return
    if season is not None and season not in data.get("seasons", []):
        await cq.answer("Неверный сезон")
        return
    episodes = [ep for ep in data["all_episodes"] if season is None or ep["season"] == season]
    await state.update_data(episodes=episodes, season=season, whole_show=season is None)
    await cq.message.edit_text("🔎 Получаю озвучки выбранного сезона…")
    try:
        title = _title_from_state(await state.get_data())
        buttons = await get_parser(title.url).fetch_episode_players(title, title.episodes[0])
        if not buttons:
            raise ParseError("Для выбранного сезона нет доступных плееров")
    except Exception as e:
        capture(e, "season_players_error", cq.from_user.id)
        log.exception("Ошибка получения плееров сезона")
        await cq.message.edit_text(f"⚠ {e}", reply_markup=seasons_kb(data["seasons"]))
        await cq.answer()
        return
    voices = list(group_voices(buttons))
    preferred = data.get("preferred_voice", "")
    if preferred in voices:
        voices.remove(preferred)
        voices.insert(0, preferred)
    await state.update_data(voices=voices,
        ref_buttons=[{"voice": b.voice, "provider": b.provider, "embed_url": b.embed_url,
                      "provider_id": b.provider_id} for b in buttons])
    await state.set_state(DownloadFlow.choosing_voice)
    scope = "Весь сериал" if season is None else f"Сезон {season}"
    await cq.message.edit_text(f"{scope} · {len(episodes)} серий\nВыбери озвучку:",
        reply_markup=voices_kb(voices, preferred, has_seasons=True))
    await cq.answer()


# ---------- шаг 2: озвучка ----------

@router.callback_query(DownloadFlow.choosing_voice, F.data.startswith("voice:"))
async def on_voice(cq: CallbackQuery, state: FSMContext) -> None:
    idx = int(cq.data.split(":")[1])
    data = await state.get_data()
    voices = data["voices"]
    if idx >= len(voices):
        await cq.answer("Неверный выбор")
        return
    voice = voices[idx]
    await state.update_data(voice=voice, provider="auto")
    await _ask_episodes(cq, state)
    await cq.answer()


# ---------- шаг 3: плеер ----------

@router.callback_query(DownloadFlow.choosing_provider, F.data.startswith("prov:"))
async def on_provider(cq: CallbackQuery, state: FSMContext) -> None:
    idx = int(cq.data.split(":")[1])
    data = await state.get_data()
    voice = data["voice"]
    buttons = [_btn_from_dict(d) for d in data["ref_buttons"] if d["voice"] == voice]
    provs = providers_for_voice(buttons)
    if idx >= len(provs):
        await cq.answer("Неверный выбор")
        return
    await state.update_data(provider="auto")
    await _ask_episodes(cq, state)
    await cq.answer()


async def _ask_episodes(cq: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    total = len(data["episodes"])
    await state.set_state(DownloadFlow.choosing_episodes)
    await cq.message.edit_text(
        f"Озвучка: <b>{data['voice']}</b>\n"
        f"Сколько серий скачать? (всего {total})",
        reply_markup=episodes_kb(total, whole_show=data.get("whole_show", False)),
    )


# ---------- шаг 4: серии ----------

@router.callback_query(DownloadFlow.choosing_episodes, F.data == "ep:all")
async def on_ep_all(cq: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    total = len(data["episodes"])
    await state.update_data(ep_numbers=list(range(1, total + 1)))
    await cq.answer()
    await _ask_quality(cq, state)


@router.callback_query(DownloadFlow.choosing_episodes, F.data == "ep:range")
async def on_ep_range(cq: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(range_mode="range")
    await state.set_state(DownloadFlow.waiting_range)
    await cq.message.edit_text(
        "Введи номера серий текстом: например <code>3-7</code>, <code>5</code> "
        "или <code>1,3,5-8</code>."
    )
    await cq.answer()


@router.callback_query(DownloadFlow.choosing_episodes, F.data == "ep:from")
async def on_ep_from(cq: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    total = len(data["episodes"])
    await state.update_data(range_mode="from")
    await state.set_state(DownloadFlow.waiting_range)
    await cq.message.edit_text(
        f"С какой серии начать? Введи номер (сезон пойдёт от неё до {total}-й).\n"
        f"Например <code>5</code> → скачаю серии 5–{total}."
    )
    await cq.answer()


@router.message(DownloadFlow.waiting_range, F.text)
async def on_range_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    total = len(data["episodes"])
    mode = data.get("range_mode", "range")
    try:
        if mode == "from":
            start = int(message.text.strip())
            if not (1 <= start <= total):
                raise ValueError
            nums = list(range(start, total + 1))
        else:
            nums = parse_selection(message.text, total)
    except (ValueError, TypeError):
        hint = (f"Введи один номер от 1 до {total}." if mode == "from"
                else "Пример: <code>3-7</code> или <code>1,4,9</code>.")
        await message.answer(f"Не понял. {hint}")
        return
    await state.update_data(ep_numbers=nums)
    await _ask_quality_msg(message, state)


# ---------- шаг 5: качество ----------

async def _resolve_quality_options(state: FSMContext) -> list[int]:
    data = await state.get_data()
    voice = data["voice"]
    buttons = [_btn_from_dict(d) for d in data["ref_buttons"] if d["voice"] == voice]
    selected = data.get("ep_numbers", [1])
    if selected and selected[0] != 1:
        title = _title_from_state(data)
        episode = title.episodes[selected[0] - 1]
        buttons = [b for b in await get_parser(title.url).fetch_episode_players(title, episode)
                   if b.voice == voice]
    if not buttons:
        return []
    await state.update_data(provider="auto")
    failures = []
    qualities = await qualities_for_buttons(buttons, failures=failures)
    await state.update_data(quality_failed_providers=failures)
    return qualities


async def _quality_menu(state: FSMContext, qs: list[int]):
    import html
    failed = (await state.get_data()).get("quality_failed_providers", [])
    text = "Выбери качество:"
    kb = quality_kb(qs)
    if failed:
        text += "\n⚠ Не удалось проверить: " + html.escape(", ".join(failed))
        text += ". Их качества пока не показаны."
        kb.inline_keyboard.append([InlineKeyboardButton(text="🔄 Повторить проверку", callback_data="quality_retry")])
    return text, kb


async def _ask_quality(cq: CallbackQuery, state: FSMContext) -> None:
    await cq.message.edit_text("⏳ Получаю доступные качества…")
    try:
        qs = await _resolve_quality_options(state)
    except (ExtractError, ParseError) as e:
        capture(e, "quality_resolve_error")
        await cq.message.edit_text(f"⚠ Не удалось получить видео с этого плеера: {e}\n"
                                   "Попробуй позже или выбери другую озвучку: пришли ссылку заново.")
        await state.clear()
        return
    if not qs:
        await cq.message.edit_text("⚠ Не удалось определить качества. Пришли ссылку заново.")
        await state.clear()
        return
    await state.update_data(qualities=qs)
    await state.set_state(DownloadFlow.choosing_quality)
    text, kb = await _quality_menu(state, qs)
    await cq.message.edit_text(text, reply_markup=kb)


async def _ask_quality_msg(message: Message, state: FSMContext) -> None:
    status = await message.answer("⏳ Получаю доступные качества…")
    try:
        qs = await _resolve_quality_options(state)
    except (ExtractError, ParseError) as e:
        capture(e, "quality_resolve_error")
        await status.edit_text(f"⚠ Не удалось получить видео с этого плеера: {e}\n"
                               "Попробуй позже или выбери другую озвучку.")
        await state.clear()
        return
    if not qs:
        await status.edit_text("⚠ Не удалось определить качества. Пришли ссылку заново.")
        await state.clear()
        return
    await state.update_data(qualities=qs)
    await state.set_state(DownloadFlow.choosing_quality)
    text, kb = await _quality_menu(state, qs)
    await status.edit_text(text, reply_markup=kb)


@router.callback_query(DownloadFlow.choosing_quality, F.data == "quality_retry")
async def on_quality_retry(cq: CallbackQuery, state: FSMContext) -> None:
    await cq.answer()
    await _ask_quality(cq, state)


@router.callback_query(DownloadFlow.choosing_quality, F.data.startswith("q:"))
async def on_quality(cq: CallbackQuery, state: FSMContext, cfg: Config) -> None:
    q = int(cq.data.split(":")[1])
    data = await state.get_data()
    if q not in data.get("qualities", []):
        await cq.answer("Неверное качество", show_alert=True)
        return
    await state.update_data(quality=q, delivery="telegram", owner_flow=cq.from_user.id == cfg.admin_id)
    if cq.from_user.id == cfg.admin_id:
        await state.set_state(DownloadFlow.choosing_delivery)
        available = category_ready(cfg, data.get("media_category", "anime"))
        text = "Куда отправить серии?"
        if not available:
            text += "\n\nМедиатека пока недоступна: подключи папку, задай LIBRARY_DIR и создай маркер .animedl-library на подключённой шаре. Telegram уже работает."
        await cq.message.edit_text(text, reply_markup=delivery_kb())
    else:
        await _ask_confirm(cq, state)
    await cq.answer()


@router.callback_query(DownloadFlow.choosing_delivery, F.data.startswith("delivery:"))
async def on_delivery(cq: CallbackQuery, state: FSMContext, cfg: Config) -> None:
    if cq.from_user.id != cfg.admin_id:
        await cq.answer("Медиатека доступна только владельцу", show_alert=True)
        return
    mode = cq.data.split(":", 1)[1]
    try:
        validate_delivery(cfg, cq.from_user.id, mode, (await state.get_data()).get("media_category", "anime"))
    except (ValueError, PermissionError) as e:
        await cq.answer(str(e), show_alert=True)
        return
    await state.update_data(delivery=mode)
    if mode == "telegram":
        await _ask_confirm(cq, state)
    else:
        data = await state.get_data()
        selected = data.get("media_category", "anime")
        await state.set_state(DownloadFlow.choosing_category)
        await cq.message.edit_text("Категория медиатеки (отмечена категория источника):", reply_markup=category_kb(selected))
    await cq.answer()


@router.callback_query(DownloadFlow.choosing_category, F.data.startswith("category:"))
async def on_category(cq: CallbackQuery, state: FSMContext, cfg: Config) -> None:
    if cq.from_user.id != cfg.admin_id:
        await cq.answer("Медиатека доступна только владельцу", show_alert=True)
        return
    category = cq.data.split(":", 1)[1]
    data = await state.get_data()
    try:
        is_movie(category)
        validate_delivery(cfg, cq.from_user.id, data.get("delivery", "library"), category)
        if is_movie(category) and len(data.get("ep_numbers", [])) != 1:
            raise ValueError("Для фильма выбери один медиафайл, а не несколько серий")
    except (ValueError, PermissionError) as e:
        await cq.answer(str(e), show_alert=True)
        return
    await state.update_data(library_category=category)
    await _ask_library_details(cq, state)
    await cq.answer()


async def _ask_library_details(cq: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if is_movie(data.get("library_category", data.get("media_category", "anime"))):
        await state.update_data(library_title=data["title"], library_season=None, library_season_name="")
        await _ask_confirm(cq, state)
        return
    if data.get("seasons"):
        await state.update_data(library_title=data["title"], library_season=data.get("season"), library_season_name="")
        await _ask_confirm(cq, state)
        return
    name, season, season_name = library_selection_suggestion(data["title"])
    name = data.get("source_series_title") or name
    season = data.get("season") or season
    if season is None and not season_name:
        season = 1
    await state.update_data(library_title=name, library_season=season, library_season_name=season_name)
    await _ask_confirm(cq, state)


@router.callback_query(DownloadFlow.confirm, F.data == "library:details:edit")
async def on_library_details_edit(cq: CallbackQuery, state: FSMContext, cfg: Config) -> None:
    import html
    if cq.from_user.id != cfg.admin_id:
        await cq.answer("Медиатека доступна только владельцу", show_alert=True)
        return
    data = await state.get_data()
    if data.get("delivery", "telegram") == "telegram" or data.get("whole_show"):
        await cq.answer("Для этой загрузки изменение сезона недоступно", show_alert=True)
        return
    await state.set_state(DownloadFlow.choosing_library_details)
    label = data.get("library_season_name") or str(data.get("library_season") or 1)
    movie = is_movie(data.get("library_category", "anime"))
    current = data["library_title"] if movie else f"{label} | {data['library_title']}"
    await cq.message.edit_text(
        f"Сейчас: <code>{html.escape(current)}</code>\n\n" +
        ("Введи новое название фильма." if movie else
         "Введи <code>номер или название сезона | название сериала</code>, "
         "например <code>3 | Необъятный океан</code> или <code>Бедствие | Блич</code>.\n"
         "Для всех сезонов используй одинаковое название сериала." +
         ("\nНомер сезона для этого источника фиксирован; можно изменить название сериала." if data.get("seasons") else "")),
        reply_markup=library_details_kb(True))
    await cq.answer()


@router.callback_query(DownloadFlow.choosing_library_details, F.data == "library:details:confirm")
async def on_library_details_confirm(cq: CallbackQuery, state: FSMContext, cfg: Config) -> None:
    if cq.from_user.id != cfg.admin_id:
        await cq.answer("Медиатека доступна только владельцу", show_alert=True)
        return
    data = await state.get_data()
    try:
        validate_library_details(data.get("library_title"), data.get("library_season"), data.get("library_season_name", ""), data.get("library_category", "anime"))
    except ValueError as e:
        await cq.answer(str(e), show_alert=True)
        return
    await _ask_confirm(cq, state)
    await cq.answer()


@router.message(DownloadFlow.choosing_library_details, F.text)
async def on_library_details_text(message: Message, state: FSMContext, cfg: Config) -> None:
    if not message.from_user or message.from_user.id != cfg.admin_id:
        return
    try:
        data = await state.get_data()
        category = data.get("library_category", "anime")
        if is_movie(category):
            name, season, season_name = " ".join(message.text.split()), None, ""
        else:
            raw_season, name = (" ".join(part.split()) for part in message.text.split("|", 1))
            if name.lstrip("+-").isdigit() and not raw_season.lstrip("+-").isdigit():
                raw_season, name = name, raw_season
            numeric = raw_season.lstrip("+-").isdigit()
            season = int(raw_season) if numeric else None
            season_name = "" if numeric else raw_season
            if data.get("seasons") and (season != data.get("season") or season_name):
                await message.answer("Номер сезона берётся из источника. Оставь его прежним и измени название сериала.")
                return
        validate_library_details(name, season, season_name, category)
    except (ValueError, TypeError):
        await message.answer("Введи название или номер сезона и сериал: <code>Бедствие | Блич</code> или <code>2 | One Room</code>.")
        return
    await state.update_data(library_season=season, library_season_name=season_name, library_title=name)
    status = await message.answer("Проверяю параметры…")
    await _show_confirmation(status, state)


async def _ask_confirm(cq: CallbackQuery, state: FSMContext) -> None:
    await _show_confirmation(cq.message, state)


async def _show_confirmation(message: Message, state: FSMContext) -> None:
    import html
    data = await state.get_data()
    nums = data["ep_numbers"]
    rng = str(nums[0]) if len(nums) == 1 else f"{nums[0]}–{nums[-1]} ({len(nums)} шт.)"
    if data.get("whole_show"):
        rng = f"Весь сериал: {len(nums)} серий, {len(data['seasons'])} сезонов"
    elif data.get("seasons"):
        rng = f"Сезон {data['season']} · {rng}"
    label = {"telegram": "Telegram", "library": "Медиатека", "both": "Telegram + медиатека"}[data["delivery"]]
    delivery_line = f"\n📁 Доставка: <b>{label}</b>" if data.get("owner_flow") else ""
    library_details = ""
    if data["delivery"] != "telegram":
        category = data.get("library_category", data.get("media_category", "anime"))
        library_details = f"\n📚 {MEDIA_CATEGORIES[category][0]}: <b>{html.escape(data['library_title'])}</b>"
        if not is_movie(category):
            season_label = "все" if data.get("whole_show") else (data.get("library_season_name") or str(data["library_season"]))
            library_details += f" · сезон <b>{html.escape(season_label)}</b>"
    await state.set_state(DownloadFlow.confirm)
    await message.edit_text(
        "Проверь параметры:\n"
        f"🎬 <b>{html.escape(data['title'])}</b>\n"
        f"🎙 Озвучка: <b>{html.escape(data['voice'])}</b>\n"
        f"🔢 Серии: <b>{rng}</b>\n"
        f"📺 Качество: <b>{data['quality']}p</b>{delivery_line}{library_details}",
        reply_markup=confirm_kb(edit_library=data["delivery"] != "telegram" and not data.get("whole_show")))


# ---------- шаг 6: подтверждение и загрузка ----------

@router.callback_query(DownloadFlow.confirm, F.data.in_({"go", "retry_failed"}))
async def on_go(cq: CallbackQuery, state: FSMContext, bot: Bot,
                cfg: Config, cache: CacheStore) -> None:
    data = await state.get_data()
    delivery = data.get("delivery", "telegram")
    if getattr(cq, "data", "go") == "retry_failed" and data.get("retry_message") != cq.message.message_id:
        await cq.answer("Эта попытка уже устарела. Используй последнее сообщение.", show_alert=True)
        return
    try:
        category = data.get("library_category", data.get("media_category", "anime"))
        validate_delivery(cfg, cq.from_user.id, delivery, category)
        if delivery != "telegram":
            season = data.get("library_season")
            if data.get("seasons"):
                season = data["episodes"][0].get("season")
            validate_library_details(data.get("library_title"), season, data.get("library_season_name", ""), category)
            if is_movie(category) and len(data.get("ep_numbers", [])) != 1:
                raise ValueError("Для фильма выбери один медиафайл")
    except (ValueError, PermissionError) as e:
        await cq.answer(str(e), show_alert=True)
        return
    await state.set_state(DownloadFlow.processing)
    await cq.answer()

    parser = get_parser(data["url"])
    title = _title_from_state(data)
    ep_by_num = {i: e for i, e in enumerate(title.episodes, 1)} if data.get("seasons") else {e.number: e for e in title.episodes}
    nums = data["ep_numbers"]
    voice, provider, quality = data["voice"], data["provider"], data["quality"]

    chat_id = cq.message.chat.id
    ctrl = start_job(chat_id)
    total = len(nums)
    status = cq.message

    async def on_status(text: str) -> None:
        suffix = "\n\n⏸ На паузе" if ctrl.is_paused else ""
        try:
            await status.edit_text(text + suffix, reply_markup=job_kb(ctrl.is_paused))
        except Exception:  # not modified / flood — не критично
            pass

    failed_nums = []
    is_admin = cq.from_user.id == cfg.admin_id
    if is_admin:
        # админ — приоритет: скачивает сразу, минуя общую очередь
        failed_nums = await _run_season(bot, cfg, cache, parser, title, ep_by_num, nums,
                          voice, provider, quality, ctrl, chat_id, status, on_status,
                          user_id=cq.from_user.id, delivery=delivery,
                          library_title=data.get("library_title"), library_season=data.get("library_season"),
                          library_season_name=data.get("library_season_name", ""), library_category=data.get("library_category"))
    else:
        # глобальная очередь: одновременно машина тянет ограниченное число задач
        if is_busy():
            await on_status("⏳ Бот сейчас занят другой загрузкой — ты в очереди, жди…")
        async with global_semaphore():
            if not ctrl.cancelled:
                failed_nums = await _run_season(bot, cfg, cache, parser, title, ep_by_num, nums,
                                  voice, provider, quality, ctrl, chat_id, status, on_status,
                          user_id=cq.from_user.id, delivery=delivery,
                          library_title=data.get("library_title"), library_season=data.get("library_season"),
                          library_season_name=data.get("library_season_name", ""), library_category=data.get("library_category"))
    end_job(chat_id)
    if failed_nums:
        await state.update_data(ep_numbers=failed_nums, retry_message=status.message_id)
        await state.set_state(DownloadFlow.confirm)
    else:
        await state.clear()


async def _run_season(bot, cfg, cache, parser, title, ep_by_num, nums,
                      voice, provider, quality, ctrl, chat_id, status, on_status,
                      *, user_id=None, delivery="telegram", library_title=None, library_season=None, library_season_name="", library_category=None):
    total = len(nums)
    await on_status(f"🚀 Начинаю загрузку: {total} серий ({voice} · {quality}p)")

    results = []
    result_numbers = []
    done = 0
    async def process(ep, mode):
        try:
            return await process_episode(
                bot=bot, cfg=cfg, cache=cache, parser=parser, title=title,
                episode=ep, voice=voice, provider=provider, quality=quality,
                on_status=on_status, chat_id=chat_id, cancel_check=lambda: ctrl.cancelled,
                user_id=user_id, delivery=mode, library_title=library_title, library_season=library_season,
                library_season_name=library_season_name, library_category=library_category,
            )
        except Exception as e:  # noqa: BLE001
            capture(e, "episode_unhandled_error", user_id, title=title.title, site=title.site,
                    episode=ep.number, provider=provider, quality=quality)
            log.exception("Ошибка обработки серии %s", ep.number)
            return EpisodeResult(ep.number, "error", note=str(e), season=ep.season)

    for n in nums:
        # пауза между сериями (даёт возможность продолжить/отменить)
        if ctrl.is_paused:
            await on_status(f"⏸ Пауза. Обработано {done}/{total}. "
                            f"Нажми «Продолжить» или «Стоп».")
            await ctrl.wait_if_paused()
        if ctrl.cancelled:
            break
        ep = ep_by_num.get(n)
        if ep is None:
            res = EpisodeResult(n, "error", note="Источник: выбранная серия отсутствует в списке источника",
                                telegram="error" if delivery != "library" else "skipped",
                                library="error" if delivery != "telegram" else "skipped")
        else:
            res = await process(ep, delivery)
        results.append(res)
        result_numbers.append(n)
        done += 1
        if res.status == "cancelled" or ctrl.cancelled:
            break

    # Retry exhausted source failures once after the other episodes have finished.
    # Keep job ordinals: episode numbers can repeat across seasons of a whole show.
    failed = [i for i, r in enumerate(results) if r.source_failed]
    if failed and not ctrl.cancelled and not any(r.status == "cancelled" for r in results):
        await on_status(f"🔄 Повторно проверяю недоступные серии: {len(failed)}. Успешные серии повторяться не будут.")
        for i in failed:
            if ctrl.is_paused:
                await on_status("⏸ Повторная загрузка на паузе. Нажми «Продолжить» или «Стоп».")
                await ctrl.wait_if_paused()
            if ctrl.cancelled:
                break
            previous = results[i]
            # A cached Telegram video may already be delivered when the source
            # failed for the library. Retry only its missing channel.
            delivered_tg = previous.telegram in ("sent", "cached")
            mode = "library" if delivered_tg else delivery
            recovered = await process(ep_by_num[result_numbers[i]], mode)
            if delivered_tg:
                recovered.telegram = previous.telegram
                if recovered.status != "cancelled":
                    recovered.status = "sent_new" if recovered.library == "saved" else "partial"
            results[i] = recovered
            if recovered.status == "cancelled" or ctrl.cancelled:
                break

    # Report each selected delivery channel, including partial success.
    import html
    was_cancelled = ctrl.cancelled or any(r.status == "cancelled" for r in results)
    failures = [r for r in results if r.status in ("error", "partial")]
    head = "🛑 Остановлено." if was_cancelled else ("⚠ Завершено с ошибками." if failures else "✅ Готово.")
    lines = [f"{head} Обработано {len(results)}/{total} серий."]
    if delivery != "library":
        sent = sum(r.telegram == "sent" for r in results)
        cached = sum(r.telegram == "cached" for r in results)
        errors = sum(r.telegram == "error" for r in results)
        lines.append(f"Telegram: {sent} новых, {cached} из кэша, {errors} ошибок.")
    if delivery != "telegram":
        saved = sum(r.library == "saved" for r in results)
        errors = sum(r.library == "error" for r in results)
        lines.append(f"Медиатека: {saved} сохранено, {errors} ошибок.")
    # A whole show can exceed Telegram's 4096-character limit. Keep the result
    # message deliverable and put errors before routine metadata notices.
    noted = sorted((r for r in results if r.note), key=lambda r: r.status not in ("error", "partial"))
    shown = 0
    for r in noted:
        note = r.note[:500] + ("…" if len(r.note) > 500 else "")
        line = "• " + (f"сезон {r.season}, " if r.season is not None else "") + f"серия {r.episode}: {html.escape(note)}"
        if len("\n".join(lines + [line])) > 3800:
            break
        lines.append(line)
        shown += 1
    if shown < len(noted):
        lines.append(f"Ещё {len(noted) - shown} примечаний; итоги доставки указаны выше.")
    try:
        await status.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔄 Повторить неудавшиеся", callback_data="retry_failed")
        ]]) if failures else None)
    except Exception:
        pass
    return [result_numbers[i] for i, r in enumerate(results) if r.status in ("error", "partial")]


# ---------- управление задачей: пауза / продолжить / стоп ----------

@router.callback_query(F.data == "job:pause")
async def on_job_pause(cq: CallbackQuery) -> None:
    ctrl = get_job(cq.message.chat.id)
    if ctrl:
        ctrl.pause()
        await cq.answer("Пауза после текущей серии")
    else:
        await cq.answer("Нет активной задачи")


@router.callback_query(F.data == "job:resume")
async def on_job_resume(cq: CallbackQuery) -> None:
    ctrl = get_job(cq.message.chat.id)
    if ctrl:
        ctrl.resume()
        await cq.answer("Продолжаю")
    else:
        await cq.answer("Нет активной задачи")


@router.callback_query(F.data == "job:cancel")
async def on_job_cancel(cq: CallbackQuery) -> None:
    ctrl = get_job(cq.message.chat.id)
    if ctrl:
        ctrl.cancel()
        await cq.answer("Останавливаю…")
    else:
        await cq.answer("Нет активной задачи")


# ---------- назад/отмена ----------

@router.callback_query(F.data == "cancel")
async def on_cancel(cq: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cq.message.edit_text("Отменено. Пришли новую ссылку, когда будешь готов.")
    await cq.answer()


@router.callback_query(F.data == "back:voice")
async def back_voice(cq: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.set_state(DownloadFlow.choosing_voice)
    await cq.message.edit_text("Выбери озвучку:", reply_markup=voices_kb(data["voices"], data.get("preferred_voice", ""), has_seasons=bool(data.get("seasons"))))
    await cq.answer()


@router.callback_query(DownloadFlow.choosing_voice, F.data == "back:season")
async def back_season(cq: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("seasons"):
        await cq.answer("Сезоны недоступны")
        return
    await state.set_state(DownloadFlow.choosing_season)
    await cq.message.edit_text("Выбери сезон или весь сериал:", reply_markup=seasons_kb(data["seasons"], data.get("season")))
    await cq.answer()
