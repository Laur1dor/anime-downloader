"""Конфигурация из .env (без сторонних настроечных фреймворков)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _get_int(name: str, default: int) -> int:
    try:
        return int(_get(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_id: int
    access_mode: str              # "admin" (только админ) | "public" (все)
    max_concurrent_jobs: int      # глобальный лимит одновременных скачиваний
    tg_api_base_url: str          # "" -> облачный Bot API
    tg_api_local_mode: bool
    tmp_dir: Path
    cache_db_path: Path
    log_dir: Path
    log_level: str
    max_tg_mb: int
    rate_file: Path               # файл с лимитом Мбит (пишет админ, читает шейпер)
    reload_containers: list[str]  # какие docker-контейнеры рестартит кнопка
    library_dir: Path | None      # папка медиатеки на NAS (пусто = архивирование выкл)

    retry_cache_bytes: int = 4 * 1024 ** 3
    retry_cache_hours: int = 24
    audit_database_url: str = field(default="", repr=False)
    audit_retention_days: int = 30
    audit_max_events: int = 50000
    audit_pg_max_events: int = 100000
    audit_alerts: bool = True
    jellyfin_url: str = ""
    jellyfin_api_key: str = field(default="", repr=False)
    media_root: Path | None = None

    def category_dir(self, category: str) -> Path | None:
        from .models import MEDIA_CATEGORIES
        if category not in MEDIA_CATEGORIES:
            raise ValueError("Неизвестная категория медиатеки")
        if self.media_root:
            return self.media_root / MEDIA_CATEGORIES[category][1]
        return self.library_dir if category == "anime" else None

    @property
    def max_tg_bytes(self) -> int:
        return min(max(1, self.max_tg_mb), 2000 if self.tg_api_base_url else 49) * 1024 * 1024

    @property
    def is_public(self) -> bool:
        return self.access_mode == "public"


def load_config() -> Config:
    token = _get("BOT_TOKEN")
    if not token or "PUT_YOUR_TOKEN" in token:
        raise RuntimeError("BOT_TOKEN не задан в .env")
    admin = _get_int("ADMIN_ID", 0)
    if not admin:
        raise RuntimeError("ADMIN_ID не задан в .env")

    def _path(env_name: str, default: str) -> Path:
        raw = _get(env_name, default) or default
        p = Path(raw)
        return p if p.is_absolute() else (BASE_DIR / p)

    access_mode = (_get("ACCESS_MODE", "admin") or "admin").lower()
    if access_mode not in ("admin", "public"):
        access_mode = "admin"

    cfg = Config(
        bot_token=token,
        admin_id=admin,
        access_mode=access_mode,
        max_concurrent_jobs=max(1, _get_int("MAX_CONCURRENT_JOBS", 1)),
        tg_api_base_url=_get("TELEGRAM_API_BASE_URL"),
        tg_api_local_mode=_get("TELEGRAM_API_LOCAL_MODE", "0") in ("1", "true", "True"),
        tmp_dir=_path("TMP_DIR", "./tmp"),
        cache_db_path=_path("CACHE_DB_PATH", "./cache.sqlite3"),
        log_dir=_path("LOG_DIR", "./logs"),
        log_level=_get("LOG_LEVEL", "INFO") or "INFO",
        max_tg_mb=_get_int("MAX_TG_MB", 49),
        rate_file=_path("RATE_FILE", "./data/bandwidth_mbit.txt"),
        reload_containers=[c.strip() for c in _get("RELOAD_CONTAINERS", "").split(",") if c.strip()],
        library_dir=(_path("LIBRARY_DIR", "") if _get("LIBRARY_DIR") else None),
        retry_cache_bytes=max(0, _get_int("RETRY_CACHE_MB", 4096)) * 1024 ** 2,
        retry_cache_hours=max(1, _get_int("RETRY_CACHE_HOURS", 24)),
        audit_database_url=_get("AUDIT_DATABASE_URL"),
        audit_retention_days=max(1, _get_int("AUDIT_RETENTION_DAYS", 30)),
        audit_max_events=max(100, _get_int("AUDIT_MAX_EVENTS", 50000)),
        audit_pg_max_events=max(100, _get_int("AUDIT_PG_MAX_EVENTS", 100000)),
        audit_alerts=_get("AUDIT_ALERTS", "1").lower() in ("1", "true", "yes"),
        jellyfin_url=_get("JELLYFIN_URL"),
        jellyfin_api_key=_get("JELLYFIN_API_KEY"),
        media_root=(_path("MEDIA_ROOT", "") if _get("MEDIA_ROOT") else None),
    )
    cfg.tmp_dir.mkdir(parents=True, exist_ok=True)
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    return cfg
