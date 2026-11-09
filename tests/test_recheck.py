import unittest,tempfile,time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from animedl.observability import AuditStore,operation,error_details
from animedl.models import TitleInfo,EpisodeRef,PlayerButton,VideoVariant
from animedl.recheck import save_case,verify_case
class RecheckTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.cfg=SimpleNamespace(tmp_dir=Path(self.tmp.name)/'tmp')
  self.store=AuditStore(Path(self.tmp.name)/'audit.db')
  self.title=TitleInfo('yummyanime','https://old.yummyani.me/catalog/item/test','123','Title')
  self.ep=EpisodeRef(2,'ep',season=1)
  self.proof=operation('source_download','yummyanime',self.title.url,'123',1,2,'Voice',720)
  self.store.record('source_error',17,'error',**(self.proof|error_details(ValueError('x'))))
  self.parser=SimpleNamespace(fetch_title=AsyncMock(return_value=self.title),fetch_episode_players=AsyncMock(return_value=[PlayerButton('Voice','Kodik','embed'),PlayerButton('Other','Kodik','other')]))
  self.patch=patch('animedl.sites.registry.get_parser',return_value=self.parser);self.patch.start()
  save_case(self.store,17,self.proof,self.title,self.ep,'Voice',720)
  self.identity=self.store.incidents.identity(17,self.proof)[0]
 async def asyncTearDown(self):
  self.patch.stop();await self.store.close();self.tmp.cleanup()
 async def test_full_verification_closes_exact_user_without_sending_media(self):
  async def download(v,p,progress,cancel_check):
   p.write_bytes(b'video');return SimpleNamespace(size_bytes=5,duration=20,width=1280,height=720)
  with patch('animedl.services.extract_variants',AsyncMock(return_value=[VideoVariant(720,'media')])) as extract:
   await verify_case(self.store,self.cfg,self.identity,download=download)
  self.assertEqual(self.store.incidents.rows()[0]['state'],'resolved')
  self.assertEqual(extract.await_count,1)
  self.assertEqual(list(self.cfg.tmp_dir.iterdir()),[])
 async def test_lower_quality_cannot_close_720_failure(self):
  with patch('animedl.services.extract_variants',AsyncMock(return_value=[VideoVariant(480,'media')])):
   with self.assertRaisesRegex(ValueError,'480p'):
    await verify_case(self.store,self.cfg,self.identity,download=AsyncMock())
  self.assertEqual(self.store.incidents.rows()[0]['state'],'open')
 async def test_false_720_file_stays_open_and_is_removed(self):
  async def download(v,p,progress,cancel_check):
   p.write_bytes(b'video');return SimpleNamespace(size_bytes=5,duration=20,width=854,height=480)
  with patch('animedl.services.extract_variants',AsyncMock(return_value=[VideoVariant(720,'media')])):
   with self.assertRaises(ValueError):await verify_case(self.store,self.cfg,self.identity,download=download)
  self.assertEqual(self.store.incidents.rows()[0]['state'],'open')
  self.assertEqual(list(self.cfg.tmp_dir.iterdir()),[])
 async def test_changed_identity_or_missing_snapshot_does_not_resolve(self):
  self.parser.fetch_title.return_value=TitleInfo('yummyanime',self.title.url,'different','Title')
  with self.assertRaises(ValueError):await verify_case(self.store,self.cfg,self.identity)
  self.store.db.execute('DELETE FROM audit_recheck_cases');self.store.db.commit()
  with self.assertRaisesRegex(ValueError,'Нет сохранённых'):await verify_case(self.store,self.cfg,self.identity)
