import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from animedl.models import EpisodeRef, TitleInfo
from animedl.sites.base import ParseError
from animedl.sites.sopranos import SopranosParser, parse_episodes, parse_season_links
from animedl.players.plplayer import PLPlayerExtractor
from animedl.players.base import ExtractError


ROOT = "https://watch-the-sopranos-online.org"
FIXTURES = Path(__file__).parent / "fixtures"


class SopranosTests(unittest.IsolatedAsyncioTestCase):
    def test_actual_navigation_preserves_irregular_slugs(self):
        html = (FIXTURES / "sopranos-episode6x12.html").read_text(encoding="utf-8")
        links = parse_season_links(html, ROOT)
        self.assertEqual(list(links), [1, 2, 3, 4, 5, 6])
        self.assertEqual(links[2], ROOT + "/seasons/2-season-1/")
        self.assertEqual(links[6], ROOT + "/seasons/6-season1/")

    def test_actual_episode_cards_have_local_numbers_and_seasons(self):
        for fixture, season, count in [("sopranos-season1.html", 1, 13), ("sopranos-episode6x12.html", 6, 21)]:
            episodes = parse_episodes((FIXTURES / fixture).read_text(encoding="utf-8"), ROOT, season=season)
            self.assertEqual([ep.number for ep in episodes], list(range(1, count + 1)))
            self.assertEqual({ep.season for ep in episodes}, {season})
            self.assertEqual(episodes[-1].db_id, f"{ROOT}/episodes/{season}-sezon-{count}-episode/")

    def test_actual_cards_use_named_title_instead_of_image_number_label(self):
        html = (FIXTURES / "sopranos-episode6x12.html").read_text(encoding="utf-8")
        episodes = parse_episodes(html, ROOT, season=6)
        self.assertEqual(episodes[0].title, "Только для членов клуба")
        self.assertEqual(episodes[11].title, "Каиша")

    def test_cards_do_not_include_external_or_different_season_links(self):
        html = '<ul class="episodios"><li><a href="https://evil.test/episodes/1-sezon-1-episode/">x</a></li><li><a href="/episodes/2-sezon-1-episode/">x</a></li><li><a href="/episodes/many-saints-of-newark/">film</a></li></ul>'
        self.assertEqual(parse_episodes(html, ROOT, season=1), [])

    async def test_denied_or_challenge_page_is_explicit_error(self):
        for page in [AsyncMock(side_effect=ParseError("403")), AsyncMock(return_value="<h1>Captcha</h1>")]:
            with patch("animedl.sites.sopranos.fetch_page", page):
                with self.assertRaises(ParseError):
                    await SopranosParser().fetch_title(ROOT + "/episodes/6-sezon-12-episode/")

    async def test_missing_real_player_never_returns_guessed_embed(self):
        html = (FIXTURES / "sopranos-episode6x12.html").read_text(encoding="utf-8")
        response = httpx.Response(200, text=html)
        with patch("animedl.sites.sopranos.new_client", return_value=httpx.AsyncClient(transport=httpx.MockTransport(lambda req: response))):
            with self.assertRaisesRegex(ParseError, "Гоблина"):
                await SopranosParser().fetch_episode_players(
                    TitleInfo("sopranos", ROOT, "the-sopranos", "Клан Сопрано"),
                    EpisodeRef(12, ROOT + "/episodes/6-sezon-12-episode/", season=6))

    async def test_actual_goblin_embed_and_ajax_options(self):
        html = (FIXTURES / "sopranos-player.html").read_text(encoding="utf-8")
        calls = []
        def respond(request):
            calls.append(request)
            if request.method == "POST":
                self.assertIn(b"action=doo_player_ajax", request.content)
                return httpx.Response(200, json={"embed_url": "https://alternative.example/player"})
            return httpx.Response(200, text=html)
        with patch("animedl.sites.sopranos.new_client", return_value=httpx.AsyncClient(transport=httpx.MockTransport(respond))):
            buttons = await SopranosParser().fetch_episode_players(
                TitleInfo("sopranos", ROOT, "the-sopranos", "Клан Сопрано"),
                EpisodeRef(12, ROOT + "/episodes/6-sezon-12-episode/", season=6))
        self.assertEqual([button.voice for button in buttons], ["Гоблин", "Украинский", "Плеер 2"])
        self.assertEqual(buttons[0].provider, "PLPlayer")
        self.assertIn("voice=89", buttons[0].url)
        self.assertEqual([r.method for r in calls], ["GET", "POST", "POST"])

    async def test_plplayer_qualities_and_wrong_voice_guard(self):
        html = (FIXTURES / "plplayer.html").read_text(encoding="utf-8")
        master = "#EXTM3U\n#EXT-X-STREAM-INF:RESOLUTION=1920x1080\n1080.m3u8\n#EXT-X-STREAM-INF:RESOLUTION=640x360\n360.m3u8"
        def client():
            return httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=html if req.url.host == "plplayer.online" else master)))
        with patch("animedl.players.plplayer.new_client", side_effect=lambda **kwargs: client()):
            variants = await PLPlayerExtractor().extract("https://plplayer.online/s/636?season=6&episode=12&voice=89")
            self.assertEqual([v.quality for v in variants], [1080, 360])
            self.assertTrue(all(v.kind == "ytdlp" and v.direct and v.headers["Referer"] == "https://plplayer.online/" for v in variants))
            with self.assertRaises(ExtractError):
                await PLPlayerExtractor().extract("https://plplayer.online/s/636?season=6&episode=12&voice=419")

    async def test_ajax_raw_html_player_response(self):
        html = (FIXTURES / "sopranos-player.html").read_text(encoding="utf-8")
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
        soup.select_one("#dooplay_player_response iframe").decompose()
        def respond(request):
            if request.method == "POST":
                return httpx.Response(200, headers={"content-type": "text/html"}, text='<iframe class="metaframe" src="https://plplayer.online/s/636?season=6&amp;episode=12&amp;voice=89" allowfullscreen></iframe>')
            return httpx.Response(200, text=str(soup))
        with patch("animedl.sites.sopranos.new_client", return_value=httpx.AsyncClient(transport=httpx.MockTransport(respond))):
            buttons = await SopranosParser().fetch_episode_players(
                TitleInfo("sopranos", ROOT, "the-sopranos", "Клан Сопрано"),
                EpisodeRef(12, ROOT + "/episodes/6-sezon-12-episode/", season=6))
        self.assertEqual(buttons[0].voice, "Гоблин")
        self.assertEqual(buttons[0].url, "https://plplayer.online/s/636?season=6&episode=12&voice=89")

    async def test_plplayer_does_not_treat_error_page_as_video(self):
        with patch("animedl.players.plplayer.new_client", return_value=httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, text="<h1>Forbidden</h1>")))):
            with self.assertRaises(ExtractError):
                await PLPlayerExtractor().extract("https://plplayer.online/s/636?season=6&episode=12&voice=89")
