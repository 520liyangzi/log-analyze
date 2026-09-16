"""Index-only expiry. Original archives and investigation data are never removed."""
import datetime as dt
from functools import wraps
import shutil
import sqlite3
import threading


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
    rows = db.execute("""SELECT id,completed_at,created,index_version,state FROM datasets d
        WHERE state='ready' OR (state='failed' AND
          (EXISTS(SELECT 1 FROM logs WHERE dataset=d.id) OR EXISTS(SELECT 1 FROM files WHERE dataset=d.id)))""").fetchall()
    return [row for row in rows if expired_at(
        (row['completed_at'] if row['state'] == 'ready' else None) or row['created'], cutoff)]


def expire_indexes(store, cutoff, busy=lambda: False):
    result = dict(deferred=False, expired_count=0, expired_ids=[], reclaimed_bytes=0,
                  compacted=False, reason='没有过期索引需要清理')

    def defer(reason):
        result.update(deferred=True, reason=reason)
        return result

    with store.lifecycle_lock:
        if busy():
            return dict(result, deferred=True, reason='有 AI 排查正在运行，15 分钟后重试')
        with store.access_lock:
            if store.connections or store.maintenance_owner is not None:
                return dict(result, deferred=True, reason='有日志查询或导入正在进行，15 分钟后重试')
        # A normal startup with no expired data must not briefly reserve the
        # database and reject the first upload/search from the browser.
        with store.connect() as db:
            pending = db.execute("SELECT value FROM log_metadata WHERE key='compaction_pending'").fetchone()
            needs_work = (candidates(db, cutoff)
                          or db.execute('PRAGMA freelist_count').fetchone()[0]
                          or (pending and pending[0] == '1'))
        if not needs_work:
            return result
        with store.access_lock:
            if store.connections:
                return dict(result, deferred=True, reason='有日志查询或导入正在进行，15 分钟后重试')
            store.maintenance_owner = threading.get_ident()
    before = None
    try:
        before = database_bytes(store)
        with store.connect() as db:
            db.execute('PRAGMA busy_timeout=1000')
            if db.execute("SELECT 1 FROM datasets WHERE state IN ('importing','deleting') LIMIT 1").fetchone():
                return defer('有日志导入或手动删除排队，15 分钟后重试')
            for row in candidates(db, cutoff):
                identifier = row['id']
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
                db.commit()
                result['expired_ids'].append(identifier)
                result['expired_count'] += 1
                store.progress.pop(identifier, None)
                checkpoint = db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                if checkpoint[0]:
                    return defer('索引已清理；数据库仍被占用，空间整理将在 15 分钟后重试')
            pending = db.execute("SELECT value FROM log_metadata WHERE key='compaction_pending'").fetchone()
            free_pages = db.execute('PRAGMA freelist_count').fetchone()[0]
            if free_pages or (pending and pending[0] == '1'):
                # VACUUM can require up to twice the database size in temporary
                # space. Leave the committed expired state intact if unavailable.
                needed = store.database.stat().st_size * 2 + 16 * 1024 * 1024
                if shutil.disk_usage(store.directory).free < needed:
                    db.execute("INSERT OR REPLACE INTO log_metadata VALUES ('compaction_pending','1')")
                    return defer('索引已清理，但磁盘余量不足以收缩数据库；释放磁盘后自动重试')
                # Deleted FTS postings may remain in old segments. Merge those
                # segments before VACUUM so their freed pages can also shrink.
                for table in store.fts_tables:
                    db.execute(f"INSERT INTO {table}({table}) VALUES('optimize')")
                db.commit()
                db.execute('VACUUM')
                checkpoint = db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                if checkpoint[0]:
                    return defer('数据库已整理，WAL 仍被占用，15 分钟后重试释放空间')
                db.execute("INSERT OR REPLACE INTO log_metadata VALUES ('compaction_pending','0')")
                result['compacted'] = True
            if result['expired_count'] or result['compacted']:
                result['reason'] = '过期日志索引已清理并整理数据库；原始 ZIP、AI 对话和配置均保留'
    except (sqlite3.Error, OSError):
        result.update(deferred=True, reason='索引清理或数据库整理未完成，15 分钟后重试；ZIP 保留不变')
    finally:
        try:
            if before is not None:
                result['reclaimed_bytes'] = max(0, before - database_bytes(store))
        except OSError:
            pass  # Reporting failures must never leave maintenance locked.
        finally:
            with store.access_lock:
                store.maintenance_owner = None
    return result
