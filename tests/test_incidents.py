import asyncio
import json
import logging
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import SendMessage, EditMessageText
from animedl import observability as audit


class IncidentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'audit.db'
        self.store = audit.AuditStore(self.path)
        self.sender = AsyncMock(return_value=SimpleNamespace(message_id=71))
        self.updater = AsyncMock(return_value=True)

    async def asyncTearDown(self):
        await self.store.close()
        self.temp.cleanup()

    def op(self, name='source_download', resource='episode-9', start=10, **details):
        return audit.operation(name, resource, **details) | {'attempt_started': start}

    def fail(self, op, *, uid=1, at=20, status='error'):
        self.store.record('source_error', uid, status, _event_time=at,
                          **(op | audit.error_details(ValueError('SECRET https://private.invalid/'))))

    def succeed(self, op, *, uid=1, at=30, proof='download_validated'):
        self.store.record('operation_succeeded', uid, _event_time=at, **op, proof=proof)

    def one(self):
        rows = self.store.incidents.rows()
        self.assertEqual(len(rows), 1)
        return rows[0]

    async def notify(self, now):
        await self.store.incidents.notify(self.sender, self.updater, owner=1, now=now)

    def test_only_matching_actor_operation_resource_and_proof_can_resolve(self):
        op = self.op()
        self.fail(op)
        self.succeed(op, uid=2)
        self.succeed(self.op(resource='episode-10'))
        self.succeed(op, proof='asset_verified')
        self.assertEqual(self.store.incidents.counts()['open'], 1)
        self.succeed(op)
        self.assertEqual(self.one()['state'], 'resolved')
        self.assertEqual(self.one()['details']['proof'], 'download_validated')
        self.assertNotIn('SECRET', json.dumps(self.one()))

    def test_service_recovery_can_be_proved_by_another_users_same_backend_request(self):
        op = self.op('jellyfin_refresh', 'server', scope='service')
        self.fail(op, uid=1)
        self.succeed(op, uid=2, proof='http_accepted')
        self.assertEqual(self.one()['state'], 'resolved')

    def test_late_parallel_success_does_not_close_newer_failure(self):
        old, failing = self.op(start=5), self.op(start=15)
        self.fail(failing, at=20)
        self.succeed(old, at=30)
        self.assertEqual(self.one()['state'], 'open')
        self.succeed(self.op(start=21), at=31)
        self.assertEqual(self.one()['state'], 'resolved')

    def test_late_warning_does_not_reopen_or_replace_newer_evidence(self):
        old, newer = self.op(start=5), self.op(start=15)
        self.fail(newer, at=20)
        self.fail(old, at=10, status='warning')
        self.succeed(old, at=21)
        self.assertEqual(self.one()['state'], 'open')
        self.succeed(newer, at=22)
        self.fail(old, at=11, status='warning')
        self.assertEqual(self.one()['state'], 'resolved')
        self.assertEqual(self.one()['count'], 3)
        self.assertEqual(self.one()['reopens'], 0)

    async def test_silence_does_not_resolve_unknown_warning(self):
        op = self.op('unverified', 'logger-location')
        self.fail(op, status='warning')
        self.succeed(op, proof='http_accepted')
        await self.notify(10000)
        self.assertEqual(self.one()['state'], 'open')

    async def test_quick_transient_is_kept_in_history_without_push(self):
        op = self.op()
        self.fail(op)
        self.succeed(op)
        await self.notify(100)
        self.sender.assert_not_awaited()
        self.assertEqual(self.one()['state'], 'resolved')

    async def test_resolution_and_reopen_edit_same_notification(self):
        op = self.op()
        self.fail(op)
        await self.notify(100)
        self.succeed(op, at=110)
        await self.notify(120)
        self.updater.assert_not_awaited()  # Global slot is durable, even for edits.
        await self.notify(131)
        self.assertEqual(self.updater.call_args.args[0], 71)
        self.assertIn('✅ Подтверждено восстановление', self.updater.call_args.args[1])
        self.fail(self.op(start=140), at=141)
        await self.notify(162)
        self.assertEqual(self.one()['reopens'], 1)
        self.assertIn('открыто', self.updater.call_args.args[1])
        self.sender.assert_awaited_once()

    async def test_hundred_repetitions_are_one_incident_and_no_burst(self):
        op = self.op()
        for i in range(100):
            self.fail(op, at=20+i/100)
        await self.notify(100)
        await self.notify(101)
        self.sender.assert_awaited_once()
        self.assertEqual(self.one()['count'], 100)

    async def test_hundred_distinct_errors_use_one_summary_and_bounded_storage(self):
        self.store.incidents.limit = 50
        for i in range(100):
            self.fail(self.op(resource=str(i)), at=20+i/100)
        await self.notify(100)
        self.sender.assert_awaited_once()
        self.assertIn('Сводка проблем', self.sender.call_args.args[0])
        counts = self.store.incidents.counts()
        self.assertEqual((counts['open'], counts['evicted'], counts['pending']), (50, 50, 0))
        self.assertLess(len(self.sender.call_args.args[0]), 4096)

    async def test_429_honours_retry_after_across_restart(self):
        self.fail(self.op())
        self.sender.side_effect = TelegramRetryAfter(method=SendMessage(chat_id=1,text='alert'),
                                                     message='retry', retry_after=900)
        await self.notify(100)
        await self.store.close()
        self.store = audit.AuditStore(self.path)
        await self.notify(500)
        self.sender.assert_awaited_once()
        self.assertEqual(self.store.incidents.counts()['failures'], 0)
        self.sender.side_effect = None
        await self.notify(1002)
        self.assertEqual(self.sender.await_count, 2)

    async def test_failed_notifications_have_persistent_bounded_retry_budget(self):
        self.fail(self.op())
        self.sender.side_effect = TimeoutError('SECRET')
        for now in (100, 200, 400, 500):
            await self.notify(now)
        self.assertEqual(self.sender.await_count, 3)
        await self.store.close()
        self.store = audit.AuditStore(self.path)
        await self.notify(699)
        self.assertEqual(self.sender.await_count, 3)
        self.assertEqual(self.store.incidents.counts()['pending'], 1)
        self.sender.side_effect = None
        await self.notify(701)  # One half-open probe after the five-minute cooldown.
        self.assertEqual(self.sender.await_count, 4)
        self.assertEqual(self.store.incidents.counts()['failures'], 0)

    async def test_deleted_notification_falls_back_to_one_shared_summary(self):
        op = self.op()
        self.fail(op)
        await self.notify(100)
        self.succeed(op, at=110)
        self.updater.side_effect = TelegramBadRequest(
            method=EditMessageText(chat_id=1,message_id=71,text='update'),message='message to edit not found')
        await self.notify(131)
        await self.notify(162)
        self.assertEqual(self.sender.await_count, 2)
        self.assertIn('Сводка проблем', self.sender.call_args.args[0])
        self.assertEqual(self.store.incidents.counts()['resolved'], 1)

    async def test_success_arriving_during_alert_is_not_lost(self):
        op = self.op()
        self.fail(op)
        async def send(text):
            self.succeed(op, at=110)
            return SimpleNamespace(message_id=71)
        self.sender.side_effect = send
        await self.notify(100)
        self.assertEqual(self.store.incidents.counts()['pending'], 1)
        await self.notify(131)
        self.assertIn('решено', self.updater.call_args.args[1])
        self.assertEqual(self.store.incidents.counts()['pending'], 0)

    def test_threaded_warning_storm_is_bounded_and_contains_no_log_text(self):
        handler = audit.WarningCollector(self.store)
        record = logging.LogRecord('animedl.test',logging.WARNING,'/private/file.py',7,
            'SECRET https://private.invalid/%s',('private-token',),None,func='worker')
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: handler.handle(record), range(1000)))
        self.assertEqual(self.store.log_queue.qsize(), 256)
        self.store._drain_logs()
        lost = self.store.db.execute("SELECT value FROM audit_meta WHERE key='warnings_dropped'").fetchone()[0]
        self.assertEqual(lost, 744)
        raw = ' '.join(r[0] for r in self.store.db.execute('SELECT details FROM audit_events'))
        self.assertNotIn('SECRET', raw)
        self.assertNotIn('private-token', raw)
        self.assertNotIn('/private/', raw)
        rows=self.store.incidents.rows()
        self.assertEqual(next(r for r in rows if r['action']=='logging_warning')['count'],256)
        self.assertEqual(next(r for r in rows if r['action']=='warning_collection_overflow')['details']['dropped_warnings'],744)

    def test_queued_warning_is_processed_before_its_confirmed_success(self):
        handler = audit.WarningCollector(self.store)
        op = self.op(start=0)
        previous = audit._current
        audit._current = self.store
        user_token = audit._user.set(1)
        try:
            with audit.bind_operation(op):
                handler.handle(logging.LogRecord('animedl.test',logging.WARNING,'file.py',7,'SECRET',(),None))
            audit.confirmed(op, 'download_validated', 1)
        finally:
            audit._user.reset(user_token)
            audit._current = previous
        self.assertEqual(self.one()['state'], 'resolved')

    async def test_notification_warning_cannot_feed_itself(self):
        self.store.notifier = AsyncMock()
        handler = audit.WarningCollector(self.store)
        def inner(text):
            handler.handle(logging.LogRecord('animedl.telegram_client',logging.WARNING,'file.py',7,'SECRET',(),None))
        self.store.notifier.side_effect = inner
        self.fail(self.op())
        await self.store._alerts()
        self.assertEqual(self.store.log_queue.qsize(), 0)

    def test_retention_does_not_mark_open_incidents_resolved(self):
        self.fail(self.op())
        self.store.incidents.prune(100000)
        self.assertEqual(self.one()['state'], 'open')

    async def test_real_handler_registration_is_removed_on_close(self):
        await self.store.start()
        logger = logging.getLogger('animedl')
        self.assertIn(self.store.log_handler, logger.handlers)
        await self.store.close()
        self.assertNotIn(self.store.log_handler, logger.handlers)
        self.store = audit.AuditStore(self.path)

    async def test_real_session_success_proves_only_matching_chat_access(self):
        from aiogram.exceptions import TelegramForbiddenError
        from aiogram.methods import GetMe
        from animedl.telegram_client import build_bot
        bot=build_bot(SimpleNamespace(tg_api_base_url='http://localhost:8081',tg_api_local_mode=True,bot_token='123456:TEST'))
        previous=audit._current;audit._current=self.store
        method=SendMessage(chat_id=1,text='test')
        error=TelegramForbiddenError(method=method,message='bot was blocked by the user SECRET')
        bot.session.make_request=AsyncMock(side_effect=error)
        try:
            with self.assertRaises(TelegramForbiddenError):
                try:
                    await bot(method)
                except TelegramForbiddenError as exc:
                    audit.capture(exc,'telegram_error',1)
                    raise
            bot.session.make_request.side_effect=None
            bot.session.make_request.return_value=SimpleNamespace(message_id=1)
            await bot(GetMe())
            await bot.send_message(2,'other chat')
            self.assertEqual(self.one()['state'],'open')
            await bot(method)
            self.assertEqual(self.one()['state'],'resolved')
        finally:
            audit._current=previous
            await bot.session.close()

    async def test_jellyfin_recovery_needs_actual_refresh_acceptance(self):
        from unittest.mock import Mock,patch
        import httpx
        from animedl.jellyfin import refresh_library
        cfg=SimpleNamespace(jellyfin_url='http://test.invalid',jellyfin_api_key='SECRET')
        client=SimpleNamespace(post=AsyncMock(side_effect=httpx.ConnectError('SECRET')))
        context=AsyncMock();context.__aenter__.return_value=client
        previous=audit._current;audit._current=self.store
        try:
            with patch('animedl.jellyfin.httpx.AsyncClient',return_value=context):
                with self.assertRaises(httpx.ConnectError):await refresh_library(cfg)
                self.assertEqual(self.one()['state'],'open')
                client.post.side_effect=None
                client.post.return_value=SimpleNamespace(raise_for_status=Mock())
                await refresh_library(cfg)
            self.assertEqual(self.one()['state'],'resolved')
            self.assertNotIn('SECRET',json.dumps(self.one()))
            self.assertNotIn('test.invalid',json.dumps(self.one()))
        finally:audit._current=previous

    async def test_quality_menu_uses_real_episode_season_not_selection_length(self):
        from animedl.handlers.flow import _quality_proof
        data={'url':'https://example.test/show','voice':'JAM','ep_numbers':[1,2],
              'episodes':[{'number':1,'season':1,'db_id':'one'}]}
        state=SimpleNamespace(get_data=AsyncMock(side_effect=lambda:dict(data)))
        first=await _quality_proof(state)
        data['ep_numbers']=[1]
        self.assertEqual(first['operation_key'],(await _quality_proof(state))['operation_key'])
        data['episodes']=[{'number':1,'season':2,'db_id':'two'}]
        self.assertNotEqual(first['operation_key'],(await _quality_proof(state))['operation_key'])

    async def test_owner_reply_can_recover_transport_budget_but_not_bypass_429(self):
        self.fail(self.op())
        self.sender.side_effect=TimeoutError()
        for now in (100,200,400):await self.notify(now)
        self.store.incidents.owner_reply_confirmed(now=500)
        self.sender.side_effect=None
        await self.notify(529)
        self.assertEqual(self.sender.await_count,3)
        await self.notify(531)
        self.assertEqual(self.sender.await_count,4)
        self.store.db.execute('UPDATE audit_notification_state SET next_api=900,failures=0 WHERE id=1')
        self.store.incidents.owner_reply_confirmed(now=600)
        self.assertEqual(self.store.db.execute('SELECT next_api FROM audit_notification_state').fetchone()[0],900)

    async def test_eviction_and_recreation_during_notification_does_not_ack_new_incident(self):
        self.store.incidents.limit=10
        op=self.op()
        self.fail(op)
        identity=self.one()['id']
        async def send(text):
            for i in range(10):self.fail(self.op(resource=str(i)),at=30+i)
            self.fail(op,at=50)
            return SimpleNamespace(message_id=71)
        self.sender.side_effect=send
        await self.notify(100)
        revision,notified=self.store.db.execute('SELECT revision,notified FROM audit_incidents WHERE id=?',(identity,)).fetchone()
        self.assertGreater(revision,notified)


if __name__ == '__main__':
    unittest.main()
