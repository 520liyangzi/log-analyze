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
from urllib.request import Request, urlopen

from analysis_rules import AnalysisRules
from app import make_server
from demo import create_demo
from terminal_bridge import TASK_PROMPT


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


if __name__ == '__main__':
    unittest.main()
