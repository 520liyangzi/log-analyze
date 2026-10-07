import json
from pathlib import Path
import tempfile
import unittest

from collector_environments import CollectorEnvironments


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


if __name__ == '__main__':
    unittest.main()
