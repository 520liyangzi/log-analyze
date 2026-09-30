"""Generated archives exercise staging, persisted review and explicit import."""
import gzip
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
import uuid
import zipfile

from app import Store
from import_plans import ImportConflict


FIELDS = ('id', 'included', 'patterns', 'node', 'namespace', 'pod', 'service', 'kind', 'line_mode')


def line(marker):
    return ('[2026-09-30 09:29:02.186 +0800] [9124859898865451127] [9124859898865451127] '
            '[INFO] [worker] [Sample.java] [com.example] [run] [10] ' + marker + '\n')


class ImportPlansTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='logscope plans ')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / 'data')
        self.addCleanup(lambda: self.store.pool.shutdown(wait=True))

    def archive(self, name='fixture.zip', extra=None):
        nested = io.BytesIO()
        with zipfile.ZipFile(nested, 'w', compression=zipfile.ZIP_DEFLATED) as output:
            output.writestr('custom/events/wire.txt', 'wire-one\nwire-two\n')
            output.writestr('custom/events/history.log.gz', gzip.compress(line('history-marker').encode()))
        path = self.root / name
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as output:
            output.writestr('pod-a/output/service.log', line('service-first') + line('service-second'))
            output.writestr('pod-a/output/image.png', b'\x89PNG\x00\x00\x00image')
            output.writestr('docs/readme.md', '# Documentation, excluded by default\n')
            output.writestr('node-b.zip', nested.getvalue())
            for name, content in (extra or {}).items():
                output.writestr(name, content)
        return path

    def drain(self):
        self.store.pool.submit(lambda: None).result(timeout=20)

    def stage(self, name='fixture.zip', extra=None):
        source = self.archive(name, extra)
        identifier = self.store.submit(source, name, review=True)
        self.drain()
        self.assertFalse(source.exists())
        preview = self.store.imports.preview(identifier)
        self.assertEqual(preview['state'], 'review', preview)
        return identifier, preview

    @staticmethod
    def body(preview, groups=None, **options):
        return dict(dataset=preview['id'], revision=preview['revision'],
                    groups=groups if groups is not None else [
                        {key: group[key] for key in FIELDS} for group in preview['groups']],
                    **dict({key: preview[key] for key in ('encoding', 'offset', 'unit')}, **options))

    def counts(self, identifier):
        with self.store.connect() as db:
            return tuple(db.execute(f'SELECT count(*) FROM {table} WHERE dataset=?', (identifier,)).fetchone()[0]
                         for table in ('logs', 'files'))

    def row(self, identifier):
        with self.store.connect() as db:
            return dict(db.execute('SELECT * FROM datasets WHERE id=?', (identifier,)).fetchone())

    def original_hash(self, identifier):
        return hashlib.sha256((self.store.directory / 'archives' / (identifier + '.zip')).read_bytes()).hexdigest()

    def reopen(self):
        self.store.pool.shutdown(wait=True)
        self.store = Store(self.root / 'data')

    def test_review_preserves_original_and_has_no_log_index(self):
        source = self.archive()
        original = hashlib.sha256(source.read_bytes()).hexdigest()
        identifier = self.store.submit(source, 'fixture.zip', review=True)
        self.drain()
        preview = self.store.imports.preview(identifier)
        self.assertEqual(preview['state'], 'review')
        self.assertEqual(self.counts(identifier), (0, 0))
        self.assertEqual(self.original_hash(identifier), original)
        self.assertEqual(len(preview['groups']), 3)
        self.assertTrue(any(group['archive_chain'] == ['fixture.zip', 'node-b.zip'] for group in preview['groups']))
        self.assertNotIn(str(self.root), json.dumps(preview, ensure_ascii=False))
        with self.assertRaises(ValueError):
            self.store.search({'dataset': identifier})

    def test_saved_draft_survives_restart_and_old_revision_conflicts(self):
        identifier, preview = self.stage()
        edits = [{key: group[key] for key in FIELDS} for group in preview['groups']]
        edits[0].update(node='custom-node', pod='custom-pod', patterns='service.log', line_mode='lines')
        saved = self.store.imports.save_draft(self.body(preview, edits, offset='+0000', unit='s'))
        self.assertNotEqual(saved['revision'], preview['revision'])
        with self.assertRaises(ImportConflict):
            self.store.imports.save_draft(self.body(preview))
        self.reopen()
        restored = self.store.imports.preview(identifier)
        self.assertEqual(restored['revision'], saved['revision'])
        self.assertEqual(restored['groups'][0]['pod'], 'custom-pod')
        self.assertEqual((restored['offset'], restored['unit']), ('+0000', 's'))
        self.assertEqual(self.counts(identifier), (0, 0))

    def test_changed_encoding_requires_rescan_before_confirming_gb18030_text(self):
        source = self.root / 'gb18030.zip'
        marker = '发现编码问题服务异常，需要检查中文日志内容'
        with zipfile.ZipFile(source, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('custom/app.log', line(marker).encode('gb18030'))
        identifier = self.store.submit(source, source.name, encoding='utf-8', review=True)
        self.drain()
        preview = self.store.imports.preview(identifier)
        self.assertEqual(preview['state'], 'review')
        self.assertEqual((preview['encoding'], preview['scan_encoding']), ('utf-8', 'utf-8'))
        self.assertFalse(preview['groups'][0]['files'][0]['text'])
        # Older sidecars are upgraded using the original encoding, before any
        # draft edits are applied; clients cannot spoof the scan metadata.
        plan_path = self.store.directory / 'import-plans' / (identifier + '.json')
        original = json.loads(plan_path.read_text('utf-8'))
        original.pop('scan_encoding')
        plan_path.write_text(json.dumps(original), 'utf-8')
        body = self.body(preview, encoding='auto')
        body['scan_encoding'] = 'auto'
        saved = self.store.imports.save_draft(body)
        self.assertEqual((saved['encoding'], saved['scan_encoding']), ('auto', 'utf-8'))
        with self.assertRaisesRegex(ValueError, '编码已更改，请先重新扫描'):
            self.store.imports.confirm(self.body(saved))
        self.assertEqual(self.counts(identifier), (0, 0))
        self.assertEqual(self.store.imports.preview(identifier)['revision'], saved['revision'])
        self.store.imports.rescan(identifier)
        self.drain()
        rescanned = self.store.imports.preview(identifier)
        self.assertEqual((rescanned['encoding'], rescanned['scan_encoding']), ('auto', 'auto'))
        self.assertTrue(rescanned['groups'][0]['files'][0]['text'])
        self.assertIn(marker, rescanned['groups'][0]['sample'])
        self.store.imports.confirm(self.body(rescanned))
        self.drain()
        self.assertEqual(self.row(identifier)['state'], 'ready')
        matches = self.store.search({'dataset': identifier, 'q': marker})
        self.assertEqual(matches['summary']['total'], 1)
        self.assertTrue(self.store.verify(matches['rows'][0]['id'])['verified'])

    def test_confirm_builds_selected_indexes_and_verifies_immutable_sources(self):
        identifier, preview = self.stage()
        original = self.original_hash(identifier)
        edits = [{key: group[key] for key in FIELDS} for group in preview['groups']]
        for edit in edits:
            edit['included'] = False
        selected = next(group for group in preview['groups'] if group['directory'] == 'pod-a/output')
        edit = next(group for group in edits if group['id'] == selected['id'])
        edit.update(included=True, patterns='service.log', node='my-node', namespace='my-space',
                    pod='my-pod', service='my-service', kind='my-kind', line_mode='lines')
        result = self.store.imports.confirm(self.body(preview, edits))
        self.assertEqual(result, {'id': identifier, 'state': 'importing'})
        self.drain()
        self.assertEqual(self.row(identifier)['state'], 'ready')
        found = self.store.search({'dataset': identifier, 'q': 'service-'})
        self.assertEqual(found['summary']['total'], 2)
        row = found['rows'][0]
        self.assertEqual((row['node'], row['pod'], row['kind']), ('my-node', 'my-pod', 'my-kind'))
        self.assertEqual(row['path'], 'pod-a/output/service.log')
        self.assertEqual(row['archive'], 'fixture.zip')
        self.assertTrue(self.store.verify(row['id'])['verified'])
        self.assertEqual(self.original_hash(identifier), original)
        self.assertEqual(self.counts(identifier), (2, 1))
        for action in (lambda: self.store.imports.confirm(self.body(preview)),
                       lambda: self.store.imports.save_draft(self.body(preview)),
                       lambda: self.store.imports.rescan(identifier)):
            with self.assertRaises(ImportConflict):
                action()

    def test_nested_gzip_text_without_timestamps_and_double_confirm(self):
        identifier, preview = self.stage()
        entered, release = threading.Event(), threading.Event()
        blocker = self.store.pool.submit(lambda: (entered.set(), release.wait(5)))
        self.assertTrue(entered.wait(2))
        try:
            self.store.imports.confirm(self.body(preview))
            with self.assertRaises(ImportConflict):
                self.store.imports.confirm(self.body(preview))
        finally:
            release.set()
            blocker.result(timeout=5)
        self.drain()
        self.assertEqual(self.counts(identifier), (5, 3))
        gzip_row = self.store.search({'dataset': identifier, 'q': 'history-marker'})['rows'][0]
        self.assertIn('fixture.zip → node-b.zip', gzip_row['source'])
        self.assertTrue(self.store.verify(gzip_row['id'])['verified'])
        unknown = self.store.search({'dataset': identifier, 'q': 'wire-'})
        self.assertEqual(unknown['summary']['total'], 2)
        self.assertTrue(all(row['ts'] is None for row in unknown['rows']))

    def test_empty_selection_can_be_saved_but_cannot_be_confirmed(self):
        identifier, preview = self.stage()
        edits = [dict(id=group['id'], included=False) for group in preview['groups']]
        saved = self.store.imports.save_draft(self.body(preview, edits))
        self.assertFalse(any(group['included'] for group in saved['groups']))
        with self.assertRaisesRegex(ValueError, '没有匹配'):
            self.store.imports.confirm(self.body(saved))
        self.assertEqual(self.row(identifier)['state'], 'review')
        self.assertEqual(self.store.imports.preview(identifier)['revision'], saved['revision'])

    def test_binary_or_broken_gzip_matches_are_rejected_explicitly(self):
        for filename, content in [('binary.log', b'\x00\x01\x02\x03'), ('broken.log.gz', b'not gzip')]:
            with self.subTest(filename=filename):
                identifier, preview = self.stage(filename + '.zip', {'pod-a/output/' + filename: content})
                with self.assertRaisesRegex(ValueError, filename.replace('.', '\\.')):
                    self.store.imports.confirm(self.body(preview))
                self.assertEqual(self.row(identifier)['state'], 'review')
                self.assertEqual(self.counts(identifier), (0, 0))
                selected = next(group for group in preview['groups'] if group['directory'] == 'pod-a/output')
                saved = self.store.imports.save_draft(self.body(preview, [dict(id=selected['id'], patterns='service.log')]))
                self.store.imports.confirm(self.body(saved))
                self.drain()
                self.assertEqual(self.row(identifier)['state'], 'ready')

    def test_explicit_wildcard_does_not_silently_skip_binary_file(self):
        identifier, preview = self.stage()
        selected = next(group for group in preview['groups'] if group['directory'] == 'pod-a/output')
        with self.assertRaisesRegex(ValueError, 'image.png'):
            self.store.imports.confirm(self.body(preview, [dict(id=selected['id'], patterns='*')]))
        self.assertEqual(self.counts(identifier), (0, 0))

    def test_binary_manifest_is_excluded_by_default_but_explicit_selection_is_rejected(self):
        source = self.root / 'manifest.zip'
        with zipfile.ZipFile(source, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('valid.log', line('manifest-exclusion-marker'))
            archive.writestr('fileList.txt', b'\x00\x01\x02not-a-text-manifest')
        identifier = self.store.submit(source, source.name, review=True)
        self.drain()
        preview = self.store.imports.preview(identifier)
        self.assertEqual(preview['state'], 'review')
        self.assertEqual(len(preview['groups']), 1)
        group = preview['groups'][0]
        self.assertFalse(next(file for file in group['files'] if file['name'] == 'fileList.txt')['text'])
        with self.assertRaisesRegex(ValueError, r'fileList\.txt'):
            self.store.imports.confirm(self.body(preview, [dict(id=group['id'], patterns='fileList.txt')]))
        self.assertEqual(self.counts(identifier), (0, 0))
        self.assertEqual(self.store.imports.preview(identifier)['revision'], preview['revision'])
        self.store.imports.confirm(self.body(preview))
        self.drain()
        self.assertEqual(self.row(identifier)['state'], 'ready')
        self.assertEqual(self.counts(identifier), (1, 1))
        match = self.store.search({'dataset': identifier, 'q': 'manifest-exclusion-marker'})['rows'][0]
        self.assertEqual(match['filename'], 'valid.log')
        self.assertTrue(self.store.verify(match['id'])['verified'])

    def test_manifest_distinguishes_intentional_layout_exclusion_from_missing_file(self):
        source = self.root / 'selected-manifest.zip'
        with zipfile.ZipFile(source, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('chosen.log', line('chosen-layout-marker'))
            archive.writestr('excluded.log', line('excluded-layout-marker'))
            archive.writestr('fileList.txt', 'chosen.log\nexcluded.log\nmissing.log\n')
        identifier = self.store.submit(source, source.name, review=True)
        self.drain()
        preview = self.store.imports.preview(identifier)
        selected = preview['groups'][0]
        self.store.imports.confirm(self.body(preview, [dict(id=selected['id'], included=True, patterns='chosen.log')]))
        self.drain()
        row = self.row(identifier)
        self.assertEqual(row['state'], 'ready', row)
        audit = json.loads(row['audit'])
        self.assertEqual(audit['listed_files'], 3)
        self.assertEqual(audit['missing_count'], 1)
        self.assertEqual(audit['excluded_by_layout_count'], 1)
        self.assertEqual(audit['missing'], ['missing.log'])
        self.assertEqual(self.store.search({'dataset': identifier, 'q': 'chosen-layout-marker'})['summary']['total'], 1)
        self.assertEqual(self.store.search({'dataset': identifier, 'q': 'excluded-layout-marker'})['summary']['total'], 0)

    def test_scan_failure_and_rescan_keep_original_zip(self):
        source = self.archive()
        original = hashlib.sha256(source.read_bytes()).hexdigest()
        with mock.patch('archive_layout.scan_archive', side_effect=ValueError('synthetic scan failure')):
            identifier = self.store.submit(source, 'fixture.zip', review=True)
            self.drain()
        self.assertEqual(self.row(identifier)['state'], 'failed')
        self.assertIn('synthetic scan failure', self.row(identifier)['error'])
        self.assertEqual(self.original_hash(identifier), original)
        response = self.store.imports.rescan(identifier)
        self.assertEqual(response['state'], 'scanning')
        self.drain()
        self.assertEqual(self.store.imports.preview(identifier)['state'], 'review')
        self.assertEqual(self.original_hash(identifier), original)

    def test_scan_state_after_restart_becomes_retryable_failure(self):
        identifier, preview = self.stage()
        with self.store.connect() as db:
            db.execute("UPDATE datasets SET state='scanning' WHERE id=?", (identifier,))
        self.reopen()
        state = self.store.imports.preview(identifier)
        self.assertEqual(state['state'], 'failed')
        self.assertIn('扫描被中断', state['error'])
        self.assertEqual(state['revision'], preview['revision'])
        self.store.imports.rescan(identifier)
        self.drain()
        self.assertEqual(self.row(identifier)['state'], 'review')

    def test_import_failure_retains_plan_and_archive_and_can_be_reconfirmed(self):
        identifier, preview = self.stage()
        original = self.original_hash(identifier)
        with mock.patch('app.parse_line', side_effect=ValueError('synthetic parse failure')):
            self.store.imports.confirm(self.body(preview))
            self.drain()
        failed = self.store.imports.preview(identifier)
        self.assertEqual(failed['state'], 'failed')
        self.assertEqual(self.counts(identifier), (0, 0))
        self.assertEqual(self.original_hash(identifier), original)
        self.reopen()
        self.store.imports.confirm(self.body(self.store.imports.preview(identifier)))
        self.drain()
        self.assertEqual(self.row(identifier)['state'], 'ready')
        self.assertEqual(self.counts(identifier), (5, 3))

    def test_restart_reconfirm_replaces_committed_partial_rows_without_id_reuse(self):
        identifier, preview = self.stage()
        with self.store.connect() as db:
            file_id = db.execute('INSERT INTO files(dataset,node,pod,filename) VALUES(?,?,?,?)',
                                 (identifier, 'stale-node', 'stale-pod', 'stale.log')).lastrowid
            db.execute('INSERT INTO logs(id,dataset,file_id,line,end_line,raw) VALUES(?,?,?,?,?,?)',
                       (66, identifier, file_id, 1, 1, 'stale-partially-imported-marker'))
            if self.store.write_fts:
                db.execute(f'INSERT INTO {self.store.write_fts}(rowid,raw) VALUES(?,?)',
                           (66, 'stale-partially-imported-marker'))
            db.execute("UPDATE log_metadata SET value='66' WHERE key='last_log_id'")
            db.execute("UPDATE datasets SET state='importing' WHERE id=?", (identifier,))
        self.reopen()
        recovered = self.store.imports.preview(identifier)
        self.assertEqual(recovered['state'], 'failed')
        self.assertEqual(recovered['revision'], preview['revision'])
        self.assertEqual(self.counts(identifier), (1, 1))
        self.store.imports.confirm(self.body(recovered))
        self.drain()
        self.assertEqual(self.counts(identifier), (5, 3))
        self.assertEqual(self.store.search({'dataset': identifier, 'q': 'stale-partially-imported-marker'})['summary']['total'], 0)
        self.assertGreater(min(row['id'] for row in self.store.search({'dataset': identifier})['rows']), 66)

    def test_oversized_draft_does_not_overwrite_prior_plan(self):
        identifier, preview = self.stage()
        path = self.store.directory / 'import-plans' / (identifier + '.json')
        original = path.read_bytes()
        edits = [dict(id=group['id'], node='n' * 300, namespace='s' * 300, pod='p' * 300)
                 for group in preview['groups']]
        with mock.patch.object(self.store.imports, 'MAX_PLAN_BYTES', len(original) + 20):
            with self.assertRaisesRegex(ValueError, '导入计划超过'):
                self.store.imports.save_draft(self.body(preview, edits))
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.store.imports.preview(identifier)['revision'], preview['revision'])

    def test_queue_failures_never_leave_false_scanning_or_importing_states(self):
        source = self.archive()
        with mock.patch.object(self.store.pool, 'submit', side_effect=RuntimeError('queue stopped')):
            with self.assertRaises(ValueError):
                self.store.submit(source, 'fixture.zip', review=True)
        identifier = self.store.datasets()[0]['id']
        self.assertEqual(self.row(identifier)['state'], 'failed')
        self.assertTrue((self.store.directory / 'archives' / (identifier + '.zip')).exists())
        with mock.patch.object(self.store.pool, 'submit', side_effect=RuntimeError('queue stopped')):
            with self.assertRaises(ValueError):
                self.store.imports.rescan(identifier)
        self.assertEqual(self.row(identifier)['state'], 'failed')
        self.store.imports.rescan(identifier)
        self.drain()
        preview = self.store.imports.preview(identifier)
        with mock.patch.object(self.store.pool, 'submit', side_effect=RuntimeError('queue stopped')):
            with self.assertRaises(ValueError):
                self.store.imports.confirm(self.body(preview))
        self.assertEqual(self.row(identifier)['state'], 'failed')
        self.assertEqual(self.counts(identifier), (0, 0))
        self.assertTrue((self.store.directory / 'archives' / (identifier + '.zip')).exists())

    def test_untrusted_ids_immutable_paths_and_invalid_options_are_rejected(self):
        identifier, preview = self.stage()
        for invalid in ('../data', str(self.root), uuid.uuid4().hex, identifier + '.zip'):
            for action in (lambda: self.store.imports.preview(invalid), lambda: self.store.imports.rescan(invalid)):
                with self.subTest(identifier=invalid), self.assertRaises(ValueError):
                    action()
        with self.assertRaisesRegex(ValueError, '归档来源'):
            self.store.imports.save_draft(self.body(preview, [dict(id=preview['groups'][0]['id'], directory='../changed')]))
        for options in ({'encoding': 'invalid'}, {'offset': '../../secret'}, {'unit': 'days'}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.store.imports.save_draft(self.body(preview, **options))
        self.assertEqual(self.store.imports.preview(identifier)['revision'], preview['revision'])

    def test_damaged_or_missing_sidecar_has_recovery_preview_and_rescan(self):
        identifier, _ = self.stage()
        path = self.store.directory / 'import-plans' / (identifier + '.json')
        path.write_text('{broken', 'utf-8')
        preview = self.store.imports.preview(identifier)
        self.assertEqual(preview['groups'], [])
        self.assertEqual(preview['revision'], '')
        self.assertIn('重新扫描', preview['warnings'][0])
        self.store.imports.rescan(identifier)
        self.drain()
        self.assertTrue(self.store.imports.preview(identifier)['groups'])


if __name__ == '__main__':
    unittest.main()
