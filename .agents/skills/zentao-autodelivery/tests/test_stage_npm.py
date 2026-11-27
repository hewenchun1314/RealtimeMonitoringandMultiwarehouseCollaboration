import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from stage_npm import prepare


class StageNpmTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.cache = self.root / 'cache'
        self.installations = []

    def worktree(self, name, archive=b'local-package-v1', scripts=None):
        path = self.root / name
        path.mkdir()
        (path / 'package.json').write_text(json.dumps({'name': 'fixture', 'scripts': scripts or {}}), encoding='utf-8')
        (path / 'package-lock.json').write_text(json.dumps({'lockfileVersion': 3, 'packages': {
            'node_modules/package': {'resolved': 'file:vendor/package.tgz'},
        }}), encoding='utf-8')
        (path / 'vendor').mkdir()
        (path / 'vendor' / 'package.tgz').write_bytes(archive)
        return path

    def install(self, command, *, cwd, check):
        self.assertEqual(command[-2:], ['ci', '--prefer-offline'])
        self.assertTrue(check)
        self.installations.append(cwd)
        installed = cwd / 'node_modules'
        installed.mkdir(exist_ok=True)
        (installed / '.package-lock.json').write_text('fixture install', encoding='utf-8')
        (installed / 'example.js').write_text('installed', encoding='utf-8')

    def prepare(self, path):
        with (patch('stage_npm.subprocess.check_output', side_effect=[b'fixture-node', b'fixture-npm', b'{}']),
              patch('stage_npm.subprocess.run', side_effect=self.install)):
            prepare(path, self.cache)

    def test_new_worktree_restores_matching_dependencies_without_reinstalling(self):
        first = self.worktree('first')
        second = self.worktree('second')
        self.prepare(first)
        (first / 'node_modules' / 'example.js').write_text('edited by first agent', encoding='utf-8')
        self.prepare(second)
        self.assertEqual(self.installations, [first])
        self.assertFalse((second / 'node_modules').is_symlink())
        self.assertEqual((second / 'node_modules' / 'example.js').read_text(encoding='utf-8'), 'installed')

    def test_changed_vendored_archive_invalidates_snapshot_even_if_lockfile_is_unchanged(self):
        first = self.worktree('first')
        second = self.worktree('second', archive=b'local-package-v2')
        self.prepare(first)
        self.prepare(second)
        self.assertEqual(self.installations, [first, second])

    def test_root_install_lifecycle_runs_in_each_worktree(self):
        first = self.worktree('first', scripts={'prepare': 'project-specific'})
        second = self.worktree('second', scripts={'prepare': 'project-specific'})
        self.prepare(first)
        self.prepare(second)
        self.assertEqual(self.installations, [first, second])
