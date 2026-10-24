"""Оркестрация: перечисление озвучек/качеств и обработка одной серии (кэш→скачать→отправить)."""
from __future__ import annotations

import logging
import time
import json
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
import asyncio
import tempfile
import html
import errno
import re
import shutil
import ssl
from typing import Awaitable, Callable

import httpx

from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError, TelegramServerError

from .admin_ops import ArchiveOutcome, archive_to_library, category_ready, library_ready
from .artwork import download_poster, create_episode_thumbnail
from .cache.store import CacheStore
from .config import Config
from .download.downloader import DownloadCancelled, DownloadError, Progress, download_variant
from .jellyfin import refresh_library
from .recovery import RecoveryStore
from .download.splitter import split_for_telegram
from .models import EpisodeRef, PlayerButton, TitleInfo, VideoVariant, make_cache_hash, validate_library_details
from .players.registry import extract_variants
from .sites.base import SiteParser
from .telegram_client import extract_file_id, send_video_by_id, send_video_file
from .observability import capture, emit

log = logging.getLogger(__name__)

# Эвристические подсказки максимального качества для UI (до реального извлечения).
PROVIDER_QUALITY_HINT = {"Kodik": 720, "AniBoom": 1080, "CVH": 1080}

# Приоритет провайдеров в выдаче: сперва лучший рабочий вариант по качеству.
# AniBoom (до 1080p) и CVH (cdnvideohub/VK, до 1080p+) — оба поддержаны; Kodik
# (720p) — надёжный фолбэк; остальное — через yt-dlp.
_PROVIDER_RANK = {"AniBoom": 0, "CVH": 1, "Kodik": 2}

StatusCb = Callable[[str], Awaitable[None]]


def _source_errors(error: Exception):
    """Inspect wrapped downloader errors without dropping the original cause."""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        yield error
        error = error.__cause__ or error.__context__


def _retryable_source_error(error: Exception) -> bool:
    chain = list(_source_errors(error))
    # A fresh URL cannot fix local permissions, space or invalid media.
    if any(isinstance(e, (ValueError, DownloadCancelled)) or
           (isinstance(e, OSError) and e.errno in (errno.ENOSPC, errno.EACCES, errno.EROFS, errno.EDQUOT))
           for e in chain):
        return False
    for e in chain:
        if isinstance(e, httpx.HTTPStatusError):
            return e.response.status_code in (401, 403, 408, 429) or e.response.status_code >= 500
        if isinstance(e, (httpx.TransportError, TimeoutError, ConnectionError, ssl.SSLError)):
            return True
    if isinstance(error, DownloadError):
        text = str(error).lower()
        if any(marker in text for marker in ("no space left", "disk full", "permission denied", "invalid data")):
            return False
        return bool(re.search(r"certificate_verify_failed|certificate has expired|certificate_expired|"
                              r"timed? out|timeout|connection (?:reset|refused|aborted)|"
                              r"temporary failure in name resolution|network is unreachable|"
                              r"http error (?:401|403|408|429|5\d\d)", text))
    return False


def _source_error_note(error: Exception) -> str:
    text = str(error).lower()
    if any(isinstance(e, OSError) and e.errno in (errno.ENOSPC, errno.EDQUOT)
           for e in _source_errors(error)) or "no space left" in text:
        return "Недостаточно свободного места на диске для загрузки видео"
    if "http error 403" in text or any(isinstance(e, httpx.HTTPStatusError)
            and e.response.status_code == 403 for e in _source_errors(error)):
        return "Источник отклонил загрузку (HTTP 403); обновление ссылки не помогло. Это не ограничение размера Telegram"
    if "certificate has expired" in text or "certificate_expired" in text:
        return "У источника истёк SSL-сертификат; повтор с новым адресом не помог. Попробуй позже"
    if any(isinstance(e, ssl.SSLError) for e in _source_errors(error)) or "certificate_verify_failed" in text:
        return "Источник не прошёл проверку SSL-сертификата. Попробуй позже"
    if _retryable_source_error(error):
        return "Источник временно недоступен; повтор с новым адресом не помог. Попробуй позже"
    return str(error)


