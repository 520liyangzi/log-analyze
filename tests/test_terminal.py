import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.request import Request, urlopen

from app import make_server
from demo import create_demo, TRACE
from install_skill import install

BASE = Path(__file__).resolve().parents[1]


class TerminalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='logscope test space ')
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
        request = Request(self.url + path, data=json.dumps(body).encode() if body is not None else None,
                          headers={'Content-Type': 'application/json'})
        with urlopen(request, timeout=20) as response:
            return json.load(response)

    def cli(self, *args, cwd=None):
        result = subprocess.run([sys.executable, str(BASE / 'skills/logscope/scripts/logscope.py'),
                                 '--url', self.url, '--dataset', self.dataset, *args],
                                capture_output=True, text=True, encoding='utf-8', cwd=cwd, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def wait_output(self, identifier, wanted, timeout=12, cursor=0):
        text = ''
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.api('/api/terminal/output?id=' + identifier + '&cursor=' + str(cursor))
            cursor = result['cursor']
            text += result['output']
            if wanted in text:
                return text, cursor
            time.sleep(.04)
        self.fail('Terminal did not produce expected output ' + repr(wanted) + ': ' + repr(text[-2000:]))

    def test_raw_archive_verification_and_tamper_detection(self):
        result = self.cli('search', '--q', 'gzip-history-hit')
        identifier = result['rows'][0]['id']
        verified = self.cli('verify', str(identifier))
        self.assertTrue(verified['verified'])
        self.assertTrue(verified['available'])
        with self.server.store.connect() as db:
            old = db.execute('SELECT raw FROM logs WHERE id=?', (identifier,)).fetchone()['raw']
            db.execute('UPDATE logs SET raw=? WHERE id=?', ('tampered index text', identifier))
        try:
            self.assertFalse(self.cli('verify', str(identifier))['verified'])
        finally:
            with self.server.store.connect() as db:
                db.execute('UPDATE logs SET raw=? WHERE id=?', (old, identifier))

    def test_cli_full_flow_pagination_trace_and_scan(self):
        access = self.cli('search', '--q', '/api/model/map', '--access-only', '--size', '20')
        self.assertEqual(access['summary']['total'], 160)
        self.assertEqual(access['next_page'], 2)
        scan = self.cli('search', '--q', '/api/model/map', '--access-only', '--scan')
        self.assertEqual(scan['summary']['total'], access['summary']['total'])
        failed = self.cli('search', '--q', '/api/model/map', '--access-only', '--status', '5xx')['rows'][0]
        nearby = self.cli('correlate', str(failed['id']), '--kind', 'root', '--same-thread')
        error = next(r for r in nearby['rows'] if r['level'] == 'ERROR')
        self.assertEqual(nearby['association'], 'candidate')
        self.assertTrue(self.cli('verify', str(error['id']))['verified'])
        self.assertEqual(self.cli('trace', TRACE)['summary']['total'], 6)
        self.assertIn('Caused by:', self.cli('record', str(error['id']))['raw'])
        target = Path(self.temp.name) / 'all results.ndjson'
        self.cli('export', '--q', '/api/model/map', '--access-only', '--output', str(target))
        self.assertEqual(len(target.read_text('utf-8').splitlines()), 160)

    def test_legacy_archive_has_explicit_unavailable_state(self):
        row = self.cli('search', '--q', 'gzip-history-hit')['rows'][0]
        with self.server.store.connect() as db:
            old = db.execute('SELECT archive_chain FROM files WHERE id=?', (row['file_id'],)).fetchone()[0]
            db.execute('UPDATE files SET archive_chain=NULL WHERE id=?', (row['file_id'],))
        try:
            result = self.cli('verify', str(row['id']))
            self.assertFalse(result['available'])
            self.assertFalse(result['verified'])
        finally:
            with self.server.store.connect() as db:
                db.execute('UPDATE files SET archive_chain=? WHERE id=?', (old, row['file_id']))

    def test_skill_install_is_self_contained_and_refuses_overwrite(self):
        target = Path(self.temp.name) / 'custom skill' / 'logscope'
        install(target)
        self.assertTrue((target / 'scripts/logscope.py').exists())
        with self.assertRaises(ValueError):
            install(target)
        result = subprocess.run([sys.executable, str(target / 'scripts/logscope.py'), '--url', self.url, 'datasets'],
                                capture_output=True, text=True, encoding='utf-8', timeout=20)
        self.assertEqual(json.loads(result.stdout)[0]['id'], self.dataset)

    def test_real_pty_input_unicode_resize_interrupt_and_report(self):
        config = self.api('/api/terminal/config')
        if not config['available']:
            self.fail('Terminal dependency unavailable in test environment: ' + config['reason'])
        self.api('/api/terminal/config', {'command': ''})
        info = self.api('/api/terminal/start', {'dataset': self.dataset, 'question': '接口 /api/x?arg="& test" 报错', 'cols': 100, 'rows': 30})
        identifier = info['id']
        # The child must observe a TTY; pipes are not sufficient for interactive Claude.
        script = Path(info['cwd']) / 'interactive_test.py'
        script.write_text("import sys, time\nprint('PTY_' + str(sys.stdin.isatty()), flush=True)\nvalue=input('INPUT_REQUIRED>')\nprint('REPLY_' + value, flush=True)\ntry:\n time.sleep(30)\nexcept KeyboardInterrupt:\n print('INTERRUPTED_OK',flush=True)\n", 'utf-8')
        if os.name == 'nt':
            command = subprocess.list2cmdline([sys.executable, str(script)])
        else:
            import shlex
            command = shlex.join([sys.executable, str(script)])
        try:
            self.api('/api/terminal/input', {'id': identifier, 'data': command + '\r'})
            first_output, cursor = self.wait_output(identifier, 'INPUT_REQUIRED>')
            self.assertIn('PTY_True', first_output)
            self.api('/api/terminal/resize', {'id': identifier, 'cols': 132, 'rows': 40})
            self.api('/api/terminal/input', {'id': identifier, 'data': '中文确认 yes\r'})
            reply, cursor = self.wait_output(identifier, 'REPLY_中文确认 yes', cursor=cursor)
            self.api('/api/terminal/input', {'id': identifier, 'data': '\x03'})
            self.wait_output(identifier, 'INTERRUPTED_OK', cursor=cursor)
            task = json.loads((Path(info['cwd']) / 'task.json').read_text('utf-8'))
            self.assertEqual(task['dataset'], self.dataset)
            self.assertTrue((Path(info['cwd']) / '.claude/skills/logscope/SKILL.md').exists())
            (Path(info['cwd']) / 'report.md').write_text('# 模拟报告\n原文已核验。', 'utf-8')
            report = self.api('/api/terminal/report?id=' + identifier)
            self.assertTrue(report['available'])
            self.assertIn('原文已核验', report['text'])
            self.assertEqual(self.api('/api/terminal/sessions')[-1]['id'], identifier)
        finally:
            self.api('/api/terminal/stop', {'id': identifier})
        self.assertEqual(self.api('/api/terminal/output?id=' + identifier)['state'], 'stopped')

    def test_terminal_auth_boundary_and_untrusted_question_not_executed(self):
        from urllib.error import HTTPError
        with self.assertRaises(HTTPError) as error:
            request = Request(self.url + '/api/terminal/start', data=json.dumps({'dataset':self.dataset}).encode(),
                              headers={'Content-Type':'application/json', 'Origin':'https://evil.invalid'})
            urlopen(request)
        self.assertEqual(error.exception.code, 403)


if __name__ == '__main__':
    unittest.main()
