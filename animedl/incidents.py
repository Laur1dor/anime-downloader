"""Bounded incident state and notification delivery, sharing the audit database."""
from __future__ import annotations

import hashlib
import asyncio
import html
import json
import time
import uuid
from datetime import datetime, timezone

from .error_messages import error_explanation, format_error_alert, event_context

PROOFS = {'download_validated', 'all_parts_sent', 'file_published', 'player_verified',
          'http_accepted', 'asset_verified', 'database_roundtrip', 'cache_verified'}
EXPECTED_PROOFS = {
    'source_download': 'download_validated', 'telegram_delivery': 'all_parts_sent',
    'library_publish': 'file_published', 'quality_probe': 'player_verified',
    'episode_thumbnail': 'asset_verified', 'poster_fetch': 'asset_verified',
    'jellyfin_refresh': 'http_accepted', 'postgres_mirror': 'database_roundtrip',
    'telegram_access': 'http_accepted', 'telegram_polling': 'http_accepted',
    'voice_enumeration': 'player_verified', 'search_provider': 'http_accepted',
    'title_parse': 'player_verified', 'quality_menu': 'player_verified',
    'search_request': 'http_accepted', 'telegram_cache_write': 'cache_verified',
    'recovery_cache_read': 'cache_verified', 'recovery_cache_write': 'cache_verified',
    'recovery_cache_discard': 'cache_verified',
}
PROOF_LABELS = {
    'download_validated': 'Видео заново скачано и прошло проверку.',
    'all_parts_sent': 'Все части этой серии приняты Telegram.',
    'file_published': 'Файл этой серии доступен в медиатеке.',
    'player_verified': 'Этот плеер подтвердил доступные качества.',
    'http_accepted': 'Сервис подтвердил успешный запрос.',
    'asset_verified': 'Нужное изображение получено и проверено.',
    'database_roundtrip': 'Запись в PostgreSQL снова подтверждена.',
    'cache_verified': 'Операция с кэшем выполнена успешно.',
}


