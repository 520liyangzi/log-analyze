"""LogScope: offline archive search. Python 3.10+, standard library only."""
import argparse
import concurrent.futures
import contextlib
import datetime as dt
import gzip
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import tempfile
import time
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from log_collector import LogCollector
from terminal_bridge import TerminalManager, dimensions

BASE = Path(__file__).resolve().parent
STAMP = re.compile(r'^\[?((?:\d{4}-\d\d-\d\d|\d{8})[ T]\d\d:\d\d:\d\d(?:[.,]\d{1,6})?)(?:\s*([+-]\d{4}))?')
ROOT = re.compile(r'^\[[^\]]+\]\s*\[([^\]]*)\]\s*\[([^\]]*)\]\s*\[(TRACE|DEBUG|INFO|WARN|ERROR|FATAL)\]\s*\[([^\]]*)\]')
ACCESS = re.compile(r'"(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS|CONNECT|TRACE)\s+(.*?)\s+HTTP/[\d.]+"\s+(\d{3})\s+(.*)')
LEVEL = re.compile(r'\b(TRACE|DEBUG|INFO|WARN|ERROR|FATAL)\b')
EXTRA_FIELDS = dict(thread_id='TEXT', route_id='TEXT', request_id='TEXT', response_size='INTEGER',
                    code_file='TEXT', logger='TEXT', code_method='TEXT', code_line='INTEGER', module='TEXT')
PARSER_VERSION = 2


def timestamp(value, offset='+0800'):
    m = STAMP.match(value)
    if not m:
        return None
    raw = m[1].replace(',', '.').replace('T', ' ')
    if raw[4] != '-':
        raw = raw[:4] + '-' + raw[4:6] + '-' + raw[6:]
    zone = m[2] or offset
    parsed = dt.datetime.fromisoformat(raw)
    sign = 1 if zone[0] == '+' else -1
    delta = dt.timedelta(hours=int(zone[1:3]), minutes=int(zone[3:5])) * sign
    return int(parsed.replace(tzinfo=dt.timezone(delta)).timestamp() * 1000)


def parse_line(raw, offset='+0800', duration_unit='ms'):
    result = dict(ts=timestamp(raw, offset), time='', level='', thread='', trace='', span='', method='', url='', status=None, duration=None)
    result.update({key: None if value == 'INTEGER' else '' for key, value in EXTRA_FIELDS.items()})
    match = STAMP.match(raw)
    if match:
        result['time'] = match[1].replace(',', '.') + ' ' + (match[2] or offset)
    root = ROOT.match(raw)
    if root:
        result.update(trace=root[1], span=root[2], level=root[3], thread=root[4])
        # The second ID is a repeated traceId in this format, not a parent/child span.
        fields = re.match(r'\s*\[([^\]]*)\]\s*\[([^\]]*)\]\s*\[([^\]]*)\]\s*\[(\d+)\]', raw[root.end():])
        if fields:
            result.update(code_file=fields[1], logger=fields[2], code_method=fields[3], code_line=int(fields[4]))
    else:
        level = LEVEL.search(raw)
        if level:
            result['level'] = level[1]
        header = re.search(r'\b(?:TRACE|DEBUG|INFO|WARN|ERROR|FATAL)\s+(\d+)\s+\[([^\]]*)\]\[ROOT\]\[\]\[([^\]]*?)\s+(\d+)\]', raw)
        if header:
            result.update(thread_id=header[1], thread=header[2], logger=header[3], code_line=int(header[4]))
            module = re.match(r'\s*\[(WSF-[^\]]+)\]', raw[header.end():])
            if module:
                result['module'] = module[1]
        else:
            thread = re.search(r'\[([^\]]*(?:exec-|thread-|pool-)[^\]]*)\]', raw, re.I)
            if thread:
                result['thread'] = thread[1]
    request_id = re.search(r'\bRequestId\s*[:=]\s*([^\s,;\]"\\]+)', raw, re.I)
    if request_id:
        result['request_id'] = request_id[1]
    access = ACCESS.search(re.sub(r'\\+(?=")', '', raw))
    if access:
        result.update(method=access[1], url=access[2], status=int(access[3]))
        tail = access[4].split()
        if len(tail) >= 3:
            result['response_size'] = int(tail[0]) if tail[0].isdigit() else None
            result['route_id'] = tail[1] if tail[1] != '-' else ''
        if tail:
            try:
                result['duration'] = float(tail[-1]) * {'ms': 1, 's': 1000, 'us': .001}[duration_unit]
            except (ValueError, KeyError):
                pass
    return result


