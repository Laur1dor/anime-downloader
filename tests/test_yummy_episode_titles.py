import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from animedl.sites.yummyanime import YummyAnimeParser, _parse_episode_titles, _parse_episodes


class YummyEpisodeTitleTests(unittest.IsolatedAsyncioTestCase):
    def test_names_preserved_and_placeholder_ignored(self):
        markup = '''
        <option value="1" data-id="11" data-hash="a" data-title="1 серия">1 серия</option>
        <option value="2" data-id="12" data-hash="b" data-title="  Начало &amp; конец ">2 серия</option>
        <option value="3" data-id="13" data-hash="c" data-title="3 серия">Возвращение домой</option>
        <option value="4" data-id="14" data-hash="d" data-translation-type="voice" data-title="Озвучка">4</option>
        '''
        self.assertEqual(_parse_episode_titles(markup), {2: "Начало & конец", 3: "Возвращение домой"})
        self.assertEqual(_parse_episodes(markup), {1: ("11", "a"), 2: ("12", "b"), 3: ("13", "c")})

    async def test_named_voice_wins_over_other_voice_numbered_placeholder(self):
        parser = YummyAnimeParser()
        parser._enumerate_voices = AsyncMock(return_value=("Anime", [("A", "a"), ("B", "b")], ""))
        pages = {
            "a": '<option value="1" data-id="1" data-hash="a" data-title="1 серия">1 серия</option>',
            "b": '<option value="1" data-id="2" data-hash="b" data-title="Возвращение">1 серия</option>',
        }
        client = SimpleNamespace(get=AsyncMock(side_effect=lambda u: SimpleNamespace(text=pages[u])))
        context = AsyncMock()
        context.__aenter__.return_value = client
        with patch("animedl.sites.yummyanime.new_client", return_value=context):
            title = await parser.fetch_title("https://old.yummyani.me/catalog/item/anime")
        self.assertEqual(title.episodes[0].title, "Возвращение")


if __name__ == "__main__":
    unittest.main()
