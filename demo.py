"""Generate synthetic logs matching the user's archive structure; no real log data."""
import argparse
import gzip
import io
from pathlib import Path
import zipfile

TRACE = '9124859898865451127'


def root_line(time='09:55:14.160', trace=TRACE, level='INFO', thread='http-nio-uds-exec-7', message='开始加载 model map'):
    return f'[2026-09-08 {time} +0800] [{trace}] [{trace}] [{level}] [{thread}] [ModelService.java] [com.example.model] [Map] [125] {message}'


def create_demo(target):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    listing = []
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as outer:
        for node, pod in [('node-a','model-service-7b8d9-x2k4m'),('node-b','model-service-7b8d9-p9q6r')]:
            inner_data = io.BytesIO()
            prefix = f'prod_{pod}/modelservice/{pod}-modelservice/log/'
            with zipfile.ZipFile(inner_data, 'w', zipfile.ZIP_DEFLATED) as inner:
                access = []
                roots = []
                for i in range(80):
                    trace = TRACE if i == 14 else str(9124859898865400000 + i + (1000 if node=='node-b' else 0))
                    second = i % 60
                    minute = 55 + i // 60
                    time = f'09:{minute:02}:{second:02}'
                    failed = i == 14 and node == 'node-a'
                    status = 500 if failed else 200
                    elapsed = 3051 if failed else (i % 28 + 3)
                    access.append(f'{"2026-09-08"} {time},158 INFO  162 [http-nio-uds-exec-7][ROOT][][c.h.c.t.a.l.AccessLogValveExt 28] \\"GET /api/model/map HTTP/1.1\\" {status} 14 - {elapsed}')
                    roots.append(root_line(time+'.160', trace, message='开始请求 /api/model/map'))
                    if failed:
                        roots.append(root_line(time+'.186', trace, 'ERROR', message='模型缓存加载失败：连接池获取连接超时'))
                        roots.extend(['java.sql.SQLTransientConnectionException: HikariPool - Connection is not available, request timed out after 3000ms.', '\tat com.example.model.ModelService.load(ModelService.java:125)', 'Caused by: java.net.SocketTimeoutException: Read timed out'])
                    else:
                        roots.append(root_line(time+'.186', trace, message='Successfully loaded model map,count: 5'))
                files = {'access.log':'\n'.join(access)+'\n', 'root.log':'\n'.join(roots)+'\n', 'run.log':root_line('09:55:14.180',message='执行请求：准备访问模型缓存')+'\n'}
                for filename, content in files.items():
                    inner.writestr(prefix + filename, content.encode('utf-8'))
                    listing.append(node+'.zip/'+prefix+filename)
                filename='root.2026-09-07.log.gz'
                inner.writestr(prefix+filename,gzip.compress((root_line('08:00:00.000',trace='history-only',message='历史压缩日志 gzip-history-hit')+'\n').encode()))
                listing.append(node+'.zip/'+prefix+filename)
            outer.writestr(node+'.zip',inner_data.getvalue())
        outer.writestr('fileList.txt','\n'.join(listing))
    return target


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',default='demo-logs.zip')
    args=parser.parse_args()
    print(create_demo(args.output).resolve())
