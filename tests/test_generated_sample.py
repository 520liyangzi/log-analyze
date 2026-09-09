"""End-to-end checks using the repository's realistic synthetic log archive."""
from pathlib import Path
import tempfile
import unittest

from app import Store
from sample_logs import ROUTE_CHAT, TRACE_CHAT, TRACE_MODEL, create_sample


class GeneratedSampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='logscope generated sample ')
        cls.archive = create_sample(Path(cls.temp.name) / 'generated-log-sample.zip')
        cls.store = Store(Path(cls.temp.name) / 'data')
        cls.dataset = cls.store.submit(cls.archive, cls.archive.name)
        cls.store.pool.shutdown(wait=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_archive_shape_manifest_and_all_records_verify(self):
        info = self.store.datasets()[0]
        self.assertEqual(info['files'], 16)
        self.assertEqual(info['records'], 30)
        self.assertEqual(info['audit']['listed_files'], 16)
        self.assertEqual(info['audit']['actual_files'], 16)
        self.assertEqual(info['audit']['missing_count'], 0)
        self.assertEqual(info['audit']['unlisted_count'], 0)
        rows = self.store.search({'dataset': self.dataset, 'size': 200})['rows']
        self.assertEqual(len(rows), info['records'])
        self.assertTrue(all(self.store.verify(row['id'])['verified'] for row in rows))
        self.assertEqual({row['node'] for row in rows},
                         {'vnf-demo-1-node01-1788749694', 'vnf-demo-1-node02-1788749694'})
        self.assertEqual({row['kind'] for row in rows}, {'access', 'root', 'rest', 'wsf'})

    def test_global_endpoint_node_type_and_gzip_search(self):
        global_result = self.store.search({'dataset': self.dataset, 'endpoint': '/api/model/map',
                                           'access_only': '1', 'size': 50})
        self.assertEqual(global_result['summary']['total'], 2)
        failed = self.store.search({'dataset': self.dataset, 'endpoint': '/api/model/map',
                                    'status': '5xx', 'access_only': '1', 'size': 50})
        self.assertEqual(failed['summary']['total'], 1)
        self.assertEqual((failed['rows'][0]['pod'], failed['rows'][0]['duration']),
                         ('pod-alpha-aaa111', 3012))
        scoped = self.store.search({'dataset': self.dataset, 'q': 'synthetic pool timeout',
                                    'node': 'vnf-demo-1-node01-1788749694', 'kind': 'root', 'size': 50})
        self.assertEqual(scoped['summary']['total'], 1)
        gzip_hit = self.store.search({'dataset': self.dataset, 'q': 'synthetic gzip history marker', 'size': 50})
        self.assertEqual(gzip_hit['summary']['total'], 2)
        self.assertTrue(all(row['filename'].endswith('.log.gz') for row in gzip_hit['rows']))
        self.assertTrue(all(row['source'].count(' → ') == 2 for row in gzip_hit['rows']))

    def test_trace_request_key_and_candidate_correlation(self):
        trace = self.store.search({'dataset': self.dataset, 'trace': TRACE_CHAT, 'size': 50})
        self.assertEqual(trace['summary']['total'], 8)
        self.assertEqual(len({row['pod'] for row in trace['rows']}), 2)
        model = self.store.search({'dataset': self.dataset, 'trace': TRACE_MODEL, 'size': 50})
        self.assertTrue(any(row['level'] == 'ERROR' for row in model['rows']))
        route = self.store.search({'dataset': self.dataset, 'request_key': ROUTE_CHAT + '-node1', 'size': 50})
        self.assertEqual(route['summary']['total'], 1)
        anchor = self.store.search({'dataset': self.dataset,
                                    'endpoint': '/api/rest/example/v2/query/chat-task',
                                    'pod': 'pod-alpha-aaa111', 'access_only': '1', 'size': 50})['rows'][0]
        related = self.store.correlate(anchor['id'], seconds=5, size=100)
        self.assertEqual({row['kind'] for row in related['rows']}, {'access', 'root', 'rest', 'wsf'})
        async_error = next(row for row in related['rows'] if row['level'] == 'ERROR')
        self.assertEqual(async_error['thread'], 'qtp-synthetic-4')
        self.assertIn('same_pod_time_window', async_error['association_reasons'])
        same_thread = self.store.correlate(anchor['id'], seconds=5, same_thread=True, size=100)
        self.assertFalse(any(row['id'] == async_error['id'] for row in same_thread['rows']))


if __name__ == '__main__':
    unittest.main()
