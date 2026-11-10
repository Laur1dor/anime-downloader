"""Support is explicit: feedback/replies never silently become media searches."""
from __future__ import annotations
from aiogram import Router,F
from aiogram.filters import Command
from aiogram.types import Message,CallbackQuery,ForceReply
from aiogram.exceptions import TelegramForbiddenError
from ..observability import AuditStore
from ..config import Config
router=Router()

def _owner(message,cfg):
    return bool(message.from_user and message.from_user.id==cfg.admin_id and message.chat.type=="private" and message.chat.id==cfg.admin_id)

async def _feedback_message(message:Message,cfg:Config,audit:AuditStore,state=None,bot=None):
    if not message.from_user or not message.text or not message.text.strip() or message.chat.type!="private":return False
    if message.text.split()[0].split('@')[0]=='/feedback':return True
    if not audit or message.text.startswith('/'):return False
    reply=message.reply_to_message
    if not reply:return False
    if message.from_user.id==cfg.admin_id:
        return bool(audit.support.owner_target(reply.message_id))
    state_name=await state.get_state() if state else None
    idle=state_name in (None,"DownloadFlow:processing","DownloadFlow:confirm")
    known=audit.support.target(message.from_user.id,reply.message_id,any_message=idle)
    own_bot=bool(bot and getattr(getattr(reply,'from_user',None),'id',None)==bot.id)
    important=(getattr(reply,'text',None) or '').startswith(('Извини','Извините','✅ Готово.','⚠ Завершено','Ответ владельца'))
    return known or own_bot and (idle or important)

@router.message(_feedback_message)
async def on_feedback(message:Message,cfg:Config,audit:AuditStore,bot) -> None:
    if not audit:
        await message.answer("Журнал обратной связи временно недоступен. Попробуй позже — сообщение не сохранено.");return
    uid=message.from_user.id;store=audit.support
    target=store.owner_target(message.reply_to_message.message_id) if message.reply_to_message and uid==cfg.admin_id else None
    if target:
        await _reply(message,bot,store,target[0],message.text,request_id=target[1])
        return
    body=message.text
    if body.split()[0].split('@')[0]=='/feedback':
        chunks=body.split(maxsplit=1)
        body=chunks[1].strip() if len(chunks)>1 else ''
    if not body:
        prompt=await message.answer('Напиши, что получилось или что пошло не так. Сообщение увидит владелец бота.',reply_markup=ForceReply(selective=True))
        store.mark_target(uid,prompt.message_id)
        return
    if len(body)>2000:
        await message.answer('Для обратной связи отправь текст до 2000 символов.');return
    if not store.allow_feedback(uid):
        await message.answer('Сообщения обратной связи ограничены: подожди немного. Максимум 20 обращений в сутки.');return
    reply=message.reply_to_message
    quoted=(getattr(reply,'text',None) or getattr(reply,'caption',None) or '[Медиа]') if reply else ''
    if reply and getattr(getattr(reply,'from_user',None),'id',None)==bot.id and not store.target(uid,reply.message_id,any_message=True):
        store.outgoing(uid,reply.message_id,quoted,kind='historical',owner=cfg.admin_id)
        store.mark_target(uid,reply.message_id)
    store.feedback(uid,message.message_id,body,request_id=store.reply_request(uid,reply.message_id) if reply else '',reply_mid=reply.message_id if reply else None,quoted=quoted)
    ack=await message.answer('Спасибо! Обратная связь записана для владельца. Здесь можно продолжить переписку ответом на сообщение.')
    store.mark_target(uid,ack.message_id)

@router.callback_query(F.data.startswith('fb:'))
async def on_outcome(cq:CallbackQuery,audit:AuditStore):
    if not audit or not cq.from_user or not cq.message or cq.message.chat.type!="private":return
    parts=(cq.data or '').split(':')
    if len(parts)!=3 or parts[1] not in ('yes','no'):
        await cq.answer('Некорректный ответ');return
    identity=parts[2]
    if not audit.db.execute('SELECT 1 FROM support_requests WHERE id=? AND uid=?',(identity,cq.from_user.id)).fetchone():
        await cq.answer('Этот запрос недоступен',show_alert=True);return
    import time
    previous=audit.db.execute('SELECT user_outcome,updated FROM support_requests WHERE id=?',(identity,)).fetchone()
    if previous[0]==parts[1]:
        await cq.answer('Ответ уже записан');return
    if not audit.support.allow_outcome(cq.from_user.id) or previous[0]!='unknown' and time.time()-previous[1]<10:
        await cq.answer('Подожди немного перед следующим ответом');return
    audit.support.outcome(identity,cq.from_user.id,parts[1])
    await cq.answer('Спасибо, ответ записан')
    if parts[1]=='no':
        prompt=await cq.message.answer('Опиши проблему ответом на это сообщение — владелец увидит её вместе с результатом запроса.',reply_markup=ForceReply(selective=True))
        audit.support.mark_target(cq.from_user.id,prompt.message_id,identity)

