"""Run the platform's offline Python and browser suites on Windows, macOS or Linux."""
from pathlib import Path
import os
import platform
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory

SKILL = Path(__file__).resolve().parents[1]


def main():
    node = shutil.which('node')
    if not node or not shutil.which('npm') or not shutil.which('git'):
        sys.exit('Install Node.js, npm and Git on this test host.')
    print(f'Platform: {platform.system()} {platform.machine()}, Python {platform.python_version()}', flush=True)
    env = {**os.environ, 'PYTHONUTF8': '1', 'PYTHONDONTWRITEBYTECODE': '1',
           'GIT_TERMINAL_PROMPT': '0', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_COUNT': '0'}
    with TemporaryDirectory() as directory:
        # Native text fixtures use platform line endings. Individual repositories
        # can still override this default when testing exact index/worktree bytes.
        defaults = Path(directory) / 'gitconfig'
        defaults.write_bytes(b'[core]\n\tautocrlf = input\n')
        env['GIT_CONFIG_GLOBAL'] = str(defaults)
        python = subprocess.run([sys.executable, '-B', '-m', 'unittest', 'discover',
                                 '-s', str(SKILL / 'tests')], env=env, cwd=SKILL.parents[2])
        browser = subprocess.run([node, '--test', *map(str, sorted((SKILL / 'tests').glob('test_*.cjs')))],
                                 env=env, cwd=SKILL.parents[2])
    return int(bool(python.returncode or browser.returncode))


if __name__ == '__main__':
    sys.exit(main())
