import tempfile
import unittest
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey

from animedl.catalog import CatalogHit, CatalogStore
from animedl.handlers import search


class SearchFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.catalog = CatalogStore(Path(self.tmp.name) / "catalog.db")
        search._recent.clear()
        search._active.clear()
        search._selecting.clear()
        self.status = SimpleNamespace(edit_text=AsyncMock(), message_id=42)
        self.message = SimpleNamespace(text="Блич", from_user=SimpleNamespace(id=123),
            chat=SimpleNamespace(id=123), answer=AsyncMock(return_value=self.status),
            bot=SimpleNamespace(edit_message_text=AsyncMock(return_value=self.status)))
        self.state = FSMContext(MemoryStorage(), StorageKey(bot_id=1,chat_id=123,user_id=123))

    def tearDown(self):
        self.catalog.close()
        self.tmp.cleanup()

    async def test_failed_network_still_returns_cached_hits_and_records_errors(self):
        self.catalog.remember(CatalogHit("yummy", "https://old.yummyani.me/catalog/item/bleach", "Блич"))
        with patch.object(search, "search_sources", AsyncMock(side_effect=TimeoutError)):
            await search.on_search(self.message, self.state, self.catalog)
        self.assertIn("Блич", self.status.edit_text.call_args.args[0])
        self.assertEqual(self.catalog.stats()["provider_errors"], 1)
        self.assertEqual(self.catalog.stats()["searches"], 1)

    async def test_active_flow_is_not_overwritten(self):
        await self.state.set_state("DownloadFlow:choosing_voice")
        with patch.object(search, "search_sources", AsyncMock()) as source:
            await search.on_search(self.message, self.state, self.catalog)
        source.assert_not_awaited()

    async def test_selection_is_owned_and_enters_existing_flow(self):
        token = self.catalog.store_results(123, [CatalogHit("yummy", "https://old.yummyani.me/catalog/item/bleach", "Блич")])
        cq = SimpleNamespace(data=f"find:{token}:0", from_user=SimpleNamespace(id=999),
            message=self.message, answer=AsyncMock())
        with patch.object(search, "open_title", AsyncMock()) as opener:
            await search.on_result(cq, self.state, self.catalog)
            opener.assert_not_awaited()
            cq.from_user.id = 123
            await search.on_result(cq, self.state, self.catalog)
            opener.assert_awaited_once_with(self.message, self.state,
                "https://old.yummyani.me/catalog/item/bleach", self.catalog, status=self.message)
        self.assertEqual(self.catalog.stats()["selections"], 1)

    async def test_stats_are_owner_only(self):
        cfg = SimpleNamespace(admin_id=456)
        await search.on_stats(self.message, cfg, self.catalog)
        self.message.answer.assert_not_awaited()

    async def test_rate_limit_does_not_start_second_search(self):
        response = SimpleNamespace(hits=[], errors={})
        with patch.object(search, "search_sources", AsyncMock(return_value=response)) as source:
            await search.on_search(self.message, self.state, self.catalog)
            await search.on_search(self.message, self.state, self.catalog)
        source.assert_awaited_once()
        self.assertEqual(self.catalog.stats()["no_results"], 1)

    async def setup_results(self, count=12):
        hits = [CatalogHit('yummy', f'https://example.test/{i}', f'Очень длинное название {i}') for i in range(count)]
        with patch.object(search, 'search_sources', AsyncMock(return_value=SimpleNamespace(hits=hits, errors={}))):
            await search.on_search(self.message, self.state, self.catalog)
        return (await self.state.get_data())['search_token']

    async def test_pages_use_numbers_only_and_edit_same_message(self):
        token = await self.setup_results()
        first_text = self.status.edit_text.call_args.args[0]
        self.assertIn('1/3', first_text)
        rows = self.status.edit_text.call_args.kwargs['reply_markup'].inline_keyboard
        self.assertEqual([b.text for b in rows[0]], ['1','2','3','4','5'])
        cq = SimpleNamespace(data=f'search:page:{token}:1',message=self.status,
                             from_user=self.message.from_user,answer=AsyncMock())
        self.status.chat = self.message.chat
        await search.on_search_navigation(cq,self.state,self.catalog)
        self.assertEqual(self.message.answer.await_count, 1)
        self.assertIn('2/3',self.status.edit_text.call_args.args[0])
        rows = self.status.edit_text.call_args.kwargs['reply_markup'].inline_keyboard
        self.assertEqual([b.text for b in rows[0]], ['6','7','8','9','10'])

    async def test_typed_absolute_number_opens_in_original_message(self):
        token = await self.setup_results()
        self.message.text = '9'
        with patch.object(search,'open_title',AsyncMock()) as opener:
            await search.on_result_number(self.message,self.state,self.catalog)
        self.assertEqual(opener.call_args.args[2],'https://example.test/8')
        self.assertIs(opener.call_args.kwargs['status'],self.status)
        self.assertEqual(self.message.bot.edit_message_text.call_args.kwargs['message_id'],42)

    async def test_back_restores_results_and_cancel_invalidates_buttons(self):
        token = await self.setup_results()
        self.status.chat=self.message.chat
        await self.state.set_state('DownloadFlow:choosing_voice')
        cq = SimpleNamespace(data=f'search:page:{token}:0',message=self.status,
                             from_user=self.message.from_user,answer=AsyncMock())
        await search.on_search_navigation(cq,self.state,self.catalog)
        self.assertEqual(await self.state.get_state(),search.RESULTS_STATE)
        cq.data=f'search:cancel:{token}'
        await search.on_search_navigation(cq,self.state,self.catalog)
        self.assertIsNone(await self.state.get_state())
        self.assertEqual(self.catalog.get_results(123,token),[])

    async def test_cancel_during_network_does_not_restore_results(self):
        entered=asyncio.Event();release=asyncio.Event()
        async def source(*args,**kwargs):
            entered.set();await release.wait()
            return SimpleNamespace(hits=[CatalogHit('x','https://example.test/a','Anime')],errors={})
        with patch.object(search,'search_sources',source):
            task=asyncio.create_task(search.on_search(self.message,self.state,self.catalog))
            await entered.wait();await self.state.clear();release.set();await task
        self.assertIsNone(await self.state.get_state())
        self.status.edit_text.assert_not_awaited()

    async def test_duplicate_selection_starts_only_one_flow(self):
        token=await self.setup_results()
        entered=asyncio.Event();release=asyncio.Event()
        async def open_it(*args,**kwargs):entered.set();await release.wait()
        with patch.object(search,'open_title',AsyncMock(side_effect=open_it)) as opener:
            task=asyncio.create_task(search._open_result(123,token,0,self.message,self.state,self.catalog,target=self.status))
            await entered.wait()
            await search._open_result(123,token,0,self.message,self.state,self.catalog,target=self.status)
            release.set();await task
            opener.assert_awaited_once()

    async def test_numeric_input_after_session_expiry_does_not_open_anything(self):
        token=await self.setup_results()
        self.catalog.discard_results(123,token)
        self.message.text='1'
        with patch.object(search,'open_title',AsyncMock()) as opener:
            await search.on_result_number(self.message,self.state,self.catalog)
            opener.assert_not_awaited()
        self.assertIsNone(await self.state.get_state())

    async def test_slash_cancel_edits_search_message_and_discards_session(self):
        from animedl.handlers.common import cmd_cancel
        token=await self.setup_results()
        await cmd_cancel(self.message,self.state,self.catalog)
        self.assertEqual(self.message.answer.await_count,1)
        self.assertEqual(self.message.bot.edit_message_text.call_args.kwargs['message_id'],42)
        self.assertEqual(self.catalog.get_results(123,token),[])

    async def test_text_flow_steps_reuse_search_message(self):
        from animedl.handlers.flow import _flow_reply
        await self.setup_results()
        await _flow_reply(self.message,self.state,'Выбери качество')
        self.assertEqual(self.message.answer.await_count,1)
        self.assertEqual(self.message.bot.edit_message_text.call_args.kwargs['message_id'],42)

    async def test_different_result_does_not_inherit_library_folder_or_voice(self):
        token=await self.setup_results()
        await self.state.update_data(voice='Old voice',library_title='Wrong show',ep_numbers=[9])
        async def check(*args,**kwargs):
            data=await self.state.get_data()
            self.assertNotIn('library_title',data)
            self.assertNotIn('voice',data)
            self.assertNotIn('ep_numbers',data)
            self.assertEqual(data['search_message_id'],42)
        with patch.object(search,'open_title',AsyncMock(side_effect=check)):
            await search._open_result(123,token,1,self.message,self.state,self.catalog,target=self.status)

    async def test_repeated_invalid_number_does_not_raise_false_error(self):
        from aiogram.exceptions import TelegramBadRequest
        from aiogram.methods import EditMessageText
        await self.setup_results()
        self.message.text='999'
        self.message.bot.edit_message_text.side_effect=TelegramBadRequest(
            method=EditMessageText(chat_id=123,message_id=42,text='same'),message='Bad Request: message is not modified')
        await search.on_result_number(self.message,self.state,self.catalog)
        self.assertEqual(await self.state.get_state(),search.RESULTS_STATE)
