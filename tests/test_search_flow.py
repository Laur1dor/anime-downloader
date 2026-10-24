import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from animedl.catalog import CatalogHit, CatalogStore
from animedl.handlers import search


class SearchFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.catalog = CatalogStore(Path(self.tmp.name) / "catalog.db")
        search._recent.clear()
        search._active.clear()
        self.status = SimpleNamespace(edit_text=AsyncMock())
        self.message = SimpleNamespace(text="Блич", from_user=SimpleNamespace(id=123),
            chat=SimpleNamespace(id=123), answer=AsyncMock(return_value=self.status))
        self.state = SimpleNamespace(get_state=AsyncMock(return_value=None),
            set_state=AsyncMock(), clear=AsyncMock())

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
        self.state.get_state.return_value = "DownloadFlow:choosing_voice"
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
                "https://old.yummyani.me/catalog/item/bleach", self.catalog)
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
