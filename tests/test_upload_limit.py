import unittest
from types import SimpleNamespace
from animedl.services import telegram_upload_limit
class UploadLimitTests(unittest.TestCase):
 def test_local_timeout_limit_and_cloud_limit(self):
  c=SimpleNamespace(max_tg_bytes=2000*1024**2,tg_api_base_url='http://local:8081',tg_api_local_mode=True)
  self.assertEqual(telegram_upload_limit(c),c.max_tg_bytes)
  c.tg_api_local_mode=False
  self.assertEqual(telegram_upload_limit(c),c.max_tg_bytes)
  c.max_tg_bytes=50*1024**2;c.tg_api_local_mode=True
  self.assertEqual(telegram_upload_limit(c),50*1024**2)
 def test_configured_bound(self):
  c=SimpleNamespace(max_tg_bytes=2000*1024**2,tg_api_base_url='http://local:8081',tg_api_local_mode=True,telegram_upload_part_mb=64)
  self.assertEqual(telegram_upload_limit(c),64*1024**2)

 def test_old_fragment_cache_does_not_force_small_episodes_into_parts(self):
  import tempfile
  from pathlib import Path
  from animedl.cache.store import CacheStore
  from animedl.services import _cached_video
  with tempfile.TemporaryDirectory() as root:
   cache=CacheStore(Path(root)/'cache.db')
   try:
    cfg=SimpleNamespace(max_tg_bytes=2000*1024**2,tg_api_base_url='http://local:8081',tg_api_local_mode=True,telegram_upload_part_mb=0)
    cache.put('old','parts:["a","b"]',quality=1080,size_bytes=900*1024**2)
    self.assertIsNone(_cached_video(cache,'old',1080,cfg))
    cache.put('large','parts:["a","b"]',quality=1080,size_bytes=2500*1024**2)
    self.assertIsNotNone(_cached_video(cache,'large',1080,cfg))
    cache.put('whole','whole_id',quality=1080,size_bytes=900*1024**2)
    self.assertEqual(_cached_video(cache,'whole',1080,cfg),'whole_id')
   finally:cache.close()
