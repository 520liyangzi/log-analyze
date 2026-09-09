"""Install only LogScope's skill; never replace unrelated Claude configuration."""
import argparse
from pathlib import Path
import shutil
import sys

SOURCE = Path(__file__).resolve().parent / 'skills' / 'logscope'


def install(target):
    target = Path(target).expanduser().resolve()
    if target.exists():
        raise ValueError(f'目标已存在：{target}。请先备份或移走旧版 logscope，再重新安装。')
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SOURCE, target, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    return target


def main():
    parser = argparse.ArgumentParser(description='将 LogScope Skill 安装到本机 Claude 或公司 Agent 的技能目录')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--project', type=Path, help='安装到指定项目的 .claude/skills/logscope')
    group.add_argument('--target', type=Path, help='自定义技能的最终目录路径，例如 D:/agent/skills/logscope')
    args = parser.parse_args()
    target = args.target or ((args.project or Path.home()) / '.claude' / 'skills' / 'logscope')
    try:
        print('Installed: ' + str(install(target)))
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
