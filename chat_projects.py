"""Explicit Git synchronization and bounded, immutable code queries."""
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
import threading
from urllib.parse import urlsplit
import uuid

from analysis_rules import atomic_json


def run_git(root, *args, limit=160000, timeout=60, allow_nomatch=False):
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='never')
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error:
        try:
            result = subprocess.run(['git', '--no-pager', '-C', str(root), *args], stdout=output,
                                    stderr=error, stdin=subprocess.DEVNULL, env=env, timeout=timeout, check=False)
        except FileNotFoundError:
            raise ValueError('服务电脑没有找到 git 命令，请安装 Git 并确认 PATH 配置') from None
        except subprocess.TimeoutExpired:
            raise ValueError('Git 操作超时，请检查服务电脑的网络、VPN 和仓库登录状态后重试') from None
        except OSError:
            raise ValueError('无法启动 Git，请检查服务电脑的 Git 安装与目录权限') from None
        if result.returncode and not (allow_nomatch and result.returncode == 1):
            error.seek(0)
            detail = error.read(32000).decode('utf-8', errors='replace').lower()
            if any(token in detail for token in ('authentication failed', 'could not read username',
                                                  'permission denied', 'repository not found',
                                                  'could not read from remote repository')):
                message = 'Git 远程仓库不可访问，请检查拉取链接及服务电脑上的 Git 登录和仓库权限'
            elif any(token in detail for token in ('could not resolve', 'failed to connect', 'connection refused',
                                                    'connection timed out', 'network is unreachable')):
                message = '无法连接 Git 远程仓库，请检查服务电脑的 DNS、网络、VPN 和代理配置'
            elif 'certificate' in detail or 'ssl' in detail:
                message = 'Git TLS 校验失败，请检查服务电脑信任的证书和仓库 HTTPS 配置'
            else:
                message = 'Git 操作失败，请检查远程仓库、分支和目录权限'
            raise ValueError(message + '；原始 Git 输出不回显')
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


def git_remote(value):
    """Validate transport URLs before hashing, persisting, or displaying them."""
    if not isinstance(value, str):
        raise ValueError('请填写 HTTP(S)、SSH 或 SCP 格式的 Git 拉取链接')
    remote = value.strip().rstrip('/')
    if (not remote or len(remote) > 2000 or remote.startswith('-')
            or any(char.isspace() or not char.isprintable() for char in remote)
            or '\\' in remote or re.match(r'^[A-Za-z]:', remote)):
        raise ValueError('请填写有效的 Git 拉取链接，不支持本地路径或命令参数')
    if '?' in remote or '#' in remote:
        raise ValueError('Git 拉取链接不能包含查询参数或片段；请使用服务电脑上的 Git 凭据管理')
    if '://' in remote:
        try:
            parsed = urlsplit(remote)
            port = parsed.port
        except ValueError:
            raise ValueError('Git 拉取链接的主机或端口格式无效') from None
        if parsed.scheme not in ('http', 'https', 'ssh') or not parsed.hostname or parsed.hostname.startswith('-'):
            raise ValueError('Git 拉取链接仅支持 HTTP(S)、SSH 或 SCP 格式')
        try:
            hostname = parsed.hostname.encode('idna').decode('ascii')
        except UnicodeError:
            raise ValueError('Git 拉取链接的主机名格式无效') from None
        if not re.fullmatch(r'[A-Za-z0-9_.:-]+', hostname):
            raise ValueError('Git 拉取链接的主机名格式无效')
        if port is not None and not 1 <= port <= 65535:
            raise ValueError('Git 拉取链接的端口必须在 1–65535 之间')
        if parsed.password is not None or (parsed.scheme in ('http', 'https') and parsed.username is not None):
            raise ValueError('Git 拉取链接不能内嵌用户名密码或 token；请使用服务电脑上的 Git 凭据管理')
        if parsed.username is not None and not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', parsed.username):
            raise ValueError('SSH 用户名格式无效；拉取链接中不能包含密码或 token')
        path = parsed.path
    else:
        match = re.fullmatch(r'(?:[A-Za-z0-9_][A-Za-z0-9_.-]*@)?'
                             r'(?:[A-Za-z0-9][A-Za-z0-9.-]*|\[[0-9A-Fa-f:]+\]):(.+)', remote)
        if not match:
            raise ValueError('请填写 HTTP(S)、SSH 或 SCP 格式的 Git 拉取链接，不支持本地路径')
        path = match.group(1)
    if not path or path == '/' or path.startswith(('-', ':')):
        raise ValueError('Git 拉取链接必须包含仓库路径')
    name = path.rsplit('/', 1)[-1]
    if name.lower().endswith('.git'):
        name = name[:-4]
    name = re.sub(r'[^A-Za-z0-9._-]+', '-', name).strip('._-')[:64] or 'repository'
    identifier = name + '-' + hashlib.sha256(remote.encode('utf-8')).hexdigest()[:12]
    return remote, name, identifier


