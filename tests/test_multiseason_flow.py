import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from animedl.fsm.states import DownloadFlow
from animedl.handlers.flow import on_season, _ask_library_details, _run_season, _title_from_state
from animedl.keyboards import episodes_kb
from animedl.models import EpisodeRef, PlayerButton, TitleInfo
from animedl.services import EpisodeResult


class MultiSeasonFlowTests(unittest.IsolatedAsyncioTestCase):
    def state(self, data):
        async def update(**values):
            data.update(values)
        return SimpleNamespace(get_data=AsyncMock(side_effect=lambda: data),
            update_data=AsyncMock(side_effect=update), set_state=AsyncMock())

    def data(self):
        return dict(site="sopranos", url="https://example.test/episodes/2-sezon-1-episode/",
            anime_id="sopranos", title="Клан Сопрано", season=2, seasons=[1, 2],
            preferred_voice="Гоблин", all_episodes=[
                dict(number=1, db_id="s1e1", season=1),
                dict(number=2, db_id="s1e2", season=1),
                dict(number=1, db_id="s2e1", season=2)])

    async def test_season_choice_filters_episodes_and_marks_goblin_default(self):
        data = self.data()
        cq = SimpleNamespace(data="season:2", answer=AsyncMock(),
            message=SimpleNamespace(edit_text=AsyncMock()))
        parser = SimpleNamespace(fetch_episode_players=AsyncMock(return_value=[
            PlayerButton("Украинский", "Player", "uk"), PlayerButton("Гоблин", "Player", "goblin")]))
        with patch("animedl.handlers.flow.get_parser", return_value=parser):
            await on_season(cq, self.state(data))
        self.assertEqual([ep["db_id"] for ep in data["episodes"]], ["s2e1"])
        self.assertFalse(data["whole_show"])
        self.assertEqual(data["voices"], ["Гоблин", "Украинский"])
        self.assertEqual(parser.fetch_episode_players.call_args.args[1].season, 2)
        kb = cq.message.edit_text.call_args.kwargs["reply_markup"]
        self.assertIn("по умолчанию", kb.inline_keyboard[0][0].text)

    async def test_all_seasons_keeps_duplicate_episode_numbers_and_source_seasons(self):
        data = self.data()
        cq = SimpleNamespace(data="season:all", answer=AsyncMock(),
            message=SimpleNamespace(edit_text=AsyncMock()))
        parser = SimpleNamespace(fetch_episode_players=AsyncMock(return_value=[PlayerButton("Гоблин", "Player", "url")]))
        with patch("animedl.handlers.flow.get_parser", return_value=parser):
            await on_season(cq, self.state(data))
        self.assertTrue(data["whole_show"])
        title = _title_from_state(data)
        self.assertEqual([(e.season, e.number) for e in title.episodes], [(1, 1), (1, 2), (2, 1)])
        self.assertEqual(title.preferred_voice, "Гоблин")
        callbacks = [b.callback_data for row in episodes_kb(3, whole_show=True).inline_keyboard for b in row]
        self.assertIn("ep:all", callbacks)
        self.assertNotIn("ep:range", callbacks)

    async def test_invalid_season_does_not_change_selection(self):
        data = self.data()
        state = self.state(data)
        cq = SimpleNamespace(data="season:99", answer=AsyncMock())
        await on_season(cq, state)
        state.update_data.assert_not_awaited()

    async def test_whole_show_library_uses_source_seasons_without_manual_prompt(self):
        data = self.data() | dict(season=None, whole_show=True, quality=720, ep_numbers=[1, 2, 3],
            delivery="library", voice="Гоблин", provider="Player", owner_flow=True)
        state = self.state(data)
        cq = SimpleNamespace(message=SimpleNamespace(edit_text=AsyncMock()))
        await _ask_library_details(cq, state)
        self.assertEqual(data["library_title"], "Клан Сопрано")
        self.assertIsNone(data["library_season"])
        state.set_state.assert_awaited_with(DownloadFlow.confirm)
        self.assertIn("Весь сериал: 3 серий, 2 сезонов", cq.message.edit_text.call_args.args[0])

    async def test_full_show_processes_each_episode_in_order_without_number_collision(self):
        title = TitleInfo("sopranos", "url", "sopranos", "Клан Сопрано", episodes=[
            EpisodeRef(1, "s1e1", season=1), EpisodeRef(2, "s1e2", season=1), EpisodeRef(1, "s2e1", season=2)])
        ctrl = SimpleNamespace(is_paused=False, cancelled=False)
        status = SimpleNamespace(edit_text=AsyncMock())
        async def process(**kwargs):
            ep = kwargs["episode"]
            return EpisodeResult(ep.number, "sent_new", library="saved", season=ep.season)
        with patch("animedl.handlers.flow.process_episode", AsyncMock(side_effect=process)) as processor:
            await _run_season(None, None, None, None, title,
                dict(enumerate(title.episodes, 1)), [1, 2, 3], "Гоблин", "Player", 720,
                ctrl, 1, status, AsyncMock(), delivery="library", library_title=title.title)
        self.assertEqual([(call.kwargs["episode"].season, call.kwargs["episode"].number)
            for call in processor.call_args_list], [(1, 1), (1, 2), (2, 1)])
        self.assertIn("3/3", status.edit_text.call_args.args[0])

    async def test_full_show_summary_fits_telegram_message_limit(self):
        title = TitleInfo("sopranos", "url", "sopranos", "Клан Сопрано")
        episodes = {i: EpisodeRef(i, str(i), season=1) for i in range(1, 87)}
        ctrl = SimpleNamespace(is_paused=False, cancelled=False)
        status = SimpleNamespace(edit_text=AsyncMock())
        async def process(**kwargs):
            ep = kwargs["episode"]
            return EpisodeResult(ep.number, "sent_new", note="Jellyfin: обновление библиотеки запрошено", library="saved", season=1)
        with patch("animedl.handlers.flow.process_episode", AsyncMock(side_effect=process)):
            await _run_season(None, None, None, None, title, episodes, list(episodes),
                "Гоблин", "Player", 720, ctrl, 1, status, AsyncMock(), delivery="library")
        summary = status.edit_text.call_args.args[0]
        self.assertLessEqual(len(summary), 4096)
        self.assertIn("86/86", summary)
        self.assertIn("Ещё", summary)


if __name__ == "__main__":
    unittest.main()
