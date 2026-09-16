"""Index-only expiry. Original archives and investigation data are never removed."""
import datetime as dt
import contextlib
from functools import wraps
import shutil
import sqlite3
import threading
import time


class IndexMaintenance(ValueError):
    """A reachable service is temporarily maintaining its log database."""


class MaintenanceStopped(Exception):
    pass


class MaintenanceControl:
    """Cooperative cancellation plus SQLite interrupt, never forced file removal.

    Interrupt requests stop running SQL; SQLite may still need time to roll back.
    The maintenance reservation stays held until that rollback has finished.
    """
    def __init__(self, timeout_seconds=300, on_progress=None):
        self.started = time.monotonic()
        self.deadline = self.started + max(.001, timeout_seconds)
        self.on_progress = on_progress
        self.lock = threading.RLock()
        self.reason = None
        self.connection = None
        self.progress = dict(stage='checking', message='正在检查过期日志', completed=0, total=0, current='')
        self.finished_at = None
        self.timer = threading.Timer(max(.001, timeout_seconds), self.cancel, args=('timeout',))
        self.timer.daemon = True
        self.timer.start()

    def snapshot(self):
        with self.lock:
            return dict(self.progress, elapsed_seconds=round((self.finished_at or time.monotonic()) - self.started, 1),
                        cancel_requested=self.reason is not None)

    def publish(self, stage, message, **values):
        with self.lock:
            self.progress.update(stage=stage, message=message, **values)
        if self.on_progress:
            self.on_progress(self.snapshot())
        self.checkpoint()

    def cancel(self, reason='cancelled'):
        with self.lock:
            if self.finished_at is not None:
                return
            self.reason = self.reason or reason
            if self.connection is not None:
                try:
                    self.connection.interrupt()
                except sqlite3.Error:
                    pass

    def _interrupted(self):
        with self.lock:
            if self.reason is None and time.monotonic() >= self.deadline:
                self.reason = 'timeout'
            return int(self.reason is not None)

    def checkpoint(self):
        if self._interrupted():
            raise MaintenanceStopped()

    @contextlib.contextmanager
    def bind(self, db):
        self.checkpoint()
        with self.lock:
            self.connection = db
            db.set_progress_handler(self._interrupted, 1000)
        try:
            yield db
        finally:
            with self.lock:
                # Stop interrupting before the Store context rolls back/closes.
                db.set_progress_handler(None, 0)
                self.connection = None

    def finish(self):
        with self.lock:
            self.timer.cancel()
            self.finished_at = self.finished_at or time.monotonic()


@contextlib.contextmanager
def maintenance_connection(store, control):
    with store.connect(timeout=1) as db:
        with control.bind(db):
            yield db


