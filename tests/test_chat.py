import copy
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ai_client import ModelClient, ModelConfig, DEFAULT_CONFIG, Cancelled
from app import Store, make_server
from chat_engine import ChatManager
from chat_projects import ChatProjects, query_project
from demo import create_demo


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True,
                          text=True, encoding='utf-8').stdout.strip()


def response(text='', calls=None):
    message = dict(role='assistant', content=text)
    if calls:
        message['tool_calls'] = [dict(id='call_' + str(i), type='function', function=dict(name=n, arguments=json.dumps(a)))
                                 for i, (n, a) in enumerate(calls)]
    return dict(choices=[dict(index=0, message=message, finish_reason='tool_calls' if calls else 'stop')])


class FakeModel:
    def __init__(self):
        self.requests = []
        self.replies = []
        self.gate = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.requests.append(data)
                reply = owner.replies.pop(0) if owner.replies else response('默认模拟回复')
                if callable(reply):
                    reply = reply(data)
                if reply == 'error':
                    self.send_response(401)
                    self.end_headers()
                    self.wfile.write(b'fake-secret-123 http://private-model.invalid')
                    return
                stream = data.get('stream')
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream' if stream else 'application/json')
                self.end_headers()
                try:
                    if not stream:
                        self.wfile.write(json.dumps(reply).encode())
                        return

                    def emit(item):
                        self.wfile.write(('data: ' + json.dumps(item) + '\n\n').encode())
                        self.wfile.flush()

                    if reply == 'wait':
                        emit(dict(choices=[dict(index=0, delta=dict(content='已经收到的文字'), finish_reason=None)]))
                        owner.gate.wait(8)
                        reply = response('最终输出')
                    if reply == 'broken':
                        emit(dict(choices=[dict(index=0, delta=dict(content='部分结果'), finish_reason=None)]))
                        return
                    if 'content' in reply:  # Anthropic
                        for i, block in enumerate(reply['content']):
                            emit(dict(type='content_block_start', index=i, content_block=block))
                            if block['type'] == 'tool_use':
                                emit(dict(type='content_block_delta', index=i, delta=dict(type='input_json_delta', partial_json=json.dumps(block['input']))))
                            else:
                                emit(dict(type='content_block_delta', index=i, delta=dict(type='text_delta', text=block['text'])))
                        emit(dict(type='message_delta', delta=dict(stop_reason=reply['stop_reason'])))
                    else:
                        choice = reply['choices'][0]
                        message = choice['message']
                        for part in [message['content'][:3], message['content'][3:]]:
                            if part:
                                emit(dict(choices=[dict(index=0, delta=dict(content=part), finish_reason=None)]))
                        for i, call in enumerate(message.get('tool_calls', [])):
                            fn = call['function']
                            emit(dict(choices=[dict(index=0, delta=dict(tool_calls=[dict(index=i, id=call['id'], type='function', function=dict(name=fn['name'], arguments=fn['arguments'][:4]))]), finish_reason=None)]))
                            emit(dict(choices=[dict(index=0, delta=dict(tool_calls=[dict(index=i, function=dict(arguments=fn['arguments'][4:]))]), finish_reason=None)]))
                        emit(dict(choices=[dict(index=0, delta={}, finish_reason=choice['finish_reason'])]))
                    self.wfile.write(b'data: [DONE]\n\n')
                except (OSError, ConnectionError):
                    pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port) + '/v1'

    def close(self):
        self.gate.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class NativeChatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'data')
        self.dataset = self.store.submit(create_demo(self.root / 'demo.zip'), 'demo.zip')
        self.store.pool.shutdown(wait=True)
        self.chat = ChatManager(self.store)
        self.model = FakeModel()
        self.config = dict(DEFAULT_CONFIG, base_url=self.model.url, api_key='fake-secret-123', model='fake-model')
        self.chat.config.path.write_text(json.dumps(self.config), 'utf-8')

    def tearDown(self):
        self.model.gate.set()
        self.chat.close()
        self.chat.pool.shutdown(wait=True)
        self.chat.projects.pool.shutdown(wait=True)
        self.model.close()
        self.temp.cleanup()

    def preview(self, **changes):
        return self.chat.preview(dict(dataset=self.dataset, question='请排查 /api/model/map 的异常', **changes))

    def send(self, draft, request='request-1'):
        return self.chat.send(dict(preview_id=draft['preview_id'], request_id=request))

    def wait(self, identifier):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = self.chat.get(identifier)
            if result['session']['state'] not in ('running', 'stopping') and identifier not in self.chat.active:
                return result
            time.sleep(.02)
        self.fail('Native chat did not finish')

    def repository(self):
        repo = self.root / 'repo'
        repo.mkdir()
        git(repo, 'init', '-b', 'main')
        git(repo, 'config', 'user.email', 'test@example.com')
        git(repo, 'config', 'user.name', 'test')
        (repo / 'Service.java').write_text('class Service {\n  // root failure marker\n}\n', 'utf-8')
        git(repo, 'add', 'Service.java')
        git(repo, 'commit', '-m', 'initial')
        bare = self.root / 'origin.git'
        bare.mkdir()
        git(bare, 'init', '--bare')
        git(repo, 'remote', 'add', 'origin', str(bare))
        git(repo, 'push', '-u', 'origin', 'main')
        return repo

    def sync(self, repo):
        job = self.chat.projects.sync(dict(path=str(repo)))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            job = self.chat.projects.status(job['id'])
            if job['state'] != 'running':
                self.assertEqual(job['state'], 'ready', job)
                return job
            time.sleep(.02)
        self.fail('Sync did not finish')

    def test_streaming_log_code_tool_loop_followup_persistence_and_delete(self):
        repo = self.repository()
        job = self.sync(repo)
        row = self.store.search(dict(dataset=self.dataset, level='ERROR'))['rows'][0]
        self.model.replies = [response('先查日志', [('search_logs', dict(endpoint='/api/model/map', status='5xx'))]),
                              response('', [('log_context', dict(id=row['id'])), ('verify_log', dict(id=row['id']))]),
                              response('对照代码', [('project_search', dict(keyword='failure'))]),
                              response('', [('project_read', dict(path='Service.java', start=1, end=3))]),
                              response('## 定位结果\n日志异常，代码 Service.java:2 是相关证据。')]
        draft = self.preview(use_code=True, sync_id=job['id'], branch='origin/main')
        self.assertEqual(self.model.requests, [])
        self.assertNotIn('fake-secret', draft['text'])
        started = self.send(draft)
        result = self.wait(started['id'])
        self.assertEqual(result['session']['state'], 'idle', result)
        tools = [e['body'] for e in result['events'] if e['kind'] == 'tool']
        self.assertEqual(len(tools), 5)
        self.assertTrue(all(t['state'] == 'done' for t in tools), tools)
        self.assertIn('定位结果', self.chat.report(started['id'])['text'])
        self.assertEqual(self.chat.get(started['id'], result['cursor'])['events'], [])
        self.model.replies = [response('补充说明：当前只是相关线索。')]
        self.send(self.preview(id=started['id']), request='follow-up')
        self.wait(started['id'])
        self.assertTrue(any(m['role'] == 'tool' for m in self.model.requests[-1]['messages']))
        self.chat.close()
        restored = ChatManager(self.store)
        self.assertEqual(len(restored.list()), 1)
        self.assertGreater(len(restored.get(started['id'])['events']), len(result['events']))
        restored.delete(started['id'])
        self.assertEqual(restored.list(), [])
        self.assertFalse((restored.directory / started['id']).exists())
        self.assertEqual(self.store.datasets()[0]['state'], 'ready')
        restored.close()

    def test_duplicate_submission_and_preview_conflict(self):
        self.model.replies = ['wait']
        draft = self.preview()
        first = self.send(draft)
        second = self.send(draft)
        self.assertEqual(first['id'], second['id'])
        with self.assertRaises(ValueError):
            self.chat.delete(first['id'])
        self.model.gate.set()
        self.wait(first['id'])
        a = self.preview(id=first['id'])
        b = self.preview(id=first['id'])
        self.send(a, 'new-turn')
        self.wait(first['id'])
        with self.assertRaises(ValueError):
            self.send(b, 'stale-turn')

    def test_cancellation_saves_partial_and_keeps_other_sessions(self):
        self.model.replies = ['wait']
        session = self.send(self.preview())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if any(e['kind'] == 'assistant' and e['body'].get('text') for e in self.chat.get(session['id'])['events']):
                break
            time.sleep(.02)
        stopped = self.chat.stop(session['id'])
        stopped_at = time.monotonic()
        self.assertIn(stopped['session']['state'], ('stopping', 'stopped'))
        result = self.wait(session['id'])
        self.assertEqual(result['session']['state'], 'stopped')
        self.assertTrue(any('已经收到' in e['body'].get('text', '') for e in result['events']))
        self.assertFalse(any(e['body'].get('streaming') for e in result['events']))
        self.assertNotIn(session['id'], self.chat.active)
        self.assertLess(time.monotonic() - stopped_at, 3)

    def test_model_errors_never_expose_key_or_url_and_retry_works(self):
        self.model.replies = ['error']
        session = self.send(self.preview())
        result = self.wait(session['id'])
        self.assertEqual(result['session']['state'], 'failed')
        public = json.dumps(result)
        self.assertNotIn(self.config['api_key'], public)
        self.assertNotIn(self.config['base_url'], public)
        self.assertNotIn(self.config['api_key'], json.dumps(self.chat.config.public()))
        self.model.replies = [response('重试成功')]
        self.send(self.preview(id=session['id']), 'retry')
        self.assertEqual(self.wait(session['id'])['session']['state'], 'idle')

    def test_broken_stream_is_not_reported_as_success(self):
        self.model.replies = ['broken']
        session = self.send(self.preview())
        self.assertEqual(self.wait(session['id'])['session']['state'], 'failed')
        self.assertEqual(self.chat.report(session['id'])['text'], '')

    def test_tool_validation_pinned_dataset_and_repeat_guard(self):
        session = dict(task=dict(dataset=self.dataset, project=None))
        for name, args in [('shell', dict(command='whoami')), ('search_logs', dict(dataset='other')),
                           ('search_logs', dict(q={})), ('project_read', dict(path='../ai-config.json'))]:
            with self.assertRaises(ValueError):
                self.chat.execute(session, name, args)
        self.model.replies = [response('', [('search_logs', dict(q='failure'))]),
                              response('', [('search_logs', dict(q='failure'))]), response('没有重复扫描。')]
        started = self.send(self.preview())
        result = self.wait(started['id'])
        events = [e for e in result['events'] if e['kind'] == 'tool']
        self.assertEqual(events[1]['body']['state'], 'failed')
        self.assertIn('相同查询', events[1]['body']['result']['error'])

    def test_git_fetch_preserves_dirty_files_and_pins_old_revision(self):
        repo = self.repository()
        job = self.sync(repo)
        snapshot = self.chat.projects.snapshot(dict(sync_id=job['id'], branch='origin/main'))
        (repo / 'Service.java').write_text('class Service { /* newer committed */ }\n', 'utf-8')
        git(repo, 'add', 'Service.java')
        git(repo, 'commit', '-m', 'newer')
        git(repo, 'push', 'origin', 'main')
        (repo / 'Service.java').write_text('local uncommitted work', 'utf-8')
        self.sync(repo)
        old = query_project(snapshot, 'project_read', dict(path='Service.java'))
        self.assertIn('failure', old['content'])
        self.assertEqual((repo / 'Service.java').read_text('utf-8'), 'local uncommitted work')
        self.assertEqual(git(repo, 'branch', '--show-current'), 'main')
        with self.assertRaises(ValueError):
            query_project(snapshot, 'project_read', dict(path='data/ai-config.json'))

    def test_context_budget_keeps_tool_call_pairs(self):
        messages = [dict(role='user', content='a' * 2000), response('old')['choices'][0]['message'],
                    dict(role='user', content='new')]
        compact, shortened = self.chat.budget(messages, 200)
        self.assertTrue(shortened)
        self.assertEqual(compact, [dict(role='user', content='new')])

    def test_anthropic_stream_and_json_adapter(self):
        self.config['provider'] = 'anthropic'
        self.chat.config.path.write_text(json.dumps(self.config), 'utf-8')
        self.model.replies = [dict(content=[dict(type='tool_use', id='use_1', name='search_logs', input=dict(q='timeout'))], stop_reason='tool_use'),
                              dict(content=[dict(type='text', text='已检查超时日志')], stop_reason='end_turn')]
        session = self.send(self.preview())
        self.assertEqual(self.wait(session['id'])['session']['state'], 'idle')
        followup = self.model.requests[-1]
        self.assertIn('system', followup)
        self.assertTrue(any(b['type'] == 'tool_result' for m in followup['messages'] for b in m['content']))
        self.config.update(stream=False, provider='openai')
        self.chat.config.path.write_text(json.dumps(self.config), 'utf-8')
        self.model.replies = [response('非流式也可用')]
        new = self.send(self.preview(), 'nonstream')
        self.assertEqual(self.wait(new['id'])['session']['state'], 'idle')


