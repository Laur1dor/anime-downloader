import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from animedl.cache.store import CacheStore
from animedl.download.downloader import DownloadResult
from animedl.models import EpisodeRef, PlayerButton, TitleInfo, VideoVariant
from animedl.recovery import RecoveryStore
from animedl.services import process_episode, EpisodeResult
from animedl.handlers.flow import _run_season, on_go
from animedl.telegram_client import _send_with_flood_retry, send_video_file
from aiogram.exceptions import TelegramNetworkError, TelegramBadRequest
from aiogram.methods import SendVideo


class RecoveryTests(unittest.TestCase):
    def test_bounded_eviction_expiry_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'input.mp4'
            source.write_bytes(b'video')
            result = DownloadResult(source, 10, 10, 2, 5)
            store = RecoveryStore(root / 'cache', 5, 30)
            self.assertTrue(store.save('a', result, 'Kodik', 720))
            self.assertTrue(store.save('b', result, 'Alloha', 1080))
            self.assertIsNone(store.load('a', root / 'gone.mp4'))
            restarted = RecoveryStore(root / 'cache', 5, 30)
            loaded, provider, quality = restarted.load('b', root / 'copy.mp4')
            self.assertEqual((loaded.path.read_bytes(), provider, quality), (b'video', 'Alloha', 1080))
            for entry in restarted._entries():
                os.utime(entry, (time.time() - 60, time.time() - 60))
            self.assertIsNone(restarted.load('b', root / 'expired.mp4'))
            self.assertEqual(restarted._entries(), [])

    def test_oversized_file_not_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'v.mp4'
            path.write_bytes(b'123456')
            self.assertFalse(RecoveryStore(Path(tmp) / 'cache', 5, 30).save(
                'x', DownloadResult(path, 1, 1, 1, 6), 'p', 720))


class UploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_file_upload_has_long_timeout_and_no_ambiguous_transport_retry(self):
        method = SendVideo(chat_id=1, video='id')
        bot = SimpleNamespace(send_video=AsyncMock(side_effect=TelegramNetworkError(method=method, message='timeout')))
        with self.assertRaises(TelegramNetworkError):
            await send_video_file(bot, 1, Path('example.mp4'), width=640, height=360, duration=10)
        self.assertEqual(bot.send_video.await_count, 1)
        self.assertEqual(bot.send_video.call_args.kwargs['request_timeout'], 3600)

    async def test_retry_button_retries_only_failed_selection(self):
        status = SimpleNamespace(edit_text=AsyncMock())
        ctrl = SimpleNamespace(is_paused=False, cancelled=False)
        processor = AsyncMock(side_effect=[EpisodeResult(1, 'sent_new', telegram='sent'),
                                           EpisodeResult(2, 'error', telegram='error')])
        with patch('animedl.handlers.flow.process_episode', processor):
            failed = await _run_season(None, None, None, None, TitleInfo('s', 'url', 'id', 'Title'),
                {1: EpisodeRef(1, '1'), 2: EpisodeRef(2, '2')}, [1, 2], 'V', 'auto', 720,
                ctrl, 1, status, AsyncMock())
        self.assertEqual(failed, [2])
        self.assertEqual(status.edit_text.call_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data,
                         'retry_failed')

    async def test_old_retry_button_cannot_start_latest_selection(self):
        state = SimpleNamespace(get_data=AsyncMock(return_value={'retry_message': 42}))
        cq = SimpleNamespace(data='retry_failed', message=SimpleNamespace(message_id=41), answer=AsyncMock())
        await on_go(cq, state, None, None, None)
        self.assertTrue(cq.answer.call_args.kwargs['show_alert'])

    async def test_failed_second_part_resumes_without_source_or_duplicate_first_part(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = SimpleNamespace(admin_id=1, tmp_dir=root / 'tmp', cache_db_path=root / 'cache.sqlite',
                retry_cache_bytes=1024, retry_cache_hours=24, max_tg_bytes=5, max_tg_mb=1)
            cfg.tmp_dir.mkdir()
            cache = CacheStore(cfg.cache_db_path)
            self.addCleanup(cache.close)
            parser = SimpleNamespace(fetch_episode_players=AsyncMock(return_value=[PlayerButton('v', 'Alloha', 'embed')]))
            async def download(variant, out_base, *args, **kwargs):
                path = out_base.with_suffix('.mp4')
                path.write_bytes(b'1234567890')
                return DownloadResult(path, 1920, 1080, 10, 10)
            async def split(result, limit, **kwargs):
                output = []
                for i in range(2):
                    path = result.path.parent / f'part{i}.mp4'
                    path.write_bytes(b'12345')
                    output.append(DownloadResult(path, 100, 100, 5, 5))
                return output
            def message(file_id):
                return SimpleNamespace(video=SimpleNamespace(file_id=file_id))
            upload = AsyncMock(side_effect=[message('first'), RuntimeError('connection'), message('second')])
            kwargs = dict(bot=Mock(), cfg=cfg, cache=cache, parser=parser,
                title=TitleInfo('yummyanime', 'url', '1', 'Title'), episode=EpisodeRef(1, '1'),
                voice='v', provider='auto', quality=1080, chat_id=1, user_id=1, on_status=AsyncMock())
            with patch('animedl.services.download_variant', AsyncMock(side_effect=download)) as downloading, \
                 patch('animedl.services.extract_variants', AsyncMock(return_value=[VideoVariant(1080, 'stream')])), \
                 patch('animedl.services.split_for_telegram', AsyncMock(side_effect=split)), \
                 patch('animedl.services.send_video_file', upload):
                first = await process_episode(**kwargs)
                self.assertEqual(first.telegram, 'error')
                parser.fetch_episode_players.side_effect = AssertionError('source should not be contacted')
                second = await process_episode(**kwargs)
                self.assertEqual(second.telegram, 'sent')
                self.assertEqual(downloading.await_count, 1)
                self.assertEqual(upload.await_count, 3)
                self.assertIn('2/2', upload.call_args.kwargs['caption'])
                parser.fetch_episode_players.side_effect = None
                with patch('animedl.services.send_video_by_id', AsyncMock()) as resend:
                    third = await process_episode(**kwargs)
                self.assertEqual(third.telegram, 'cached')
                self.assertEqual([c.args[2] for c in resend.await_args_list], ['first', 'second'])
            cache.close()

    async def test_transport_retry_is_bounded_but_bad_request_not_retried(self):
        method = SendVideo(chat_id=1, video='id')
        retry = AsyncMock(side_effect=[TelegramNetworkError(method=method, message='timeout'), 'ok'])
        with patch('animedl.telegram_client.asyncio.sleep', AsyncMock()):
            self.assertEqual(await _send_with_flood_retry(retry), 'ok')
            broken = AsyncMock(side_effect=TelegramNetworkError(method=method, message='timeout'))
            with self.assertRaises(TelegramNetworkError):
                await _send_with_flood_retry(broken)
            self.assertEqual(broken.await_count, 3)
            uncertain = AsyncMock(side_effect=TelegramNetworkError(method=method, message='timeout'))
            with self.assertRaises(TelegramNetworkError):
                await _send_with_flood_retry(uncertain, retry_transport=False)
            self.assertEqual(uncertain.await_count, 1)
            invalid = AsyncMock(side_effect=TelegramBadRequest(method=method, message='invalid'))
            with self.assertRaises(TelegramBadRequest):
                await _send_with_flood_retry(invalid)
            self.assertEqual(invalid.await_count, 1)
