import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app import Store, make_server
from ai_client import DEFAULT_CONFIG
from demo import create_demo
from test_chat import FakeModel


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
            model = FakeModel()
            server.chats.config.path.write_text(json.dumps(dict(DEFAULT_CONFIG, base_url=model.url,
                                                                 api_key='fake-secret-123', model='fake-model')), 'utf-8')
            try:
                first = server.store.submit(create_demo(Path(temp) / 'first.zip'), 'first.zip')
                second = server.store.submit(create_demo(Path(temp) / 'second.zip'), 'second.zip')
                self.wait_ready(server, [first, second])
                archive = server.store.directory / 'archives' / (first + '.zip')
                self.assertTrue(archive.exists())
                model.replies = ['wait']
                _, preview = self.api(base, '/api/chat/preview', {'dataset': first, 'question': '占用测试'})
                _, session = self.api(base, '/api/chat/send', {'preview_id': preview['preview_id'], 'request_id': 'delete-in-use-test'})
                try:
                    with self.assertRaises(HTTPError) as error:
                        self.api(base, '/api/datasets/delete', {'dataset': first})
                    with error.exception as failure:
                        self.assertEqual(failure.code, 400)
                    self.assertTrue(archive.exists())
                    self.assertTrue(server.chats.dataset_in_use(first))
                finally:
                    self.api(base, '/api/chat/stop', {'id': session['id']})
                    model.gate.set()
                    server.chats.pool.shutdown(wait=True)
                self.assertFalse(server.chats.dataset_in_use(first))
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
                    for table in server.store.fts_tables:
                        self.assertEqual(db.execute(f'SELECT count(*) FROM {table} WHERE rowid NOT IN (SELECT id FROM logs)').fetchone()[0], 0)
            finally:
                model.gate.set()
                server.shutdown()
                server.server_close()
                thread.join()
                server.store.pool.shutdown(wait=True)
                server.chats.pool.shutdown(wait=True)
                server.chats.projects.pool.shutdown(wait=True)
                model.close()

    def test_restart_finishes_interrupted_deletion(self):
        with tempfile.TemporaryDirectory() as temp:
            store=Store(Path(temp)/'data')
            with store.connect() as db:
                db.execute("INSERT INTO datasets(id,name,state,created) VALUES('stale','old.zip','deleting','2026')")
            store.pool.shutdown(wait=True)
            reopened=Store(Path(temp)/'data')
            try:
                self.assertFalse(any(row['id']=='stale' for row in reopened.datasets()))
            finally:
                reopened.pool.shutdown(wait=True)

    def test_explicit_compaction_delete_still_supported(self):
        with tempfile.TemporaryDirectory() as temp:
            store=Store(Path(temp)/'data')
            identifier=store.submit(create_demo(Path(temp)/'compact.zip'),'compact.zip')
            store.pool.shutdown(wait=True)
            store.delete_dataset(identifier, compact=True)
            self.assertFalse(any(row['id']==identifier for row in store.datasets()))
