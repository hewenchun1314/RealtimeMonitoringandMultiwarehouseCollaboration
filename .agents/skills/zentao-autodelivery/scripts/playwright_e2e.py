"""Launch the project's Playwright browser with a local Windows credential."""

import argparse
import ctypes
from ctypes import wintypes
from http.client import HTTPConnection, HTTPException, HTTPSConnection
import json
from pathlib import Path
import re
from socket import create_connection
import sqlite3
import subprocess
from time import monotonic, sleep
from urllib.parse import urlsplit

from workflow import CONFIG_PATH, DATABASE_PATH, PROJECT_ROOT


def windows_credential(reference, expected_username):
    prefix = 'windows-dpapi:'
    if not reference.startswith(prefix) or not reference[len(prefix):]:
        raise ValueError('Unsupported E2E credential reference')

    class Credential(ctypes.Structure):
        _fields_ = [
            ('Flags', wintypes.DWORD), ('Type', wintypes.DWORD), ('TargetName', wintypes.LPWSTR),
            ('Comment', wintypes.LPWSTR), ('LastWritten', wintypes.FILETIME),
            ('CredentialBlobSize', wintypes.DWORD), ('CredentialBlob', ctypes.c_void_p),
            ('Persist', wintypes.DWORD), ('AttributeCount', wintypes.DWORD),
            ('Attributes', ctypes.c_void_p), ('TargetAlias', wintypes.LPWSTR), ('UserName', wintypes.LPWSTR),
        ]

    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    pointer = ctypes.POINTER(Credential)()
    advapi.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(type(pointer))]
    advapi.CredReadW.restype = wintypes.BOOL
    advapi.CredFree.argtypes = [ctypes.c_void_p]
    advapi.CredFree.restype = None
    if not advapi.CredReadW(reference[len(prefix):], 1, 0, ctypes.byref(pointer)):
        return None
    try:
        item = pointer.contents
        if item.UserName != expected_username or not item.CredentialBlobSize:
            return None
        password = ctypes.string_at(item.CredentialBlob, item.CredentialBlobSize).decode('utf-16-le').rstrip('\0')
        return {'username': item.UserName, 'password': password} if password else None
    finally:
        advapi.CredFree(pointer)


def local_origin(value):
    parsed = urlsplit(value)
    if (parsed.scheme not in ('http', 'https') or parsed.hostname not in ('localhost', '127.0.0.1', '::1')
            or parsed.username is not None or parsed.password is not None or parsed.path not in ('', '/')
            or parsed.query or parsed.fragment or '\\' in value):
        raise ValueError('Use a loopback URL origin')
    return f'{parsed.scheme}://{parsed.netloc}'


def relative_url(value):
    if (not isinstance(value, str) or not value.startswith('/') or value.startswith('//')
            or any(char == '\\' or ord(char) < 32 for char in value)):
        raise ValueError('Use an origin-relative URL')
    return value


def read_config(marker):
    if marker is None:
        return json.loads(CONFIG_PATH.read_text(encoding='utf-8-sig'))
    with sqlite3.connect(DATABASE_PATH.as_uri() + '?mode=ro', uri=True) as conn:
        row = conn.execute('SELECT config_snapshot FROM work_items WHERE marker=?', (marker,)).fetchone()
    if not row or not row[0]:
        raise ValueError('The work item has no configuration snapshot')
    return json.loads(row[0])


def browser_options(config, args):
    e2e = config['e2e']
    if not e2e.get('enabled', True):
        raise ValueError('E2E is disabled')
    playwright = e2e['playwright']
    profile = json.loads(args.profile.read_text(encoding='utf-8-sig')) if args.profile else {}
    if not isinstance(profile, dict):
        raise ValueError('The test profile must be a JSON object')
    checks = profile.get('checks', []) if args.profile or args.scenario else playwright.get('preflight_checks', [])
    if not isinstance(checks, list) or any(
        not isinstance(check, dict) or not isinstance(check.get('text'), str) or not check['text'].strip()
        or not isinstance(check.get('name'), str) or not re.fullmatch(r'[a-z0-9-]{1,50}', check['name'])
        or check.get('action', 'visible') not in ('visible', 'click', 'expand')
        for check in checks
    ):
        raise ValueError('Each profile check requires a name, exact text and a supported action')
    if checks and not any(check.get('action', 'visible') == 'visible' for check in checks):
        raise ValueError('Preflight requires an explicit read assertion')
    workspace = (CONFIG_PATH.parent / config['workspaces'][e2e['workspace']]).resolve(strict=True)
    options = {
        'workspace': str(workspace), 'channel': playwright.get('channel'),
        'baseUrl': local_origin(args.base_url or playwright['preflight_origin']),
        'loginPath': relative_url(e2e['login']['path']), 'platformPath': relative_url(e2e['platform_entry_path']),
        'entryPath': relative_url(profile.get('entry_path') or playwright['preflight_entry_path']),
        'workArea': profile.get('work_area') or e2e['default_work_area']['name'], 'checks': checks,
    }
    if args.scenario:
        scenario = args.scenario.resolve(strict=True)
        if not (args.marker and args.base_url and checks and scenario.is_relative_to(PROJECT_ROOT)):
            raise ValueError('Acceptance requires a work-item snapshot, candidate origin, profile and local scenario')
        options['scenario'] = str(scenario)
    elif args.marker or args.base_url:
        raise ValueError('Candidate parameters require an acceptance scenario')
    return options


