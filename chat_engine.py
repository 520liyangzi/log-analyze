"""Persistent native investigations, independent of CLI terminals and provider sessions."""
import concurrent.futures
import copy
import datetime as dt
import json
from pathlib import Path
import shutil
import sqlite3
import threading
import time
import uuid

from ai_client import Cancelled, ModelClient, ModelConfig
from analysis_rules import AnalysisRules
from chat_projects import ChatProjects, query_project
from runtime_paths import RESOURCE_ROOT


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def dumps(value):
    return json.dumps(value, ensure_ascii=False)


def tool(name, description, properties, required=()):
    return dict(name=name, description=description,
                parameters=dict(type='object', properties=properties, required=list(required), additionalProperties=False))


STRING = {'type': 'string'}
INTEGER = {'type': 'integer'}
TOOLS = [
    tool('search_logs', '搜索本会话日志索引。条件可叠加。流水号按完整字符串匹配。核对总数和分页，不能把第一页当全部。',
         {**{k: STRING for k in ('q', 'endpoint', 'trace', 'node', 'namespace', 'pod', 'service', 'kind', 'filename',
                                  'thread', 'thread_id', 'request_key', 'level', 'status', 'start', 'end')},
          'page': INTEGER, 'size': INTEGER, 'access_only': {'type': 'boolean'},
          'min_duration': {'type': 'number'}, 'order': {'type': 'string', 'enum': ['time', 'errors_slow']}}),
    tool('log_context', '读取已命中日志的原文件上下文及堆栈。', {'id': INTEGER, 'radius': INTEGER}, ['id']),
    tool('correlate_logs', '查同 Pod 相邻时间的候选日志；时间/线程接近不是因果证明。',
         {'id': INTEGER, 'seconds': {'type': 'number'}, 'page': INTEGER, 'kind': STRING}, ['id']),
    tool('verify_log', '只对关键证据回读原始 ZIP 核验原文；不会重建索引。', {'id': INTEGER}, ['id']),
    tool('project_search', '在会话固定 commit 中按字面关键词搜索代码，不切换工作区、不执行项目代码。',
         {'keyword': STRING, 'path': STRING}, ['keyword']),
    tool('project_read', '读取已命中文件的局部代码，单次最多 300 行。',
         {'path': STRING, 'start': INTEGER, 'end': INTEGER}, ['path']),
]
TOOL_MAP = {t['name']: t for t in TOOLS}


def validate_tool(name, args):
    if name not in TOOL_MAP or not isinstance(args, dict):
        raise ValueError('未知工具或参数格式错误；不提供任意命令执行')
    schema = TOOL_MAP[name]['parameters']
    if set(args) - set(schema['properties']) or any(k not in args for k in schema['required']):
        raise ValueError('工具参数缺失或存在未支持的字段')
    for key, value in args.items():
        rule = schema['properties'][key]
        expected = rule['type']
        valid = ((expected == 'string' and isinstance(value, str) and len(value) <= 1000)
                 or (expected == 'integer' and type(value) is int)
                 or (expected == 'number' and type(value) in (int, float))
                 or (expected == 'boolean' and type(value) is bool))
        if not valid or ('enum' in rule and value not in rule['enum']):
            raise ValueError('工具参数类型或长度错误：' + key)


