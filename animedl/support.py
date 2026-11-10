"""Bounded, persistent support conversations and separate delivery/user outcomes."""
from __future__ import annotations
import json
import time
import uuid

def telegram_text(text, units=3900):
    raw=text.encode('utf-16-le')
    return text if len(raw)<=units*2 else raw[:(units-1)*2].decode('utf-16-le',errors='ignore')+'…'


class SupportStore:
    def __init__(self, db):
        self.db=db
        db.executescript("""CREATE TABLE IF NOT EXISTS support_messages(
          uid INTEGER,mid INTEGER,direction TEXT,body TEXT,kind TEXT,created REAL,updated REAL,
          PRIMARY KEY(uid,mid,direction));
        CREATE TABLE IF NOT EXISTS support_requests(
          id TEXT PRIMARY KEY,uid INTEGER,description TEXT,technical TEXT,user_outcome TEXT,
          created REAL,updated REAL,details TEXT);
        CREATE TABLE IF NOT EXISTS support_queue(
          key TEXT PRIMARY KEY,uid INTEGER,request_id TEXT,body TEXT,version INTEGER,acked INTEGER,
          owner_mid INTEGER,next_at REAL,failures INTEGER,created REAL);
        CREATE TABLE IF NOT EXISTS support_callback_limits(uid INTEGER PRIMARY KEY,last_at REAL);
        CREATE TABLE IF NOT EXISTS support_meta(key TEXT PRIMARY KEY,value INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS support_limits(uid INTEGER PRIMARY KEY,last_at REAL,day INTEGER,count INTEGER);
        """)
        if 'request_id' not in {r[1] for r in db.execute('PRAGMA table_info(support_messages)')}:
            db.execute("ALTER TABLE support_messages ADD COLUMN request_id TEXT NOT NULL DEFAULT ''")
            db.commit()

    def profile(self, uid):
        row=self.db.execute('SELECT username FROM audit_contacts WHERE user_id=?',(uid,)).fetchone()
        return 'https://t.me/'+row[0] if row and row[0] else 'tg://user?id='+str(uid)

    def enqueue(self, key, uid, body, request_id=''):
        self.db.execute('INSERT INTO support_queue VALUES(?,?,?,?,1,0,NULL,0,0,?) '
            'ON CONFLICT(key) DO UPDATE SET body=excluded.body,version=version+1,next_at=0',
            (key,uid,request_id,telegram_text(body),time.time()))
        self.db.commit()
        self.prune()

    def outgoing(self, uid, mid, body, kind='message', *, owner=None):
        if type(uid) is not int or uid<=0 or uid==owner or type(mid) is not int:return
        now=time.time()
        important=kind!='historical' and (kind=='support' or body.startswith(('Извини','Извините','✅ Готово.','⚠ Завершено','🛑 Остановлено.')))
        self.db.execute('INSERT INTO support_messages(uid,mid,direction,body,kind,created,updated) VALUES(?,?,?,?,?,?,?) '
            'ON CONFLICT(uid,mid,direction) DO UPDATE SET body=excluded.body,kind=excluded.kind,updated=excluded.updated',
            (uid,mid,'out',body[:4096],'reply_target' if important or kind=='media' else kind,now,now))
        self.db.commit()
        if kind=='media':
            request=self.db.execute('SELECT id FROM support_requests WHERE uid=? ORDER BY created DESC LIMIT 1',(uid,)).fetchone()
            if request:self.mark_target(uid,mid,request[0])
        if important and body.startswith(('✅ Готово.','⚠ Завершено','🛑 Остановлено.')):
            row=self.db.execute('SELECT id,details FROM support_requests WHERE uid=? ORDER BY created DESC LIMIT 1',(uid,)).fetchone()
            if row:
                details=json.loads(row[1]);details['last_summary']=body[:2500]
                self.db.execute('UPDATE support_requests SET details=? WHERE id=?',(json.dumps(details,ensure_ascii=False),row[0]));self.db.commit()
                self.mark_target(uid,mid,row[0])
                self.request_report(row[0]);return
        if important:
            self.enqueue(f'out:{uid}:{mid}',uid,
                f'Сообщение пользователю {uid}\n{self.profile(uid)}\nПолный текст: /user_message {uid} {mid}\nОтветь на это сообщение, чтобы написать пользователю.\n\n{body}')
        self.prune()

    def mark_target(self, uid, mid, request_id=''):
        self.db.execute("UPDATE support_messages SET kind='reply_target',request_id=? WHERE uid=? AND mid=? AND direction='out'",(request_id,uid,mid));self.db.commit()

    def reply_request(self, uid, mid):
        row=self.db.execute("SELECT request_id FROM support_messages WHERE uid=? AND mid=? AND direction='out'",(uid,mid)).fetchone()
        return row[0] if row else '' 

    def target(self, uid, mid, *, any_message=False):
        row=self.db.execute("SELECT kind FROM support_messages WHERE uid=? AND mid=? AND direction='out'",(uid,mid)).fetchone()
        return bool(row and (any_message or row[0]=='reply_target'))

    def owner_target(self, mid):
        return self.db.execute('SELECT uid,request_id FROM support_queue WHERE owner_mid=?',(mid,)).fetchone()

    def allow_feedback(self, uid, now=None):
        now=time.time() if now is None else now;day=int(now//86400)
        row=self.db.execute('SELECT last_at,day,count FROM support_limits WHERE uid=?',(uid,)).fetchone()
        if row and (now-row[0]<10 or row[1]==day and row[2]>=20):return False
        count=row[2]+1 if row and row[1]==day else 1
        self.db.execute('INSERT OR REPLACE INTO support_limits VALUES(?,?,?,?)',(uid,now,day,count));self.db.commit();return True

    def allow_outcome(self, uid, now=None):
        now=time.time() if now is None else now
        row=self.db.execute('SELECT last_at FROM support_callback_limits WHERE uid=?',(uid,)).fetchone()
        if row and now-row[0]<10:return False
        self.db.execute('INSERT OR REPLACE INTO support_callback_limits VALUES(?,?)',(uid,now));self.db.commit();return True

    def feedback(self, uid, mid, body, request_id='', *, reply_mid=None, quoted=''):
        now=time.time()
        self.db.execute('INSERT OR IGNORE INTO support_messages(uid,mid,direction,body,kind,created,updated) VALUES(?,?,?,?,?,?,?)',(uid,mid,'in',body[:2000],'feedback',now,now));self.db.commit()
        context=''
        row=self.db.execute('SELECT id,description,technical,user_outcome FROM support_requests WHERE uid=? AND id=?',(uid,request_id)).fetchone() if request_id else self.db.execute('SELECT id,description,technical,user_outcome FROM support_requests WHERE uid=? ORDER BY created DESC LIMIT 1',(uid,)).fetchone()
        if row:
            context=f'Последний запрос #{row[0]}: {row[1]}\nДоставка: {row[2]}; оценка пользователя: {row[3]}\n'
        if reply_mid:
            context+=f'Ответ на сообщение {reply_mid}: {quoted[:600]}\nПолный текст: /user_message {uid} {reply_mid}\n'
        self.enqueue(f'in:{uid}:{mid}',uid,f'Обратная связь от {uid}\n{self.profile(uid)}\n{context}\n{body[:2000]}\n\nОтветь на это сообщение, чтобы ответить пользователю.',request_id or (row[0] if row else ''))

    def begin(self, uid, description, details):
        identity=uuid.uuid4().hex[:16];now=time.time()
        self.db.execute('INSERT INTO support_requests VALUES(?,?,?,?,?,?,?,?)',
            (identity,uid,description[:1000],'in_progress','unknown',now,now,json.dumps(details,ensure_ascii=False)))
        self.db.commit();self.prune();return identity

    def finish(self, identity, technical, details):
        row=self.db.execute('SELECT details FROM support_requests WHERE id=?',(identity,)).fetchone()
        if not row:return
        saved=json.loads(row[0]);saved.update(details)
        self.db.execute('UPDATE support_requests SET technical=?,details=?,updated=? WHERE id=?',
                        (technical,json.dumps(saved,ensure_ascii=False),time.time(),identity));self.db.commit()
        self.request_report(identity)

    def outcome(self, identity, uid, outcome):
        if outcome not in ('yes','no'):return False
        row=self.db.execute('SELECT user_outcome FROM support_requests WHERE id=? AND uid=?',(identity,uid)).fetchone()
        if not row:return False
        if row[0]!=outcome:
            self.db.execute('UPDATE support_requests SET user_outcome=?,updated=? WHERE id=? AND uid=?',(outcome,time.time(),identity,uid));self.db.commit()
            self.request_report(identity)
        return True

    def request_report(self, identity):
        row=self.db.execute('SELECT uid,description,technical,user_outcome,details FROM support_requests WHERE id=?',(identity,)).fetchone()
        if not row:return
        uid,description,technical,outcome,raw=row;details=json.loads(raw)
        technical={'in_progress':'выполняется','delivered':'доставка подтверждена сервисом','partial':'есть ошибки доставки','cancelled':'отменён','blocked':'пользователь заблокировал бота'}.get(technical,technical)
        summary='\n\nПоследнее сообщение пользователю:\n'+details.get('last_summary','') if details.get('last_summary') else ''
        label={'yes':'пользователь подтвердил успех','no':'пользователь сообщил о проблеме','unknown':'пользователь ещё не подтвердил'}[outcome]
        self.enqueue('request:'+identity,uid,f'Запрос #{identity}\nПользователь {uid}\n{self.profile(uid)}\n{description}\n\nТехнический результат: {technical}.\nОтправлено успешно: {details.get("delivered",0)} из {details.get("expected",0)}.\nОбратная связь: {label}.{summary}\n\nОтветь на это сообщение, чтобы написать пользователю.',identity)

    def requests(self, uid=None):
        return self.db.execute('SELECT id,uid,description,technical,user_outcome FROM support_requests '+
            ('WHERE uid=? ' if uid else '')+'ORDER BY created DESC LIMIT 10', (uid,) if uid else ()).fetchall()

    def messages(self, uid):
        return self.db.execute('SELECT mid,direction,body FROM support_messages WHERE uid=? ORDER BY updated DESC LIMIT 5',(uid,)).fetchall()

    def prune(self):
        cutoff=time.time()-30*86400
        for table, limit in [('support_messages',10000),('support_requests',1000),('support_queue',1000)]:
            self.db.execute(f'DELETE FROM {table} WHERE created<?',(cutoff,))
            self.db.execute(f'DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} ORDER BY created DESC LIMIT -1 OFFSET ?)',(limit,))
            removed=self.db.execute('SELECT changes()').fetchone()[0]
            if removed:
                self.db.execute('INSERT INTO support_meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=value+excluded.value',(table+'_evicted',removed))
        self.db.execute('DELETE FROM support_limits WHERE last_at<?',(cutoff,))
        self.db.execute('DELETE FROM support_callback_limits WHERE last_at<?',(cutoff,));self.db.commit()

    async def tick(self, sender, updater=None, *, now=None):
        if not sender:return
        import asyncio
        from aiogram.exceptions import TelegramRetryAfter,TelegramBadRequest
        now=time.time() if now is None else now
        if now<self.db.execute('SELECT next_api FROM audit_notification_state WHERE id=1').fetchone()[0]:return
        row=self.db.execute('SELECT key,uid,body,version,owner_mid FROM support_queue WHERE version>acked AND next_at<=? ORDER BY created LIMIT 1',(now,)).fetchone()
        if not row:return
        key,uid,body,version,mid=row
        self.db.execute('UPDATE audit_notification_state SET next_api=? WHERE id=1',(now+30,))
        self.db.execute('UPDATE support_queue SET acked=? WHERE key=?',(version,key));self.db.commit()
        try:
            result=await asyncio.wait_for(updater(mid,body) if mid and updater else sender(body),timeout=5)
            actual=getattr(result,'message_id',mid)
            if type(actual) is int:self.db.execute('UPDATE support_queue SET owner_mid=? WHERE key=?',(actual,key))
        except TelegramRetryAfter as exc:
            delay=max(30,exc.retry_after+1)
            self.db.execute('UPDATE support_queue SET acked=MIN(acked,?),next_at=? WHERE key=?',(version-1,now+delay,key))
            self.db.execute('UPDATE audit_notification_state SET next_api=? WHERE id=1',(now+delay,))
        except TelegramBadRequest as exc:
            if mid and any(x in exc.message.lower() for x in ('message to edit not found', "message can't be edited")):
                self.db.execute('UPDATE support_queue SET owner_mid=NULL,acked=MIN(acked,?),next_at=? WHERE key=?',(version-1,now+30,key))
            elif 'message is not modified' not in exc.message.lower():
                self.db.execute('UPDATE support_queue SET failures=failures+1 WHERE key=?',(key,))
        except Exception:
            self.db.execute('UPDATE support_queue SET failures=failures+1 WHERE key=?',(key,))
        self.db.commit()


def current_support():
    from . import observability
    return observability._current.support if observability._current else None


from aiogram.client.session.middlewares.base import BaseRequestMiddleware

class OutgoingSupportMiddleware(BaseRequestMiddleware):
    async def __call__(self, make_request, bot, method):
        from . import observability
        from aiogram.methods import SendMessage, EditMessageText, SendVideo, SendDocument
        try:
            result=await make_request(bot,method)
        except Exception:
            store=observability._current
            if store and isinstance(method,(SendMessage,EditMessageText,SendVideo,SendDocument)):
                uid=getattr(method,'chat_id',None)
                if type(uid) is int and uid>0 and uid!=getattr(store,'owner_id',None):
                    try:
                        now=time.time()
                        body=getattr(method,'text',None) or getattr(method,'caption',None) or '[Медиа без подписи]'
                        store.db.execute('INSERT OR IGNORE INTO support_messages(uid,mid,direction,body,kind,created,updated) VALUES(?,?,?,?,?,?,?)',
                            (uid,time.time_ns(),'uncertain',body[:4096],'unconfirmed',now,now));store.db.commit()
                    except Exception:
                        pass
            raise
        store=observability._current
        if store and isinstance(method,(SendMessage,EditMessageText,SendVideo,SendDocument)):
            try:
                uid=getattr(method,'chat_id',None)
                mid=getattr(result,'message_id',getattr(method,'message_id',None))
                body=getattr(result,'text',None) or getattr(result,'caption',None) or getattr(method,'text',None) or getattr(method,'caption',None) or ''
                if isinstance(method,(SendVideo,SendDocument)):
                    media=getattr(result,'video',None) or getattr(result,'document',None)
                    body=f'[Медиа: {getattr(media,"file_size",0) or 0} байт] '+body
                store.support.outgoing(uid,mid,body,kind='media' if isinstance(method,(SendVideo,SendDocument)) else 'message',owner=getattr(store,'owner_id',None))
            except Exception:
                import logging
                logging.getLogger(__name__).warning('Support journal unavailable',extra={'audit_recorded':True})
        return result


def safe_finish(store, identity, technical, details):
    if not store or not identity:return
    try:
        store.finish(identity,technical,details)
    except Exception:
        import logging
        logging.getLogger(__name__).warning('Support result journal unavailable',extra={'audit_recorded':True})
