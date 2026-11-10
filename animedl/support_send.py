"""Manual operator notification with the same persistent journal as the running bot.
Usage: python -m animedl.support_send USER_ID < message.txt
"""
import asyncio,json,sys
from .config import load_config
from .telegram_client import build_bot
from . import observability

async def main():
    uid=int(sys.argv[1]);body=sys.stdin.read().strip()
    if uid<=0 or not body or len(body)>3500:raise ValueError('Expected user ID and message up to 3500 characters')
    cfg=load_config();store=observability.AuditStore(cfg.cache_db_path.with_name('audit.sqlite3'))
    store.owner_id=cfg.admin_id
    previous=observability._current;observability._current=store
    bot=build_bot(cfg)
    try:
        if not store.db.execute('SELECT 1 FROM audit_contacts WHERE user_id=?',(uid,)).fetchone():
            raise ValueError('User has no recorded conversation with this bot')
        message=await bot.send_message(uid,body,parse_mode=None)
        store.support.outgoing(uid,message.message_id,message.text or body,kind='support',owner=cfg.admin_id)
        print(json.dumps({'sent':True,'message_id':message.message_id}))
    finally:
        observability._current=previous
        await store.close();await bot.session.close()

if __name__=='__main__':asyncio.run(main())
