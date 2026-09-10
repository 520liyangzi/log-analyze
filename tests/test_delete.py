import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app import make_server
from demo import create_demo


class DeleteDatasetTests(unittest.TestCase):
    def api(self, base, path, body=None):
        request = Request(base + path,
                          data=json.dumps(body).encode() if body is not None else None,
                          headers={'Content-Type': 'application/json'})
        with urlopen(request, timeout=20) as response:
            return response.status, json.load(response)

    def wait_ready(self, server, identifiers):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            states = {row['id']: row['state'] for row in server.store.datasets()}
            if all(states.get(identifier) == 'ready' for identifier in identifiers):
                return
            time.sleep(.03)
        self.fail('日志包未在测试时间内完成导入')

    def test_delete_reclaims_archive_index_and_keeps_other_dataset(self):
        with tempfile.TemporaryDirectory(prefix='logscope delete ') as temp:
            server = make_server(Path(temp) / 'data', 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = 'http://127.0.0.1:' + str(server.server_address[1])
            first = server.store.submit(create_demo(Path(temp) / 'first.zip'), 'first.zip')
            second = server.store.submit(create_demo(Path(temp) / 'second.zip'), 'second.zip')
            self.wait_ready(server, [first, second])
            archive = server.store.directory / 'archives' / (first + '.zip')
            self.assertTrue(archive.exists())
            session = server.terminals.start({'dataset': first, 'question': '占用测试', 'run_command': False})
            try:
                with self.assertRaises(HTTPError) as error:
                    self.api(base, '/api/datasets/delete', {'dataset': first})
                self.assertEqual(error.exception.code, 400)
            finally:
                self.api(base, '/api/terminal/stop', {'id': session['id']})
            status, _ = self.api(base, '/api/datasets/delete', {'dataset': first})
            self.assertEqual(status, 202)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and any(row['id'] == first for row in server.store.datasets()):
                time.sleep(.03)
            self.assertFalse(any(row['id'] == first for row in server.store.datasets()))
            self.assertTrue(any(row['id'] == second for row in server.store.datasets()))
            self.assertFalse(archive.exists())
            with server.store.connect() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM logs WHERE dataset=?', (first,)).fetchone()[0], 0)
                self.assertEqual(db.execute('SELECT count(*) FROM files WHERE dataset=?', (first,)).fetchone()[0], 0)
                self.assertGreater(db.execute('SELECT count(*) FROM logs WHERE dataset=?', (second,)).fetchone()[0], 0)
                if server.store.fts:
                    self.assertEqual(db.execute('SELECT count(*) FROM log_fts WHERE rowid NOT IN (SELECT id FROM logs)').fetchone()[0], 0)
            server.shutdown();server.server_close();thread.join()
            server.store.pool.shutdown(wait=True)
