import json
import os
import sqlite3
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from animedl import observability as audit
from animedl.handlers.admin import cmd_audit_errors, cmd_audit_stats


class AuditTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "audit.db"
        self.store = audit.AuditStore(self.path, max_events=100)

    async def asyncTearDown(self):
        await self.store.close()
        self.tmp.cleanup()

    def test_no_secrets_and_stable_error_groups(self):
        token = "123456:abcdefghijklmnopqrstuvwxy"
        exc = ValueError("https://private.invalid/?token=SECRET " + token)
        details = audit.error_details(exc)
        self.store.record("source_error", 123, "error", **details,
            title="Test https://private.invalid/?token=SECRET " + token,
            headers={"Authorization": "SECRET"}, url="https://SECRET", password="SECRET")
        raw = self.store.db.execute("SELECT details FROM audit_events").fetchone()[0]
        self.assertNotIn("SECRET", raw)
        self.assertNotIn(token, raw)
        self.assertNotIn("headers", raw)
        self.assertEqual(details["fingerprint"], audit.error_details(ValueError("other secret"))["fingerprint"])
        self.assertNotEqual(details["fingerprint"], audit.error_details(TypeError("same"))["fingerprint"])
        self.assertEqual(audit.error_details(RuntimeError("HTTP Error 403 SECRET"))["http_status"], 403)
        self.assertNotEqual(audit.error_details(RuntimeError("HTTP Error 403"))["fingerprint"],
                            audit.error_details(RuntimeError("HTTP Error 500"))["fingerprint"])

    async def test_durable_pending_journal_and_strict_limit(self):
        for i in range(130):
            self.store.record("episode_started", 1, title=f"Title {i}")
        self.assertEqual(self.store.stats()["total"], 100)
        self.assertEqual(self.store.stats()["users"], 1)
        await self.store.close()
        self.store = audit.AuditStore(self.path, dsn="postgresql://dummy", max_events=100)
        self.assertEqual(self.store.stats()["pending"], 100)
        self.store.record("after_restart", 2)
        self.assertEqual(self.store.stats()["total"], 100)
        self.assertEqual(self.store.stats()["dropped"], 1)

    def test_retention_and_finished_episode_stats(self):
        self.store.record("episode_finished", 4, "sent_new", bytes=1024, title="Movie")
        self.store.record("episode_finished", 5, "error", bytes=0)
        self.store.db.execute("UPDATE audit_events SET ts=? WHERE user_id=5", (time.time() - 40 * 86400,))
        self.store.db.commit()
        self.store.prune()
        self.assertEqual(self.store.stats()["jobs"], [("sent_new", 1, 1024)])

    async def test_db_mirror_failure_does_not_drop_or_block_actions(self):
        self.store.dsn = "postgresql://dummy"
        self.store.flush = AsyncMock(side_effect=ConnectionError("secret DSN"))
        self.store.record("input", 7)
        notifier = AsyncMock()
        await self.store.start(notifier)
        await __import__("asyncio").sleep(0.02)
        self.assertEqual(self.store.stats()["pending"], 1)
        self.assertTrue(self.store._db_failed)

    async def test_mirror_uses_stable_ids_and_marks_only_after_success(self):
        self.store.dsn = "postgresql://dummy"
        self.store.record("test", 7)
        saved_id = self.store.db.execute("SELECT id FROM audit_events").fetchone()[0]
        connection = SimpleNamespace(execute=AsyncMock(), executemany=AsyncMock(side_effect=ConnectionError))
        # Python looks up async context manager methods on the type.
        class Acquire:
            async def __aenter__(self):
                return connection
            async def __aexit__(self, *args):
                return False
        self.store.pool = SimpleNamespace(acquire=Acquire, close=AsyncMock())
        with self.assertRaises(ConnectionError):
            await self.store.flush()
        self.assertEqual(self.store.stats()["pending"], 1)
        connection.executemany.side_effect = None
        await self.store.flush()
        self.assertEqual(self.store.stats()["pending"], 0)
        self.assertEqual(connection.executemany.call_args.args[1][0][0], saved_id)
        self.assertIn("ON CONFLICT(id) DO NOTHING", connection.executemany.call_args.args[0])

    async def test_error_alert_dedup_and_safe_text(self):
        self.store.notifier = AsyncMock()
        self.store._last_alert_scan = 0
        details = audit.error_details(ValueError("PRIVATE"))
        self.store.record("source_error", 1, "error", **details)
        self.store.record("source_error", 2, "error", **details)
        await self.store._alerts()
        self.store.notifier.assert_awaited_once()
        self.assertNotIn("PRIVATE", self.store.notifier.call_args.args[0])
        self.assertEqual(self.store.errors()[0][3], 2)

    async def test_owner_only_commands(self):
        message = SimpleNamespace(from_user=SimpleNamespace(id=2), answer=AsyncMock())
        cfg = SimpleNamespace(admin_id=1)
        await cmd_audit_stats(message, cfg, self.store)
        await cmd_audit_errors(message, cfg, self.store)
        message.answer.assert_not_awaited()
        message.from_user.id = 1
        await cmd_audit_stats(message, cfg, self.store)
        message.answer.assert_awaited_once()

    async def test_local_journal_failure_is_fail_open(self):
        self.store.db.close()
        self.store.record("input", 1)
        # Replace the closed handle so cleanup and the assertion can run.
        self.store.db = sqlite3.connect(self.path)
        self.assertTrue(self.store._local_failed)

    async def test_middleware_tracks_safe_metadata_and_error(self):
        audit._current = self.store
        state = SimpleNamespace(get_state=AsyncMock(return_value="DownloadFlow:choosing_quality"),
            get_data=AsyncMock(return_value={"title": "Movie", "quality": 1080, "password": "PRIVATE"}))
        message = SimpleNamespace(from_user=SimpleNamespace(id=12), text="https://example.invalid/?token=PRIVATE")
        handler = AsyncMock(side_effect=ValueError("PRIVATE"))
        with self.assertRaises(ValueError):
            await audit.AuditMiddleware()(handler, message, {"state": state})
        rows = self.store.db.execute("SELECT action,user_id,details FROM audit_events ORDER BY ts").fetchall()
        self.assertEqual([r[0] for r in rows], ["link", "handler_error", "action_finished"])
        self.assertEqual(json.loads(rows[-1][2])["quality"], 1080)
        self.assertTrue(all(r[1] == 12 and "PRIVATE" not in r[2] for r in rows))
        self.assertIsNone(audit._user.get())

    @unittest.skipUnless(os.environ.get("AUDIT_TEST_DATABASE_URL"), "set AUDIT_TEST_DATABASE_URL for isolated real PostgreSQL test")
    async def test_real_postgresql_mirror_cap_reports_and_idempotent_retry(self):
        import asyncpg
        dsn = os.environ["AUDIT_TEST_DATABASE_URL"]
        schema = "audit_test_" + uuid.uuid4().hex
        admin = await asyncpg.connect(dsn, timeout=5)
        try:
            # A unique schema prevents any modification of production audit tables.
            await admin.execute(f'CREATE SCHEMA "{schema}"')
            self.store.dsn = dsn
            self.store.max_events = 500
            self.store.pg_max_events = 100
            self.store.pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2,
                server_settings={"search_path": schema}, command_timeout=5)
            for i in range(245):
                self.store.record("episode_started", i % 3, title="Movie", provider="Alloha", quality=1080)
            self.store.record("episode_finished", 1, "sent_new", bytes=12345, title="Movie", provider="Alloha", quality=1080)
            self.store.record("source_error", 2, "error", **audit.error_details(RuntimeError("HTTP Error 403 SECRET")))
            await self.store.flush()
            self.assertEqual(self.store.stats()["pending"], 0)
            report = await self.store.report()
            self.assertEqual(report["total"], 100)
            self.assertIn("PostgreSQL", report["source"])
            self.assertEqual(report["jobs"][0][2], 12345)
            self.assertEqual(report["providers"][0][0], "Alloha")
            self.assertEqual(report["top_users"][0][0], 1)
            rows, source = await self.store.error_report()
            self.assertEqual(rows[0][5], "403")
            self.assertIn("PostgreSQL", source)
            latest = self.store.db.execute("SELECT id FROM audit_events ORDER BY ts DESC LIMIT 1").fetchone()[0]
            self.store.db.execute("UPDATE audit_events SET mirrored=0 WHERE id=?", (latest,))
            self.store.db.commit()
            await self.store.flush()
            self.assertEqual((await self.store.report())["total"], 100)
        finally:
            if self.store.pool:
                await self.store.pool.close()
                self.store.pool = None
            await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            await admin.close()
