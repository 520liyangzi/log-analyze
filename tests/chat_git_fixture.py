"""Generated, local-only Git repository served over real dumb HTTP for browsers.

The owner supplies a temporary directory and is responsible for removing it after
``close()``. No production repository, external service, or credentials are used.
"""
import json
import os
from pathlib import Path
import subprocess
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


class LocalGitFixture:
    branch = 'origin/release/2026'

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.source = self.root / 'git-fixture-source'
        self.public = self.root / 'git-fixture-http'
        self.remote = self.public / 'fixture.git'
        self.source.mkdir(parents=True)
        self.public.mkdir(parents=True)
        self._lock = threading.RLock()
        self._advanced = False
        self._closed = False
        self._environment = dict(os.environ, GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='never',
                                 GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
        self._git(self.source, 'init', '--initial-branch=main')
        self._git(self.source, 'config', 'user.name', 'LogScope browser fixture')
        self._git(self.source, 'config', 'user.email', 'fixture@example.invalid')
        self._write_service('browser-code-main')
        self._git(self.source, 'add', 'Service.java')
        self._git(self.source, 'commit', '-m', 'Generated main branch')
        self._git(self.source, 'checkout', '-b', 'release/2026')
        self._write_service('browser-code-v1')
        self._git(self.source, 'add', 'Service.java')
        self._git(self.source, 'commit', '-m', 'Generated release version one')
        self.initial_commit = self._git(self.source, 'rev-parse', 'HEAD')
        self.current_commit = self.initial_commit
        self._git(self.public, 'init', '--bare', '--initial-branch=main', str(self.remote))
        self._git(self.source, 'remote', 'add', 'origin', str(self.remote))
        self._git(self.source, 'push', 'origin', 'main', 'release/2026')
        self._git(self.remote, 'update-server-info')

        fixture = self

        class Handler(SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=str(fixture.public), **kwargs)

            def log_message(self, *_args):
                pass

            def json(self, value, status=200):
                raw = json.dumps(value, ensure_ascii=False).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(raw)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if urlsplit(self.path).path == '/__fixture__/repository':
                    self.json(fixture.metadata())
                else:
                    super().do_GET()

            def do_POST(self):
                if urlsplit(self.path).path != '/__fixture__/advance':
                    self.json({'error': 'Unknown fixture control endpoint'}, 404)
                    return
                try:
                    self.json(fixture.advance())
                except (OSError, subprocess.SubprocessError) as error:
                    self.json({'error': 'Could not advance generated Git fixture: ' + str(error)}, 500)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)
        self.remote_url = self.url + '/fixture.git'
        self._thread = threading.Thread(target=self.server.serve_forever,
                                        name='chat-browser-git-fixture', daemon=True)
        self._thread.start()

    def _git(self, directory, *args):
        result = subprocess.run(['git', '--no-pager', '-c', 'commit.gpgsign=false', '-C', str(directory), *args],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=self._environment, timeout=30, check=True)
        return result.stdout.decode('utf-8', errors='replace').strip()

    def _write_service(self, marker):
        (self.source / 'Service.java').write_text(
            '// Synthetic source for LogScope browser regression.\n'
            'public final class Service {\n'
            '    public String diagnosticMarker() {\n'
            f'        return "{marker}";\n'
            '    }\n'
            '}\n', encoding='utf-8')

    def metadata(self):
        with self._lock:
            return dict(remote_url=self.remote_url, branch=self.branch,
                        initial_commit=self.initial_commit, current_commit=self.current_commit)

    def advance(self):
        with self._lock:
            if not self._advanced:
                self._write_service('browser-code-v2')
                self._git(self.source, 'add', 'Service.java')
                self._git(self.source, 'commit', '-m', 'Generated release version two')
                self._git(self.source, 'push', 'origin', 'release/2026')
                self._git(self.remote, 'update-server-info')
                self.current_commit = self._git(self.source, 'rev-parse', 'HEAD')
                self._advanced = True
            return self.metadata()

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=5)
