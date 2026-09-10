"""Run the user-provided collect_logs.py and hand its ZIP to LogScope."""
import concurrent.futures
import datetime as dt
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
import zipfile


class LogCollector:
    def __init__(self, store, script):
        self.store = store
        self.script = Path(script)
        self.directory = store.directory / 'collector-work'
        self.directory.mkdir(exist_ok=True)
        self.jobs = {}
        self.processes = {}
        self.lock = threading.RLock()
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)

    @staticmethod
    def _time(value, label):
        try:
            return dt.datetime.strptime(str(value).strip(), '%Y-%m-%d %H:%M:%S')
        except ValueError:
            raise ValueError(f'{label}格式应为 YYYY-MM-DD HH:MM:SS') from None

    def capability(self):
        return {'available': self.script.is_file(), 'script': self.script.name,
                'reason': '' if self.script.is_file() else f'未找到 {self.script.name}，请把脚本放到 app.py 同级目录'}

    def start(self, body):
        if not self.script.is_file():
            raise ValueError(self.capability()['reason'])
        with self.lock:
            existing = list(self.jobs)
        for identifier in existing:
            self.status(identifier)
        pod = str(body.get('pod', '')).strip()
        if not pod or len(pod) > 300:
            raise ValueError('请填写 Pod 名称关键字（最多 300 字符）')
        start_text, end_text = str(body.get('start', '')).strip(), str(body.get('end', '')).strip()
        start, end = self._time(start_text, '开始时间'), self._time(end_text, '结束时间')
        if start >= end:
            raise ValueError('结束时间必须晚于开始时间')
        timeout = int(body.get('timeout') or 300)
        poll = int(body.get('poll') or 5)
        if not 10 <= timeout <= 7200:
            raise ValueError('超时时间需要在 10～7200 秒之间')
        if not 1 <= poll <= 300:
            raise ValueError('轮询间隔需要在 1～300 秒之间')
        with self.lock:
            if any(job['state'] in ('collecting', 'importing') for job in self.jobs.values()):
                raise ValueError('已有日志正在采集或导入，请完成后再开始下一次')
            identifier = uuid.uuid4().hex
            job = dict(id=identifier, state='collecting', pod=pod, start=start_text, end=end_text,
                       created=dt.datetime.now(dt.timezone.utc).isoformat(), message='正在启动采集脚本…',
                       dataset_id='', output='')
            self.jobs[identifier] = job
        options = {key: str(body.get(key, '')).strip() for key in ('url', 'user', 'password', 'headless')}
        options.update(timeout=timeout, poll=poll, encoding=str(body.get('encoding', 'auto')),
                       offset=str(body.get('offset', '+0800')), unit=str(body.get('unit', 'ms')))
        self.pool.submit(self._run, identifier, job.copy(), options)
        return self.status(identifier)

    def _terminate(self, process):
        if process.poll() is not None:
            return
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def _run(self, identifier, job, options):
        output_dir = self.directory / identifier
        output_dir.mkdir()
        staged = self.store.directory / (identifier + '.collect.upload')
        password = options.get('password', '')
        command = [sys.executable, str(self.script), '--pod', job['pod'], '--start', job['start'],
                   '--end', job['end'], '--output', str(output_dir)]
        for key in ('url', 'user', 'password', 'headless'):
            if options[key]:
                command.extend(['--' + key, options[key]])
        command.extend(['--timeout', str(options['timeout']), '--poll', str(options['poll'])])
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
        process = None
        try:
            process = subprocess.Popen(command, cwd=self.script.parent, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace',
                                       creationflags=flags, start_new_session=os.name != 'nt')
            with self.lock:
                self.processes[identifier] = process
                self.jobs[identifier]['message'] = '采集脚本运行中，正在等待平台生成 ZIP…'
            try:
                output, _ = process.communicate(timeout=options['timeout'] + 60)
            except subprocess.TimeoutExpired:
                self._terminate(process)
                output, _ = process.communicate(timeout=15)
                raise ValueError(f'采集超过 {options["timeout"] + 60} 秒，已停止脚本')
            safe_output = (output or '')[-8000:]
            if password:
                safe_output = safe_output.replace(password, '[REDACTED]')
            if process.returncode:
                raise ValueError(f'采集脚本退出码 {process.returncode}：{safe_output[-2000:].strip()}')
            zips = sorted(output_dir.rglob('*.zip'), key=lambda path: path.stat().st_mtime, reverse=True)
            if not zips:
                raise ValueError('采集脚本执行成功，但输出目录中没有找到 ZIP 文件')
            archive = zips[0]
            if not zipfile.is_zipfile(archive):
                raise ValueError(f'脚本生成的文件不是有效 ZIP：{archive.name}')
            shutil.move(str(archive), staged)
            shutil.rmtree(output_dir, ignore_errors=True)
            name = re.sub(r'[^0-9A-Za-z_.\-\u4e00-\u9fff]+', '-', job['pod']).strip('-') or 'pod'
            name += '_' + job['start'].replace(':', '').replace(' ', '_') + '_' + job['end'].replace(':', '').replace(' ', '_') + '.zip'
            dataset = self.store.submit(staged, name, options['encoding'], options['offset'], options['unit'])
            staged = None
            with self.lock:
                self.jobs[identifier].update(state='importing', dataset_id=dataset,
                                             message=f'已下载 {archive.name}，正在解析并建立索引…', output=safe_output)
        except Exception as exc:
            with self.lock:
                current = self.jobs.get(identifier)
                if current:
                    current.update(state='failed', message=str(exc))
        finally:
            with self.lock:
                self.processes.pop(identifier, None)
            shutil.rmtree(output_dir, ignore_errors=True)
            if staged:
                Path(staged).unlink(missing_ok=True)

    def status(self, identifier):
        with self.lock:
            if identifier not in self.jobs:
                raise ValueError('采集任务不存在或服务已经重启')
            job = dict(self.jobs[identifier])
        if job['state'] == 'importing' and job['dataset_id']:
            dataset = next((item for item in self.store.datasets() if item['id'] == job['dataset_id']), None)
            if dataset:
                if dataset['state'] == 'ready':
                    job.update(state='ready', message=f'采集并导入完成：{dataset["records"]:,} 条日志')
                elif dataset['state'] == 'failed':
                    job.update(state='failed', message='ZIP 已下载，但导入失败：' + dataset.get('error', '未知错误'))
                elif dataset.get('progress'):
                    progress = dataset['progress']
                    job['message'] = f'正在建立索引：{progress.get("files", 0):,} 个文件、{progress.get("records", 0):,} 条记录'
            with self.lock:
                self.jobs[identifier].update(state=job['state'], message=job['message'])
        return job

    def close(self):
        with self.lock:
            processes = list(self.processes.values())
        for process in processes:
            self._terminate(process)
        self.pool.shutdown(wait=False, cancel_futures=True)
