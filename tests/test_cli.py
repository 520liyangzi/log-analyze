"""Standalone read-only CLI coverage, independent of any embedded terminal."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from app import make_server
from demo import create_demo, TRACE
from install_skill import install

BASE = Path(__file__).resolve().parents[1]


class CLITests(unittest.TestCase):
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

    def cli(self, *args, cwd=None):
        result = subprocess.run([sys.executable, str(BASE / 'skills/logscope/scripts/logscope.py'),
                                 '--url', self.url, '--dataset', self.dataset, *args],
                                capture_output=True, text=True, encoding='utf-8', cwd=cwd, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

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
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)[0]['id'], self.dataset)


if __name__ == '__main__':
    unittest.main()
