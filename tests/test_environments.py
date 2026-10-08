import json
from pathlib import Path
import tempfile
import unittest

from collector_environments import CollectorEnvironments


class CollectorEnvironmentTests(unittest.TestCase):
    def test_create_update_resolve_delete_without_exposing_password(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = CollectorEnvironments(temporary)
            created = store.save({'name': '测试环境', 'url': 'https://logs.example.test:31945',
                                  'user': 'admin', 'password': 'secret'})
            self.assertNotIn('password', created)
            self.assertTrue(created['has_password'])
            self.assertNotIn('password', store.list()[0])
            self.assertEqual(store.resolve(created['id'])['password'], 'secret')
            updated = store.save({'id': created['id'], 'name': '测试环境',
                                  'url': 'https://new.example.test:31945', 'user': 'operator', 'password': ''})
            self.assertEqual(updated['url'], 'https://new.example.test:31945')
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
            store.save({'name': '生产', 'url': 'https://one.example:31945', 'user': 'admin', 'password': 'secret'})
            with self.assertRaises(ValueError):
                store.save({'name': '生产', 'url': 'https://two.example:31945', 'user': 'admin', 'password': 'secret'})

    def test_invalid_addresses_never_overwrite_a_saved_environment(self):
        invalid = [
            ('https://logs.example.test', '端口'),
            ('https://logs.example.test:31945/', '/'),
            ('https://logs.example.test:31945/logs', '/'),
            ('https://logs.example.test:0', '端口'),
            ('https://logs.example.test:65536', '端口'),
            ('https://logs.example.test:nope', '端口'),
            ('https://logs.example.test:', '端口'),
            ('https://logs.example.test:31945?token=test', '参数'),
            ('https://logs.example.test:31945#fragment', '#'),
            ('https://user:secret@logs.example.test:31945', '用户名'),
            ('https://logs .example.test:31945', '空格'),
            ('https://logs.example.test:31945\npath', '空格'),
            ('ftp://logs.example.test:31945', 'http'),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            store = CollectorEnvironments(temporary)
            original = store.save(dict(name='保留环境', url='https://logs.example.test:31945',
                                       user='admin', password='synthetic-secret'))
            persisted = store.path.read_bytes()
            for address, hint in invalid:
                for identifier in ('', original['id']):
                    with self.subTest(url=address, operation='edit' if identifier else 'new'):
                        with self.assertRaises(ValueError) as failure:
                            store.save(dict(id=identifier, name='待修改环境', url=address,
                                            user='changed', password='changed-secret'))
                        self.assertIn(hint, str(failure.exception))
                        self.assertEqual(store.path.read_bytes(), persisted)
            corrected = store.save(dict(id=original['id'], name='保留环境',
                                        url=' https://logs.example.test:443 ',
                                        user='operator', password=''))
            self.assertEqual(corrected['url'], 'https://logs.example.test:443')
            self.assertEqual(store.resolve(original['id'])['password'], 'synthetic-secret')

    def test_explicit_default_ports_and_bracketed_ipv6_are_valid(self):
        addresses = ['http://logs.example.test:80', 'https://logs.example.test:443',
                     'https://192.0.2.10:31945', 'http://localhost:1',
                     'https://logs.example.test:65535', 'https://[2001:db8::1]:31945']
        with tempfile.TemporaryDirectory() as temporary:
            store = CollectorEnvironments(temporary)
            for position, address in enumerate(addresses):
                with self.subTest(url=address):
                    item = store.save(dict(name='环境' + str(position), url=address,
                                           user='admin', password='synthetic-secret'))
                    self.assertEqual(item['url'], address)


if __name__ == '__main__':
    unittest.main()
