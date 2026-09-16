"""Bounded nightly retention. Opening the application never implies catch-up."""
import copy
import datetime as dt
import json
from pathlib import Path
import threading
import uuid

import index_retention


class RetentionManager:
    HOURS = 72
    SCHEDULE = '02:00'
    RETRY = dt.timedelta(minutes=15)
    DEFAULT_CONFIG = dict(enabled=True, startup_catchup=False, max_run_seconds=300)

    def __init__(self, store, busy, clock=None):
        self.store, self.busy = store, busy
        self.clock = clock or (lambda: dt.datetime.now().astimezone())
        self._system_clock = clock is None
        self.path = Path(store.directory) / 'retention-state.json'
        self.config_path = Path(store.directory) / 'retention-config.json'
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._closed = False
        self._job = None
        self._started_at = self._now()
        self._last_completed_day = None
        self._last_attempt_day = None
        self._last_run = None
        self._last_result = None
        self._last_progress = None
        self._retry_at = None
        self._phase = 'idle'
        self._message = '每天本机 02:00 清理过期索引；启动不补跑，错过等下一天。'
        self.config = self.DEFAULT_CONFIG.copy()
        self._read_config()
        self._load()
        self._catchup_pending = bool(self.config['enabled'] and self.config['startup_catchup'])

    @staticmethod
    def _aware(value):
        if not isinstance(value, dt.datetime):
            raise ValueError('清理时钟必须返回 datetime')
        return value.astimezone() if value.tzinfo is None else value

    def _now(self):
        return self._aware(self.clock())

    @staticmethod
    def _atomic_json(path, value):
        temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), 'utf-8')
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def _read_config(self):
        try:
            if not self.config_path.exists():
                self._atomic_json(self.config_path, self.config)
                return
            value = json.loads(self.config_path.read_text('utf-8-sig'))
            if not isinstance(value, dict):
                raise ValueError('配置应为 JSON 对象')
            config = dict(self.DEFAULT_CONFIG, **value)
            if not all(isinstance(config[key], bool) for key in ('enabled', 'startup_catchup')):
                raise ValueError('enabled 和 startup_catchup 必须为 true/false')
            seconds = config['max_run_seconds']
            if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 1 <= seconds <= 3600:
                raise ValueError('max_run_seconds 必须在 1～3600 秒之间')
            self.config = {key: config[key] for key in self.DEFAULT_CONFIG}
        except (OSError, ValueError, TypeError) as exc:
            self.config['enabled'] = False
            self._phase = 'error'
            self._message = '自动清理配置无法读取或创建，已暂停自动清理：' + str(exc)[:250]

    def _load(self):
        try:
            if not self.path.exists():
                return
            value = json.loads(self.path.read_text('utf-8-sig'))
            if not isinstance(value, dict):
                raise ValueError('状态应为 JSON 对象')
            completed = value.get('last_completed_day')
            attempted = value.get('last_attempt_day')
            completed = dt.date.fromisoformat(completed) if completed is not None else None
            attempted = dt.date.fromisoformat(attempted) if attempted is not None else None
            result = value.get('result')
            if result is not None and not isinstance(result, dict):
                raise ValueError('result 格式错误')
            last_run = value.get('last_run')
            if last_run is not None:
                dt.datetime.fromisoformat(last_run)
            self._last_completed_day, self._last_attempt_day = completed, attempted
            self._last_result, self._last_run = result, last_run
            # Ignore v2.1 retry_at: a restart never restores a daytime retry.
        except (OSError, ValueError, TypeError):
            self._last_completed_day = self._last_attempt_day = None
            self._last_result = self._last_run = None
            if self.config['enabled']:
                self._phase = 'error'
                self._message = '自动清理状态文件损坏或无法读取；不会默认启动补跑，将等待下次凌晨 02:00。'

    def _at_hour(self, day, hour, now):
        value = dt.datetime.combine(day, dt.time(hour))
        # Resolve OS-local boundaries independently of today's fixed UTC offset.
        return value.astimezone() if self._system_clock else value.replace(tzinfo=now.tzinfo)

    def _due_day(self, now):
        return now.date() if now >= self._at_hour(now.date(), 2, now) else now.date() - dt.timedelta(days=1)

    def _night_window(self, now):
        return self._at_hour(now.date(), 2, now) <= now < self._at_hour(now.date(), 3, now)

    def _scheduled_ready(self, now):
        day = now.date()
        if (not self._night_window(now)
                or self._at_hour(day, 2, now) <= self._started_at
                or (self._last_completed_day is not None and self._last_completed_day >= day)):
            return False
        if self._retry_at is not None and self._retry_at.date() == day:
            return now >= self._retry_at
        return self._last_attempt_day is None or self._last_attempt_day < day

    def _next_run(self, now):
        if self._closed or not self.config['enabled'] or self._job is not None:
            return None
        if self._catchup_pending:
            due = self._due_day(now)
            if self._last_completed_day is None or self._last_completed_day < due:
                return now.isoformat()
        if self._scheduled_ready(now):
            return now.isoformat()
        if self._retry_at and self._retry_at > now and self._night_window(self._retry_at):
            return self._retry_at.isoformat()
        day = now.date()
        boundary = self._at_hour(day, 2, now)
        if boundary <= now or boundary <= self._started_at:
            day += dt.timedelta(days=1)
        if self._last_completed_day is not None:
            day = max(day, self._last_completed_day + dt.timedelta(days=1))
        if self._last_attempt_day is not None:
            day = max(day, self._last_attempt_day + dt.timedelta(days=1))
        return self._at_hour(day, 2, now).isoformat()

    def status(self):
        now = self._now()
        with self.lock:
            job = self._job
            control = job.get('control') if job else None
            progress = copy.deepcopy(self._last_progress)
            value = dict(enabled=self.config['enabled'], startup_catchup=self.config['startup_catchup'],
                         max_run_seconds=self.config['max_run_seconds'], hours=self.HOURS,
                         schedule=self.SCHEDULE, retry_window='02:00–03:00', timezone=str(now.tzinfo),
                         phase=self._phase, message=self._message, last_run=self._last_run,
                         last_result=copy.deepcopy(self._last_result), next_run=self._next_run(now),
                         can_cancel=bool(job and not job.get('cancel_requested')))
        # Controller progress may take this manager lock: never snapshot in it.
        if control is not None:
            progress = control.snapshot()
            if progress.get('cancel_requested'):
                value.update(phase='cancelling', can_cancel=False,
                             message='清理达到单次时限，正在停止并等待数据库回滚完成…'
                             if control.reason == 'timeout' else '正在停止清理，等待数据库操作退出…')
        value.update(maintenance=getattr(self.store, 'maintenance_owner', None) is not None,
                     progress=progress)
        return value

    def tick(self, now=None):
        now = self._aware(now) if now is not None else self._now()
        with self.lock:
            due = None
            catchup = False
            if not self._closed and self.config['enabled'] and self._job is None:
                if self._catchup_pending:
                    self._catchup_pending = False
                    candidate = self._due_day(now)
                    if self._last_completed_day is None or self._last_completed_day < candidate:
                        due, catchup = candidate, True
                if due is None and self._scheduled_ready(now):
                    due = now.date()
                if due is not None:
                    job = dict(token=uuid.uuid4().hex, due=due, scheduled_at=now, catchup=catchup,
                               control=None, future=None, cancel_requested=False)
                    self._job = job
                    self._last_attempt_day = max(due, self._last_attempt_day or due)
                    self._retry_at = None
                    self._last_progress = None
                    self._phase = 'queued'
                    self._message = '清理任务已排队；可取消，尚未暂停日志查询。'
            if due is None:
                job = None
        if job is not None:
            try:
                future = self.store.pool.submit(self._run, job)
                with self.lock:
                    if self._job is job:
                        job['future'] = future
                        should_cancel = self._closed or job['cancel_requested']
                    else:
                        should_cancel = False
                if should_cancel and future.cancel():
                    self._finish(job, now, self._cancel_result(), None)
            except Exception as exc:
                self._finish(job, now, self._error_result('无法安排自动清理：' + str(exc)), None)
        return self.status()

    @staticmethod
    def _error_result(message):
        return dict(deferred=True, cancelled=False, timed_out=False, expired_count=0,
                    reclaimed_bytes=0, compacted=False, reason=message[:500], expired_ids=[], error=True)

    @staticmethod
    def _cancel_result(reason='cancelled'):
        timed_out = reason == 'timeout'
        return dict(deferred=False, cancelled=True, timed_out=timed_out, stop_reason=reason,
                    expired_count=0, reclaimed_bytes=0, compacted=False, expired_ids=[],
                    reason='清理达到单次时限，已停止；下次凌晨再检查。' if timed_out
                    else '本次清理已取消；下次凌晨再检查。')

    def _progress(self, job, snapshot):
        with self.lock:
            if self._job is job:
                self._last_progress = copy.deepcopy(snapshot)
                if not job['cancel_requested']:
                    self._message = str(snapshot.get('message') or self._message)

    def _run(self, job):
        with self.lock:
            if self._job is not job:
                return
            stopped = self._closed or job['cancel_requested']
            started = max(job['scheduled_at'], self._now())
        if stopped:
            self._finish(job, started, self._cancel_result(), None)
            return
        # A long import queue must not start a nightly rewrite in working hours.
        if not job['catchup'] and (started.date() != job['due'] or not self._night_window(started)):
            self._finish(job, started, dict(self._cancel_result(), reason='已错过夜间清理窗口，等下次凌晨 02:00。'), None)
            return
        control = None
        snapshot = None
        try:
            control = index_retention.MaintenanceControl(
                timeout_seconds=self.config['max_run_seconds'],
                on_progress=lambda value: self._progress(job, value))
            with self.lock:
                if self._job is job:
                    job['control'] = control
                    stopped = self._closed or job['cancel_requested']
                    self._phase = 'cancelling' if stopped else 'running'
                    self._message = '正在停止本次清理，等待数据库操作退出…' if stopped else '正在检查过期索引…'
                    self._last_run = started.isoformat()
            if stopped:
                control.cancel()
            cutoff = started.astimezone(dt.timezone.utc) - dt.timedelta(hours=self.HOURS)
            value = self.store.expire_indexes(cutoff, self.busy, control=control)
            if not isinstance(value, dict) or not isinstance(value.get('deferred'), bool):
                raise ValueError('日志存储返回了无效的清理结果')
            result = copy.deepcopy(value)
        except Exception as exc:
            result = self._error_result('自动清理失败：' + str(exc))
        finally:
            if control is not None:
                control.finish()
                snapshot = control.snapshot()
        if control is not None and control.reason is not None:
            stopped_result = self._cancel_result(control.reason)
            result.update(cancelled=True, timed_out=control.reason == 'timeout',
                          stop_reason=control.reason, deferred=False, reason=stopped_result['reason'])
        # Store has returned and released maintenance_owner before reporting
        # cancellation complete. No Store execution or interrupt runs in lock.
        self._finish(job, started, result, snapshot)

    def _retry_time(self, finished):
        candidate = (finished.astimezone(dt.timezone.utc) + self.RETRY).astimezone(finished.tzinfo)
        return candidate if self._night_window(finished) and self._night_window(candidate) else None

    def _finish(self, job, started, result, progress):
        with self.lock:
            if self._job is not job:
                return
            finished = max(started, self._now())
            stopped = bool(result.get('cancelled') or result.get('timed_out'))
            deferred = bool(result.get('deferred')) and not stopped
            completed = self._last_completed_day
            if not deferred and not stopped:
                completed = max(job['due'], completed or job['due'])
            self._last_run = started.isoformat()
            self._last_result = result
            self._last_progress = progress or self._last_progress
            self._retry_at = self._retry_time(finished) if deferred else None
            self._phase = 'cancelled' if stopped else ('error' if result.get('error') else 'deferred') if deferred else 'idle'
            self._message = str(result.get('reason') or ('本次清理完成；ZIP、AI 历史和配置保留。' if not deferred
                                                       else '本次清理暂缓，将在允许的夜间时段再检查。'))
            payload = dict(version=2, last_completed_day=completed.isoformat() if completed else None,
                           last_attempt_day=self._last_attempt_day.isoformat() if self._last_attempt_day else None,
                           last_run=self._last_run, result=result,
                           retry_at=self._retry_at.isoformat() if self._retry_at else None)
            try:
                self._atomic_json(self.path, payload)
            except (OSError, ValueError, TypeError) as exc:
                self._message += ' 清理状态保存失败：' + str(exc)[:200]
                if not stopped:
                    self._phase = 'error'
                    self._retry_at = self._retry_time(finished)
                    self._last_result = dict(result, deferred=True, reason=self._message)
            else:
                self._last_completed_day = completed
            finally:
                self._job = None

    def cancel(self):
        with self.lock:
            job = self._job
            if job is not None:
                job['cancel_requested'] = True
                self._phase = 'cancelling'
                self._message = '正在停止本次清理，等待数据库操作退出后恢复查询…'
                control, future = job['control'], job['future']
            else:
                control = future = None
        if job is not None:
            if control is not None:
                control.cancel()
            elif future is not None and future.cancel():
                self._finish(job, self._now(), self._cancel_result(), None)
        return self.status()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:
                with self.lock:
                    self._phase = 'error'
                    self._message = '自动清理调度异常，等允许的夜间时段重试：' + str(exc)[:250]
            self._stop.wait(15)

    def start(self):
        with self.lock:
            if self._closed or (self._thread is not None and self._thread.is_alive()):
                return
            self._started_at = self._now()
            self._thread = threading.Thread(target=self._loop, name='logscope-retention', daemon=True)
            self._thread.start()

    def close(self):
        with self.lock:
            self._closed = True
            self._stop.set()
            thread = self._thread
        self.cancel()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3)
