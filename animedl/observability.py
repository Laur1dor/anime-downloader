"""Bounded local audit journal, optional PostgreSQL mirror and safe error groups.

The journal is intentionally independent of download/cache databases. PostgreSQL
outages never stop a Telegram handler; records are retried with stable UUIDs.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import re
import sqlite3
import time
import traceback
import uuid
import queue
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery
from .error_messages import error_reason, format_error_alert
from .incidents import Incidents

log = logging.getLogger(__name__)
_current = None
_user = ContextVar("audit_user", default=None)
_operation = ContextVar("audit_operation", default=None)
_monitor_io = ContextVar("audit_monitor_io", default=False)
_captured = ContextVar("audit_captured_exception", default=None)
_FIELDS = {"site", "title", "provider", "voice", "quality", "delivery", "category",
           "episode", "season", "bytes", "telegram", "library", "state", "choice",
           "duration_ms", "error_class", "location", "fingerprint", "job_id", "source_failed", "http_status",
           "cause_class", "cause_location", "stage", "reason", "operation", "operation_key",
           "attempt_id", "attempt_started", "proof", "scope", "severity", "component",
           "incident_id", "incident_state", "dropped_warnings"}
_STAGES = {'navigation': 'открытие плеера', 'manifest': 'ожидание HLS',
           'selection': 'проверка серии и озвучки', 'quality_menu': 'меню качества',
           'quality_switch': 'переключение качества', 'media_download': 'загрузка видео',
           'budget': 'общий лимит времени'}


def error_context(cause_class=None, stage=None):
    parts = []
    if stage in _STAGES:
        parts.append('Этап: ' + _STAGES[stage])
    if cause_class:
        parts.append('Причина: ' + html.escape(cause_class))
    return ' · '.join(parts)
_SQL = """CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY, ts DOUBLE PRECISION NOT NULL, user_id BIGINT,
    action TEXT NOT NULL, status TEXT NOT NULL, details JSONB NOT NULL);
    CREATE INDEX IF NOT EXISTS audit_events_ts ON audit_events(ts);
    CREATE INDEX IF NOT EXISTS audit_events_action_ts ON audit_events(action, ts);
    CREATE INDEX IF NOT EXISTS audit_events_user_ts ON audit_events(user_id, ts);"""


def safe_details(values):
    """Allowlisted structured metadata only; never URLs, headers or exception text."""
    result = {}
    for key, value in values.items():
        if key not in _FIELDS or value is None:
            continue
        if isinstance(value, (bool, int, float)):
            result[key] = value
        elif isinstance(value, str):
            # A title/voice must never accidentally become a signed media URL.
            value = re.sub(r"https?://\S+", "[url]", value)
            value = re.sub(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b", "[token]", value)
            result[key] = " ".join(value.split())[:200]
    return result


def error_details(exc):
    frames = traceback.extract_tb(exc.__traceback__)
    frame = frames[-1] if frames else None
    location = f"{Path(frame.filename).name}:{frame.name}:{frame.lineno}" if frame else "unknown"
    cls = type(exc).__name__
    status = None
    cause_class = cause_location = stage = reason = None
    current, seen = exc, set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        reason = reason or error_reason(current)
        if stage is None and getattr(current, 'probe_stage', None) in _STAGES:
            stage = current.probe_stage
        if current is not exc and not isinstance(current, asyncio.CancelledError):
            cause_class = type(current).__name__
            trace = traceback.extract_tb(current.__traceback__)
            last = trace[-1] if trace else None
            cause_location = f'{Path(last.filename).name}:{last.name}:{last.lineno}' if last else 'unknown'
        response = getattr(current, "response", None)
        candidate = getattr(response, "status_code", None)
        if status is None and isinstance(candidate, int) and 100 <= candidate <= 599:
            status = candidate
        match = re.search(r"HTTP(?:\s+error)?\s*[: ]\s*([45]\d\d)\b", str(current)[:2000], re.I)
        if match and status is None:
            status = int(match[1])
        current = current.__cause__ or current.__context__
    signature = f"{cls}:{status}:{location}"
    if cause_class or stage:
        signature += f':{cause_class}:{stage}'
    if reason:
        signature += ':' + reason
    return {"error_class": cls, "location": location, "cause_class": cause_class,
            "cause_location": cause_location, "stage": stage,
            "http_status": status, "reason": reason,
            "fingerprint": hashlib.sha256(signature.encode()).hexdigest()[:16]}


def emit(action, user_id=None, status="ok", **details):
    if _current is not None and not _monitor_io.get():
        _current.record(action, user_id if user_id is not None else _user.get(), status,
                        **((_operation.get() or {}) | details))


def capture(exc, action="error", user_id=None, **details):
    from aiogram.exceptions import TelegramForbiddenError
    if isinstance(exc, TelegramForbiddenError) and type(getattr(exc.method, 'chat_id', None)) is int:
        user_id = exc.method.chat_id
        details |= operation('telegram_access', user_id)
    _captured.set((id(exc), time.monotonic()))
    emit(action, user_id, "error", **(details | error_details(exc)))


def operation(name, *identity, **details):
    raw = json.dumps([name, *identity], ensure_ascii=False, default=str, separators=(',', ':'))
    return details | dict(operation=name, operation_key=hashlib.sha256(raw.encode()).hexdigest(),
                          attempt_id=uuid.uuid4().hex, attempt_started=time.time())


@contextmanager
def bind_operation(details):
    token = _operation.set(details)
    try:
        yield
    finally:
        _operation.reset(token)


def confirmed(details, proof, user_id=None):
    emit('operation_succeeded', user_id, proof=proof, **details)


def needs_confirmation(details, user_id=None):
    if _current is None:
        return False
    try:
        return _current.incidents.is_open(user_id if user_id is not None else _user.get(), details)
    except sqlite3.Error:
        return False


def warning(action, user_id=None, **details):
    details.setdefault('error_class', 'Warning')
    details.setdefault('fingerprint', hashlib.sha256(action.encode()).hexdigest()[:16])
    emit(action, user_id, 'warning', **details)


class WarningCollector(logging.Handler):
    """Never format log text/args; worker threads enqueue only safe metadata."""
    def __init__(self, store):
        super().__init__(logging.WARNING)
        self.store = store

    def emit(self, record):
        if _monitor_io.get() or getattr(record, 'audit_recorded', False) or record.name.startswith(('animedl.observability', 'animedl.incidents')):
            return  # Monitoring its own delivery failures would recurse forever.
        captured = _captured.get()
        if record.exc_info and captured and captured[0] == id(record.exc_info[1]) and time.monotonic() - captured[1] < 5:
            return
        location = f'{Path(record.pathname).name}:{record.funcName}:{record.lineno}'
        cls = type(record.exc_info[1]).__name__ if record.exc_info else 'LogWarning'
        signature = f'{record.name}:{Path(record.pathname).name}:{record.funcName}:{record.levelno}:{cls}'
        details = (_operation.get() or {}) | dict(error_class=cls, location=location,
            component=record.name, reason='logged_error' if record.levelno >= logging.ERROR else 'logged_warning',
            fingerprint=hashlib.sha256(signature.encode()).hexdigest()[:16])
        if record.name.startswith('aiogram') and record.funcName == '_listen_updates':
            details |= operation('telegram_polling', scope='service')
        try:
            self.store.log_queue.put_nowait((record.created, _user.get(),
                'error' if record.levelno >= logging.ERROR else 'warning', safe_details(details)))
        except queue.Full:
            with self.store.log_overflow_lock:
                self.store.log_overflow += 1


class AuditStore:
    def __init__(self, path: Path, *, dsn="", retention_days=30, max_events=50000, pg_max_events=100000):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=0.05)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA journal_size_limit=4194304")
        self.db.executescript("""CREATE TABLE IF NOT EXISTS audit_events (
            id TEXT PRIMARY KEY, ts REAL NOT NULL, user_id INTEGER, action TEXT NOT NULL,
            status TEXT NOT NULL, details TEXT NOT NULL, mirrored INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS audit_ts ON audit_events(ts);
            CREATE INDEX IF NOT EXISTS audit_pending ON audit_events(mirrored,ts);
            CREATE TABLE IF NOT EXISTS audit_meta (key TEXT PRIMARY KEY, value REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS audit_alerts (fingerprint TEXT PRIMARY KEY, ts REAL NOT NULL);""")
        self.dsn = dsn
        self.retention_days = max(1, retention_days)
        self.max_events = max(100, max_events)
        self.pg_max_events = max(100, pg_max_events)
        self.pool = None
        self.task = None
        self.notifier = None
        self._schema_ready = False
        self._count = self.db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
        self._last_prune = 0
        self._last_pg_prune = 0
        self._last_alert_scan = time.time()
        self._db_failed = False
        self._local_failed = False
        self.incidents = Incidents(self.db)
        from .support import SupportStore
        self.support = SupportStore(self.db)
        self.log_queue = queue.Queue(maxsize=256)
        self.log_overflow = 0
        self.log_overflow_lock = threading.Lock()
        self.log_handler = None
        self.updater = None
        self._draining_logs = False
        self._next_flush = 0

    def record(self, action, user_id=None, status="ok", *, _event_time=None, **details):
        try:
            if action == 'operation_succeeded' and not self._draining_logs:
                self._drain_logs()
            now = time.time() if _event_time is None else _event_time
            details = safe_details(details)
            if status in ('error', 'warning') and action not in ('episode_finished', 'action_finished'):
                details.setdefault('fingerprint', hashlib.sha256(action.encode()).hexdigest()[:16])
                details['severity'] = status
                identity, state = self.incidents.failure(user_id, action, details, now)
                details |= dict(incident_id=identity, incident_state=state)
            elif action == 'operation_succeeded':
                identity = self.incidents.success(user_id, details, now)
                if identity:
                    details |= dict(incident_id=identity, incident_state='resolved')
            self.db.execute("INSERT INTO audit_events VALUES (?,?,?,?,?,?,0)",
                (uuid.uuid4().hex, now, user_id, action[:60], status[:30],
                 json.dumps(details, ensure_ascii=False)))
            self.db.commit()
            self._count += 1
            # Hard event limit even if PostgreSQL is unavailable for months.
            if self._count > self.max_events or time.time() - self._last_prune > 60:
                self.prune()
            self._local_failed = False
            return True
        except (sqlite3.Error, OSError):
            try:
                self.db.rollback()
            except sqlite3.Error:
                pass
            if not self._local_failed:
                log.error("Audit journal unavailable; download processing continues")
                self._local_failed = True
            return False

    def _drain_logs(self):
        self._draining_logs = True
        lost = 0
        try:
            for _ in range(256):
                try:
                    ts, uid, status, details = self.log_queue.get_nowait()
                except queue.Empty:
                    break
                if not self.record('logging_warning', uid, status, _event_time=ts, **details):
                    lost += 1
            with self.log_overflow_lock:
                lost += self.log_overflow
                self.log_overflow = 0
            if lost:
                self.db.execute("INSERT INTO audit_meta VALUES('warnings_dropped',?) ON CONFLICT(key) "
                                'DO UPDATE SET value=value+excluded.value', (lost,))
                self.db.commit()
                total = self.db.execute("SELECT value FROM audit_meta WHERE key='warnings_dropped'").fetchone()[0]
                details = operation('warning_collection', scope='service', reason='warning_overflow',
                                    dropped_warnings=int(total), error_class='Warning')
                details['fingerprint'] = hashlib.sha256(b'warning_collection_overflow').hexdigest()[:16]
                self.record('warning_collection_overflow', None, 'warning', **details)
        except (sqlite3.Error, OSError):
            with self.log_overflow_lock:
                self.log_overflow += lost
            log.error('Warning journal unavailable; bot continues')
        finally:
            self._draining_logs = False

    def prune(self):
        cutoff = time.time() - self.retention_days * 86400
        pending = self.db.execute("SELECT COUNT(*) FROM audit_events WHERE mirrored=0").fetchone()[0]
        self.db.execute("DELETE FROM audit_events WHERE ts < ?", (cutoff,))
        self.db.execute("DELETE FROM audit_events WHERE id IN (SELECT id FROM audit_events ORDER BY ts DESC LIMIT -1 OFFSET ?)",
                        (self.max_events,))
        remaining = self.db.execute("SELECT COUNT(*) FROM audit_events WHERE mirrored=0").fetchone()[0]
        dropped = max(0, pending - remaining)
        if dropped and self.dsn:
            self.db.execute("INSERT INTO audit_meta VALUES ('dropped',?) ON CONFLICT(key) DO UPDATE SET value=value+excluded.value", (dropped,))
        self.db.execute("DELETE FROM audit_alerts WHERE ts < ?", (cutoff,))
        self.incidents.prune(cutoff)
        self.db.commit()
        self._count = self.db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
        self._last_prune = time.time()

    async def start(self, notifier=None, *, owner_id=None, updater=None, user_notifier=None, contact_resolver=None, support_sender=None, support_updater=None):
        global _current
        _current = self
        self.notifier = notifier
        self.owner_id = owner_id
        self.updater = updater
        self.support_sender=support_sender
        self.support_updater=support_updater
        self.contact_resolver = contact_resolver
        self.user_notifier = user_notifier
        self.log_handler = WarningCollector(self)
        for name in ('animedl', 'aiogram'):
            logging.getLogger(name).addHandler(self.log_handler)
        self.task = asyncio.create_task(self._worker(), name="audit-mirror")

    async def close(self):
        global _current
        if _current is self:
            _current = None
        if self.log_handler:
            for name in ('animedl', 'aiogram'):
                logging.getLogger(name).removeHandler(self.log_handler)
            self.log_handler.close()
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.pool:
            await asyncio.wait_for(self.pool.close(), timeout=5)
        self._drain_logs()
        self.db.close()

    async def flush(self):
        if not self.dsn:
            return
        if self.pool is None:
            import asyncpg
            self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=2,
                                                 timeout=3, command_timeout=5)
        if not self._schema_ready:
            async with self.pool.acquire() as connection:
                await connection.execute(_SQL)
            self._schema_ready = True
        rows = self.db.execute("SELECT id,ts,user_id,action,status,details FROM audit_events WHERE mirrored=0 ORDER BY ts LIMIT 250").fetchall()
        async with self.pool.acquire() as connection:
            if rows:
                await connection.executemany("INSERT INTO audit_events VALUES ($1,$2,$3,$4,$5,$6::jsonb) ON CONFLICT(id) DO NOTHING", rows)
                self.db.executemany("UPDATE audit_events SET mirrored=1 WHERE id=?", [(r[0],) for r in rows])
                self.db.commit()
            if time.time() - self._last_pg_prune > 60:
                await connection.execute("DELETE FROM audit_events WHERE ts < $1", time.time() - self.retention_days * 86400)
                await connection.execute("DELETE FROM audit_events WHERE id IN (SELECT id FROM audit_events ORDER BY ts DESC OFFSET $1)", self.pg_max_events)
                self._last_pg_prune = time.time()

    async def _alerts(self):
        self._drain_logs()
        token = _monitor_io.set(True)
        try:
            resolver = getattr(self, 'contact_resolver', None)
            if resolver and time.time() >= getattr(self, '_next_contact_query', 0):
                missing = self.db.execute('SELECT DISTINCT user_id FROM audit_incidents WHERE user_id IS NOT NULL '
                    'AND user_id NOT IN (SELECT user_id FROM audit_contacts) LIMIT 1').fetchone()
                if missing:
                    self._next_contact_query = time.time() + 30
                    uid = missing[0]
                    self.incidents.profile(uid)
                    try:
                        chat = await asyncio.wait_for(resolver(uid), timeout=5)
                        self.incidents.profile(uid, getattr(chat, 'username', None))
                    except Exception:
                        pass  # No retries or monitoring recursion for unavailable profiles.
            await self.incidents.notify(self.notifier, self.updater,
                                        owner=getattr(self, 'owner_id', None))
            await self.support.tick(getattr(self,'support_sender',None),getattr(self,'support_updater',None))
            await self.incidents.notify_users(getattr(self, 'user_notifier', None), owner=getattr(self, 'owner_id', None))
        finally:
            _monitor_io.reset(token)

    async def _tick(self):
        self.prune()
        if time.time()-getattr(self,"_last_support_prune",0)>60:
            self.support.prune()
            self._last_support_prune=time.time()
        await self._alerts()
        if self.dsn and time.time() >= self._next_flush:
            proof = operation('postgres_mirror', scope='service')
            try:
                await self.flush()
            except Exception as exc:
                self.record('audit_mirror_error', None, 'error', **(proof | error_details(exc)))
                if not self._db_failed:
                    log.warning('Audit mirror unavailable (%s); events stay in bounded journal', type(exc).__name__)
                self._db_failed = True
                self._next_flush = time.time() + 30
            else:
                if self.incidents.is_open(None, proof):
                    self.record('operation_succeeded', None, **proof, proof='database_roundtrip')
                self._db_failed = False

    async def _worker(self):
        while True:
            try:
                await self._tick()
            except Exception as exc:
                log.error('Monitoring iteration failed (%s); bot continues', type(exc).__name__)
            await asyncio.sleep(5)

    def stats(self):
        since = time.time() - self.retention_days * 86400
        total, users, errors, warnings = self.db.execute("SELECT COUNT(*),COUNT(DISTINCT user_id),SUM(status='error'),SUM(status='warning') FROM audit_events WHERE ts>=?", (since,)).fetchone()
        actions = self.db.execute("SELECT action,COUNT(*) FROM audit_events WHERE ts>=? GROUP BY action ORDER BY COUNT(*) DESC LIMIT 12", (since,)).fetchall()
        jobs = self.db.execute("SELECT status,COUNT(*),COALESCE(SUM(CAST(json_extract(details,'$.bytes') AS INTEGER)),0) FROM audit_events WHERE action='episode_finished' AND ts>=? GROUP BY status", (since,)).fetchall()
        titles = self.db.execute("SELECT json_extract(details,'$.title'),COUNT(*) FROM audit_events WHERE action='episode_started' AND ts>=? GROUP BY json_extract(details,'$.title') ORDER BY COUNT(*) DESC LIMIT 8", (since,)).fetchall()
        pending = self.db.execute("SELECT COUNT(*) FROM audit_events WHERE mirrored=0").fetchone()[0]
        dropped = self.db.execute("SELECT value FROM audit_meta WHERE key='dropped'").fetchone()
        providers = self.db.execute("SELECT COALESCE(json_extract(details,'$.provider'),'—'),json_extract(details,'$.quality'),status,COUNT(*) FROM audit_events WHERE action='episode_finished' AND ts>=? GROUP BY 1,2,3 ORDER BY COUNT(*) DESC LIMIT 12", (since,)).fetchall()
        top_users = self.db.execute("SELECT user_id,COUNT(*),SUM(status IN ('error','partial')) FROM audit_events WHERE action='episode_finished' AND ts>=? GROUP BY user_id ORDER BY COUNT(*) DESC LIMIT 8", (since,)).fetchall()
        failures = self.db.execute("SELECT json_extract(details,'$.error_class'),json_extract(details,'$.http_status'),COUNT(*) FROM audit_events WHERE status='error' AND ts>=? AND json_extract(details,'$.error_class') IS NOT NULL GROUP BY 1,2 ORDER BY COUNT(*) DESC LIMIT 8", (since,)).fetchall()
        return dict(total=total, users=users, errors=errors or 0, warnings=warnings or 0, actions=actions, jobs=jobs,
                    titles=titles, pending=pending, dropped=int(dropped[0]) if dropped else 0,
                    providers=providers, top_users=top_users, failures=failures,
                    source=f"SQLite, до {self.max_events} последних событий",
                    mirror="недоступен" if self._db_failed else "PostgreSQL" if self.dsn else "локальный журнал")

    def errors(self):
        rows = self.db.execute("""SELECT json_extract(details,'$.fingerprint'),
            json_extract(details,'$.error_class'),json_extract(details,'$.location'),
            COUNT(*),MAX(ts),json_extract(details,'$.http_status'),
            json_extract(details,'$.cause_class'),json_extract(details,'$.stage'),
            json_extract(details,'$.reason'),action FROM audit_events WHERE status IN ('error','warning')
            AND json_extract(details,'$.fingerprint') IS NOT NULL
            GROUP BY json_extract(details,'$.fingerprint'),action ORDER BY MAX(ts) DESC LIMIT 12""").fetchall()
        return rows

    async def report(self):
        """Use complete retained PostgreSQL data; explicitly label offline fallback."""
        local = self.stats()
        if not self.pool or self._db_failed:
            return local
        since = time.time() - self.retention_days * 86400
        try:
            async with self.pool.acquire() as conn:
                totals = await conn.fetchrow("SELECT COUNT(*),COUNT(DISTINCT user_id),COUNT(*) FILTER(WHERE status='error'),COUNT(*) FILTER(WHERE status='warning') FROM audit_events WHERE ts>=$1", since)
                actions = await conn.fetch("SELECT action,COUNT(*) FROM audit_events WHERE ts>=$1 GROUP BY action ORDER BY COUNT(*) DESC LIMIT 12", since)
                jobs = await conn.fetch("SELECT status,COUNT(*),COALESCE(SUM(COALESCE((details->>'bytes')::bigint,0)),0) FROM audit_events WHERE action='episode_finished' AND ts>=$1 GROUP BY status", since)
                titles = await conn.fetch("SELECT details->>'title',COUNT(*) FROM audit_events WHERE action='episode_started' AND ts>=$1 GROUP BY 1 ORDER BY COUNT(*) DESC LIMIT 8", since)
                providers = await conn.fetch("SELECT COALESCE(details->>'provider','—'),details->>'quality',status,COUNT(*) FROM audit_events WHERE action='episode_finished' AND ts>=$1 GROUP BY 1,2,3 ORDER BY COUNT(*) DESC LIMIT 12", since)
                users = await conn.fetch("SELECT user_id,COUNT(*),COUNT(*) FILTER(WHERE status IN ('error','partial')) FROM audit_events WHERE action='episode_finished' AND ts>=$1 GROUP BY user_id ORDER BY COUNT(*) DESC LIMIT 8", since)
                failures = await conn.fetch("SELECT details->>'error_class',details->>'http_status',COUNT(*) FROM audit_events WHERE status='error' AND ts>=$1 AND details->>'error_class' IS NOT NULL GROUP BY 1,2 ORDER BY COUNT(*) DESC LIMIT 8", since)
            local.update(total=totals[0], users=totals[1], errors=totals[2], warnings=totals[3], actions=actions,
                         jobs=jobs, titles=titles, providers=providers, top_users=users, failures=failures,
                         source=f"PostgreSQL, до {self.pg_max_events} последних событий")
            return local
        except Exception as exc:
            log.warning("Audit report uses local fallback (%s)", type(exc).__name__)
            return local

    async def error_report(self):
        if self.pool and not self._db_failed:
            try:
                async with self.pool.acquire() as conn:
                    rows = await conn.fetch("""SELECT details->>'fingerprint',details->>'error_class',
                        details->>'location',COUNT(*),MAX(ts),details->>'http_status',
                        details->>'cause_class',details->>'stage',details->>'reason',action
                        FROM audit_events WHERE status IN ('error','warning') AND details->>'fingerprint' IS NOT NULL
                        GROUP BY 1,2,3,6,7,8,9,10 ORDER BY MAX(ts) DESC LIMIT 12""")
                return rows, f"PostgreSQL, до {self.pg_max_events} событий / {self.retention_days} дн."
            except Exception as exc:
                log.warning("Audit errors use local fallback (%s)", type(exc).__name__)
        return self.errors(), f"SQLite, до {self.max_events} событий / {self.retention_days} дн."


class AuditMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        uid = user.id if user else None
        if user and _current:
            try:
                _current.incidents.profile(uid, getattr(user, 'username', None))
            except Exception:
                log.warning("Audit contact unavailable")
        token = _user.set(uid)
        state = data.get("state")
        state_name = await state.get_state() if state else ""
        details = {"state": state_name or "idle"}
        text = getattr(event, "text", "") or ""
        if isinstance(event, CallbackQuery):
            raw = event.data or ""
            prefix, _, choice = raw.partition(":")
            action = "click:" + re.sub(r"[^a-zA-Z0-9_]", "", prefix)[:40]
            # Numeric positions and fixed short choices only; no arbitrary callback data.
            if re.fullmatch(r"\d{1,6}|telegram|library|both|anime|series|movies|anime_movies", choice):
                details["choice"] = choice
        elif text.startswith("/"):
            command = text.split()[0].split("@")[0]
            action = "command:" + re.sub(r"[^a-zA-Z0-9_]", "", command)[:40]
        else:
            action = "link" if text.startswith(("http://", "https://")) else "input"
        emit(action, uid, **details)
        started = time.monotonic()
        try:
            return await handler(event, data)
        except Exception as exc:
            try:
                if state:
                    selected = await state.get_data()
                    for key in ("site", "title", "voice", "provider", "quality", "delivery"):
                        if key in selected:
                            details[key] = selected[key]
            except Exception:
                log.warning("Audit error metadata unavailable")
            capture(exc, "handler_error", uid, **details)
            raise
        finally:
            try:
                if state:
                    selected = await state.get_data()
                    for key in ("site", "title", "voice", "provider", "quality", "delivery", "library_category"):
                        if key in selected:
                            details["category" if key == "library_category" else key] = selected[key]
                emit("action_finished", uid, duration_ms=int((time.monotonic() - started) * 1000), **details)
            except Exception:
                log.warning("Audit action metadata unavailable")
            finally:
                _user.reset(token)