def port_open(host, port):
    try:
        with create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def http_ready(url):
    address = urlsplit(url)
    connection_type = HTTPSConnection if address.scheme == 'https' else HTTPConnection
    connection = connection_type(address.hostname, address.port, timeout=3)
    try:
        target = address.path + ('?' + address.query if address.query else '')
        connection.request('GET', target, headers={'Accept': 'text/html'})
        response = connection.getresponse()
        return response.status == 200 and 'text/html' in response.getheader('Content-Type', '').lower()
    except (OSError, HTTPException):
        return False
    finally:
        connection.close()


def prepare_frontend(config, options, startup_log):
    origin = urlsplit(options['baseUrl'])
    entry_port = origin.port or (443 if origin.scheme == 'https' else 80)
    if port_open(origin.hostname, entry_port):
        return {'mode': 'reused'}

    extra_ports = config['playwright'].get('preflight_startup_ports', [])
    if not isinstance(extra_ports, list) or any(type(port) is not int or not 1 <= port <= 65535 for port in extra_ports):
        raise ValueError('Preflight startup ports must be valid TCP port numbers')
    command = config['start_command']
    if not isinstance(command, str) or not command.strip():
        raise ValueError('Preflight requires a frontend start command')
    with startup_log.open('wb') as output:
        try:
            # The worker runs this process inside the same Windows Job as Playwright.
            process = subprocess.Popen(['pwsh', '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', command],
                                       cwd=options['workspace'], stdin=subprocess.DEVNULL,
                                       stdout=output, stderr=subprocess.STDOUT)
        except OSError:
            return {'mode': 'start-failed', 'reason': 'start-command-unavailable', 'log': str(startup_log)}
    launched = {'pid': process.pid, 'log': str(startup_log)}
    ports = dict.fromkeys([entry_port, *extra_ports])
    deadline = monotonic() + 120
    while monotonic() < deadline:
        if (exit_code := process.poll()) is not None:
            return {'mode': 'start-failed', 'reason': 'start-command-exited', 'exit_code': exit_code, **launched}
        if all(port_open(origin.hostname, port) for port in ports) and http_ready(options['baseUrl'] + options['loginPath']):
            return {'mode': 'started', **launched}
        sleep(1)
    return {'mode': 'start-failed', 'reason': 'startup-timeout', **launched}


def run_browser(config, options, args):
    frontend = None
    if args.startup_log:
        frontend = prepare_frontend(config['e2e'], options, args.startup_log)
        if frontend['mode'] == 'start-failed':
            return {'status': 'blocked', 'stage': 'frontend-startup', 'frontend': frontend}
    login = config['e2e']['login']
    try:
        options['credentials'] = windows_credential(login['credential_ref'], login['username'])
    except (OSError, ValueError):
        options['credentials'] = None
    command = ['node', str(Path(__file__).with_suffix('.cjs'))]
    try:
        completed = subprocess.run(command, input=json.dumps(options).encode('utf-8'), capture_output=True,
                                   timeout=None if args.scenario else 100)
        report = completed.stdout.splitlines()[-1:]
        result = json.loads(report[0]) if report else {'status': 'blocked', 'stage': 'browser-runner'}
    except subprocess.TimeoutExpired:
        result = {'status': 'blocked', 'stage': 'browser-timeout'}
    if frontend is not None:
        result['frontend'] = frontend
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', type=Path)
    parser.add_argument('--scenario', type=Path)
    parser.add_argument('--marker')
    parser.add_argument('--base-url')
    parser.add_argument('--startup-log', type=Path)
    args = parser.parse_args()
    try:
        config = read_config(args.marker)
        options = browser_options(config, args)
        if args.startup_log and args.scenario:
            raise ValueError('Formal acceptance uses the separate candidate launch')
        result = run_browser(config, options, args)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, subprocess.SubprocessError):
        result = {'status': 'blocked', 'stage': 'runner-configuration'}
    print(json.dumps(result, ensure_ascii=False))
    return int(result.get('status') != 'passed')


if __name__ == '__main__':
    raise SystemExit(main())
