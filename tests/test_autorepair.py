import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from maintenance.policy import Ledger, eligible, validate
from maintenance.policy import source_hash
from maintenance import release
from maintenance import worker
from animedl.runtime_health import RuntimeHealth


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.original = {"animedl/players/plplayer.py": "def parse(x):\n    return x['video']\n"}
        self.proposal = {"summary": "Поддержать новое поле", "files": [
            {"path": "animedl/players/plplayer.py", "old": "return x['video']", "new": "return x.get('video', x.get('url'))"}],
            "test": {"path": "tests/test_repair_video.py", "content": "import unittest\nclass Test(unittest.TestCase):\n def test_value(self):\n  self.assertEqual(1, 1)\n"}}

    def test_small_parser_patch_with_new_test_allowed(self):
        self.assertEqual(len(validate(self.proposal, self.original)), 2)

    def test_scope_path_test_overwrite_and_process_access_denied(self):
        for path in ("../bot.py", "/bot.py", "animedl/../bot.py", "animedl\\players\\plplayer.py", "animedl/handlers/access.py", "requirements.txt"):
            p = copy.deepcopy(self.proposal); p['files'][0]['path'] = path
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate(p, self.original)
        p = copy.deepcopy(self.proposal)
        p['files'][0]['old'] = "def parse(x):\n    return x['video']\n"
        p['files'][0]['new'] = 'import subprocess\n'
        with self.assertRaises(ValueError):
            validate(p, self.original)
        with self.assertRaises(ValueError):
            validate(self.proposal, self.original | {self.proposal['test']['path']: 'existing'})

    def test_network_quota_and_unknown_errors_are_not_code_repairs(self):
        details = dict(error_class='ExtractError', location='plplayer.py:extract:50', fingerprint='a'*16)
        self.assertTrue(eligible(details))
        self.assertFalse(eligible(details | {'http_status': 403}))
        self.assertFalse(eligible(details | {'error_class': 'TimeoutError'}))
        self.assertFalse(eligible(details | {'location': 'access.py:check:20'}))

    def test_durable_budget_deduplicates_and_stops_after_three(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'ledger.db'
            store = Ledger(path)
            self.assertTrue(store.claim('a', 's', 100000))
            second = Ledger(path)
            self.assertFalse(second.claim('a', 's', 100001))
            second.close()
            self.assertTrue(store.claim('b', 's', 100002))
            self.assertTrue(store.claim('c', 's', 100003))
            self.assertFalse(store.claim('d', 's', 100004))
            self.assertTrue(store.claim('d', 's', 200000))
            store.close()


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'project'; self.root.mkdir()
        self.state = Path(self.temp.name) / 'state'; self.state.mkdir()
        (self.root / 'data').mkdir()
        self.addCleanup(patch.stopall)
        patch.object(release, 'ROOT', self.root).start()
        patch.object(release, 'STATE', self.state).start()

    def test_stale_or_unready_heartbeat_rejected(self):
        f = self.root / 'data/runtime-health.json'
        f.write_text(json.dumps(dict(ts=time.time()-60, started=time.time(), ready=True, active=0, draining=False)))
        self.assertIsNone(release.health())
        f.write_text(json.dumps(dict(ts=time.time(), started=time.time()-20, ready=True, active=0, draining=False)))
        self.assertIsNotNone(release.health())
        self.assertIsNone(release.health(after=time.time()))
        self.assertIsNone(release.health(draining=True))

    def test_watchdog_restores_image_source_and_releases_drain_after_worker_death(self):
        (self.root / 'parser.py').write_text('broken')
        (self.root / 'new_test.py').write_text('new')
        (self.root / 'data/release.drain').touch()
        tx = dict(id='release', status='activating', deadline=time.time()-1, previous='sha256:old',
                  originals={'parser.py': 'working', 'new_test.py': None})
        release.atomic(self.state / 'transaction.json', tx)
        with patch.object(release, 'replace_image') as replace:
            self.assertEqual(release.watchdog()['status'], 'rolled_back')
            replace.assert_called_once_with('sha256:old')
        self.assertEqual((self.root / 'parser.py').read_text(), 'working')
        self.assertFalse((self.root / 'new_test.py').exists())
        self.assertFalse((self.root / 'data/release.drain').exists())
        with patch.object(release, 'replace_image') as replace:
            release.watchdog(); replace.assert_not_called()

    def test_sandbox_has_no_host_credentials_or_network(self):
        result = type('Result', (), {'returncode': 0})()
        with patch.object(release, 'run', return_value=result) as run:
            release.sandbox('image', Path('/candidate'), ['python', '-m', 'unittest'])
        args = run.call_args_list[0].args[0]
        self.assertIn('none', args)
        self.assertIn('65534:65534', args)
        self.assertIn('--read-only', args)
        self.assertNotIn('--env-file', args)
        self.assertFalse(any('docker.sock' in x or '/root/' in x for x in args))

    def candidate(self):
        request_id = 'a' * 32
        d = self.state / 'candidates' / request_id; d.mkdir(parents=True)
        source = {'parser.py': 'working'}
        (self.root / 'parser.py').write_text('working')
        release.atomic(d / 'prepared.json', dict(id=request_id, source=source_hash(source),
            image='candidate', previous='old', created=time.time(), changes={'parser.py': 'fixed'}))
        return request_id, source

    def test_failed_candidate_is_rolled_back_before_source_is_committed(self):
        request_id, source = self.candidate()
        result = type('Result', (), {'stdout': json.dumps([{'RestartCount': 1, 'State': {'Status': 'exited'}}])})()
        with patch.object(release, 'snapshot', return_value=source), \
             patch.object(release, 'image', return_value='old'), \
             patch.object(release, 'health', return_value={'active': 0}), \
             patch.object(release, 'replace_image') as replace, \
             patch.object(release, 'run', return_value=result), \
             patch.object(release.time, 'sleep'):
            with self.assertRaisesRegex(ValueError, 'process failed'):
                release.activate(request_id, 'b'*40)
        self.assertEqual([c.args[0] for c in replace.call_args_list], ['candidate', 'old'])
        self.assertEqual((self.root/'parser.py').read_text(), 'working')
        self.assertEqual(release.journal()['status'], 'rolled_back')
        self.assertFalse((self.root/'data/release.drain').exists())

    def test_active_download_prevents_replacement(self):
        request_id, source = self.candidate()
        clock = [time.time()]
        def advance(_): clock[0] += 60
        with patch.object(release, 'snapshot', return_value=source), \
             patch.object(release, 'image', return_value='old'), \
             patch.object(release, 'health', return_value={'active': 1}), \
             patch.object(release, 'replace_image') as replace, \
             patch.object(release.time, 'time', side_effect=lambda: clock[0]), \
             patch.object(release.time, 'sleep', side_effect=advance):
            with self.assertRaisesRegex(ValueError, 'Busy'):
                release.activate(request_id, 'b'*40)
            replace.assert_not_called()
        self.assertFalse((self.root/'data/release.drain').exists())

    def test_candidate_retention_deletes_only_old_owned_directory(self):
        root = self.state/'candidates';root.mkdir()
        for i in range(11):
            p=root/f'{i:032x}';p.mkdir()
            import os
            os.utime(p,(100+i,100+i))
        result=type('Result',(),{'stdout':'unused'})()
        with patch.object(release,'image',return_value='current'),patch.object(release,'run',return_value=result):
            release.prune_candidates()
        self.assertFalse((root/('0'*32)).exists())
        self.assertEqual(len(list(root.iterdir())),10)

    def test_healthy_candidate_commits_source_after_observation(self):
        request_id,source=self.candidate()
        clock=[time.time()]
        def advance(seconds):clock[0]+=seconds
        result=type('Result',(),{'stdout':json.dumps([{'RestartCount':0,'State':{'Status':'running'}}])})()
        with patch.object(release,'snapshot',return_value=source), \
             patch.object(release,'image',return_value='old'), \
             patch.object(release,'health',return_value={'active':0}), \
             patch.object(release,'replace_image') as replace, \
             patch.object(release,'run',return_value=result), \
             patch.object(release.time,'time',side_effect=lambda:clock[0]), \
             patch.object(release.time,'sleep',side_effect=advance):
            self.assertEqual(release.activate(request_id,'b'*40)['status'],'committed')
        replace.assert_called_once_with('candidate')
        self.assertEqual((self.root/'parser.py').read_text(),'fixed')
        self.assertEqual(release.journal()['status'],'committed')
        self.assertFalse((self.root/'data/release.drain').exists())


class DrainTests(unittest.IsolatedAsyncioTestCase):
    async def test_drain_blocks_new_work_but_existing_handler_finishes(self):
        with tempfile.TemporaryDirectory() as d:
            guard = RuntimeHealth(Path(d)); guard.ready = True
            event = AsyncMock(); handler = AsyncMock(return_value='done')
            self.assertEqual(await guard(handler, event, {}), 'done')
            self.assertEqual(guard.active, 0)
            (Path(d) / 'release.drain').touch()
            handler.reset_mock()
            await guard(handler, event, {})
            handler.assert_not_awaited(); event.answer.assert_awaited_once()
            guard.write()
            h = json.loads((Path(d) / 'runtime-health.json').read_text())
            self.assertTrue(h['draining']); self.assertTrue(h['ready'])


class ProviderTests(unittest.TestCase):
    def test_provider_cannot_overwrite_existing_log_or_follow_link(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'protected';path.write_text('keep')
            with self.assertRaises(FileExistsError):worker.exclusive_write(path,'overwrite')
            self.assertEqual(path.read_text(),'keep')
    def test_generator_receives_no_inherited_project_secret_and_saves_usage(self):
        with tempfile.TemporaryDirectory() as d:
            destination = Path(d)/'request'
            def provider(args, **kwargs):
                self.assertNotIn('PROJECT_TEST_SECRET', kwargs['env'])
                self.assertNotIn('PARENT_SESSION_ID', kwargs['env'])
                self.assertEqual(kwargs['env']['HTTPS_PROXY'], 'socks5h://proxy.example.org:1080')
                Path(args[2]).write_text(json.dumps(dict(summary='Insufficient evidence',files=[],test={'path':'','content':''})))
                return type('Result', (), {'returncode':0,'stdout':json.dumps({'type':'turn.completed','usage':{'input_tokens':12,'output_tokens':3}}),'stderr':''})()
            with patch.dict('os.environ', {'PROJECT_TEST_SECRET':'private-value','PARENT_SESSION_ID':'parent-session'}), \
                 patch.object(worker.subprocess,'run',side_effect=provider):
                result = worker.generate({'count':2},{'module':'clean'},['provider','{schema}','{answer}'],
                                         destination,proxy='socks5h://proxy.example.org:1080')
            self.assertEqual(result['files'],[])
            self.assertEqual(json.loads((destination/'usage.json').read_text())['input_tokens'],12)

    def test_dirty_checkout_stops_before_contacting_provider_or_server(self):
        with tempfile.TemporaryDirectory() as d, patch.object(worker,'PRIVATE',Path(d)), \
             patch.object(worker,'git',return_value=' M edited.py'), patch.object(worker,'Remote') as remote:
            self.assertEqual(worker.once({'public_root':d})['status'],'paused_dirty_checkout')
            remote.assert_not_called()

    def test_interrupted_attempt_pauses_instead_of_spending_again(self):
        with tempfile.TemporaryDirectory() as d:
            ledger=Ledger(Path(d)/'ledger.sqlite3');ledger.claim('a'*16,'source');ledger.close()
            with patch.object(worker,'PRIVATE',Path(d)),patch.object(worker,'git',return_value=''), \
                 patch.object(worker,'Remote') as remote:
                self.assertEqual(worker.once({'public_root':d})['status'],'paused_interrupted_attempt')
                self.assertTrue((Path(d)/'reconcile.json').exists())
                remote.assert_not_called()
