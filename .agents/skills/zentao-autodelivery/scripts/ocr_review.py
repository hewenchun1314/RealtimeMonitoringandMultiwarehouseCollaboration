"""Prepare per-repository code review evidence using OCR's local delegate commands."""

import argparse
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path


OCR_VERSION = '1.14.2'
COMMAND_TIMEOUT = 20


def commit_sha(value):
    if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value):
        raise argparse.ArgumentTypeError('Expected a full Git commit SHA from the workflow ledger')
    return value


def run_delegate(executable, command, scope, paths=()):
    args = [str(executable), 'delegate', command, *scope, '--format', 'json']
    if paths:
        args.extend(['--', *paths])
    result = subprocess.run(args, capture_output=True, encoding='utf-8', timeout=COMMAND_TIMEOUT,
                            env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'})
    if result.returncode:
        raise ValueError(f'OCR {command} exited with {result.returncode}: {result.stderr.strip()}')
    response = json.loads(result.stdout)
    if not isinstance(response, dict) or response.get('schema_version') != '1':
        raise ValueError(f'Unexpected OCR {command} JSON schema')
    return response


def rule_batches(paths):
    # Leave room for executable and scope arguments within Windows' command line limit.
    batch = []
    size = 0
    for path in paths:
        units = len(subprocess.list2cmdline([path]).encode('utf-16-le')) // 2 + 1
        if batch and size + units > 20000:
            yield batch
            batch = []
            size = 0
        batch.append(path)
        size += units
    if batch:
        yield batch


def prepare_report(report, executable, repo, base, candidate):
    scope = ['--repo', str(repo), '--from', base, '--to', candidate]
    preview = run_delegate(executable, 'preview', scope)
    report['preview'] = preview
    if (preview.get('mode') != 'range' or preview.get('from') != base or preview.get('to') != candidate
            or preview.get('merge_base') != base or Path(preview['repository']).resolve() != repo):
        raise ValueError('OCR preview did not match the registered repository and commit range')

    paths = [entry['path'] for entry in preview['reviewable_files']]
    for batch in rule_batches(paths):
        groups = run_delegate(executable, 'rule', scope, batch)['groups']
        if sorted(path for group in groups for path in group['files']) != sorted(batch):
            raise ValueError('OCR rule results did not cover the requested files')
        report['rules'].extend(groups)
    report['status'] = 'prepared'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--base', type=commit_sha, required=True)
    parser.add_argument('--candidate', type=commit_sha, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--ocr-exe', type=Path, default=(
        Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local'))
        / 'Programs' / 'ocr' / OCR_VERSION / 'ocr.exe'))
    args = parser.parse_args()
    repo, output = args.repo.resolve(), args.output.resolve()
    if output.is_relative_to(repo):
        parser.error('Save OCR evidence outside the candidate worktree')
    executable = args.ocr_exe.resolve()
    report = {'mode': 'delegation', 'status': 'unavailable', 'repo': str(repo), 'base': args.base,
              'candidate': args.candidate, 'ocr_exe': str(executable), 'rules': []}
    try:
        prepare_report(report, executable, repo, args.base, args.candidate)
    except subprocess.TimeoutExpired:
        report['reason'] = f'OCR delegate command timed out after {COMMAND_TIMEOUT} seconds'
    except (OSError, ValueError, KeyError, TypeError) as error:
        report['reason'] = str(error)

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=output.parent, delete=False) as staged:
        staged_path = Path(staged.name)
    try:
        staged_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        staged_path.replace(output)
    finally:
        staged_path.unlink(missing_ok=True)
    print(json.dumps({'status': report['status'], 'report': str(output)}))
    return 0 if report['status'] == 'prepared' else 1


if __name__ == '__main__':
    raise SystemExit(main())
