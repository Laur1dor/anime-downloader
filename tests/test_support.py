import tempfile,unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from aiogram.methods import SendMessage
from aiogram.exceptions import TelegramNetworkError,TelegramRetryAfter,TelegramBadRequest,TelegramForbiddenError
from animedl import observability as audit
from animedl.support import OutgoingSupportMiddleware
from animedl.handlers.support import _feedback_message,on_reply_command,on_outcome

class SupportTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.store=audit.AuditStore(Path(self.tmp.name)/'audit.db');self.store.owner_id=99
  self.old=audit._current;audit._current=self.store;self.s=self.store.support
  self.store.incidents.profile(17,'valid_user')
 async def asyncTearDown(self):
  audit._current=self.old;await self.store.close();self.tmp.cleanup()
 def message(self,uid=17,mid=1,text='test',reply=None,chat_type='private'):
  return NS(from_user=NS(id=uid),chat=NS(id=uid,type=chat_type),message_id=mid,text=text,reply_to_message=NS(message_id=reply) if reply else None,answer=AsyncMock(return_value=NS(message_id=50)))
 async def test_visible_outgoing_body_is_stored_but_owner_is_excluded(self):
  middleware=OutgoingSupportMiddleware();result=NS(message_id=1,text='Visible text',caption=None)
  await middleware(AsyncMock(return_value=result),None,SendMessage(chat_id=17,text='<b>Visible text</b>'))
  self.assertEqual(self.s.messages(17)[0][2],'Visible text')
  await middleware(AsyncMock(return_value=result),None,SendMessage(chat_id=99,text='owner'))
  self.assertEqual(self.s.messages(99),[])
 async def test_failed_send_has_unconfirmed_log_and_no_retry(self):
  method=SendMessage(chat_id=17,text='Exact intended text')
  request=AsyncMock(side_effect=TelegramNetworkError(method=method,message='timeout'))
  with self.assertRaises(TelegramNetworkError):await OutgoingSupportMiddleware()(request,None,method)
  request.assert_awaited_once()
  self.assertEqual(self.s.messages(17)[0][1:],('uncertain','Exact intended text'))
 def test_delivery_and_user_confirmation_are_independent_and_actor_checked(self):
  identity=self.s.begin(17,'Wanted Voice 1080p episodes 5-7',dict(expected=3,episodes=[5,6,7]))
  self.s.finish(identity,'delivered',dict(delivered=3))
  self.assertEqual(self.s.requests(17)[0][3:],('delivered','unknown'))
  self.assertFalse(self.s.outcome(identity,18,'yes'))
  self.assertTrue(self.s.outcome(identity,17,'no'))
  self.assertEqual(self.s.requests(17)[0][3:],('delivered','no'))
 async def test_reply_routing_preserves_active_choice_and_accepts_media_feedback(self):
  self.s.outgoing(17,1,'Выбери серии')
  message=self.message(reply=1,text='1-3')
  state=NS(get_state=AsyncMock(return_value='range'))
  self.assertFalse(await _feedback_message(message,NS(admin_id=99),self.store,state))
  self.s.outgoing(17,2,'[Видео] Episode',kind='media')
  message.reply_to_message.message_id=2
  self.assertTrue(await _feedback_message(message,NS(admin_id=99),self.store,state))
  message.chat.type='group'
  self.assertFalse(await _feedback_message(message,NS(admin_id=99),self.store,state))
 async def test_owner_reply_routes_only_to_known_user_and_handles_block(self):
  bot=NS(send_message=AsyncMock(return_value=NS(message_id=2)))
  await on_reply_command(self.message(99,text='/reply 17 Hello'),NS(admin_id=99),self.store,bot)
  self.assertEqual(bot.send_message.call_args.args[:2],(17,'Ответ владельца бота:\nHello\n\nМожно ответить на это сообщение.'))
  bot.send_message.reset_mock()
  await on_reply_command(self.message(18,text='/reply 17 attack'),NS(admin_id=99),self.store,bot)
  await on_reply_command(self.message(99,text='/reply 17 attack',chat_type='group'),NS(admin_id=99),self.store,bot)
  bot.send_message.assert_not_awaited()
  bot.send_message.side_effect=TelegramForbiddenError(method=SendMessage(chat_id=17,text='x'),message='blocked')
  owner=self.message(99,text='/reply 17 Hello')
  await on_reply_command(owner,NS(admin_id=99),self.store,bot)
  self.assertIn('заблокировал',owner.answer.call_args.args[0])
  bot.send_message.assert_awaited_once()
 async def test_outcome_does_not_block_immediate_written_details(self):
  identity=self.s.begin(17,'Wanted',dict(expected=1))
  cq=NS(data='fb:no:'+identity,from_user=NS(id=17),message=self.message(),answer=AsyncMock())
  await on_outcome(cq,self.store)
  self.assertTrue(self.s.allow_feedback(17))
 async def test_support_queue_coalesces_edits_and_limits_api_requests(self):
  sender=AsyncMock(return_value=NS(message_id=100));updater=AsyncMock()
  for i in range(100):self.s.enqueue('same',17,str(i))
  await self.s.tick(sender,updater,now=100)
  self.assertEqual(sender.await_count,1);self.assertEqual(sender.call_args.args[0],'99')
  self.assertEqual(self.s.owner_target(100),(17,''))
  self.s.enqueue('same',17,'new')
  await self.s.tick(sender,updater,now=101);updater.assert_not_awaited()
  await self.s.tick(sender,updater,now=131);updater.assert_awaited_once_with(100,'new')
 async def test_uncertain_owner_notification_is_not_resent(self):
  sender=AsyncMock(side_effect=TimeoutError());self.s.enqueue('one',17,'feedback')
  await self.s.tick(sender,now=100);await self.s.tick(sender,now=131)
  sender.assert_awaited_once()
 async def test_429_and_deleted_notification_are_safe(self):
  sender=AsyncMock(side_effect=[TelegramRetryAfter(method=SendMessage(chat_id=99,text='x'),message='wait',retry_after=80),NS(message_id=100),NS(message_id=101)])
  self.s.enqueue('one',17,'feedback')
  await self.s.tick(sender,now=100);await self.s.tick(sender,now=150)
  self.assertEqual(sender.await_count,1)
  await self.s.tick(sender,now=182)
  self.s.enqueue('one',17,'changed')
  updater=AsyncMock(side_effect=TelegramBadRequest(method=SendMessage(chat_id=99,text='x'),message='message to edit not found'))
  await self.s.tick(sender,updater,now=213);await self.s.tick(sender,updater,now=244)
  self.assertEqual(sender.await_count,3)
  self.assertEqual(self.s.owner_target(101),(17,''))
 def test_feedback_rate_and_retention(self):
  self.assertTrue(self.s.allow_feedback(17,now=100));self.assertFalse(self.s.allow_feedback(17,now=101))
  self.s.feedback(17,1,'exact user feedback')
  self.assertIn('exact user feedback',self.s.messages(17)[0][2])
  self.store.db.execute('UPDATE support_messages SET created=0');self.store.db.commit();self.s.prune()
  self.assertEqual(self.s.messages(17),[])

 async def test_batch_keeps_requested_episodes_and_partial_delivery_separate(self):
  from unittest.mock import patch,Mock
  from animedl.handlers.flow import _run_season
  from animedl.services import EpisodeResult
  from animedl.models import TitleInfo,EpisodeRef
  status=NS(edit_text=AsyncMock())
  title=TitleInfo('yummyanime','https://example.test/title','123','Title')
  result=[EpisodeResult(5,'sent_new',telegram='sent'),EpisodeResult(6,'error',telegram='error')]
  with patch('animedl.handlers.flow.process_episode',AsyncMock(side_effect=result)):
   failed=await _run_season(Mock(),NS(admin_id=99),Mock(),Mock(),title,
      {1:EpisodeRef(5,'e5',season=2),2:EpisodeRef(6,'e6',season=2)},[1,2],'Voice','auto',1080,
      NS(is_paused=False,cancelled=False),17,status,AsyncMock(),user_id=17)
  self.assertEqual(failed,[2])
  row=self.s.requests(17)[0]
  self.assertEqual(row[3:],('partial','unknown'))
  self.assertIn('S2E5',row[2]);self.assertIn('S2E6',row[2])
  buttons=status.edit_text.call_args.kwargs['reply_markup'].inline_keyboard
  self.assertTrue(any(b.callback_data=='fb:no:'+row[0] for group in buttons for b in group))
 def test_healthy_users_profile_survives_incident_pruning(self):
  self.s.begin(17,'Wanted',dict(expected=1))
  self.store.incidents.prune(0);self.store.db.commit()
  self.assertEqual(self.s.profile(17),'https://t.me/valid_user')
 def test_unicode_owner_messages_fit_utf16_budget(self):
  self.s.enqueue('emoji',17,'😀'*4000)
  text=self.store.db.execute('SELECT body FROM support_queue').fetchone()[0]
  self.assertLessEqual(len(text.encode('utf-16-le'))//2,3900)

 async def test_legacy_notification_reply_is_feedback_but_another_bot_is_not(self):
  message=self.message(reply=100,text='Не получилось')
  message.reply_to_message.from_user=NS(id=1234)
  message.reply_to_message.text='Извини за сбой, попробуй повторить'
  active=NS(get_state=AsyncMock(return_value='DownloadFlow:processing'))
  self.assertTrue(await _feedback_message(message,NS(admin_id=99),self.store,active,NS(id=1234)))
  self.assertFalse(await _feedback_message(message,NS(admin_id=99),self.store,active,NS(id=5678)))

 def test_reply_to_old_media_keeps_original_request_context(self):
  first=self.s.begin(17,'First anime',dict(expected=1))
  self.s.outgoing(17,55,'[Видео] First anime',kind='media')
  self.s.begin(17,'Another anime',dict(expected=1))
  self.assertEqual(self.s.reply_request(17,55),first)
  self.s.feedback(17,56,'Wrong voice',request_id=self.s.reply_request(17,55),reply_mid=55,quoted='First anime')
  body=self.store.db.execute("SELECT body FROM support_queue WHERE key='in:17:56'").fetchone()[0]
  self.assertIn('First anime',body);self.assertNotIn('Another anime',body)

 async def test_repeated_negative_button_does_not_create_repeated_prompts(self):
  identity=self.s.begin(17,'Wanted',dict(expected=1))
  message=self.message()
  cq=NS(data='fb:no:'+identity,from_user=NS(id=17),message=message,answer=AsyncMock())
  await on_outcome(cq,self.store);await on_outcome(cq,self.store)
  self.assertEqual(message.answer.await_count,1)
