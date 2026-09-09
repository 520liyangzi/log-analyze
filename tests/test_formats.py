"""Synthetic regression cases based on the supplied format specification (no uploaded ZIP)."""
import gzip
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from app import Store, parse_line, source_meta, timestamp


class FormatTests(unittest.TestCase):
    def test_namespace_and_service_underscores_use_pod_service_directory(self):
        meta = source_meta('ns_private_region_pod-with_under/service_with-dash/pod-with_under-service_with-dash/log/root_20260908080001313+0800.log.gz', ['outer.zip','node.zip'])
        self.assertEqual((meta['namespace'],meta['pod'],meta['service'],meta['kind']),
                         ('ns_private_region','pod-with_under','service_with-dash','root'))

    def test_full_headers_route_ids_and_repeated_trace(self):
        access = '2026-09-08 09:29:39,885 INFO 162 [custom-thread][ROOT][][com.example.Valve 28] \\"POST /api/chat?key=one HTTP/1.1\\" 200 872 RouteID-synthetic-7 3155'
        row = parse_line(access)
        self.assertEqual((row['thread_id'],row['thread'],row['response_size'],row['route_id'],row['status'],row['duration']),
                         ('162','custom-thread',872,'RouteID-synthetic-7',200,3155))
        root = '[2026-09-08 09:29:37.522 +0800] [9000000000000000001] [9000000000000000001] [WARN] [qtp-worker-4] [Agent.java] [com.example.Agent] [lambda$read$7] [208] RequestId:RouteID-other-9, failed'
        row = parse_line(root)
        self.assertEqual(row['trace'],row['span'])
        self.assertEqual((row['request_id'],row['code_file'],row['logger'],row['code_method'],row['code_line']),
                         ('RouteID-other-9','Agent.java','com.example.Agent','lambda$read$7',208))
        wsf = '2026-09-08 09:00:01,819 INFO 162 [qtp200-61][ROOT][][com.example.Filter 367] [WSF-ParamValidate] message'
        self.assertEqual(parse_line(wsf)['module'],'WSF-ParamValidate')
        self.assertEqual(parse_line(wsf)['thread'],'qtp200-61')
        self.assertEqual(timestamp('20260908 09:00:01,819'),timestamp('2026-09-08 09:00:01.819 +0800'))

    def test_multi_type_http_200_async_error_and_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive=Path(tmp)/'synthetic.zip'
            listing=[]
            with zipfile.ZipFile(archive,'w') as outer:
                for node,pod in [('node-1','pod-one'),('node-2','pod-two')]:
                    buffer=io.BytesIO()
                    prefix=f'ns_test_{pod}/svc/{pod}-svc/log/'
                    files={
                        'access.log':'2026-09-08 09:29:39,885 INFO 162 [http-exec-3][ROOT][][Valve 28] "POST /api/chat?x=1 HTTP/1.1" 200 872 RouteID-one 3155\n',
                        'root.log':'[2026-09-08 09:29:37.522 +0800] [9999999999999999999] [9999999999999999999] [WARN] [http-exec-3] [Agent.java] [com.example.Agent] [read] [20] business warning\n',
                        'rest.log':'[2026-09-08 09:29:39.458 +0800] [] [] [ERROR] [qtp-worker-4] [Client.java] [com.example.Client] [callback] [12] asynchronous failure\n\tat java.base/java.lang.Thread.run(Unknown Source)\n',
                        'wsf.log':'2026-09-08 09:29:37,500 INFO 162 [http-exec-3][ROOT][][Filter 28] [WSF-Check] checking\n',
                    }
                    with zipfile.ZipFile(buffer,'w') as inner:
                        for name,text in files.items():
                            inner.writestr(prefix+name,text)
                            listing.append(node+'.zip/'+prefix+name)
                        name='rest_20260908080003490+0800.log.gz'
                        inner.writestr(prefix+name,gzip.compress(files['rest.log'].encode()))
                        listing.append(node+'.zip/'+prefix+name)
                    outer.writestr(node+'.zip',buffer.getvalue())
                outer.writestr('fileList.txt','\n'.join(listing+['missing-node.zip/ns_pod/svc/pod-svc/log/root.log']))
            store=Store(Path(tmp)/'data');dataset=store.submit(archive,'synthetic.zip');store.pool.shutdown(wait=True)
            info=store.datasets()[0]
            self.assertEqual(info['records'],10)
            self.assertEqual(info['audit']['missing_count'],1)
            self.assertEqual(info['audit']['unlisted_count'],0)
            self.assertEqual(info['audit']['physical_lines'],14)
            self.assertEqual(info['audit']['recognized_time'],10)
            anchor=store.search({'dataset':dataset,'endpoint':'/api/chat'})['rows'][0]
            self.assertEqual(anchor['status'],200)
            candidates=store.correlate(anchor['id'])
            self.assertEqual({r['kind'] for r in candidates['rows']},{'access','root','rest','wsf'})
            self.assertTrue(any(r['level']=='ERROR' and r['thread']=='qtp-worker-4' for r in candidates['rows']))
            for row in candidates['rows']:
                self.assertTrue(store.verify(row['id'])['verified'])
            narrow=store.correlate(anchor['id'],same_thread=True)
            self.assertFalse(any(r['thread']=='qtp-worker-4' for r in narrow['rows']))
            self.assertEqual(store.search({'dataset':dataset,'endpoint':'/api/cha'})['summary']['total'],0)
            self.assertEqual(store.search({'dataset':dataset,'request_key':'RouteID-one'})['summary']['total'],2)
            self.assertEqual(store.search({'dataset':dataset,'request_key':'RouteID-on'})['summary']['total'],0)
            self.assertEqual(store.search({'dataset':dataset,'trace':'9999999999999999999'})['summary']['total'],2)

    def test_migration_marks_old_parser_without_destroying_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(tmp)
            with store.connect() as db:
                db.execute("INSERT INTO datasets(id,name,state,created,parser_version) VALUES('legacy','old','ready','2026',1)")
            store.pool.shutdown(wait=True)
            reopened=Store(tmp)
            try:
                row=reopened.datasets()[0]
                self.assertEqual(row['state'],'ready')
                self.assertTrue(any('重新上传' in w for w in row['warnings']))
            finally:
                reopened.pool.shutdown(wait=True)


if __name__ == '__main__':
    unittest.main()
