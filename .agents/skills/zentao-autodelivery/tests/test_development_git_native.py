from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import development_git_native as native


class NativeFastForwardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'repo'
        self.root.mkdir()
        self.deadline = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
        self.git('init', '-q', '-b', 'target')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('config', 'core.autocrlf', 'false')
        for name in ('partial', 'replace', 'old', 'hook-file'):
            (self.root / name).write_bytes(b'base')
        self.git('add', '.')
        self.git('commit', '-qm', 'base')
        self.before = self.git('rev-parse', 'HEAD')
        self.git('switch', '-qc', 'candidate')
        self.git('mv', 'old', 'new')
        self.git('commit', '-qm', 'candidate')
        self.after = self.git('rev-parse', 'HEAD')
        self.git('switch', '-q', 'target')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], stderr=subprocess.PIPE).decode().strip()

    def fingerprint(self):
        return native.fingerprint(self.root, self.deadline)

    def planned(self):
        return native.native_fingerprint_if_disjoint(self.root, self.before, self.after, self.deadline)

    def test_fast_forward_preserves_binary_partial_staging_ita_and_recreated_deleted_path(self):
        partial = self.root / 'partial'
        replacement = self.root / 'replace'
        intent = self.root / 'intent'
        pending = b'first staged\x00\xff'
        partial.write_bytes(pending)
        self.git('add', 'partial')
        partial.write_bytes(b'other\r\n\x00')
        self.git('rm', '-q', 'replace')
        replacement.write_bytes(b'new untracked at staged deletion')
        intent.write_bytes(b'intended')
        self.git('add', '-N', 'intent')
        before = self.planned()
        self.assertRegex(before, r'^[a-f0-9]{64}$')
        self.git('merge', '--ff-only', self.after)
        self.assertEqual(self.fingerprint(), before)
        (self.root / 'untracked-output').write_text('log')
        self.assertEqual(self.fingerprint(), before)
        replacement.write_bytes(b'another replacement')
        self.assertNotEqual(self.fingerprint(), before)
        replacement.write_bytes(b'new untracked at staged deletion')
        intent.write_bytes(b'new intent')
        self.assertNotEqual(self.fingerprint(), before)
        intent.write_bytes(b'intended')
        self.git('add', 'partial')
        self.assertNotEqual(self.fingerprint(), before)

    def test_original_hook_editing_an_additional_tracked_file_is_detected(self):
        (self.root / 'partial').write_bytes(b'user draft')
        before = self.planned()
        hook = self.root / '.git/hooks/post-merge'
        hook.write_bytes(b'#!/bin/sh\nprintf hook >hook-file\n')
        hook.chmod(0o755)
        self.git('merge', '--ff-only', self.after)
        self.assertEqual((self.root / 'hook-file').read_bytes(), b'hook')
        self.assertNotEqual(before, self.fingerprint())

    def test_both_rename_paths_case_and_ancestor_are_reserved(self):
        (self.root / 'old').write_bytes(b'local')
        self.assertIsNone(self.planned())
        self.git('restore', 'old')
        (self.root / 'NEW').mkdir()
        (self.root / 'NEW/child').write_bytes(b'draft')
        self.git('add', 'NEW/child')
        self.assertIsNone(self.planned())

    def test_disjoint_link_or_junction_is_preserved_without_hashing_its_target(self):
        link = self.root / 'partial'
        link.unlink()
        outside = self.root.parent / 'outside'
        outside.mkdir()
        (outside / 'private').write_bytes(b'outside')
        if os.name == 'nt':
            import _winapi
            _winapi.CreateJunction(str(outside), str(link))
        else:
            link.symlink_to(outside, target_is_directory=True)
        before = self.planned()
        self.assertIsNotNone(before)
        (outside / 'private').write_bytes(b'external change')
        self.git('merge', '--ff-only', self.after)
        self.assertEqual(self.fingerprint(), before)
        self.assertTrue(link.samefile(outside))

    def test_gitlink_tracks_nested_index_and_raw_bytes_across_fast_forward(self):
        module = self.root / 'module'
        module.mkdir()
        self.git('-C', str(module), 'init', '-q', '-b', 'lib')
        (module / 'source').write_bytes(b'original')
        self.git('-C', str(module), 'add', 'source')
        self.git('-C', str(module), '-c', 'user.name=Test', '-c', 'user.email=fixture@example.invalid',
                 'commit', '-qm', 'library')
        self.git('add', 'module')
        self.git('commit', '-qm', 'add module')
        self.before = self.git('rev-parse', 'HEAD')
        self.git('switch', '-qc', 'with-module')
        (self.root / 'old').write_bytes(b'candidate')
        self.git('commit', '-qam', 'candidate')
        self.after = self.git('rev-parse', 'HEAD')
        self.git('switch', '-q', 'target')
        (module / 'source').write_bytes(b'index')
        self.git('-C', str(module), 'add', 'source')
        (module / 'source').write_bytes(b'work\x00\xff')
        before = self.planned()
        self.assertIsNotNone(before)
        self.git('merge', '--ff-only', self.after)
        self.assertEqual(before, self.fingerprint())
        (module / 'source').write_bytes(b'other\x00\xff')
        self.assertNotEqual(before, self.fingerprint())