class ChatHTTPTests(unittest.TestCase):
    def test_configuration_is_file_only_and_shared_host_is_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            server = make_server(Path(temporary) / 'data', 0, host='0.0.0.0', allowed_hosts=['group.test'])
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = 'http://127.0.0.1:' + str(server.server_port)
            try:
                config = dict(DEFAULT_CONFIG, api_key='private-key', base_url='http://private.internal/v1', model='example')
                server.chats.config.path.write_text(json.dumps(config), 'utf-8')
                with urlopen(base + '/api/chat/capability') as r:
                    data = r.read().decode()
                self.assertNotIn('private-key', data)
                self.assertNotIn('private.internal', data)
                self.assertIn('FMEMateService', data)
                for path in ['/data/ai-config.json', '/ai-config.json', '/api/chat/config']:
                    with self.assertRaises(HTTPError) as exc:
                        urlopen(base + path)
                    self.assertEqual(exc.exception.code, 404)
                with urlopen(Request(base + '/', headers={'Host': 'group.test:' + str(server.server_port)})) as r:
                    self.assertEqual(r.status, 200)
                with self.assertRaises(HTTPError):
                    urlopen(Request(base + '/', headers={'Host': 'untrusted.test'}))
                with urlopen(base + '/chat.js') as r:
                    self.assertEqual(r.status, 200)
            finally:
                server.shutdown()
                server.server_close()
                server.store.pool.shutdown(wait=True)
                thread.join()


if __name__ == '__main__':
    unittest.main()