def source_meta(name, chain):
    parts = PurePosixPath(name.replace('\\', '/')).parts
    indices = [i for i, part in enumerate(parts) if part == 'log']
    if not indices:
        return None
    index = indices[-1]
    if index < 3:
        return None
    namespace_pod, service = parts[index - 3:index - 1]
    pod_service = parts[index - 1]
    suffix = '-' + service
    expected_pod = pod_service[:-len(suffix)] if pod_service.endswith(suffix) else ''
    if expected_pod and namespace_pod.endswith('_' + expected_pod):
        pod = expected_pod
        namespace = namespace_pod[:-len(expected_pod) - 1]
    else:
        # Fall back for imperfect packages without assuming namespace lacks underscores.
        namespace, sep, pod = namespace_pod.rpartition('_')
        if not sep:
            pod, namespace = namespace_pod, ''
    filename = parts[-1]
    if '.log' not in filename.lower():
        return None
    # root.2026-09-08.log.gz / root.log.1.gz / root.log -> root
    kind = re.split(r'[.\-_](?=\d)|\.log', filename, maxsplit=1, flags=re.I)[0]
    return dict(node=PurePosixPath(chain[1] if len(chain) > 1 else chain[0]).stem,
                namespace=namespace, pod=pod, service=service, kind=kind,
                filename=filename, archive=' → '.join(chain), path=name,
                source=' → '.join([*chain, name]))


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / 'archives').mkdir(exist_ok=True)
        self.database = self.directory / 'logs.sqlite3'
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.progress = {}
        with self.connect() as db:
            db.executescript('''
              PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS datasets(id TEXT PRIMARY KEY, name TEXT, state TEXT,
                created TEXT, files INTEGER DEFAULT 0, records INTEGER DEFAULT 0,
                error TEXT DEFAULT '', warnings TEXT DEFAULT '[]');
              CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY, dataset TEXT, node TEXT,
                namespace TEXT, pod TEXT, service TEXT, kind TEXT, filename TEXT, archive TEXT,
                path TEXT, source TEXT, records INTEGER DEFAULT 0, encoding TEXT);
              CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY, dataset TEXT, file_id INTEGER,
                line INTEGER, end_line INTEGER, ts INTEGER, time TEXT, level TEXT, thread TEXT,
                trace TEXT, span TEXT, method TEXT, url TEXT, status INTEGER, duration REAL, raw TEXT);
              CREATE INDEX IF NOT EXISTS logs_dataset_time ON logs(dataset,ts,id);
              CREATE INDEX IF NOT EXISTS logs_trace ON logs(dataset,trace,ts);
              CREATE INDEX IF NOT EXISTS logs_url ON logs(dataset,url) WHERE url != '';
              CREATE INDEX IF NOT EXISTS logs_file_line ON logs(file_id,line);
              CREATE INDEX IF NOT EXISTS files_scope ON files(dataset,node,pod,kind);
            ''')
            if 'archive_chain' not in {r['name'] for r in db.execute('PRAGMA table_info(files)')}:
                db.execute("ALTER TABLE files ADD COLUMN archive_chain TEXT")
            columns = {r['name'] for r in db.execute('PRAGMA table_info(logs)')}
            for key, kind in EXTRA_FIELDS.items():
                if key not in columns:
                    db.execute(f'ALTER TABLE logs ADD COLUMN {key} {kind}')
            dataset_columns = {r['name'] for r in db.execute('PRAGMA table_info(datasets)')}
            for key, kind in [('parser_version', 'INTEGER DEFAULT 1'), ('audit', "TEXT DEFAULT '{}' ")]:
                if key not in dataset_columns:
                    db.execute(f'ALTER TABLE datasets ADD COLUMN {key} {kind}')
            db.execute('CREATE INDEX IF NOT EXISTS logs_route ON logs(dataset,route_id)')
            db.execute('CREATE INDEX IF NOT EXISTS logs_request_id ON logs(dataset,request_id)')
            db.execute("UPDATE datasets SET state='failed', error='上次导入被中断，请重新上传' WHERE state='importing'")
        self.fts = False
        with self.connect() as db:
            try:
                db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS log_fts USING fts5(raw, tokenize='trigram')")
                self.fts = True
            except sqlite3.OperationalError:
                pass

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=60)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def submit(self, path, name, encoding='auto', offset='+0800', unit='ms'):
        if encoding not in ('auto', 'utf-8', 'gb18030'):
            raise ValueError('编码无效')
        if not re.fullmatch(r'[+-](?:0\d|1[0-3])[0-5]\d|[+-]1400', offset):
            raise ValueError('时区格式应为 +0800')
        if unit not in ('ms', 's', 'us'):
            raise ValueError('耗时单位无效')
        identifier = uuid.uuid4().hex
        with self.connect() as db:
            db.execute('INSERT INTO datasets(id,name,state,created) VALUES(?,?,?,?)',
                       (identifier, name, 'importing', dt.datetime.now(dt.timezone.utc).isoformat()))
        self.pool.submit(self.ingest, identifier, path, name, encoding, offset, unit)
        return identifier

    def ingest(self, identifier, path, name, encoding, offset, unit):
        warnings, stats = [], {'files': 0, 'records': 0, 'bytes': 0, 'entries': 0}
        actual_paths, listed_paths = set(), set()
        audit = dict(manifest_present=False, recognized_time=0, unrecognized_time=0, physical_lines=0)
        maximum = int(os.getenv('LOG_MAX_EXPANDED_GB', '20')) * 1024 ** 3
        max_record = int(os.getenv('LOG_MAX_RECORD_MB', '8')) * 1024 ** 2
        try:
            with self.connect() as db:
                def add_record(fid, line, end_line, raw, parsed):
                    base = ('ts','time','level','thread','trace','span','method','url','status','duration')
                    columns = ('dataset','file_id','line','end_line',*base,*EXTRA_FIELDS.keys(),'raw')
                    values = (identifier, fid, line, end_line,
                              *[parsed[k] for k in base], *[parsed[k] for k in EXTRA_FIELDS], raw)
                    cursor = db.execute('INSERT INTO logs(' + ','.join(columns) + ') VALUES('
                                        + ','.join('?' for _ in columns) + ')', values)
                    if self.fts:
                        db.execute('INSERT INTO log_fts(rowid,raw) VALUES(?,?)', (cursor.lastrowid, raw))
                    audit['recognized_time' if parsed['ts'] is not None else 'unrecognized_time'] += 1
                    stats['records'] += 1

                def read_log(stream, meta, chain):
                    cursor = db.execute('INSERT INTO files(dataset,node,namespace,pod,service,kind,filename,archive,path,source,encoding) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                                        (identifier, *meta.values(), encoding))
                    fid = cursor.lastrowid
                    db.execute('UPDATE files SET archive_chain=? WHERE id=?', (json.dumps(chain), fid))
                    before = stats['records']
                    pending, parsed, start, end, size = [], None, 0, 0, 0
                    selected = encoding
                    for number, raw in enumerate(iter(lambda: stream.readline(max_record + 1), b''), 1):
                        if len(raw) > max_record:
                            raise ValueError('单行日志超过限制，请调整 LOG_MAX_RECORD_MB')
                        stats['bytes'] += len(raw)
                        if stats['bytes'] > maximum:
                            raise ValueError('解压后内容超过限制，请调整 LOG_MAX_EXPANDED_GB')
                        if selected == 'auto':
                            try:
                                value = raw.decode('utf-8-sig')
                            except UnicodeDecodeError:
                                value = raw.decode('gb18030', errors='replace')
                        else:
                            value = raw.decode(selected, errors='replace')
                        value = value.rstrip('\r\n')
                        if '\ufffd' in value and len(warnings) < 100:
                            warning = meta['source'] + '：存在无法解码的字符，可指定编码重新上传'
                            if warning not in warnings:
                                warnings.append(warning)
                        current = parse_line(value, offset, unit)
                        if current['ts'] is not None or not pending:
                            if pending:
                                add_record(fid, start, end, '\n'.join(pending), parsed)
                            pending, parsed, start, size = [value], current, number, len(raw)
                        else:
                            pending.append(value)
                            size += len(raw)
                        end = number
                        if size > max_record:
                            raise ValueError('单条多行日志超过限制，请调整 LOG_MAX_RECORD_MB')
                        if number % 10000 == 0:
                            self.progress[identifier] = dict(stats, current=meta['filename'])
                    if pending:
                        add_record(fid, start, end, '\n'.join(pending), parsed)
                    db.execute('UPDATE files SET records=? WHERE id=?', (stats['records'] - before, fid))
                    stats['files'] += 1
                    audit['physical_lines'] += end
                    actual_paths.add('/'.join([*chain[1:], meta['path']]).replace('\\', '/'))
                    self.progress[identifier] = dict(stats, current=meta['filename'])

                def walk(archive, chain, depth=0):
                    if depth > 4:
                        raise ValueError('压缩嵌套层数超过 4 层')
                    for item in archive.infolist():
                        stats['entries'] += 1
                        if stats['entries'] > 100000:
                            raise ValueError('压缩包文件条目超过 100000')
                        if item.is_dir():
                            continue
                        if item.flag_bits & 1:
                            raise ValueError('暂不支持加密 ZIP')
                        if item.file_size > maximum:
                            raise ValueError('压缩成员超过解压限制')
                        lower = item.filename.lower()
                        if lower.endswith('.zip'):
                            with archive.open(item) as source, tempfile.TemporaryFile() as temp:
                                copied = 0
                                while chunk := source.read(1024 * 1024):
                                    copied += len(chunk)
                                    stats['bytes'] += len(chunk)
                                    if copied > maximum or stats['bytes'] > maximum:
                                        raise ValueError('嵌套压缩包超过解压限制')
                                    temp.write(chunk)
                                temp.seek(0)
                                with zipfile.ZipFile(temp) as nested:
                                    walk(nested, chain + [item.filename], depth + 1)
                        else:
                            meta = source_meta(item.filename, chain)
                            if meta:
                                with archive.open(item) as source:
                                    if lower.endswith('.gz'):
                                        with gzip.GzipFile(fileobj=source) as uncompressed:
                                            read_log(uncompressed, meta, chain)
                                    else:
                                        read_log(source, meta, chain)
                with zipfile.ZipFile(path) as archive:
                    if 'fileList.txt' in archive.namelist():
                        audit['manifest_present'] = True
                        with archive.open('fileList.txt') as manifest:
                            content = manifest.read(8 * 1024 * 1024 + 1)
                        if len(content) > 8 * 1024 * 1024:
                            warnings.append('fileList.txt 超过 8 MB，未核对清单；实际压缩包仍正常遍历。')
                            audit['manifest_checked'] = False
                        else:
                            listed_paths = {line.strip().replace('\\', '/').removeprefix('./')
                                            for line in content.decode('utf-8-sig', errors='replace').splitlines() if line.strip()}
                            audit['manifest_checked'] = True
                    walk(archive, [name])
                if not stats['files']:
                    raise ValueError('未找到符合 namespace_pod/service/pod-service/log/ 结构的日志')
                # Publish the original archive before committing the searchable dataset.
                shutil.move(str(path), str(self.directory / 'archives' / (identifier + '.zip')))
                audit.update(actual_files=len(actual_paths), listed_files=len(listed_paths))
                if audit.get('manifest_checked'):
                    missing, unlisted = sorted(listed_paths - actual_paths), sorted(actual_paths - listed_paths)
                    audit.update(missing_count=len(missing), unlisted_count=len(unlisted), missing=missing[:100], unlisted=unlisted[:100])
                    if missing or unlisted:
                        warnings.append(f'清单核对：{len(missing)} 个清单路径未导入，{len(unlisted)} 个实际日志不在清单中。')
                db.execute("UPDATE datasets SET state='ready',files=?,records=?,warnings=?,parser_version=?,audit=? WHERE id=?",
                           (stats['files'], stats['records'], json.dumps(warnings, ensure_ascii=False), PARSER_VERSION,
                            json.dumps(audit, ensure_ascii=False), identifier))
        except Exception as exc:
            (self.directory / 'archives' / (identifier + '.zip')).unlink(missing_ok=True)
            with self.connect() as db:
                db.execute("UPDATE datasets SET state='failed',error=? WHERE id=?", (str(exc), identifier))
        finally:
            Path(path).unlink(missing_ok=True)
            self.progress.pop(identifier, None)

    def datasets(self):
        with self.connect() as db:
            rows = [dict(row) for row in db.execute('SELECT * FROM datasets ORDER BY created DESC')]
        for row in rows:
            row['warnings'] = json.loads(row['warnings'])
            row['audit'] = json.loads(row['audit'])
            if row['parser_version'] < PARSER_VERSION:
                row['warnings'].append('该日志包使用旧版解析器，请重新上传以修正 Pod 并补充 RouteID 等新字段。')
            row['progress'] = self.progress.get(row['id'])
            archive = self.directory / 'archives' / (row['id'] + '.zip')
            row['archive_bytes'] = archive.stat().st_size if archive.exists() else 0
        return rows

    def request_delete(self, identifier):
        with self.connect() as db:
            row = db.execute('SELECT state FROM datasets WHERE id=?', (identifier,)).fetchone()
            if not row:
                raise ValueError('日志包不存在或已经删除')
            if row['state'] == 'importing':
                raise ValueError('日志包正在导入，完成后再删除')
            if row['state'] == 'deleting':
                return
            db.execute("UPDATE datasets SET state='deleting',error='' WHERE id=?", (identifier,))
        self.pool.submit(self.delete_dataset, identifier, row['state'])

    def delete_dataset(self, identifier, previous_state='ready'):
        try:
            with self.connect() as db:
                if self.fts:
                    db.execute('DELETE FROM log_fts WHERE rowid IN (SELECT id FROM logs WHERE dataset=?)',
                               (identifier,))
                db.execute('DELETE FROM logs WHERE dataset=?', (identifier,))
                db.execute('DELETE FROM files WHERE dataset=?', (identifier,))
            (self.directory / 'archives' / (identifier + '.zip')).unlink(missing_ok=True)
            # Return free pages to the operating system after large packages. If
            # compaction lacks temporary disk space, the pages remain reusable.
            try:
                with self.connect() as db:
                    db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                    db.execute('VACUUM')
            except sqlite3.Error:
                pass
            with self.connect() as db:
                db.execute('DELETE FROM datasets WHERE id=?', (identifier,))
        except Exception as exc:
            with self.connect() as db:
                if db.execute('SELECT 1 FROM datasets WHERE id=?', (identifier,)).fetchone():
                    db.execute('UPDATE datasets SET state=?,error=? WHERE id=?',
                               (previous_state, '删除失败：' + str(exc), identifier))
        finally:
            self.progress.pop(identifier, None)

    def require_ready(self, db, identifier):
        row = db.execute('SELECT state FROM datasets WHERE id=?', (identifier,)).fetchone()
        if not row or row['state'] != 'ready':
            raise ValueError('请先选择导入完成的日志包')

    def filters(self, identifier):
        with self.connect() as db:
            self.require_ready(db, identifier)
            return [dict(row) for row in db.execute('SELECT * FROM files WHERE dataset=? ORDER BY node,pod,kind,filename', (identifier,))]

    def dataset_scope(self, identifier):
        """Small task snapshot so an Agent does not spend turns rediscovering the index."""
        with self.connect() as db:
            self.require_ready(db, identifier)
            dataset = dict(db.execute('SELECT name,files,records,warnings,audit,parser_version FROM datasets WHERE id=?',
                                      (identifier,)).fetchone())
            dimensions = {}
            for column in ('node', 'namespace', 'pod', 'service', 'kind'):
                values = [row[0] for row in db.execute(
                    f'SELECT DISTINCT {column} FROM files WHERE dataset=? ORDER BY {column} LIMIT 201', (identifier,))]
                dimensions[column + 's'] = values[:200]
                if len(values) > 200:
                    dimensions[column + 's_truncated'] = True
        dataset['warnings'] = json.loads(dataset['warnings'])
        dataset['audit'] = json.loads(dataset['audit'])
        dataset.update(dimensions)
        return dataset

    def query_parts(self, params):
        identifier = params.get('dataset', '')
        clauses, args = ['l.dataset=?'], [identifier]
        for key in ('node', 'pod', 'namespace', 'service', 'kind'):
            if params.get(key):
                clauses.append('f.' + key + '=?')
                args.append(params[key])
        if params.get('file_id'):
            clauses.append('f.id=?')
            args.append(int(params['file_id']))
        for key in ('trace', 'thread', 'thread_id', 'level', 'route_id', 'request_id'):
            if params.get(key):
                clauses.append('l.' + key + '=?')
                args.append(params[key])
        if params.get('request_key'):
            clauses.append('(l.route_id=? OR l.request_id=?)')
            args.extend([params['request_key'], params['request_key']])
        if params.get('endpoint'):
            endpoint = params['endpoint'].split('?', 1)[0]
            # This form can use logs_url for both an exact URL and the same path
            # followed by a query string; the previous substr expression could not.
            clauses.append("l.url!='' AND l.url>=? AND l.url<? AND "
                           "(l.url=? OR substr(l.url,length(?)+1,1)='?')")
            args.extend([endpoint, endpoint + '@', endpoint, endpoint])
        if params.get('filename'):
            clauses.append('f.filename GLOB ?')
            args.append(params['filename'])
        for key, op in (('start', '>='), ('end', '<=')):
            if params.get(key):
                value = timestamp(params[key])
                if value is None:
                    raise ValueError('时间格式不正确')
                clauses.append('l.ts' + op + '?')
                args.append(value)
        if params.get('access_only') == '1':
            clauses.append("l.method != ''")
        if params.get('status'):
            if re.fullmatch(r'[1-5]xx', params['status']):
                clauses.append('l.status BETWEEN ? AND ?')
                args.extend([int(params['status'][0]) * 100, int(params['status'][0]) * 100 + 99])
            else:
                clauses.append('l.status=?')
                args.append(int(params['status']))
        if params.get('min_duration'):
            clauses.append('l.duration>=?')
            args.append(float(params['min_duration']))
        keyword = params.get('q', '')
        if len(keyword) > 2000:
            raise ValueError('搜索词最多 2000 个字符')
        if keyword:
            if self.fts and params.get('scan') != '1' and len(keyword) >= 3 and '\n' not in keyword:
                clauses.append('l.id IN (SELECT rowid FROM log_fts WHERE log_fts MATCH ?)')
                args.append('"' + keyword.replace('"', '""') + '"')
            clauses.append('instr(l.raw,?)>0' if params.get('case') == '1' else 'instr(lower(l.raw),lower(?))>0')
            args.append(keyword)
        return ' AND '.join(clauses), args

    def search(self, params):
        before = time.monotonic()
        where, args = self.query_parts(params)
        page, size = max(1, int(params.get('page', 1))), min(200, max(1, int(params.get('size', 50))))
        join = ' FROM logs l JOIN files f ON f.id=l.file_id WHERE ' + where
        order = 'l.ts IS NULL,l.ts,l.id'
        if params.get('order') == 'errors_slow':
            order = '(l.status >= 400) DESC,l.duration DESC,l.ts,l.id'
        with self.connect() as db:
            self.require_ready(db, params.get('dataset', ''))
            summary = dict(db.execute('''SELECT count(*) total, count(DISTINCT f.node) nodes,
                count(DISTINCT f.namespace || '/' || f.pod) pods, count(DISTINCT f.id) files,
                sum(CASE WHEN l.status>=400 OR l.level IN ('ERROR','FATAL') THEN 1 ELSE 0 END) errors,
                avg(l.duration) avg_duration''' + join, args).fetchone())
            rows = [dict(row) for row in db.execute('SELECT l.*,f.node,f.namespace,f.pod,f.service,f.kind,f.filename,f.archive,f.path,f.source' + join +
                                                   ' ORDER BY ' + order + ' LIMIT ? OFFSET ?', args + [size, (page - 1) * size])]
        return dict(rows=rows, summary=summary, page=page, size=size, elapsed_ms=round((time.monotonic() - before) * 1000), fts=self.fts)

    def context(self, identifier, radius=10):
        with self.connect() as db:
            row = db.execute('SELECT * FROM logs WHERE id=?', (identifier,)).fetchone()
            if not row:
                raise ValueError('日志不存在')
            self.require_ready(db, row['dataset'])
            return [dict(r) for r in db.execute('SELECT * FROM logs WHERE file_id=? AND line BETWEEN ? AND ? ORDER BY line',
                                               (row['file_id'], max(1, row['line'] - radius), row['end_line'] + radius))]

    def record(self, identifier):
        with self.connect() as db:
            row = db.execute('SELECT l.*,f.node,f.namespace,f.pod,f.service,f.kind,f.filename,f.source,f.path,f.archive_chain,f.encoding '
                             'FROM logs l JOIN files f ON f.id=l.file_id WHERE l.id=?', (identifier,)).fetchone()
            if not row:
                raise ValueError('日志不存在')
            self.require_ready(db, row['dataset'])
            return dict(row)

    def correlate(self, identifier, seconds=5, same_thread=False, page=1, kind='', size=50):
        row = self.record(identifier)
        if row['ts'] is None:
            raise ValueError('该日志没有可识别的时间')
        if same_thread and not row['thread']:
            raise ValueError('该日志没有线程字段，请取消同线程限制')
        seconds = min(3600, max(1, float(seconds)))
        def at(ms):
            return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f +0000')
        lookback = seconds * 1000 + max(0, row['duration'] or 0)
        params = dict(dataset=row['dataset'], node=row['node'], namespace=row['namespace'], pod=row['pod'],
                      start=at(row['ts'] - lookback), end=at(row['ts'] + seconds * 1000),
                      thread=row['thread'] if same_thread else '', kind=kind, page=page, size=size)
        result = self.search(params)
        for candidate in result['rows']:
            reasons = ['same_pod_time_window']
            if row['thread'] and candidate['thread'] == row['thread']:
                reasons.append('same_thread')
            if row['trace'] and candidate['trace'] == row['trace']:
                reasons.append('same_trace_id')
            keys = {row.get('route_id'), row.get('request_id')} - {'', None}
            if keys.intersection({candidate.get('route_id'), candidate.get('request_id')}):
                reasons.append('same_request_key')
            candidate['association_reasons'] = reasons
        result.update(association='candidate', anchor_id=identifier, filters=params,
                      window_note='向前窗口包含 access 耗时以覆盖请求处理期间；日志时间是否代表完成时刻仍需按实际配置确认。',
                      caveat='时间/线程关联只是候选；线程复用、异步切换、节点时钟偏差都可能影响判断。')
        return result

    def verify(self, identifier):
        row = self.record(identifier)
        path = self.directory / 'archives' / (row['dataset'] + '.zip')
        if not path.exists() or not row['archive_chain']:
            return dict(available=False, verified=False, id=identifier, source=row['source'],
                        reason='旧版导入未保留原始 ZIP，请重新上传后核验。')
        chain = json.loads(row['archive_chain'])
        max_bytes = int(os.getenv('LOG_MAX_EXPANDED_GB', '20')) * 1024 ** 3
        max_line = int(os.getenv('LOG_MAX_RECORD_MB', '8')) * 1024 ** 2
        copied, lines = 0, []
        with contextlib.ExitStack() as stack:
            archive = stack.enter_context(zipfile.ZipFile(path))
            for member in chain[1:]:
                matches = [entry for entry in archive.infolist() if entry.filename == member]
                if len(matches) != 1:
                    raise ValueError('压缩包包含重名成员，无法唯一核验来源')
                source = stack.enter_context(archive.open(matches[0]))
                temp = stack.enter_context(tempfile.TemporaryFile())
                while chunk := source.read(1024 * 1024):
                    copied += len(chunk)
                    if copied > max_bytes:
                        raise ValueError('原文核验超过读取限制')
                    temp.write(chunk)
                temp.seek(0)
                archive = stack.enter_context(zipfile.ZipFile(temp))
            matches = [entry for entry in archive.infolist() if entry.filename == row['path']]
            if len(matches) != 1:
                raise ValueError('日志包含重名成员，无法唯一核验来源')
            source = stack.enter_context(archive.open(matches[0]))
            if row['filename'].lower().endswith('.gz'):
                source = stack.enter_context(gzip.GzipFile(fileobj=source))
            for number in range(1, row['end_line'] + 1):
                raw = source.readline(max_line + 1)
                copied += len(raw)
                if len(raw) > max_line or copied > max_bytes:
                    raise ValueError('原文核验超过读取限制')
                if not raw:
                    break
                if number < row['line']:
                    continue
                if row['encoding'] == 'auto':
                    try:
                        text = raw.decode('utf-8-sig')
                    except UnicodeDecodeError:
                        text = raw.decode('gb18030', errors='replace')
                else:
                    text = raw.decode(row['encoding'], errors='replace')
                lines.append(text.rstrip('\r\n'))
        original = '\n'.join(lines)
        return dict(available=True, verified=original == row['raw'], id=identifier, source=row['source'],
                    line=row['line'], end_line=row['end_line'], raw=original,
                    comparison='按导入编码解码并去除行尾换行符后，与索引原文比较；不是日志语义准确性的证明。')

    def export(self, params):
        where, args = self.query_parts(params)
        with self.connect() as db:
            self.require_ready(db, params.get('dataset', ''))
            for row in db.execute('SELECT l.*,f.node,f.namespace,f.pod,f.kind,f.filename,f.archive,f.source FROM logs l JOIN files f ON f.id=l.file_id WHERE ' + where + ' ORDER BY l.ts IS NULL,l.ts,l.id', args):
                yield (json.dumps(dict(row), ensure_ascii=False) + '\n').encode('utf-8')


class Handler(BaseHTTPRequestHandler):
    server_version = 'LogScope/1.13'
    def log_message(self, fmt, *args):
        pass
    @property
    def store(self):
        return self.server.store
    def json(self, value, status=200):
        raw = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(raw)
    def allowed(self):
        host = self.headers.get('Host', '')
        if host not in self.server.allowed_hosts:
            self.json({'error': 'Host 不受信任'}, 403)
            return False
        origin = self.headers.get('Origin')
        if origin and origin != 'http://' + host:
            self.json({'error': '不允许跨域请求'}, 403)
            return False
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            self.json({'error': '不允许跨站请求'}, 403)
            return False
        return True
    def do_GET(self):
        if not self.allowed():
            return
        parsed = urlsplit(self.path)
        params = {k: v[0] for k, v in parse_qs(parsed.query, keep_blank_values=True).items()}
        try:
            if parsed.path == '/api/datasets':
                self.json(self.store.datasets())
            elif parsed.path == '/api/collector/capability':
                self.json(self.server.collector.capability())
            elif parsed.path == '/api/collector/status':
                self.json(self.server.collector.status(params.get('id', '')))
            elif parsed.path == '/api/files':
                self.json(self.store.filters(params.get('dataset', '')))
            elif parsed.path == '/api/search':
                self.json(self.store.search(params))
            elif parsed.path == '/api/context':
                self.json(self.store.context(int(params['id']), min(200, max(1, int(params.get('radius', 15))))))
            elif parsed.path == '/api/record':
                self.json(self.store.record(int(params['id'])))
            elif parsed.path == '/api/verify':
                self.json(self.store.verify(int(params['id'])))
            elif parsed.path == '/api/correlate':
                self.json(self.store.correlate(int(params['id']), params.get('seconds', 5), params.get('same_thread') == '1',
                                               params.get('page', 1), params.get('kind', ''), params.get('size', 50)))
            elif parsed.path == '/api/terminal/config':
                self.json(self.server.terminals.config())
            elif parsed.path == '/api/analysis/rules':
                self.json(self.server.terminals.rules.get(params.get('version')))
            elif parsed.path == '/api/terminal/task':
                self.json(self.server.terminals.task_details(params['id']))
            elif parsed.path == '/api/terminal/sessions':
                self.json(self.server.terminals.list())
            elif parsed.path == '/api/terminal/output':
                self.json(self.server.terminals.get(params['id']).poll(params.get('cursor', 0)))
            elif parsed.path == '/api/terminal/history':
                self.json(self.server.terminals.history(params['id']))
            elif parsed.path == '/api/terminal/report':
                self.json(self.server.terminals.report(params['id']))
            elif parsed.path == '/api/project/branches':
                self.json(self.server.terminals.project_branches(params.get('path', '')))
            elif parsed.path == '/api/export':
                iterator = self.store.export(params)
                first = next(iterator, b'')
                self.send_response(200)
                self.send_header('Content-Type', 'application/x-ndjson; charset=utf-8')
                self.send_header('Content-Disposition', 'attachment; filename="log-results.ndjson"')
                self.send_header('Connection', 'close')
                self.end_headers()
                self.close_connection = True
                try:
                    self.wfile.write(first)
                    for chunk in iterator:
                        self.wfile.write(chunk)
                finally:
                    iterator.close()
            else:
                routes = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css',
                          '/enhancements.css': 'enhancements.css', '/terminal.js': 'terminal.js',
                          '/vendor/xterm.js': 'vendor/xterm.js', '/vendor/xterm.css': 'vendor/xterm.css',
                          '/vendor/addon-fit.js': 'vendor/addon-fit.js'}
                if parsed.path not in routes:
                    return self.json({'error': '不存在'}, 404)
                file = BASE / 'dist' / routes[parsed.path]
                raw = file.read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', {'.html':'text/html; charset=utf-8','.js':'application/javascript; charset=utf-8','.css':'text/css; charset=utf-8'}[file.suffix])
                self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (ValueError, KeyError, sqlite3.Error, OSError, zipfile.BadZipFile) as exc:
            self.json({'error': str(exc)}, 400)
    def do_POST(self):
        if not self.allowed():
            return
        parsed = urlsplit(self.path)
        params = {k:v[0] for k,v in parse_qs(parsed.query).items()}
        temporary = None
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if parsed.path == '/api/upload':
                if size <= 0 or size > int(os.getenv('LOG_MAX_UPLOAD_GB', '4')) * 1024 ** 3:
                    return self.json({'error':'上传大小无效，默认最大 4 GB'}, 413)
                name = params.get('name', 'logs.zip').replace('\\', '/').split('/')[-1]
                if not name.lower().endswith('.zip'):
                    raise ValueError('请上传 ZIP 文件')
                with tempfile.NamedTemporaryFile(delete=False, dir=self.store.directory, suffix='.upload') as file:
                    temporary = file.name
                    remaining = size
                    while remaining:
                        chunk = self.rfile.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise ValueError('上传中断')
                        file.write(chunk)
                        remaining -= len(chunk)
                if not zipfile.is_zipfile(temporary):
                    raise ValueError('文件不是有效的 ZIP')
                identifier = self.store.submit(temporary, name, params.get('encoding', 'auto'), params.get('offset', '+0800'), params.get('unit', 'ms'))
                temporary = None
                self.json({'id': identifier}, 202)
            else:
                if size < 0 or size > 100000:
                    raise ValueError('请求过大')
                if self.headers.get_content_type() != 'application/json':
                    raise ValueError('需要 application/json 请求')
                body = json.loads(self.rfile.read(size) or b'{}')
                if parsed.path == '/api/terminal/config':
                    self.json(self.server.terminals.save_config(body))
                elif parsed.path == '/api/collector/start':
                    self.json(self.server.collector.start(body), 202)
                elif parsed.path == '/api/analysis/rules':
                    self.json(self.server.terminals.rules.save(body))
                elif parsed.path == '/api/terminal/preview':
                    self.json(self.server.terminals.preview(body))
                elif parsed.path == '/api/terminal/code-preview':
                    self.json(self.server.terminals.preview_code(body))
                elif parsed.path == '/api/terminal/code-task':
                    self.json(self.server.terminals.create_code_task(body))
                elif parsed.path == '/api/terminal/rules':
                    self.json(self.server.terminals.update_rules(body))
                elif parsed.path == '/api/terminal/start':
                    self.json(self.server.terminals.start(body), 201)
                elif parsed.path == '/api/terminal/resume':
                    self.json(self.server.terminals.resume(body), 201)
                elif parsed.path == '/api/terminal/session-id':
                    self.json(self.server.terminals.save_ai_session_id(body))
                elif parsed.path == '/api/datasets/delete':
                    identifier = str(body.get('dataset', ''))
                    if self.server.terminals.dataset_in_use(identifier):
                        raise ValueError('该日志包正在被 AI 终端使用，请先结束对应终端')
                    self.store.request_delete(identifier)
                    self.json({'ok': True, 'id': identifier}, 202)
                elif parsed.path == '/api/terminal/input':
                    self.server.terminals.get(body['id']).write(body.get('data', ''))
                    self.json({'ok': True})
                elif parsed.path == '/api/terminal/resize':
                    cols, rows = dimensions(body.get('cols', 100), body.get('rows', 30))
                    self.server.terminals.get(body['id']).pty.resize(cols, rows)
                    self.json({'ok': True})
                elif parsed.path == '/api/terminal/stop':
                    session = self.server.terminals.get(body['id'])
                    if body.get('mode') == 'graceful':
                        self.json(session.request_stop())
                    else:
                        session.stop()
                        self.json(session.info())
                elif parsed.path == '/api/terminal/delete':
                    self.json(self.server.terminals.delete(body))
                else:
                    self.json({'error':'不存在'}, 404)
        except (ValueError, KeyError, OSError, EOFError) as exc:
            self.json({'error': str(exc)}, 400)
        finally:
            if temporary:
                Path(temporary).unlink(missing_ok=True)


class LocalServer(ThreadingHTTPServer):
    def server_close(self):
        if hasattr(self, 'collector'):
            self.collector.close()
        if hasattr(self, 'terminals'):
            self.terminals.close()
        super().server_close()


def make_server(directory, port=8765, collect_script=None):
    server = LocalServer(('127.0.0.1', port), Handler)
    server.store = Store(directory)
    port = server.server_address[1]
    server.allowed_hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
    server.terminals = TerminalManager(server.store, f'http://127.0.0.1:{port}')
    server.collector = LogCollector(server.store, collect_script or BASE / 'collect_logs.py')
    return server


def main():
    parser = argparse.ArgumentParser(description='LogScope 本地日志分析')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--data', default=str(BASE / 'data'))
    args = parser.parse_args()
    server = make_server(args.data, args.port)
    print(f'LogScope 已启动：http://127.0.0.1:{server.server_address[1]}', flush=True)
    print('日志只存储在本机。按 Ctrl+C 停止。', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        server.store.pool.shutdown(wait=True)


if __name__ == '__main__':
    main()
