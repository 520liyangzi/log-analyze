"""Exercise task snapshots and the real command/PTY boundary, without a model account."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from analysis_rules import AnalysisRules
from app import make_server
from demo import create_demo
from terminal_bridge import TASK_PROMPT, TerminalManager


class AnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='logscope rules space ')
        cls.server = make_server(Path(cls.temp.name) / 'data', 0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = 'http://127.0.0.1:' + str(cls.server.server_address[1])
        cls.dataset = cls.server.store.submit(create_demo(Path(cls.temp.name) / 'demo.zip'), 'demo.zip')
        cls.server.store.pool.shutdown(wait=True)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temp.cleanup()

    def api(self, path, body=None):
        request = Request(self.url + path,
                          data=json.dumps(body, ensure_ascii=False).encode('utf-8') if body is not None else None,
                          headers={'Content-Type': 'application/json'})
        with urlopen(request, timeout=20) as response:
            return json.load(response)

    def save_rules(self, business):
        current = self.api('/api/analysis/rules')
        return self.api('/api/analysis/rules', dict(workflow=current['workflow'], business=business,
                                                   base_version=current['version'], note='test revision'))

    def output_until(self, identifier, token, cursor=0):
        output = ''
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            result = self.api(f'/api/terminal/output?id={identifier}&cursor={cursor}')
            cursor = result['cursor']
            output += result['output']
            if token in output:
                return output, cursor
            time.sleep(.04)
        self.fail('Expected output not seen: ' + repr(output[-2000:]))

    def test_rule_history_conflict_restore_and_restart(self):
        initial = self.api('/api/analysis/rules')
        updated = self.save_rules('导出超过 10 秒再标慢；HTTP 200 也检查业务码。')
        self.assertEqual(updated['version'], initial['version'] + 1)
        old = self.api('/api/analysis/rules?version=' + str(initial['version']))
        self.assertEqual(old['business'], initial['business'])
        with self.assertRaises(HTTPError) as error:
            self.api('/api/analysis/rules', dict(workflow=initial['workflow'], business='stale overwrite', base_version=initial['version']))
        self.assertEqual(error.exception.code, 400)
        self.assertEqual(self.api('/api/analysis/rules')['business'], updated['business'])
        restarted = AnalysisRules(self.server.store.directory)
        self.assertEqual(restarted.snapshot()['business'], updated['business'])
        restored = self.api('/api/analysis/rules', dict(**updated['defaults'], base_version=updated['version'], note='restore'))
        self.assertEqual(restored['version'], updated['version'] + 1)
        self.assertEqual(self.api('/api/analysis/rules?version=' + str(updated['version']))['business'], updated['business'])
        with self.assertRaises(HTTPError):
            self.api('/api/analysis/rules', dict(workflow='', base_version=restored['version']))
        with self.assertRaises(HTTPError):
            self.api('/api/analysis/rules', dict(workflow='a' * 20001, base_version=restored['version']))

    def test_task_preview_pinned_rules_and_explicit_updates(self):
        initial = self.save_rules('任务创建时的约定')
        body = dict(dataset=self.dataset, question='查一下 /api/model/map', rules_version=initial['version'])
        before = len(self.api('/api/terminal/sessions'))
        preview = self.api('/api/terminal/preview', body)
        self.assertIn('scope', preview['task'])
        self.assertIn('/api/model/map', preview['task']['query_hints']['endpoints'])
        self.assertEqual(len(self.api('/api/terminal/sessions')), before)
        self.save_rules('稍后新保存的约定')
        session = self.api('/api/terminal/start', dict(body, run_command=False))
        identifier = session['id']
        directory = Path(session['cwd'])
        try:
            self.assertEqual((directory / 'task.md').read_text('utf-8'), preview['text'])
            self.assertEqual(json.loads((directory / 'rules.json').read_text('utf-8'))['business'], initial['business'])
            latest = self.save_rules('仅在用户发送后采用的新约定')
            update = self.api('/api/terminal/rules', dict(id=identifier, rules_version=latest['version']))
            self.assertIn(latest['business'], (directory / update['file']).read_text('utf-8'))
            task = self.api('/api/terminal/task?id=' + identifier)
            self.assertEqual(task['text'], preview['text'])
            self.assertEqual(task['task']['rules_version'], initial['version'])
            self.assertEqual(task['rule_updates'][0]['version'], latest['version'])
            self.assertEqual(json.loads((directory / 'rule-updates.json').read_text('utf-8'))[0]['version'], latest['version'])
            with self.assertRaises(HTTPError):
                self.api('/api/terminal/start', dict(body, rules_version=999999))
        finally:
            self.api('/api/terminal/stop', dict(id=identifier))
        with self.assertRaises(HTTPError):
            self.api('/api/terminal/rules', dict(id=identifier))

    def test_auto_launch_fixed_argument_query_report_and_manual_fallback(self):
        # This deterministic stand-in tests transport and query tools, not AI reasoning.
        fake = Path(self.temp.name) / 'fake agent.py'
        fake.write_text("""import json, pathlib, subprocess, sys