class ChatProjects:
    def __init__(self, data_directory):
        self.storage = (Path(data_directory).resolve() / 'projects').resolve()
        self.storage.mkdir(parents=True, exist_ok=True)
        self.registry_path = self.storage / 'repositories.json'
        self.lock = threading.RLock()
        self.repo_locks = {}
        self.jobs = {}
        self.latest_jobs = {}
        self.registry = {}
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        try:
            saved = json.loads(self.registry_path.read_text('utf-8'))
            for entry in saved.get('repositories', []):
                remote, name, identifier = git_remote(entry.get('remote_url'))
                root = self.storage / identifier
                if entry.get('id') == identifier and entry.get('root') == str(root):
                    self.registry[identifier] = dict(id=identifier, name=name, remote_url=remote,
                                                     root=str(root), updated_at=str(entry.get('updated_at', '')))
        except (OSError, ValueError, TypeError, AttributeError):
            # A missing/broken index never authorizes a different local path.
            # Explicitly syncing a known URL can rediscover its managed clone.
            self.registry = {}

    def repositories(self):
        with self.lock:
            entries = [dict(entry) for entry in self.registry.values()]
        return dict(storage_path=str(self.storage),
                    repositories=sorted(entries, key=lambda entry: entry['updated_at'], reverse=True))

    def sync(self, body):
        if body.get('path'):
            raise ValueError('项目由程序管理，请仅填写 Git 拉取链接，不再接受本地项目路径')
        remote, name, repository_id = git_remote(body.get('remote_url', ''))
        path = self.storage / repository_id
        identifier = uuid.uuid4().hex
        with self.lock:
            if sum(j['state'] == 'running' for j in self.jobs.values()) >= 4:
                raise ValueError('已有项目正在同步，请稍后重试')
            self.latest_jobs[repository_id] = identifier
            self.repo_locks.setdefault(repository_id, threading.Lock())
            self.jobs[identifier] = dict(id=identifier, state='running', root=str(path), remote_url=remote,
                                         repository_id=repository_id, branches=[], current='', updated_at='',
                                         message='正在准备仓库同步…')
        self.pool.submit(self._sync, identifier, path, remote, name, repository_id)
        return self.status(identifier)

    def _message(self, identifier, message):
        with self.lock:
            self.jobs[identifier]['message'] = message

    @staticmethod
    def _check_clone(path, remote):
        if path.is_symlink() or path.resolve() != path or not (path / '.git').is_dir() or (path / '.git').is_symlink():
            raise ValueError('托管仓库目录异常，请维护者检查 data/projects；不会操作其他本地仓库')
        configured, _ = run_git(path, 'config', '--get', 'remote.origin.url')
        if configured.strip() != remote:
            raise ValueError('托管仓库的 origin 与拉取链接不一致，请维护者检查；不会替换远程地址')

    @staticmethod
    def _branches(path):
        refs, _ = run_git(path, 'for-each-ref', '--format=%(refname)', 'refs/remotes/origin/', limit=1024 * 1024)
        branches = [ref[len('refs/remotes/'):] for ref in refs.splitlines()
                    if ref.startswith('refs/remotes/origin/') and ref != 'refs/remotes/origin/HEAD']
        if not branches:
            raise ValueError('远程仓库没有可读取的分支，请先向仓库推送代码')
        head, _ = run_git(path, 'ls-remote', '--symref', 'origin', 'HEAD', timeout=120)
        current = ''
        for line in head.splitlines():
            if line.startswith('ref: refs/heads/') and line.endswith('\tHEAD'):
                selected = 'origin/' + line[len('ref: refs/heads/'):].split('\t', 1)[0]
                if selected in branches:
                    current = selected
                    run_git(path, 'symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/' + selected)
                    break
        current = current or branches[0]
        branches.remove(current)
        return [current, *branches][:1000], current

    def _sync(self, identifier, path, remote, name, repository_id):
        guard = self.repo_locks[repository_id]
        staging = None
        try:
            with guard:
                if not path.exists():
                    self._message(identifier, '首次克隆到 data/projects，正在下载仓库…')
                    staging = Path(tempfile.mkdtemp(prefix='.clone-', dir=self.storage))
                    run_git(self.storage, 'clone', '--no-checkout', '--no-tags', '--', remote, str(staging), timeout=120)
                    self._check_clone(staging, remote)
                    self._message(identifier, '正在读取远程分支和默认分支…')
                    branches, current = self._branches(staging)
                    staging.replace(path)
                    staging = None
                else:
                    self._check_clone(path, remote)
                    self._message(identifier, '正在更新远程分支，不切换代码工作区…')
                    run_git(path, 'fetch', '--no-tags', '--prune', 'origin',
                            '+refs/heads/*:refs/remotes/origin/*', timeout=120)
                    self._message(identifier, '正在读取远程分支和默认分支…')
                    branches, current = self._branches(path)
                updated_at = dt.datetime.now(dt.timezone.utc).isoformat()
                record = dict(id=repository_id, name=name, remote_url=remote, root=str(path), updated_at=updated_at)
                with self.lock:
                    records = {**self.registry, repository_id: record}
                    atomic_json(self.registry_path, dict(version=1, repositories=list(records.values())))
                    self.registry = records
                result = dict(id=identifier, state='ready', root=str(path), remote_url=remote,
                              repository_id=repository_id, branches=branches, current=current, updated_at=updated_at,
                              message='同步完成；请选择与日志部署版本对应的远程分支')
        except Exception as exc:
            reason = str(exc) if isinstance(exc, ValueError) else '请检查托管目录权限、Git 安装、网络和仓库登录'
            result = dict(id=identifier, state='failed', root=str(path), remote_url=remote,
                          repository_id=repository_id, branches=[], current='', updated_at='',
                          message='项目同步失败：' + reason + '；请重试，不会静默使用旧代码。')
        finally:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)
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
        with self.lock:
            if job['state'] != 'ready' or self.latest_jobs.get(job['repository_id']) != job['id']:
                raise ValueError('请先完成本仓库最新一次同步，再重新选择远程分支')
        guard = self.repo_locks[job['repository_id']]
        with guard:
            with self.lock:
                if job['state'] != 'ready' or self.latest_jobs.get(job['repository_id']) != job['id']:
                    raise ValueError('请先完成本仓库最新一次同步，再重新选择远程分支')
            branch = str(body.get('branch', ''))
            if branch not in job['branches']:
                raise ValueError('所选远程分支不存在，请重新同步并选择')
            path = Path(job['root'])
            self._check_clone(path, job['remote_url'])
            commit, _ = run_git(path, 'rev-parse', '--verify', 'refs/remotes/' + branch + '^{commit}')
            commit = commit.strip()
            # Keep selected commits reachable after branch deletion/force pushes.
            run_git(path, 'update-ref', 'refs/logscope/snapshots/' + commit, commit)
            return dict(project_root=str(path), branch=branch, commit=commit,
                        repository_id=job['repository_id'], remote_url=job['remote_url'])

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
