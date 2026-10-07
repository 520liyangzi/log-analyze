"""Generated ZIP fixtures exercise the explicit scan / review / import HTTP flow."""
import gzip
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen
import zipfile

from app import make_server


GROUP_FIELDS = ('id', 'included', 'patterns', 'node', 'namespace', 'pod', 'service', 'kind', 'line_mode')


def log_line(marker):
    return ('[2026-09-08 09:29:02.186 +0800] [9124859898865451127] [9124859898865451127] '
            '[INFO] [http-worker] [Fixture.java] [example.Fixture] [run] [10] ' + marker + '\n')


def make_mixed_zip():
    outer_bytes = io.BytesIO()
    with zipfile.ZipFile(outer_bytes, 'w', compression=zipfile.ZIP_DEFLATED) as outer:
        for node in ('node-a', 'node-b'):
            nested_bytes = io.BytesIO()
            with zipfile.ZipFile(nested_bytes, 'w', compression=zipfile.ZIP_DEFLATED) as nested:
                nested.writestr('shared/logs/root.log.gz', gzip.compress(log_line('fixture-' + node + '-gzip').encode()))
                if node == 'node-a':
                    nested.writestr('prod_pod-a/service/pod-a-service/log/root.log', log_line('fixture-legacy'))
                    nested.writestr('custom/pod-alpha/logs/app.log', log_line('fixture-custom'))
                    nested.writestr('custom/pod-alpha/logs/special.abc', log_line('fixture-special'))
            outer.writestr(node + '.zip', nested_bytes.getvalue())
        outer.writestr('stdout', 'fixture-stdout-first\nfixture-stdout-second\n')
        outer.writestr('excluded/logs/root.log', log_line('fixture-excluded'))
        outer.writestr('notes/readme.md', '# Generated documentation, not a log\n')
    return outer_bytes.getvalue()


def make_simple_zip():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('ns_pod/svc/pod-svc/log/root.log', log_line('fixture-simple'))
    return buffer.getvalue()


class ImportLayoutHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='logscope import-http ')
        self.root = Path(self.temp.name)
        self.server = self.thread = None
        self.start_server()

    def start_server(self):
        self.server = make_server(self.root / 'data', 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_address[1])

    def stop_server(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=10)
            self.server.store.pool.shutdown(wait=True)
            self.server = self.thread = None

    def tearDown(self):
        self.stop_server()
        self.temp.cleanup()

    def api(self, path, body=None, headers=None):
        request = Request(self.base + path, data=None if body is None else json.dumps(body).encode(),
                          headers=dict({'Content-Type': 'application/json'}, **(headers or {})))
        with urlopen(request, timeout=15) as response:
            return response.status, json.load(response)

    def assert_api_error(self, status, path, body=None, code=None, headers=None):
        with self.assertRaises(HTTPError) as error:
            self.api(path, body, headers)
        with error.exception as response:
            self.assertEqual(response.code, status)
            data = json.load(response)
            self.assertTrue(data.get('error'))
            if code:
                self.assertEqual(data.get('code'), code)
        return data

    def upload(self, raw, name='sample.zip'):
        request = Request(self.base + '/api/upload?name=' + quote(name), data=raw,
                          headers={'Content-Type': 'application/zip'})
        with urlopen(request, timeout=15) as response:
            self.assertEqual(response.status, 202)
            value = json.load(response)
        self.assertEqual(value['state'], 'scanning')
        return value['id']

    def wait_state(self, identifier, wanted='review'):
        deadline = time.monotonic() + 15
        latest = None
        while time.monotonic() < deadline:
            _, datasets = self.api('/api/datasets')
            latest = next((row for row in datasets if row['id'] == identifier), None)
            if latest and latest['state'] == wanted:
                return latest
            if latest and latest['state'] == 'failed' and wanted != 'failed':
                self.fail('Import unexpectedly failed: ' + str(latest))
            time.sleep(.02)
        self.fail(f'dataset did not reach {wanted}: {latest}')

    def preview(self, identifier):
        return self.api('/api/imports/preview?dataset=' + identifier)[1]

    def assert_no_index(self, identifier):
        with self.server.store.connect() as db:
            for table in ('logs', 'files'):
                self.assertEqual(db.execute(f'SELECT count(*) FROM {table} WHERE dataset=?', (identifier,)).fetchone()[0], 0)

    @staticmethod
    def body(preview):
        return dict(dataset=preview['id'], revision=preview['revision'],
                    encoding=preview['encoding'], offset=preview['offset'], unit=preview['unit'],
                    groups=[{key: group[key] for key in GROUP_FIELDS} for group in preview['groups']])

    def test_explicit_review_mixed_paths_custom_patterns_and_verified_originals(self):
        raw = make_mixed_zip()
        identifier = self.upload(raw, '混合日志目录.zip')
        self.wait_state(identifier)
        preview = self.preview(identifier)
        self.assertEqual(preview['state'], 'review')
        self.assertGreaterEqual(len(preview['groups']), 5)
        self.assert_no_index(identifier)
        self.assert_api_error(400, '/api/search?dataset=' + identifier + '&q=fixture')
        edits = self.body(preview)
        saw_special = saw_stdout = False
        for group, edit in zip(preview['groups'], edits['groups']):
            names = {file['name'] for file in group['files']}
            edit['included'] = bool(names & {'root.log', 'root.log.gz', 'app.log', 'special.abc', 'stdout'}) and not group['directory'].startswith('excluded')
            edit['patterns'] = '*.log,*.log.gz,*.abc,stdout'
            if 'special.abc' in names:
                saw_special = True
                edit.update(node='manual-node', namespace='manual-ns', pod='manual-pod', service='manual-service', kind='runtime')
            if 'stdout' in names:
                saw_stdout = True
                edit.update(node='stdout-node', pod='stdout-pod', kind='stdout', line_mode='lines')
            if 'root.log.gz' in names:
                edit['node'] = 'node-a' if 'node-a.zip' in group['archive_chain'] else 'node-b'
        self.assertTrue(saw_special and saw_stdout)
        _, saved = self.api('/api/imports/draft', edits)
        self.assertNotEqual(saved['revision'], preview['revision'])
        self.assert_no_index(identifier)
        status, accepted = self.api('/api/imports/confirm', {'dataset': identifier, 'revision': saved['revision']})
        self.assertEqual(status, 202)
        self.assertEqual(accepted['state'], 'importing')
        self.wait_state(identifier, 'ready')
        _, matches = self.api('/api/search?dataset=' + identifier + '&q=fixture-&size=200')
        self.assertEqual(matches['summary']['total'], 7)
        self.assertFalse(any('fixture-excluded' in row['raw'] for row in matches['rows']))
        special = next(row for row in matches['rows'] if 'fixture-special' in row['raw'])
        self.assertEqual((special['node'], special['namespace'], special['pod'], special['service'], special['kind']),
                         ('manual-node', 'manual-ns', 'manual-pod', 'manual-service', 'runtime'))
        gz = [row for row in matches['rows'] if row['filename'] == 'root.log.gz']
        self.assertEqual({row['node'] for row in gz}, {'node-a', 'node-b'})
        self.assertEqual(len({row['source'] for row in gz}), 2)
        stdout = [row for row in matches['rows'] if row['filename'] == 'stdout']
        self.assertEqual(len(stdout), 2)
        self.assertTrue(all(row['line'] == row['end_line'] for row in stdout))
        for row in matches['rows']:
            _, proof = self.api('/api/verify?id=' + str(row['id']))
            self.assertTrue(proof['verified'], proof)
            self.assertEqual(proof['raw'], row['raw'])
        with urlopen(self.base + '/api/archives/download?dataset=' + identifier, timeout=15) as response:
            self.assertEqual(response.read(), raw)

    def test_draft_restart_recovery_and_revision_conflicts(self):
        identifier = self.upload(make_simple_zip())
        self.wait_state(identifier)
        first = self.preview(identifier)
        edits = self.body(first)
        edits['groups'][0].update(pod='saved-pod', node='saved-node', patterns='root.log', included=True)
        _, saved = self.api('/api/imports/draft', edits)
        self.assert_api_error(409, '/api/imports/draft', edits, code='IMPORT_CONFLICT')
        self.assert_api_error(409, '/api/imports/confirm', {'dataset': identifier, 'revision': first['revision']}, code='IMPORT_CONFLICT')
        self.stop_server()
        self.start_server()
        restored = self.preview(identifier)
        self.assertEqual(restored['state'], 'review')
        self.assertEqual(restored['revision'], saved['revision'])
        self.assertEqual(restored['groups'][0]['pod'], 'saved-pod')
        self.assertEqual(restored['groups'][0]['node'], 'saved-node')
        self.assert_no_index(identifier)
        confirmation = {'dataset': identifier, 'revision': saved['revision']}
        self.assertEqual(self.api('/api/imports/confirm', confirmation)[0], 202)
        self.assert_api_error(409, '/api/imports/confirm', confirmation, code='IMPORT_CONFLICT')
        self.wait_state(identifier, 'ready')
        _, result = self.api('/api/search?dataset=' + identifier + '&q=fixture-simple')
        self.assertEqual(result['rows'][0]['pod'], 'saved-pod')

    def test_failed_import_keeps_original_zip_and_can_rescan_then_retry(self):
        raw = make_simple_zip()
        identifier = self.upload(raw)
        self.wait_state(identifier)
        preview = self.preview(identifier)
        original_open = zipfile.ZipFile.open

        def interrupted_read(archive, name, *args, **kwargs):
            filename = name.filename if isinstance(name, zipfile.ZipInfo) else name
            if filename.endswith('root.log'):
                raise OSError('synthetic temporary ZIP read failure')
            return original_open(archive, name, *args, **kwargs)

        with mock.patch('zipfile.ZipFile.open', interrupted_read):
            self.api('/api/imports/confirm', {'dataset': identifier, 'revision': preview['revision']})
            self.wait_state(identifier, 'failed')
        self.assert_no_index(identifier)
        with urlopen(self.base + '/api/archives/download?dataset=' + identifier, timeout=15) as response:
            self.assertEqual(response.read(), raw)
        old_groups = {group['id'] for group in preview['groups']}
        status, rescanning = self.api('/api/imports/rescan', {'dataset': identifier})
        self.assertEqual(status, 202)
        self.assertEqual(rescanning['state'], 'scanning')
        self.wait_state(identifier)
        retry = self.preview(identifier)
        self.assertEqual({group['id'] for group in retry['groups']}, old_groups)
        self.assertNotEqual(retry['revision'], preview['revision'])
        self.api('/api/imports/confirm', {'dataset': identifier, 'revision': retry['revision']})
        self.wait_state(identifier, 'ready')
        self.assertEqual(self.api('/api/search?dataset=' + identifier + '&q=fixture-simple')[1]['summary']['total'], 1)

    def test_review_can_be_deleted_without_ever_building_an_index(self):
        released, started = threading.Event(), threading.Event()

        def hold_import_worker():
            started.set()
            released.wait(timeout=10)

        self.server.store.pool.submit(hold_import_worker)
        self.assertTrue(started.wait(timeout=5))
        try:
            identifier = self.upload(make_simple_zip())
            self.assertEqual(self.api('/api/datasets')[1][0]['state'], 'scanning')
            self.assert_api_error(400, '/api/datasets/delete', {'dataset': identifier})
            self.assert_api_error(409, '/api/datasets/delete',
                                  {'dataset': identifier, 'import_only': True}, code='IMPORT_CONFLICT')
            self.assert_api_error(409, '/api/imports/confirm', {'dataset': identifier, 'revision': 0}, code='IMPORT_CONFLICT')
        finally:
            released.set()
        self.wait_state(identifier)
        self.assert_no_index(identifier)
        self.assertEqual(self.api('/api/datasets/delete',
                                 {'dataset': identifier, 'import_only': True, 'compact': False})[0], 202)
        self.server.store.pool.submit(lambda: None).result(timeout=15)
        _, datasets = self.api('/api/datasets')
        self.assertFalse(any(row['id'] == identifier for row in datasets))
        self.assertFalse((self.server.store.directory / 'archives' / (identifier + '.zip')).exists())
        self.assertFalse((self.server.store.directory / 'import-plans' / (identifier + '.json')).exists())
        self.assert_api_error(400, '/api/imports/preview?dataset=' + identifier)

    def test_stale_import_cancellation_does_not_delete_a_completed_package(self):
        identifier = self.upload(make_simple_zip())
        self.wait_state(identifier)
        self.api('/api/imports/confirm', self.body(self.preview(identifier)))
        self.wait_state(identifier, 'ready')
        self.assert_api_error(409, '/api/datasets/delete',
                              {'dataset': identifier, 'import_only': True}, code='IMPORT_CONFLICT')
        self.assertEqual(self.wait_state(identifier, 'ready')['state'], 'ready')
        self.assertTrue((self.server.store.directory / 'archives' / (identifier + '.zip')).exists())
        self.assertTrue((self.server.store.directory / 'import-plans' / (identifier + '.json')).exists())
        self.assertEqual(self.api('/api/search?dataset=' + identifier + '&q=fixture-simple')[1]['summary']['total'], 1)

    def test_import_endpoints_keep_host_and_origin_boundary(self):
        identifier = self.upload(make_simple_zip())
        self.wait_state(identifier)
        self.assert_api_error(403, '/api/imports/preview?dataset=' + identifier, headers={'Host': 'untrusted.invalid'})
        self.assert_api_error(403, '/api/imports/rescan', {'dataset': identifier}, headers={'Origin': 'http://untrusted.invalid'})
        self.assert_no_index(identifier)


if __name__ == '__main__':
    unittest.main()
