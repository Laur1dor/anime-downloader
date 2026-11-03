import unittest
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from animedl.sites.yummyanime import YummyAnimeParser, _parse_episode_titles, _parse_episodes


class YummyEpisodeTitleTests(unittest.IsolatedAsyncioTestCase):
    def test_legacy_yummy_voice_cache_is_not_reused(self):
        from animedl.models import make_cache_hash
        for provider in ("auto", "Kodik", "Alloha"):
            raw = f"yummyanime|bleach|9|JAM|{provider}|720"
            self.assertNotEqual(make_cache_hash("yummyanime", "bleach", 9, "JAM", provider, 720),
                                hashlib.sha256(raw.encode()).hexdigest())
        raw = "animego|bleach|9|JAM|Kodik|720"
        self.assertEqual(make_cache_hash("animego", "bleach", 9, "JAM", "Kodik", 720),
                         hashlib.sha256(raw.encode()).hexdigest())

    async def test_missing_jam_episode_is_not_relabelled_from_amber(self):
        parser = YummyAnimeParser()
        parser._enumerate_voices = AsyncMock(return_value=("Anime", [("JAM", "jam"), ("Amber", "amber")], ""))
        pages = {
            "jam": '''<option value="8" data-id="8" data-hash="a" data-other-translation="false">8 серия</option>
                <option value="9" data-id="99" data-hash="b" data-other-translation="true"
                data-title="9 серия (Другой перевод: Amber)">9 серия</option>''',
            "amber": '<option value="9" data-id="99" data-hash="b" data-other-translation="false">9 серия</option>',
        }
        client = SimpleNamespace(get=AsyncMock(side_effect=lambda u: SimpleNamespace(text=pages[u])))
        context = AsyncMock()
        context.__aenter__.return_value = client
        with patch("animedl.sites.yummyanime.new_client", return_value=context):
            title = await parser.fetch_title("https://old.yummyani.me/catalog/item/anime")
        self.assertEqual([e.number for e in title.episodes], [8, 9])
        buttons = await parser.fetch_episode_players(title, title.episodes[1])
        self.assertEqual([(b.voice, b.provider) for b in buttons], [("Amber", "Kodik")])
        self.assertEqual(_parse_episode_titles(pages["jam"]), {})

    def test_fallback_label_without_flag_is_rejected_but_real_dub_is_kept(self):
        markup = '''<option value="9" data-id="99" data-hash="b" data-title="9 серия (Другой перевод: Amber)">9</option>
            <option value="10" data-id="10" data-hash="c" data-other-translation="false" data-title="Настоящее название">10</option>'''
        self.assertEqual(_parse_episodes(markup), {10: ("10", "c")})
        self.assertEqual(_parse_episode_titles(markup), {10: "Настоящее название"})

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