@dataclass
class EpisodeResult:
    episode: int
    status: str            # sent_new | sent_cached | partial | error | cancelled
    note: str = ""         # напр. "провайдер заменён на AniBoom"
    size_bytes: int = 0
    telegram: str = "skipped"
    library: str = "skipped"
    season: int | None = None
    source_failed: bool = False


def group_voices(buttons: list[PlayerButton]) -> "OrderedDict[str, list[PlayerButton]]":
    """voice -> список кнопок (по одному провайдеру каждая), с сохранением порядка."""
    voices: OrderedDict[str, list[PlayerButton]] = OrderedDict()
    for b in buttons:
        voices.setdefault(b.voice, []).append(b)
    return voices


def providers_for_voice(buttons: list[PlayerButton]) -> list[tuple[str, int]]:
    seen: OrderedDict[str, int] = OrderedDict()
    for b in buttons:
        seen.setdefault(b.provider, PROVIDER_QUALITY_HINT.get(b.provider, 0))
    items = list(seen.items())
    items.sort(key=lambda it: _PROVIDER_RANK.get(it[0], 99))
    return items


def _pick_variant(variants: list[VideoVariant], quality: int) -> VideoVariant:
    exact = [v for v in variants if v.quality == quality]
    if exact:
        return exact[0]
    lower = sorted((v for v in variants if v.quality <= quality),
                   key=lambda v: v.quality, reverse=True)
    if lower:
        return lower[0]
    return max(variants, key=lambda v: v.quality)


async def qualities_for_button(button: PlayerButton) -> list[int]:
    variants = await extract_variants(button)
    return sorted({v.quality for v in variants}, reverse=True)


async def qualities_for_buttons(buttons: list[PlayerButton], *, failures: list[str] | None = None) -> list[int]:
    """Offer actual qualities from every player, even if one player is broken."""
    limit = asyncio.Semaphore(3)

    async def probe(button):
        async with limit:
            try:
                return await asyncio.wait_for(qualities_for_button(button), timeout=45)
            except Exception as exc:
                capture(exc, "quality_probe_error", provider=button.provider)
                log.info("Quality probe failed for %s (%s)", button.provider, type(exc).__name__)
                if failures is not None and button.provider not in failures:
                    failures.append(button.provider)
                return []

    results = await asyncio.gather(*(probe(button) for button in buttons))
    return sorted({quality for qualities in results for quality in qualities if quality > 0}, reverse=True)


def validate_delivery(cfg: Config, user_id: int | None, mode: str, category: str = "anime") -> None:
    if mode not in ("telegram", "library", "both"):
        raise ValueError("Неизвестный способ доставки")
    if mode != "telegram":
        if user_id != cfg.admin_id:
            raise PermissionError("Медиатека доступна только владельцу бота")
        if not category_ready(cfg, category):
            raise ValueError("Хранилище не подключено: проверь LIBRARY_DIR, подключение и маркер .animedl-library")


