import asyncio
import errno
import tempfile
import unittest
import httpx
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from animedl.admin_ops import archive_to_library, list_library, resolve_library_season
from animedl.download.downloader import DownloadCancelled, DownloadError, DownloadResult
from animedl.models import EpisodeRef, PlayerButton, TitleInfo, VideoVariant, explicit_season, library_title_suggestion, library_selection_suggestion
from animedl.services import process_episode, _retryable_source_error
from animedl.sites.yummyanime import YummyAnimeParser
from animedl.config import Config
from animedl.handlers.flow import (on_category, on_delivery, on_quality, on_go, on_library_details_text,
    on_library_details_confirm, _title_from_state)


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tmp = self.root / "tmp"
        self.tmp.mkdir()
        self.library = self.root / "library"
        self.library.mkdir()
        (self.library / ".animedl-library").touch()
        self.cfg = SimpleNamespace(admin_id=1, library_dir=self.library,
            tmp_dir=self.tmp, max_tg_bytes=1000, max_tg_mb=1,
            jellyfin_url="", jellyfin_api_key="")
        self.cfg.media_root = None
        self.cfg.category_dir = Config.category_dir.__get__(self.cfg)
        self.cache = Mock()
        self.cache.get.return_value = None
        self.parser = SimpleNamespace(fetch_episode_players=AsyncMock(return_value=[
            PlayerButton("Voice", "Kodik", "https://example.test/embed")]))
        self.kwargs = dict(bot=Mock(), cfg=self.cfg, cache=self.cache,
            parser=self.parser, title=TitleInfo("animego", "url", "123", "Title"),
            episode=EpisodeRef(1, "ep"), voice="Voice", provider="Kodik",
            quality=720, on_status=AsyncMock(), chat_id=1, user_id=1,
            library_title="Title", library_season=1)
        self.paths = []
        async def download(variant, out_base, progress, cancel_check=None):
            path = out_base.with_suffix(".mp4")
            path.write_bytes(b"video")
            self.paths.append(path)
            return DownloadResult(path, 1280, 720, 10, 5)
        self.download = AsyncMock(side_effect=download)
        self.upload = AsyncMock(return_value=SimpleNamespace(video=SimpleNamespace(file_id="file")))
        self.cached = AsyncMock()
        self.archive = Mock(wraps=archive_to_library)
        self.thumbnail = AsyncMock(return_value=True)
        for name, value in (("download_variant", self.download), ("send_video_file", self.upload),
                            ("send_video_by_id", self.cached), ("archive_to_library", self.archive),
                            ("create_episode_thumbnail", self.thumbnail),
                            ("extract_variants", AsyncMock(return_value=[VideoVariant(720, "url")]))):
            patcher = patch("animedl.services." + name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def run_episode(self, mode="telegram", **changes):
        result = await process_episode(**(self.kwargs | changes), delivery=mode)
        self.assertEqual(list(self.tmp.iterdir()), [])
        return result

    async def test_telegram_only_never_writes_owner_library(self):
        result = await self.run_episode(user_id=2)
        self.assertEqual((result.telegram, result.library), ("sent", "skipped"))
        self.archive.assert_not_called()

    async def test_only_owner_uses_mounted_share_for_temporary_work(self):
        self.cfg.media_root = self.root / 'media'
        self.cfg.media_root.mkdir()
        (self.cfg.media_root / '.animedl-library').touch()
        await self.run_episode(user_id=2)
        self.assertFalse((self.cfg.media_root / '.animedl-work').exists())
        await self.run_episode(user_id=1)
        workspace = self.cfg.media_root / '.animedl-work'
        self.assertTrue(workspace.is_dir())
        self.assertEqual(list(workspace.iterdir()), [])
        self.assertTrue(self.paths[-1].is_relative_to(workspace))

    async def test_external_metadata_failure_does_not_block_archive(self):
        self.kwargs['title'].mal_id = 60636
        with patch('animedl.episode_metadata.get_episode_names', AsyncMock(side_effect=RuntimeError('metadata down'))):
            result = await self.run_episode('library')
        self.assertEqual(result.library, 'saved')

    async def test_source_episode_name_has_priority_over_external_catalogue(self):
        self.kwargs['title'].mal_id = 60636
        self.kwargs['episode'].title = 'Actual source title'
        with patch('animedl.episode_metadata.get_episode_names', AsyncMock()) as lookup:
            result = await self.run_episode('library')
        self.assertEqual(result.library, 'saved')
        lookup.assert_not_awaited()
        self.assertEqual(self.archive.call_args.kwargs['episode_title'], 'Actual source title')

    async def test_missing_voice_refreshes_partial_catalogue_before_download(self):
        self.parser.fetch_title = AsyncMock()
        self.parser.fetch_episode_players.side_effect = [[], [PlayerButton("Voice", "Kodik", "embed")]]
        result = await self.run_episode()
        self.assertEqual(result.telegram, "sent")
        self.parser.fetch_title.assert_awaited_once_with("url")
        self.assertEqual(self.download.await_count, 1)

    async def test_unavailable_voice_never_substitutes_another_dub(self):
        self.parser.fetch_title = AsyncMock()
        self.parser.fetch_episode_players.return_value = [PlayerButton("Other", "Kodik", "embed")]
        result = await self.run_episode()
        self.assertTrue(result.source_failed)
        self.download.assert_not_awaited()

    async def test_kept_better_library_video_does_not_regenerate_thumbnail(self):
        src = self.root / "existing.mp4"
        src.write_bytes(b"better")
        path = archive_to_library(src, self.library, "Title", 1, season=1,
                                  voice="Voice", quality=1080)
        with patch("animedl.services.refresh_library", AsyncMock()) as refresh:
            result = await self.run_episode("both")
        self.assertEqual(path.read_bytes(), b"better")
        self.assertIn("1080p", result.note)
        self.thumbnail.assert_not_awaited()
        refresh.assert_not_awaited()
        self.upload.assert_awaited_once()

    async def test_library_only_ignores_telegram_limit(self):
        self.cfg.max_tg_bytes = 1
        result = await self.run_episode("library")
        self.assertEqual((result.telegram, result.library), ("skipped", "saved"))
        self.upload.assert_not_awaited()
        self.cache.get.assert_not_called()
        self.assertEqual(len(list(self.library.rglob("*.mp4"))), 1)

    async def test_both_delivers_each_channel(self):
        result = await self.run_episode("both")
        self.assertEqual((result.status, result.telegram, result.library), ("sent_new", "sent", "saved"))
        self.cache.put.assert_called_once()

    async def test_numbered_source_seasons_have_distinct_archive_cache_and_captions(self):
        first = EpisodeRef(1, "season-1-episode-1", season=1)
        second = EpisodeRef(1, "season-2-episode-1", season=2)
        await self.run_episode("both", episode=first, library_season=None)
        await self.run_episode("both", episode=second, library_season=None)
        paths = sorted(self.library.rglob("*.mp4"))
        self.assertEqual([p.parent.name for p in paths], ["Season 01", "Season 02"])
        self.assertNotEqual(self.cache.put.call_args_list[0].args[0], self.cache.put.call_args_list[1].args[0])
        self.assertIn("сезон 1, серия 1", self.upload.call_args_list[0].kwargs["caption"])
        self.assertIn("сезон 2, серия 1", self.upload.call_args_list[1].kwargs["caption"])

    async def test_non_owner_cannot_request_library_or_both(self):
        for mode in ("library", "both"):
            with self.assertRaises(PermissionError):
                await self.run_episode(mode, user_id=2)
        self.download.assert_not_awaited()

    async def test_absent_library_rejected_before_download(self):
        self.cfg.library_dir = None
        with self.assertRaises(ValueError):
            await self.run_episode("library")
        self.download.assert_not_awaited()

    async def test_cached_telegram_does_not_skip_archive(self):
        self.cache.get.return_value = "cached"
        result = await self.run_episode("both")
        self.assertEqual((result.telegram, result.library), ("cached", "saved"))
        self.cached.assert_awaited_once()
        self.download.assert_awaited_once()
        self.archive.assert_called_once()
        self.upload.assert_not_awaited()

    async def test_telegram_cache_only_avoids_download(self):
        self.cache.get.return_value = "cached"
        result = await self.run_episode()
        self.assertEqual(result.status, "sent_cached")
        self.download.assert_not_awaited()

    async def test_upload_failure_does_not_undo_archive(self):
        self.upload.side_effect = RuntimeError("upload failed")
        result = await self.run_episode("both")
        self.assertEqual((result.status, result.telegram, result.library), ("partial", "error", "saved"))
        self.assertTrue(list(self.library.rglob("*.mp4")))

    async def test_library_failure_does_not_block_upload(self):
        self.archive.side_effect = OSError("disk full")
        result = await self.run_episode("both")
        self.assertEqual((result.status, result.telegram, result.library), ("partial", "sent", "error"))
        self.upload.assert_awaited_once()

    async def test_cache_failure_still_archives(self):
        self.cache.get.return_value = "cached"
        self.cached.side_effect = RuntimeError("telegram offline")
        result = await self.run_episode("both")
        self.assertEqual((result.status, result.telegram, result.library), ("sent_new", "sent", "saved"))
        self.upload.assert_awaited_once()

    async def test_source_failure_preserves_cached_telegram_success(self):
        self.cache.get.return_value = "cached"
        self.parser.fetch_episode_players.side_effect = RuntimeError("source offline")
        result = await self.run_episode("both")
        self.assertEqual((result.status, result.telegram, result.library), ("partial", "cached", "error"))

    async def test_cancel_cleans_partial_download(self):
        async def cancelled(variant, out_base, progress, cancel_check=None):
            out_base.with_suffix(".part").write_bytes(b"partial")
            raise DownloadCancelled()
        self.download.side_effect = cancelled
        result = await self.run_episode("both")
        self.assertEqual(result.status, "cancelled")
        self.archive.assert_not_called()
        self.upload.assert_not_awaited()

    async def test_transient_download_refreshes_same_source_and_cleans_partial(self):
        paths = []
        async def download(variant, out_base, progress, cancel_check=None):
            paths.append(out_base)
            if len(paths) == 1:
                out_base.with_suffix(".part").write_bytes(b"old partial")
                raise DownloadError("CERTIFICATE_VERIFY_FAILED: certificate has expired https://private.test/token")
            self.assertFalse(paths[0].parent.exists())
            self.assertEqual(variant.url, "fresh")
            path = out_base.with_suffix(".mp4")
            path.write_bytes(b"video")
            return DownloadResult(path, 1280, 720, 10, 5)
        self.download.side_effect = download
        variants = AsyncMock(side_effect=[[VideoVariant(720, "expired")], [VideoVariant(720, "fresh")]])
        with patch("animedl.services.extract_variants", variants), patch("animedl.services.asyncio.sleep", AsyncMock()):
            result = await self.run_episode("both")
        self.assertEqual(result.status, "sent_new")
        self.assertEqual(variants.await_args_list[0], variants.await_args_list[1])
        self.assertNotEqual(paths[0], paths[1])
        self.parser.fetch_episode_players.assert_awaited_once()
        self.upload.assert_awaited_once()

    async def test_exhausted_ssl_retry_reports_friendly_reason_without_url(self):
        self.download.side_effect = DownloadError("CERTIFICATE_VERIFY_FAILED: certificate has expired https://private.test/token")
        with patch("animedl.services.asyncio.sleep", AsyncMock()):
            result = await self.run_episode("both")
        self.assertEqual(self.download.await_count, 3)
        self.assertEqual(result.status, "error")
        self.assertIn("истёк SSL-сертификат", result.note)
        self.assertNotIn("https://", result.note)
        self.archive.assert_not_called()

    async def test_local_disk_failure_is_not_retried(self):
        self.download.side_effect = OSError(errno.ENOSPC, "No space left on device")
        result = await self.run_episode("both")
        self.assertEqual(result.status, "error")
        self.download.assert_awaited_once()

    async def test_invalid_media_is_not_retried(self):
        self.download.side_effect = ValueError("invalid media")
        result = await self.run_episode("both")
        self.assertEqual(result.status, "error")
        self.download.assert_awaited_once()

    async def test_cancel_after_transient_failure_does_not_retry(self):
        self.download.side_effect = DownloadError("Connection reset by peer")
        result = await self.run_episode("both", cancel_check=Mock(side_effect=[False, True]))
        self.assertEqual(result.status, "cancelled")
        self.download.assert_awaited_once()
        self.upload.assert_not_awaited()

    async def test_cancel_during_retry_wait_does_not_start_another_attempt(self):
        self.download.side_effect = DownloadError("Connection reset by peer")
        with patch("animedl.services.asyncio.sleep", AsyncMock()):
            result = await self.run_episode("both", cancel_check=Mock(side_effect=[False, False, True]))
        self.assertEqual(result.status, "cancelled")
        self.download.assert_awaited_once()

    async def test_transient_extraction_failure_also_refreshes_selected_source(self):
        variants = AsyncMock(side_effect=[TimeoutError("timed out"), [VideoVariant(720, "fresh")]])
        with patch("animedl.services.extract_variants", variants), patch("animedl.services.asyncio.sleep", AsyncMock()):
            result = await self.run_episode()
        self.assertEqual(result.status, "sent_new")
        self.assertEqual(variants.await_count, 2)
        self.download.assert_awaited_once()

    async def test_wrapped_disk_or_validation_error_is_not_transient(self):
        for cause in (OSError(errno.ENOSPC, "disk full"), ValueError("invalid media")):
            error = DownloadError("connection timeout while handling failed download")
            error.__cause__ = cause
            self.assertFalse(_retryable_source_error(error))

    async def test_http_retry_only_for_temporary_or_expired_link_responses(self):
        request = httpx.Request("GET", "https://example.test/video")
        for status, retry in ((401, True), (403, True), (408, True), (429, True), (503, True), (404, False)):
            error = httpx.HTTPStatusError("source error", request=request, response=httpx.Response(status, request=request))
            self.assertEqual(_retryable_source_error(error), retry)

    async def test_concurrent_jobs_use_distinct_temp_paths(self):
        await asyncio.gather(process_episode(**self.kwargs), process_episode(**self.kwargs))
        self.assertEqual(list(self.tmp.iterdir()), [])
        self.assertNotEqual(self.paths[0].parent, self.paths[1].parent)

    async def test_missing_mount_marker_rejected(self):
        (self.library / ".animedl-library").unlink()
        with self.assertRaises(ValueError):
            await self.run_episode("both")
        self.download.assert_not_awaited()

    async def test_cached_telegram_failure_falls_back_to_download(self):
        self.cache.get.return_value = "expired"
        self.cached.side_effect = RuntimeError("file id expired")
        result = await self.run_episode()
        self.assertEqual(result.telegram, "sent")
        self.download.assert_awaited_once()
        self.upload.assert_awaited_once()

    async def test_non_owner_ui_goes_directly_to_telegram_confirm(self):
        cq = SimpleNamespace(data="q:720", from_user=SimpleNamespace(id=2),
            answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()))
        data = dict(qualities=[720], ep_numbers=[1], title="Title", voice="Voice", provider="Kodik")
        async def update(**values):
            data.update(values)
        state = SimpleNamespace(get_data=AsyncMock(side_effect=lambda: data),
            update_data=AsyncMock(side_effect=update), set_state=AsyncMock())
        await on_quality(cq, state, self.cfg)
        self.assertEqual(data["delivery"], "telegram")
        text = cq.message.edit_text.call_args.args[0]
        self.assertNotIn("Доставка", text)
        self.assertNotIn("Медиатека", text)

    async def test_missing_storage_owner_ui_explains_unavailability(self):
        self.cfg.library_dir = None
        cq = SimpleNamespace(data="q:720", from_user=SimpleNamespace(id=1),
            answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()))
        state = SimpleNamespace(get_data=AsyncMock(return_value={"qualities": [720]}),
            update_data=AsyncMock(), set_state=AsyncMock())
        await on_quality(cq, state, self.cfg)
        self.assertIn("LIBRARY_DIR", cq.message.edit_text.call_args.args[0])

    async def test_forged_confirm_cannot_start_storage_job(self):
        cq = SimpleNamespace(from_user=SimpleNamespace(id=2), answer=AsyncMock())
        state = SimpleNamespace(get_data=AsyncMock(return_value={"delivery": "both"}),
            set_state=AsyncMock())
        await on_go(cq, state, Mock(), self.cfg, self.cache)
        state.set_state.assert_not_awaited()
        self.download.assert_not_awaited()

    async def test_mount_disappearing_during_download_reports_library_error(self):
        download = self.download.side_effect
        async def unmount(*args, **kwargs):
            result = await download(*args, **kwargs)
            (self.library / ".animedl-library").unlink()
            return result
        self.download.side_effect = unmount
        result = await self.run_episode("both")
        self.assertEqual((result.telegram, result.library), ("sent", "error"))
        self.assertFalse(list(self.library.rglob("*.mp4")))

    async def test_telegram_limit_in_both_preserves_library(self):
        self.cfg.max_tg_bytes = 1
        result = await self.run_episode("both")
        self.assertEqual((result.telegram, result.library, result.status), ("error", "saved", "partial"))
        self.upload.assert_not_awaited()

    async def test_cache_write_failure_does_not_hide_successful_upload(self):
        self.cache.put.side_effect = OSError("database full")
        result = await self.run_episode()
        self.assertEqual(result.telegram, "sent")

    async def test_jellyfin_refresh_success_after_archive(self):
        with patch("animedl.services.refresh_library", AsyncMock(return_value=True)) as refresh:
            result = await self.run_episode("both")
        refresh.assert_awaited_once_with(self.cfg)
        self.assertEqual((result.library, result.telegram), ("saved", "sent"))
        self.assertIn("Jellyfin", result.note)

    async def test_jellyfin_refresh_failure_preserves_both_deliveries(self):
        with patch("animedl.services.refresh_library", AsyncMock(side_effect=TimeoutError("secret"))):
            result = await self.run_episode("both")
        self.assertEqual((result.library, result.telegram, result.status), ("saved", "sent", "sent_new"))
        self.assertIn("Jellyfin", result.note)
        self.assertNotIn("secret", result.note)
        self.assertTrue(list(self.library.rglob("*.mp4")))

    async def test_jellyfin_not_called_without_successful_archive(self):
        with patch("animedl.services.refresh_library", AsyncMock()) as refresh:
            await self.run_episode()
            self.archive.side_effect = OSError("disk full")
            await self.run_episode("library")
        refresh.assert_not_awaited()

    async def test_missing_season_or_series_name_rejected_before_download(self):
        for values in ({"library_season": None}, {"library_season": 0}, {"library_title": ""}):
            with self.assertRaises(ValueError):
                await self.run_episode("library", **values)
        self.download.assert_not_awaited()

    async def test_owner_confirms_explicit_season_and_clean_series_name(self):
        cq = SimpleNamespace(data="delivery:library", from_user=SimpleNamespace(id=1),
            answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()))
        data = dict(title="One Room Season 2", season=2, quality=720, voice="Voice", provider="Kodik",
            ep_numbers=[1], owner_flow=True)
        async def update(**values):
            data.update(values)
        state = SimpleNamespace(get_data=AsyncMock(side_effect=lambda: data),
            update_data=AsyncMock(side_effect=update), set_state=AsyncMock())
        await on_delivery(cq, state, self.cfg)
        cq.data = "category:anime"
        await on_category(cq, state, self.cfg)
        self.assertEqual((data["library_title"], data["library_season"]), ("One Room", 2))
        self.assertIn("Проверь параметры", cq.message.edit_text.call_args.args[0])

    async def test_owner_can_set_season_and_grouped_name_manually(self):
        data = dict(title="One Room 3", quality=720, voice="Voice", provider="Kodik",
            ep_numbers=[1], delivery="library", owner_flow=True)
        async def update(**values):
            data.update(values)
        state = SimpleNamespace(get_data=AsyncMock(side_effect=lambda: data),
            update_data=AsyncMock(side_effect=update), set_state=AsyncMock())
        message = SimpleNamespace(text="3 | One Room", from_user=SimpleNamespace(id=1),
            answer=AsyncMock(return_value=SimpleNamespace(edit_text=AsyncMock())))
        await on_library_details_text(message, state, self.cfg)
        self.assertEqual((data["library_title"], data["library_season"]), ("One Room", 3))

    async def test_non_owner_cannot_confirm_or_edit_library_details(self):
        state = SimpleNamespace(update_data=AsyncMock(), get_data=AsyncMock())
        message = SimpleNamespace(text="3 | One Room", from_user=SimpleNamespace(id=2))
        await on_library_details_text(message, state, self.cfg)
        cq = SimpleNamespace(from_user=SimpleNamespace(id=2), answer=AsyncMock())
        await on_library_details_confirm(cq, state, self.cfg)
        state.update_data.assert_not_awaited()
        state.get_data.assert_not_awaited()

    async def test_different_source_seasons_share_confirmed_series_folder(self):
        await self.run_episode("library", library_title="One Room", library_season=2)
        other = TitleInfo("animego", "url3", "456", "One Room Season 3")
        await self.run_episode("library", library_title="One Room", library_season=3, title=other)
        shows = [p for p in self.library.iterdir() if p.is_dir()]
        self.assertEqual(len(shows), 1)
        self.assertTrue((shows[0] / "Season 02").is_dir())
        self.assertTrue((shows[0] / "Season 03").is_dir())

    async def test_source_poster_is_saved_to_series_folder_after_archive(self):
        title = TitleInfo("animego", "https://example.test/anime", "123", "Title", poster="https://example.test/poster.jpg")
        with patch("animedl.services.download_poster", AsyncMock(return_value=True)) as poster:
            result = await self.run_episode("library", title=title)
        video = next(self.library.rglob("*.mp4"))
        poster.assert_awaited_once_with(title.poster, video.parent.parent, referer=title.url, season_dir=video.parent)
        self.assertEqual(result.library, "saved")

    async def test_poster_failure_keeps_deliveries_successful(self):
        title = TitleInfo("animego", "url", "123", "Title", poster="https://example.test/poster.jpg")
        with patch("animedl.services.download_poster", AsyncMock(return_value=False)):
            result = await self.run_episode("both", title=title)
        self.assertEqual((result.status, result.library, result.telegram), ("sent_new", "saved", "sent"))
        self.assertIn("Постер", result.note)

    async def test_no_poster_download_for_empty_source_or_failed_archive(self):
        with patch("animedl.services.download_poster", AsyncMock()) as poster:
            await self.run_episode("library")
            self.archive.side_effect = OSError("disk full")
            title = TitleInfo("animego", "url", "123", "Title", poster="https://example.test/poster.jpg")
            await self.run_episode("library", title=title)
        poster.assert_not_awaited()

    async def test_episode_thumbnail_generated_before_jellyfin_refresh(self):
        events = []
        async def thumbnail(path, duration):
            self.assertTrue(path.is_file())
            self.assertEqual(duration, 10)
            events.append("thumbnail")
            return True
        async def refresh(cfg):
            events.append("refresh")
            return True
        self.thumbnail.side_effect = thumbnail
        with patch("animedl.services.refresh_library", AsyncMock(side_effect=refresh)):
            result = await self.run_episode("library")
        video = next(self.library.rglob("*.mp4"))
        self.thumbnail.assert_awaited_once_with(video, 10)
        self.assertEqual(events, ["thumbnail", "refresh"])
        self.assertEqual(result.library, "saved")

    async def test_episode_thumbnail_failure_keeps_delivery_success(self):
        self.thumbnail.return_value = False
        result = await self.run_episode("both")
        self.assertEqual((result.status, result.library, result.telegram), ("sent_new", "saved", "sent"))
        self.assertIn("Миниатюра", result.note)

    async def test_episode_thumbnail_skipped_for_telegram_or_failed_archive(self):
        await self.run_episode()
        self.archive.side_effect = OSError("disk full")
        await self.run_episode("library")
        self.thumbnail.assert_not_awaited()

    async def test_owner_named_season_input_shows_name_without_index(self):
        data = dict(title="Блич — Бедствие", quality=720, voice="Voice", provider="Kodik",
            ep_numbers=[1], delivery="library", owner_flow=True)
        async def update(**values):
            data.update(values)
        state = SimpleNamespace(get_data=AsyncMock(side_effect=lambda: data),
            update_data=AsyncMock(side_effect=update), set_state=AsyncMock())
        status = SimpleNamespace(edit_text=AsyncMock())
        message = SimpleNamespace(text="Бедствие | Блич", from_user=SimpleNamespace(id=1),
            answer=AsyncMock(return_value=status))
        await on_library_details_text(message, state, self.cfg)
        self.assertIsNone(data["library_season"])
        self.assertEqual(data["library_season_name"], "Бедствие")
        self.assertIn("сезон <b>Бедствие</b>", status.edit_text.call_args.args[0])
        self.assertNotIn("сезон <b>1</b>", status.edit_text.call_args.args[0])

    async def test_owner_named_season_proposal_requires_confirmation(self):
        cq = SimpleNamespace(data="delivery:library", from_user=SimpleNamespace(id=1),
            answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()))
        data = dict(title="Блич — Бедствие", season=None, quality=720, voice="Voice", provider="Kodik",
            ep_numbers=[1], owner_flow=True)
        async def update(**values):
            data.update(values)
        state = SimpleNamespace(get_data=AsyncMock(side_effect=lambda: data),
            update_data=AsyncMock(side_effect=update), set_state=AsyncMock())
        await on_delivery(cq, state, self.cfg)
        cq.data = "category:anime"
        await on_category(cq, state, self.cfg)
        self.assertEqual((data["library_title"], data["library_season_name"]), ("Блич", "Бедствие"))
        self.assertIsNone(data["library_season"])
        self.assertIn("Проверь параметры", cq.message.edit_text.call_args.args[0])

    async def test_owner_cannot_set_overlong_or_empty_named_season(self):
        for text in (" | Блич", "x" * 121 + " | Блич"):
            state = SimpleNamespace(update_data=AsyncMock(), get_data=AsyncMock(return_value={}))
            message = SimpleNamespace(text=text, from_user=SimpleNamespace(id=1), answer=AsyncMock())
            await on_library_details_text(message, state, self.cfg)
            state.update_data.assert_not_awaited()

    async def test_non_owner_cannot_set_named_season(self):
        state = SimpleNamespace(update_data=AsyncMock())
        message = SimpleNamespace(text="Бедствие | Блич", from_user=SimpleNamespace(id=2))
        await on_library_details_text(message, state, self.cfg)
        state.update_data.assert_not_awaited()
        with self.assertRaises(PermissionError):
            await self.run_episode("library", user_id=2, library_season=None, library_season_name="Бедствие")

    async def test_service_supports_named_season_and_clean_nfo_title(self):
        import xml.etree.ElementTree as ET
        result = await self.run_episode("library", library_season=None, library_season_name="Бедствие")
        self.assertEqual(result.library, "saved")
        nfo = next(self.library.rglob("season.nfo"))
        self.assertEqual(ET.parse(nfo).getroot().findtext("title"), "Бедствие")

    async def test_forged_delivery_callback_rejected(self):
        cq = SimpleNamespace(data="delivery:both", from_user=SimpleNamespace(id=2), answer=AsyncMock())
        state = SimpleNamespace(update_data=AsyncMock())
        await on_delivery(cq, state, self.cfg)
        state.update_data.assert_not_awaited()
        self.assertTrue(cq.answer.call_args.kwargs["show_alert"])

    async def test_sopranos_service_uses_series_folder_and_never_anime(self):
        from animedl.models import MEDIA_CATEGORIES
        self.cfg.media_root = self.root / "media"
        self.cfg.media_root.mkdir()
        (self.cfg.media_root / ".animedl-library").touch()
        for _, folder in MEDIA_CATEGORIES.values():
            directory = self.cfg.media_root / folder
            directory.mkdir()
            (directory / ".animedl-library").touch()
        title = TitleInfo("sopranos", "url", "sopranos", "Sopranos", media_category="series")
        result = await self.run_episode("library", title=title)
        self.assertEqual(result.library, "saved")
        self.assertEqual(len(list((self.cfg.media_root / "Series").rglob("*.mp4"))), 1)
        self.assertEqual(list((self.cfg.media_root / "Anime").rglob("*.mp4")), [])

    async def test_movie_service_stores_poster_at_movie_folder_without_season(self):
        self.cfg.media_root = self.root / "media"
        self.cfg.media_root.mkdir()
        (self.cfg.media_root / ".animedl-library").touch()
        films = self.cfg.media_root / "AnimeMovies"
        films.mkdir()
        (films / ".animedl-library").touch()
        title = TitleInfo("animego", "url", "123", "Film", media_category="anime_movies", poster="https://example.test/poster.jpg")
        with patch("animedl.services.download_poster", AsyncMock(return_value=True)) as poster:
            result = await self.run_episode("both", title=title, library_season=None)
        self.assertEqual(result.library, "saved")
        video = next(films.rglob("*.mp4"))
        self.assertEqual(video.parent.parent, films)
        poster.assert_awaited_once_with(title.poster, video.parent, referer=title.url, season_dir=None)
        self.assertTrue((video.parent / "movie.nfo").is_file())

    async def test_series_without_media_root_cannot_fall_back_to_anime(self):
        title = TitleInfo("sopranos", "url", "id", "Sopranos", media_category="series")
        with self.assertRaises(ValueError):
            await self.run_episode("library", title=title)
        self.download.assert_not_awaited()
        self.assertEqual(list(self.library.rglob("*.mp4")), [])


