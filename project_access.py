"""Read-only Git repository discovery for code-assisted investigations."""
from pathlib import Path
import subprocess


def _git(root, *args, timeout=20):
    try:
        result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=timeout, check=False)
    except FileNotFoundError:
        raise ValueError('没有找到 git 命令，请先安装 Git') from None
    except subprocess.TimeoutExpired:
        raise ValueError('读取 Git 仓库超时') from None
    if result.returncode:
        raise ValueError((result.stderr or result.stdout or 'Git 命令失败').strip())
    return result.stdout


def inspect_repository(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 4096 or '\x00' in value:
        raise ValueError('请填写有效的项目目录')
    requested = Path(value.strip()).expanduser()
    if not requested.is_absolute():
        raise ValueError('项目目录请填写绝对路径')
    if not requested.is_dir():
        raise ValueError('项目目录不存在或不是文件夹')
    root_text = _git(requested, 'rev-parse', '--show-toplevel').strip()
    root = Path(root_text).resolve()
    if not root.is_dir():
        raise ValueError('无法确定 Git 项目根目录')
    current = _git(root, 'branch', '--show-current').strip()
    refs = _git(root, 'for-each-ref', '--format=%(refname:short)', 'refs/heads', 'refs/remotes')
    branches = []
    for branch in refs.splitlines():
        branch = branch.strip()
        if branch and not branch.endswith('/HEAD') and branch not in branches:
            branches.append(branch)
    if current and current in branches:
        branches.remove(current)
        branches.insert(0, current)
    if not branches:
        raise ValueError('仓库里没有可读取的本地或远程分支')
    return {'root': str(root), 'current': current, 'branches': branches[:1000]}


def select_revision(path, branch):
    repository = inspect_repository(path)
    if branch not in repository['branches']:
        raise ValueError('所选分支不存在，请重新读取分支列表')
    commit = _git(repository['root'], 'rev-parse', '--verify', branch + '^{commit}').strip()
    return {**repository, 'branch': branch, 'commit': commit}
