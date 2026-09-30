import gzip
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile

import archive_layout as layout


LOG = '2026-09-08 09:29:02.186 INFO test searchable marker\n'


def zip_bytes(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files:
            archive.writestr(name, content)
    return stream.getvalue()


class ArchiveLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'package.zip'

    def scan(self, files):
        self.path.write_bytes(zip_bytes(files))
        return layout.scan_archive(self.path, self.path.name)

    def test_legacy_metadata_and_field_order_are_preserved(self):
        name = 'ns_private_region_pod-with_under/service_with-dash/pod-with_under-service_with-dash/log/root_20260908080001313+0800.log.gz'
        chain = ['outer.zip', 'node.zip']
        meta = layout.legacy_meta(name, chain)
        self.assertEqual(list(meta), ['node', 'namespace', 'pod', 'service', 'kind', 'filename', 'archive', 'path', 'source'])
        self.assertEqual(meta['namespace'], 'ns_private_region')
        self.assertEqual(meta['pod'], 'pod-with_under')
        self.assertEqual(meta['service'], 'service_with-dash')
        self.assertEqual(meta['kind'], 'root')
        self.assertEqual(meta['node'], 'node')
        self.assertIsNone(layout.legacy_meta('service/logs/root.log', chain))
        self.assertIsNone(layout.legacy_meta('ns_pod/service/pod-service/log/data.txt', chain))

    def test_nested_zip_and_legacy_default_selection(self):
        name = 'ns_pod/svc/pod-svc/log/root.log'
        plan = self.scan([('node.zip', zip_bytes([(name, LOG)]))])
        group = plan['groups'][0]
        self.assertEqual(group['archive_chain'], ['package.zip', 'node.zip'])
        self.assertEqual(group['confidence'], 'high')
        self.assertEqual(group['line_mode'], 'auto')
        resolver = layout.build_resolver(plan)
        self.assertEqual(resolver.file_count, 1)
        meta, mode = resolver(name, group['archive_chain'])
        self.assertEqual(meta, layout.legacy_meta(name, group['archive_chain']))
        self.assertEqual(mode, 'auto')

    def test_service_logs_and_arbitrary_app_directories(self):
        plan = self.scan([('payments/pod-a/logs/access.log', LOG), ('billing/app/output.txt', 'hello\nworld\n'),
                          ('other/application/task.out', LOG)])
        self.assertEqual(len(plan['groups']), 3)
        self.assertEqual(layout.build_resolver(plan).file_count, 3)
        generic = plan['groups'][0]
        self.assertEqual(generic['namespace'], '')
        self.assertEqual(generic['service'], 'payments')
        self.assertEqual(generic['pod'], 'pod-a')
        self.assertEqual(generic['confidence'], 'low')
        self.assertEqual(plan['groups'][1]['line_mode'], 'lines')

    def test_root_gzip_extensionless_and_manual_unknown_file(self):
        plan = self.scan([('root.log.gz', gzip.compress(LOG.encode())), ('stdout', 'first\nsecond\n'),
                          ('custom.events', 'plain event\n'), ('fileList.txt', 'root.log.gz\n')])
        group = plan['groups'][0]
        self.assertEqual(group['directory'], '')
        resolver = layout.build_resolver(plan)
        self.assertEqual(resolver.file_count, 2)
        self.assertIsNone(resolver('custom.events', ['package.zip']))
        self.assertIsNone(resolver('fileList.txt', ['package.zip']))
        edited = layout.validate_edits(plan, [{'id': group['id'], 'patterns': '*'}])
        self.assertEqual(layout.build_resolver(edited).file_count, 4)
        self.assertEqual(plan['groups'][0]['patterns'], layout.DEFAULT_PATTERNS)

    def test_windows_paths_retain_exact_original_source(self):
        name = r'app\logs\run.log'
        plan = self.scan([(name, LOG)])
        # ZipInfo normalizes the platform's separator when constructing a ZIP
        # member: Windows writes this fixture as app/logs/run.log, while POSIX
        # preserves its backslashes.  Provenance must match the actual member
        # name read from the archive, not the pre-write fixture argument.
        with zipfile.ZipFile(self.path) as archive:
            member_name = archive.infolist()[0].filename
        other_spelling = name if member_name != name else name.replace('\\', '/')
        group = plan['groups'][0]
        self.assertEqual(group['directory'], 'app/logs')
        self.assertEqual(group['files'][0]['path'], member_name)
        resolver = layout.build_resolver(plan)
        meta, _ = resolver(member_name, ['package.zip'])
        self.assertEqual(meta['path'], member_name)
        self.assertEqual(meta['source'], 'package.zip → ' + member_name)
        self.assertNotEqual(other_spelling, member_name)
        self.assertIsNone(resolver(other_spelling, ['package.zip']))

    def test_binary_and_corrupt_gzip_never_become_selected_logs(self):
        plan = self.scan([('app/root.log', LOG), ('app/image.log', b'\x89PNG\x00\x01\x02\x03'),
                          ('app/broken.log.gz', b'not gzip'), ('app/secret.bin', b'\x00' * 30)])
        group = plan['groups'][0]
        self.assertEqual(sum(file['text'] for file in group['files']), 1)
        edited = layout.validate_edits(plan, [{'id': group['id'], 'patterns': '*'}])
        resolver = layout.build_resolver(edited)
        self.assertEqual(resolver.file_count, 1)
        self.assertIsNone(resolver('app/image.log', ['package.zip']))
        self.assertIn('无法读取', group['files'][2]['reason'])

    def test_namespace_not_guessed_from_generic_underscores(self):
        plan = self.scan([('foo_bar/task/output.log', LOG)])
        self.assertEqual(plan['groups'][0]['namespace'], '')
        self.assertEqual(plan['groups'][0]['pod'], '')

    def test_imperfect_legacy_directory_is_not_claimed_high_confidence(self):
        plan = self.scan([('some_namespace/b/unrelated/log/root.log', LOG)])
        self.assertEqual(plan['groups'][0]['confidence'], 'low')
        self.assertEqual(plan['groups'][0]['namespace'], '')
        self.assertEqual(plan['groups'][0]['pod'], 'unrelated')

    def test_utf8_prefix_cut_preserves_chinese_sample(self):
        with patch.object(layout, 'SAMPLE_BYTES', 16):
            plan = self.scan([('output.log', '这是中文日志样例内容\n')])
        self.assertEqual(plan['groups'][0]['sample'], '这是中文日')
        self.assertTrue(plan['groups'][0]['files'][0]['text'])

    def test_edit_labels_do_not_change_real_source(self):
        plan = self.scan([('custom/event.log', LOG)])
        identifier = plan['groups'][0]['id']
        edited = layout.validate_edits(plan, [{'id': identifier, 'node': 'real-node', 'namespace': 'real-ns',
                                              'pod': 'real-pod', 'service': 'real-service', 'kind': 'run', 'line_mode': 'lines'}])
        meta, mode = layout.build_resolver(edited)('custom/event.log', ['package.zip'])
        self.assertEqual(meta['node'], 'real-node')
        self.assertEqual(meta['kind'], 'run')
        self.assertEqual(mode, 'lines')
        self.assertEqual(meta['source'], 'package.zip → custom/event.log')
        self.assertEqual(meta['archive'], 'package.zip')
        self.assertEqual(meta['path'], 'custom/event.log')
        for field, value in [('archive_chain', ['another.zip']), ('directory', 'elsewhere'), ('files', [])]:
            with self.assertRaisesRegex(ValueError, '不可改写'):
                layout.validate_edits(plan, [{'id': identifier, field: value}])

    def test_invalid_edits_rejected(self):
        plan = self.scan([('output.log', LOG)])
        identifier = plan['groups'][0]['id']
        bad_edits = [[{'id': 'missing'}], [{'id': identifier}, {'id': identifier}],
                     [{'id': identifier, 'included': 'yes'}], [{'id': identifier, 'patterns': ''}],
                     [{'id': identifier, 'patterns': '../*'}], [{'id': identifier, 'node': 'bad\nnode'}],
                     [{'id': identifier, 'line_mode': 'guess'}]]
        for edits in bad_edits:
            with self.subTest(edits=edits), self.assertRaises(ValueError):
                layout.validate_edits(plan, edits)

    def test_duplicate_members_and_slash_equivalence_rejected(self):
        for names in [('a/log.txt', 'a/log.txt'), ('a/log.txt', r'a\log.txt')]:
            with self.subTest(names=names), warnings.catch_warnings():
                warnings.simplefilter('ignore', UserWarning)
                with self.assertRaisesRegex(ValueError, '重名成员'):
                    self.scan([(name, LOG) for name in names])

    def test_nested_depth_is_limited_and_four_levels_work(self):
        contents = zip_bytes([('root.log', LOG)])
        for _ in range(3):
            contents = zip_bytes([('inner.zip', contents)])
        plan = self.scan([('inner.zip', contents)])
        self.assertEqual(len(plan['groups'][0]['archive_chain']), 5)
        with self.assertRaisesRegex(ValueError, '4 层'):
            self.scan([('inner.zip', zip_bytes([('inner.zip', contents)]))])

    def test_preview_samples_are_bounded_and_all_file_metadata_retained(self):
        with patch.object(layout, 'MAX_SAMPLE_BYTES', 9000):
            plan = self.scan([(f'app{i}/root.log', LOG + 'x' * 200000) for i in range(8)])
        self.assertLessEqual(plan['sample_bytes'], 9000)
        self.assertEqual(sum(group['file_count'] for group in plan['groups']), 8)
        self.assertEqual(layout.build_resolver(plan).file_count, 8)
        self.assertLess(sum(len(file.get('sample', '')) for group in plan['groups'] for file in group['files']), 10000)

    def test_entries_groups_and_preview_size_fail_instead_of_truncating(self):
        for setting, value in [('MAX_ENTRIES', 1), ('MAX_GROUPS', 1), ('MAX_PLAN_BYTES', 100)]:
            with self.subTest(setting=setting), patch.object(layout, setting, value), self.assertRaises(ValueError):
                self.scan([('app/root.log', LOG), ('other/root.log', LOG)])

    def test_gb18030_and_gzip_read_small_prefix_only(self):
        text = '没有时间戳的日志内容\n' * 10000
        self.path.write_bytes(zip_bytes([('logs/output.txt.gz', gzip.compress(text.encode('gb18030')))]))
        plan = layout.scan_archive(self.path, 'package.zip', encoding='gb18030')
        file = plan['groups'][0]['files'][0]
        self.assertTrue(file['text'])
        self.assertIn('没有时间戳', file['sample'])
        self.assertEqual(plan['groups'][0]['line_mode'], 'lines')
        self.assertLessEqual(plan['sample_bytes'], layout.SAMPLE_BYTES)

    def test_paths_never_extract_to_user_filesystem(self):
        name = '../../must-not-exist.log'
        plan = self.scan([(name, LOG)])
        self.assertEqual(list(Path(self.temp.name).iterdir()), [self.path])
        self.assertEqual(plan['groups'][0]['files'][0]['path'], name)

    def test_zero_text_selection_is_counted_and_visible(self):
        plan = self.scan([('image.png', b'\x00' * 200)])
        self.assertFalse(plan['groups'][0]['included'])
        self.assertTrue(plan['warnings'])
        edited = layout.validate_edits(plan, [{'id': plan['groups'][0]['id'], 'included': True, 'patterns': '*'}])
        self.assertEqual(layout.build_resolver(edited).file_count, 0)

    def test_progress_is_plain_json_and_plan_does_not_contain_host_path(self):
        self.path.write_bytes(zip_bytes([('logs/root.log', LOG)]))
        events = []
        plan = layout.scan_archive(self.path, 'package.zip', progress=events.append)
        self.assertEqual(events[-1]['current'], '扫描完成')
        self.assertEqual(events[-1]['stage'], 'scanning')
        self.assertNotIn(self.temp.name, json.dumps(plan))


if __name__ == '__main__':
    unittest.main()
