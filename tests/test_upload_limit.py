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
