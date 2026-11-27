"""Run a configured backend command or HTTP check inside the current stage."""
import argparse
from contextlib import closing
from http.client import HTTPConnection, HTTPSConnection, HTTPException
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
from urllib.parse import urlsplit


def command_args(value):
    if isinstance(value, str):
        value = shlex.split(value)
    if (not isinstance(value, list) or not value or
            any(not isinstance(arg, str) or not arg.strip() or '\0' in arg for arg in value)):
        raise ValueError('Configure the executable and its arguments as a nonempty array')
    return value.copy()


def resolve_command(argv):
    executable = shutil.which(argv[0])
    if not executable:
        raise FileNotFoundError(f'Executable unavailable: {argv[0]}')
    return [executable, *argv[1:]]


def start_config(value):
    if not isinstance(value, dict):
        raise ValueError('Configure start as an object with argv and optional env/jar')
    argv = command_args(value.get('argv'))
    env = value.get('env', {})
    if not isinstance(env, dict) or any(not isinstance(k, str) or not re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', k)
                                        or not isinstance(v, str) or '\0' in v for k, v in env.items()):
        raise ValueError('start.env must contain environment variable names and string values')
    pattern = value.get('jar')
    if pattern is not None:
        if (not isinstance(pattern, str) or not pattern or argv.count('{jar}') != 1 or ':' in pattern or
                '\\' in pattern or Path(pattern).is_absolute() or '..' in Path(pattern).parts):
            raise ValueError('start.jar must be a workspace-relative glob for one {jar} argument')
    elif '{jar}' in argv:
        raise ValueError('Configure start.jar for the {jar} argument')
    return argv, env, pattern


def startup(value, workspace):
    argv, env, pattern = start_config(value)
    if pattern:
        artifacts = [file.resolve() for file in workspace.glob(pattern)
                     if file.is_file() and not file.name.endswith(('-sources.jar', '-javadoc.jar', '-tests.jar'))]
        if len(artifacts) != 1 or not artifacts[0].is_relative_to(workspace.resolve()):
            raise ValueError(f'Expected exactly one built artifact matching {pattern} inside the workspace')
        argv[argv.index('{jar}')] = str(artifacts[0])
    return argv, {**os.environ, **env}


def http_settings(settings):
    if not isinstance(settings, dict):
        raise ValueError('Configure acceptance.url and optional timeout_seconds')
    url = settings.get('url')
    if not isinstance(url, str):
        raise ValueError('Configure an HTTP acceptance URL')
    endpoint = urlsplit(url)
    if (endpoint.scheme not in ('http', 'https') or not endpoint.hostname or endpoint.username is not None
            or endpoint.password is not None or endpoint.fragment or '\\' in url or any(c.isspace() for c in url)):
        raise ValueError('Invalid HTTP acceptance URL')
    timeout = settings.get('timeout_seconds', 10)
    if type(timeout) is not int or timeout <= 0:
        raise ValueError('HTTP check timeout must be a positive integer')
    return endpoint, timeout


def check_http(settings):
    endpoint, timeout = http_settings(settings)
    client = HTTPSConnection if endpoint.scheme == 'https' else HTTPConnection
    with closing(client(endpoint.hostname, endpoint.port, timeout=timeout)) as connection:
        path = endpoint.path or '/'
        if endpoint.query:
            path += '?' + endpoint.query
        connection.request('GET', path, headers={'Accept': 'application/json'})
        response = connection.getresponse()
        if not 200 <= response.status < 300:
            raise ValueError(f'Acceptance endpoint returned HTTP {response.status}')
        return {'url': settings['url'], 'status': response.status}


def main():
    from backend_dependencies import backend_services
    from runtime_paths import CONFIG_PATH, DATABASE_PATH

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['build', 'test', 'start', 'check'])
    parser.add_argument('--service', required=True)
    parser.add_argument('--workspace', type=Path, default=Path.cwd(),
                        help='Verified checkout, or the assembled reactor for a Maven build/test')
    parser.add_argument('--config', type=Path, default=CONFIG_PATH)
    parser.add_argument('--marker', help='Use the admitted work-item service configuration')
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text(encoding='utf-8-sig'))
        service = backend_services(config)[args.service]
        if args.marker:
            with closing(sqlite3.connect(DATABASE_PATH.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
                row = conn.execute('SELECT config_snapshot FROM work_items WHERE marker=?', (args.marker,)).fetchone()
            if not row or backend_services(json.loads(row[0])).get(args.service) != service:
                raise ValueError('Current service configuration does not match the admitted work item')
        if args.operation == 'check' and 'acceptance' in service:
            print(json.dumps(check_http(service['acceptance'])))
            return 0
        workspace = args.workspace.resolve(strict=True)
        env = None
        if args.operation == 'start' and 'start' in service:
            argv, env = startup(service['start'], workspace)
        else:
            field = ('acceptance' if args.operation == 'check' else args.operation) + '_command'
            argv = command_args(service[field])
        return subprocess.run(resolve_command(argv), cwd=workspace, env=env, stdin=subprocess.DEVNULL).returncode
    except (HTTPException, KeyError, OSError, sqlite3.Error, TypeError, ValueError) as error:
        print(f'Service {args.operation} failed: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
