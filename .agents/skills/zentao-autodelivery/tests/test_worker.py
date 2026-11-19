from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from worker import command


class WorkerTests(unittest.TestCase):
    def test_worker_uses_never_for_new_and_resumed_chats(self):
        cli = Path('codex.exe')
        chat = '01a1f1df-ddb1-77f1-aa07-a96f2a74d43a'
        for operation in ('prepare', 'run', 'probe'):
            argv = command(cli, operation, chat)
            self.assertEqual(argv[:4], [str(cli), '--ask-for-approval', 'never', 'exec'])
            self.assertEqual(argv[-1], '-')
            self.assertIn('--json', argv)
            self.assertEqual('--ephemeral' in argv, operation == 'probe')
            self.assertEqual('resume' in argv, operation == 'run')
            self.assertEqual(chat in argv, operation == 'run')


if __name__ == '__main__':
    unittest.main()
