"""Index expiry uses generated fixtures only; original ZIPs must stay intact."""
import datetime as dt
import hashlib
import json
from pathlib import Path
import random
import shutil
import tempfile
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import urlopen
import zipfile

from app import Store, make_server


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 9, 16, 2, 0, tzinfo=UTC)
CUTOFF = NOW - dt.timedelta(hours=72)


class IndexRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='logscope retention ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'data')
        self.addCleanup(lambda: self.store.pool.shutdown(wait=True))

    def imported(self, name='sample.zip', marker='synthetic-marker', count=2, padding=''):
        path = self.root / name
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('ns_pod/svc/pod-svc/log/root.log', ''.join(
                f'[2026-09-08 09:29:02.186 +0800] [{index + 1}] [{index + 1}] '
                f'[INFO] [worker] [Sample.java] [com.example] [run] [10] '
                f'{marker} {index} {padding}\n' for index in range(count)))
        identifier = self.store.submit(path, name)
        self.store.pool.submit(lambda: None).result(timeout=30)
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM datasets WHERE id=?', (identifier,)).fetchone()
        self.assertEqual(row['state'], 'ready', dict(row))
        self.assertTrue(row['completed_at'])
        return identifier

    def age(self, identifier, completed=CUTOFF, created=None):
        def text(value):
            return value.isoformat() if isinstance(value, dt.datetime) else value
        with self.store.connect() as db:
            db.execute('UPDATE datasets SET completed_at=?,created=? WHERE id=?',
                       (text(completed), text(created if created is not None else CUTOFF), identifier))

    def state(self, identifier):
        with self.store.connect() as db:
            return dict(db.execute('SELECT * FROM datasets WHERE id=?', (identifier,)).fetchone())

    def archive_hash(self, identifier):
        return hashlib.sha256((self.store.directory / 'archives' / (identifier + '.zip')).read_bytes()).hexdigest()

    def test_72_hour_boundary_uses_import_completion_and_legacy_created_fallback(self):
        cases = [
            ('exact', CUTOFF, CUTOFF - dt.timedelta(hours=1), 'expired'),
            ('young', CUTOFF + dt.timedelta(microseconds=1), CUTOFF - dt.timedelta(days=10), 'ready'),
            ('old', CUTOFF - dt.timedelta(microseconds=1), NOW, 'expired'),
            ('legacy', None, CUTOFF, 'expired'),
            ('legacy-young', None, CUTOFF + dt.timedelta(seconds=1), 'ready'),
            ('naive-legacy', None, CUTOFF.replace(tzinfo=None).isoformat(), 'expired'),
            ('invalid', None, 'not-a-date', 'ready'),
        ]
        identifiers = []
        for name, completed, created, state in cases:
            identifier = self.imported(name + '.zip')
            self.age(identifier, completed, created)
            identifiers.append((identifier, state))
        result = self.store.expire_indexes(CUTOFF)
        self.assertFalse(result['deferred'], result)
        self.assertEqual(result['expired_count'], 4)
        self.assertEqual(set(result['expired_ids']), {identifier for identifier, state in identifiers if state == 'expired'})
        for identifier, expected in identifiers:
            with self.subTest(identifier=identifier):
                self.assertEqual(self.state(identifier)['state'], expected)
                self.assertEqual(bool(self.state(identifier)['expired_at']), expected == 'expired')

    def test_expiry_preserves_zip_history_configuration_and_other_searchable_package(self):
        old = self.imported('old.zip', 'old-package-marker')
        current = self.imported('current.zip', 'current-package-marker')
        self.age(old)
        self.age(current, NOW)
        originals = {identifier: self.archive_hash(identifier) for identifier in (old, current)}
        preserved = {
            self.store.directory / 'chat.sqlite3': b'synthetic conversation database bytes',
            self.store.directory / 'ai-config.json': b'{"api_key":"synthetic-not-a-real-key"}',
            self.store.directory / 'retention-owner-note.txt': b'not part of a log index',
            self.store.directory / 'chat-sessions' / 'sample' / 'report.md': '已保存的排查报告'.encode(),
            self.store.directory / 'downloads' / 'download.zip': b'downloaded-original-fixture',
        }
        for path, content in preserved.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        result = self.store.expire_indexes(CUTOFF)
        self.assertEqual(result['expired_ids'], [old])
        self.assertTrue(result['compacted'], result)
        for identifier, expected in originals.items():
            self.assertEqual(self.archive_hash(identifier), expected)
        for path, content in preserved.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertEqual(self.store.search({'dataset': current, 'q': 'current-package-marker'})['summary']['total'], 2)
        with self.assertRaisesRegex(ValueError, '过期'):
            self.store.search({'dataset': old, 'q': 'old-package-marker'})
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM logs WHERE dataset=?', (old,)).fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM files WHERE dataset=?', (old,)).fetchone()[0], 0)
        path, name = self.store.original_archive(old)
        self.assertEqual(name, 'old.zip')
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), originals[old])

    def test_reimport_never_reuses_old_log_evidence_ids(self):
        old = self.imported('first.zip')
        before_ids = {row['id'] for row in self.store.search({'dataset': old})['rows']}
        self.age(old)
        original = self.store.directory / 'archives' / (old + '.zip')
        self.store.expire_indexes(CUTOFF)
        self.store.pool.shutdown(wait=True)
        self.store = Store(self.root / 'data')
        copy = self.root / 'reimport.zip'
        shutil.copyfile(original, copy)
        new = self.store.submit(copy, 'reimport.zip')
        self.store.pool.submit(lambda: None).result(timeout=30)
        after_ids = {row['id'] for row in self.store.search({'dataset': new})['rows']}
        self.assertGreater(min(after_ids), max(before_ids))
        self.assertTrue(before_ids.isdisjoint(after_ids))
        with self.assertRaises(ValueError):
            self.store.verify(min(before_ids))
        self.assertTrue(self.store.verify(min(after_ids))['verified'])

    def test_failed_partial_import_expires_by_creation_time_and_keeps_original_zip(self):
        identifier = self.imported('partial.zip', marker='partial-import-marker')
        original_hash = self.archive_hash(identifier)
        with self.store.connect() as db:
            db.execute("UPDATE datasets SET state='failed',created=?,completed_at=? WHERE id=?",
                       (CUTOFF.isoformat(), NOW.isoformat(), identifier))
        result = self.store.expire_indexes(CUTOFF)
        self.assertFalse(result['deferred'], result)
        self.assertEqual(result['expired_ids'], [identifier])
        self.assertEqual(self.state(identifier)['state'], 'expired')
        self.assertEqual(self.archive_hash(identifier), original_hash)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM logs WHERE dataset=?', (identifier,)).fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM files WHERE dataset=?', (identifier,)).fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM log_fts_v2 WHERE log_fts_v2 MATCH 'par'").fetchone()[0], 0)

    def test_record_dataset_binding_rejects_cross_package_and_expired_evidence(self):
        old = self.imported('old-evidence.zip')
        current = self.imported('current-evidence.zip')
        self.age(old)
        self.age(current, NOW)
        log_id = self.store.search({'dataset': current})['rows'][0]['id']
        self.assertEqual(self.store.record(log_id, current)['dataset'], current)
        with self.assertRaisesRegex(ValueError, '不属于'):
            self.store.record(log_id, old)

        with mock.patch('app.RetentionManager.start'):
            server = make_server(self.store.directory, 0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = 'http://127.0.0.1:' + str(server.server_address[1])
        try:
            with urlopen(base + f'/api/record?id={log_id}&dataset={current}', timeout=10) as response:
                self.assertEqual(json.load(response)['dataset'], current)
            for expected in ('不属于', '过期'):
                with self.assertRaises(HTTPError) as error:
                    urlopen(base + f'/api/record?id={log_id}&dataset={old}', timeout=10)
                with error.exception as response:
                    self.assertEqual(response.code, 400)
                    self.assertIn(expected, json.load(response)['error'])
                if expected == '不属于':
                    self.assertEqual(server.store.expire_indexes(CUTOFF)['expired_ids'], [old])
                    with self.assertRaisesRegex(ValueError, '过期'):
                        self.store.record(log_id, old)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=10)
            server.store.pool.shutdown(wait=True)

    def test_both_legacy_and_external_content_fts_expire_without_affecting_live_rows(self):
        current = self.imported('live.zip', 'retained-compact-marker')
        compact_old = self.imported('old-compact.zip', 'expired-compact-marker')
        self.age(current, NOW)
        self.age(compact_old)
        with self.store.connect() as db:
            db.execute("CREATE VIRTUAL TABLE log_fts USING fts5(raw, tokenize='trigram')")
            db.execute("INSERT INTO datasets(id,name,state,created,index_version) VALUES('legacy','legacy.zip','ready',?,1)", (CUTOFF.isoformat(),))
            file_id = db.execute("INSERT INTO files(dataset,node,pod,kind,filename) VALUES('legacy','n','p','root','root.log')").lastrowid
            log_id = db.execute("INSERT INTO logs(dataset,file_id,line,end_line,raw) VALUES('legacy',?,1,1,'expired-legacy-marker')", (file_id,)).lastrowid
            db.execute('INSERT INTO log_fts(rowid,raw) VALUES(?,?)', (log_id, 'expired-legacy-marker'))
            db.execute("DELETE FROM log_metadata WHERE key='last_log_id'")
        self.store.pool.shutdown(wait=True)
        self.store = Store(self.root / 'data')
        self.assertEqual(self.store.search({'dataset': 'legacy', 'q': 'expired-legacy-marker'})['summary']['total'], 1)
        result = self.store.expire_indexes(CUTOFF)
        self.assertFalse(result['deferred'], result)
        self.assertEqual(set(result['expired_ids']), {'legacy', compact_old})
        self.assertEqual(self.store.search({'dataset': current, 'q': 'retained-compact-marker'})['summary']['total'], 2)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM log_fts WHERE log_fts MATCH 'exp'").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM log_fts_v2 WHERE log_fts_v2 MATCH 'exp'").fetchone()[0], 0)
            self.assertGreaterEqual(int(db.execute("SELECT value FROM log_metadata WHERE key='last_log_id'").fetchone()[0]), log_id)

    def test_vacuum_reclaims_actual_database_bytes(self):
        identifier = self.imported('large.zip', count=350, padding='abcdefghij' * 1000)
        self.age(identifier)
        before = self.store.database.stat().st_size
        self.assertGreater(before, 3_000_000)
        result = self.store.expire_indexes(CUTOFF)
        after = self.store.database.stat().st_size
        self.assertFalse(result['deferred'], result)
        self.assertTrue(result['compacted'], result)
        self.assertLess(after, before // 3)
        self.assertGreater(result['reclaimed_bytes'], before // 2)
        self.assertTrue((self.store.directory / 'archives' / (identifier + '.zip')).exists())

    def test_unique_segmented_fts_postings_are_reclaimed_for_both_index_generations(self):
        # Repeating one line does not expose FTS tombstone/segment growth.
        # Multiple committed batches of distinct trigrams do: VACUUM without
        # FTS optimize can leave the empty database larger than before expiry.
        for table in ('log_fts_v2', 'log_fts'):
            with self.subTest(table=table):
                if table == 'log_fts':
                    with self.store.connect() as db:
                        db.execute("CREATE VIRTUAL TABLE log_fts USING fts5(raw, tokenize='trigram')")
                    self.store.fts_tables.append(table)
                    self.store.write_fts = table
                rng = random.Random(20260916)
                text = ''.join(
                    '[2026-09-08 09:29:02.186 +0800] [1] [1] [INFO] [worker] '
                    + ''.join(rng.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=500)) + '\n'
                    for _ in range(5000))
                path = self.root / (table + '.zip')
                with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                    archive.writestr('ns_pod/svc/pod-svc/log/root.log', text)
                with mock.patch('app.IMPORT_BATCH_SIZE', 500):
                    identifier = self.store.submit(path, path.name)
                    self.store.pool.submit(lambda: None).result(timeout=30)
                self.assertEqual(self.state(identifier)['state'], 'ready')
                self.assertEqual(self.state(identifier)['records'], 5000)
                self.age(identifier)
                before = self.store.database.stat().st_size
                self.assertGreater(before, 5_000_000)
                result = self.store.expire_indexes(CUTOFF)
                self.assertFalse(result['deferred'], result)
                self.assertTrue(result['compacted'], result)
                self.assertLess(self.store.database.stat().st_size, before // 3)
                self.assertGreater(result['reclaimed_bytes'], before // 2)

    def test_size_reporting_failures_always_release_maintenance_owner(self):
        identifier = self.imported()
        self.age(identifier)
        with mock.patch('index_retention.database_bytes', side_effect=OSError('synthetic size failure')):
            result = self.store.expire_indexes(CUTOFF)
        self.assertTrue(result['deferred'], result)
        self.assertIsNone(self.store.maintenance_owner)
        self.assertEqual(self.store.connections, 0)
        self.assertEqual(self.state(identifier)['state'], 'ready')

        with mock.patch('index_retention.database_bytes', side_effect=[self.store.database.stat().st_size, OSError('synthetic final size failure')]):
            result = self.store.expire_indexes(CUTOFF)
        self.assertFalse(result['deferred'], result)
        self.assertEqual(self.state(identifier)['state'], 'expired')
        self.assertIsNone(self.store.maintenance_owner)
        self.assertEqual(self.store.connections, 0)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT 1').fetchone()[0], 1)

    def test_busy_and_open_connection_defer_without_deleting_any_rows(self):
        identifier = self.imported()
        self.age(identifier)
        for mode in ('ai', 'connection'):
            with self.subTest(mode=mode):
                if mode == 'ai':
                    result = self.store.expire_indexes(CUTOFF, busy=lambda: True)
                else:
                    with self.store.connect():
                        result = self.store.expire_indexes(CUTOFF)
                self.assertTrue(result['deferred'], result)
                self.assertEqual(result['expired_count'], 0)
                self.assertEqual(self.state(identifier)['state'], 'ready')
                self.assertIsNone(self.store.maintenance_owner)
        self.assertEqual(self.store.connections, 0)
        self.assertEqual(self.store.expire_indexes(CUTOFF)['expired_count'], 1)

    def test_queued_import_and_delete_defer_maintenance(self):
        identifier = self.imported()
        self.age(identifier)
        for state in ('importing', 'deleting'):
            with self.store.connect() as db:
                db.execute("INSERT INTO datasets(id,name,state,created) VALUES('queued','queued.zip',?,?)", (state, CUTOFF.isoformat()))
            result = self.store.expire_indexes(CUTOFF)
            self.assertTrue(result['deferred'], result)
            self.assertEqual(self.state(identifier)['state'], 'ready')
            self.assertIsNone(self.store.maintenance_owner)
            with self.store.connect() as db:
                db.execute("DELETE FROM datasets WHERE id='queued'")

    def test_disk_shortage_commits_expiry_and_retries_compaction_after_restart(self):
        identifier = self.imported()
        self.age(identifier)
        original_hash = self.archive_hash(identifier)
        real_usage = shutil.disk_usage(self.store.directory)
        with mock.patch('index_retention.shutil.disk_usage', return_value=real_usage._replace(free=0)):
            result = self.store.expire_indexes(CUTOFF)
        self.assertTrue(result['deferred'], result)
        self.assertEqual(result['expired_count'], 1)
        self.assertFalse(result['compacted'])
        self.assertEqual(self.state(identifier)['state'], 'expired')
        self.assertEqual(self.archive_hash(identifier), original_hash)
        self.store.pool.shutdown(wait=True)
        self.store = Store(self.root / 'data')
        self.assertEqual(self.state(identifier)['state'], 'expired')
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT value FROM log_metadata WHERE key='compaction_pending'").fetchone()[0], '1')
        retry = self.store.expire_indexes(CUTOFF)
        self.assertFalse(retry['deferred'], retry)
        self.assertEqual(retry['expired_count'], 0)
        self.assertTrue(retry['compacted'], retry)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT value FROM log_metadata WHERE key='compaction_pending'").fetchone()[0], '0')

    def test_queries_from_other_threads_fail_fast_during_compaction(self):
        identifier = self.imported()
        self.age(identifier)
        real_usage = shutil.disk_usage(self.store.directory)
        failures = []

        def attempt_query():
            try:
                self.store.search({'dataset': identifier})
            except ValueError as exc:
                failures.append(str(exc))

        def during_compaction(_):
            worker = threading.Thread(target=attempt_query)
            worker.start()
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive(), 'query blocked behind VACUUM')
            return real_usage

        with mock.patch('index_retention.shutil.disk_usage', side_effect=during_compaction):
            result = self.store.expire_indexes(CUTOFF)
        self.assertFalse(result['deferred'], result)
        self.assertEqual(len(failures), 1)
        self.assertIn('清理', failures[0])
        self.assertEqual(self.store.connections, 0)
        self.assertIsNone(self.store.maintenance_owner)

    def test_http_retention_status_and_expired_archive_download(self):
        # Scheduler tests exercise the background timer independently. Do not
        # let the wall clock race this endpoint's deliberately aged fixture.
        with mock.patch('app.RetentionManager.start'):
            server = make_server(self.root / 'http-data', 0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = 'http://127.0.0.1:' + str(server.server_address[1])
        original_name = '节点 A 日志.zip'
        upload = self.root / 'http.zip'
        with zipfile.ZipFile(upload, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('ns_pod/svc/pod-svc/log/root.log',
                             '[2026-09-08 09:29:02.186 +0800] [1] [1] [INFO] [worker] http-fixture\n')
        original_bytes = upload.read_bytes()
        try:
            identifier = server.store.submit(upload, original_name)
            server.store.pool.submit(lambda: None).result(timeout=30)
            with server.store.connect() as db:
                db.execute('UPDATE datasets SET completed_at=? WHERE id=?', (CUTOFF.isoformat(), identifier))
            result = server.store.expire_indexes(CUTOFF)
            self.assertEqual(result['expired_count'], 1)
            with urlopen(base + '/api/retention', timeout=10) as response:
                status = json.load(response)
            self.assertTrue(status['enabled'])
            self.assertEqual(status['hours'], 72)
            self.assertEqual(status['schedule'], '02:00')
            with urlopen(base + '/api/archives/download?dataset=' + identifier, timeout=10) as response:
                self.assertEqual(response.headers.get_content_type(), 'application/zip')
                self.assertIn("filename*=UTF-8''" + quote(original_name, safe=''), response.headers['Content-Disposition'])
                self.assertEqual(int(response.headers['Content-Length']), len(original_bytes))
                self.assertEqual(response.read(), original_bytes)
            with self.assertRaises(HTTPError) as error:
                urlopen(base + '/api/archives/download?dataset=../ai-config', timeout=10)
            with error.exception as response:
                self.assertEqual(response.code, 400)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=10)
            server.store.pool.shutdown(wait=True)


if __name__ == '__main__':
    unittest.main()
