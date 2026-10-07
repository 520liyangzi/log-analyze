"""Managed project synchronization using real Git repositories and no network."""
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

import chat_projects
from chat_projects import ChatProjects, query_project


def git(root, *args):
    return subprocess.run(
        ['git', '-C', str(root), *args], check=True, capture_output=True,
        text=True, encoding='utf-8',
    ).stdout.strip()


class ManagedChatProjectsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / 'data'
        self.remotes = self.root / 'remotes'
        self.remotes.mkdir()
        self.config = self.root / 'gitconfig'
        self.config.touch()
        self.env = mock.patch.dict(os.environ, {
            'GIT_CONFIG_GLOBAL': str(self.config),
            'GIT_CONFIG_NOSYSTEM': '1',
            'GIT_CONFIG_COUNT': '0',
            'GIT_CONFIG_PARAMETERS': '',
            # A bad fixture URL must fail locally, never contact the network.
            'GIT_ALLOW_PROTOCOL': 'file',
            'GIT_TERMINAL_PROMPT': '0',
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        destination = self.remotes.as_uri() + '/'
        for prefix in (
            'https://git.fixture.invalid/',
            'http://git.fixture.invalid/',
            'ssh://git@git.fixture.invalid/',
            'ssh://git.fixture.invalid/',
            'deploy@git.fixture.invalid:',
        ):
            git(self.root, 'config', '--file', str(self.config), '--add',
                'url.' + destination + '.insteadOf', prefix)
        self.managers = []
        self.addCleanup(self.close_managers)
        self.projects = self.new_manager()

    def close_managers(self):
        for manager in self.managers:
            manager.close()
            manager.pool.shutdown(wait=True)

    def new_manager(self):
        manager = ChatProjects(self.data)
        self.managers.append(manager)
        return manager

    def remote(self, namespace='team', name='widget', marker='original marker'):
        remote = self.remotes / namespace / (name + '.git')
        remote.mkdir(parents=True)
        git(remote, 'init', '--bare', '-b', 'main')
        writer = self.root / ('writer-' + namespace + '-' + name)
        writer.mkdir()
        git(writer, 'init', '-b', 'main')
        git(writer, 'config', 'user.email', 'fixture@example.invalid')
        git(writer, 'config', 'user.name', 'Git fixture')
        (writer / 'Service.java').write_text('class Service {\n  // ' + marker + '\n}\n', 'utf-8')
        git(writer, 'add', 'Service.java')
        git(writer, 'commit', '-m', 'initial fixture')
        git(writer, 'remote', 'add', 'origin', str(remote))
        git(writer, 'push', '-u', 'origin', 'main')
        return {
            'url': 'https://git.fixture.invalid/' + namespace + '/' + name + '.git',
            'remote': remote, 'writer': writer, 'commit': git(writer, 'rev-parse', 'HEAD'),
        }

    def wait(self, job, expected='ready', manager=None):
        manager = manager or self.projects
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = manager.status(job['id'])
            if result['state'] != 'running':
                if expected is not None:
                    self.assertEqual(result['state'], expected, result)
                return result
            time.sleep(.01)
        self.fail('Managed Git synchronization did not finish: ' + str(job))

    def sync(self, url, expected='ready', manager=None):
        manager = manager or self.projects
        return self.wait(manager.sync({'remote_url': url}), expected, manager)

    def snapshot(self, job, branch='origin/main'):
        return self.projects.snapshot({'sync_id': job['id'], 'branch': branch})

    def expected_root(self, url, name='widget'):
        digest = hashlib.sha256(url.encode('utf-8')).hexdigest()[:12]
        # Windows tempfile may return RUNNER~1 while the managed path resolves
        # to runneradmin. Compare canonical locations, retaining the alias as
        # the input so this still exercises normalization by ChatProjects.
        return (self.data / 'projects' / (name + '-' + digest)).resolve()

    def test_clone_is_managed_and_repository_catalog_survives_restart(self):
        fixture = self.remote()
        initial_catalog = self.projects.repositories()
        self.assertEqual(Path(initial_catalog['storage_path']), (self.data / 'projects').resolve())
        self.assertEqual(initial_catalog['repositories'], [])

        job = self.sync(fixture['url'])
        self.assertTrue({
            'id', 'state', 'message', 'root', 'remote_url', 'repository_id',
            'branches', 'current', 'updated_at',
        }.issubset(job), job)
        root = Path(job['root'])
        self.assertEqual(root, self.expected_root(fixture['url']))
        self.assertEqual(job['remote_url'], fixture['url'])
        self.assertEqual(job['current'], 'origin/main')
        self.assertEqual(job['branches'], ['origin/main'])
        self.assertEqual(git(root, 'config', '--get', 'remote.origin.url'), fixture['url'])
        self.assertEqual(git(root, 'rev-parse', 'origin/main'), fixture['commit'])

        catalog = self.projects.repositories()
        self.assertEqual(len(catalog['repositories']), 1)
        row = catalog['repositories'][0]
        self.assertTrue({'id', 'name', 'remote_url', 'root', 'updated_at'}.issubset(row), row)
        self.assertEqual(row['id'], job['repository_id'])
        self.assertEqual(row['name'], 'widget')
        self.assertEqual(row['remote_url'], fixture['url'])
        self.assertEqual(Path(row['root']), root)
        self.assertTrue(row['updated_at'])

        self.projects.close()
        self.projects.pool.shutdown(wait=True)
        self.projects = self.new_manager()
        restored = self.projects.repositories()['repositories']
        self.assertEqual(restored, catalog['repositories'])
        again = self.sync(fixture['url'])
        self.assertEqual(again['root'], str(root))
        self.assertEqual(again['repository_id'], row['id'])
        self.assertEqual(len(self.projects.repositories()['repositories']), 1)

    def test_remote_default_and_only_origin_branches_are_selectable(self):
        fixture = self.remote()
        git(fixture['writer'], 'branch', 'release')
        git(fixture['writer'], 'push', 'origin', 'release')
        git(fixture['remote'], 'symbolic-ref', 'HEAD', 'refs/heads/release')
        first = self.sync(fixture['url'])
        self.assertEqual(first['current'], 'origin/release')
        self.assertEqual(set(first['branches']), {'origin/main', 'origin/release'})

        root = Path(first['root'])
        git(root, 'branch', 'local-only', 'origin/main')
        git(root, 'update-ref', 'refs/remotes/upstream/main', fixture['commit'])
        job = self.sync(fixture['url'])
        self.assertEqual(job['current'], 'origin/release')
        self.assertEqual(set(job['branches']), {'origin/main', 'origin/release'})
        for branch in ('main', 'local-only', 'upstream/main', 'origin/HEAD', fixture['commit']):
            with self.subTest(branch=branch), self.assertRaises(ValueError):
                self.snapshot(job, branch)
        self.assertEqual(self.snapshot(job, 'origin/release')['commit'], fixture['commit'])

    def test_same_repository_name_at_different_urls_is_isolated(self):
        alpha = self.remote('alpha', marker='alpha source')
        beta = self.remote('beta', marker='beta source')
        jobs = [self.sync(fixture['url']) for fixture in (alpha, beta)]
        self.assertNotEqual(jobs[0]['root'], jobs[1]['root'])
        self.assertNotEqual(jobs[0]['repository_id'], jobs[1]['repository_id'])
        for fixture, job, marker in zip((alpha, beta), jobs, ('alpha source', 'beta source')):
            self.assertEqual(Path(job['root']), self.expected_root(fixture['url']))
            result = query_project(self.snapshot(job), 'project_read', {'path': 'Service.java'})
            self.assertIn(marker, result['content'])
        rows = self.projects.repositories()['repositories']
        self.assertEqual({row['remote_url'] for row in rows}, {alpha['url'], beta['url']})

    def test_fetch_updates_remote_code_and_prunes_deleted_branches(self):
        fixture = self.remote()
        git(fixture['writer'], 'branch', 'obsolete')
        git(fixture['writer'], 'push', 'origin', 'obsolete')
        first = self.sync(fixture['url'])
        snapshot = self.snapshot(first)
        self.assertIn('origin/obsolete', first['branches'])
        (fixture['writer'] / 'Service.java').write_text('class Service { /* new source */ }\n', 'utf-8')
        git(fixture['writer'], 'commit', '-am', 'new source')
        git(fixture['writer'], 'push', 'origin', 'main', ':obsolete')

        latest = self.sync(fixture['url'])
        self.assertEqual(latest['root'], first['root'])
        self.assertEqual(latest['branches'], ['origin/main'])
        new_snapshot = self.snapshot(latest)
        self.assertNotEqual(new_snapshot['commit'], snapshot['commit'])
        self.assertEqual(new_snapshot['commit'], git(fixture['writer'], 'rev-parse', 'HEAD'))
        self.assertIn('new source', query_project(new_snapshot, 'project_read', {'path': 'Service.java'})['content'])
        self.assertIn('original marker', query_project(snapshot, 'project_read', {'path': 'Service.java'})['content'])

    def test_snapshot_keeps_force_pushed_commit_alive_after_git_gc(self):
        fixture = self.remote()
        first = self.sync(fixture['url'])
        snapshot = self.snapshot(first)
        root = Path(snapshot['project_root'])
        self.assertEqual(root, Path(first['root']))
        self.assertEqual(snapshot['commit'], fixture['commit'])

        git(fixture['writer'], 'checkout', '--orphan', 'replacement')
        (fixture['writer'] / 'Service.java').write_text('class Replacement {}\n', 'utf-8')
        git(fixture['writer'], 'add', 'Service.java')
        git(fixture['writer'], 'commit', '-m', 'replace all history')
        git(fixture['writer'], 'push', '--force', 'origin', 'HEAD:main')
        latest = self.sync(fixture['url'])
        self.assertNotEqual(self.snapshot(latest)['commit'], snapshot['commit'])

        # A normal clone's local main branch must not accidentally preserve the
        # old commit and hide a missing snapshot retention reference.
        git(root, 'update-ref', 'refs/heads/main', git(root, 'rev-parse', 'origin/main'))
        retained = git(root, 'for-each-ref', '--format=%(refname)', '--points-at', snapshot['commit']).splitlines()
        self.assertTrue(any(not ref.startswith(('refs/heads/', 'refs/remotes/')) for ref in retained), retained)
        git(root, 'reflog', 'expire', '--expire=now', '--all')
        git(root, 'gc', '--prune=now')
        result = query_project(snapshot, 'project_read', {'path': 'Service.java'})
        self.assertEqual(result['commit'], fixture['commit'])
        self.assertIn('original marker', result['content'])

    def test_failed_clone_leaves_no_partial_repository_and_retry_succeeds(self):
        url = 'https://git.fixture.invalid/retry/widget.git'
        self.sync(url, expected='failed')
        self.assertFalse(self.expected_root(url).exists())
        catalog = self.projects.repositories()
        self.assertEqual(catalog['repositories'], [])
        storage = Path(catalog['storage_path'])
        if storage.exists():
            self.assertEqual([path for path in storage.iterdir() if path.is_dir()], [])

        fixture = self.remote('retry')
        ready = self.sync(url)
        self.assertEqual(Path(ready['root']), self.expected_root(url))
        self.assertEqual(self.snapshot(ready)['commit'], fixture['commit'])
        self.assertEqual(len(self.projects.repositories()['repositories']), 1)

    def test_failed_fetch_invalidates_old_sync_but_existing_snapshot_remains_readable(self):
        fixture = self.remote()
        old_job = self.sync(fixture['url'])
        snapshot = self.snapshot(old_job)
        unavailable = fixture['remote'].with_name('unavailable.git')
        fixture['remote'].rename(unavailable)
        failed = self.sync(fixture['url'], expected='failed')
        with self.assertRaises(ValueError):
            self.snapshot(old_job)
        with self.assertRaises(ValueError):
            self.snapshot(failed)
        self.assertIn('original marker', query_project(snapshot, 'project_read', {'path': 'Service.java'})['content'])

        unavailable.rename(fixture['remote'])
        retry = self.sync(fixture['url'])
        self.assertEqual(retry['repository_id'], old_job['repository_id'])
        self.assertEqual(self.snapshot(retry)['commit'], fixture['commit'])
        with self.assertRaises(ValueError):
            self.snapshot(old_job)

    def test_new_sync_immediately_invalidates_previous_ready_job(self):
        fixture = self.remote()
        previous = self.sync(fixture['url'])
        fetch_started, release = threading.Event(), threading.Event()
        real_run_git = chat_projects.run_git

        def controlled_git(root, *args, **kwargs):
            if args and args[0] == 'fetch':
                fetch_started.set()
                if not release.wait(5):
                    raise RuntimeError('Test did not release the pending fetch')
            return real_run_git(root, *args, **kwargs)

        with mock.patch.object(chat_projects, 'run_git', side_effect=controlled_git):
            current = self.projects.sync({'remote_url': fixture['url']})
            try:
                self.assertTrue(fetch_started.wait(5))
                with self.assertRaises(ValueError):
                    self.snapshot(previous)
            finally:
                release.set()
            self.wait(current)

    def test_concurrent_sync_of_same_url_clones_once(self):
        fixture = self.remote()
        clone_started, release = threading.Event(), threading.Event()
        real_run_git = chat_projects.run_git
        clones = []
        counter_lock = threading.Lock()

        def controlled_git(root, *args, **kwargs):
            if args and args[0] == 'clone':
                with counter_lock:
                    clones.append(args)
                clone_started.set()
                if not release.wait(5):
                    raise RuntimeError('Test did not release the pending clone')
            return real_run_git(root, *args, **kwargs)

        with mock.patch.object(chat_projects, 'run_git', side_effect=controlled_git):
            first = self.projects.sync({'remote_url': fixture['url']})
            try:
                self.assertTrue(clone_started.wait(5))
                second = self.projects.sync({'remote_url': fixture['url']})
            finally:
                release.set()
            self.wait(first, expected=None)
            ready = self.wait(second)
        self.assertEqual(len(clones), 1)
        self.assertEqual(Path(ready['root']), self.expected_root(fixture['url']))
        self.assertEqual(len(self.projects.repositories()['repositories']), 1)

    def test_supported_remote_schemes_use_managed_storage(self):
        fixture = self.remote()
        for remote in (
            fixture['url'],
            'http://git.fixture.invalid/team/widget.git',
            'ssh://git@git.fixture.invalid/team/widget.git',
            'ssh://git.fixture.invalid/team/widget.git',
            'deploy@git.fixture.invalid:team/widget.git',
        ):
            with self.subTest(remote=remote):
                job = self.sync(remote)
                self.assertEqual(Path(job['root']), self.expected_root(remote))
                self.assertEqual(self.snapshot(job)['commit'], fixture['commit'])

    def test_local_paths_credentials_and_url_parameters_are_rejected(self):
        for remote in (
            '', str(self.root / 'repo'), './repo', '../repo', 'C:\\projects\\repo',
            'file:///tmp/repo.git', 'git://git.fixture.invalid/repo.git',
            'ext::git upload-pack /tmp/repo',
            'ext::private-command', '--upload-pack=private-command',
            'ssh://-oProxyCommand=private-command/repo.git',
            'ssh://git@host;private-command/repo.git',
            'https://user@git.fixture.invalid/repo.git',
            'https://user:secret@git.fixture.invalid/repo.git',
            'ssh://git:secret@git.fixture.invalid/repo.git',
            'https://git.fixture.invalid/repo.git?token=secret',
            'https://git.fixture.invalid/repo.git#secret',
            'ssh://git@git.fixture.invalid/repo.git?token=secret',
            'deploy@git.fixture.invalid:repo.git#secret',
            'https://git.fixture.invalid/repo.git\nother',
        ):
            with self.subTest(remote=remote), self.assertRaises(ValueError):
                self.projects.sync({'remote_url': remote})
        for body in (
            {'path': str(self.root / 'arbitrary')},
            {'remote_url': 'https://git.fixture.invalid/team/widget.git', 'path': str(self.root / 'arbitrary')},
        ):
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.projects.sync(body)
        self.assertEqual(self.projects.repositories()['repositories'], [])
        self.assertFalse((self.root / 'arbitrary').exists())

    def test_legacy_project_snapshots_remain_readable(self):
        fixture = self.remote()
        legacy = {'project_root': str(fixture['writer']), 'branch': 'main', 'commit': fixture['commit']}
        result = query_project(legacy, 'project_read', {'path': 'Service.java'})
        self.assertIn('original marker', result['content'])
        self.assertEqual(result['commit'], fixture['commit'])


if __name__ == '__main__':
    unittest.main()
