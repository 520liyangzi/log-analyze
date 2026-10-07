"""Removing the embedded shell disables its endpoints without deleting data."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from analysis_rules import AnalysisRules
from app import make_server


class TerminalRemovalTests(unittest.TestCase):
    def test_removed_routes_and_assets_leave_existing_data_and_rules_intact(self):
        with tempfile.TemporaryDirectory(prefix='logscope retired terminal ') as temporary:
            data = Path(temporary) / 'data'
            data.mkdir()
            preserved = {
                data / 'terminal-config.json': b'{"command":"retired-agent-command"}',
                data / 'terminal-sessions' / 'existing' / 'session.json': b'{"id":"existing","state":"stopped"}',
                data / 'terminal-sessions' / 'existing' / 'terminal.log': '旧终端记录，保留磁盘原文。'.encode(),
                data / 'terminal-sessions' / 'existing' / 'report.md': b'# Existing terminal report\n',
                data / 'chat-sessions' / 'existing' / 'report.md': b'# Existing native report\n',
                data / 'archives' / 'retained.zip': b'retained fixture bytes',
            }
            for path, content in preserved.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            rules = AnalysisRules(data)
            initial = rules.get()
            rules.save(dict(workflow=initial['workflow'], business='原有业务规则仍然有效', base_version=initial['version']))
            preserved[rules.path] = rules.path.read_bytes()
            server = make_server(data, 0)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            base = 'http://127.0.0.1:' + str(server.server_address[1])
            try:
                self.assertFalse(hasattr(server, 'terminals'))
                get_paths = ['/api/terminal/' + route for route in ('config', 'sessions', 'output', 'task', 'history', 'report')]
                get_paths += ['/terminal.js', '/vendor/xterm.js', '/vendor/xterm.css', '/vendor/addon-fit.js', '/api/project/branches']
                post_paths = ['/api/terminal/' + route for route in ('config', 'start', 'resume', 'input', 'resize', 'stop', 'delete', 'preview', 'rules', 'session-id', 'code-preview', 'code-task')]
                for path, payload in [(path, None) for path in get_paths] + [(path, b'{}') for path in post_paths]:
                    with self.subTest(path=path, method='POST' if payload else 'GET'):
                        request = Request(base + path, data=payload, headers={'Content-Type': 'application/json'})
                        with self.assertRaises(HTTPError) as error:
                            urlopen(request, timeout=10)
                        with error.exception as response:
                            self.assertEqual(response.code, 404)
                with urlopen(base + '/api/analysis/rules', timeout=10) as response:
                    self.assertEqual(json.load(response)['business'], '原有业务规则仍然有效')
                with urlopen(base + '/api/chat/capability', timeout=10) as response:
                    self.assertNotIn('local_terminal', json.load(response))
                with urlopen(base + '/api/chat/sessions', timeout=10) as response:
                    self.assertEqual(json.load(response), [])
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=10)
                server.store.pool.shutdown(wait=True)
                server.chats.pool.shutdown(wait=True)
                server.chats.projects.pool.shutdown(wait=True)
            for path, content in preserved.items():
                self.assertEqual(path.read_bytes(), content, str(path))


if __name__ == '__main__':
    unittest.main()
