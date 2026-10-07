"""Versioned rules, native task snapshots and independent read-only Git tools."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ai_client import DEFAULT_CONFIG
from analysis_rules import AnalysisRules
from app import make_server
from demo import create_demo
from project_access import inspect_repository, select_revision
from test_chat import FakeModel, response

BASE = Path(__file__).resolve().parents[1]


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
        cls.model = FakeModel()
        cls.server.chats.config.path.write_text(json.dumps(dict(DEFAULT_CONFIG, base_url=cls.model.url,
                                                                  api_key='fake-secret-123', model='fake-model')), 'utf-8')

    @classmethod
    def tearDownClass(cls):
        cls.model.gate.set()
        cls.server.shutdown()
        cls.server.server_close()
        cls.server.chats.close(wait=True)
        cls.server.chats.projects.pool.shutdown(wait=True)
        cls.thread.join()
        cls.model.close()
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

    def test_rule_history_conflict_restore_and_restart(self):
        initial = self.api('/api/analysis/rules')
        updated = self.save_rules('导出超过 10 秒再标慢；HTTP 200 也检查业务码。')
        self.assertEqual(updated['version'], initial['version'] + 1)
        old = self.api('/api/analysis/rules?version=' + str(initial['version']))
        self.assertEqual(old['business'], initial['business'])
        with self.assertRaises(HTTPError) as error:
            self.api('/api/analysis/rules', dict(workflow=initial['workflow'], business='stale overwrite', base_version=initial['version']))
        with error.exception as failure:
            self.assertEqual(failure.code, 400)
        self.assertEqual(self.api('/api/analysis/rules')['business'], updated['business'])
        restarted = AnalysisRules(self.server.store.directory)
        self.assertEqual(restarted.snapshot()['business'], updated['business'])
        restored = self.api('/api/analysis/rules', dict(**updated['defaults'], base_version=updated['version'], note='restore'))
        self.assertEqual(restored['version'], updated['version'] + 1)
        self.assertEqual(self.api('/api/analysis/rules?version=' + str(updated['version']))['business'], updated['business'])
        for invalid in ('', 'a' * 20001):
            with self.assertRaises(HTTPError) as error:
                self.api('/api/analysis/rules', dict(workflow=invalid, base_version=restored['version']))
            error.exception.close()

    def test_native_preview_pins_rules_without_creating_session(self):
        initial = self.save_rules('任务创建时的约定')
        before = len(self.api('/api/chat/sessions'))
        preview = self.api('/api/chat/preview', dict(dataset=self.dataset, question='查一下 /api/model/map'))
        self.assertIn('scope', preview['task'])
        self.assertEqual(preview['task']['rules_version'], initial['version'])
        self.assertIn(initial['business'], preview['text'])
        self.assertEqual(len(self.api('/api/chat/sessions')), before)
        latest = self.save_rules('之后保存的新约定')
        self.model.replies = [response('按照已预览规则给出模拟报告。')]
        session = self.api('/api/chat/send', dict(preview_id=preview['preview_id'], request_id='pinned-rule-test'))
        deadline = time.monotonic() + 10
        while session['id'] in self.server.chats.active and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertNotIn(session['id'], self.server.chats.active)
        task_file = self.server.chats.directory / session['id'] / 'task.md'
        self.assertEqual(task_file.read_text('utf-8'), preview['text'])
        followup = self.api('/api/chat/preview', dict(id=session['id'], question='继续查看这个请求'))
        self.assertEqual(followup['task']['rules_version'], initial['version'])
        self.assertIn(initial['business'], followup['text'])
        self.assertNotIn(latest['business'], followup['text'])
        fresh = self.api('/api/chat/preview', dict(dataset=self.dataset, question='新建排查'))
        self.assertEqual(fresh['task']['rules_version'], latest['version'])
        self.assertIn(latest['business'], fresh['text'])

    def test_project_discovery_and_cli_use_fixed_commit_without_checkout(self):
        repository = Path(self.temp.name) / 'business project'
        repository.mkdir()

        def git(*args):
            return subprocess.run(['git', *args], cwd=repository, check=True,
                                  capture_output=True, text=True, encoding='utf-8').stdout.strip()

        git('init', '-b', 'main')
        git('config', 'user.email', 'test@example.invalid')
        git('config', 'user.name', 'LogScope Test')
        source = repository / 'OrderService.java'
        original = 'class OrderService { void failOrder() { throw new RuntimeException("E102"); } }\n'
        source.write_text(original, 'utf-8')
        git('add', '.')
        git('commit', '-m', 'initial')
        branches = inspect_repository(str(repository))
        self.assertEqual(branches['current'], 'main')
        self.assertIn('main', branches['branches'])
        with self.assertRaises(ValueError):
            inspect_repository(str(Path(self.temp.name)))
        selected = select_revision(str(repository), 'main')
        with self.assertRaises(ValueError):
            select_revision(str(repository), 'missing-branch')
        task_dir = Path(self.temp.name) / 'code cli task'
        task_dir.mkdir()
        (task_dir / 'code-task.json').write_text(json.dumps(dict(project_root=selected['root'],
                                                               branch=selected['branch'], commit=selected['commit'])), 'utf-8')

        def cli(*args, success=True):
            result = subprocess.run([sys.executable, str(BASE / 'skills/logscope/scripts/project.py'), *args],
                                    cwd=task_dir, text=True, encoding='utf-8', capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0 if success else 1, result.stderr)
            return json.loads(result.stdout if success else result.stderr)

        self.assertEqual(cli('info')['commit'], selected['commit'])
        self.assertEqual(cli('tree')['files'], ['OrderService.java'])
        self.assertIn('OrderService.java', cli('grep', 'E102')['matches'][0])
        source.write_text(original.replace('E102', 'E103'), 'utf-8')
        git('add', '.')
        git('commit', '-m', 'move branch')
        source.write_text('// local uncommitted change\n', 'utf-8')
        self.assertNotEqual(select_revision(str(repository), 'main')['commit'], selected['commit'])
        self.assertIn('E102', cli('show', 'OrderService.java')['content'])
        self.assertTrue(cli('log')['commits'][0].startswith(selected['commit']))
        self.assertEqual(source.read_text('utf-8'), '// local uncommitted change\n')
        self.assertEqual(git('branch', '--show-current'), 'main')
        self.assertIn('error', cli('show', '../outside.java', success=False))


if __name__ == '__main__':
    unittest.main()
