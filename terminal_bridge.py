"""Real local PTY sessions for the browser. No model SDK and no shell-output imitation."""
import codecs
import collections
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from analysis_rules import AnalysisRules, atomic_json, render_code_task, render_rules, render_task
from project_access import inspect_repository, select_revision

BASE = Path(__file__).resolve().parent
TASK_PROMPT = 'Read task.md in the current directory. Use its rules and query tools to investigate the question. Reply in Chinese and write report.md.'
DEFAULT_RESUME_TEMPLATE = '{command} --sessions {session_id}'
SESSION_ID_RE = re.compile(r'(?i)\bsessions?(?:\s*id)?\s*[:=]?\s*([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\b')


def dimensions(cols, rows):
    return min(400, max(20, int(cols))), min(200, max(5, int(rows)))


class UnixPTY:
    def __init__(self, cwd, env, cols, rows):
        import pty
        master, slave = pty.openpty()
        self.master = master
        shell = os.environ.get('SHELL') or '/bin/bash'
        if not Path(shell).is_file():
            shell = '/bin/sh'
        try:
            self.resize(cols, rows)
            # A separate executable claims the controlling terminal; no Python preexec_fn
            # runs after fork in the multithreaded HTTP process.
            self.proc = subprocess.Popen([sys.executable, str(BASE / 'terminal_child.py'), shell],
                                         stdin=slave, stdout=slave, stderr=slave, cwd=cwd, env=env,
                                         start_new_session=True, close_fds=True)
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self.pid = self.proc.pid
        self.closed = False

    def read(self):
        return self.decoder.decode(os.read(self.master, 16384))

    def write(self, text):
        data = text.encode('utf-8')
        while data:
            written = os.write(self.master, data)
            data = data[written:]

    def resize(self, cols, rows):
        import fcntl
        import struct
        import termios
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))

    def alive(self):
        return self.proc.poll() is None

    def close(self):
        if self.closed:
            return
        self.closed = True
        groups = {self.pid}
        try:
            groups.add(os.tcgetpgrp(self.master))
        except OSError:
            pass
        for group in groups:
            if group <= 0 or group == os.getpgrp():
                continue
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        os.close(self.master)


