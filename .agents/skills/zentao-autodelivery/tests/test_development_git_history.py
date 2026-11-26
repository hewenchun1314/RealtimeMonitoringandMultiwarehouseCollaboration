"""Recovery of disposable worktrees used to resolve a real divergent merge."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import development_git_history as history
from development_git_overlay import OverlayConflict


class HistoryCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.repo, self.parent = base / 'repo', base / 'item'
        self.repo.mkdir()
        self.parent.mkdir()
        self.deadline = (datetime.now(timezone.utc) + timedelta(minutes=3)).isoformat()
        self.run_git('init', '-q', '-b', 'target')
        self.run_git('config', 'user.name', 'Fixture')
        self.run_git('config', 'user.email', 'fixture@example.invalid')
        self.run_git('config', 'core.autocrlf', 'false')
        (self.repo / '.gitignore').write_text('*.ignored\n', encoding='utf-8')
        (self.repo / '.gitattributes').write_bytes(b'app text eol=lf\n')
        file = self.repo / 'app'
        file.write_bytes(b'base\n')
        self.run_git('add', '.')
        self.run_git('commit', '-qm', 'base')
        self.run_git('switch', '-qc', 'item')
        file.write_bytes(b'candidate\n')
        self.run_git('commit', '-qam', 'candidate')
        self.candidate = self.run_git('rev-parse', 'HEAD')
        self.run_git('switch', '-q', 'target')
        file.write_bytes(b'target\n')
        self.run_git('commit', '-qam', 'target')
        self.tip = self.run_git('rev-parse', 'HEAD')
        with self.assertRaises(OverlayConflict) as error:
            self.resolve('inspect', {})
        self.decisions = {error.exception.receipt['conflict_id']: {
            'files': {'app': 'resolved\n'}, 'verification': 'Compared the two intended edits'}}

    def run_git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], stderr=subprocess.PIPE).decode().strip()

    def resolve(self, label, decisions):
        journal = self.parent.parent / label
        journal.mkdir()
        return history.reviewed_history_merge(self.repo, self.tip, self.candidate, journal,
                                              self.parent, decisions, self.deadline)

    def test_failed_commit_hook_removes_its_checkout_before_retry(self):
        hook = self.repo / '.git/hooks/pre-commit'
        hook.write_bytes(b'#!/bin/sh\nexit 1\n')
        hook.chmod(0o755)
        with self.assertRaises(ValueError):
            self.resolve('hook-failure', self.decisions)
        self.assertEqual(self.run_git('worktree', 'list', '--porcelain').count('worktree '), 1)
        self.assertEqual(list(self.parent.iterdir()), [])
        hook.unlink()
        merged = self.resolve('retry', self.decisions)
        self.assertEqual(self.run_git('show', merged + ':app'), 'resolved')
        self.assertEqual(self.run_git('rev-parse', 'HEAD'), self.tip)

    def test_reviewed_text_uses_the_target_paths_clean_filter(self):
        self.decisions[next(iter(self.decisions))]['files']['app'] = 'resolved\r\n'
        merged = self.resolve('windows-resolution', self.decisions)
        self.assertEqual(subprocess.check_output(['git', '-C', str(self.repo), 'show', merged + ':app']), b'resolved\n')

    def interrupt(self):
        real = history.git
        def cut_after_merge(root, *args, **kwargs):
            if args[:3] == ('rev-parse', '--verify', 'MERGE_HEAD'):
                raise InterruptedError('merge stopped')
            return real(root, *args, **kwargs)
        with patch.object(history, 'git', side_effect=cut_after_merge), \
                patch.object(history, 'remove_history_checkout', side_effect=InterruptedError('worker stopped')), \
                self.assertRaises(InterruptedError):
            self.resolve('stopped', self.decisions)
        return next(path for path in self.parent.iterdir() if path.is_dir())

    def test_reclaims_unmodified_conflict_index_and_auto_merge_bytes(self):
        tree = self.interrupt()
        self.assertIn('<<<<<<<', (tree / 'app').read_text())
        merged = self.resolve('resume', self.decisions)
        self.assertEqual(self.run_git('show', merged + ':app'), 'resolved')
        self.assertFalse(tree.exists())
        self.assertEqual(self.run_git('worktree', 'list', '--porcelain').count('worktree '), 1)

    def test_retains_replacement_directory_at_the_previous_checkout_path(self):
        tree = self.interrupt()
        tree.rename(tree.with_name('original-checkout'))
        tree.mkdir()
        note = tree / 'user-file'
        note.write_bytes(b'keep')
        with self.assertRaisesRegex(ValueError, 'ownership changed'):
            self.resolve('foreign-directory', self.decisions)
        self.assertEqual(note.read_bytes(), b'keep')

    def test_preserves_changed_conflict_bytes_staged_content_and_ignored_files(self):
        tree = self.interrupt()
        file = tree / 'app'
        original = file.read_bytes()
        file.write_bytes(b'edited during recovery\n')
        with self.assertRaisesRegex(ValueError, 'Retained'):
            self.resolve('edited', self.decisions)
        self.assertEqual(file.read_bytes(), b'edited during recovery\n')
        subprocess.run(['git', '-C', str(tree), 'add', '--', 'app'], check=True)
        file.write_bytes(original)
        with self.assertRaisesRegex(ValueError, 'Retained'):
            self.resolve('staged', self.decisions)
        self.assertEqual(subprocess.check_output(['git', '-C', str(tree), 'show', ':app']), b'edited during recovery\n')
        subprocess.run(['git', '-C', str(tree), 'restore', '--staged', '--source=' + self.tip, '--', 'app'], check=True)
        file.write_bytes(b'target\n')
        ignored = tree / 'mine.ignored'
        ignored.write_bytes(b'preserve')
        with self.assertRaisesRegex(ValueError, 'Retained'):
            self.resolve('ignored', self.decisions)
        self.assertEqual(ignored.read_bytes(), b'preserve')
