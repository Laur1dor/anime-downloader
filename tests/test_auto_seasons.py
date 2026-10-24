import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from animedl.fsm.states import DownloadFlow
from animedl.handlers.flow import _ask_library_details, _title_from_state, on_library_details_edit, on_library_details_text
from animedl.keyboards import confirm_kb
from animedl.sites.yummyanime import YummyAnimeParser


class AutoSeasonTests(unittest.IsolatedAsyncioTestCase):
    def state(self, data):
        async def update(**values):
            data.update(values)
        return SimpleNamespace(get_data=AsyncMock(side_effect=lambda: data),
            update_data=AsyncMock(side_effect=update), set_state=AsyncMock())

    def data(self, **values):
        return dict(dict(title="Title", voice="Voice", provider="Kodik", quality=720,
            ep_numbers=[1], delivery="library", owner_flow=True), **values)

    def callback(self, user=1):
        return SimpleNamespace(from_user=SimpleNamespace(id=user), answer=AsyncMock(),
            message=SimpleNamespace(edit_text=AsyncMock()))

    def test_verified_alternative_season_groups_russian_numeric_title(self):
        markup = '<h1>Необъятный океан 3</h1><ul class="alt-names-list"><li>Grand Blue Dreaming Season 3</li><li>Grand Blue Season 3</li></ul>'
        self.assertEqual(YummyAnimeParser._source_season_details(markup, "Необъятный океан 3"), ("Необъятный океан", 3))

    def test_ambiguous_digits_and_unrelated_recommendations_are_not_seasons(self):
        for markup in ('<li>Recommended Season 3</li>', '<ul class="alt-names-list"><li>Grand Blue</li></ul>',
                       '<ul class="alt-names-list"><li>Season 2</li><li>Season 3</li></ul>'):
            self.assertEqual(YummyAnimeParser._source_season_details(markup, "86"), ("86", None))

    async def test_unknown_season_has_automatic_default_without_typing(self):
        data = self.data()
        state, cq = self.state(data), self.callback()
        await _ask_library_details(cq, state)
        self.assertEqual((data["library_title"], data["library_season"]), ("Title", 1))
        state.set_state.assert_awaited_with(DownloadFlow.confirm)

    async def test_source_metadata_automatically_selects_common_title_and_season(self):
        data = self.data(title="Необъятный океан 3", season=3, source_series_title="Необъятный океан")
        await _ask_library_details(self.callback(), self.state(data))
        self.assertEqual((data["library_title"], data["library_season"]), ("Необъятный океан", 3))

    async def test_named_arc_keeps_name_without_manual_entry(self):
        data = self.data(title="Блич — Бедствие")
        await _ask_library_details(self.callback(), self.state(data))
        self.assertEqual((data["library_title"], data["library_season"], data["library_season_name"]), ("Блич", None, "Бедствие"))

    async def test_edit_is_optional_and_owner_only(self):
        data = self.data(library_title="Title", library_season=1)
        state = self.state(data)
        await on_library_details_edit(self.callback(2), state, SimpleNamespace(admin_id=1))
        state.set_state.assert_not_awaited()
        cq = self.callback()
        await on_library_details_edit(cq, state, SimpleNamespace(admin_id=1))
        state.set_state.assert_awaited_with(DownloadFlow.choosing_library_details)
        self.assertIn("1 | Title", cq.message.edit_text.call_args.args[0])
        self.assertFalse(any(b.callback_data == "library:details:edit" for row in confirm_kb().inline_keyboard for b in row))

    async def test_reversed_numeric_edit_cannot_create_show_named_three(self):
        data = self.data(title="Необъятный океан 3")
        message = SimpleNamespace(text="Необъятный океан | 3", from_user=SimpleNamespace(id=1),
            answer=AsyncMock(return_value=SimpleNamespace(edit_text=AsyncMock())))
        await on_library_details_text(message, self.state(data), SimpleNamespace(admin_id=1))
        self.assertEqual((data["library_title"], data["library_season"]), ("Необъятный океан", 3))

    def test_source_common_title_survives_state_round_trip(self):
        data = dict(site="yummyanime", url="url", anime_id="id", title="Необъятный океан 3", episodes=[],
            season=3, source_series_title="Необъятный океан")
        self.assertEqual(_title_from_state(data).library_series_title, "Необъятный океан")

    async def test_fixed_source_season_cannot_be_changed_by_optional_edit(self):
        data = self.data(season=6, seasons=[1, 2, 3, 4, 5, 6])
        state = self.state(data)
        message = SimpleNamespace(text="3 | Клан Сопрано", from_user=SimpleNamespace(id=1), answer=AsyncMock())
        await on_library_details_text(message, state, SimpleNamespace(admin_id=1))
        state.update_data.assert_not_awaited()
        self.assertIn("Номер сезона берётся из источника", message.answer.call_args.args[0])

    async def test_optional_movie_edit_changes_only_title(self):
        data = self.data(library_category="anime_movies")
        message = SimpleNamespace(text="Новый фильм", from_user=SimpleNamespace(id=1),
            answer=AsyncMock(return_value=SimpleNamespace(edit_text=AsyncMock())))
        await on_library_details_text(message, self.state(data), SimpleNamespace(admin_id=1))
        self.assertEqual(data["library_title"], "Новый фильм")
        self.assertIsNone(data["library_season"])