async def process_episode(
    *, bot: Bot, cfg: Config, cache: CacheStore, parser: SiteParser,
    title: TitleInfo, episode: EpisodeRef, voice: str, provider: str,
    quality: int, on_status: StatusCb, chat_id: int,
    cancel_check=None, user_id: int | None = None, delivery: str = "telegram",
    library_title: str | None = None, library_season: int | None = None, library_season_name: str = "",
    library_category: str | None = None,
) -> EpisodeResult:
    audit_started = time.monotonic()
    audit_job = uuid.uuid4().hex
    actual_quality = quality
    def audit_result(result):
        emit("episode_finished", user_id, result.status, job_id=audit_job,
             title=title.title, site=title.site, episode=episode.number, season=episode.season,
             voice=voice, provider=provider, quality=actual_quality, delivery=delivery,
             category=library_category or title.media_category, bytes=result.size_bytes,
             telegram=result.telegram, library=result.library, source_failed=result.source_failed,
             duration_ms=int((time.monotonic() - audit_started) * 1000))
        return result
    emit("episode_started", user_id, job_id=audit_job, title=title.title, site=title.site,
         episode=episode.number, season=episode.season, voice=voice, provider=provider,
         quality=quality, delivery=delivery, category=library_category or title.media_category)
    auto_provider = provider == "auto"
    category = library_category or title.media_category
    from .models import is_movie
    movie = is_movie(category)
    validate_delivery(cfg, user_id, delivery, category)
    wants_tg, wants_library = delivery != "library", delivery != "telegram"
    episode_season = episode.season if episode.season is not None else library_season
    episode_season_name = "" if episode.season is not None else library_season_name
    if wants_library:
        validate_library_details(library_title, episode_season, episode_season_name, category)
        library_title = " ".join(library_title.split())
    res = EpisodeResult(episode.number, "error", season=episode.season)
    notes = []
    tag = (f"Сезон {episode.season} · " if episode.season is not None else "") + f"Серия {episode.number}"
    cache_identity = title.anime_id + (f"|season:{episode.season}" if episode.season is not None else "")
    key = make_cache_hash(title.site, cache_identity, episode.number, voice, provider, quality)
    recovery_key = key
    budget = getattr(cfg, "retry_cache_bytes", 0)
    work_root = cfg.tmp_dir
    recovery_root = cfg.cache_db_path.parent / "recovery" if budget else None
    if user_id is not None and user_id == getattr(cfg, "admin_id", None) and library_ready(getattr(cfg, "media_root", None)):
        try:
            work_root = cfg.media_root / ".animedl-work"
            work_root.mkdir(mode=0o700, exist_ok=True)
            recovery_root = work_root / "recovery"
        except OSError:
            work_root = cfg.tmp_dir
    recovery = RecoveryStore(recovery_root, budget,
        getattr(cfg, "retry_cache_hours", 24) * 3600) if budget else None
    def video_caption(q):
        label = f"сезон {episode.season}, серия {episode.number}" if episode.season is not None else f"серия {episode.number}"
        return html.escape(f"{title.title} — {label} • {voice} • {provider} • {q}p")
    caption = video_caption(quality)

    async def cached_send(file_id):
        try:
            await on_status(f"{tag}: отправка из кэша Telegram ⚡")
            ids = json.loads(file_id[6:]) if file_id.startswith("parts:") else [file_id]
            for i, item in enumerate(ids, 1):
                suffix = f" • часть {i}/{len(ids)}" if len(ids) > 1 else ""
                await send_video_by_id(bot, chat_id, item, caption=caption + suffix)
            res.telegram = "cached"
        except Exception as e:
            res.telegram = "skipped"
            notes.append(f"Кэш Telegram недоступен, отправляю заново: {e}")

    def finish():
        channels = [res.telegram] if wants_tg else []
        if wants_library:
            channels.append(res.library)
        if all(c in ("sent", "cached", "saved") for c in channels):
            res.status = "sent_cached" if res.telegram == "cached" and not wants_library else "sent_new"
        elif any(c in ("sent", "cached", "saved") for c in channels):
            res.status = "partial"
        else:
            res.status = "error"
        res.note = "; ".join(notes)
        return audit_result(res)

    cached = cache.get(key, expected_quality=quality) if wants_tg and not auto_provider else None
    if wants_tg and auto_provider:
        for cached_provider in ("AniBoom", "CVH", "Kodik", "Alloha", "PLPlayer"):
            cached_key = make_cache_hash(title.site, cache_identity, episode.number, voice, cached_provider, quality)
            cached = cache.get(cached_key, expected_quality=quality)
            if cached:
                provider = cached_provider
                caption = html.escape(f"{title.title} — {tag.lower()} • {voice} • {cached_provider} • {quality}p")
                break
    if cached:
        await cached_send(cached)
        if not wants_library and res.telegram == "cached":
            return finish()

    # Each job has its own folder: different users/voices cannot overwrite temp files.
    with tempfile.TemporaryDirectory(prefix="episode_", dir=work_root) as tmp:
        try:
            recovered = None
            if recovery:
                try:
                    recovered = await asyncio.to_thread(recovery.load, recovery_key, Path(tmp) / "recovered.mp4")
                except Exception as exc:
                    log.warning("Recovery cache read failed (%s)", type(exc).__name__)
            if recovered:
                result, provider, actual_quality = recovered
                variant = VideoVariant(actual_quality, "")
                key = make_cache_hash(title.site, cache_identity, episode.number, voice, provider, actual_quality)
                await on_status(f"{tag}: использую сохранённое видео, повторное скачивание не требуется…")
            else:
                await on_status(f"{tag}: ищу источник…")
                buttons = await parser.fetch_episode_players(title, episode)
                voices = group_voices(buttons)
                if voice not in voices:
                    # A partial catalogue response must not stay cached for the whole job.
                    await parser.fetch_title(title.url)
                    buttons = await parser.fetch_episode_players(title, episode)
                    voices = group_voices(buttons)
                if voice not in voices:
                    raise ValueError(f"озвучка «{voice}» недоступна для этой серии")
                if auto_provider:
                    candidates = sorted(voices[voice], key=lambda b: _PROVIDER_RANK.get(b.provider, 99))
                else:
                    button = next((b for b in voices[voice] if b.provider == provider), None)
                    if button is None:
                        button = min(voices[voice], key=lambda b: _PROVIDER_RANK.get(b.provider, 99))
                        notes.append(f"провайдер {provider} недоступен → {button.provider}")
                    candidates = [button]
                result = None
                last_source_error = None
                for player_index, button in enumerate(candidates):
                    try:
                        for attempt in range(3):
                            if cancel_check and cancel_check():
                                raise DownloadCancelled()
                            attempt_dir = Path(tmp) / f"player_{player_index}_attempt_{attempt + 1}"
                            attempt_dir.mkdir()
                            try:
                                await on_status(f"{tag}: получаю видео…")
                                # Refresh signed links, keeping the selected voice and provider.
                                variants = await extract_variants(button)
                                if auto_provider:
                                    variant = next((v for v in variants if v.quality == quality), None)
                                    if variant is None:
                                        raise ValueError(f"{button.provider}: {quality}p недоступно")
                                else:
                                    variant = _pick_variant(variants, quality)
                                provider = button.provider
                                actual_quality = variant.quality
                                caption = video_caption(variant.quality)
                                key = make_cache_hash(title.site, cache_identity, episode.number, voice, provider, variant.quality)
                                if wants_tg and res.telegram == "skipped":
                                    cached = cache.get(key, expected_quality=variant.quality)
                                    if cached:
                                        await cached_send(cached)
                                        if not wants_library and res.telegram == "cached":
                                            return finish()
                                progress = Progress()

                                async def pump():
                                    last = -1
                                    while progress.status in ("queued", "downloading", "processing"):
                                        if progress.status == "downloading" and abs(progress.percent - last) >= 3:
                                            last = progress.percent
                                            await on_status(f"{tag}: скачивание {progress.percent:.0f}% ({variant.quality}p)")
                                        elif progress.status == "processing":
                                            await on_status(f"{tag}: обработка видео…")
                                        await asyncio.sleep(2)

                                pump_task = asyncio.create_task(pump())
                                try:
                                    result = await download_variant(variant, attempt_dir / "video", progress, cancel_check=cancel_check)
                                    # CDN labels can lie (e.g. 720p points at the 480p file).
                                    # Width also accounts for cinematic letterboxing/cropped video.
                                    if max(result.height, result.width * 9 / 16) < variant.quality * 0.94:
                                        result = None
                                        raise ValueError(f"{button.provider}: файл ниже заявленного качества {variant.quality}p; отправка отменена")
                                finally:
                                    pump_task.cancel()
                                    await asyncio.gather(pump_task, return_exceptions=True)
                                break
                            except DownloadCancelled:
                                raise
                            except Exception as error:
                                shutil.rmtree(attempt_dir)
                                if cancel_check and cancel_check():
                                    raise DownloadCancelled() from error
                                if attempt == 2 or not _retryable_source_error(error):
                                    raise
                                await on_status(f"{tag}: источник временно недоступен, обновляю ссылку (попытка {attempt + 2}/3)…")
                                await asyncio.sleep(attempt + 1)
                        break
                    except DownloadCancelled:
                        raise
                    except Exception as error:
                        # Local disk/permissions and cancellation are not repaired by another player.
                        local_failure = any(isinstance(e, OSError) and e.errno in
                            (errno.ENOSPC, errno.EACCES, errno.EROFS, errno.EDQUOT)
                            for e in _source_errors(error))
                        if not auto_provider or local_failure:
                            raise
                        last_source_error = error
                        log.info("Player %s failed for %sp (%s)", button.provider, quality, type(error).__name__)
                if result is None:
                    if last_source_error and _retryable_source_error(last_source_error):
                        raise last_source_error
                    raise ValueError(f"Качество {quality}p недоступно у работающих плееров этой озвучки; качество не снижено")
            res.size_bytes = result.size_bytes
            if cancel_check and cancel_check():
                raise DownloadCancelled()
        except DownloadCancelled:
            res.status = "cancelled"
            res.note = "отменено на этой серии"
            return audit_result(res)
        except Exception as e:
            capture(e, "source_error", user_id, job_id=audit_job, title=title.title,
                    site=title.site, provider=provider, quality=quality, episode=episode.number)
            res.source_failed = True
            if wants_tg and res.telegram == "skipped":
                res.telegram = "error"
            if wants_library:
                res.library = "error"
            notes.append(f"Источник: {_source_error_note(e)}")
            return finish()

        # Archive first, so a slow or failed Telegram upload cannot block local playback.
        if wants_library:
            try:
                from .models import episode_name
                display_title = episode.title
                if category == "anime" and title.mal_id and episode_name(display_title, episode.number) == f"Серия {episode.number}":
                    from .episode_metadata import get_episode_names
                    try:
                        names = await get_episode_names(title.mal_id, getattr(cfg, "cache_db_path", cfg.tmp_dir).parent)
                        display_title = names.get(episode.number, display_title)
                    except Exception as exc:
                        log.info("Optional episode metadata unavailable: %s", type(exc).__name__)
                await on_status(f"{tag}: сохранение в медиатеку…")
                if not category_ready(cfg, category):
                    raise OSError("Медиатека категории отключена")
                archive_outcome = ArchiveOutcome()
                archived_path = await asyncio.to_thread(archive_to_library, result.path, cfg.category_dir(category),
                    library_title, episode.number, season=episode_season, season_name=episode_season_name, voice=voice, quality=variant.quality,
                    provider=provider, identity=f"library-series|{library_title.casefold()}",
                    source_identity=f"{title.site}|{cache_identity}", source_url=title.url,
                    media_tag=category, outcome=archive_outcome, episode_title=display_title)
                res.library = "saved"
                if not archive_outcome.updated:
                    notes.append(f"Медиатека: оставлено имеющееся качество {archive_outcome.quality}p")
                elif archive_outcome.upgraded:
                    notes.append(f"Медиатека: качество повышено до {archive_outcome.quality}p")
            except Exception as e:
                res.library = "error"
                notes.append(f"Медиатека: {e}")
                log.warning("Не удалось сохранить серию в медиатеку: %s", e)
                capture(e, "library_error", user_id, job_id=audit_job, title=title.title, site=title.site)
        if res.library == "saved" and archive_outcome.updated:
            try:
                if not await create_episode_thumbnail(archived_path, result.duration):
                    notes.append("Миниатюра эпизода недоступна; видео сохранено")
            except Exception:
                notes.append("Миниатюра эпизода недоступна; видео сохранено")
            if title.poster:
                try:
                    if not await download_poster(title.poster, archived_path.parent if movie else archived_path.parent.parent,
                                                 referer=title.url, season_dir=None if movie else archived_path.parent):
                        notes.append("Постер источника недоступен; видео сохранено")
                except Exception:
                    notes.append("Постер источника недоступен; видео сохранено")
        if res.library == "saved" and (archive_outcome.updated or archive_outcome.metadata_updated):
            try:
                if await refresh_library(cfg):
                    notes.append("Jellyfin: обновление библиотеки запрошено")
            except Exception as e:
                # Do not include the URL, request headers or response body: they may contain secrets.
                import httpx
                reason = f"HTTP {e.response.status_code}" if isinstance(e, httpx.HTTPStatusError) else type(e).__name__
                notes.append(f"Jellyfin: обновление не удалось ({reason}); файл сохранён, обнови библиотеку вручную")
                log.warning("Не удалось запросить обновление Jellyfin (%s)", reason)
        if wants_tg and res.telegram == "skipped":
            try:
                result.meta.setdefault("upload_id", uuid.uuid4().hex)
                await on_status(f"{tag}: подготовка отправки в Telegram…")
                parts = await split_for_telegram(result, cfg.max_tg_bytes, cancel_check=cancel_check)
                session = f"{result.meta['upload_id']}:{chat_id}:{cfg.max_tg_bytes}"
                completed = cache.get_parts(session) if isinstance(cache, CacheStore) else {}
                file_ids = []
                caption = video_caption(variant.quality)
                for i, part in enumerate(parts, 1):
                    if cancel_check and cancel_check():
                        raise DownloadCancelled()
                    if i in completed:
                        file_ids.append(completed[i])
                        continue
                    suffix = f" • часть {i}/{len(parts)}" if len(parts) > 1 else ""
                    await on_status(f"{tag}: отправка в Telegram{suffix}…")
                    upload = asyncio.create_task(send_video_file(bot, chat_id, part.path,
                        width=part.width, height=part.height, duration=part.duration, caption=caption + suffix,
                        local_file_root=(getattr(cfg, "tg_local_file_root", None)
                            if user_id is not None and user_id == cfg.admin_id
                            and getattr(cfg, "tg_api_base_url", "")
                            and getattr(cfg, "tg_api_local_mode", False) else None)))
                    try:
                        while not upload.done():
                            if cancel_check and cancel_check():
                                raise DownloadCancelled()
                            await asyncio.wait({upload}, timeout=0.5)
                        msg = upload.result()
                    finally:
                        if not upload.done():
                            upload.cancel()
                            await asyncio.gather(upload, return_exceptions=True)
                    file_id = extract_file_id(msg)
                    if file_id:
                        file_ids.append(file_id)
                        if isinstance(cache, CacheStore):
                            cache.put_part(session, i, file_id)
                res.telegram = "sent"
                if len(file_ids) == len(parts):
                    stored = file_ids[0] if len(parts) == 1 else "parts:" + json.dumps(file_ids)
                    try:
                        cache.put(key, stored, title=caption, quality=variant.quality, size_bytes=result.size_bytes)
                    except Exception as e:
                        notes.append("Видео отправлено, но кэш Telegram не сохранён")
                        log.warning("Telegram cache write failed (%s)", type(e).__name__)
            except Exception as e:
                res.telegram = "error"
                capture(e, "telegram_error", user_id, job_id=audit_job, title=title.title,
                        site=title.site, provider=provider, quality=quality, episode=episode.number)
                notes.append("Telegram: отправка отменена; уже принятые видео могут прийти позже" if isinstance(e, DownloadCancelled)
                             else "Telegram не подтвердил отправку; видео может прийти позже. Проверь чат перед повтором"
                             if isinstance(e, (TelegramNetworkError, TelegramServerError))
                             else f"Telegram: {e}" if isinstance(e, ValueError)
                             else f"Telegram: {type(e).__name__}: отправка не завершена")
        if recovery:
            try:
                if (wants_tg and res.telegram == "error") or (wants_library and res.library == "error"):
                    if recovered or await asyncio.to_thread(recovery.save, recovery_key, result, provider, variant.quality):
                        notes.append(f"Видео сохранено для повтора до {cfg.retry_cache_hours} ч.; кэш ограничен, старые файлы вытесняются")
                    else:
                        notes.append("Не хватило места в ограниченном кэше; при повторе видео будет скачано заново")
                else:
                    await asyncio.to_thread(recovery.discard, recovery_key)
            except Exception as exc:
                log.warning("Recovery cache unavailable (%s)", type(exc).__name__)
                notes.append("Сохранить видео для повтора не удалось")
        return finish()
