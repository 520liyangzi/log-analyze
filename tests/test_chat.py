import copy
import json
import os
import sqlite3
import shutil
import ssl
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
    def __init__(self, tls_context=None):
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
                                emit(dict(choices=[dict(index=0, delta=dict(content=part, tool_calls=None), finish_reason=None)]))
                        for i, call in enumerate(message.get('tool_calls', [])):
                            fn = call['function']
                            emit(dict(choices=[dict(index=0, delta=dict(tool_calls=[dict(index=i, id=call['id'], type='function', function=dict(name=fn['name'], arguments=fn['arguments'][:4]))]), finish_reason=None)]))
                            emit(dict(choices=[dict(index=0, delta=dict(tool_calls=[dict(index=i, function=dict(arguments=fn['arguments'][4:]))]), finish_reason=None)]))
                        emit(dict(choices=[dict(index=0, delta=None, finish_reason=choice['finish_reason'])]))
                    self.wfile.write(b'data: [DONE]\n\n')
                except (OSError, ConnectionError):
                    pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        if tls_context:
            self.server.socket = tls_context.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = ('https' if tls_context else 'http') + '://127.0.0.1:' + str(self.server.server_port) + '/v1'

    def close(self):
        self.gate.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class NativeChatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.git_environment = mock.patch.dict(os.environ, {
            'GIT_CONFIG_COUNT': '1',
            'GIT_CONFIG_KEY_0': 'url.' + self.root.as_uri() + '/.insteadOf',
            'GIT_CONFIG_VALUE_0': 'https://git.fixture.invalid/',
            'GIT_ALLOW_PROTOCOL': 'file',
        })
        self.git_environment.start()
        self.addCleanup(self.git_environment.stop)
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
        body = dict(dataset=self.dataset, question='请排查 /api/model/map 的异常')
        body.update(changes)
        return self.chat.preview(body)

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
        git(bare, 'symbolic-ref', 'HEAD', 'refs/heads/main')
        git(repo, 'remote', 'add', 'origin', str(bare))
        git(repo, 'push', '-u', 'origin', 'main')
        return repo

    def sync(self, repo):
        job = self.chat.projects.sync(dict(remote_url='https://git.fixture.invalid/origin.git'))
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

    def test_three_turn_followup_sends_prior_questions_answers_and_tool_evidence(self):
        self.model.replies = [response('先查日志', [('search_logs', dict(q='timeout'))]),
                              response('第一轮结论：发现超时线索。'),
                              response('第二轮结论：继续检查关联条件。'),
                              response('第三轮结论：需要额外核验。')]
        questions = ['第一问：定位接口超时', '第二问：请说明候选关系', '第三问：如何验证原因']
        session = self.send(self.preview(question=questions[0]), 'three-turn-first')
        self.assertEqual(self.wait(session['id'])['session']['state'], 'idle')
        first_wire = copy.deepcopy(self.chat._get(session['id'])['wire'])
        tool_answers = [m for m in first_wire if m['role'] == 'tool']
        self.assertEqual(len(tool_answers), 1)
        self.assertIn('rows', json.loads(tool_answers[0]['content']))

        self.send(self.preview(id=session['id'], question=questions[1]), 'three-turn-second')
        self.assertEqual(self.wait(session['id'])['session']['state'], 'idle')
        second_request = self.model.requests[-1]['messages']
        self.assertEqual(second_request[1:-1], first_wire)
        self.assertEqual(second_request[-1], dict(role='user', content=questions[1]))
        second_wire = copy.deepcopy(self.chat._get(session['id'])['wire'])

        self.send(self.preview(id=session['id'], question=questions[2]), 'three-turn-third')
        self.assertEqual(self.wait(session['id'])['session']['state'], 'idle')
        third_request = self.model.requests[-1]['messages']
        self.assertEqual(third_request[1:-1], second_wire)
        self.assertEqual([m['content'] for m in third_request if m['role'] == 'user'], questions)
        self.assertEqual([m for m in third_request if m['role'] == 'tool'], tool_answers)
        for answer in ('第一轮结论：发现超时线索。', '第二轮结论：继续检查关联条件。'):
            self.assertTrue(any(m['role'] == 'assistant' and m.get('content') == answer for m in third_request))

        # A new investigation has its own wire history even on the same dataset.
        self.model.replies = [response('独立会话的回答')]
        separate = self.send(self.preview(question='一个独立的新问题'), 'three-turn-separate')
        self.assertEqual(self.wait(separate['id'])['session']['state'], 'idle')
        self.assertNotEqual(separate['id'], session['id'])
        self.assertEqual(self.model.requests[-1]['messages'][1:],
                         [dict(role='user', content='一个独立的新问题')])

    def test_broken_reply_is_recovered_once_before_followup_without_tool_fragments(self):
        self.model.replies = ['broken']
        session = self.send(self.preview(question='首次排查问题'), 'recover-broken-first')
        self.assertEqual(self.wait(session['id'])['session']['state'], 'failed')
        self.assertEqual(self.chat.report(session['id'])['text'], '')
        self.assertEqual([m['role'] for m in self.chat._get(session['id'])['wire']], ['user'])

        self.model.replies = [response('已接着分析')]
        self.send(self.preview(id=session['id'], question='继续刚才的分析'), 'recover-broken-followup')
        self.assertEqual(self.wait(session['id'])['session']['state'], 'idle')
        messages = self.model.requests[-1]['messages'][1:]
        self.assertEqual([m['role'] for m in messages], ['user', 'assistant', 'user'])
        recovered = messages[1]
        self.assertIn('部分结果', recovered['content'])
        self.assertRegex(recovered['content'], '中断|未完成')
        self.assertIn('结论', recovered['content'])
        self.assertNotIn('tool_calls', recovered)
        self.assertEqual(messages[-1]['content'], '继续刚才的分析')

        # A later failure with no received text must not resurrect an older
        # interruption after the newer user turns, or insert its text twice.
        self.model.replies = ['error']
        self.send(self.preview(id=session['id'], question='检查新的线索'), 'recover-broken-empty')
        self.assertEqual(self.wait(session['id'])['session']['state'], 'failed')
        self.model.replies = [response('重试后完成')]
        self.send(self.preview(id=session['id'], question='重试最新问题'), 'recover-broken-retry')
        self.assertEqual(self.wait(session['id'])['session']['state'], 'idle')
        messages = self.model.requests[-1]['messages'][1:]
        self.assertEqual(sum('部分结果' in m.get('content', '') for m in messages), 1)
        self.assertEqual(messages[1], recovered)
        self.assertEqual(messages[-2:], [dict(role='user', content='检查新的线索'),
                                        dict(role='user', content='重试最新问题')])

    def test_stopped_reply_is_available_as_marked_context_on_next_turn(self):
        self.model.replies = ['wait']
        session = self.send(self.preview(question='停止之前的问题'), 'recover-stop-first')
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if any('已经收到的文字' in e['body'].get('text', '')
                   for e in self.chat.get(session['id'])['events']):
                break
            time.sleep(.02)
        else:
            self.fail('Expected partial response before requesting cancellation')
        self.chat.stop(session['id'])
        self.assertEqual(self.wait(session['id'])['session']['state'], 'stopped')
        self.model.gate.set()
        self.assertEqual(self.chat.report(session['id'])['text'], '')

        self.model.replies = [response('停止后继续的回答')]
        self.send(self.preview(id=session['id'], question='请从中断处继续'), 'recover-stop-followup')
        self.assertEqual(self.wait(session['id'])['session']['state'], 'idle')
        messages = self.model.requests[-1]['messages'][1:]
        self.assertEqual([m['role'] for m in messages], ['user', 'assistant', 'user'])
        self.assertIn('已经收到的文字', messages[1]['content'])
        self.assertRegex(messages[1]['content'], '中断|未完成')
        self.assertIn('结论', messages[1]['content'])
        self.assertNotIn('tool_calls', messages[1])
        self.assertEqual(messages[-1]['content'], '请从中断处继续')

    def test_restart_recovers_saved_streaming_text_once_without_changing_report(self):
        self.model.replies = ['broken']
        session = self.send(self.preview(question='服务重启之前的问题'), 'recover-restart-first')
        result = self.wait(session['id'])
        event = next(e for e in result['events'] if e['kind'] == 'assistant')
        # Recreate the durable state of a process that died during streaming:
        # a saved user wire plus a streaming assistant event, without a reply.
        body = dict(event['body'], text='重启之前已经收到的线索', streaming=True)
        body.pop('interrupted', None)
        self.chat.event(session['id'], 'assistant', body, event['id'])
        self.chat._status(session['id'], state='running', status='正在接收回复')
        self.chat.close()
        self.chat.pool.shutdown(wait=True)
        self.chat.projects.pool.shutdown(wait=True)
        self.chat = ChatManager(self.store)
        restored = self.chat.get(session['id'])
        self.assertEqual(restored['session']['state'], 'interrupted')
        restored_event = next(e for e in restored['events'] if e['id'] == event['id'])
        self.assertTrue(restored_event['body']['interrupted'])
        self.assertFalse(restored_event['body']['streaming'])
        self.assertEqual(self.chat.report(session['id'])['text'], '')

        self.model.replies = [response('服务恢复后继续分析')]
        self.send(self.preview(id=session['id'], question='接着重启前的内容'), 'recover-restart-followup')
        self.assertEqual(self.wait(session['id'])['session']['state'], 'idle')
        messages = self.model.requests[-1]['messages'][1:]
        self.assertEqual([m['role'] for m in messages], ['user', 'assistant', 'user'])
        self.assertIn('重启之前已经收到的线索', messages[1]['content'])
        self.assertRegex(messages[1]['content'], '中断|未完成')
        self.assertIn('结论', messages[1]['content'])
        self.assertNotIn('tool_calls', messages[1])

        # Recovery bookkeeping must survive another process restart.
        self.chat.close()
        self.chat.pool.shutdown(wait=True)
        self.chat.projects.pool.shutdown(wait=True)
        self.chat = ChatManager(self.store)
        self.model.replies = [response('继续已有历史')]
        self.send(self.preview(id=session['id'], question='继续下一步'), 'recover-restart-again')
        self.assertEqual(self.wait(session['id'])['session']['state'], 'idle')
        messages = self.model.requests[-1]['messages'][1:]
        self.assertEqual(sum('重启之前已经收到的线索' in m.get('content', '') for m in messages), 1)
        self.assertTrue(any(m['role'] == 'assistant' and m.get('content') == '服务恢复后继续分析'
                            for m in messages))

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

    def test_invalid_tool_arguments_preserve_partial_reply_without_running_tools(self):
        reply = response('已收到线索，正在组织查询。', [('search_logs', {})])
        reply['choices'][0]['message']['tool_calls'][0]['function']['arguments'] = '{private-invalid-marker'
        self.model.replies = [reply]
        session = self.send(self.preview())
        result = self.wait(session['id'])
        self.assertEqual(result['session']['state'], 'failed')
        self.assertIn('[tool_arguments]', result['session']['status'])
        self.assertFalse(any(e['kind'] == 'tool' for e in result['events']))
        partials = [e['body'] for e in result['events'] if e['kind'] == 'assistant']
        self.assertTrue(any('已收到线索' in b.get('text', '') and b.get('interrupted') for b in partials))
        self.assertFalse(any(b.get('streaming') for b in partials))
        self.assertNotIn('private-invalid-marker', json.dumps(result))
        self.assertEqual(self.chat.report(session['id'])['text'], '')
        self.model.replies = [response('重试成功')]
        self.send(self.preview(id=session['id']), 'retry-format')
        self.assertEqual(self.wait(session['id'])['session']['state'], 'idle')
        messages = self.model.requests[-1]['messages'][1:]
        self.assertEqual([m['role'] for m in messages], ['user', 'assistant', 'user'])
        recovered = messages[1]
        self.assertIn('已收到线索', recovered['content'])
        self.assertRegex(recovered['content'], '中断|未完成')
        self.assertIn('结论', recovered['content'])
        self.assertNotIn('tool_calls', recovered)
        self.assertNotIn('private-invalid-marker', json.dumps(messages))

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

    def test_investigation_continues_past_legacy_round_limit_until_final_answer(self):
        # Existing configuration files may still contain this former hard cap.
        self.config['max_tool_rounds'] = 1
        self.chat.config.path.write_text(json.dumps(self.config), 'utf-8')
        rounds = 12
        self.model.replies = [response('正在核查第 ' + str(i + 1) + ' 个线索',
                                       [('search_logs', dict(q='unique-continuation-probe-' + str(i), size=1))])
                              for i in range(rounds)]
        self.model.replies.append(response('已完成全部线索核查，给出最终结论。'))
        session = self.send(self.preview(), 'continue-past-round-limit')
        result = self.wait(session['id'])
        self.assertEqual(result['session']['state'], 'idle', result['session']['status'])
        self.assertEqual(len(self.model.requests), rounds + 1)
        self.assertTrue(all(request.get('tools') for request in self.model.requests),
                        'Every model round must retain its available query tools')
        tools = [e['body'] for e in result['events'] if e['kind'] == 'tool']
        self.assertEqual(len(tools), rounds)
        self.assertTrue(all(t['state'] == 'done' and t['name'] == 'search_logs' for t in tools))
        self.assertEqual(len({t['args']['q'] for t in tools}), rounds)
        self.assertIn('已完成全部线索核查', self.chat.report(session['id'])['text'])
        self.assertFalse(any('已到排查轮数上限' in e['body'].get('text', '') for e in result['events']))

    def test_cancellation_after_more_than_eight_tool_rounds_still_exits(self):
        self.config['max_tool_rounds'] = 1
        self.chat.config.path.write_text(json.dumps(self.config), 'utf-8')
        rounds = 10
        self.model.replies = [response('', [('search_logs', dict(q='stop-after-many-rounds-' + str(i), size=1))])
                              for i in range(rounds)] + ['wait']
        session = self.send(self.preview(), 'stop-past-round-limit')
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = self.chat.get(session['id'])
            if any('已经收到的文字' in e['body'].get('text', '') for e in result['events']):
                break
            if result['session']['state'] not in ('running', 'stopping'):
                self.fail('Investigation ended before reaching the cancellable round: ' + result['session']['status'])
            time.sleep(.02)
        else:
            self.fail('Expected a live response after more than eight query rounds')
        self.assertEqual(len(self.model.requests), rounds + 1)
        self.assertTrue(all(request.get('tools') for request in self.model.requests))
        started = time.monotonic()
        self.chat.stop(session['id'])
        result = self.wait(session['id'])
        self.assertEqual(result['session']['state'], 'stopped')
        self.assertLess(time.monotonic() - started, 3)
        self.assertNotIn(session['id'], self.chat.active)
        self.assertEqual(len([e for e in result['events'] if e['kind'] == 'tool']), rounds)
        self.assertEqual(self.chat.report(session['id'])['text'], '')
        self.assertFalse(any(e['body'].get('streaming') for e in result['events']))

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

    def test_context_budget_shortens_old_tool_before_discarding_question_and_answer(self):
        call = response('先检索原始线索', [('search_logs', dict(q='timeout'))])['choices'][0]['message']
        messages = [dict(role='user', content='原问题：为什么接口超时？'), call,
                    dict(role='tool', tool_call_id=call['tool_calls'][0]['id'],
                         content=json.dumps(dict(rows=[dict(raw='超时日志 ' * 3000)]), ensure_ascii=False)),
                    dict(role='assistant', content='上一轮结论：连接池等待超时，原因尚待核验。'),
                    dict(role='user', content='继续分析连接池问题')]
        original = copy.deepcopy(messages)
        compact, shortened = self.chat.budget(messages, 3000)
        self.assertTrue(shortened)
        self.assertEqual([m['role'] for m in compact], ['user', 'assistant', 'tool', 'assistant', 'user'])
        self.assertEqual(compact[0], original[0])
        self.assertEqual(compact[1], original[1])
        self.assertEqual(compact[3:], original[3:])
        self.assertEqual(compact[2]['tool_call_id'], call['tool_calls'][0]['id'])
        self.assertIn('省略', json.loads(compact[2]['content'])['note'])
        self.assertLess(len(compact[2]['content']), len(original[2]['content']))
        self.assertEqual(messages, original, 'The persisted full history must not be modified by budgeting')

        # A large result in the current turn must not erase the short previous
        # question and conclusion before any evidence compaction is attempted.
        current = [original[0], original[3], original[4], original[1], original[2]]
        compact, shortened = self.chat.budget(current, 3000)
        self.assertTrue(shortened)
        self.assertEqual(compact[:4], current[:4])
        self.assertEqual(compact[4]['tool_call_id'], call['tool_calls'][0]['id'])
        self.assertIn('省略', json.loads(compact[4]['content'])['note'])
        self.assertEqual(current[4], original[2])

    def test_database_context_releases_connection_without_garbage_collection(self):
        with self.chat.db() as db:
            db.execute('SELECT 1')
        with self.assertRaises(sqlite3.ProgrammingError):
            db.execute('SELECT 1')

    @unittest.skipUnless(shutil.which('openssl'), 'TLS fixture needs openssl')
    def test_https_model_with_verified_local_certificate(self):
        certificate, key = self.root / 'test-cert.pem', self.root / 'test-key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-keyout', str(key), '-out', str(certificate), '-subj', '/CN=localhost',
                        '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1'],
                       check=True, capture_output=True)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate, key)
        self.model.close()
        self.model = FakeModel(context)
        self.config['base_url'] = self.model.url
        self.chat.config.path.write_text(json.dumps(self.config), 'utf-8')
        self.model.replies = [response('HTTPS 已验证')]
        verified_client = ssl.create_default_context(cafile=str(certificate))
        with mock.patch('ssl._create_default_https_context', return_value=verified_client):
            session = self.send(self.preview())
            result = self.wait(session['id'])
        self.assertEqual(result['session']['state'], 'idle', result)
        self.assertIn('HTTPS 已验证', self.chat.report(session['id'])['text'])
        # Also cancel a silent, certificate-verified TLS response mid-stream.
        self.model.replies = ['wait']
        with mock.patch('ssl._create_default_https_context', return_value=verified_client):
            pending = self.send(self.preview(), 'https-cancel')
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if any(e['kind'] == 'assistant' and e['body'].get('text') for e in self.chat.get(pending['id'])['events']):
                    break
                time.sleep(.02)
            stopped_at = time.monotonic()
            self.chat.stop(pending['id'])
            stopped = self.wait(pending['id'])
        self.assertLess(time.monotonic() - stopped_at, 3)
        self.assertEqual(stopped['session']['state'], 'stopped')
        self.assertTrue(any('已经收到' in e['body'].get('text', '') for e in stopped['events']))

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
                with urlopen(base + '/api/chat/projects') as r:
                    projects = json.load(r)
                self.assertEqual(projects, dict(storage_path=str((Path(temporary) / 'data' / 'projects').resolve()),
                                                repositories=[]))
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