def dataset_lifecycle(method):
    """Serialize starting users/writers with the maintenance admission check."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        store = getattr(self, 'store', self)
        with store.lifecycle_lock:
            return method(self, *args, **kwargs)
    return wrapped


def database_bytes(store):
    total = 0
    for suffix in ('', '-wal', '-shm'):
        try:
            total += store.database.with_name(store.database.name + suffix).stat().st_size
        except FileNotFoundError:
            pass
    return total


def expired_at(value, cutoff):
    try:
        timestamp = dt.datetime.fromisoformat(value)
        # Old application timestamps are UTC. Do not treat a missing/invalid
        # date as expired, nor compare against timestamps inside log content.
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=dt.timezone.utc)
        return timestamp <= cutoff
    except (ValueError, TypeError):
        return False


def candidates(db, cutoff):
    # A terminated import may have committed batches before startup marked it
    # failed. Those abandoned index rows must not live forever either.
    rows = db.execute("""SELECT id,name,completed_at,created,index_version,state FROM datasets d
        WHERE state='ready' OR (state='failed' AND
          (EXISTS(SELECT 1 FROM logs WHERE dataset=d.id) OR EXISTS(SELECT 1 FROM files WHERE dataset=d.id)))""").fetchall()
    return [row for row in rows if expired_at(
        (row['completed_at'] if row['state'] == 'ready' else None) or row['created'], cutoff)]


def expire_indexes(store, cutoff, busy=lambda: False, control=None):
    owned_control = control is None
    control = control or MaintenanceControl()
    result = dict(deferred=False, expired_count=0, expired_ids=[], reclaimed_bytes=0,
                  compacted=False, cancelled=False, timed_out=False, reason='没有过期索引需要清理')
    reserved, before = False, None

    def defer(reason):
        result.update(deferred=True, reason=reason)
        return result

    try:
        control.publish('checking', '正在检查过期日志与数据库状态')
        with store.lifecycle_lock:
            if busy():
                return defer('有 AI 排查或采集正在运行，本轮暂缓')
            with store.access_lock:
                if store.connections or store.maintenance_owner is not None:
                    return defer('有日志查询或导入正在进行，本轮暂缓')
            with maintenance_connection(store, control) as db:
                pending = db.execute("SELECT value FROM log_metadata WHERE key='compaction_pending'").fetchone()
                expired = candidates(db, cutoff)
                needs_work = expired or db.execute('PRAGMA freelist_count').fetchone()[0] or (pending and pending[0] == '1')
            if not needs_work:
                return result
            control.checkpoint()
            with store.access_lock:
                if store.connections:
                    return defer('有日志查询或导入正在进行，本轮暂缓')
                store.maintenance_owner = threading.get_ident()
                reserved = True
        before = database_bytes(store)
        with maintenance_connection(store, control) as db:
            if db.execute("SELECT 1 FROM datasets WHERE state IN ('importing','deleting') LIMIT 1").fetchone():
                return defer('有日志导入或手动删除排队，本轮暂缓')
            for row in expired:
                identifier = row['id']
                control.publish('deleting', '正在删除过期日志记录和索引', completed=result['expired_count'],
                                total=len(expired), current=row['name'])
                table = store.fts_table(row['index_version'])
                if table:
                    db.execute(f'DELETE FROM {table} WHERE rowid IN (SELECT id FROM logs WHERE dataset=?)', (identifier,))
                db.execute('DELETE FROM logs WHERE dataset=?', (identifier,))
                db.execute('DELETE FROM files WHERE dataset=?', (identifier,))
                db.execute("UPDATE datasets SET state='expired',expired_at=?,error='' WHERE id=?",
                           (dt.datetime.now(dt.timezone.utc).isoformat(), identifier))
                db.execute("INSERT OR REPLACE INTO log_metadata VALUES ('compaction_pending','1')")
                # Expiry state and index deletion commit atomically. A crash
                # during the later VACUUM never restores this package to ready.
                control.checkpoint()
                db.commit()
                result['expired_ids'].append(identifier)
                result['expired_count'] += 1
                store.progress.pop(identifier, None)
                control.publish('checkpoint', '正在写回已完成的清理', completed=result['expired_count'])
                checkpoint = db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                if checkpoint[0]:
                    return defer('索引已清理；数据库仍被占用，空间整理暂缓')
            pending = db.execute("SELECT value FROM log_metadata WHERE key='compaction_pending'").fetchone()
            free_pages = db.execute('PRAGMA freelist_count').fetchone()[0]
            if free_pages or (pending and pending[0] == '1'):
                # VACUUM can require up to twice the database size in temporary
                # space. Leave the committed expired state intact if unavailable.
                needed = store.database.stat().st_size * 2 + 16 * 1024 * 1024
                if shutil.disk_usage(store.directory).free < needed:
                    db.execute("INSERT OR REPLACE INTO log_metadata VALUES ('compaction_pending','1')")
                    return defer('索引已清理，但磁盘余量不足以收缩数据库；请释放磁盘后等待下一次清理')
                # Deleted FTS postings may remain in old segments. Merge those
                # segments before VACUUM so their freed pages can also shrink.
                for table in store.fts_tables:
                    control.publish('fts', '正在合并全文索引残留', current=table)
                    db.execute(f"INSERT INTO {table}({table}) VALUES('optimize')")
                control.checkpoint()
                db.commit()
                control.publish('vacuum', '正在收缩日志数据库文件', current=store.database.name)
                db.execute('VACUUM')
                control.publish('checkpoint', '正在释放数据库 WAL 空间')
                checkpoint = db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                if checkpoint[0]:
                    return defer('数据库已整理，WAL 仍被占用，稍后再整理空间')
                db.execute("INSERT OR REPLACE INTO log_metadata VALUES ('compaction_pending','0')")
                result['compacted'] = True
            if result['expired_count'] or result['compacted']:
                result['reason'] = '过期日志索引已清理并整理数据库；原始 ZIP、AI 对话和配置均保留'
            control.publish('finishing', '正在完成维护并恢复日志查询', current='')
    except (MaintenanceStopped, sqlite3.Error, OSError):
        if control.reason:
            result.update(cancelled=True, timed_out=control.reason == 'timeout', stop_reason=control.reason,
                          reason='清理已超时停止，日志查询已恢复；已完成的清理保留，未完成的事务已回滚' if control.reason == 'timeout'
                          else '清理已取消，日志查询已恢复；已完成的清理保留，未完成的事务已回滚')
        else:
            result.update(deferred=True, reason='索引清理或数据库整理未完成，已恢复日志查询；ZIP 保留不变')
    finally:
        try:
            if before is not None:
                result['reclaimed_bytes'] = max(0, before - database_bytes(store))
        except OSError:
            pass  # Reporting failures must never leave maintenance locked.
        finally:
            with store.access_lock:
                if reserved:
                    store.maintenance_owner = None
            if owned_control:
                control.finish()
    return result
