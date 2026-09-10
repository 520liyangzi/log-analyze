#!/usr/bin/env python3
"""Read-only Git code query CLI for a LogScope code investigation task."""
import argparse
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys


def task():
    path = Path.cwd() / 'code-task.json'
    if not path.exists():
        raise ValueError('当前目录没有 code-task.json，请先从页面创建代码定位任务')
    return json.loads(path.read_text('utf-8'))


def git(config, *args, timeout=60, allow_nomatch=False):
    result = subprocess.run(['git', '-C', config['project_root'], *args], capture_output=True,
                            text=True, encoding='utf-8', errors='replace', timeout=timeout, check=False)
    if result.returncode and not (allow_nomatch and result.returncode == 1):
        raise ValueError((result.stderr or result.stdout or 'Git 查询失败').strip())
    return result.stdout


def safe_path(value):
    value = value.replace('\\', '/').strip('/')
    path = PurePosixPath(value)
    if not value or path.is_absolute() or '..' in path.parts:
        raise ValueError('文件路径必须是项目内的相对路径')
    return str(path)


def main(argv=None):
    config = task()
    parser = argparse.ArgumentParser(description='LogScope 只读代码定位工具')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('info', help='显示项目、分支和固定提交')
    tree = sub.add_parser('tree', help='列出分支中的文件')
    tree.add_argument('--path', default='')
    tree.add_argument('--max', type=int, default=500)
    grep = sub.add_parser('grep', help='在指定分支中搜索代码')
    grep.add_argument('pattern')
    grep.add_argument('--path', default='')
    grep.add_argument('--ignore-case', action='store_true')
    grep.add_argument('--max', type=int, default=200)
    show = sub.add_parser('show', help='读取指定分支中的文件片段')
    show.add_argument('path')
    show.add_argument('--start', type=int, default=1)
    show.add_argument('--end', type=int, default=240)
    log = sub.add_parser('log', help='查看固定提交前的历史')
    log.add_argument('--max', type=int, default=20)
    args = parser.parse_args(argv)
    revision = config['commit']
    if args.command == 'info':
        result = {k: config[k] for k in ('project_root', 'branch', 'commit')}
    elif args.command == 'tree':
        maximum = min(5000, max(1, args.max))
        command = ['ls-tree', '-r', '--name-only', revision]
        if args.path:
            command.extend(['--', safe_path(args.path)])
        files = git(config, *command).splitlines()
        result = {'files': files[:maximum], 'returned': min(len(files), maximum),
                  'truncated': len(files) > maximum}
    elif args.command == 'grep':
        if not args.pattern or len(args.pattern) > 1000:
            raise ValueError('搜索词不能为空且最多 1000 字符')
        maximum = min(2000, max(1, args.max))
        command = ['grep', '-n', '-I', '--full-name']
        if args.ignore_case:
            command.append('-i')
        command.extend(['-e', args.pattern, revision])
        if args.path:
            command.extend(['--', safe_path(args.path)])
        lines = git(config, *command, allow_nomatch=True).splitlines()
        result = {'matches': lines[:maximum], 'returned': min(len(lines), maximum),
                  'truncated': len(lines) > maximum}
    elif args.command == 'show':
        path = safe_path(args.path)
        start, end = max(1, args.start), max(1, args.end)
        if end < start or end - start > 1000:
            raise ValueError('单次最多读取 1001 行代码')
        content = git(config, 'show', revision + ':' + path)
        if len(content.encode('utf-8')) > 4 * 1024 * 1024:
            raise ValueError('文件超过 4 MB，请缩小到具体文本文件')
        lines = content.splitlines()
        result = {'path': path, 'start': start, 'end': min(end, len(lines)),
                  'content': '\n'.join(lines[start - 1:end]), 'total_lines': len(lines)}
    else:
        maximum = min(100, max(1, args.max))
        lines = git(config, 'log', '--format=%H %ad %s', '--date=iso-strict', '-' + str(maximum), revision).splitlines()
        result = {'commits': lines}
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
