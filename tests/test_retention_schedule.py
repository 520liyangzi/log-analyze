import concurrent.futures
import datetime as dt
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from retention import RetentionManager


ZONE = dt.timezone(dt.timedelta(hours=8))


def at(day=16, hour=2, minute=0):
    return dt.datetime(2026, 9, day, hour, minute, tzinfo=ZONE)


def result(deferred=False, **values):
    return dict(deferred=deferred, expired_count=values.get('expired_count', 1),
                reclaimed_bytes=values.get('reclaimed_bytes', 1234), compacted=not deferred,
                reason='使用中，稍后重试' if deferred else '', expired_ids=['example'])


class Pool:
    def __init__(self):
        self.queued = []

    def submit(self, function, *args):
        future = concurrent.futures.Future()
        self.queued.append((future, function, args))
        return future

    def run(self):
        future, function, args = self.queued.pop(0)
        if future.set_running_or_notify_cancel():
            try:
                future.set_result(function(*args))
            except BaseException as exc:
                future.set_exception(exc)
                raise


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.pool = Pool()
        self.calls = []
        self.results = [result()]

    def expire_indexes(self, cutoff, busy):
        self.calls.append((cutoff, busy))
        value = self.results.pop(0) if self.results else result()
        if isinstance(value, Exception):
            raise value
        return value


class RetentionScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(self.temporary.name)
        self.now = at()
        self.busy = mock.Mock(return_value=False)
        self.manager = self.create_manager()

    def create_manager(self):
        manager = RetentionManager(self.store, self.busy, lambda: self.now)
        self.addCleanup(manager.close)
        return manager

    def finish(self):
        self.manager.tick()
        self.store.pool.run()

    def test_local_two_utc_cutoff_and_once_per_day(self):
        self.now = at(15)
        self.finish()
        self.assertEqual(self.store.calls[0], (at(12).astimezone(dt.timezone.utc), self.busy))
        self.assertEqual(self.store.calls[0][0].tzinfo, dt.timezone.utc)
        self.now = at(16, 1, 59)
        self.manager.tick()
        self.assertEqual(len(self.store.pool.queued), 0)
        self.assertEqual(self.manager.status()['next_run'], at(16).isoformat())
        self.now = at(16)
        self.manager.tick()
        self.manager.tick()
        self.assertEqual(len(self.store.pool.queued), 1)
        self.store.pool.run()
        self.manager.tick()
        self.assertEqual(len(self.store.calls), 2)
        self.assertEqual(len(self.store.pool.queued), 0)

    def test_restart_catches_up_latest_missed_day_and_preserves_completion(self):
        self.finish()
        self.manager.close()
        self.manager = self.create_manager()
        self.manager.tick()
        self.assertEqual(len(self.store.pool.queued), 0)
        self.now = at(19, 14)
        self.finish()
        saved = json.loads(self.manager.path.read_text('utf-8'))
        self.assertEqual(saved['last_completed_day'], '2026-09-19')
        self.assertEqual(saved['result']['expired_count'], 1)
        self.assertEqual(self.manager.status()['next_run'], at(20).isoformat())

    def test_start_before_two_catches_up_yesterday_without_skipping_today(self):
        self.now = at(16, 1)
        self.finish()
        saved = json.loads(self.manager.path.read_text('utf-8'))
        self.assertEqual(saved['last_completed_day'], '2026-09-15')
        self.assertEqual(self.manager.status()['next_run'], at(16).isoformat())
        self.now = at(16)
        self.finish()
        self.assertEqual(len(self.store.calls), 2)

    def test_busy_or_compaction_deferred_retries_after_fifteen_minutes(self):
        self.store.results = [result(True), result()]
        self.finish()
        status = self.manager.status()
        self.assertEqual(status['phase'], 'deferred')
        self.assertEqual(status['next_run'], at(16, 2, 15).isoformat())
        saved = json.loads(self.manager.path.read_text('utf-8'))
        self.assertIsNone(saved['last_completed_day'])
        self.manager.close()
        self.manager = self.create_manager()
        self.now = at(16, 2, 14)
        self.manager.tick()
        self.assertEqual(len(self.store.pool.queued), 0)
        self.now = at(16, 2, 15)
        self.finish()
        self.assertEqual(self.manager.status()['phase'], 'idle')
        self.assertEqual(self.manager.status()['next_run'], at(17).isoformat())

    def test_cleanup_exception_is_saved_and_retry_remains_available(self):
        self.store.results = [RuntimeError('database is locked'), result()]
        self.finish()
        status = self.manager.status()
        self.assertIn('database is locked', status['message'])
        self.assertEqual(status['phase'], 'error')
        self.assertTrue(status['last_result']['deferred'])
        self.assertIsNone(self.manager._last_completed_day)
        self.now = at(16, 2, 15)
        self.finish()
        self.assertEqual(self.manager.status()['phase'], 'idle')

    def test_state_write_failure_does_not_mark_day_completed(self):
        with mock.patch.object(self.manager, '_write', side_effect=OSError('read only')):
            self.finish()
        self.assertEqual(self.manager.status()['phase'], 'error')
        self.assertIn('状态文件写入失败', self.manager.status()['message'])
        self.assertIsNone(self.manager._last_completed_day)
        self.assertFalse(self.manager.path.exists())
        self.now = at(16, 2, 15)
        self.finish()
        self.assertEqual(self.manager._last_completed_day, self.now.date())

    def test_corrupt_state_reports_problem_and_allows_repair(self):
        self.manager.path.write_text('{broken', 'utf-8')
        self.manager.close()
        self.manager = self.create_manager()
        self.assertIn('状态文件损坏', self.manager.status()['message'])
        self.finish()
        self.assertEqual(json.loads(self.manager.path.read_text('utf-8'))['last_completed_day'], '2026-09-16')

    def test_close_cancels_queued_job_and_rejects_future_ticks(self):
        self.manager.tick()
        self.manager.close()
        self.store.pool.run()
        self.assertEqual(self.store.calls, [])
        self.now = at(17)
        self.manager.tick()
        self.manager.start()
        self.assertEqual(self.store.pool.queued, [])
        self.assertEqual(self.manager.status()['phase'], 'stopped')

    def test_status_is_database_free_and_returns_detached_result(self):
        self.finish()
        count = len(self.store.calls)
        snapshot = self.manager.status()
        snapshot['last_result']['expired_ids'].append('altered')
        self.assertEqual(self.manager.status()['last_result']['expired_ids'], ['example'])
        self.assertEqual(len(self.store.calls), count)
        self.assertEqual(snapshot['hours'], 72)
        self.assertEqual(snapshot['schedule'], '02:00')
        self.assertTrue(snapshot['enabled'])

    def test_start_is_idempotent_and_close_stops_scheduler_thread(self):
        reached = threading.Event()
        original_tick = self.manager.tick

        def tick():
            value = original_tick()
            reached.set()
            return value

        with mock.patch.object(self.manager, 'tick', side_effect=tick):
            self.manager.start()
            self.assertTrue(reached.wait(2))
            worker = self.manager._thread
            self.manager.start()
            self.assertIs(worker, self.manager._thread)
            self.manager.close()
            self.assertFalse(worker.is_alive())
        self.assertEqual(len(self.store.pool.queued), 1)
        self.store.pool.run()
        self.assertEqual(self.store.calls, [])

    def test_late_queued_execution_uses_current_time_for_expiry(self):
        self.manager.tick()
        self.now = at(16, 4)
        self.store.pool.run()
        self.assertEqual(self.store.calls[0][0], at(13, 4).astimezone(dt.timezone.utc))

    def test_queue_crossing_next_night_completes_latest_due_day(self):
        self.manager.tick()
        self.now = at(17, 3)
        self.store.pool.run()
        self.manager.tick()
        self.assertEqual(self.store.pool.queued, [])
        self.assertEqual(self.manager.status()['next_run'], at(18).isoformat())

    def test_submit_failure_clears_pending_and_allows_retry(self):
        with mock.patch.object(self.store.pool, 'submit', side_effect=RuntimeError('pool busy')):
            status = self.manager.tick()
        self.assertEqual(status['phase'], 'error')
        self.assertIn('无法安排自动清理', status['message'])
        self.assertIsNone(self.manager._pending)
        self.now = at(16, 2, 15)
        self.finish()
        self.assertEqual(self.manager.status()['phase'], 'idle')

    def test_non_object_result_is_retried_instead_of_marking_complete(self):
        self.store.results = ['invalid']
        self.finish()
        self.assertEqual(self.manager.status()['phase'], 'error')
        self.assertIsNone(self.manager._last_completed_day)
        self.assertIn('无效的清理结果', self.manager.status()['message'])

    def test_explicit_tick_time_is_supported(self):
        self.manager.tick(at(17, 5))
        self.store.pool.run()
        self.assertEqual(self.store.calls[0][0], at(14, 5).astimezone(dt.timezone.utc))


if __name__ == '__main__':
    unittest.main()
