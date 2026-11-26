"""Real Git integration tests for preserving the index and working copy separately."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import development_git_overlay as overlay


class OverlayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'repo'
        self.root.mkdir()
        self.directory = self.root.parent / 'overlay'
        self.deadline = (datetime.now(timezone.utc) + timedelta(minutes=3)).isoformat()
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Overlay test')
        self.git('config', 'user.email', 'overlay@example.invalid')
        self.git('config', 'core.autocrlf', 'false')
        self.original = ''.join(f'line {i}\n' for i in range(40))
        self.file = self.root / 'options'
        self.file.write_bytes(self.original.encode())
        self.git('add', 'options')
        self.git('commit', '-qm', 'baseline')
        self.before = self.git('rev-parse', 'HEAD')
        self.git('switch', '-qc', 'item')
        self.file.write_bytes(self.original.replace('line 2\n', 'candidate\n').encode())
        self.git('commit', '-qam', 'candidate')
        self.candidate = self.git('rev-parse', 'HEAD')
        self.git('switch', '-q', 'main')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], stderr=subprocess.PIPE).decode().strip()

    def prepare(self, resolutions=None):
        return overlay.prepare_overlay(self.root, self.before, self.candidate, self.directory, self.deadline, resolutions=resolutions)

    def call(self, fn):
        return fn(self.root, self.directory, self.deadline)

    def test_partial_staging_and_repeatable_restoration(self):
        index = self.original.replace('line 17\n', 'staged\n')
        work = index.replace('line 32\n', 'unstaged\n')
        self.file.write_bytes(index.encode())
        self.git('add', 'options')
        self.file.write_bytes(work.encode())
        self.prepare()
        self.call(overlay.park_overlay)
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.call(overlay.park_overlay)
        self.git('merge', '--ff-only', self.candidate)
        self.call(overlay.restore_overlay)
        self.call(overlay.restore_overlay)
        self.call(overlay.verify_overlay)
        self.assertEqual(self.git('show', ':options'), index.replace('line 2\n', 'candidate\n').strip())
        self.assertEqual(self.file.read_bytes(), work.replace('line 2\n', 'candidate\n').encode())

    def test_conflict_has_three_inputs_and_only_resolution_enters_working_copy(self):
        local = self.original.replace('line 2\n', 'local\n')
        self.file.write_bytes(local.encode())
        with self.assertRaisesRegex(ValueError, 'conflict'):
            self.prepare()
        self.assertEqual(self.file.read_bytes(), local.encode())
        conflict = next((self.directory / 'conflicts' / 'worktree').glob('*/details.json')).parent
        self.assertEqual((conflict / 'local').read_bytes(), local.encode())
        result = local.replace('local\n', 'combined\n').encode()
        receipt = json.loads((self.directory / 'conflict.json').read_bytes())
        self.prepare({receipt['conflict_id']: {'files': {'options': result.decode()}, 'verification': 'reviewed'}})
        self.call(overlay.park_overlay)
        self.git('merge', '--ff-only', self.candidate)
        self.call(overlay.restore_overlay)
        self.assertEqual(self.file.read_bytes(), result)
        self.assertEqual(self.git('diff', '--cached', '--name-only'), '')

    def test_concurrent_edit_is_preserved(self):
        self.file.write_bytes(self.original.replace('line 32\n', 'earlier\n').encode())
        self.prepare()
        self.file.write_bytes(b'later\n')
        with self.assertRaises(ValueError):
            self.call(overlay.park_overlay)
        self.assertEqual(self.file.read_bytes(), b'later\n')

    def test_restore_original_after_aborted_merge(self):
        self.file.write_bytes(b'local bytes\r\n')
        self.prepare_conflict_if_needed()
        self.call(overlay.park_overlay)
        self.call(overlay.restore_original_overlay)
        self.assertEqual(self.file.read_bytes(), b'local bytes\r\n')

    def prepare_conflict_if_needed(self):
        try:
            self.prepare()
        except ValueError:
            receipt = json.loads((self.directory / 'conflict.json').read_bytes())
            self.prepare({receipt['conflict_id']: {'files': {'options': {'take': 'local'}}, 'verification': 'reviewed'}})

    def test_staged_rename_intent_to_add_and_untracked_file_survive(self):
        self.git('mv', 'options', 'new-name')
        intent = self.root / 'intent'
        intent.write_bytes(b'unfinished\n')
        self.git('add', '-N', 'intent')
        untouched = self.root / 'untracked'
        untouched.write_bytes(b'unrelated\r\n')
        self.prepare()
        self.call(overlay.park_overlay)
        self.assertFalse(intent.exists())
        self.assertEqual(untouched.read_bytes(), b'unrelated\r\n')
        self.git('merge', '--ff-only', self.candidate)
        self.call(overlay.restore_overlay)
        self.call(overlay.verify_overlay)
        self.assertEqual(self.git('show', ':new-name'), self.original.replace('line 2\n', 'candidate\n').strip())
        self.assertFalse(self.file.exists())
        self.assertEqual(intent.read_bytes(), b'unfinished\n')
        self.assertEqual(overlay._intent_paths(self.root, self.deadline), ['intent'])
        self.assertEqual(untouched.read_bytes(), b'unrelated\r\n')

    def test_staged_deletion_with_untracked_replacement(self):
        self.git('rm', '--cached', '--', 'options')
        self.file.write_bytes(b'untracked replacement\r\n')
        with self.assertRaises(overlay.OverlayConflict) as error:
            self.prepare()
        self.prepare({error.exception.receipt['conflict_id']: {
            'files': {'options': {'take': 'delete'}}, 'verification': 'Reviewed staged remove'}})
        self.call(overlay.park_overlay)
        self.git('merge', '--ff-only', self.candidate)
        self.call(overlay.restore_overlay)
        self.assertIn('D\toptions', self.git('diff', '--cached', '--name-status'))
        self.assertEqual(self.file.read_bytes(), b'untracked replacement\r\n')

    def test_interrupted_file_install_resumes_in_original_journal(self):
        from unittest.mock import patch

        self.file.write_bytes(self.original.replace('line 32\n', 'local\n').encode())
        self.prepare()
        self.call(overlay.park_overlay)
        self.git('merge', '--ff-only', self.candidate)
        with patch.object(overlay, '_link_move', side_effect=OSError('simulated process interruption')):
            with self.assertRaises(OSError):
                self.call(overlay.restore_overlay)
        self.call(overlay.restore_overlay)
        self.call(overlay.verify_overlay)
        self.assertIn(b'local\n', self.file.read_bytes())

    def test_dirty_submodule_is_never_checked_out_or_included_in_merge(self):
        sub = self.root / 'sub'
        sub.mkdir()
        def child(*args):
            return subprocess.check_output(['git', '-C', str(sub), *args], stderr=subprocess.PIPE).decode().strip()
        child('init', '-q', '-b', 'main')
        child('config', 'user.name', 'Child')
        child('config', 'user.email', 'child@test.invalid')
        (sub / 'child-file').write_bytes(b'first')
        child('add', 'child-file')
        child('commit', '-qm', 'first')
        self.git('add', 'sub')
        self.git('commit', '-qm', 'record gitlink')
        self.before = self.git('rev-parse', 'HEAD')
        self.git('switch', '-q', 'item')
        self.git('merge', '-q', 'main', '-m', 'combine test baseline')
        self.candidate = self.git('rev-parse', 'HEAD')
        self.git('switch', '-q', 'main')
        (sub / 'child-file').write_bytes(b'second')
        child('commit', '-qam', 'nested commit')
        second = child('rev-parse', 'HEAD')
        (sub / 'child-file').write_bytes(b'user nested edits')
        self.file.write_bytes(self.original.replace('line 32\n', 'user work\n').encode())
        self.prepare()
        self.call(overlay.park_overlay)
        self.assertTrue(self.call(overlay.matches_parked_overlay))
        self.git('merge', '--ff-only', self.candidate)
        self.call(overlay.restore_overlay)
        self.call(overlay.verify_overlay)
        self.assertEqual(child('rev-parse', 'HEAD'), second)
        self.assertEqual((sub / 'child-file').read_bytes(), b'user nested edits')

    def test_unrelated_staged_change_during_merge_is_preserved_while_restoring_local_edits(self):
        self.file.write_bytes(self.original.replace('line 32\n', 'local\n').encode())
        self.prepare()
        self.call(overlay.park_overlay)
        self.git('merge', '--ff-only', self.candidate)
        (self.root / 'hook-output').write_bytes(b'check result')
        self.git('add', 'hook-output')
        state = self.call(overlay.restore_overlay)
        self.assertTrue(state['additional_checkout_changes'])
        self.assertEqual(state['phase'], 'restored')
        self.assertEqual(self.git('show', ':hook-output'), 'check result')
        self.assertIn(b'local\n', self.file.read_bytes())

    def verify_hidden_work(self, flag, expected):
        self.git('update-index', flag, 'options')
        self.file.write_bytes(self.original.replace('line 32\n', 'hidden local edit\n').encode())
        self.assertEqual(self.git('status', '--short'), '')
        self.prepare()
        self.call(overlay.park_overlay)
        self.assertTrue(self.call(overlay.matches_parked_overlay))
        self.git('merge', '--ff-only', self.candidate)
        self.call(overlay.restore_overlay)
        self.call(overlay.verify_overlay)
        self.assertIn(b'candidate\n', self.file.read_bytes())
        self.assertIn(b'hidden local edit\n', self.file.read_bytes())
        self.assertEqual(self.git('ls-files', '-v', 'options'), expected + ' options')

    def test_assume_unchanged_edits_are_replayed_with_the_original_flag(self):
        self.verify_hidden_work('--assume-unchanged', 'h')

    def test_materialized_skip_worktree_edits_are_replayed_with_the_original_flag(self):
        self.verify_hidden_work('--skip-worktree', 'S')

    def test_native_merge_materializing_sparse_path_restores_its_absence(self):
        self.git('update-index', '--skip-worktree', 'options')
        self.file.unlink()
        self.prepare()
        self.call(overlay.park_overlay)
        self.git('merge', '--ff-only', self.candidate)
        self.call(overlay.restore_overlay)
        self.call(overlay.verify_overlay)
        self.assertFalse(self.file.exists())
        self.assertEqual(self.git('ls-files', '-v', 'options'), 'S options')

    def test_crlf_resolution_is_cleaned_for_the_path_and_restored_with_checkout_eol(self):
        self.git('config', 'core.autocrlf', 'true')
        local = self.original.replace('line 2\n', 'local\n').replace('\n', '\r\n')
        self.file.write_bytes(local.encode())
        with self.assertRaises(overlay.OverlayConflict) as error:
            self.prepare()
        resolution = local.replace('local\r\n', 'combined\r\n')
        prepared = self.prepare({error.exception.receipt['conflict_id']: {
            'files': {'options': resolution}, 'verification': 'Review supplied as Windows text'}})
        planned = subprocess.check_output(['git', '-C', str(self.root), 'show', prepared['new_work_tree'] + ':options'])
        self.assertEqual(planned, resolution.replace('\r\n', '\n').encode())
        self.call(overlay.park_overlay)
        self.git('merge', '--ff-only', self.candidate)
        self.call(overlay.restore_overlay)
        state = self.call(overlay.verify_overlay)
        canonical = subprocess.check_output(['git', '-C', str(self.root), 'show', state['new_work_tree'] + ':options'])
        self.assertEqual(canonical, resolution.replace('\r\n', '\n').encode())
        self.assertEqual(self.file.read_bytes(), resolution.encode())
        self.assertEqual(self.git('diff', '--cached', '--name-only'), '')

    def test_resolution_uses_attributes_introduced_by_the_candidate(self):
        self.git('switch', '-q', 'item')
        (self.root / '.gitattributes').write_bytes(b'options text eol=lf\n')
        self.git('add', '.gitattributes')
        self.git('commit', '-qm', 'Define attributes on candidate')
        self.candidate = self.git('rev-parse', 'HEAD')
        self.git('switch', '-q', 'main')
        local = self.original.replace('line 2\n', 'local\n')
        self.file.write_bytes(local.encode())
        with self.assertRaises(overlay.OverlayConflict) as error:
            self.prepare()
        resolution = local.replace('local\n', 'combined\n')
        self.prepare({error.exception.receipt['conflict_id']: {
            'files': {'options': resolution.replace('\n', '\r\n')}, 'verification': 'Reviewed Windows text'}})
        self.call(overlay.park_overlay)
        self.git('merge', '--ff-only', self.candidate)
        self.call(overlay.restore_overlay)
        self.call(overlay.verify_overlay)
        self.assertEqual(self.file.read_bytes(), resolution.encode())
