import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import AsyncMock, patch

from animedl.handlers import inbox


class InboxTests(TestCase):
    def test_only_owner_in_private_chat(self):
        cfg = NS(admin_id=123, media_root=Path("/media"))
        message = NS(chat=NS(id=123, type="private"), from_user=NS(id=123),
                     video=NS(file_id="video"), document=None, video_note=None)
        self.assertTrue(asyncio.run(inbox.OwnerVideo()(message, cfg)))
        message.from_user.id = 456
        self.assertFalse(asyncio.run(inbox.OwnerVideo()(message, cfg)))
        message.from_user.id = 123
        message.chat.type = "group"
        self.assertFalse(asyncio.run(inbox.OwnerVideo()(message, cfg)))

    def test_foreign_callback_does_not_fetch_file(self):
        cfg = NS(admin_id=123, media_root=Path("/media"))
        bot = NS(get_file=AsyncMock())
        callback = NS(from_user=NS(id=456), message=None, answer=AsyncMock())
        asyncio.run(inbox.save(callback, bot, cfg))
        bot.get_file.assert_not_awaited()

    def test_publish_preserves_bytes_and_rejects_bad_paths(self):
        with TemporaryDirectory() as path:
            root = Path(path)
            api = root / "api"
            api.mkdir()
            source = api / "video.mp4"
            source.write_bytes(b"video bytes")
            with self.assertRaises(OSError):
                inbox.inbox_dir(root)
            (root / ".animedl-library").touch()
            target = inbox.inbox_dir(root)
            media = NS(file_name="../clip.mp4", file_size=11)
            with patch.object(inbox, "_api_root", api):
                dest = inbox.publish(source, target, 12, media)
                self.assertEqual(dest, root / "TelegramInbox" / "12-clip.mp4")
                self.assertEqual(dest.read_bytes(), source.read_bytes())
                self.assertEqual(inbox.publish(source, target, 12, media), dest)
                with self.assertRaises(OSError):
                    inbox.publish(dest, target, 13, media)
                media.file_size = 12
                with self.assertRaises(OSError):
                    inbox.publish(source, target, 13, media)
            self.assertEqual(list(target.iterdir()), [dest])
