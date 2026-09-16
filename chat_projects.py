"""Explicit Git synchronization and bounded, immutable code queries."""
import concurrent.futures
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
import threading
import uuid

from project_access import inspect_repository, select_revision


def run_git(root, *args, limit=160000, timeout=60, allow_nomatch=False):
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='never')
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error:
        try:
            result = subprocess.run(['git', '--no-pager', '-C', str(root), *args], stdout=output,
                                    stderr=error, env=env, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError('Git 操作失败或超时，请在服务电脑上检查 Git、网络和仓库凭据。') from None
        if result.returncode and not (allow_nomatch and result.returncode == 1):
            raise ValueError('Git 操作失败，请在服务电脑检查远程地址、分支和凭据；未改动工作区。')
        output.seek(0)
        raw = output.read(limit + 1)
        return raw[:limit].decode('utf-8', errors='replace'), len(raw) > limit


def relative_path(value, empty=False):
    value = str(value).replace('\\', '/')
    path = PurePosixPath(value)
    if (not value and not empty) or path.is_absolute() or '..' in path.parts or ':' in value or '\x00' in value:
        raise ValueError('只能读取当前项目中的相对路径')
    if any(part.lower() in ('.git', '.ssh', 'data') for part in path.parts):
        raise ValueError('不能读取运行数据或凭据目录')
    if path.name.lower() in ('ai-config.json', 'collector-environments.json') or path.name.startswith('.env') or path.suffix.lower() in ('.key', '.pem', '.p12'):
        raise ValueError('不能读取凭据配置文件')
    return value


class ChatProjects:
    def __init__(self):
        self.lock = threading.RLock()
        self.repo_locks = {}
        self.jobs = {}
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)

    def sync(self, body):
        path = str(body.get('path', '')).strip()
        if not path or not Path(path).is_absolute() or '\x00' in path or len(path) > 4096:
            raise ValueError('请填写服务电脑上的 Git 项目绝对路径')
        remote = str(body.get('remote_url', '')).strip()
        if remote and (len(remote) > 2000 or not re.match(r'^(https?://|ssh://|git@[\w.-]+:)', remote) or any(c in remote for c in '\r\n\x00')):
            raise ValueError('远程地址应为 HTTP(S) 或 SSH Git 仓库地址')
        identifier = uuid.uuid4().hex
        with self.lock:
            if sum(j['state'] == 'running' for j in self.jobs.values()) >= 4:
                raise ValueError('已有项目正在同步，请稍后重试')
            self.jobs[identifier] = dict(id=identifier, state='running', message='正在同步远程分支，不切换工作区…')
        self.pool.submit(self._sync, identifier, Path(path), remote)
        return self.status(identifier)

    def _sync(self, identifier, path, remote):
        key = os.path.normcase(str(path.resolve()))
        with self.lock:
            guard = self.repo_locks.setdefault(key, threading.Lock())
        try:
            with guard:
                if not path.exists():
                    if not remote:
                        raise ValueError('项目目录不存在：填写远程地址后可克隆，或先在服务电脑准备仓库')
                    path.parent.mkdir(parents=True, exist_ok=True)
                    run_git(path.parent, 'clone', '--no-checkout', '--', remote, str(path), timeout=120)
                repository = inspect_repository(str(path))
                configured, _ = run_git(path, 'remote', 'get-url', 'origin')
                if remote and remote.rstrip('/') != configured.strip().rstrip('/'):
                    raise ValueError('填写的地址与现有 origin 不一致；请在服务电脑修改配置，不会自动替换远程地址')
                run_git(path, 'fetch', '--no-tags', 'origin', timeout=120)
                repository = inspect_repository(str(path))
                result = dict(id=identifier, state='ready', message='同步完成；请选择与日志部署版本对应的分支', **repository)
        except Exception:
            result = dict(id=identifier, state='failed', message='项目同步失败。请在服务电脑检查目录、origin、网络与 Git 登录；未切换分支，不会静默使用旧代码。')
        with self.lock:
            self.jobs[identifier] = result
            # Completed sync tasks are transient, unlike saved investigations.
            if len(self.jobs) > 100:
                for old in list(self.jobs)[:-50]:
                    if self.jobs[old]['state'] != 'running':
                        self.jobs.pop(old)

    def status(self, identifier):
        with self.lock:
            if identifier not in self.jobs:
                raise ValueError('项目同步记录已过期，请重新同步')
            return dict(self.jobs[identifier])

    def snapshot(self, body):
        job = self.status(str(body.get('sync_id', '')))
        if job['state'] != 'ready':
            raise ValueError('请先完成项目同步')
        branch = str(body.get('branch', ''))
        selected = select_revision(job['root'], branch)
        return dict(project_root=selected['root'], branch=branch, commit=selected['commit'])

    def close(self):
        self.pool.shutdown(wait=False, cancel_futures=True)


def query_project(project, name, args):
    if not project:
        raise ValueError('当前会话未选择项目；请在页面启用代码分析并预览确认')
    root, revision = project['project_root'], project['commit']
    if name == 'project_search':
        keyword = str(args.get('keyword', ''))
        if not keyword or len(keyword) > 500:
            raise ValueError('代码搜索词长度需为 1～500 个字符')
        path = relative_path(args.get('path', ''), empty=True)
        exclusions = [':(exclude)**/.env*', ':(exclude)**/*.pem', ':(exclude)**/*.key',
                      ':(exclude)**/ai-config.json', ':(exclude)**/data/**']
        output, clipped = run_git(root, 'grep', '-F', '-n', '-I', '--no-textconv', '-m', '20',
                                  '-e', keyword, revision, '--', path or '.', *exclusions, allow_nomatch=True)
        matches = []
        for line in output.splitlines():
            parts = line.split(':', 3)
            if len(parts) == 4 and parts[2].isdigit():
                try:
                    relative_path(parts[1])
                except ValueError:
                    continue
                matches.append(dict(path=parts[1], line=int(parts[2]), text=parts[3][:1500]))
        return dict(commit=revision, matches=matches[:60], truncated=clipped or len(matches) > 60,
                    note='每文件最多 20 条，全局最多 60 条；需要时缩小路径继续搜索')
    path = relative_path(args.get('path', ''))
    start, end = int(args.get('start', 1)), int(args.get('end', 200))
    if start < 1 or end < start or end - start >= 300:
        raise ValueError('单次可读取 1～300 行代码')
    output, clipped = run_git(root, 'show', revision + ':' + path, limit=4 * 1024 * 1024)
    if clipped:
        raise ValueError('文件超过 4 MB，拒绝读取')
    lines = output.splitlines()
    return dict(commit=revision, path=path, start=start, end=min(end, len(lines)), total_lines=len(lines),
                content='\n'.join(f'{i + 1}: {lines[i]}' for i in range(start - 1, min(end, len(lines)))))
