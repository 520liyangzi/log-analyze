import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from collector_environments import CollectorEnvironments
from terminal_bridge import DEFAULT_COMMAND, TerminalManager, terminal_environment


class CollectorEnvironmentTests(unittest.TestCase):
    def test_create_update_resolve_delete_without_exposing_password(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = CollectorEnvironments(temporary)
            created = store.save({'name': '测试环境', 'url': 'https://logs.example.test',
                                  'user': 'admin', 'password': 'secret'})
            self.assertNotIn('password', created)
            self.assertTrue(created['has_password'])
            self.assertNotIn('password', store.list()[0])
            self.assertEqual(store.resolve(created['id'])['password'], 'secret')
            updated = store.save({'id': created['id'], 'name': '测试环境',
                                  'url': 'https://new.example.test', 'user': 'operator', 'password': ''})
            self.assertEqual(updated['url'], 'https://new.example.test')
            self.assertEqual(store.resolve(created['id'])['password'], 'secret')
            raw = json.loads((Path(temporary) / 'collector-environments.json').read_text('utf-8'))
            self.assertEqual(raw['environments'][0]['password'], 'secret')
            self.assertTrue(store.delete(created['id'])['ok'])
            self.assertEqual(store.list(), [])

    def test_validation_and_duplicate_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = CollectorEnvironments(temporary)
            with self.assertRaises(ValueError):
                store.save({'name': 'bad', 'url': 'not-a-url', 'user': 'admin', 'password': 'secret'})
            store.save({'name': '生产', 'url': 'https://one.example', 'user': 'admin', 'password': 'secret'})
            with self.assertRaises(ValueError):
                store.save({'name': '生产', 'url': 'https://two.example', 'user': 'admin', 'password': 'secret'})


class TerminalEnvironmentTests(unittest.TestCase):
    def test_default_company_command_and_windows_does_not_fake_xterm(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = mock.Mock()
            store.directory = Path(temporary)
            manager = TerminalManager(store, 'http://127.0.0.1:8765')
            self.assertEqual(manager.config()['command'], DEFAULT_COMMAND)
        with mock.patch.dict(os.environ, {'TERM': 'xterm-256color', 'COLORTERM': 'truecolor'}):
            with mock.patch('terminal_bridge.os.name', 'nt'):
                environment = terminal_environment('http://127.0.0.1:8765', 'dataset')
        self.assertNotIn('TERM', environment)
        self.assertNotIn('COLORTERM', environment)


if __name__ == '__main__':
    unittest.main()
