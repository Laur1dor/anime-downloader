import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from animedl.catalog import CatalogHit, CatalogStore, normalize_name
from animedl.models import TitleInfo


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "catalog.sqlite3"
        self.catalog = CatalogStore(self.path)

    def tearDown(self):
        self.catalog.close()
        self.tmp.cleanup()

    def test_aliases_and_normalization(self):
        title = TitleInfo("yummy", "https://example.org/a?utm_source=tg#player", "a", "Необъятный океан 2")
        self.catalog.remember(title, aliases=("Grand Blue Dreaming Season 2", "Ёж: сезон—2"))
        self.assertEqual(normalize_name(" ЁЖ: сезон—２ "), "еж сезон 2")
        self.assertEqual(self.catalog.search("ЕЖ сезон 2")[0].url, "https://example.org/a")
        self.assertEqual(self.catalog.search("grand blue")[0].title, title.title)
        self.assertEqual(self.catalog.search("!!!"), [])

    def test_similar_names_and_external_ids_never_merge_sources(self):
        for url, name in [("a", "Океан 1"), ("b", "Океан 2"), ("c", "Океан 2")]:
            self.catalog.remember(CatalogHit("yummy", "https://example.org/" + url, name, mal_id=12))
        self.assertEqual(len(self.catalog.search("океан")), 3)
        self.assertEqual(len(self.catalog.search("океан 2")), 2)
        self.assertEqual(len(self.catalog.search("океан 1")), 1)

    def test_remember_merges_aliases_only_for_exact_url(self):
        hit = CatalogHit("site", "https://example.org/a", "Русское название", ("English",))
        self.catalog.remember(hit)
        self.catalog.remember(CatalogHit("site", hit.url, "Русское название", ("日本語",)))
        self.assertEqual(self.catalog.search("English")[0].url, hit.url)
        self.assertEqual(self.catalog.search("日本語")[0].url, hit.url)

    def test_search_hits_do_not_erase_fetched_metadata(self):
        url = "https://example.org/a"
        self.catalog.remember(CatalogHit("site", url, "First name", ("Initial alias",)))
        self.catalog.remember(TitleInfo("site", url, "a", "Fetched name",
                                      mal_id=42, poster="https://example.org/poster.jpg"))
        self.catalog.remember(CatalogHit("site", url, "Search name", ("Fresh alias",)))
        result = self.catalog.search("Search name")[0]
        self.assertEqual(result.mal_id, 42)
        self.assertEqual(result.poster, "https://example.org/poster.jpg")
        for alias in ("First name", "Fetched name", "Initial alias", "Fresh alias"):
            self.assertEqual(self.catalog.search(alias)[0].url, url)

    def test_numeric_tokens_do_not_match_other_seasons(self):
        self.catalog.remember(CatalogHit("site", "https://example.org/3", "Океан сезон 3"))
        self.catalog.remember(CatalogHit("site", "https://example.org/13", "Океан сезон 13"))
        self.assertEqual([h.url for h in self.catalog.search("океан 3")], ["https://example.org/3"])

    def test_session_ownership_expiration_indices_and_persistence(self):
        hit = CatalogHit("site", "https://example.org/a", "Anime", ("Alias",))
        with patch("animedl.catalog.time.time", return_value=100):
            token = self.catalog.store_results(1, [hit])
        self.catalog.close()
        self.catalog = CatalogStore(self.path)
        with patch("animedl.catalog.time.time", return_value=101):
            self.assertEqual(self.catalog.get_result(1, token, 0), hit)
            self.assertIsNone(self.catalog.get_result(2, token, 0))
            self.assertIsNone(self.catalog.get_result(1, token, -1))
            self.assertIsNone(self.catalog.get_result(1, token, 1))
        with patch("animedl.catalog.time.time", return_value=1900):
            self.assertIsNone(self.catalog.get_result(1, token, 0))

    def test_stats_have_seven_day_window_and_no_raw_queries(self):
        with patch("animedl.catalog.time.time", return_value=100):
            self.catalog.event(99, "search")
        with patch("animedl.catalog.time.time", return_value=700000):
            self.catalog.event(1, "search", query="private query", token="secret")
            self.catalog.event(1, "selection")
            self.catalog.event(2, "search")
            self.catalog.event(2, "no_results")
            self.catalog.event(2, "provider_error")
            self.assertEqual(self.catalog.stats(), {"users": 2, "searches": 2, "selections": 1,
                "no_results": 1, "provider_errors": 1, "events": 5})
        self.assertNotIn(b"private query", self.path.read_bytes())
        self.assertNotIn(b"secret", self.path.read_bytes())

    def test_invalid_urls_are_not_retained(self):
        for url in ("file:///etc/passwd", "https://user:password@example.org/a", "https://example.org/\n"):
            with self.assertRaises(ValueError):
                self.catalog.remember(CatalogHit("site", url, "Title"))


if __name__ == "__main__":
    unittest.main()