class ArchiveTests(unittest.TestCase):
    def test_variants_distinct_and_nested_library_visible(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            src = root / "source.mp4"
            src.write_bytes(b"video")
            library = root / "library"
            library.mkdir()
            (library / ".animedl-library").touch()
            paths = [archive_to_library(src, library, "Title", 1, season=1, voice=voice, quality=q,
                identity="animego|123") for voice, q in (("Voice A", 720), ("Voice B", 720), ("Voice A", 1080))]
            self.assertEqual(len(set(paths)), 2)
            self.assertTrue(all("S01E01" in p.name and p.parent.name == "Season 01" for p in paths))
            self.assertEqual(list_library(library)[0][2], 2)

    def test_long_cyrillic_names_fit_filesystem_byte_limit(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / ".animedl-library").touch()
            src = root / "src.mp4"
            src.write_bytes(b"video")
            dst = archive_to_library(src, root, "Длинное название " * 8, 123, season=1,
                voice="Название озвучки " * 30, quality=1080, provider="Kodik")
            self.assertLessEqual(len(dst.name.encode("utf-8")), 255)
            self.assertLessEqual(len(dst.parent.parent.name.encode("utf-8")), 255)
            self.assertEqual(dst.read_bytes(), b"video")

    def test_nfo_has_clean_episode_name_season_and_provenance(self):
        import xml.etree.ElementTree as ET
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / ".animedl-library").touch()
            src = root / "src.mp4"
            src.write_bytes(b"video")
            dst = archive_to_library(src, root, "One & Room", 1, season=3, voice="Voice & dub",
                provider="Kodik", quality=720, source_identity="animego|456", source_url="https://example.test/anime")
            episode = ET.parse(dst.with_suffix(".nfo")).getroot()
            self.assertEqual(episode.findtext("title"), "Серия 1")
            self.assertEqual(episode.findtext("showtitle"), "One & Room")
            self.assertEqual(episode.findtext("season"), "3")
            self.assertEqual(episode.findtext("episode"), "1")
            self.assertIn("Voice & dub", episode.findtext("plot"))
            self.assertIn("animego|456", episode.findtext("plot"))
            self.assertNotIn("Voice", dst.name)
            show = ET.parse(dst.parent.parent / "tvshow.nfo").getroot()
            self.assertEqual(show.findtext("title"), "One & Room")
            self.assertEqual(show.findtext("tag"), "anime")
            season = ET.parse(dst.parent / "season.nfo").getroot()
            self.assertEqual(season.findtext("seasonnumber"), "3")

    def test_named_arcs_get_distinct_stable_seasons_under_parallel_archive(self):
        import concurrent.futures
        import xml.etree.ElementTree as ET
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / ".animedl-library").touch()
            src = root / "src.mp4"
            src.write_bytes(b"video")
            def archive(label):
                return archive_to_library(src, root, "Блич", 1, season_name=label)
            labels = ("Бедствие", "Конфликт")
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                first, second = list(executor.map(archive, labels))
            self.assertEqual(first.parent.parent, second.parent.parent)
            self.assertNotEqual(first.parent, second.parent)
            self.assertEqual(archive(labels[0]), first)
            self.assertEqual(archive(labels[1]), second)
            self.assertEqual(ET.parse(first.parent / "season.nfo").getroot().findtext("title"), labels[0])
            self.assertEqual(ET.parse(second.parent / "season.nfo").getroot().findtext("title"), labels[1])

    def test_legacy_nfo_show_root_reused_without_moving_existing_file(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / ".animedl-library").touch()
            legacy = root / "old-source-title [e58c97906d]"
            legacy.mkdir()
            (legacy / "tvshow.nfo").write_text("<tvshow><title>Блич</title></tvshow>", encoding="utf-8")
            folder = legacy / "Season 01"
            folder.mkdir()
            existing = folder / "existing.mp4"
            existing.write_bytes(b"old")
            (folder / "season.nfo").write_text("<season><title>Бедствие</title><seasonnumber>1</seasonnumber></season>", encoding="utf-8")
            src = root / "src.mp4"
            src.write_bytes(b"new")
            dst = archive_to_library(src, root, "Блич", 2, season_name="Бедствие", identity="new-canonical-identity")
            self.assertEqual(dst.parent, folder)
            self.assertEqual(existing.read_bytes(), b"old")
            self.assertEqual([p for p in root.iterdir() if p.is_dir()], [legacy])

    def test_multiple_matching_show_roots_fail_instead_of_merging(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / ".animedl-library").touch()
            for name in ("first", "second"):
                folder = root / name
                folder.mkdir()
                (folder / "tvshow.nfo").write_text("<tvshow><title>Блич</title></tvshow>", encoding="utf-8")
            src = root / "src.mp4"
            src.write_bytes(b"video")
            with self.assertRaises(ValueError):
                archive_to_library(src, root, "Блич", 1, season_name="Бедствие")
            self.assertEqual(list(root.rglob("*.mp4")), [src])

    def test_numeric_migration_can_name_existing_default_season(self):
        import xml.etree.ElementTree as ET
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            index, folder = resolve_library_season(root, 1)
            self.assertEqual(resolve_library_season(root, 1, "Бедствие"), (index, folder))
            self.assertEqual(resolve_library_season(root, season_name="Бедствие"), (index, folder))
            self.assertEqual(ET.parse(folder / "season.nfo").getroot().findtext("title"), "Бедствие")
            with self.assertRaises(ValueError):
                resolve_library_season(root, 1, "Конфликт")

    def test_failed_copy_never_publishes_partial_file(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            src = root / "src.mp4"
            src.write_bytes(b"original")
            (root / ".animedl-library").touch()
            dst = archive_to_library(src, root, "Title", 1, season=1, voice="Voice", quality=720)
            def fail_copy(source, staging):
                Path(staging).write_bytes(b"incomplete")
                raise OSError("disk full")
            with patch("shutil.copy2", side_effect=fail_copy):
                with self.assertRaises(OSError):
                    archive_to_library(src, root, "Title", 1, season=1, voice="Voice", quality=1080)
            self.assertEqual(dst.read_bytes(), b"original")
            self.assertEqual(list(root.rglob("*.part")), [])


class SeasonMetadataTests(unittest.TestCase):
    def test_only_explicit_season_labels_are_used(self):
        for title, season in (("One Room Season 2", 2), ("Аниме 3 сезон", 3),
                ("Аниме сезон 4", 4), ("One Room 2nd Season", 2),
                ("One Room 2", None), ("86", None), ("Season 1 / Season 2", None)):
            self.assertEqual(explicit_season(title), season, title)
        self.assertEqual(library_title_suggestion("One Room Season 2"), "One Room")
        self.assertEqual(library_title_suggestion("One Room 2"), "One Room 2")
        self.assertEqual(library_selection_suggestion("Блич — Бедствие"), ("Блич", None, "Бедствие"))

    def test_source_title_keeps_named_arc_without_guessing_season(self):
        title = "Блич: Тысячелетняя кровавая война — Бедствие"
        raw = f"<title>{title} — смотреть аниме онлайн бесплатно</title><h1>{title}</h1>"
        parsed = YummyAnimeParser._source_title(raw)
        self.assertEqual(parsed, title)
        self.assertIsNone(explicit_season(parsed))
        self.assertEqual(YummyAnimeParser._source_title(
            f'<meta property="og:title" content="{title} — смотреть аниме онлайн бесплатно">'), title)

    def test_fsm_restores_source_season_and_poster(self):
        title = _title_from_state(dict(episodes=[dict(number=1, db_id="ep")],
            site="animego", url="url", anime_id="123", title="Title", season=2, poster="poster-url"))
        self.assertEqual((title.season, title.poster), (2, "poster-url"))


if __name__ == "__main__":
    unittest.main()
