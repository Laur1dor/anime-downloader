import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from animedl.admin_ops import ArchiveOutcome, archive_to_library


class LibraryQualityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / '.animedl-library').touch()
        self.src = self.root / 'input.bin'

    def publish(self, quality, payload, **kwargs):
        self.src.write_bytes(payload)
        outcome = ArchiveOutcome()
        path = archive_to_library(self.src, self.root, 'Title', 12,
            season=kwargs.pop('season', 6), voice=kwargs.pop('voice', 'Goblin'),
            quality=quality, outcome=outcome, **kwargs)
        return path, outcome

    def test_upgrade_reuses_legacy_path_and_removes_stale_stream_metadata(self):
        path, _ = self.publish(360, b'low', provider='Old', source_identity='old')
        legacy = path.with_name('legacy-quality-hash.mp4')
        path.rename(legacy)
        path.with_suffix('.nfo').rename(legacy.with_suffix('.nfo'))
        tree = ET.parse(legacy.with_suffix('.nfo'))
        ET.SubElement(tree.getroot(), 'fileinfo').text = 'old 360p streams'
        tree.write(legacy.with_suffix('.nfo'), encoding='utf-8')
        upgraded, outcome = self.publish(1080, b'high', provider='New', source_identity='new')
        self.assertEqual(upgraded, legacy)
        self.assertEqual(legacy.read_bytes(), b'high')
        self.assertTrue(outcome.upgraded)
        self.assertEqual(outcome.quality, 1080)
        self.assertIsNone(ET.parse(legacy.with_suffix('.nfo')).getroot().find('fileinfo'))
        self.assertEqual(len(list(self.root.rglob('*.mp4'))), 1)

    def test_lower_and_equal_quality_keep_existing_media_and_metadata(self):
        path, _ = self.publish(1080, b'best')
        nfo = path.with_suffix('.nfo').read_bytes()
        for quality in (360, 1080):
            kept, outcome = self.publish(quality, b'other')
            self.assertEqual(kept, path)
            self.assertFalse(outcome.updated)
            self.assertEqual(outcome.quality, 1080)
            self.assertEqual(path.read_bytes(), b'best')
            self.assertEqual(path.with_suffix('.nfo').read_bytes(), nfo)

    def test_distinct_voices_and_episodes_remain_independent(self):
        first, _ = self.publish(1080, b'first')
        second, _ = self.publish(360, b'second', voice='Other')
        third, _ = self.publish(360, b'third', season=5)
        self.assertEqual(len({first, second, third}), 3)
        self.assertEqual(first.read_bytes(), b'first')

    def test_failed_upgrade_copy_preserves_original(self):
        path, _ = self.publish(360, b'original')
        nfo = path.with_suffix('.nfo').read_bytes()
        def fail(src, dst):
            Path(dst).write_bytes(b'partial')
            raise OSError('disk full')
        with patch('shutil.copy2', side_effect=fail):
            with self.assertRaises(OSError):
                self.publish(1080, b'new')
        self.assertEqual(path.read_bytes(), b'original')
        self.assertEqual(path.with_suffix('.nfo').read_bytes(), nfo)
        self.assertEqual(list(self.root.rglob('*.part')), [])

    def test_movies_upgrade_across_provider_and_source_identity(self):
        path, _ = self.publish(360, b'low', season=None, media_tag='movies', identity='old')
        upgraded, outcome = self.publish(1080, b'high', season=None, media_tag='movies', identity='new')
        self.assertEqual(path, upgraded)
        self.assertTrue(outcome.upgraded)
        self.assertEqual(path.read_bytes(), b'high')

    def test_actual_episode_title_updates_metadata_without_replacing_video(self):
        path, _ = self.publish(1080, b'best')
        before = path.stat().st_mtime_ns
        same, outcome = self.publish(720, b'lower', episode_title='A Name &amp; Another')
        self.assertEqual(same, path)
        self.assertFalse(outcome.updated)
        self.assertTrue(outcome.metadata_updated)
        self.assertEqual(path.stat().st_mtime_ns, before)
        self.assertEqual(path.read_bytes(), b'best')
        self.assertEqual(ET.parse(path.with_suffix('.nfo')).findtext('title'), 'A Name & Another')

    def test_upgrade_without_source_name_keeps_existing_real_title(self):
        path, _ = self.publish(360, b'low', episode_title='An actual name')
        same, _ = self.publish(1080, b'high', episode_title='12 серия')
        self.assertEqual(path, same)
        self.assertEqual(ET.parse(path.with_suffix('.nfo')).findtext('title'), 'An actual name')

    def test_numeric_source_labels_keep_clean_fallback(self):
        from animedl.models import episode_name
        for value in ('', '12', '12 серия', 'Серия 12', 'Episode 12'):
            self.assertEqual(episode_name(value, 12), 'Серия 12')
        self.assertEqual(episode_name('46-й размер', 2), '46-й размер')


if __name__ == '__main__':
    unittest.main()