p=pathlib.Path.cwd()
p.joinpath('received.json').write_text(json.dumps(sys.argv[1:]), encoding='utf-8')
task=json.loads(p.joinpath('task.json').read_text('utf-8'))
assert sys.stdin.isatty()
if len(sys.argv)>1:
 result=subprocess.run([sys.executable,'tools/logscope.py','search','--q','gzip-history-hit'],capture_output=True,text=True,encoding='utf-8',check=True)
 evidence=json.loads(result.stdout)
 row=evidence['rows'][0]
 result=subprocess.run([sys.executable,'tools/logscope.py','verify',str(row['id'])],capture_output=True,text=True,encoding='utf-8',check=True)
 assert json.loads(result.stdout)['verified']
 p.joinpath('report.md').write_text('规则 v'+str(task['rules_version'])+'，已核验 '+str(evidence['summary']['total'])+' 条匹配中的首条。',encoding='utf-8')
print('FAKE_AGENT_READY',flush=True)
answer=input()
print('FOLLOWUP_'+answer,flush=True)
""", 'utf-8')
        command = subprocess.list2cmdline([sys.executable, str(fake)]) if os.name == 'nt' else shlex.join([sys.executable, str(fake)])
        old_config = self.api('/api/terminal/config')
        malicious = '中文问题 " & echo bad > question-executed.txt & `touch question-executed.txt` $(touch question-executed.txt) %PATH%'
        try:
            self.api('/api/terminal/config', dict(command=command, launch_mode='argument'))
            info = self.api('/api/terminal/start', dict(dataset=self.dataset, question=malicious))
            try:
                _, cursor = self.output_until(info['id'], 'FAKE_AGENT_READY')
                directory = Path(info['cwd'])
                self.assertEqual(json.loads((directory / 'received.json').read_text('utf-8')), [TASK_PROMPT])
                self.assertFalse((directory / 'question-executed.txt').exists())
                self.assertEqual(json.loads((directory / 'task.json').read_text('utf-8'))['question'], malicious)
                self.assertTrue(self.api('/api/terminal/report?id=' + info['id'])['available'])
                self.api('/api/terminal/input', dict(id=info['id'], data='继续查看线程\r'))
                self.output_until(info['id'], 'FOLLOWUP_继续查看线程', cursor)
            finally:
                self.api('/api/terminal/stop', dict(id=info['id']))
            self.api('/api/terminal/config', dict(command=command, launch_mode='manual'))
            manual = self.api('/api/terminal/start', dict(dataset=self.dataset, question='manual'))
            try:
                self.output_until(manual['id'], 'FAKE_AGENT_READY')
                self.assertEqual(json.loads((Path(manual['cwd']) / 'received.json').read_text('utf-8')), [])
                self.assertFalse(self.api('/api/terminal/report?id=' + manual['id'])['available'])
            finally:
                self.api('/api/terminal/stop', dict(id=manual['id']))
        finally:
            self.api('/api/terminal/config', old_config)

    def test_saved_task_history_session_id_and_company_resume_command(self):
        fake = Path(self.temp.name) / 'resume agent.py'
        fake.write_text("""import sys
