"""Cooperative maintenance cancellation using generated logs and local SQLite.

Cancellation interrupts SQL, but the connection still has to roll back before
queries can resume. These tests do not promise a hard real-time cancellation SLA.
"""
import datetime as dt
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import zipfile

from app import Store, make_server
from index_retention import MaintenanceControl
from retention import RetentionManager


CUTOFF = dt.datetime(2026, 9, 13, 2, tzinfo=dt.timezone.utc)


class RetentionCancelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='logscope cancel ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'data')
        self.addCleanup(lambda: self.store.pool.shutdown(wait=True))

    def imported(self, name):
        path = self.root / name
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('ns_pod/svc/pod-svc/log/root.log', ''.join(
                f'[2026-09-08 09:29:02.186 +0800] [{number}] [{number}] '
                f'[INFO] [worker] retention-cancel-marker {number}\n'
                for number in range(12)))
        identifier = self.store.submit(path, name)
        self.store.pool.submit(lambda: None).result(timeout=30)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT state FROM datasets WHERE id=?', (identifier,)).fetchone()[0], 'ready')
            db.execute('UPDATE datasets SET completed_at=? WHERE id=?', (CUTOFF.isoformat(), identifier))
        return identifier

    def state(self, identifier):
        with self.store.connect() as db:
            return db.execute('SELECT state FROM datasets WHERE id=?', (identifier,)).fetchone()[0]

    def zip_hash(self, identifier):
        return hashlib.sha256((self.store.directory / 'archives' / (identifier + '.zip')).read_bytes()).hexdigest()

    def assert_released(self):
        self.assertIsNone(self.store.maintenance_owner)
        self.assertEqual(self.store.connections, 0)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT 1').fetchone()[0], 1)

    def control(self, **kwargs):
        control = MaintenanceControl(**kwargs)
        self.addCleanup(control.finish)
        return control

    def test_cancel_before_start_preserves_every_index_and_archive(self):
        identifier = self.imported('cancel-before.zip')
        original_hash = self.zip_hash(identifier)
        control = self.control()
        control.cancel()
        result = self.store.expire_indexes(CUTOFF, control=control)
        self.assertTrue(result['cancelled'], result)
        self.assertFalse(result['timed_out'], result)
        self.assertFalse(result['deferred'], result)
        self.assertEqual(result['stop_reason'], 'cancelled')
        self.assertEqual(result['expired_count'], 0)
        self.assertEqual(control.reason, 'cancelled')
        self.assertTrue(control.snapshot()['cancel_requested'])
        self.assertEqual(self.state(identifier), 'ready')
        self.assertEqual(self.store.search({'dataset': identifier})['summary']['total'], 12)
        self.assertEqual(self.zip_hash(identifier), original_hash)
        self.assert_released()

    def test_progress_reports_all_phases(self):
        identifier = self.imported('progress.zip')
        events = []
        control = self.control(on_progress=lambda snapshot: events.append(dict(snapshot)))
        result = self.store.expire_indexes(CUTOFF, control=control)
        self.assertFalse(result['cancelled'], result)
        self.assertTrue(result['compacted'], result)
        stages = {event['stage'] for event in events}
        self.assertTrue({'checking', 'deleting', 'fts', 'vacuum', 'checkpoint', 'finishing'} <= stages, stages)
        for event in events:
            self.assertTrue({'stage', 'message', 'elapsed_seconds', 'completed', 'total', 'current', 'cancel_requested'} <= event.keys())
            self.assertGreaterEqual(event['elapsed_seconds'], 0)
        self.assertTrue(any(event['stage'] == 'deleting' and event['current'] == 'progress.zip' for event in events))
        self.assertEqual(self.state(identifier), 'expired')
        self.assert_released()

    def test_cancel_before_fts_keeps_committed_expiry_and_zip(self):
        identifier = self.imported('cancel-fts.zip')
        original_hash = self.zip_hash(identifier)

        def on_progress(snapshot):
            if snapshot['stage'] == 'fts':
                control.cancel()

        control = self.control(on_progress=on_progress)
        result = self.store.expire_indexes(CUTOFF, control=control)
        self.assertTrue(result['cancelled'], result)
        self.assertFalse(result['deferred'], result)
        self.assertEqual(result['expired_ids'], [identifier])
        self.assertEqual(self.state(identifier), 'expired')
        self.assertEqual(self.zip_hash(identifier), original_hash)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM logs WHERE dataset=?', (identifier,)).fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT value FROM log_metadata WHERE key='compaction_pending'").fetchone()[0], '1')
        self.assert_released()

    def test_second_package_cancellation_preserves_first_commit_and_second_index(self):
        identifiers = [self.imported(name) for name in ('first.zip', 'second.zip')]
        archives = {identifier: self.zip_hash(identifier) for identifier in identifiers}
        deleting = []

        def on_progress(snapshot):
            if snapshot['stage'] == 'deleting':
                deleting.append(snapshot['current'])
                if snapshot['completed'] == 1:
                    control.cancel()

        control = self.control(on_progress=on_progress)
        result = self.store.expire_indexes(CUTOFF, control=control)
        self.assertTrue(result['cancelled'], result)
        self.assertEqual(result['expired_count'], 1)
        self.assertEqual(len(result['expired_ids']), 1)
        self.assertEqual(len(set(deleting)), 2)
        for identifier in identifiers:
            expected = 'expired' if identifier in result['expired_ids'] else 'ready'
            self.assertEqual(self.state(identifier), expected)
            self.assertEqual(self.zip_hash(identifier), archives[identifier])
            if expected == 'ready':
                self.assertEqual(self.store.search({'dataset': identifier})['summary']['total'], 12)
        self.assert_released()

    def test_short_timeout_preserves_committed_expiry_and_marks_non_retrying_stop(self):
        identifier = self.imported('timeout.zip')
        original_hash = self.zip_hash(identifier)
        reached_fts = threading.Event()

        def on_progress(snapshot):
            if snapshot['stage'] == 'fts':
                reached_fts.set()
                # Let the real deadline expire while maintenance is active.
                # The separate CTE regression proves running SQL interruption.
                control.timer.join(timeout=3)

        control = self.control(timeout_seconds=.5, on_progress=on_progress)
        result = self.store.expire_indexes(CUTOFF, control=control)
        self.assertTrue(reached_fts.is_set())
        self.assertTrue(result['cancelled'], result)
        self.assertTrue(result['timed_out'], result)
        self.assertFalse(result['deferred'], result)
        self.assertEqual(result['stop_reason'], 'timeout')
        self.assertEqual(result['expired_ids'], [identifier])
        self.assertEqual(self.state(identifier), 'expired')
        self.assertEqual(self.zip_hash(identifier), original_hash)
        self.assert_released()

    def test_sql_interrupt_rolls_back_current_package_after_rows_begin_deleting(self):
        identifier = self.imported('rollback.zip')
        control = self.control()
        reached_delete = threading.Event()
        original_connect = sqlite3.connect

        def cancel_after_delete():
            reached_delete.set()
            control.cancel()
            return 1

        def connect(*args, **kwargs):
            db = original_connect(*args, **kwargs)
            db.create_function('cancel_after_delete', 0, cancel_after_delete)
            return db

        with self.store.connect() as db:
            db.execute('CREATE TRIGGER test_cancel AFTER DELETE ON logs BEGIN SELECT cancel_after_delete(); END')
        with mock.patch('app.sqlite3.connect', side_effect=connect):
            result = self.store.expire_indexes(CUTOFF, control=control)
        self.assertTrue(reached_delete.is_set(), 'fixture must reach a real SQLite DELETE')
        self.assertTrue(result['cancelled'], result)
        self.assertEqual(result['expired_count'], 0)
        self.assertEqual(self.state(identifier), 'ready')
        self.assertEqual(self.store.search({'dataset': identifier})['summary']['total'], 12)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM files WHERE dataset=?', (identifier,)).fetchone()[0], 1)
            db.execute('DROP TRIGGER test_cancel')
        self.assert_released()

    def test_running_sqlite_query_is_interrupted_by_cancel_and_timeout(self):
        for mode in ('cancelled', 'timeout'):
            with self.subTest(mode=mode):
                control = self.control(timeout_seconds=.3 if mode == 'timeout' else 10)
                executing = threading.Event()
                outcome = []
                connections = []

                def run_sql():
                    db = sqlite3.connect(':memory:')
                    connections.append(db)

                    def observed(value):
                        if value == 1000:
                            executing.set()
                        return value

                    db.create_function('observed', 1, observed)
                    try:
                        with control.bind(db):
                            db.execute('WITH RECURSIVE rows(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM rows WHERE x<100000000) SELECT sum(observed(x)) FROM rows').fetchone()
                        outcome.append('completed')
                    except Exception as exc:
                        outcome.append(exc)
                    finally:
                        db.close()
                        control.finish()

                worker = threading.Thread(target=run_sql, daemon=True)
                worker.start()
                try:
                    self.assertTrue(executing.wait(timeout=5), 'SQL must run, not merely be cancelled before execute')
                    if mode == 'cancelled':
                        control.cancel()
                    worker.join(timeout=5)
                    self.assertFalse(worker.is_alive(), 'SQLite VM did not respond to interruption')
                    self.assertEqual(control.reason, mode)
                    self.assertEqual(len(outcome), 1)
                    self.assertIsInstance(outcome[0], sqlite3.OperationalError)
                    self.assertIn('interrupt', str(outcome[0]).lower())
                finally:
                    if worker.is_alive():
                        control.cancel()
                        for db in connections:
                            db.interrupt()
                        worker.join(timeout=5)

    def test_http_busy_response_keeps_status_and_cancel_endpoints_available(self):
        with mock.patch('app.RetentionManager.start'):
            server = make_server(self.root / 'http-data', 0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = 'http://127.0.0.1:' + str(server.server_address[1])
        try:
            with server.store.access_lock:
                server.store.maintenance_owner = -1  # Another thread owns maintenance.
            with self.assertRaises(HTTPError) as error:
                urlopen(base + '/api/datasets', timeout=5)
            with error.exception as response:
                self.assertEqual(response.code, 503)
                self.assertEqual(json.load(response)['code'], 'INDEX_MAINTENANCE')
            with urlopen(base + '/api/retention', timeout=5) as response:
                self.assertTrue(json.load(response)['enabled'])
            request = Request(base + '/api/retention/cancel', data=b'{}', headers={'Content-Type': 'application/json'})
            with urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertIsInstance(json.load(response), dict)
        finally:
            with server.store.access_lock:
                server.store.maintenance_owner = None
            server.shutdown()
            server.server_close()
            worker.join(timeout=10)
            server.store.pool.shutdown(wait=True)

    def test_http_cancel_does_not_claim_queries_resumed_before_rollback_returns(self):
        identifier = self.imported('http-rollback.zip')
        with self.store.connect() as db:
            db.execute('CREATE TRIGGER test_http_cancel AFTER DELETE ON logs BEGIN SELECT observed_delete(); END')
        with mock.patch('app.RetentionManager.start'):
            server = make_server(self.store.directory, 0)
        server.retention.close()
        now = [CUTOFF + dt.timedelta(days=3, minutes=-1)]
        server.retention = RetentionManager(server.store, server.retention_busy, lambda: now[0])
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = 'http://127.0.0.1:' + str(server.server_address[1])
        deleting, allow_sql_return = threading.Event(), threading.Event()
        rolling_back, allow_rollback = threading.Event(), threading.Event()
        original_connect = sqlite3.connect

        def observed_delete():
            deleting.set()
            allow_sql_return.wait(timeout=5)
            return 1

        class RollbackPausedConnection(sqlite3.Connection):
            def rollback(self):
                rolling_back.set()
                allow_rollback.wait(timeout=5)
                return super().rollback()

        def connect(*args, **kwargs):
            db = original_connect(*args, factory=RollbackPausedConnection, **kwargs)
            db.create_function('observed_delete', 0, observed_delete)
            return db

        try:
            with mock.patch('app.sqlite3.connect', side_effect=connect):
                now[0] += dt.timedelta(minutes=1)
                server.retention.tick()
                self.assertTrue(deleting.wait(timeout=5))
                request = Request(base + '/api/retention/cancel', data=b'{}', headers={'Content-Type': 'application/json'})
                with urlopen(request, timeout=5) as response:
                    self.assertEqual(json.load(response)['phase'], 'cancelling')
                allow_sql_return.set()
                self.assertTrue(rolling_back.wait(timeout=5))
                with urlopen(base + '/api/retention', timeout=5) as response:
                    status = json.load(response)
                self.assertEqual(status['phase'], 'cancelling')
                self.assertTrue(status['maintenance'])
                with self.assertRaises(HTTPError) as error:
                    urlopen(base + '/api/datasets', timeout=5)
                with error.exception as response:
                    self.assertEqual(response.code, 503)
                    self.assertEqual(json.load(response)['code'], 'INDEX_MAINTENANCE')
                allow_rollback.set()
                server.store.pool.submit(lambda: None).result(timeout=10)
                with urlopen(base + '/api/retention', timeout=5) as response:
                    status = json.load(response)
                self.assertEqual(status['phase'], 'cancelled')
                self.assertFalse(status['maintenance'])
                with urlopen(base + '/api/datasets', timeout=5) as response:
                    packages = json.load(response)
                self.assertEqual(next(row['state'] for row in packages if row['id'] == identifier), 'ready')
        finally:
            allow_sql_return.set()
            allow_rollback.set()
            server.shutdown()
            server.server_close()
            worker.join(timeout=10)
            server.store.pool.shutdown(wait=True)


if __name__ == '__main__':
    unittest.main()
