import gzip
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import zipfile

from app import Store, parse_line, timestamp, make_server
from demo import create_demo, TRACE, root_line


class LogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.store = Store(Path(cls.temp.name)/'data')
        cls.archive = create_demo(Path(cls.temp.name)/'demo.zip')
        cls.identifier = cls.store.submit(cls.archive,'demo.zip')
        cls.store.pool.shutdown(wait=True)
        assert cls.store.datasets()[0]['state']=='ready', cls.store.datasets()

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def search(self, **params):
        return self.store.search(dict(dataset=self.identifier, **params))

    def test_user_access_format(self):
        line='2026-09-08 09:55:14,158 INFO  162 [http-nio-uds-exec-7][ROOT][][c.h.c.t.a.l.AccessLogValveExt 28] \\"GET xxxx/xxxx/xxxxx HTTP/1.1\\" 200 14 - 3'
        parsed=parse_line(line)
        self.assertEqual((parsed['thread'],parsed['status'],parsed['duration'],parsed['url']),('http-nio-uds-exec-7',200,3,'xxxx/xxxx/xxxxx'))
        self.assertEqual(parse_line(line,duration_unit='s')['duration'],3000)

    def test_user_root_format_and_timezone(self):
        line='[2026-09-08 09:29:02.186 +0800] [9124859898865451127] [9124859898865451127] [INFO] [http-nio-uds-exec-9] [aaaaservice.java] [com.xxxx] [Map] [125] Successfully loaded model map,count: 5'
        parsed=parse_line(line)
        self.assertEqual(parsed['trace'],TRACE)
        self.assertEqual(parsed['thread'],'http-nio-uds-exec-9')
        self.assertEqual(parsed['ts'],timestamp('2026-09-08 01:29:02.186 +0000'))

    def test_nested_zip_gzip_and_source(self):
        result=self.search(q='gzip-history-hit')
        self.assertEqual(result['summary']['total'],2)
        self.assertEqual(result['summary']['nodes'],2)
        self.assertIn('demo.zip → node-a.zip → prod_',result['rows'][0]['source'])
        self.assertTrue(result['rows'][0]['filename'].endswith('.gz'))
        self.assertEqual(result['rows'][0]['kind'],'root')

    def test_combined_filters_and_literal_query(self):
        result=self.search(q='/api/model/map',node='node-a',pod='model-service-7b8d9-x2k4m',kind='access',filename='access*.log',status='5xx',min_duration='3000')
        self.assertEqual(result['summary']['total'],1)
        self.assertEqual(result['rows'][0]['status'],500)
        self.assertEqual(self.search(q='%')['summary']['total'],0)
        self.assertEqual(self.search(q='map,count: 5',node='node-b',kind='root')['summary']['total'],80)
        self.assertEqual(self.search(q='MODEL MAP')['summary']['total'],159)
        self.assertEqual(self.search(q='MODEL MAP',case='1')['summary']['total'],0)
        self.assertEqual(self.search(q='模型缓存')['summary']['total'],3)

    def test_trace_exact_multiline_context(self):
        result=self.search(trace=TRACE)
        self.assertEqual(result['summary']['total'],6)
        self.assertEqual(self.search(trace=TRACE[:-1])['summary']['total'],0)
        errors=[r for r in result['rows'] if r['level']=='ERROR']
        self.assertEqual(len(errors),1)
        self.assertIn('Caused by:',errors[0]['raw'])
        self.assertEqual(errors[0]['end_line']-errors[0]['line'],3)
        self.assertTrue(any('ConnectionException' in r['raw'] for r in self.store.context(errors[0]['id'])))
        times=[r['ts'] for r in result['rows']]
        self.assertEqual(times,sorted(times))

    def test_time_thread_correlation(self):
        result=self.search(node='node-a',kind='root',thread='http-nio-uds-exec-7',start='2026-09-08 09:55:14.000 +0800',end='2026-09-08 09:55:14.999 +0800')
        self.assertEqual(result['summary']['total'],2)
        self.assertEqual(result['summary']['errors'],1)
        pasted=self.search(node='node-a',kind='root',start='2026-09-08 09:55:14.000',end='2026-09-08 09:55:14.999')
        self.assertEqual(pasted['summary']['total'],2)

    def test_complete_pagination_and_export(self):
        params=dict(dataset=self.identifier,q='/api/model/map',kind='access')
        first=self.store.search(params)
        self.assertEqual(first['summary']['total'],160)
        ids=[]
        for page in range(1,5):
            ids.extend(r['id'] for r in self.store.search(dict(params,page=page))['rows'])
        self.assertEqual(len(ids),160)
        self.assertEqual(len(set(ids)),160)
        exported=[json.loads(line) for line in self.store.export(params)]
        self.assertEqual(len(exported),160)
        self.assertEqual([r['id'] for r in exported],ids)

    def test_no_fts_fallback(self):
        original=self.store.fts
        try:
            self.store.fts=False
            self.assertEqual(self.search(q='gzip-history-hit')['summary']['total'],2)
        finally:
            self.store.fts=original

    def test_exact_file_scope(self):
        file=self.store.filters(self.identifier)[0]
        result=self.search(file_id=file['id'])
        self.assertEqual(result['summary']['files'],1)
        self.assertTrue(all(r['file_id']==file['id'] for r in result['rows']))


