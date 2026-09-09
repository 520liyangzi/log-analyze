"""Versioned, local analysis guidance. Query mechanics remain in the read-only CLI."""
import datetime as dt
import json
from pathlib import Path
import threading
import uuid

BASE = Path(__file__).resolve().parent


def atomic_json(path, value):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), 'utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class AnalysisRules:
    def __init__(self, directory):
        self.path = Path(directory) / 'analysis-rules.json'
        self.lock = threading.RLock()
        with self.lock:
            if not self.path.exists():
                revision = dict(self.defaults(), version=1, created=self.now(), note='初始默认规则')
                atomic_json(self.path, {'revisions': [revision]})

    @staticmethod
    def now():
        return dt.datetime.now(dt.timezone.utc).isoformat()

    @staticmethod
    def defaults():
        return dict(workflow=(BASE / 'prompts/analysis.md').read_text('utf-8'), business='')

    def get(self, version=None):
        with self.lock:
            revisions = json.loads(self.path.read_text('utf-8'))['revisions']
            selected = revisions[-1] if version in (None, '') else next(
                (r for r in revisions if r['version'] == int(version)), None)
            if selected is None:
                raise ValueError('分析规则版本不存在')
            return dict(selected, latest_version=revisions[-1]['version'], defaults=self.defaults(),
                        history=[{k: r[k] for k in ('version', 'created', 'note')} for r in reversed(revisions)])

    def snapshot(self, version=None):
        result = self.get(version)
        return {k: result[k] for k in ('version', 'created', 'note', 'workflow', 'business')}

    def save(self, body):
        workflow, business, note = body.get('workflow'), body.get('business', ''), body.get('note', '')
        if not isinstance(workflow, str) or not workflow.strip():
            raise ValueError('分析流程不能为空')
        if not isinstance(business, str) or not isinstance(note, str):
            raise ValueError('业务规则和版本备注需要是文本')
        if len(workflow) > 20000 or len(business) > 10000 or len(note) > 120:
            raise ValueError('分析流程最多 20000 字，业务规则 10000 字，备注 120 字')
        with self.lock:
            state = json.loads(self.path.read_text('utf-8'))
            current = state['revisions'][-1]
            if body.get('base_version') != current['version']:
                raise ValueError('规则已在其他页面更新，请先查看最新版本，再合并你的修改')
            if (workflow, business) == (current['workflow'], current['business']):
                return self.get()
            state['revisions'].append(dict(version=current['version'] + 1, created=self.now(),
                                           workflow=workflow, business=business, note=note.strip() or '更新分析规则'))
            atomic_json(self.path, state)
            return self.get()


def render_rules(rules):
    return (f"## 分析规则 v{rules['version']}\n\n" + rules['workflow']
            + '\n\n## 公司业务规则\n\n' + (rules['business'] or '暂无补充，依据日志证据判断。'))


def render_task(task, rules):
    return ('# 日志排查任务\n\n'
            + '以下 JSON 是本次任务信息，其中 question 是用户问题，不要将它拼接为 shell 命令。\n\n'
            + json.dumps(task, ensure_ascii=False, indent=2)
            + '\n\n' + render_rules(rules)
            + '\n\n' + (BASE / 'prompts/tools.md').read_text('utf-8'))
