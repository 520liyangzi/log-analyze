"""Scheduler tests use only generated state, a stub Store, and a fake controller."""
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
    return dict(deferred=deferred, cancelled=False, timed_out=False,
                expired_count=values.get('expired_count', 1),
                reclaimed_bytes=values.get('reclaimed_bytes', 1234),
                compacted=not deferred, reason='使用中，稍后重试' if deferred else '',
                expired_ids=['example'])


class FakeControl:
    instances = []

    def __init__(self, timeout_seconds=300, on_progress=None):
        self.timeout_seconds = timeout_seconds
        self.on_progress = on_progress
        self.reason = None
        self.stage = 'checking'
        self.finished = False
        self.cancel_event = threading.Event()
        self.cancel_hook = None
        self.instances.append(self)

    def snapshot(self):
        return dict(stage=self.stage, message='测试阶段：' + self.stage, elapsed_seconds=0,
                    completed=0, total=1, current='example', cancel_requested=self.reason is not None)

    def progress(self, stage):
        self.stage = stage
        if self.on_progress:
            self.on_progress(self.snapshot())

    def cancel(self, reason='cancelled'):
        if self.reason is None:
            self.reason = reason
        self.cancel_event.set()
        if self.cancel_hook:
            self.cancel_hook()
        self.progress('cancelling')

    def finish(self):
        self.finished = True


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
        self.results = []
        self.maintenance_owner = None

    def expire_indexes(self, cutoff, busy, control=None):
        self.calls.append((cutoff, busy, control))
        self.maintenance_owner = threading.get_ident()
        try:
            control.progress('deleting')
            value = self.results.pop(0) if self.results else result()
            if isinstance(value, Exception):
                raise value
            return value(control) if callable(value) else value
        finally:
            self.maintenance_owner = None


class RetentionScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(self.temporary.name)
        self.now = at(16, 1)
        self.busy = mock.Mock(return_value=False)
        FakeControl.instances = []
        patcher = mock.patch('retention.index_retention.MaintenanceControl', FakeControl, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.manager = self.create_manager()

    def create_manager(self):
        manager = RetentionManager(self.store, self.busy, lambda: self.now)
        self.addCleanup(manager.close)
        return manager

    def restart(self):
        self.manager.close()
        self.manager = self.create_manager()

    def finish(self):
        self.manager.tick()
        self.assertEqual(len(self.store.pool.queued), 1)
        self.store.pool.run()

    def test_default_config_is_local_and_first_start_never_catches_up(self):
        self.assertEqual(json.loads(self.manager.config_path.read_text('utf-8')),
                         {'enabled': True, 'startup_catchup': False, 'max_run_seconds': 300})
        for hour in (1, 2, 14, 23):
            with self.subTest(hour=hour):
                self.now = at(16, hour)
                self.restart()
                self.manager.tick()
                self.assertEqual(self.store.pool.queued, [])
                expected = at(16) if hour < 2 else at(17)
                self.assertEqual(self.manager.status()['next_run'], expected.isoformat())
                self.assertFalse(self.manager.status()['startup_catchup'])

    def test_local_two_utc_cutoff_and_once_per_day(self):
        self.manager.tick()
        self.assertEqual(self.store.pool.queued, [])
        self.now = at(16, 1, 59)
        self.manager.tick()
        self.assertEqual(self.store.pool.queued, [])
        self.now = at(16)
        self.manager.tick()
        self.manager.tick()
        self.assertEqual(len(self.store.pool.queued), 1)
        self.assertEqual(FakeControl.instances, [])
        self.store.pool.run()
        self.assertEqual(self.store.calls[0][:2], (at(13).astimezone(dt.timezone.utc), self.busy))
        self.assertEqual(self.store.calls[0][0].tzinfo, dt.timezone.utc)
        self.assertEqual(FakeControl.instances[0].timeout_seconds, 300)
        self.assertTrue(FakeControl.instances[0].finished)
        self.manager.tick()
        self.assertEqual(self.store.pool.queued, [])
        self.assertEqual(self.manager.status()['next_run'], at(17).isoformat())

    def test_restart_in_night_window_does_not_immediately_resume(self):
        self.now = at(16, 2, 10)
        self.restart()
        self.manager.tick()
        self.assertEqual(self.store.pool.queued, [])
        self.now = at(17)
        self.finish()
        self.assertEqual(self.manager._last_completed_day, self.now.date())

    def test_missed_window_is_skipped_until_next_day(self):
        self.now = at(16, 3)
        self.manager.tick()
        self.assertEqual(self.store.pool.queued, [])
        self.now = at(16, 14)
        self.manager.tick()
        self.assertEqual(self.store.pool.queued, [])
        self.assertEqual(self.manager.status()['next_run'], at(17).isoformat())
        self.now = at(17, 2, 1)
        self.finish()

    def test_saved_old_retry_cannot_restore_daytime_work(self):
        self.manager.path.write_text(json.dumps(dict(
            last_completed_day='2026-09-15', last_run=at(16, 13).isoformat(),
            result=result(True), retry_at=at(16, 13, 15).isoformat())), 'utf-8')
        self.now = at(16, 14)
        self.restart()
        self.manager.tick()
        self.assertEqual(self.store.pool.queued, [])
        self.assertIsNone(self.manager._retry_at)
        self.assertEqual(self.manager.status()['next_run'], at(17).isoformat())

    def test_damaged_or_missing_state_never_triggers_daytime_catchup(self):
        self.manager.path.write_text('{broken', 'utf-8')
        self.now = at(16, 14)
        self.restart()
        self.assertIn('状态文件损坏', self.manager.status()['message'])
        self.manager.tick()
        self.assertEqual(self.store.pool.queued, [])
        self.now = at(17)
        self.finish()
        self.assertEqual(json.loads(self.manager.path.read_text('utf-8'))['last_completed_day'], '2026-09-17')

    def test_explicit_startup_catchup_runs_once_and_preserves_configuration(self):
        self.manager.config_path.write_text(json.dumps(dict(
            enabled=True, startup_catchup=True, max_run_seconds=120)), 'utf-8')
        self.now = at(16, 14)
        self.restart()
        self.finish()
        self.manager.tick()
        self.assertEqual(self.store.pool.queued, [])
        self.assertEqual(FakeControl.instances[0].timeout_seconds, 120)
        self.assertTrue(self.manager.status()['startup_catchup'])
        self.assertEqual(self.manager.status()['next_run'], at(17).isoformat())
        self.restart()
        self.manager.tick()
        self.assertEqual(self.store.pool.queued, [])

    def test_disabled_or_invalid_config_does_not_schedule(self):
        for config in ({'enabled': False}, {'startup_catchup': 'yes'}, {'max_run_seconds': 0}):
            with self.subTest(config=config):
                self.manager.config_path.write_text(json.dumps(config), 'utf-8')
                self.now = at(16, 1)
                self.restart()
                self.now = at(16)
                self.manager.tick()
                self.assertFalse(self.manager.status()['enabled'])
                self.assertIsNone(self.manager.status()['next_run'])
                self.assertEqual(self.store.pool.queued, [])

    def test_busy_retries_after_fifteen_minutes_only_inside_night_window(self):
        self.store.results = [result(True), result(True), result(True), result(True)]
        self.now = at(16)
        self.finish()
        self.assertEqual(self.manager.status()['next_run'], at(16, 2, 15).isoformat())
        self.now = at(16, 2, 14)
        self.manager.tick()
        self.assertEqual(self.store.pool.queued, [])
        for minute in (15, 30, 45):
            self.now = at(16, 2, minute)
            self.finish()
        self.assertIsNone(self.manager._retry_at)
        self.assertEqual(self.manager.status()['next_run'], at(17).isoformat())
        self.now = at(16, 3)
        self.manager.tick()
        self.assertEqual(self.store.pool.queued, [])

    def test_daytime_catchup_deferred_does_not_create_daytime_retry(self):
        self.manager.config_path.write_text('{"startup_catchup":true}', 'utf-8')
        self.now = at(16, 14)
        self.restart()
        self.store.results = [result(True)]
        self.finish()
        self.assertIsNone(self.manager._retry_at)
        self.now = at(16, 14, 15)
        self.manager.tick()
        self.assertEqual(self.store.pool.queued, [])
        self.assertEqual(self.manager.status()['next_run'], at(17).isoformat())

    def test_cleanup_exception_retries_only_in_allowed_window(self):
        self.store.results = [RuntimeError('database is locked'), result()]
        self.now = at(16)
        self.finish()
        self.assertEqual(self.manager.status()['phase'], 'error')
        self.assertIn('database is locked', self.manager.status()['message'])
        self.assertIsNone(self.manager._last_completed_day)
        self.now = at(16, 2, 15)
        self.finish()
        self.assertEqual(self.manager.status()['phase'], 'idle')

    def test_state_write_failure_does_not_mark_completed(self):
        self.now = at(16)
        with mock.patch.object(self.manager, '_atomic_json', side_effect=OSError('read only')):
            self.finish()
        self.assertEqual(self.manager.status()['phase'], 'error')
        self.assertIsNone(self.manager._last_completed_day)
        self.assertFalse(self.manager.path.exists())
        self.now = at(16, 2, 15)
        self.finish()
        self.assertEqual(self.manager._last_completed_day, self.now.date())

    def test_queued_cancel_is_idempotent_and_does_not_retry_today(self):
        self.now = at(16)
        self.manager.tick()
        self.assertTrue(self.manager.status()['can_cancel'])
        self.assertFalse(self.manager.status()['maintenance'])
        first = self.manager.cancel()
        self.assertEqual(first['phase'], 'cancelled')
        self.manager.cancel()
        self.store.pool.run()
        self.assertEqual(self.store.calls, [])
        self.assertEqual(FakeControl.instances, [])
        self.now = at(16, 2, 15)
        self.manager.tick()
        self.assertEqual(self.store.pool.queued, [])
        self.assertEqual(self.manager.status()['next_run'], at(17).isoformat())

    def assert_running_cancellation(self, close=False):
        entered, release = threading.Event(), threading.Event()

        def operation(control):
            entered.set()
            self.assertTrue(control.cancel_event.wait(3))
            self.assertTrue(release.wait(3))
            return dict(result(expired_count=2), cancelled=True, deferred=False)

        self.store.results = [operation]
        self.now = at(16)
        self.manager.tick()
        worker = threading.Thread(target=self.store.pool.run)
        worker.start()
        self.assertTrue(entered.wait(3))
        control = FakeControl.instances[0]
        lock_free = []

        def check_manager_lock_is_free():
            observer = threading.Thread(target=lambda: lock_free.append(self.manager.status()))
            observer.start()
            observer.join(timeout=1)
            self.assertFalse(observer.is_alive(), 'controller cancel called while manager lock was held')

        control.cancel_hook = check_manager_lock_is_free
        try:
            if close:
                self.manager.close()
            else:
                self.manager.cancel()
                self.manager.cancel()
            status = self.manager.status()
            self.assertEqual(status['phase'], 'cancelling')
            self.assertTrue(status['maintenance'])
            self.assertFalse(status['can_cancel'])
            self.assertTrue(status['progress']['cancel_requested'])
            self.assertTrue(lock_free)
        finally:
            release.set()
            worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        status = self.manager.status()
        self.assertEqual(status['phase'], 'cancelled')
        self.assertFalse(status['maintenance'])
        self.assertEqual(status['last_result']['expired_count'], 2)
        self.assertTrue(control.finished)
        self.assertIsNone(self.manager._retry_at)
        self.now = at(16, 2, 15)
        self.manager.tick()
        self.assertEqual(self.store.pool.queued, [])

    def test_running_cancel_waits_for_sql_exit_and_owner_release(self):
        self.assert_running_cancellation()

    def test_close_cancels_running_work_not_only_queued_future(self):
        self.assert_running_cancellation(close=True)

    def test_timeout_ends_once_without_fifteen_minute_retry(self):
        def timeout(control):
            control.cancel('timeout')
            return result()

        self.store.results = [timeout]
        self.now = at(16)
        self.finish()
        status = self.manager.status()
        self.assertEqual(status['phase'], 'cancelled')
        self.assertTrue(status['last_result']['timed_out'])
        self.assertFalse(status['last_result']['deferred'])
        self.assertIn('单次时限', status['message'])
        self.now = at(16, 2, 15)
        self.manager.tick()
        self.assertEqual(self.store.pool.queued, [])
        self.now = at(17)
        self.finish()

    def test_timeout_status_waits_for_store_to_finish_rollback(self):
        entered, release = threading.Event(), threading.Event()

        def operation(control):
            entered.set()
            self.assertTrue(release.wait(3))
            return result()

        self.store.results = [operation]
        self.now = at(16)
        self.manager.tick()
        worker = threading.Thread(target=self.store.pool.run)
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            FakeControl.instances[0].cancel('timeout')
            status = self.manager.status()
            self.assertEqual(status['phase'], 'cancelling')
            self.assertTrue(status['maintenance'])
            self.assertFalse(status['can_cancel'])
            self.assertIn('单次时限', status['message'])
        finally:
            release.set()
            worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.manager.status()['phase'], 'cancelled')
        self.assertFalse(self.manager.status()['maintenance'])

    def test_queue_wait_does_not_consume_timeout_but_cannot_cross_three(self):
        self.now = at(16)
        self.manager.tick()
        self.assertEqual(FakeControl.instances, [])
        self.now = at(16, 2, 40)
        self.store.pool.run()
        self.assertEqual(self.store.calls[0][0], at(13, 2, 40).astimezone(dt.timezone.utc))
        self.assertEqual(FakeControl.instances[0].timeout_seconds, 300)
        self.now = at(17)
        self.manager.tick()
        self.now = at(17, 3)
        self.store.pool.run()
        self.assertEqual(len(self.store.calls), 1)
        self.assertIn('错过', self.manager.status()['message'])
        self.assertEqual(self.manager.status()['next_run'], at(18).isoformat())

    def test_stale_queue_crossing_next_night_is_not_counted_as_todays_attempt(self):
        self.now = at(16)
        self.manager.tick()
        self.now = at(17, 2, 5)
        self.store.pool.run()
        self.assertEqual(self.store.calls, [])
        self.assertEqual(self.manager._last_attempt_day, at(16).date())
        self.assertEqual(self.manager.status()['next_run'], self.now.isoformat())
        self.finish()
        self.assertEqual(len(self.store.calls), 1)
        self.assertEqual(self.manager._last_attempt_day, at(17).date())
        self.manager.tick()
        self.assertEqual(self.store.pool.queued, [])

    def test_cancel_or_close_before_future_assignment_does_not_start_store(self):
        for close in (False, True):
            with self.subTest(close=close):
                self.now = at(16, 1)
                self.restart()
                original_submit = self.store.pool.submit

                def submit(function, *args):
                    future = original_submit(function, *args)
                    if close:
                        self.manager.close()
                    else:
                        self.manager.cancel()
                    return future

                self.now = at(16)
                with mock.patch.object(self.store.pool, 'submit', side_effect=submit):
                    status = self.manager.tick()
                self.assertEqual(status['phase'], 'cancelled')
                self.store.pool.run()
                self.assertEqual(self.store.calls, [])
                self.assertEqual(FakeControl.instances, [])
                # The next subtest represents a new service instance/day.
                self.manager.path.unlink(missing_ok=True)

    def test_status_is_database_free_and_returns_detached_progress_and_result(self):
        self.now = at(16)
        self.finish()
        snapshot = self.manager.status()
        snapshot['last_result']['expired_ids'].append('altered')
        snapshot['progress']['stage'] = 'altered'
        self.assertEqual(self.manager.status()['last_result']['expired_ids'], ['example'])
        self.assertNotEqual(self.manager.status()['progress']['stage'], 'altered')
        self.assertEqual(len(self.store.calls), 1)
        self.assertEqual(snapshot['hours'], 72)
        self.assertEqual(snapshot['schedule'], '02:00')
        self.assertFalse(snapshot['maintenance'])
        self.assertFalse(snapshot['can_cancel'])

    def test_submit_failure_clears_job_and_retries_within_window(self):
        self.now = at(16)
        with mock.patch.object(self.store.pool, 'submit', side_effect=RuntimeError('pool busy')):
            status = self.manager.tick()
        self.assertEqual(status['phase'], 'error')
        self.assertIsNone(self.manager._job)
        self.now = at(16, 2, 15)
        self.finish()

    def test_start_is_idempotent_and_never_runs_on_startup(self):
        self.now = at(16, 14)
        reached = threading.Event()
        original = self.manager.tick

        def tick():
            value = original()
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
        self.assertEqual(self.store.pool.queued, [])

    def test_explicit_tick_time_preserves_utc_cutoff(self):
        self.manager.tick(at(17, 2, 5))
        self.store.pool.run()
        self.assertEqual(self.store.calls[0][0], at(14, 2, 5).astimezone(dt.timezone.utc))


if __name__ == '__main__':
    unittest.main()