class Incidents:
    API_INTERVAL = 30
    UPDATE_INTERVAL = 300
    MAX_FAILURES = 3
    FAILURE_COOLDOWN = 300

    def __init__(self, db, *, limit=500):
        self.db, self.limit = db, max(10, limit)
        db.executescript('''CREATE TABLE IF NOT EXISTS audit_incidents (
            id TEXT PRIMARY KEY, user_id INTEGER, operation TEXT NOT NULL, opkey TEXT NOT NULL,
            state TEXT NOT NULL, occurrences INTEGER NOT NULL, reopens INTEGER NOT NULL,
            first_seen REAL NOT NULL, last_seen REAL NOT NULL, closed_at REAL,
            last_attempt TEXT, revision INTEGER NOT NULL, notified INTEGER NOT NULL,
            not_before REAL NOT NULL, message_id INTEGER, digest INTEGER NOT NULL,
            details TEXT NOT NULL, action TEXT NOT NULL, generation TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS incident_pending ON audit_incidents(not_before);
            CREATE TABLE IF NOT EXISTS audit_notification_state (
            id INTEGER PRIMARY KEY CHECK(id=1), next_api REAL NOT NULL,
            failures INTEGER NOT NULL, digest_id INTEGER, evicted INTEGER NOT NULL);
            INSERT OR IGNORE INTO audit_notification_state VALUES(1,0,0,NULL,0);''')
        db.executescript("""CREATE TABLE IF NOT EXISTS audit_contacts (
            user_id INTEGER PRIMARY KEY,username TEXT,blocked INTEGER NOT NULL,next_notice REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS audit_recovery_notices (
            id TEXT PRIMARY KEY,generation TEXT NOT NULL,done INTEGER NOT NULL,due REAL NOT NULL);""")
        if 'generation' not in {r[1] for r in db.execute('PRAGMA table_info(audit_incidents)')}:
            db.execute("ALTER TABLE audit_incidents ADD COLUMN generation TEXT NOT NULL DEFAULT ''")
            db.execute("UPDATE audit_incidents SET generation=hex(randomblob(16)) WHERE generation=''")
            db.commit()

    def profile(self, uid, username=None):
        import re
        username = username if username and re.fullmatch(r'[A-Za-z0-9_]{5,32}', username) else None
        self.db.execute('INSERT INTO audit_contacts(user_id,username,blocked,next_notice) VALUES(?,?,0,0) '
                        'ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,blocked=0', (uid, username))
        self.db.commit()

    def contact(self, uid):
        row = self.db.execute('SELECT username FROM audit_contacts WHERE user_id=?', (uid,)).fetchone()
        if row and row[0]:
            return f'<a href="https://t.me/{row[0]}">{uid}</a>'
        return f'<a href="tg://user?id={uid}">{uid}</a> (нет username; ссылка по ID может не открыться)'

    async def notify_users(self, sender, *, owner=None, now=None):
        if not sender:
            return
        now = time.time() if now is None else now
        if now < self.db.execute('SELECT next_api FROM audit_notification_state WHERE id=1').fetchone()[0]:
            return
        # No retroactive campaign: only newly observed delivery failures enter here.
        row = self.db.execute("SELECT i.id,i.generation,i.closed_at,i.user_id FROM audit_incidents i "
            "JOIN audit_recovery_notices n ON n.id=i.id AND n.generation=i.generation "
            "WHERE i.state='resolved' AND n.done=0 AND n.due<=? AND i.user_id IS NOT NULL "
            "AND i.user_id!=? AND i.user_id NOT IN (SELECT user_id FROM audit_contacts WHERE next_notice>?) "
            "ORDER BY i.closed_at LIMIT 1", (now, owner or 0, now)).fetchone()
        if not row:
            return
        identity, generation, closed, uid = row
        self.db.execute('INSERT OR IGNORE INTO audit_contacts VALUES(?,NULL,0,0)', (uid,))
        blocked, next_notice = self.db.execute('SELECT blocked,next_notice FROM audit_contacts WHERE user_id=?', (uid,)).fetchone()
        if blocked:
            self.db.execute('UPDATE audit_recovery_notices SET done=1 WHERE id=? AND generation=?', (identity,generation))
            self.db.commit()
            return
        if now < next_notice:
            return
        # Reserve before I/O: uncertain sends are not repeated after restart.
        self.db.execute('UPDATE audit_recovery_notices SET done=1 WHERE id=? AND generation=?', (identity,generation))
        self.db.execute('UPDATE audit_contacts SET next_notice=? WHERE user_id=?', (now+86400,uid))
        self.db.execute('UPDATE audit_notification_state SET next_api=? WHERE id=1', (now+self.API_INTERVAL,))
        self.db.commit()
        text = ('Извини, при загрузке произошла ошибка. Работа этой операции снова подтверждена. '
                'Если нужные серии не пришли, можешь повторить загрузку — отправь название или ссылку. '
                'Автоматически заново ничего не скачиваем.')
        try:
            await asyncio.wait_for(sender(uid, text), timeout=5)
        except Exception as exc:
            from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
            if isinstance(exc, TelegramForbiddenError):
                self.db.execute('UPDATE audit_contacts SET blocked=1 WHERE user_id=?', (uid,))
            elif isinstance(exc, TelegramRetryAfter):
                self.db.execute('UPDATE audit_recovery_notices SET done=0,due=? WHERE id=? AND generation=?',
                                (now+max(30,exc.retry_after+1),identity,generation))
                self.db.execute('UPDATE audit_contacts SET next_notice=? WHERE user_id=?', (now+max(30,exc.retry_after+1),uid))
            self.db.commit()

    @staticmethod
    def identity(uid, details):
        if details.get('scope') == 'service':
            uid = None
        operation = details.get('operation') or 'unverified'
        key = details.get('operation_key') or details['fingerprint']
        value = json.dumps([uid, operation, key], separators=(',', ':'))
        return hashlib.sha256(value.encode()).hexdigest()[:16], uid, operation, key

    def failure(self, uid, action, details, now):
        identity, uid, operation, key = self.identity(uid, details)
        row = self.db.execute('SELECT state,details,last_seen,closed_at FROM audit_incidents WHERE id=?', (identity,)).fetchone()
        if row and (now < row[2] or row[0] == 'resolved' and now <= row[3]):
            # A worker-thread log can reach the journal after a newer failure/success.
            # Count it, but never replace newer evidence or reopen a resolved incident.
            self.db.execute('UPDATE audit_incidents SET occurrences=occurrences+1,revision=revision+1,'
                            "notified=CASE WHEN state='resolved' AND message_id IS NULL AND digest=0 "
                            'THEN revision+1 ELSE notified END WHERE id=?', (identity,))
            return identity, row[0]
        reopening = bool(row and row[0] == 'resolved')
        stored = details
        if row and not reopening:
            previous = json.loads(row[1])
            if previous.get('severity') == 'error' and details.get('severity') == 'warning':
                stored = previous
            elif previous.get('error_class') not in ('Warning','LogWarning') and details.get('error_class') == 'LogWarning':
                stored = previous | {'severity': details.get('severity', previous.get('severity'))}
        if row:
            self.db.execute('''UPDATE audit_incidents SET state='open',occurrences=occurrences+1,
                reopens=reopens+?,last_seen=?,closed_at=NULL,last_attempt=?,revision=revision+1,
                notified=CASE WHEN ? THEN 0 ELSE notified END,
                not_before=CASE WHEN ? THEN ? ELSE not_before END,details=?,action=? WHERE id=?''',
                (int(reopening), now, details.get('attempt_id'), int(reopening), int(reopening), now,
                 json.dumps(stored, ensure_ascii=False), action, identity))
        else:
            self.db.execute('INSERT INTO audit_incidents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (identity, uid, operation, key, 'open', 1, 0, now, now, None,
                 details.get('attempt_id'), 1, 0, now, None, 0, json.dumps(details, ensure_ascii=False), action, uuid.uuid4().hex))
            self.prune(0)
        if uid is not None and operation in ('source_download', 'telegram_delivery', 'library_publish', 'title_parse', 'quality_menu'):
            generation = self.db.execute('SELECT generation FROM audit_incidents WHERE id=?', (identity,)).fetchone()[0]
            self.db.execute('INSERT INTO audit_recovery_notices VALUES(?,?,0,?) ON CONFLICT(id) DO UPDATE SET '
                            'generation=excluded.generation,done=0,due=excluded.due', (identity,generation,now+300))
        return identity, 'open'

    def success(self, uid, details, now):
        if details.get('proof') not in PROOFS or not details.get('operation_key') or \
                EXPECTED_PROOFS.get(details.get('operation')) != details.get('proof'):
            return None
        identity, _, _, _ = self.identity(uid, details)
        row = self.db.execute('SELECT state,last_seen,last_attempt,details,message_id,digest,revision '
                              'FROM audit_incidents WHERE id=?', (identity,)).fetchone()
        if not row or row[0] != 'open':
            return None
        same_attempt = bool(details.get('attempt_id') and details['attempt_id'] == row[2])
        if not same_attempt and details.get('attempt_started', 0) < row[1]:
            return None  # A late success from an older parallel request is not recovery.
        old = json.loads(row[3])
        old['proof'] = details['proof']
        # If a short transient recovered before any notification, retain history quietly.
        notified = row[6] + 1 if row[4] is None and not row[5] else 0
        self.db.execute("UPDATE audit_incidents SET state='resolved',closed_at=?,revision=revision+1,"
                        "notified=CASE WHEN ? THEN ? ELSE notified END,not_before=?,details=? WHERE id=?",
                        (now, notified, notified, now, json.dumps(old, ensure_ascii=False), identity))
        return identity

    def prune(self, cutoff):
        self.db.execute("DELETE FROM audit_recovery_notices WHERE id NOT IN (SELECT id FROM audit_incidents)")
        self.db.execute("DELETE FROM audit_contacts WHERE user_id NOT IN (SELECT user_id FROM audit_incidents WHERE user_id IS NOT NULL)")
        self.db.execute("DELETE FROM audit_incidents WHERE state='resolved' AND revision<=notified AND closed_at<?", (cutoff,))
        count = self.db.execute('SELECT COUNT(*) FROM audit_incidents').fetchone()[0]
        if count > self.limit:
            number = count - self.limit
            self.db.execute("DELETE FROM audit_incidents WHERE id IN (SELECT id FROM audit_incidents "
                            "ORDER BY (state='open'),last_seen LIMIT ?)", (number,))
            self.db.execute('UPDATE audit_notification_state SET evicted=evicted+? WHERE id=1', (number,))

    def counts(self):
        rows = dict(self.db.execute('SELECT state,COUNT(*) FROM audit_incidents GROUP BY state'))
        pending = self.db.execute('SELECT COUNT(*) FROM audit_incidents WHERE revision>notified').fetchone()[0]
        next_api, failures, _, evicted = self.db.execute(
            'SELECT next_api,failures,digest_id,evicted FROM audit_notification_state WHERE id=1').fetchone()
        return dict(open=rows.get('open', 0), resolved=rows.get('resolved', 0), pending=pending,
                    failures=failures, evicted=evicted)

    def is_open(self, uid, details):
        identity = self.identity(uid, details)[0]
        row = self.db.execute('SELECT state FROM audit_incidents WHERE id=?', (identity,)).fetchone()
        return bool(row and row[0] == 'open')

    def owner_reply_confirmed(self, now=None):
        now = time.time() if now is None else now
        # Do not shorten a 429 cooldown when there was no transport failure.
        self.db.execute('UPDATE audit_notification_state SET next_api=MIN(next_api,?),failures=0 '
                        'WHERE id=1 AND failures>0', (now + self.API_INTERVAL,))
        self.db.commit()

    def rows(self, limit=12):
        raw = self.db.execute('SELECT id,user_id,state,occurrences,reopens,last_seen,closed_at,details,action '
                              "FROM audit_incidents ORDER BY (state='open') DESC,"
                              'MAX(last_seen,COALESCE(closed_at,0)) DESC LIMIT ?', (limit,)).fetchall()
        return [dict(zip(('id','user_id','state','count','reopens','last_seen','closed_at','details','action'),
                         (*row[:7], json.loads(row[7]), row[8]))) for row in raw]

    @staticmethod
    def scope(uid, owner, details):
        if details.get('scope') == 'service' or uid is None:
            return 'Служебная операция.'
        if owner is not None:
            return 'Твоя задача.' if uid == owner else 'Задача другого пользователя.'
        return ''

    def format(self, row, owner=None):
        details = row['details'] | {'fingerprint': row['id']}
        text = format_error_alert(row['action'], details,
                                  scope=self.scope(row['user_id'], owner, details))
        if row['state'] == 'resolved':
            heading = error_explanation(details, row['action'])[0]
            proof_text = {
                'telegram_access': 'Telegram снова принимает сообщения в этот чат.',
                'voice_enumeration': 'Список серий этой озвучки снова получен.',
                'jellyfin_refresh': 'Jellyfin принял запрос сканирования. Завершение самого сканирования отдельно не проверялось.',
                'search_provider': 'Источник поиска снова вернул результаты для того же запроса.',
            }.get(details.get('operation'), PROOF_LABELS[details['proof']])
            text = ('✅ Подтверждено восстановление\n' + heading + '\n' +
                    self.scope(row['user_id'], owner, details) + '\n' +
                    event_context(details) + '\n' +
                    proof_text + '\nКод: <code>' + row['id'] + '</code>')
        elif details.get('severity') == 'warning' and not text.lower().startswith('⚠ предупреждение'):
            text = text.replace('⚠ ', '⚠ Предупреждение: ', 1)
        if row['reopens']:
            text += f"\nПовторное открытие: {row['reopens']}."
        stamp = datetime.fromtimestamp(row['closed_at'] if row['state'] == 'resolved' else row['last_seen'], timezone.utc)
        text += '\n' + ('Подтверждено: ' if row['state'] == 'resolved' else 'Последнее событие: ') + stamp.strftime('%d.%m %H:%M UTC')
        if row['user_id'] is not None:
            text += '\nПользователь: ' + self.contact(row['user_id'])
        return text + f"\nСобытий: {row['count']}. Статус: " + ('решено' if row['state'] == 'resolved' else 'открыто')

    async def notify(self, sender, updater=None, *, owner=None, now=None):
        if not sender:
            return
        now = time.time() if now is None else now
        next_api, failures, digest_id = self.db.execute(
            'SELECT next_api,failures,digest_id FROM audit_notification_state WHERE id=1').fetchone()
        if now < next_api:
            return
        pending = self.db.execute('SELECT id,revision,message_id,digest,generation FROM audit_incidents '
            'WHERE revision>notified AND not_before<=? ORDER BY (state=\'resolved\') DESC,last_seen LIMIT 500', (now,)).fetchall()
        if not pending:
            return
        direct = [r for r in pending if r[2] is not None]
        use_digest = not direct and (any(r[3] for r in pending) or len(pending) > 3)
        if use_digest:
            selected = pending
            counts = self.counts()
            text = (f"⚠ Сводка проблем бота\nОткрыто: {counts['open']}. Решено: {counts['resolved']}.\n"
                    'Повторения объединены; детали и подтверждения — /errors.\n\n')
            for row in self.rows(6):
                label = error_explanation(row['details'], row['action'])[0]
                mark = '✅' if row['state'] == 'resolved' else '⚠'
                text += f"{mark} {html.escape(label)} · {row['id']} · событий: {row['count']}\n"
                if row['user_id'] is not None:
                    text += self.contact(row['user_id']) + '\n'
            if counts['evicted']:
                text += f"\nВытеснено лимитом хранения: {counts['evicted']}."
            message_id = digest_id
        else:
            selected = direct[:1] or pending[:1]
            identity, _, message_id, _, _ = selected[0]
            row = next(r for r in self.rows(self.limit) if r['id'] == identity)
            text = self.format(row, owner)
        # Reserve a durable global slot before awaiting Telegram; restarts cannot burst.
        self.db.execute('UPDATE audit_notification_state SET next_api=? WHERE id=1', (now + self.API_INTERVAL,))
        self.db.commit()
        try:
            if message_id is not None:
                if updater is None:
                    return  # Never replace an update with another push notification.
                result = await asyncio.wait_for(updater(message_id, text), timeout=5)
            else:
                result = await asyncio.wait_for(sender(text), timeout=5)
            actual_id = getattr(result, 'message_id', None)
            # Mock/alternative notifiers may not return a Telegram Message.
            if type(actual_id) is not int:
                actual_id = message_id
            if message_id is None and actual_id is None:
                actual_id = 0  # Acknowledged, but cannot safely edit this transport.
        except Exception as exc:
            from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
            if isinstance(exc, TelegramBadRequest) and message_id is not None and 'message is not modified' in exc.message.lower():
                actual_id = message_id
            elif isinstance(exc, TelegramBadRequest) and message_id is not None and any(
                    text in exc.message.lower() for text in ('message to edit not found', "message can't be edited")):
                # A deleted notification moves to one shared summary, never N new pushes.
                for identity, _, _, _, generation in selected:
                    self.db.execute('UPDATE audit_incidents SET message_id=NULL,digest=1 WHERE id=? AND generation=?', (identity,generation))
                if message_id == digest_id:
                    self.db.execute('UPDATE audit_notification_state SET digest_id=NULL WHERE id=1')
                self.db.commit()
                return
            else:
                wait = max(self.API_INTERVAL, exc.retry_after + 1) if isinstance(exc, TelegramRetryAfter) else (self.FAILURE_COOLDOWN if failures >= 2 else 30 * 2 ** failures)
                # 429 is confirmed rejection; it does not consume the uncertain-send budget.
                new_failures = failures if isinstance(exc, TelegramRetryAfter) else min(self.MAX_FAILURES, failures + 1)
                self.db.execute('UPDATE audit_notification_state SET next_api=?,failures=? WHERE id=1',
                                (now + wait, new_failures))
                self.db.commit()
                return
        for identity, revision, _, _, generation in selected:
            self.db.execute('UPDATE audit_incidents SET notified=?,message_id=?,digest=?,not_before='
                            'CASE WHEN revision=? THEN ? ELSE not_before END WHERE id=? AND generation=?',
                            (revision, None if use_digest else actual_id, int(use_digest),
                             revision, now + self.UPDATE_INTERVAL, identity, generation))
        if use_digest:
            self.db.execute('UPDATE audit_notification_state SET digest_id=? WHERE id=1', (actual_id,))
        self.db.execute('UPDATE audit_notification_state SET failures=0 WHERE id=1')
        self.db.commit()