print('RESUME_ARGS=' + '|'.join(sys.argv[1:]), flush=True)
""", 'utf-8')
        command = subprocess.list2cmdline([sys.executable, str(fake)]) if os.name == 'nt' else shlex.join([sys.executable, str(fake)])
        old_config = self.api('/api/terminal/config')
        session_uuid = 'a0c43b85-1ca0-41e3-8e99-15648dd3ec17'
        info = self.api('/api/terminal/start', dict(dataset=self.dataset, question='需要保存并恢复的排查', run_command=False))
        identifier, directory = info['id'], Path(info['cwd'])
        try:
            self.api('/api/terminal/input', dict(id=identifier, data='echo HISTORY_SAVED Session ID: ' + session_uuid + '\r'))
            output, cursor = self.output_until(identifier, 'HISTORY_SAVED')
            detected = self.api(f'/api/terminal/output?id={identifier}&cursor={cursor}')
            self.assertEqual(detected['ai_session_id'], session_uuid)
            with self.assertRaises(HTTPError):
                self.api('/api/terminal/delete', dict(id=identifier))
            saved = self.api('/api/terminal/session-id', dict(id=identifier, ai_session_id=session_uuid))
            self.assertEqual(saved['ai_session_id'], session_uuid)
            with self.assertRaises(HTTPError):
                self.api('/api/terminal/session-id', dict(id=identifier, ai_session_id='not-a-uuid'))
            self.api('/api/terminal/stop', dict(id=identifier))
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and 'HISTORY_SAVED' not in (directory / 'terminal.log').read_text('utf-8', errors='replace'):
                time.sleep(.03)
            history = self.api('/api/terminal/history?id=' + identifier)
            self.assertIn('HISTORY_SAVED', history['transcript'])
            self.assertEqual(history['info']['ai_session_id'], session_uuid)
            self.assertTrue((directory / 'session.json').exists())
            restarted = TerminalManager(self.server.store, self.url)
            restored = next(item for item in restarted.list() if item['id'] == identifier)
            self.assertFalse(restored['live'])
            self.assertEqual(restored['ai_session_id'], session_uuid)
            self.api('/api/terminal/config', dict(command=command, launch_mode='manual',
                                                   resume_template='{command} --sessions {session_id}'))
            resumed = self.api('/api/terminal/resume', dict(id=identifier, ai_session_id=session_uuid))
            self.assertEqual(resumed['id'], identifier)
            output, _ = self.output_until(identifier, 'RESUME_ARGS=--sessions|' + session_uuid)
            self.assertIn('RESUME_ARGS=--sessions|' + session_uuid, output)
            listed = next(item for item in self.api('/api/terminal/sessions') if item['id'] == identifier)
            self.assertEqual(listed['ai_session_id'], session_uuid)
            self.assertTrue(listed['saved'])
            self.api('/api/terminal/stop', dict(id=identifier))
            deleted = self.api('/api/terminal/delete', dict(id=identifier))
            self.assertTrue(deleted['ok'])
            self.assertFalse(directory.exists())
            self.assertNotIn(identifier, [item['id'] for item in self.api('/api/terminal/sessions')])
            with self.assertRaises(HTTPError):
                self.api('/api/terminal/history?id=' + identifier)
        finally:
            active = self.server.terminals.sessions.get(identifier)
            if active and active.state == 'running':
                self.api('/api/terminal/stop', dict(id=identifier))
            self.api('/api/terminal/config', old_config)

    def test_code_followup_preview_fixed_branch_and_read_only_query(self):
        repository = Path(self.temp.name) / 'business project'
        repository.mkdir()
        subprocess.run(['git', 'init', '-b', 'main'], cwd=repository, check=True, capture_output=True)
        subprocess.run(['git', 'config', 'user.email', 'test@example.invalid'], cwd=repository, check=True)
        subprocess.run(['git', 'config', 'user.name', 'LogScope Test'], cwd=repository, check=True)
        source = repository / 'OrderService.java'
        source.write_text('class OrderService { void failOrder() { throw new RuntimeException("E102"); } }\n', 'utf-8')
        subprocess.run(['git', 'add', 'OrderService.java'], cwd=repository, check=True)
        subprocess.run(['git', 'commit', '-m', 'initial'], cwd=repository, check=True, capture_output=True)
        branches = self.api('/api/project/branches?path=' + quote(str(repository)))
        self.assertEqual(branches['current'], 'main')
        self.assertIn('main', branches['branches'])
        with self.assertRaises(HTTPError):
            self.api('/api/project/branches?path=' + quote(str(Path(self.temp.name))))
        initial_body = dict(dataset=self.dataset, question='根据日志和代码定位 E102',
                            project_path=str(repository), project_branch='main')
        initial_preview = self.api('/api/terminal/preview', initial_body)
        self.assertEqual(initial_preview['task']['project']['branch'], 'main')
        self.assertIn('tools/project.py', initial_preview['text'])
        self.assertIn('先用日志索引收敛', initial_preview['text'])
        initial = self.api('/api/terminal/start', dict(initial_body,
                           project_commit=initial_preview['task']['project']['commit'], run_command=False))
        try:
            initial_directory = Path(initial['cwd'])
            self.assertEqual(json.loads((initial_directory / 'code-task.json').read_text('utf-8'))['commit'],
                             initial_preview['task']['project']['commit'])
            info = subprocess.run([sys.executable, str(initial_directory / 'tools/project.py'), 'info'],
                                  cwd=initial_directory, text=True, encoding='utf-8', capture_output=True, check=True)
            self.assertEqual(json.loads(info.stdout)['branch'], 'main')
        finally:
            self.api('/api/terminal/stop', dict(id=initial['id']))
        session = self.api('/api/terminal/start', dict(dataset=self.dataset, question='日志出现 E102', run_command=False))
        try:
            directory = Path(session['cwd'])
            with self.assertRaises(HTTPError):
                self.api('/api/terminal/code-preview', dict(id=session['id'], project_path=str(repository), branch='main'))
            (directory / 'report.md').write_text('# 日志结论\nE102 出现在 OrderService。', 'utf-8')
            body = dict(id=session['id'], project_path=str(repository), branch='main')
            preview = self.api('/api/terminal/code-preview', body)
            self.assertIn(preview['task']['commit'], preview['text'])
            created = self.api('/api/terminal/code-task', dict(body, commit=preview['task']['commit']))
            self.assertTrue((directory / 'code-task.md').exists())
            self.assertEqual(created['task']['commit'], preview['task']['commit'])
            self.assertEqual((directory / 'code-task.md').read_text('utf-8'), preview['text'])
            result = subprocess.run([sys.executable, str(directory / 'tools/project.py'), 'grep', 'E102'],
                                    cwd=directory, text=True, encoding='utf-8', capture_output=True, check=True)
            self.assertIn('OrderService.java', json.loads(result.stdout)['matches'][0])
            source.write_text(source.read_text('utf-8') + '// moved\n', 'utf-8')
            subprocess.run(['git', 'add', 'OrderService.java'], cwd=repository, check=True)
            subprocess.run(['git', 'commit', '-m', 'move branch'], cwd=repository, check=True, capture_output=True)
            with self.assertRaises(HTTPError):
                self.api('/api/terminal/code-task', dict(body, commit=preview['task']['commit']))
        finally:
            self.api('/api/terminal/stop', dict(id=session['id']))
        with self.assertRaises(HTTPError):
            self.api('/api/terminal/code-preview', dict(id=session['id'], project_path=str(repository), branch='main'))


if __name__ == '__main__':
    unittest.main()
