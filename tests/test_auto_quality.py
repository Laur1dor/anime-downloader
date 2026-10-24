import asyncio
import errno
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from animedl.download.downloader import DownloadCancelled, DownloadResult
from animedl.cache.store import CacheStore
from animedl.models import EpisodeRef, PlayerButton, TitleInfo, VideoVariant, make_cache_hash
from animedl.services import process_episode, qualities_for_buttons


class QualityUnionTests(unittest.IsolatedAsyncioTestCase):
    async def test_union_includes_other_players_and_ignores_failed_player(self):
        buttons = [PlayerButton('V', p, p) for p in ('Kodik', 'CVH', 'Broken')]

        async def extract(button):
            if button.provider == 'Broken':
                raise RuntimeError('unavailable')
            return [VideoVariant(q, 'url') for q in
                    ([360, 720] if button.provider == 'Kodik' else [720, 1080])]

        with patch('animedl.services.extract_variants', side_effect=extract):
            self.assertEqual(await qualities_for_buttons(buttons), [1080, 720, 360])

    async def test_probes_have_bounded_concurrency(self):
        active = peak = 0

        async def extract(button):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1
            return [VideoVariant(1080, 'url')]

        with patch('animedl.services.extract_variants', side_effect=extract):
            result = await qualities_for_buttons([PlayerButton('V', str(i), 'url') for i in range(9)])
        self.assertEqual(result, [1080])
        self.assertEqual(peak, 3)

    async def test_all_failed_players_do_not_offer_guessed_qualities(self):
        with patch('animedl.services.extract_variants', side_effect=RuntimeError('down')):
            self.assertEqual(await qualities_for_buttons([PlayerButton('V', 'CVH', 'url')]), [])


class AutoProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = SimpleNamespace(tmp_dir=Path(self.temp.name), max_tg_bytes=1000, max_tg_mb=1)
        self.cache = Mock()
        self.cache.get.return_value = None
        self.buttons = [PlayerButton('V', p, p) for p in ('AniBoom', 'Kodik', 'Aloha')]
        self.parser = SimpleNamespace(fetch_episode_players=AsyncMock(return_value=self.buttons))
        self.kwargs = dict(bot=Mock(), cfg=self.cfg, cache=self.cache, parser=self.parser,
            title=TitleInfo('animego', 'url', '123', 'Title'), episode=EpisodeRef(1, 'ep'),
            voice='V', provider='auto', quality=1080, on_status=AsyncMock(), chat_id=1)

        async def download(variant, output, progress, cancel_check=None):
            path = output.with_suffix('.mp4')
            path.write_bytes(b'video')
            return DownloadResult(path, 1920, 1080, 10, 5)

        self.download = AsyncMock(side_effect=download)
        self.upload = AsyncMock(return_value=SimpleNamespace(video=SimpleNamespace(file_id='file')))
        self.cached = AsyncMock()
        self.extract = AsyncMock(side_effect=lambda b: [VideoVariant(
            1080 if b.provider == 'Aloha' else 720, b.provider)])
        for name, value in [('download_variant', self.download), ('send_video_file', self.upload),
                            ('send_video_by_id', self.cached), ('extract_variants', self.extract)]:
            patcher = patch('animedl.services.' + name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def run_episode(self, **changes):
        result = await process_episode(**(self.kwargs | changes))
        self.assertEqual(list(self.cfg.tmp_dir.iterdir()), [])
        return result

    async def test_auto_finds_1080_on_another_player_and_caches_actual_provider(self):
        result = await self.run_episode()
        self.assertEqual(result.telegram, 'sent')
        self.assertEqual(self.download.call_args.args[0].url, 'Aloha')
        self.assertEqual(self.cache.put.call_args.args[0],
            make_cache_hash('animego', '123', 1, 'V', 'Aloha', 1080))

    async def test_false_quality_label_falls_back_without_uploading_lower_resolution(self):
        self.extract.side_effect = lambda b: [VideoVariant(1080, b.provider)]
        original = self.download.side_effect
        async def wrong_first(variant, output, progress, **kwargs):
            result = await original(variant, output, progress, **kwargs)
            if variant.url != 'Aloha':
                result.width, result.height = 854, 480
            return result
        self.download.side_effect = wrong_first
        result = await self.run_episode()
        self.assertEqual(result.telegram, 'sent')
        self.assertEqual(self.download.await_count, 3)
        self.assertEqual(self.upload.await_count, 1)
        self.assertIn('Aloha', self.upload.call_args.kwargs['caption'])

    async def test_no_exact_quality_never_downloads_lower(self):
        self.extract.side_effect = lambda b: [VideoVariant(720, b.provider)]
        result = await self.run_episode()
        self.assertEqual(result.status, 'error')
        self.assertIn('1080p', result.note)
        self.download.assert_not_awaited()
        self.cache.put.assert_not_called()

    async def test_extraction_failure_uses_another_player(self):
        async def extract(button):
            if button.provider == 'AniBoom':
                raise RuntimeError('broken player')
            return [VideoVariant(1080, button.provider)]
        self.extract.side_effect = extract
        result = await self.run_episode()
        self.assertEqual(result.telegram, 'sent')
        self.assertEqual(self.download.call_args.args[0].url, 'Kodik')

    async def test_source_failure_retries_then_uses_another_exact_quality_player(self):
        self.extract.side_effect = lambda b: [VideoVariant(1080, b.provider)]
        original = self.download.side_effect

        async def download(variant, *args, **kwargs):
            if variant.url == 'AniBoom':
                raise httpx.ConnectError('unavailable')
            return await original(variant, *args, **kwargs)

        self.download.side_effect = download
        with patch('animedl.services.asyncio.sleep', new=AsyncMock()):
            result = await self.run_episode()
        self.assertEqual(result.telegram, 'sent')
        self.assertEqual(self.download.await_count, 4)
        self.assertEqual(self.download.call_args.args[0].url, 'Kodik')

    async def test_cancellation_never_attempts_other_players(self):
        self.extract.side_effect = lambda b: [VideoVariant(1080, b.provider)]
        self.download.side_effect = DownloadCancelled()
        result = await self.run_episode()
        self.assertEqual(result.status, 'cancelled')
        self.assertEqual(self.download.await_count, 1)
        self.assertEqual(self.extract.await_count, 1)

    async def test_disk_full_never_attempts_other_players(self):
        self.extract.side_effect = lambda b: [VideoVariant(1080, b.provider)]
        self.download.side_effect = OSError(errno.ENOSPC, 'disk full')
        result = await self.run_episode()
        self.assertEqual(result.status, 'error')
        self.assertEqual(self.download.await_count, 1)
        self.assertEqual(self.extract.await_count, 1)

    async def test_auto_uses_actual_provider_cache_without_downloading(self):
        expected = make_cache_hash('animego', '123', 1, 'V', 'Aloha', 1080)
        self.cache.get.side_effect = lambda key, expected_quality=None: 'cached' if key == expected else None
        result = await self.run_episode()
        self.assertEqual(result.status, 'sent_cached')
        self.download.assert_not_awaited()
        self.cached.assert_awaited_once()

    async def test_manual_lower_fallback_is_cached_under_actual_quality(self):
        result = await self.run_episode(provider='Kodik')
        self.assertEqual(result.telegram, 'sent')
        self.assertEqual(self.cache.put.call_args.args[0],
            make_cache_hash('animego', '123', 1, 'V', 'Kodik', 720))
        self.assertEqual(self.cache.put.call_args.kwargs['quality'], 720)

    async def test_each_episode_resolves_its_own_players(self):
        self.parser.fetch_episode_players.side_effect = [
            [PlayerButton('V', 'Aloha', 'one')], [PlayerButton('V', 'Kodik', 'two')]]
        self.extract.side_effect = lambda b: [VideoVariant(1080, b.embed_url)]
        await self.run_episode()
        await self.run_episode(episode=EpisodeRef(2, 'ep2'))
        self.assertEqual([call.args[0].url for call in self.download.call_args_list], ['one', 'two'])

    async def test_old_720_cache_under_1080_key_is_never_reused(self):
        cache = CacheStore(self.cfg.tmp_dir.parent / (self.cfg.tmp_dir.name + '.sqlite'))
        self.addCleanup(lambda: cache.db_path.unlink(missing_ok=True))
        self.addCleanup(cache.close)
        key = make_cache_hash('animego', '123', 1, 'V', 'Aloha', 1080)
        cache.put(key, 'wrong-720-file', quality=720)
        result = await self.run_episode(cache=cache)
        self.assertEqual(result.telegram, 'sent')
        self.cached.assert_not_awaited()
        self.assertEqual(cache.get(key, expected_quality=1080), 'file')


class CacheQualityTests(unittest.TestCase):
    def test_expected_quality_filters_legacy_mismatch_and_keeps_compatible_api(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = CacheStore(Path(directory) / 'cache.sqlite')
            try:
                cache.put('requested-1080', 'actually-720', quality=720)
                self.assertIsNone(cache.get('requested-1080', expected_quality=1080))
                self.assertEqual(cache.get('requested-1080', expected_quality=720), 'actually-720')
                self.assertEqual(cache.get('requested-1080'), 'actually-720')
            finally:
                cache.close()
