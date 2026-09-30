"""Persist archive previews and require an explicit, revision-checked import."""
import copy
import datetime as dt
import fnmatch
import json
from pathlib import Path
import re
import shutil
import uuid

from index_retention import dataset_lifecycle


class ImportConflict(ValueError):
    """A stale client must reload rather than overwrite or repeat a task."""


class ImportPlans:
    EDITABLE_STATES = ('review', 'failed')
    MAX_PLAN_BYTES = 25 * 1024 * 1024

    def __init__(self, store):
        self.store = store
        self.directory = Path(store.directory) / 'import-plans'
        self.archives = Path(store.directory) / 'archives'
        self.directory.mkdir(exist_ok=True)
        with store.connect() as db:
            db.execute("UPDATE datasets SET state='failed', error=? WHERE state='scanning'",
                       ('上次目录扫描被中断，原始 ZIP 已保留；请重新扫描后确认导入。',))

    @staticmethod
    def _identifier(value):
        if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{32}', value):
            raise ValueError('日志包标识无效')
        return value

    def _row(self, identifier):
        identifier = self._identifier(identifier)
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM datasets WHERE id=?', (identifier,)).fetchone()
        if row is None:
            raise ValueError('日志包不存在或已删除')
        return dict(row)

    def _archive(self, identifier):
        identifier = self._identifier(identifier)
        path = self.archives / (identifier + '.zip')
        if path.is_symlink() or not path.is_file() or path.resolve().parent != self.archives.resolve():
            raise ValueError('此日志包的原始 ZIP 不存在或来源不正确，请重新上传')
        return path

    def _path(self, identifier):
        return self.directory / (self._identifier(identifier) + '.json')

    def _read(self, identifier):
        path = self._path(identifier)
        try:
            if path.is_symlink() or path.stat().st_size > self.MAX_PLAN_BYTES:
                raise ValueError('计划文件无效')
            plan = json.loads(path.read_text('utf-8-sig'))
            if (not isinstance(plan, dict) or plan.get('id') != identifier
                    or not isinstance(plan.get('revision'), str)
                    or not re.fullmatch(r'[a-f0-9]{32}', plan['revision'])
                    or not isinstance(plan.get('groups'), list)
                    or not isinstance(plan.get('warnings'), list)):
                raise ValueError('计划格式无效')
            return plan
        except (OSError, ValueError, TypeError) as exc:
            raise ValueError('导入计划缺失或损坏，原始 ZIP 不受影响；请重新扫描') from exc

    def _write(self, identifier, plan):
        path = self._path(identifier)
        content = json.dumps(plan, ensure_ascii=False, separators=(',', ':'))
        if len(content.encode('utf-8')) > self.MAX_PLAN_BYTES:
            raise ValueError('导入计划超过 25 MB，请拆分压缩包；未覆盖已保存规则')
        temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            temporary.write_text(content, 'utf-8')
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _empty(identifier, name, encoding='auto', offset='+0800', unit='ms'):
        return dict(version=1, id=identifier, name=name, revision=uuid.uuid4().hex,
                    encoding=encoding, scan_encoding=encoding, offset=offset, unit=unit, groups=[], warnings=[])

    def _mark_failed(self, identifier, message):
        with self.store.connect() as db:
            db.execute("UPDATE datasets SET state='failed',error=? WHERE id=? AND state IN ('scanning','importing')",
                       (str(message)[:2000], identifier))
        self.store.progress.pop(identifier, None)

    @dataset_lifecycle
    def stage(self, path, name, encoding='auto', offset='+0800', unit='ms'):
        self.store.validate_import_options(encoding, offset, unit)
        if not isinstance(name, str) or not name.strip() or len(name) > 1024 or '\x00' in name:
            raise ValueError('原始 ZIP 名称不能为空且最多 1024 字符')
        source = Path(path)
        if not source.is_file() or source.is_symlink():
            raise ValueError('上传的 ZIP 文件不存在')
        identifier = uuid.uuid4().hex
        archive = self.archives / (identifier + '.zip')
        plan = self._empty(identifier, name, encoding, offset, unit)
        with self.store.connect() as db:
            db.execute('INSERT INTO datasets(id,name,state,created,index_version) VALUES(?,?,?,?,?)',
                       (identifier, name, 'scanning', dt.datetime.now(dt.timezone.utc).isoformat(),
                        2 if self.store.write_fts == 'log_fts_v2' else 1))
        try:
            shutil.move(str(source), str(archive))
            self._write(identifier, plan)
            self._queue_scan(identifier)
        except Exception as exc:
            self._mark_failed(identifier, '无法开始目录扫描：' + str(exc))
            raise ValueError('无法开始目录扫描；已登记日志包，保留的 ZIP 可在列表中重新扫描') from exc
        return identifier

    def _queue_scan(self, identifier):
        self.store.progress[identifier] = dict(stage='scanning', message='等待扫描目录，尚未建立索引',
                                               entries=0, groups=0, bytes=0, current='')
        self.store.pool.submit(self._scan, identifier)

    def _scan(self, identifier):
        # Import lazily to keep Store/plan initialization independent of parser
        # implementation and avoid importing app.py back into this module.
        try:
            from archive_layout import scan_archive
            row = self._row(identifier)
            if row['state'] != 'scanning':
                return
            settings = self._read(identifier)
            archive = self._archive(identifier)

            def progress(value):
                self.store.progress[identifier] = copy.deepcopy(value)

            plan = scan_archive(archive, row['name'], encoding=settings['encoding'], progress=progress)
            plan.update(id=identifier, name=row['name'], revision=uuid.uuid4().hex, scan_encoding=settings['encoding'],
                        **{key: settings[key] for key in ('encoding', 'offset', 'unit')})
            with self.store.lifecycle_lock:
                current = self._row(identifier)
                if current['state'] != 'scanning':
                    return
                self._write(identifier, plan)
                with self.store.connect() as db:
                    db.execute("UPDATE datasets SET state='review',error='',warnings=? WHERE id=? AND state='scanning'",
                               (json.dumps(plan['warnings'], ensure_ascii=False), identifier))
        except Exception as exc:
            self._mark_failed(identifier, '目录扫描失败：' + str(exc))
        finally:
            self.store.progress.pop(identifier, None)

    def preview(self, identifier):
        # Revision and dataset state must represent the same instant relative
        # to save/confirm; a short lifecycle lock prevents mixed snapshots.
        with self.store.lifecycle_lock:
            row = self._row(identifier)
            self._archive(identifier)
            try:
                plan = self._read(identifier)
            except ValueError as exc:
                plan = dict(self._empty(identifier, row['name']), revision='', warnings=[str(exc)])
            return dict(id=identifier, name=row['name'], state=row['state'], error=row['error'],
                        revision=plan['revision'], encoding=plan.get('encoding', 'auto'),
                        scan_encoding=plan.get('scan_encoding', plan.get('encoding', 'auto')),
                        offset=plan.get('offset', '+0800'), unit=plan.get('unit', 'ms'),
                        groups=copy.deepcopy(plan['groups']), warnings=copy.deepcopy(plan['warnings']),
                        progress=copy.deepcopy(self.store.progress.get(identifier)))

    def _edited(self, body):
        from archive_layout import validate_edits
        if not isinstance(body, dict):
            raise ValueError('导入确认内容必须为 JSON 对象')
        identifier = self._identifier(body.get('dataset'))
        row = self._row(identifier)
        self._archive(identifier)
        if row['state'] not in self.EDITABLE_STATES:
            raise ImportConflict('该日志包状态已变化，当前不能编辑或重复确认；请刷新目录预览')
        plan = self._read(identifier)
        if body.get('revision') != plan['revision']:
            raise ImportConflict('其他页面已更新导入规则或任务已提交，请重新加载最新目录预览')
        encoding = body.get('encoding', plan.get('encoding', 'auto'))
        offset = body.get('offset', plan.get('offset', '+0800'))
        unit = body.get('unit', plan.get('unit', 'ms'))
        self.store.validate_import_options(encoding, offset, unit)
        scan_encoding = plan.get('scan_encoding', plan.get('encoding', 'auto'))
        edited = validate_edits(plan, body.get('groups', []))
        edited.update(encoding=encoding, scan_encoding=scan_encoding, offset=offset, unit=unit,
                      revision=uuid.uuid4().hex)
        return row, edited

    @dataset_lifecycle
    def save_draft(self, body):
        row, plan = self._edited(body)
        self._write(row['id'], plan)
        return self.preview(row['id'])

    @staticmethod
    def _validate_selection(plan):
        from archive_layout import DEFAULT_PATTERNS, build_resolver
        unsupported = []
        for group in plan['groups']:
            if not group['included']:
                continue
            patterns = [value.strip().lower() for value in group['patterns'].split(',') if value.strip()]
            for file in group['files']:
                # Match the resolver's explicit manifest opt-in: the default
                # *.txt rule never selects fileList.txt as a log file.
                if file['name'].lower() == 'filelist.txt' and group['patterns'] == DEFAULT_PATTERNS:
                    continue
                if not file['text'] and any(fnmatch.fnmatchcase(file['name'].lower(), pattern) for pattern in patterns):
                    unsupported.append(file['path'])
        if unsupported:
            names = '、'.join(path if len(path) <= 200 else path[:197] + '...' for path in unsupported[:5])
            remainder = f' 等 {len(unsupported)} 份文件' if len(unsupported) > 5 else ''
            raise ValueError(f'当前规则匹配到不支持的二进制文件或损坏的压缩日志：{names}{remainder}。'
                             '请缩小文件匹配规则（例如 *.log,*.txt）或取消对应目录后再确认。')
        resolver = build_resolver(plan)
        if resolver.file_count < 1:
            raise ValueError('没有匹配到可导入的文本日志；请勾选至少一个目录并检查文件匹配规则')

    @dataset_lifecycle
    def confirm(self, body):
        row, plan = self._edited(body)
        identifier = row['id']
        if plan['encoding'] != plan['scan_encoding']:
            raise ValueError('编码已更改，请先重新扫描，以新编码重新检查文本类型和样例后再确认导入')
        self._validate_selection(plan)
        self._write(identifier, plan)
        with self.store.connect() as db:
            db.execute("UPDATE datasets SET state='importing',error='' WHERE id=?", (identifier,))
        try:
            self.store.pool.submit(self.store.ingest, identifier, self._archive(identifier), row['name'],
                                   plan['encoding'], plan['offset'], plan['unit'], plan=plan, keep_archive=True)
        except Exception as exc:
            self._mark_failed(identifier, '无法安排索引导入：' + str(exc))
            raise ValueError('无法安排索引导入，原始 ZIP 和已保存规则均保留；可重新确认') from exc
        return dict(id=identifier, state='importing')

    @dataset_lifecycle
    def rescan(self, identifier):
        row = self._row(identifier)
        self._archive(identifier)
        if row['state'] not in self.EDITABLE_STATES:
            raise ImportConflict('日志包当前不能重新扫描；请等待现有任务结束，已导入日志包可重新上传')
        try:
            settings = self._read(identifier)
        except ValueError:
            settings = {}
        plan = self._empty(identifier, row['name'], settings.get('encoding', 'auto'),
                           settings.get('offset', '+0800'), settings.get('unit', 'ms'))
        self.store.validate_import_options(plan['encoding'], plan['offset'], plan['unit'])
        self._write(identifier, plan)
        with self.store.connect() as db:
            db.execute("UPDATE datasets SET state='scanning',error='' WHERE id=?", (identifier,))
        try:
            self._queue_scan(identifier)
        except Exception as exc:
            self._mark_failed(identifier, '无法安排重新扫描：' + str(exc))
            raise ValueError('无法安排重新扫描，原始 ZIP 已保留') from exc
        return dict(id=identifier, state='scanning')
