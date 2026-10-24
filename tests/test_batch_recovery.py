import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from animedl.handlers.flow import _run_season
from animedl.models import EpisodeRef, TitleInfo
from animedl.services import EpisodeResult


class BatchRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def run_batch(self, process, *, ctrl=None, episodes=None, nums=None, delivery="library"):
        episodes = episodes or {1: EpisodeRef(1, "s1e1", season=1),
                                2: EpisodeRef(1, "s2e1", season=2)}
        ctrl = ctrl or SimpleNamespace(is_paused=False, cancelled=False)
        status = SimpleNamespace(edit_text=AsyncMock())
        progress = AsyncMock()
        with patch("animedl.handlers.flow.process_episode", AsyncMock(side_effect=process)) as processor:
            await _run_season(None, None, None, None, TitleInfo("site", "url", "id", "Title"),
                              episodes, nums or list(episodes), "Voice", "Kodik", 1080,
                              ctrl, 1, status, progress, user_id=42, delivery=delivery,
                              library_title="Title", library_category="series")
        return processor, status.edit_text.call_args.args[0], progress

    async def test_only_failed_source_retried_after_others_with_same_selection(self):
        calls = []
        async def process(**kw):
            ep = kw["episode"]
            calls.append(ep.db_id)
            if len(calls) == 1:
                return EpisodeResult(ep.number, "error", source_failed=True, library="error", season=ep.season)
            return EpisodeResult(ep.number, "sent_new", library="saved", season=ep.season)
        processor, summary, _ = await self.run_batch(process)
        self.assertEqual(calls, ["s1e1", "s2e1", "s1e1"])
        self.assertIn("2 сохранено, 0 ошибок", summary)
        self.assertIn("✅ Готово", summary)
        for call in processor.call_args_list:
            self.assertEqual((call.kwargs["voice"], call.kwargs["provider"], call.kwargs["quality"],
                              call.kwargs["library_category"]), ("Voice", "Kodik", 1080, "series"))

    async def test_retry_is_bounded_and_keeps_real_season_number(self):
        async def process(**kw):
            ep = kw["episode"]
            return EpisodeResult(ep.number, "error", source_failed=True, library="error",
                                 season=ep.season, note="Источник недоступен")
        processor, summary, _ = await self.run_batch(process)
        self.assertEqual(processor.await_count, 4)
        self.assertIn("0 сохранено, 2 ошибок", summary)
        self.assertIn("сезон 2, серия 1", summary)

    async def test_cached_telegram_not_resent_on_library_source_recovery(self):
        async def process(**kw):
            if kw["delivery"] == "both":
                return EpisodeResult(1, "partial", telegram="cached", library="error", source_failed=True)
            return EpisodeResult(1, "sent_new", library="saved")
        processor, summary, _ = await self.run_batch(process, delivery="both",
            episodes={1: EpisodeRef(1, "e1")})
        self.assertEqual([c.kwargs["delivery"] for c in processor.call_args_list], ["both", "library"])
        self.assertIn("0 новых, 1 из кэша, 0 ошибок", summary)
        self.assertIn("1 сохранено, 0 ошибок", summary)

    async def test_delivery_failure_does_not_repeat_successful_channel(self):
        async def process(**kw):
            return EpisodeResult(1, "partial", telegram="sent", library="error")
        processor, summary, _ = await self.run_batch(process, delivery="both",
            episodes={1: EpisodeRef(1, "e1")})
        self.assertEqual(processor.await_count, 1)
        self.assertIn("Завершено с ошибками", summary)

    async def test_cached_telegram_preserved_when_source_recovery_still_fails(self):
        async def process(**kw):
            return EpisodeResult(1, "partial" if kw["delivery"] == "both" else "error",
                telegram="cached" if kw["delivery"] == "both" else "skipped",
                library="error", source_failed=True, note="Источник недоступен")
        processor, summary, _ = await self.run_batch(process, delivery="both",
            episodes={1: EpisodeRef(1, "e1")})
        self.assertEqual(processor.await_count, 2)
        self.assertIn("0 новых, 1 из кэша, 0 ошибок", summary)
        self.assertIn("0 сохранено, 1 ошибок", summary)
        self.assertIn("Завершено с ошибками", summary)

    async def test_cancel_after_main_pass_prevents_recovery(self):
        ctrl = SimpleNamespace(is_paused=False, cancelled=False)
        async def process(**kw):
            ctrl.cancelled = True
            return EpisodeResult(1, "error", source_failed=True, library="error")
        processor, summary, _ = await self.run_batch(process, ctrl=ctrl)
        self.assertEqual(processor.await_count, 1)
        self.assertIn("Остановлено", summary)

    async def test_cancel_from_pause_during_recovery_prevents_new_download(self):
        ctrl = SimpleNamespace(is_paused=False, cancelled=False)
        async def process(**kw):
            ctrl.is_paused = True
            return EpisodeResult(1, "error", source_failed=True, library="error")
        async def stop():
            ctrl.cancelled = True
        ctrl.wait_if_paused = AsyncMock(side_effect=stop)
        processor, summary, _ = await self.run_batch(process, ctrl=ctrl,
            episodes={1: EpisodeRef(1, "e1")})
        self.assertEqual(processor.await_count, 1)
        ctrl.wait_if_paused.assert_awaited_once()
        self.assertIn("Остановлено", summary)

    async def test_missing_selected_episode_is_explicit_error(self):
        processor, summary, _ = await self.run_batch(AsyncMock(),
            episodes={1: EpisodeRef(1, "e1")}, nums=[3])
        processor.assert_not_awaited()
        self.assertIn("серия 3", summary)
        self.assertIn("отсутствует", summary)
        self.assertIn("Завершено с ошибками", summary)


if __name__ == "__main__":
    unittest.main()
