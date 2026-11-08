import tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from animedl.admin_ops import library_overview
from animedl.models import MEDIA_CATEGORIES
class OverviewTests(unittest.TestCase):
 def test_inventory_capacity_and_unavailable(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);(root/'.animedl-library').touch()
   for _,folder in MEDIA_CATEGORIES.values():
    (root/folder).mkdir();(root/folder/'.animedl-library').touch()
   show=root/'Anime'/'A & B';show.mkdir();(show/'episode.mp4').write_bytes(b'x'*1024)
   cfg=SimpleNamespace(media_root=root,category_dir=lambda k:root/MEDIA_CATEGORIES[k][1])
   with patch('shutil.disk_usage',return_value=SimpleNamespace(total=10*1024**3,free=4*1024**3)):
    text=library_overview(cfg)
   self.assertIn('A &amp; B',text);self.assertIn('1 тайтлов',text)
   self.assertEqual(text.count('Хранилище:'),1);self.assertIn('6.0 из 10.0',text)
   self.assertIn('Пока пусто',text)
   (root/'.animedl-library').unlink()
   self.assertEqual(library_overview(cfg).count('недоступна'),4)
