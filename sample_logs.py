"""Generate a fully synthetic archive shaped like the supplied sanitized sample."""
import argparse
import gzip
import io
from pathlib import Path
import zipfile

TRACE_CHAT = '9125008448899317884'
TRACE_MODEL = '9124403017424371820'
ROUTE_CHAT = 'route-synthetic-chat-20260908'


def root(time, trace, level, thread, message, line=125):
    return (f'[{time} +0800] [{trace}] [{trace}] [{level}] [{thread}] '
            f'[SyntheticService.java] [com.example.synthetic] [handle] [{line}] {message}')


def access(time, thread_id, thread, method, url, status, size, route, duration):
    return (f'{time} INFO  {thread_id} [{thread}][ROOT][][com.example.AccessLogValve 28] '
            f'\\"{method} {url} HTTP/1.1\\" {status} {size} {route} {duration}')


def create_sample(target):
    """Create two Node ZIPs, four log types, rotations, traces and failures."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    manifest = []
    nodes = [
        ('vnf-demo-1-node01-1788749694', 'ns_alpha', 'pod-alpha-aaa111', 'ServiceA', 'http-nio-uds-exec-9'),
        ('vnf-demo-1-node02-1788749694', 'ns_beta', 'pod-beta-bbb222', 'ServiceB', 'http-nio-uds-exec-6'),
    ]
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as outer:
        for index, (node, namespace, pod, service, thread) in enumerate(nodes):
            prefix = f'{namespace}_{pod}/{service}/{pod}-{service}/log/'
            node_zip = io.BytesIO()
            chat_duration = 3155 if index == 0 else 48
            chat_access = access('2026-09-08 09:29:39,885', '162', thread, 'POST',
                                 '/api/rest/example/v2/query/chat-task?scene=demo', 200, 872,
                                 ROUTE_CHAT + f'-node{index + 1}', chat_duration)
            model_status = 500 if index == 0 else 200
            model_access = access('2026-09-08 09:55:14,158', '163', thread, 'GET',
                                  '/api/model/map', model_status, 14, '-', 3012 if index == 0 else 14)
            root_lines = [
                root('2026-09-08 09:29:37.522', TRACE_CHAT, 'INFO', thread,
                     'request accepted, endpoint:/api/rest/example/v2/query/chat-task'),
                root('2026-09-08 09:29:38.104', TRACE_CHAT, 'WARN' if index == 0 else 'INFO', thread,
                     'downstream response is slow, RequestId:request-synthetic-outbound'),
                root('2026-09-08 09:29:39.860', TRACE_CHAT, 'INFO', thread, 'chat task finished'),
                root('2026-09-08 09:55:14.160', TRACE_MODEL, 'INFO', thread, 'loading model map'),
            ]
            if index == 0:
                root_lines += [
                    root('2026-09-08 09:55:17.170', TRACE_MODEL, 'ERROR', thread,
                         'model map load failed: connection pool timeout', 208),
                    'java.sql.SQLTransientConnectionException: synthetic pool timeout after 3000ms',
                    '\tat com.example.synthetic.SyntheticService.handle(SyntheticService.java:208)',
                    'Caused by: java.net.SocketTimeoutException: synthetic read timed out',
                ]
            else:
                root_lines.append(root('2026-09-08 09:55:14.171', TRACE_MODEL, 'INFO', thread,
                                       'Successfully loaded model map,count: 5'))
            rest_lines = [
                root('2026-09-08 09:29:38.204', TRACE_CHAT, 'INFO', thread,
                     'POST synthetic downstream /chat-task status=200', 66),
                root('2026-09-08 09:29:39.458', '', 'ERROR' if index == 0 else 'INFO',
                     f'qtp-synthetic-{index + 4}',
                     'asynchronous callback failed: synthetic timeout' if index == 0 else 'asynchronous callback completed', 72),
            ]
            wsf_lines = [
                f'2026-09-08 09:29:37,500 INFO 162 [{thread}][ROOT][][com.example.Filter 367] [WSF-ParamValidate] request parameters accepted',
                f'2026-09-08 09:55:14,150 INFO 163 [{thread}][ROOT][][com.example.Filter 367] [WSF-Route] route /api/model/map',
            ]
            plain = {
                'access.log': chat_access + '\n' + model_access + '\n',
                'root.log': '\n'.join(root_lines) + '\n',
                'rest.log': '\n'.join(rest_lines) + '\n',
                'wsf.log': '\n'.join(wsf_lines) + '\n',
            }
            rotated = {
                'access_20260908080000123+0800.log.gz': access(
                    '2026-09-08 08:01:02,003', '151', 'http-history-1', 'GET', '/api/history/ping',
                    200, 2, 'route-synthetic-history', 3) + '\n',
                'root_20260908080001313+0800.log.gz': root(
                    '2026-09-08 08:01:02.010', '9124000000000000001', 'INFO', 'http-history-1',
                    'synthetic history root marker') + '\n',
                'rest_20260908080003490+0800.log.gz': root(
                    '2026-09-08 08:01:02.020', '9124000000000000001', 'INFO', 'http-history-1',
                    'synthetic history rest marker') + '\n',
                'wsf_20260908080005678+0800.log.gz': (
                    '2026-09-08 08:01:02,001 INFO 151 [http-history-1][ROOT][]'
                    '[com.example.Filter 367] [WSF-History] synthetic gzip history marker\n'),
            }
            with zipfile.ZipFile(node_zip, 'w', zipfile.ZIP_DEFLATED) as inner:
                for filename, content in plain.items():
                    inner.writestr(prefix + filename, content.encode('utf-8'))
                    manifest.append(f'{node}.zip/{prefix}{filename}')
                for filename, content in rotated.items():
                    inner.writestr(prefix + filename, gzip.compress(content.encode('utf-8')))
                    manifest.append(f'{node}.zip/{prefix}{filename}')
            outer.writestr(node + '.zip', node_zip.getvalue())
        outer.writestr('fileList.txt', '\n'.join(manifest) + '\n')
        outer.writestr('README.txt', '全部为程序生成的虚构脱敏日志，不包含真实业务数据。\n')
    return target


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='生成 LogScope 脱敏格式测试日志包')
    parser.add_argument('--output', default='generated-log-sample.zip')
    args = parser.parse_args()
    print(create_sample(args.output).resolve())
