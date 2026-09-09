#!/usr/bin/env python3
"""Read-only LogScope CLI. Self-contained, Python standard library only."""
import argparse
import json
import os
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import urlopen


def defaults():
    path = Path.cwd() / 'task.json'
    data = json.loads(path.read_text('utf-8')) if path.exists() else {}
    return os.getenv('LOGSCOPE_URL', data.get('url', 'http://127.0.0.1:8765')), os.getenv('LOGSCOPE_DATASET_ID', data.get('dataset', ''))


def request(base, route, params):
    parsed = urlsplit(base)
    if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost') or parsed.username:
        raise ValueError('只允许连接本机 LogScope HTTP 服务')
    url = base.rstrip('/') + route + '?' + urlencode({k: v for k, v in params.items() if v not in ('', None, False)})
    try:
        return urlopen(url, timeout=300)
    except HTTPError as error:
        payload = json.load(error)
        raise ValueError(payload.get('error', str(error))) from None
    except URLError as error:
        raise ValueError('无法连接 LogScope，请先启动 app.py 并检查 --url 端口：' + str(error.reason)) from None


def main(argv=None):
    base, dataset = defaults()
    parser = argparse.ArgumentParser(description='LogScope 日志检索 / 来源核验（只读）')
    parser.add_argument('--url', default=base, help='本机服务 URL，也可设置 LOGSCOPE_URL')
    parser.add_argument('--dataset', default=dataset, help='数据集 ID，也可设置 LOGSCOPE_DATASET_ID')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('datasets', help='列出导入的日志包及状态')
    sub.add_parser('files', help='列出所有 Node / Pod / 日志文件及来源')
    for name in ('search', 'export'):
        command = sub.add_parser(name, help='查询日志' if name == 'search' else '全量导出 NDJSON')
        for key in ('q', 'node', 'namespace', 'pod', 'service', 'kind', 'filename', 'thread', 'level', 'start', 'end', 'status', 'min-duration', 'file-id', 'thread-id', 'route-id', 'request-id', 'request-key', 'endpoint'):
            command.add_argument('--' + key, default='')
        command.add_argument('--case', action='store_true', help='区分大小写')
        command.add_argument('--scan', action='store_true', help='跳过 FTS，加扫索引内全部原文；不是直接扫描压缩包')
        command.add_argument('--access-only', action='store_true', help='仅已解析 HTTP 请求的记录')
        command.add_argument('--order', choices=('time','errors_slow'), default='time')
        if name == 'search':
            command.add_argument('--page', type=int, default=1)
            command.add_argument('--size', type=int, default=50)
        else:
            command.add_argument('--output', type=Path, required=True, help='输出 NDJSON 文件路径')
    trace = sub.add_parser('trace', help='完整流水号精确匹配')
    trace.add_argument('trace_id')
    trace.add_argument('--page', type=int, default=1)
    trace.add_argument('--size', type=int, default=50)
    for name in ('record', 'context', 'verify', 'correlate'):
        command = sub.add_parser(name)
        command.add_argument('id', type=int)
        if name == 'context':
            command.add_argument('--radius', type=int, default=15)
        if name == 'correlate':
            command.add_argument('--seconds', type=float, default=5)
            command.add_argument('--same-thread', action='store_true')
            command.add_argument('--kind', default='')
            command.add_argument('--page', type=int, default=1)
            command.add_argument('--size', type=int, default=50)
    args = vars(parser.parse_args(argv))
    base = args.pop('url')
    name = args.pop('command')
    output = args.pop('output', None)
    if name in ('search', 'export', 'trace', 'files') and not args.get('dataset'):
        parser.error('请先执行 datasets，再通过命令名前的 --dataset 指定目标日志包')
    for key, value in list(args.items()):
        if isinstance(value, bool):
            args[key] = '1' if value else ''
    if name == 'trace':
        args['trace'] = args.pop('trace_id')
    route = '/api/search' if name == 'trace' else '/api/' + name
    if name == 'export':
        output.parent.mkdir(parents=True, exist_ok=True)
        # Avoid replacing an earlier evidence export accidentally.
        with request(base, route, args) as response, output.open('xb') as file:
            while chunk := response.read(1024 * 1024):
                file.write(chunk)
        result = {'output': str(output.resolve()), 'format': 'ndjson', 'all_matches': True}
    else:
        with request(base, route, args) as response:
            result = json.load(response)
        if name in ('search','trace','correlate'):
            total, page, size = result['summary']['total'], result['page'], result['size']
            result.update(returned=len(result['rows']), has_more=page * size < total,
                          next_page=page + 1 if page * size < total else None,
                          query=args, coverage='本页不是全量证据，检查 has_more 与 summary.total。')
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    try:
        raise SystemExit(main())
    except (ValueError, OSError, json.JSONDecodeError) as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
