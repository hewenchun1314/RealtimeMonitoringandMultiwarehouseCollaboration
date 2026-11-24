import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'ocr_review.py'
OCR = Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs' / 'ocr' / '1.14.2' / 'ocr.exe'


class OCRReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / 'candidate repo'
        self.repo.mkdir()
        self.output = self.root / 'evidence' / 'ocr.json'
        self.git('init', '-b', 'main')
        (self.repo / '.opencodereview').mkdir()
        (self.repo / '.opencodereview' / 'rule.json').write_text(json.dumps({
            'rules': [{'path': '**/*.py', 'rule': 'Verify retry handling.'}],
        }), encoding='utf-8')
        self.base = self.commit('baseline')
        (self.repo / 'service handler.py').write_text('retries = 1\n', encoding='utf-8')
        self.candidate = self.commit('candidate')
        (self.repo / 'uncommitted.py').write_text('draft = True\n', encoding='utf-8')

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), *args], check=True, capture_output=True,
                              encoding='utf-8').stdout.strip()

    def commit(self, message):
        self.git('add', '--all')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                 '-c', 'commit.gpgsign=false', 'commit', '-m', message)
        return self.git('rev-parse', 'HEAD')

    def review(self, executable):
        result = subprocess.run(
            [sys.executable, '-B', str(SCRIPT), '--repo', str(self.repo), '--base', self.base,
             '--candidate', self.candidate, '--output', str(self.output), '--ocr-exe', str(executable)],
            capture_output=True, encoding='utf-8', timeout=30,
        )
        self.assertTrue(self.output.exists(), result.stderr)
        return result, json.loads(self.output.read_text(encoding='utf-8'))

    def test_missing_ocr_records_unavailable_without_changing_worktree(self):
        before = self.git('status', '--porcelain')
        result, report = self.review(self.root / 'missing.exe')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(report['status'], 'unavailable')
        self.assertEqual(report['candidate'], self.candidate)
        self.assertEqual(before, self.git('status', '--porcelain'))

    @unittest.skipUnless(OCR.is_file(), 'Requires the pinned Windows OCR binary')
    def test_delegates_only_candidate_files_with_matching_rules(self):
        result, report = self.review(OCR)
        self.assertEqual(result.returncode, 0, report)
        self.assertEqual(report['status'], 'prepared')
        self.assertEqual(report['preview']['merge_base'], self.base)
        self.assertEqual([entry['path'] for entry in report['preview']['reviewable_files']],
                         ['service handler.py'])
        self.assertIn('Verify retry handling.', report['rules'][0]['rule'])

    @unittest.skipUnless(OCR.is_file(), 'Requires the pinned Windows OCR binary')
    def test_identical_versions_need_no_rule_lookup(self):
        self.candidate = self.base
        result, report = self.review(OCR)
        self.assertEqual(result.returncode, 0, report)
        self.assertEqual(report['preview']['reviewable_files'], [])
        self.assertEqual(report['rules'], [])


if __name__ == '__main__':
    unittest.main()
