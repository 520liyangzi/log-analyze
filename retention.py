"""Nightly, restart-safe retention scheduling; indexed data is owned by Store."""
import copy
import datetime as dt
import json
from pathlib import Path
import threading
import uuid


class RetentionManager:
    HOURS = 72
    SCHEDULE = '02:00'
    RETRY = dt.timedelta(minutes=15)

    def __init__(self, store, busy, clock=None):
        self.store = store
        self.busy = busy
        self.clock = clock or (lambda: dt.datetime.now().astimezone())
        self._system_clock = clock is None
        self.path = Path(store.directory) / 'retention-state.json'
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._closed = False
        self._pending = None
        self._future = None
        self._last_completed_day = None
        self._last_run = None
        self._last_result = None
        self._retry_at = None
        self._phase = 'idle'
        self._message = '每天本机凌晨 02:00 清理导入完成超过 72 小时的日志索引，保留原始 ZIP。'
        self._load()

    @staticmethod
    def _aware(value):
        if not isinstance(value, dt.datetime):
            raise ValueError('清理时钟必须返回 datetime')
        return value.astimezone() if value.tzinfo is None else value

    def _now(self):
        return self._aware(self.clock())

    def _at_two(self, day, now):
        value = dt.datetime.combine(day, dt.time(2))
        # datetime.now().astimezone() exposes a fixed offset, not a zone with
        # future DST rules. Resolve each scheduled day through the OS instead.
        return value.astimezone() if self._system_clock else value.replace(tzinfo=now.tzinfo)

    def _due_day(self, now):
        today = now.date()
        return today if now >= self._at_two(today, now) else today - dt.timedelta(days=1)

    def _load(self):
        try:
            if not self.path.exists():
                return
            value = json.loads(self.path.read_text('utf-8-sig'))
            if not isinstance(value, dict):
                raise ValueError('状态应为 JSON 对象')
            completed = value.get('last_completed_day')
            completed = dt.date.fromisoformat(completed) if completed is not None else None
            result = value.get('result')
            if result is not None and not isinstance(result, dict):
                raise ValueError('result 格式错误')
            last_run = value.get('last_run')
            if last_run is not None:
                dt.datetime.fromisoformat(last_run)
            retry = value.get('retry_at')
            retry = self._aware(dt.datetime.fromisoformat(retry)) if retry else None
            self._last_completed_day = completed
            self._last_result = result
            self._last_run = last_run
            self._retry_at = retry
            if result and result.get('deferred'):
                self._phase = 'error' if result.get('error') else 'deferred'
                self._message = str(result.get('reason') or '上次清理暂缓，将自动重试。')
        except (OSError, ValueError, TypeError):
            # Do not silently trust a partially parsed/corrupt completion date.
            self._last_completed_day = None
            self._last_result = None
            self._last_run = None
            self._retry_at = None
            self._phase = 'error'
            self._message = '自动清理状态文件损坏或无法读取，将补跑清理并尝试修复状态文件。'

    def _write(self, value):
        temporary = self.path.with_name(self.path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), 'utf-8')
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def _next_run(self, now):
        if self._closed or self._pending is not None:
            return None
        if self._retry_at:
            return max(now, self._retry_at).isoformat()
        due = self._due_day(now)
        if self._last_completed_day is None or self._last_completed_day < due:
            return now.isoformat()
        following = max(due, self._last_completed_day) + dt.timedelta(days=1)
        return self._at_two(following, now).isoformat()

    def status(self):
        now = self._now()
        with self.lock:
            return dict(enabled=True, hours=self.HOURS, schedule=self.SCHEDULE,
                        timezone=str(now.tzinfo), phase=self._phase, message=self._message,
                        last_run=self._last_run, last_result=copy.deepcopy(self._last_result),
                        next_run=self._next_run(now))

    def tick(self, now=None):
        now = self._aware(now) if now is not None else self._now()
        with self.lock:
            due = self._due_day(now)
            if (self._closed or self._pending is not None
                    or (self._retry_at is not None and now < self._retry_at)
                    or (self._retry_at is None and self._last_completed_day is not None
                        and self._last_completed_day >= due)):
                return self.status()
            token = uuid.uuid4().hex
            self._pending = token
            self._phase = 'queued'
            self._message = '到达清理时间，等待当前导入或磁盘任务完成后清理过期索引。'
        try:
            future = self.store.pool.submit(self._run, token, due, now)
            with self.lock:
                if self._pending == token:
                    self._future = future
                    if self._closed:
                        future.cancel()
        except Exception as exc:
            self._finish(token, due, now, self._error_result('无法安排自动清理：' + str(exc)))
        return self.status()

    @staticmethod
    def _error_result(message):
        return dict(deferred=True, expired_count=0, reclaimed_bytes=0, compacted=False,
                    reason=message[:500], expired_ids=[], error=True)

    def _run(self, token, due, scheduled_at):
        with self.lock:
            if self._closed or self._pending != token:
                if self._pending == token:
                    self._pending = None
                    self._future = None
                return
            self._phase = 'running'
            self._message = '正在清理超过 72 小时的日志索引并整理数据库空间，原始 ZIP 会保留。'
            started = max(scheduled_at, self._now())
            due = max(due, self._due_day(started))
            self._last_run = started.isoformat()
        try:
            cutoff = started.astimezone(dt.timezone.utc) - dt.timedelta(hours=self.HOURS)
            result = self.store.expire_indexes(cutoff, self.busy)
            if not isinstance(result, dict) or not isinstance(result.get('deferred'), bool):
                raise ValueError('日志存储返回了无效的清理结果')
            result = copy.deepcopy(result)
        except Exception as exc:
            result = self._error_result('自动清理失败：' + str(exc))
        self._finish(token, due, started, result)

    def _finish(self, token, due, started, result):
        with self.lock:
            if self._pending != token:
                return
            finished = max(started, self._now())
            deferred = result.get('deferred', True)
            completed = self._last_completed_day if deferred else max(due, self._last_completed_day or due)
            retry_at = finished + self.RETRY if deferred else None
            self._last_run = started.isoformat()
            self._last_result = result
            self._retry_at = retry_at
            self._phase = ('error' if result.get('error') else 'deferred') if deferred else 'idle'
            self._message = (str(result.get('reason') or '有使用中的数据或空间整理尚未完成，15 分钟后重试。')
                             if deferred else '本次清理完成；原始 ZIP、AI 历史和配置均保留。')
            payload = dict(version=1, last_completed_day=completed.isoformat() if completed else None,
                           last_run=self._last_run, result=result,
                           retry_at=retry_at.isoformat() if retry_at else None)
            try:
                self._write(payload)
            except (OSError, ValueError, TypeError) as exc:
                # Clearing rows may already have succeeded; never say the daily
                # job is durably complete when recording that fact failed.
                self._phase = 'error'
                self._message = '清理状态文件写入失败，15 分钟后重试：' + str(exc)[:300]
                self._retry_at = finished + self.RETRY
                self._last_result = dict(result, deferred=True, reason=self._message)
            else:
                self._last_completed_day = completed
            finally:
                self._pending = None
                self._future = None
                if self._closed:
                    self._phase = 'stopped'

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:
                with self.lock:
                    self._phase = 'error'
                    self._message = '自动清理调度异常，将继续重试：' + str(exc)[:300]
            self._stop.wait(30)

    def start(self):
        with self.lock:
            if self._closed or (self._thread is not None and self._thread.is_alive()):
                return
            self._thread = threading.Thread(target=self._loop, name='logscope-retention', daemon=True)
            self._thread.start()

    def close(self):
        with self.lock:
            self._closed = True
            self._stop.set()
            self._phase = 'stopped'
            self._message = '自动清理调度已停止。'
            if self._future is not None:
                self._future.cancel()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3)