class FailureTests(unittest.TestCase):
    def test_failed_import_rolls_back_and_reports_error(self):
        with tempfile.TemporaryDirectory() as temp:
            store=Store(Path(temp)/'data')
            archive=Path(temp)/'bad.zip'
            with zipfile.ZipFile(archive,'w') as z:
                z.writestr('ns_pod/service/pod-service/log/root.log',root_line())
                z.writestr('ns_pod/service/pod-service/log/root.log.gz',b'not gzip')
            identifier=store.submit(archive,'bad.zip'); store.pool.shutdown(wait=True)
            dataset=store.datasets()[0]
            self.assertEqual(dataset['state'],'failed')
            self.assertTrue(dataset['error'])
            with store.connect() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM logs').fetchone()[0],0)
                if store.fts:self.assertEqual(db.execute('SELECT count(*) FROM log_fts').fetchone()[0],0)
            with self.assertRaises(ValueError):store.search({'dataset':identifier})

    def test_gbk_decoding_and_archive_paths_not_extracted(self):
        with tempfile.TemporaryDirectory() as temp:
            store=Store(Path(temp)/'data'); archive=Path(temp)/'gbk.zip'
            with zipfile.ZipFile(archive,'w') as z:
                z.writestr('../../escape.txt','should not extract')
                z.writestr('ns_pod/service/pod-service/log/root.log',root_line(message='中文连接失败').encode('gb18030'))
            identifier=store.submit(archive,'gbk.zip');store.pool.shutdown(wait=True)
            self.assertEqual(store.search({'dataset':identifier,'q':'中文连接失败'})['summary']['total'],1)
            self.assertFalse((Path(temp)/'escape.txt').exists())

    def test_http_upload_api_and_local_only_guard(self):
        with tempfile.TemporaryDirectory() as temp:
            server=make_server(Path(temp)/'data',0)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            base='http://127.0.0.1:'+str(server.server_address[1])
            try:
                archive=create_demo(Path(temp)/'demo.zip')
                request=Request(base+'/api/upload?name=test.zip',data=archive.read_bytes(),headers={'Content-Type':'application/zip'},method='POST')
                with urlopen(request) as response:
                    self.assertEqual(response.status,202);identifier=json.load(response)['id']
                server.store.pool.shutdown(wait=True)
                with urlopen(base+'/api/search?dataset='+identifier+'&q=gzip-history-hit') as response:
                    self.assertEqual(json.load(response)['summary']['total'],2)
                with urlopen(base+'/') as response:
                    home=response.read().decode()
                    self.assertIn('LogScope',home)
                    self.assertIn('在线采集日志',home)
                    self.assertNotIn('API 问诊',home)
                    self.assertIn("default-src 'self'",response.headers['Content-Security-Policy'])
                with self.assertRaises(HTTPError) as error:
                    urlopen(Request(base+'/api/datasets',headers={'Origin':'https://evil.invalid'}))
                self.assertEqual(error.exception.code,403)
                with self.assertRaises(HTTPError) as error:
                    urlopen(Request(base+'/api/datasets',headers={'Host':'evil.invalid'}))
                self.assertEqual(error.exception.code,403)
                with self.assertRaises(HTTPError) as error:
                    urlopen(Request(base+'/api/ai/analyze',data=b'{}',headers={'Content-Type':'application/json'}))
                self.assertEqual(error.exception.code,404)
            finally:
                server.shutdown();server.server_close();thread.join()
                server.store.pool.shutdown(wait=True)


class CollectorTests(unittest.TestCase):
    def test_collect_script_downloads_zip_and_imports_it(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);script=root/'collect_logs.py'
            script.write_text("""import argparse, io, json, pathlib, zipfile
p=argparse.ArgumentParser()
for name in ('pod','start','end','output','url','user','password','headless','timeout','poll'): p.add_argument('--'+name)
a=p.parse_args();out=pathlib.Path(a.output);out.mkdir(parents=True,exist_ok=True)
(pathlib.Path(__file__).parent/'received.json').write_text(json.dumps(vars(a),ensure_ascii=False),'utf-8')
inner=io.BytesIO()
with zipfile.ZipFile(inner,'w') as z: z.writestr('ns_order-pod/order/order-pod-order/log/root.log','[2026-09-10 14:30:00.000 +0800] [1234567890123456789] [1234567890123456789] [ERROR] [worker-1] [Order.java] [com.example] [run] [42] COLLECTED_MARKER')
with zipfile.ZipFile(out/'collected.zip','w') as z: z.writestr('node-a.zip',inner.getvalue())
print('download complete password=' + str(a.password),flush=True)
""",'utf-8')
            server=make_server(root/'data',0,collect_script=script)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            base='http://127.0.0.1:'+str(server.server_address[1])
            def api(path,body=None):
                request=Request(base+path,data=json.dumps(body).encode() if body is not None else None,
                                headers={'Content-Type':'application/json'})
                with urlopen(request,timeout=20) as response:return json.load(response)
            try:
                self.assertTrue(api('/api/collector/capability')['available'])
                job=api('/api/collector/start',dict(pod='order;touch hacked',start='2026-09-10 14:00:00',
                                                     end='2026-09-10 16:30:00',password='test secret'))
                deadline=time.monotonic()+15
                while time.monotonic()<deadline:
                    job=api('/api/collector/status?id='+job['id'])
                    if job['state'] in ('ready','failed'):break
                    time.sleep(.05)
                self.assertEqual(job['state'],'ready',job['message'])
                self.assertNotIn('test secret',job['output'])
                self.assertIn('[REDACTED]',job['output'])
                result=api('/api/search?dataset='+job['dataset_id']+'&q=COLLECTED_MARKER')
                self.assertEqual(result['summary']['total'],1)
                received=json.loads((root/'received.json').read_text('utf-8'))
                self.assertEqual(received['pod'],'order;touch hacked')
                self.assertEqual(received['start'],'2026-09-10 14:00:00')
                self.assertFalse((root/'hacked').exists())
            finally:
                server.shutdown();server.server_close();thread.join();server.store.pool.shutdown(wait=True)


if __name__=='__main__':unittest.main()