async def _reply(message,bot,store,uid,body,request_id=''):
    if len(body)>2000:
        await message.answer('Ответ должен быть до 2000 символов.');return
    try:
        sent=await bot.send_message(uid,'Ответ владельца бота:\n'+body+'\n\nМожно ответить на это сообщение.',parse_mode=None)
        store.mark_target(uid,sent.message_id,request_id)
        await message.answer('Ответ доставлен в Telegram.')
    except TelegramForbiddenError:
        store.db.execute('UPDATE audit_contacts SET blocked=1 WHERE user_id=?',(uid,));store.db.commit()
        await message.answer('Ответ не доставлен: пользователь заблокировал бота. Повторять отправку не буду.')
    except Exception:
        await message.answer('Нет подтверждения доставки ответа. Автоматический повтор отключён, чтобы не отправить дубль.')

@router.message(Command('reply'))
async def on_reply_command(message:Message,cfg:Config,audit:AuditStore,bot):
    if not _owner(message,cfg) or not audit:return
    args=(message.text or '').split(maxsplit=2)
    if len(args)!=3 or not args[1].isdigit():
        await message.answer('Ответ пользователю: /reply USER_ID текст');return
    uid=int(args[1])
    if not audit.db.execute('SELECT 1 FROM audit_contacts WHERE user_id=?',(uid,)).fetchone():
        await message.answer('Пользователь не найден в истории бота.');return
    await _reply(message,bot,audit.support,uid,args[2])

@router.message(Command('support'))
async def on_support(message:Message,cfg:Config,audit:AuditStore):
    if not _owner(message,cfg) or not audit:return
    args=(message.text or '').split()
    uid=int(args[1]) if len(args)==2 and args[1].isdigit() else None
    rows=audit.support.requests(uid)
    pending=audit.db.execute('SELECT COUNT(*) FROM support_queue WHERE version>acked').fetchone()[0]
    failed=audit.db.execute('SELECT COUNT(*) FROM support_queue WHERE failures>0').fetchone()[0]
    evicted=sum(r[0] for r in audit.db.execute('SELECT value FROM support_meta'))
    text=f'Запросы: технический результат и оценка пользователя отдельно.\nОчередь уведомлений: {pending}; неподтверждённых отправок: {failed}; вытеснено лимитом хранения: {evicted}.\n'
    for identity,actor,description,technical,outcome in rows:
        technical={'in_progress':'выполняется','delivered':'доставка подтверждена','partial':'есть ошибки','cancelled':'отменён','blocked':'бот заблокирован'}.get(technical,technical)
        outcome={'yes':'пользователь подтвердил успех','no':'пользователь сообщил о проблеме','unknown':'ещё нет ответа'}.get(outcome,outcome)
        text+=f'\n#{identity} · {actor}\n{description[:180]}\nДоставка: {technical}; оценка: {outcome}\n{audit.support.profile(actor)}\n'
    await message.answer((text+'\nИстория сообщений: /user_log USER_ID. Ответ: /reply USER_ID текст.')[:4000],parse_mode=None)

@router.message(Command('user_log'))
async def on_user_log(message:Message,cfg:Config,audit:AuditStore):
    if not _owner(message,cfg) or not audit:return
    args=(message.text or '').split()
    if len(args)!=2 or not args[1].isdigit():
        await message.answer('История: /user_log USER_ID');return
    uid=int(args[1]);rows=audit.support.messages(uid)
    lines=[f'Сообщения пользователя {uid}',audit.support.profile(uid)]
    for mid,direction,body in rows:
        lines.append(f'\n{direction} · сообщение {mid}:\n'+body[:450])
    lines.append('\nПолный текст: /user_message USER_ID MESSAGE_ID')
    await message.answer('\n'.join(lines)[:4000],parse_mode=None)

@router.message(Command('user_message'))
async def on_user_message(message:Message,cfg:Config,audit:AuditStore):
    if not _owner(message,cfg) or not audit:return
    args=(message.text or '').split()
    if len(args)!=3 or not all(x.isdigit() for x in args[1:]):
        await message.answer('Полный текст: /user_message USER_ID MESSAGE_ID');return
    row=audit.db.execute('SELECT body FROM support_messages WHERE uid=? AND mid=? ORDER BY updated DESC LIMIT 1',(int(args[1]),int(args[2]))).fetchone()
    await message.answer(row[0] or '[Сообщение без текста]' if row else 'Сообщение не найдено или срок хранения истёк.',parse_mode=None)
