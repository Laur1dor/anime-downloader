import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx

from animedl import episode_metadata as metadata


def response(entries, next_page=False):
    return Mock(json=Mock(return_value={"data": entries, "pagination": {"has_next_page": next_page}}))


class EpisodeMetadataTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        metadata._lock = asyncio.Lock()
        metadata._last_request = 0
        metadata._failures.clear()
        self.client = Mock(get=AsyncMock())
        self.context = AsyncMock()
        self.context.__aenter__.return_value = self.client
        self.factory = patch.object(metadata, "new_client", return_value=self.context)
        self.factory.start()
        self.sleep = patch.object(metadata.asyncio, "sleep", new=AsyncMock())
        self.sleep.start()

    def tearDown(self):
        self.factory.stop()
        self.sleep.stop()
        self.temp.cleanup()

    async def test_exact_id_pagination_and_reject_generated_titles(self):
        self.client.get.side_effect = [
            response([{"mal_id": 1, "title": "The &amp; Beginning"}, {"mal_id": 2, "title": "Episode 2"}], True),
            response([{"mal_id": 3, "title": "Home", "title_japanese": "帰宅"},
                      {"mal_id": 4, "title": None, "title_japanese": "名前"},
                      {"mal_id": True, "title": "Bad"}]),
        ]
        names = await metadata.get_episode_names(60636, self.root)
        self.assertEqual(names, {1: "The & Beginning", 3: "Home"})
        self.assertEqual(self.client.get.call_args_list[0].args, ("https://api.jikan.moe/v4/anime/60636/episodes",))
        self.assertEqual(self.client.get.call_args_list[1].kwargs["params"], {"page": 2})
        self.assertEqual(await metadata.get_episode_names(60636, self.root), names)
        self.assertEqual(self.client.get.call_count, 2)
        self.assertFalse(list(self.root.rglob("*.tmp")))

    async def test_invalid_ids_never_query_provider(self):
        for value in (0, -1, True, "60636", None):
            self.assertEqual(await metadata.get_episode_names(value, self.root), {})
        self.client.get.assert_not_called()

    async def test_provider_failure_preserves_expired_cache_and_throttles_retries(self):
        self.client.get.return_value = response([{"mal_id": 1, "title": "Beginning"}])
        await metadata.get_episode_names(62542, self.root)
        path = self.root / "episode-metadata/mal-62542.json"
        saved = json.loads(path.read_text())
        saved["expires_at"] = 0
        path.write_text(json.dumps(saved))
        self.client.get.side_effect = httpx.ReadTimeout("unavailable")
        self.assertEqual(await metadata.get_episode_names(62542, self.root), {1: "Beginning"})
        self.assertEqual(await metadata.get_episode_names(62542, self.root), {1: "Beginning"})
        self.assertEqual(self.client.get.call_count, 3)
        self.assertLess(json.loads(path.read_text())["expires_at"] - metadata.time.time(), 601)

    async def test_partial_failure_is_cached_briefly(self):
        self.client.get.side_effect = [response([{"mal_id": 1, "title": "One"}], True), httpx.ReadTimeout("offline")]
        self.assertEqual(await metadata.get_episode_names(123, self.root), {1: "One"})
        data = json.loads((self.root / "episode-metadata/mal-123.json").read_text())
        self.assertLess(data["expires_at"] - metadata.time.time(), 601)

    async def test_empty_result_negative_cache_and_id_isolation(self):
        self.client.get.return_value = response([])
        self.assertEqual(await metadata.get_episode_names(1, self.root), {})
        self.assertEqual(await metadata.get_episode_names(1, self.root), {})
        await metadata.get_episode_names(2, self.root)
        self.assertEqual(self.client.get.call_count, 4)

    async def test_wrong_id_and_corrupt_cache_do_not_supply_names(self):
        cache = self.root / "episode-metadata"
        cache.mkdir()
        (cache / "mal-1.json").write_text(json.dumps({"mal_id": 2, "names": {"1": "Wrong show"}, "expires_at": 99999999999}))
        self.client.get.return_value = response([])
        self.assertEqual(await metadata.get_episode_names(1, self.root), {})
        (cache / "mal-2.json").write_text("not json")
        self.assertEqual(await metadata.get_episode_names(2, self.root), {})

    async def test_pagination_limit_does_not_mark_partial_data_complete(self):
        self.client.get.return_value = response([{"mal_id": 1, "title": "One"}], True)
        self.assertEqual(await metadata.get_episode_names(5, self.root), {1: "One"})
        self.assertEqual(self.client.get.call_count, metadata._MAX_PAGES + 1)
        saved = json.loads((self.root / "episode-metadata/mal-5.json").read_text())
        self.assertLess(saved["expires_at"] - metadata.time.time(), 601)

    async def test_malformed_provider_response_is_optional(self):
        self.client.get.return_value = Mock(json=Mock(return_value={"data": None}))
        self.assertEqual(await metadata.get_episode_names(5, self.root), {})

    async def test_unwritable_cache_does_not_fail_valid_response(self):
        self.client.get.return_value = response([{"mal_id": 1, "title": "One"}])
        with patch.object(metadata.os, "replace", side_effect=OSError("disk unavailable")):
            self.assertEqual(await metadata.get_episode_names(5, self.root), {1: "One"})
        self.assertFalse(list(self.root.rglob("*.tmp")))

    async def test_mal_html_fallback_on_timeout(self):
        markup = '''<table><tr><td class="episode-number" data-raw="1">1</td>
        <td class="episode-title fs12"><a href="https://myanimelist.net/anime/60636/Bleach/episode/1">Black &amp; White</a>
        <span>Japanese title</span></td></tr></table>'''
        self.client.get.side_effect = [httpx.ReadTimeout("offline"), Mock(text=markup)]
        self.assertEqual(await metadata.get_episode_names(60636, self.root), {1: "Black & White"})
        self.assertEqual(self.client.get.call_args.args, ("https://myanimelist.net/anime/60636/_/episode",))
        self.assertEqual(await metadata.get_episode_names(60636, self.root), {1: "Black & White"})
        self.assertEqual(self.client.get.call_count, 2)

    def test_mal_html_requires_exact_id_and_consistent_number(self):
        markup = '''<table>
        <tr><td class="episode-number" data-raw="1">1</td><td class="episode-title fs12"><a href="/anime/2/Other/episode/1">Wrong entry</a></td></tr>
        <tr><td class="episode-number" data-raw="2">2</td><td class="episode-title fs12"><a href="/anime/1/Show/episode/3">Wrong number</a></td></tr>
        <tr><td class="episode-number" data-raw="3">3</td><td class="episode-title fs12"><a href="https://example.org/anime/1/Show/episode/3">Wrong host</a></td></tr>
        <tr><td class="episode-number" data-raw="4">4</td><td class="episode-title fs12"><a href="/anime/1/Show/episode/4">Episode 4</a></td></tr>
        </table><a href="/anime/1/Show/episode/5">Not episode table</a>'''
        self.assertEqual(metadata._parse_mal_page(markup, 1), {})

    async def test_antibot_page_does_not_supply_or_erase_stale_names(self):
        metadata._write_cache(self.root / "episode-metadata/mal-1.json", 1, {1: "Known title"}, -1)
        self.client.get.side_effect = [httpx.ReadTimeout("offline"), Mock(text='<h1>Verify you are human</h1>')]
        self.assertEqual(await metadata.get_episode_names(1, self.root), {1: "Known title"})


if __name__ == "__main__":
    unittest.main()