class WindowsPTY:
    def __init__(self, cwd, env, cols, rows):
        from winpty import PtyProcess
        self.proc = PtyProcess.spawn([os.environ.get('COMSPEC', 'cmd.exe'), '/d', '/q'],
                                     cwd=str(cwd), env=env, dimensions=(rows, cols))
        self.pid = self.proc.pid
        self.closed = False

    def read(self):
        return self.proc.read(16384)

    def write(self, text):
        self.proc.write(text)

    def resize(self, cols, rows):
        self.proc.setwinsize(rows, cols)

    def alive(self):
        return self.proc.isalive()

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.proc.isalive():
            subprocess.run(['taskkill', '/PID', str(self.pid), '/T', '/F'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
        try:
            self.proc.close(force=True)
        except (OSError, EOFError):
            pass


class Session:
    def __init__(self, identifier, directory, dataset, command, pty, task=None, launch_mode='manual',
                 ai_session_id='', created='', rule_updates=None, code_tasks=None):
        self.id, self.directory, self.dataset, self.command = identifier, directory, dataset, command
        self.pty = pty
        self.created = created or dt.datetime.now(dt.timezone.utc).isoformat()
        self.state, self.error = 'running', ''
        self.lock = threading.RLock()
        self.write_lock = threading.Lock()
        self.chunks = collections.deque()
        self.total, self.oldest = 0, 0
        self.limit = 2 * 1024 * 1024
        self.last_activity = time.monotonic()
        self.task = task or {}
        self.launch_mode = launch_mode
        self.ai_session_id = ai_session_id
        self.session_id_scan_tail = ''
        self.rule_updates = rule_updates or []
        self.code_tasks = code_tasks or []
        self.transcript_path = self.directory / 'terminal.log'
        if self.transcript_path.exists():
            with self.transcript_path.open('rb') as file:
                size = self.transcript_path.stat().st_size
                file.seek(max(0, size - self.limit))
                previous = file.read().decode('utf-8', 'replace')
            if previous:
                self.chunks.append((0, previous))
                self.total = len(previous)
        self.transcript = self.transcript_path.open('ab', buffering=0)
        if self.transcript_path.stat().st_size:
            resumed_at = dt.datetime.now(dt.timezone.utc).isoformat()
            marker = ('\r\n\r\n[LogScope · 会话恢复 %s]\r\n' % resumed_at).encode()
            self.transcript.write(marker)
            self.chunks.append((self.total, marker.decode()))
            self.total += len(marker.decode())
        self.persist()
        self.reader = threading.Thread(target=self.read_loop, daemon=True)
        self.reader.start()

    def append(self, text):
        with self.lock:
            self.transcript.write(text.encode('utf-8', 'replace'))
            self.chunks.append((self.total, text))
            self.total += len(text)
            while self.chunks and self.total - self.chunks[0][0] > self.limit:
                self.chunks.popleft()
            self.oldest = self.chunks[0][0] if self.chunks else self.total
            scan = self.session_id_scan_tail + text
            self.session_id_scan_tail = scan[-256:]
            match = SESSION_ID_RE.search(scan)
            if match and match.group(1).lower() != self.ai_session_id:
                self.ai_session_id = match.group(1).lower()
                self.persist()

    def read_loop(self):
        try:
            while self.state == 'running':
                text = self.pty.read()
                if text:
                    self.append(text)
                elif not self.pty.alive():
                    break
        except (EOFError, OSError):
            pass
        except Exception as error:
            self.error = str(error)
        finally:
            with self.lock:
                if self.state == 'running':
                    self.state = 'exited'
                self.persist()
            self.pty.close()
            self.transcript.close()

    def persist(self):
        atomic_json(self.directory / 'session.json', dict(
            id=self.id, dataset=self.dataset, command=self.command, state=self.state,
            error=self.error, created=self.created, cwd=str(self.directory),
            name=self.task.get('name', ''), question=self.task.get('question', ''),
            rules_version=self.task.get('rules_version'), launch_mode=self.launch_mode,
            ai_session_id=self.ai_session_id, rule_updates=list(self.rule_updates),
            code_tasks=list(self.code_tasks), updated=dt.datetime.now(dt.timezone.utc).isoformat()))

    def info(self):
        return dict(id=self.id, dataset=self.dataset, command=self.command, state=self.state, error=self.error,
                    created=self.created, cwd=str(self.directory), pid=self.pty.pid,
                    name=self.task.get('name', ''), question=self.task.get('question', ''),
                    rules_version=self.task.get('rules_version'), launch_mode=self.launch_mode,
                    rule_updates=list(self.rule_updates), code_tasks=list(self.code_tasks),
                    ai_session_id=self.ai_session_id, live=True, saved=True,
                    transcript_bytes=self.transcript_path.stat().st_size if self.transcript_path.exists() else 0)

    def poll(self, cursor):
        with self.lock:
            self.last_activity = time.monotonic()
            cursor = max(0, int(cursor))
            reset = cursor < self.oldest or cursor > self.total
            start = self.oldest if reset else cursor
            # Keep polling bounded, even after a noisy program emits megabytes.
            remaining, parts = 128 * 1024, []
            for offset, text in self.chunks:
                if offset + len(text) <= start:
                    continue
                piece = text[max(0, start - offset):][:remaining]
                parts.append(piece)
                remaining -= len(piece)
                if not remaining:
                    break
            text = ''.join(parts)
            return dict(self.info(), output=text, cursor=start + len(text), reset=reset,
                        more=start + len(text) < self.total)

    def write(self, text):
        if not isinstance(text, str) or len(text) > 65536:
            raise ValueError('终端单次输入最多 65536 个字符')
        with self.write_lock:
            if self.state != 'running':
                raise ValueError('终端已退出，请新建终端')
            self.last_activity = time.monotonic()
            self.pty.write(text)

    def stop(self):
        with self.lock:
            self.state = 'stopped'
            self.persist()
        self.pty.close()


class TerminalManager:
    def __init__(self, store, url):
        self.store, self.url = store, url
        self.directory = store.directory / 'terminal-sessions'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.sessions = {}
        self.lock = threading.RLock()
        self.config_file = store.directory / 'terminal-config.json'
        self.rules = AnalysisRules(store.directory)

    def config(self):
        config = json.loads(self.config_file.read_text('utf-8')) if self.config_file.exists() else {'command': 'claude'}
        config.setdefault('launch_mode', 'argument' if config.get('command') == 'claude' else 'manual')
        config.setdefault('resume_template', DEFAULT_RESUME_TEMPLATE)
        available, reason = True, ''
        if os.name == 'nt':
            try:
                import winpty  # noqa: F401
            except ImportError:
                available, reason = False, '网页终端需要 Windows ConPTY 依赖，请先运行 python -m pip install -r requirements.txt，再重启服务。'
        config.update(available=available, reason=reason, platform='Windows CMD' if os.name == 'nt' else 'POSIX shell',
                      prompt=TASK_PROMPT, python=sys.executable)
        return config

    def save_config(self, body):
        command = body.get('command', '')
        if not isinstance(command, str) or len(command) > 2000 or any(c in command for c in '\x00\r\n'):
            raise ValueError('启动命令应为单行，最多 2000 个字符')
        # This is an explicitly user-configured shell command, just like typing in CMD.
        mode = body.get('launch_mode', self.config()['launch_mode'])
        if mode not in ('argument', 'manual'):
            raise ValueError('请选择自动传入任务或兼容模式')
        template = body.get('resume_template', self.config()['resume_template'])
        if (not isinstance(template, str) or len(template) > 2000 or any(c in template for c in '\x00\r\n')
                or '{command}' not in template or '{session_id}' not in template):
            raise ValueError('恢复命令模板必须是单行，并包含 {command} 和 {session_id}')
        with self.lock:
            atomic_json(self.config_file, {'command': command.strip(), 'launch_mode': mode,
                                           'resume_template': template.strip()})
        return self.config()

    def prepare(self, body):
        dataset, question = str(body.get('dataset', '')), str(body.get('question', '')).strip()
        if len(question) > 20000:
            raise ValueError('问题最多 20000 个字符')
        with self.store.connect() as db:
            self.store.require_ready(db, dataset)
            name = db.execute('SELECT name FROM datasets WHERE id=?', (dataset,)).fetchone()['name']
        rules = self.rules.snapshot(body.get('rules_version'))
        endpoints = []
        for value in re.findall(r'(?<![A-Za-z0-9_])(/[A-Za-z0-9_./?=&%:+~-]+)', question):
            value = value.rstrip('.,;，。；：:!?！？')
            if value and value not in endpoints:
                endpoints.append(value)
        trace_ids = list(dict.fromkeys(re.findall(r'(?<!\d)\d{15,}(?!\d)', question)))
        task = dict(dataset=dataset, name=name, url=self.url, question=question, python=sys.executable,
                    rules_version=rules['version'], scope=self.store.dataset_scope(dataset),
                    query_hints={'endpoints': endpoints[:20], 'trace_ids': trace_ids[:20]})
        return task, rules, render_task(task, rules)

    def preview(self, body):
        task, rules, text = self.prepare(body)
        return dict(task=task, rules=rules, text=text)

    def start(self, body):
        config = self.config()
        if not config['available']:
            raise ValueError(config['reason'])
        task, rules, task_text = self.prepare(body)
        dataset = task['dataset']
        cols, rows = dimensions(body.get('cols', 100), body.get('rows', 30))
        with self.lock:
            if sum(s.state == 'running' for s in self.sessions.values()) >= 3:
                raise ValueError('最多同时运行 3 个终端，请先结束不用的终端')
            identifier = uuid.uuid4().hex
            directory = self.directory / identifier
            directory.mkdir()
            skill = directory / '.claude' / 'skills' / 'logscope'
            shutil.copytree(BASE / 'skills' / 'logscope', skill, ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
            (directory / 'tools').mkdir()
            shutil.copyfile(BASE / 'skills/logscope/scripts/logscope.py', directory / 'tools/logscope.py')
            shutil.copyfile(BASE / 'skills/logscope/scripts/project.py', directory / 'tools/project.py')
            atomic_json(directory / 'rules.json', rules)
            (directory / 'task.json').write_text(json.dumps(task, ensure_ascii=False, indent=2), 'utf-8')
            (directory / 'task.md').write_text(task_text, 'utf-8')
            (directory / 'CLAUDE.md').write_text('本目录用于 LogScope 日志排查。先读取 task.md，按其中的问题和规则调用 tools/logscope.py。\n', 'utf-8')
            env = dict(os.environ, TERM='xterm-256color', COLORTERM='truecolor', PYTHONIOENCODING='utf-8',
                       LOGSCOPE_URL=self.url, LOGSCOPE_DATASET_ID=dataset)
            pty = WindowsPTY(directory, env, cols, rows) if os.name == 'nt' else UnixPTY(directory, env, cols, rows)
            command = config['command'] if body.get('run_command', True) else ''
            mode = config['launch_mode'] if command else 'manual'
            session = Session(identifier, directory, dataset, command, pty, task, mode)
            self.sessions[identifier] = session
            if command:
                # Only this fixed ASCII instruction enters shell syntax. The user's
                # question and editable rules live in files, never command arguments.
                launch = command + (' "' + TASK_PROMPT + '"' if mode == 'argument' else '')
                session.write(launch + '\r')
            return session.info()

    @staticmethod
    def validate_ai_session_id(value):
        try:
            return str(uuid.UUID(str(value).strip()))
        except (ValueError, AttributeError, TypeError):
            raise ValueError('Session ID 必须是完整 UUID，例如 a0c43b85-1ca0-41e3-8e99-15648dd3ec17') from None

    def session_directory(self, identifier):
        if not re.fullmatch(r'[0-9a-f]{32}', str(identifier)):
            raise ValueError('排查任务不存在')
        directory = self.directory / identifier
        if not directory.is_dir():
            raise ValueError('排查任务不存在')
        return directory

    def saved_info(self, identifier):
        directory = self.session_directory(identifier)
        manifest = directory / 'session.json'
        if manifest.exists():
            info = json.loads(manifest.read_text('utf-8'))
        else:
            task_file = directory / 'task.json'
            task = json.loads(task_file.read_text('utf-8')) if task_file.exists() else {}
            info = dict(id=identifier, dataset=task.get('dataset', ''), command='', state='saved',
                        error='', created=dt.datetime.fromtimestamp(directory.stat().st_mtime, dt.timezone.utc).isoformat(),
                        cwd=str(directory), name=task.get('name', ''), question=task.get('question', ''),
                        rules_version=task.get('rules_version'), launch_mode='manual', ai_session_id='',
                        rule_updates=[], code_tasks=[])
        info.update(id=identifier, cwd=str(directory), live=False, saved=True,
                    transcript_bytes=(directory / 'terminal.log').stat().st_size if (directory / 'terminal.log').exists() else 0,
                    report_available=(directory / 'report.md').is_file() and not (directory / 'report.md').is_symlink())
        if info.get('state') == 'running':
            info['state'] = 'interrupted'
        return info

    def save_ai_session_id(self, body):
        identifier = str(body.get('id', ''))
        value = self.validate_ai_session_id(body.get('ai_session_id', ''))
        with self.lock:
            session = self.sessions.get(identifier)
            if session:
                with session.lock:
                    session.ai_session_id = value
                    session.persist()
                    return session.info()
            info = self.saved_info(identifier)
            info['ai_session_id'] = value
            info.pop('live', None); info.pop('saved', None); info.pop('report_available', None); info.pop('transcript_bytes', None)
            atomic_json(self.session_directory(identifier) / 'session.json', info)
            return self.saved_info(identifier)

    def resume(self, body):
        config = self.config()
        if not config['available']:
            raise ValueError(config['reason'])
        if not config['command'].strip():
            raise ValueError('请先设置本机 AI 启动命令')
        identifier = str(body.get('id', ''))
        cols, rows = dimensions(body.get('cols', 100), body.get('rows', 30))
        with self.lock:
            active = self.sessions.get(identifier)
            if active and active.state == 'running':
                raise ValueError('这个排查任务仍在运行')
            if sum(s.state == 'running' for s in self.sessions.values()) >= 3:
                raise ValueError('最多同时运行 3 个终端，请先结束不用的终端')
            directory = self.session_directory(identifier)
            info = self.saved_info(identifier)
            ai_session_id = self.validate_ai_session_id(body.get('ai_session_id') or info.get('ai_session_id'))
            task = json.loads((directory / 'task.json').read_text('utf-8'))
            env = dict(os.environ, TERM='xterm-256color', COLORTERM='truecolor', PYTHONIOENCODING='utf-8',
                       LOGSCOPE_URL=self.url, LOGSCOPE_DATASET_ID=task.get('dataset', ''))
            pty = WindowsPTY(directory, env, cols, rows) if os.name == 'nt' else UnixPTY(directory, env, cols, rows)
            session = Session(identifier, directory, task.get('dataset', ''), config['command'], pty, task,
                              info.get('launch_mode', config['launch_mode']), ai_session_id, info.get('created', ''),
                              info.get('rule_updates', []), info.get('code_tasks', []))
            self.sessions[identifier] = session
            launch = config['resume_template'].replace('{command}', config['command']).replace('{session_id}', ai_session_id)
            session.write(launch + '\r')
            return session.info()

    def task_details(self, identifier):
        with self.lock:
            session = self.sessions.get(identifier)
        directory = session.directory if session else self.session_directory(identifier)
        task = session.task if session else json.loads((directory / 'task.json').read_text('utf-8'))
        info = session.info() if session else self.saved_info(identifier)
        return dict(task=task, text=(directory / 'task.md').read_text('utf-8'),
                    rule_updates=list(info.get('rule_updates', [])), code_tasks=list(info.get('code_tasks', [])))

    def project_branches(self, path):
        return inspect_repository(path)

    def prepare_code(self, body):
        session = self.get(body.get('id', ''))
        with session.lock:
            if session.state != 'running':
                raise ValueError('AI 终端已结束，请先回到运行中的日志排查会话')
            report = self.report(session.id)
            if not report['available']:
                raise ValueError('日志分析报告还没有生成，请等待 report.md 后再继续代码定位')
            repository = select_revision(str(body.get('project_path', '')), str(body.get('branch', '')))
            expected = str(body.get('commit', '')).strip()
            if expected and expected != repository['commit']:
                raise ValueError('所选分支在预览后发生了变化，请重新生成任务预览')
            task = dict(project_root=repository['root'], branch=repository['branch'], commit=repository['commit'],
                        question=session.task.get('question', ''), log_report='report.md',
                        log_dataset=session.task.get('name', ''), read_only=True)
            return session, task, render_code_task(task)

    def preview_code(self, body):
        _, task, text = self.prepare_code(body)
        return dict(task=task, text=text)

    def create_code_task(self, body):
        session, task, text = self.prepare_code(body)
        with session.lock:
            number = len(session.code_tasks) + 1
            filename = 'code-task.md' if number == 1 else f'code-task-{number:04d}.md'
            json_name = filename.removesuffix('.md') + '.json'
            atomic_json(session.directory / json_name, task)
            (session.directory / filename).write_text(text, 'utf-8')
            # The CLI always reads code-task.json so each explicitly confirmed
            # follow-up becomes the active fixed revision without checking it out.
            atomic_json(session.directory / 'code-task.json', task)
            entry = dict(file=filename, json=json_name, project_root=task['project_root'],
                         branch=task['branch'], commit=task['commit'], created=AnalysisRules.now())
            session.code_tasks.append(entry)
            atomic_json(session.directory / 'code-tasks.json', session.code_tasks)
            session.persist()
            prompt = (f'Read {filename} and report.md in the current directory. '
                      'Use tools/project.py to investigate the fixed code revision. '
                      'Reply in Chinese and update report.md with a separate code-location section.')
            return dict(task=task, text=text, prompt=prompt, entry=entry)

    def update_rules(self, body):
        session = self.get(body['id'])
        rules = self.rules.snapshot(body.get('rules_version'))
        with session.lock:
            if session.state != 'running':
                raise ValueError('终端已结束，请新建任务使用最新规则')
            folder = session.directory / 'rule-updates'
            folder.mkdir(exist_ok=True)
            relative = f'rule-updates/{len(session.rule_updates) + 1:04d}.md'
            text = ('# 用户发送的分析规则更新\n\n本次问题和日志包仍以 task.json 为准，查询工具说明仍以 task.md 为准。'
                    '\n收到本条更新后，使用下面的规则替换之前的分析规则与公司业务规则，重新检查已有判断，并在报告中记录新版本。\n\n'
                    + render_rules(rules))
            (session.directory / relative).write_text(text, 'utf-8')
            update = dict(version=rules['version'], file=relative, created=AnalysisRules.now())
            session.rule_updates.append(update)
            atomic_json(session.directory / 'rule-updates.json', session.rule_updates)
            session.persist()
            return dict(update, prompt=f'Read {relative} in the current directory and apply the updated rules to this investigation. Reply in Chinese and update report.md.')

    def get(self, identifier):
        with self.lock:
            session = self.sessions.get(identifier)
        if not session:
            raise ValueError('终端会话不存在或服务已重启，请重新启动终端')
        return session

    def list(self):
        with self.lock:
            live = {identifier: session.info() for identifier, session in self.sessions.items()}
        result = []
        for directory in self.directory.iterdir():
            if directory.is_dir() and re.fullmatch(r'[0-9a-f]{32}', directory.name):
                result.append(live.get(directory.name) or self.saved_info(directory.name))
        return sorted(result, key=lambda item: item.get('created', ''), reverse=True)

    def history(self, identifier):
        info = next((item for item in self.list() if item['id'] == identifier), None)
        if not info:
            raise ValueError('排查任务不存在')
        path = self.session_directory(identifier) / 'terminal.log'
        truncated = False
        raw = b''
        if path.exists():
            size = path.stat().st_size
            with path.open('rb') as file:
                if size > 2 * 1024 * 1024:
                    file.seek(size - 2 * 1024 * 1024); truncated = True
                raw = file.read()
        return dict(info=info, transcript=raw.decode('utf-8', 'replace'), truncated=truncated)

    def dataset_in_use(self, identifier):
        with self.lock:
            return any(s.dataset == identifier and s.state == 'running' for s in self.sessions.values())

    def report(self, identifier):
        with self.lock:
            session = self.sessions.get(identifier)
        directory = session.directory if session else self.session_directory(identifier)
        path = directory / 'report.md'
        if path.is_symlink():
            raise ValueError('报告必须是任务目录内的普通文件')
        if not path.exists():
            return dict(available=False, text='', cwd=str(directory))
        if path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError('报告超过 4 MB，请在本机查看')
        return dict(available=True, text=path.read_text('utf-8'), cwd=str(directory))

    def close(self):
        for session in list(self.sessions.values()):
            if session.state == 'running':
                session.stop()