class ChatManager:
    def __init__(self, store):
        self.store = store
        self.config = ModelConfig(store.directory)
        self.rules = AnalysisRules(store.directory)
        self.projects = ChatProjects()
        self.path = store.directory / 'chat.sqlite3'
        self.directory = store.directory / 'chat-sessions'
        self.directory.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.active = {}
        self.previews = {}
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=3)
        with self.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(position INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT UNIQUE,session TEXT,version INTEGER,kind TEXT,body TEXT);
                CREATE INDEX IF NOT EXISTS events_session ON events(session,version);
                CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY,session TEXT);''')
            self.version = db.execute('SELECT coalesce(max(version),0) FROM events').fetchone()[0]
            sessions = db.execute('SELECT data FROM sessions').fetchall()
            for row in sessions:
                value = json.loads(row[0])
                if value['state'] in ('running', 'stopping'):
                    value.update(state='interrupted', status='服务已重启，历史已保存；可继续追问或重试。')
                    db.execute('UPDATE sessions SET data=? WHERE id=?', (dumps(value), value['id']))
            for row in db.execute("SELECT id,kind,body FROM events WHERE kind IN ('assistant','tool')").fetchall():
                body = json.loads(row['body'])
                if body.get('streaming') or body.get('state') == 'running':
                    body.update(streaming=False, interrupted=True)
                    if row['kind'] == 'tool':
                        body['state'] = 'failed'
                    self.version += 1
                    db.execute('UPDATE events SET body=?,version=? WHERE id=?', (dumps(body), self.version, row['id']))

    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def _get(self, identifier):
        with self.db() as db:
            row = db.execute('SELECT data FROM sessions WHERE id=?', (identifier,)).fetchone()
        if not row:
            raise ValueError('会话不存在或已被删除')
        return json.loads(row[0])

    def _save(self, session):
        session['updated'] = now()
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO sessions VALUES (?,?)', (session['id'], dumps(session)))

    def _status(self, identifier, **changes):
        with self.lock:
            session = self._get(identifier)
            session.update(changes)
            self._save(session)

    @staticmethod
    def _public(session):
        return {k: session[k] for k in ('id', 'title', 'state', 'status', 'created', 'updated', 'task')}

    def list(self):
        with self.lock, self.db() as db:
            values = [self._public(json.loads(row[0])) for row in db.execute('SELECT data FROM sessions')]
        return sorted(values, key=lambda s: s['updated'], reverse=True)

    def get(self, identifier, after=0):
        with self.lock, self.db() as db:
            session = self._get(identifier)
            events = [dict(id=r['id'], kind=r['kind'], body=json.loads(r['body']), position=r['position'])
                      for r in db.execute('SELECT * FROM events WHERE session=? AND version>? ORDER BY position', (identifier, max(0, int(after))))]
            return dict(session=self._public(session), events=events, cursor=self.version)

    def event(self, identifier, kind, body, event_id=None):
        with self.lock, self.db() as db:
            self.version += 1
            event_id = event_id or uuid.uuid4().hex
            db.execute('''INSERT INTO events(id,session,version,kind,body) VALUES (?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET version=excluded.version,body=excluded.body''',
                       (event_id, identifier, self.version, kind, dumps(body)))
        return event_id

    def preview(self, body):
        question = str(body.get('question', '')).strip()
        if not question or len(question) > 20000:
            raise ValueError('问题不能为空且最多 20000 字符')
        identifier = str(body.get('id', ''))
        existing = self._get(identifier) if identifier else None
        dataset = existing['task']['dataset'] if existing else str(body.get('dataset', ''))
        scope = self.store.dataset_scope(dataset)
        rules = existing['rules'] if existing else self.rules.snapshot()
        project = existing['task'].get('project') if existing else None
        if body.get('use_code') is True:
            project = self.projects.snapshot(body) if body.get('sync_id') else project
            if not project:
                raise ValueError('请先同步项目并选择分支')
        elif body.get('use_code') is False:
            project = None
        task = dict(dataset=dataset, name=scope['name'], scope=scope, project=project, rules_version=rules['version'])
        system = (RESOURCE_ROOT / 'prompts/native-analysis.md').read_text('utf-8')
        # Versioned workflows are reused as domain guidance, not executable CLI instructions.
        system += '\n\n本次日志范围与代码版本：\n' + dumps(task)
        system += '\n\n分析规则（其中旧 CLI 命令用同义内置工具代替，不运行终端）：\n' + rules['workflow']
        system += '\n\n业务规则：\n' + (rules['business'] or '暂无')
        text = system + '\n\n可用工具：\n' + dumps(self.tools(task)) + '\n\n用户本次问题：\n' + question
        token = uuid.uuid4().hex
        with self.lock:
            self.previews = {k: v for k, v in self.previews.items() if time.monotonic() - v['at'] < 1800}
            if len(self.previews) >= 100:
                raise ValueError('待提交预览过多，请稍后重试')
            self.previews[token] = dict(at=time.monotonic(), existing=identifier, task=task, rules=rules,
                                        system=system, text=text, question=question,
                                        expected_updated=existing['updated'] if existing else None)
        return dict(preview_id=token, text=text, task=task)

    @staticmethod
    def tools(task):
        return [t for t in TOOLS if task.get('project') or not t['name'].startswith('project_')]

    def send(self, body):
        request_id = str(body.get('request_id', ''))
        if not request_id or len(request_id) > 100:
            raise ValueError('缺少有效的请求标识')
        with self.lock, self.db() as db:
            previous = db.execute('SELECT session FROM requests WHERE id=?', (request_id,)).fetchone()
            if previous:
                return self._public(self._get(previous[0]))
            draft = self.previews.get(str(body.get('preview_id', '')))
            if not draft or time.monotonic() - draft['at'] > 1800:
                raise ValueError('预览已过期，请重新预览')
            config = self.config.load()
            with self.store.connect() as logs:
                self.store.require_ready(logs, draft['task']['dataset'])
            if len(self.active) >= 3:
                raise ValueError('已有 3 个排查任务运行中，请稍后提交')
            identifier = draft['existing'] or uuid.uuid4().hex
            if identifier in self.active:
                raise ValueError('本会话正在分析，请停止或等待完成后再发送')
            if draft['existing']:
                session = self._get(identifier)
                if session['updated'] != draft['expected_updated']:
                    raise ValueError('其他页面已更新这个会话，请刷新并重新预览')
            else:
                session = dict(id=identifier, title=draft['question'][:60], created=now(), wire=[],
                               state='idle', status='', task=draft['task'])
            directory = self.directory / identifier
            directory.mkdir(exist_ok=True)
            (directory / 'task.md').write_text(draft['text'], 'utf-8')
            project_changed = session['task'].get('project') != draft['task'].get('project')
            session.update(state='running', status='正在准备模型请求…', task=draft['task'],
                           rules=draft['rules'], system=draft['system'])
            content = draft['question']
            if project_changed:
                content += '\n\n本轮代码范围已由用户更新：' + dumps(draft['task'].get('project')) + '。历史证据属于原 commit，请勿混用。'
            session['wire'].append(dict(role='user', content=content))
            self._save(session)
            db.execute('INSERT INTO requests VALUES (?,?)', (request_id, identifier))
            self.previews.pop(str(body['preview_id']), None)
            stop = threading.Event()
            client = ModelClient(config, stop)
            self.active[identifier] = dict(stop=stop, client=client)
        self.event(identifier, 'user', dict(text=draft['question']))
        self.event(identifier, 'scope', dict(text='使用已建立的日志索引；代码按固定 commit 只读查询。', task=draft['task']))
        self.pool.submit(self._run, identifier, client, stop)
        return self._public(session)

    def _wire(self, identifier, messages):
        with self.lock:
            session = self._get(identifier)
            session['wire'] = copy.deepcopy(messages)
            self._save(session)

    @staticmethod
    def budget(messages, maximum):
        result = copy.deepcopy(messages)
        removed = False
        while len(dumps(result)) > maximum:
            next_turn = next((i for i, m in enumerate(result[1:], 1) if m['role'] == 'user'), None)
            if next_turn is not None:
                result = result[next_turn:]
                removed = True
                continue
            candidate = next((m for m in result if m['role'] == 'tool' and len(m['content']) > 1000), None)
            if candidate is None:
                raise ValueError('本次上下文超过配置上限，请缩小问题范围或新建会话')
            candidate['content'] = '{"note":"较早工具结果因上下文预算省略；完整结果仍在界面证据中，不得据此断言无结果。"}'
            removed = True
        return result, removed

    def execute(self, session, name, args):
        validate_tool(name, args)
        if name.startswith('project_'):
            return query_project(session['task'].get('project'), name, args)
        dataset = session['task']['dataset']
        if name == 'search_logs':
            params = dict(args, dataset=dataset, size=min(50, max(1, args.get('size', 20))))
            if 'access_only' in params:
                params['access_only'] = '1' if params['access_only'] else ''
            result = self.store.search(params)
        else:
            row = self.store.record(args['id'])
            if row['dataset'] != dataset:
                raise ValueError('日志不在本会话选择的日志包中')
            if name == 'log_context':
                rows = self.store.context(args['id'], min(50, max(1, args.get('radius', 15))))
                result = dict(rows=[dict(r, source=row['source'], node=row['node'], pod=row['pod'], filename=row['filename']) for r in rows])
            elif name == 'verify_log':
                result = self.store.verify(args['id'])
            else:
                result = self.store.correlate(args['id'], args.get('seconds', 10), False, args.get('page', 1), args.get('kind', ''), 20)
        if 'rows' in result:
            for row in result['rows']:
                if len(row.get('raw', '')) > 3500:
                    row['raw'] = row['raw'][:3500] + '\n[长日志截断，需核验原文]'
            result.update(returned=len(result['rows']))
            if 'summary' in result:
                result['has_more'] = result['page'] * result['size'] < result['summary']['total']
                result['next_page'] = result['page'] + 1 if result['has_more'] else None
        return result

    @staticmethod
    def bounded(result):
        result = copy.deepcopy(result)
        for key in ('rows', 'matches'):
            if key in result:
                while len(dumps(result)) > 24000 and result[key]:
                    result[key].pop()
                    result['truncated'] = True
                result['returned'] = len(result[key])
        if len(dumps(result)) > 26000:
            return dict(truncated=True, note='结果超过本次传输预算，请缩小读取范围；不能将截断视为无结果。', excerpt=dumps(result)[:24000])
        return result

    def _run(self, identifier, client, stop):
        session = self._get(identifier)
        messages = session['wire']
        seen = set()
        event_id, latest_text = None, ['']
        try:
            for index in range(client.config['max_tool_rounds'] + 1):
                if stop.is_set():
                    raise Cancelled()
                final_round = index == client.config['max_tool_rounds']
                self._status(identifier, status=f'第 {index + 1} 轮：等待模型分析…')
                event_id = self.event(identifier, 'assistant', dict(text='', streaming=True, round=index + 1))
                latest_text[0] = ''
                before, last_saved = time.monotonic(), [0.0]
                context, shortened = self.budget(messages, max(1000, client.config['max_context_chars'] - len(session['system'])))
                if shortened:
                    self.event(identifier, 'notice', dict(text='本轮仅发送预算内的近期对话/证据；完整历史保留在本机。'))

                def on_text(text):
                    latest_text[0] = text
                    if time.monotonic() - last_saved[0] > .3:
                        self.event(identifier, 'assistant', dict(text=text, streaming=True, round=index + 1), event_id)
                        last_saved[0] = time.monotonic()

                system = session['system'] + ('\n已达工具轮数上限。请基于现有证据给出结论与未解决项，不再请求工具。' if final_round else '')
                response, incomplete = client.complete(system, context, [] if final_round else self.tools(session['task']), on_text)
                self.event(identifier, 'assistant', dict(text=response['content'], streaming=False, round=index + 1,
                                                        elapsed_ms=round((time.monotonic() - before) * 1000)), event_id)
                calls = response.get('tool_calls', [])
                if not calls:
                    if not response['content'].strip():
                        raise ValueError('模型返回空内容；请维护者检查模型是否支持文本与工具调用')
                    messages.append(response)
                    self._wire(identifier, messages)
                    if index == 0:
                        self.event(identifier, 'notice', dict(text='本轮模型未新增工具查询；回复仅依据已有对话和范围快照，不代表重新核验了日志或代码。'))
                    warning = '\n\n注意：模型输出达到长度上限，可继续追问。' if incomplete else ''
                    report = '# ' + session['title'] + '\n\n日志包：' + session['task']['name'] + '\n\n'
                    if session['task'].get('project'):
                        report += '代码版本：' + dumps(session['task']['project']) + '\n\n'
                    report += response['content'] + warning
                    (self.directory / identifier / 'report.md').write_text(report, 'utf-8')
                    self._status(identifier, state='idle', status='本轮完成，可继续追问。' + (' 输出达到长度上限。' if incomplete else ''))
                    return
                if final_round:
                    raise ValueError('已到排查轮数上限，模型仍请求工具；证据已保存，可继续追问')
                messages.append(response)
                # Persist complete tool-call/result pairs even if interrupted midway.
                results = [dict(role='tool', tool_call_id=c['id'], content='{"error":"本工具尚未执行或已停止"}') for c in calls]
                messages.extend(results)
                self._wire(identifier, messages)
                for call, answer in zip(calls, results):
                    if stop.is_set():
                        raise Cancelled()
                    name = call['function']['name']
                    args = json.loads(call['function']['arguments'] or '{}')
                    started = time.monotonic()
                    tool_id = self.event(identifier, 'tool', dict(name=name, args=json.loads(client.clean(dumps(args))), state='running'))
                    self._status(identifier, status='正在查询：' + name)
                    try:
                        signature = name + json.dumps(args, sort_keys=True)
                        if signature in seen:
                            raise ValueError('本轮已执行相同查询，请复用已有结果或调整条件，不要重复搜索')
                        seen.add(signature)
                        result = self.bounded(self.execute(session, name, args))
                    except Exception as exc:
                        result = dict(error=client.clean(str(exc)) if isinstance(exc, ValueError) else '查询失败，请检查日志包或代码版本是否仍可用')
                    result = json.loads(client.clean(dumps(result)))
                    answer['content'] = dumps(result)
                    self.event(identifier, 'tool', dict(name=name, args=json.loads(client.clean(dumps(args))), result=result,
                                                       state='failed' if 'error' in result else 'done', elapsed_ms=round((time.monotonic() - started) * 1000)), tool_id)
                    self._wire(identifier, messages)
                    if stop.is_set():
                        raise Cancelled()
        except Cancelled:
            self.event(identifier, 'notice', dict(text='已停止本轮排查；已收到的内容和证据已保存。'))
            self._status(identifier, state='stopped', status='已停止，可以继续追问。')
        except Exception as exc:
            message = client.clean(str(exc)) if isinstance(exc, ValueError) else '排查中断，历史已保存；可重试本轮。'
            self.event(identifier, 'notice', dict(text=message))
            self._status(identifier, state='failed', status=message)
        finally:
            with self.lock:
                self.active.pop(identifier, None)
                if self._get(identifier)['state'] == 'stopping':
                    self._status(identifier, state='stopped', status='已停止，可以继续追问。')
                with self.db() as db:
                    rows = db.execute("SELECT id,body FROM events WHERE session=? AND kind='assistant'", (identifier,)).fetchall()
                for row in rows:
                    body = json.loads(row['body'])
                    if body.get('streaming'):
                        body.update(streaming=False, interrupted=True)
                        if row['id'] == event_id:
                            body['text'] = latest_text[0]
                        self.event(identifier, 'assistant', body, row['id'])

    def stop(self, identifier):
        with self.lock:
            self._get(identifier)
            active = self.active.get(identifier)
            if active:
                self._status(identifier, state='stopping', status='已请求停止，正在结束当前连接或查询…')
                active['stop'].set()
        if active:
            active['client'].cancel()
        return self.get(identifier)

    def delete(self, identifier):
        with self.lock, self.db() as db:
            self._get(identifier)
            if identifier in self.active:
                raise ValueError('请先停止本会话，等待当前操作退出后再删除')
            db.execute('DELETE FROM sessions WHERE id=?', (identifier,))
            db.execute('DELETE FROM events WHERE session=?', (identifier,))
            db.execute('DELETE FROM requests WHERE session=?', (identifier,))
            directory = self.directory / identifier
            # Identifier was resolved against a saved session, never a caller path.
            if directory.is_dir():
                shutil.rmtree(directory)
        return dict(ok=True)

    def report(self, identifier):
        with self.lock:
            self._get(identifier)
            file = self.directory / identifier / 'report.md'
            return dict(text=file.read_text('utf-8') if file.exists() else '')

    def dataset_in_use(self, identifier):
        with self.lock:
            return any(self._get(s)['task']['dataset'] == identifier for s in self.active)

    def close(self):
        with self.lock:
            active = list(self.active.values())
        for item in active:
            item['client'].cancel()
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.projects.close()
