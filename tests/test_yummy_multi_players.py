import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from animedl.sites.yummyanime import YummyAnimeParser, _parse_api_players


def row(provider, voice, number, url):
    return {"data": {"player": "Плеер " + provider, "dubbing": voice},
            "number": str(number), "iframe_url": url}


class YummyMultiPlayerTests(unittest.IsolatedAsyncioTestCase):
    async def test_standalone_kodik_movie_retains_direct_fallback(self):
        parser = YummyAnimeParser()
        url = "https://old.yummyani.me/catalog/item/movie"
        film = "https://kodikplayer.com/video/123/abc123/720p?translation=10"
        serials, parser._api_voices[url] = _parse_api_players([
            row("Kodik", "Озвучка AniLibria", 1, film),
            row("Alloha", "Озвучка AniLibria", 1, "//alloha.yani.tv/?episode=1"),
            row("Kodik", "Озвучка AniLibria", 1, film),
        ])
        self.assertEqual(serials, [])
        parser._enumerate_voices = AsyncMock(return_value=("Movie", serials, ""))
        with patch("animedl.sites.yummyanime.new_client", return_value=AsyncMock()):
            title = await parser.fetch_title(url)
        self.assertEqual([e.number for e in title.episodes], [1])
        buttons = await parser.fetch_episode_players(title, title.episodes[0])
        self.assertEqual([(b.voice, b.provider) for b in buttons], [
            ("Озвучка AniLibria", "Kodik"), ("Озвучка AniLibria", "Alloha")])
        self.assertEqual(buttons[0].url, film)

    def test_preserves_per_episode_alloha_translation_and_voice(self):
        kodik, voices = _parse_api_players([
            row("Kodik", "Озвучка AniLibria", 1, "//kodikplayer.com/season/1/hash/720p?episode=1"),
            row("Alloha", "Озвучка AniLibria", 0, "//alloha.yani.tv/?episode=0"),
            row("Alloha", "Озвучка AniLibria", 1, "//alloha.yani.tv/?translation=10&episode=1"),
            row("Alloha", "Озвучка AniLibria", 12, "//alloha.yani.tv/?translation=340&episode=12"),
            row("Alloha", "Озвучка AniBaza", 1, "//alloha.yani.tv/?translation=271&episode=1"),
            row("CVH", "Озвучка AniLibria", 1, "//ru.yummyani.me/iframeCVH.html"),
            None, {"data": []}, row("Alloha", "bad", "unknown", "//alloha.yani.tv/"),
        ])
        self.assertEqual(kodik, [("Озвучка AniLibria", "https://kodikplayer.com/season/1/hash/720p")])
        self.assertEqual(len(voices), 2)
        self.assertEqual(sorted(voices[0].episodes), [1, 12])
        self.assertIn("translation=340", voices[0].embed_urls[12])
        self.assertEqual(voices[1].title, "Озвучка AniBaza")

    async def test_union_includes_alloha_only_episode_without_crossing_voices(self):
        parser = YummyAnimeParser()
        url = "https://old.yummyani.me/catalog/item/fixture"
        _, parser._api_voices[url] = _parse_api_players([
            row("Alloha", "A", 1, "//alloha.yani.tv/?episode=1"),
            row("Alloha", "A", 12, "//alloha.yani.tv/?episode=12"),
            row("Alloha", "B", 1, "//alloha.yani.tv/?translation=2&episode=1"),
        ])
        parser._enumerate_voices = AsyncMock(return_value=("Fixture", [("A", "kodik")], ""))
        client = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(
            text='<option value="1" data-id="111" data-hash="aaa">1</option>')))
        context = AsyncMock()
        context.__aenter__.return_value = client
        with patch("animedl.sites.yummyanime.new_client", return_value=context):
            title = await parser.fetch_title(url)
        self.assertEqual([e.number for e in title.episodes], [1, 12])
        first = await parser.fetch_episode_players(title, title.episodes[0])
        self.assertEqual([(b.voice, b.provider) for b in first], [("A", "Kodik"), ("A", "Alloha"), ("B", "Alloha")])
        last = await parser.fetch_episode_players(title, title.episodes[1])
        self.assertEqual([(b.voice, b.provider) for b in last], [("A", "Alloha")])

    async def test_alloha_only_title_without_kodik_is_supported(self):
        parser = YummyAnimeParser()
        url = "https://old.yummyani.me/catalog/item/fixture"
        _, parser._api_voices[url] = _parse_api_players([
            row("Alloha", "A", 1, "//alloha.yani.tv/?episode=1")])
        parser._enumerate_voices = AsyncMock(return_value=("Fixture", [], ""))
        context = AsyncMock()
        with patch("animedl.sites.yummyanime.new_client", return_value=context):
            title = await parser.fetch_title(url)
        self.assertEqual([e.number for e in title.episodes], [1])


if __name__ == "__main__":
    unittest.main()
