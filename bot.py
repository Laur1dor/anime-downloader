"""Точка входа: инициализация и запуск Telegram-бота (long polling)."""
from __future__ import annotations

import asyncio
import logging
import shutil

from aiogram import Dispatcher

from animedl.cache.store import CacheStore
from animedl.catalog import CatalogStore
from animedl.config import load_config
from animedl.handlers import admin, common, flow, search
from animedl.handlers.access import AccessMiddleware
from animedl.logging_setup import setup_logging
from animedl.observability import AuditMiddleware, AuditStore
from animedl.runtime_health import RuntimeHealth
from animedl.telegram_client import build_bot

log = logging.getLogger("animedl")


def _clean_tmp(tmp_dir) -> None:
    """Удаляем незавершённые загрузки прошлой сессии."""
    for p in tmp_dir.glob("*"):
        try:
            if p.is_file():
                p.unlink()
            elif p.name.startswith("episode_") and p.is_dir() and not p.is_symlink():
                shutil.rmtree(p)
        except OSError:
            pass


async def main() -> None:
    cfg = load_config()
    setup_logging(cfg.log_dir, cfg.log_level)
    _clean_tmp(cfg.tmp_dir)
    from animedl.recovery import RecoveryStore
    await asyncio.to_thread(RecoveryStore(cfg.cache_db_path.parent / "recovery",
        cfg.retry_cache_bytes, cfg.retry_cache_hours * 3600).prune)
    from animedl.admin_ops import library_ready
    if library_ready(cfg.media_root):
        work = cfg.media_root / ".animedl-work"
        if work.is_dir():
            await asyncio.to_thread(_clean_tmp, work)
            await asyncio.to_thread(RecoveryStore(work / "recovery", cfg.retry_cache_bytes,
                cfg.retry_cache_hours * 3600).prune)

    if cfg.max_tg_mb <= 50 and not cfg.tg_api_base_url:
        log.warning("Лимит отправки ~%d МБ (облачный Bot API). Для файлов до 2 ГБ "
                    "подними локальный Bot API сервер (см. README).", cfg.max_tg_mb)

    from animedl.jobcontrol import configure_concurrency
    configure_concurrency(cfg.max_concurrent_jobs)
    log.info("Режим доступа: %s | одновременных задач: %d",
             cfg.access_mode, cfg.max_concurrent_jobs)

    cache = CacheStore(cfg.cache_db_path)
    catalog = CatalogStore(cfg.cache_db_path.with_name("catalog.sqlite3"))
    bot = build_bot(cfg)
    audit = None
    try:
        audit = AuditStore(cfg.cache_db_path.with_name("audit.sqlite3"),
            dsn=cfg.audit_database_url, retention_days=cfg.audit_retention_days,
            max_events=cfg.audit_max_events, pg_max_events=cfg.audit_pg_max_events)
        await audit.start((lambda text: bot.send_message(cfg.admin_id, text)) if cfg.audit_alerts else None)
    except Exception as exc:
        log.error("Audit unavailable (%s); bot starts without statistics", type(exc).__name__)

    dp = Dispatcher()
    dp["cfg"] = cfg
    dp["cache"] = cache
    dp["catalog"] = catalog
    dp["audit"] = audit

    health = RuntimeHealth(cfg.cache_db_path.parent)
    dp.startup.register(health.start)
    dp.message.outer_middleware(health)
    dp.callback_query.outer_middleware(health)

    # Outer middleware also sees access-denied and unmatched actions.
    dp.message.outer_middleware(AuditMiddleware())
    dp.callback_query.outer_middleware(AuditMiddleware())

    guard = AccessMiddleware(cfg.admin_id, public=cfg.is_public)
    dp.message.middleware(guard)
    dp.callback_query.middleware(guard)

    dp.include_router(common.router)
    dp.include_router(admin.router)
    dp.include_router(search.router)
    dp.include_router(flow.router)

    me = await bot.get_me()
    log.info("Бот @%s запущен. Админ: %s", me.username, cfg.admin_id)
    try:
        await dp.start_polling(bot)
    finally:
        await health.close()
        if audit:
            await audit.close()
        cache.close()
        catalog.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
