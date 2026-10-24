import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from animedl.admin_ops import archive_to_library, category_ready, list_media_library, delete_media_title
from animedl.config import Config
from animedl.fsm.states import DownloadFlow
from animedl.handlers.flow import on_category, _title_from_state
from animedl.models import MEDIA_CATEGORIES, anime_category_from_html, validate_library_details
from animedl.services import validate_delivery


class CategoriesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = SimpleNamespace(media_root=self.root, library_dir=self.root / "Anime", admin_id=1)
        self.cfg.category_dir = Config.category_dir.__get__(self.cfg)
        (self.root / ".animedl-library").touch()
        for _, directory in MEDIA_CATEGORIES.values():
            folder = self.root / directory
            folder.mkdir()
            (folder / ".animedl-library").touch()
        self.src = self.root / "input.mp4"
        self.src.write_bytes(b"video")

    def state(self, data):
        async def update(**values):
            data.update(values)
        return SimpleNamespace(get_data=AsyncMock(side_effect=lambda: data),
            update_data=AsyncMock(side_effect=update), set_state=AsyncMock())

    def test_category_folders_and_anime_only_legacy_fallback(self):
        for category, (_, folder) in MEDIA_CATEGORIES.items():
            self.assertEqual(self.cfg.category_dir(category), self.root / folder)
        self.cfg.media_root = None
        self.assertEqual(self.cfg.category_dir("anime"), self.root / "Anime")
        self.assertIsNone(self.cfg.category_dir("series"))
        with self.assertRaises(ValueError):
            self.cfg.category_dir("../Movies")

    def test_root_and_category_mount_markers_fail_closed(self):
        validate_delivery(self.cfg, 1, "library", "series")
        (self.root / ".animedl-library").unlink()
        self.assertFalse(category_ready(self.cfg, "series"))
        with self.assertRaises(ValueError):
            validate_delivery(self.cfg, 1, "library", "series")
        (self.root / ".animedl-library").touch()
        (self.root / "Series" / ".animedl-library").unlink()
        self.assertFalse(category_ready(self.cfg, "series"))
        self.assertTrue(category_ready(self.cfg, "anime"))

    def test_every_category_remains_owner_only(self):
        for category in MEDIA_CATEGORIES:
            with self.assertRaises(PermissionError):
                validate_delivery(self.cfg, 2, "both", category)
        validate_delivery(self.cfg, 2, "telegram", "movies")

    def test_source_film_type_never_guessed_from_episode_count_or_plot(self):
        self.assertEqual(anime_category_from_html("<dl><dt>Тип:</dt><dd>Фильм</dd></dl>"), "anime_movies")
        self.assertEqual(anime_category_from_html("<li><span>Тип:</span><span>Полнометражный</span></li>"), "anime_movies")
        for markup in ("<p>Всего одна серия</p>", "<p>В описании есть слова тип: фильм</p>",
                       "<li>Тип: OVA</li>", "<li>Тип: TV</li>"):
            self.assertEqual(anime_category_from_html(markup), "anime")

    def test_movie_layout_and_clean_metadata_without_season(self):
        for category in ("movies", "anime_movies"):
            directory = self.cfg.category_dir(category)
            video = archive_to_library(self.src, directory, "A Movie", 1, media_tag=category,
                identity="movie", source_identity="test", voice="Voice", quality=720)
            self.assertEqual(video.parent.parent, directory)
            self.assertFalse(any(p.name.startswith("Season") for p in video.parent.iterdir()))
            self.assertNotIn("S01E01", video.name)
            nfo = ET.parse(video.parent / "movie.nfo").getroot()
            self.assertEqual((nfo.tag, nfo.findtext("title"), nfo.findtext("tag")), ("movie", "A Movie", category))
            self.assertIsNone(nfo.find("season"))
            self.assertFalse((video.parent / "tvshow.nfo").exists())
            validate_library_details("A Movie", None, category=category)

    def test_serial_layout_and_metadata_routed_separately(self):
        for category in ("anime", "series"):
            directory = self.cfg.category_dir(category)
            video = archive_to_library(self.src, directory, "Same title", 1, season=2, media_tag=category)
            self.assertEqual(video.parent.name, "Season 02")
            self.assertEqual(video.parent.parent.parent, directory)
            self.assertEqual(ET.parse(video.parent.parent / "tvshow.nfo").getroot().findtext("tag"), category)

    def test_admin_list_and_delete_all_categories_without_cross_category_delete(self):
        for category in MEDIA_CATEGORIES:
            archive_to_library(self.src, self.cfg.category_dir(category), "Same title", 1, season=1, media_tag=category)
        items = list_media_library(self.cfg)
        self.assertEqual(len(items), 4)
        series = next(name for name, _, _ in items if name.startswith("series/"))
        self.assertTrue(delete_media_title(self.cfg, series))
        self.assertEqual(len(list_media_library(self.cfg)), 3)
        self.assertFalse(delete_media_title(self.cfg, "movies/../Anime"))
        self.assertFalse(delete_media_title(self.cfg, "unknown/title"))
        self.assertTrue((self.root / "Anime").exists())

    async def test_category_override_to_movie_bypasses_season_prompt(self):
        data = dict(title="Film", delivery="library", ep_numbers=[1], media_category="anime",
            quality=720, voice="Voice", provider="Player", owner_flow=True)
        state = self.state(data)
        cq = SimpleNamespace(data="category:anime_movies", from_user=SimpleNamespace(id=1),
            answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()))
        await on_category(cq, state, self.cfg)
        self.assertEqual(data["library_category"], "anime_movies")
        self.assertIsNone(data["library_season"])
        state.set_state.assert_awaited_with(DownloadFlow.confirm)
        self.assertNotIn("сезон", cq.message.edit_text.call_args.args[0])

    async def test_non_owner_category_callback_cannot_read_or_mutate_state(self):
        state = self.state({})
        cq = SimpleNamespace(data="category:series", from_user=SimpleNamespace(id=2), answer=AsyncMock())
        await on_category(cq, state, self.cfg)
        state.get_data.assert_not_awaited()
        state.update_data.assert_not_awaited()

    async def test_serial_batch_cannot_be_archived_as_movie(self):
        state = self.state(dict(delivery="library", ep_numbers=[1, 2]))
        cq = SimpleNamespace(data="category:movies", from_user=SimpleNamespace(id=1), answer=AsyncMock())
        await on_category(cq, state, self.cfg)
        state.update_data.assert_not_awaited()
        self.assertTrue(cq.answer.call_args.kwargs["show_alert"])

    def test_state_preserves_source_category(self):
        title = _title_from_state(dict(site="sopranos", url="url", anime_id="id", title="Sopranos",
            episodes=[dict(number=1, db_id="ep", season=1)], media_category="series"))
        self.assertEqual(title.media_category, "series")
