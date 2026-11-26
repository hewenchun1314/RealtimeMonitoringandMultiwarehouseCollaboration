"""Run the project's browser acceptance through a paired AgentLimb Chrome profile."""

import argparse
from contextlib import closing
import hashlib
from http.client import HTTPConnection, HTTPException, HTTPSConnection
import json
import os
from pathlib import Path
import re
from socket import create_connection
import sqlite3
import subprocess
from time import monotonic, sleep
from types import SimpleNamespace
from urllib.parse import urlsplit

from workflow import CONFIG_PATH, DATABASE_PATH, PROJECT_ROOT


def browser_config(config):
    # Old work-item snapshots retain their paths and checks, but all use AgentLimb.
    return config.get('agentlimb', config.get('playwright', {}))


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
    settings = browser_config(e2e)
    profile = json.loads(args.profile.read_text(encoding='utf-8-sig')) if args.profile else {}
    if not isinstance(profile, dict):
        raise ValueError('The test profile must be a JSON object')
    checks = profile.get('checks', []) if args.profile or args.scenario else settings.get('preflight_checks', [])
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
        'workspace': str(workspace), 'browser': os.getenv('ZENTAO_AGENTLIMB_BROWSER') or settings.get('browser', 'ZenTao-E2E'),
        'host': os.getenv('ZENTAO_AGENTLIMB_HOST') or str(Path.home() / '.agentlimb' / 'host' / 'mcp-server.mjs'),
        'baseUrl': local_origin(args.base_url or settings['preflight_origin']),
        'loginPath': relative_url(e2e['login']['path']), 'platformPath': relative_url(e2e['platform_entry_path']),
        'entryPath': relative_url(profile.get('entry_path') or settings['preflight_entry_path']),
        'workArea': profile.get('work_area') or e2e['default_work_area']['name'], 'checks': checks,
        'username': e2e['login'].get('username', ''),
    }
    if args.scenario:
        scenario = args.scenario.resolve(strict=True)
        if not (args.marker and args.base_url and args.startup_log and checks and scenario.is_relative_to(PROJECT_ROOT)):
            raise ValueError('Acceptance requires a work item, candidate startup, a profile and a local scenario')
        options['scenario'] = str(scenario)
        options['resources'] = resource_profile(profile.get('candidate_resources'))
    elif args.marker or args.base_url:
        raise ValueError('Candidate parameters require an acceptance scenario')
    return options


def resource_profile(resources):
    if not isinstance(resources, list) or not resources:
        raise ValueError('Identify at least one loaded candidate resource per changed application')
    result = []
    for row in resources:
        parsed = urlsplit(row['url'])
        origin = local_origin(f'{parsed.scheme}://{parsed.netloc}')
        if not parsed.path or parsed.fragment or not re.fullmatch(r'[0-9a-f]{64}', row['sha256']):
            raise ValueError('Each candidate resource requires a loopback asset URL and SHA-256')
        url = origin + relative_url(parsed.path + ('?' + parsed.query if parsed.query else ''))
        result.append({'app': row['app'], 'url': url, 'sha256': row['sha256']})
    return result


def deployment_gateway(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9._:/\[\]-]+', value):
        raise ValueError('Invalid deployment gateway')
    parsed = urlsplit(value)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.port == 0
            or parsed.netloc.endswith(':') or parsed.path not in ('', '/')):
        raise ValueError('Use a deployment gateway origin')
    return value


def candidate_workspace(options, config, branches, versions, supplied):
    from route_evidence import check_candidate

    project = Path(options['workspace']).resolve(strict=True)
    workspace = (supplied.resolve(strict=True) if supplied else next(
        (Path(proof['worktree']).resolve(strict=True) for proof in branches.values()
         if Path(proof['source_root']).resolve(strict=True) == project), None))
    if (workspace is None or workspace == project or not (workspace / 'package.json').is_file()
            or not versions or branches.keys() != versions.keys()):
        raise ValueError('Acceptance requires the assembled candidate frontend workspace')
    applications = config['development']['frontend_bundle']['applications']
    for app, sha in versions.items():
        proof = branches[app]
        checkout = Path(proof['worktree']).resolve(strict=True)
        checkout_path = (workspace / applications[app]['repository']).resolve(strict=True)
        if checkout_path != checkout or check_candidate(proof, sha):
            raise ValueError('Frontend startup does not include every exact candidate worktree')
    return str(workspace)


def check_merged_checkout(repository, branch, sha):
    from branch_check import git

    root = Path(repository).resolve(strict=True)
    if (Path(git(root, 'rev-parse', '--show-toplevel')).resolve() != root
            or git(root, 'symbolic-ref', '--quiet', 'HEAD', allowed=(0, 1)) != 'refs/heads/' + branch
            or git(root, 'rev-parse', 'HEAD') != sha
            or git(root, '--no-optional-locks', 'status', '--porcelain=v1', '--untracked-files=no',
                   '--ignore-submodules=untracked')):
        raise ValueError('Main workspace must contain the exact, clean merged checkout')


def merged_workspace(options, config, branches, versions):
    root = Path(options['workspace']).resolve(strict=True)
    applications = config['development']['frontend_bundle']['applications']
    if not (root / 'package.json').is_file() or not versions or versions.keys() != branches.keys():
        raise ValueError('Acceptance requires the merged frontend workspace')
    # The shell serves every child application and must use a committed startup definition.
    from branch_check import git
    root_sha = git(root, 'rev-parse', 'HEAD')
    root_branch = git(root, 'symbolic-ref', '--short', 'HEAD')
    check_merged_checkout(root, root_branch, root_sha)
    checkouts = {str(root): {'repository': str(root), 'branch': root_branch, 'sha': root_sha}}
    for app, sha in versions.items():
        proof, application = branches[app], applications[app]
        checkout = (root / application['repository']).resolve(strict=True)
        branch = application['branch']
        if (not checkout.is_relative_to(root) or checkout != Path(proof['source_root']).resolve(strict=True)
                or branch != proof['target_branch']):
            raise ValueError('Merged checkout must match the configured application and target branch')
        check_merged_checkout(checkout, branch, sha)
        checkouts[str(checkout)] = {'repository': str(checkout), 'branch': branch, 'sha': sha}
    return checkouts


def acceptance_setup(config, options, args):
    from deploy_state import DEPLOY_STAGE, configured_targets, deployed_receipt_matches
    from route_execution import read_history

    targets = configured_targets(config)
    with closing(sqlite3.connect(DATABASE_PATH.as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('BEGIN')
        item = conn.execute('SELECT * FROM work_items WHERE marker=?', (args.marker,)).fetchone()
        if item is None:
            raise ValueError('Missing work-item state')
        route, stages = read_history(SimpleNamespace(conn=conn), item)
        attempt = conn.execute('SELECT attempt_id, phase FROM attempts WHERE item_seq=? AND run_round=? AND outcome IS NULL',
                               (item['seq'], item['run_round'])).fetchone()
    effective_stage = (attempt['phase'] if attempt and attempt['phase'] != 'verify' else item['stage'])
    if not route or not attempt or item['state'] != 'running' or effective_stage != 'frontend.e2e':
        raise ValueError('Only the current frontend E2E attempt can run formal acceptance')
    branches = stages['frontend.branch_check']['evidence']['repositories']
    if route.get('post_merge_e2e'):
        if args.candidate_workspace or args.scenario.suffix.lower() != '.json':
            raise ValueError('Post-merge acceptance requires a JSON scenario on the main workspace')
        integration = stages.get('frontend.integration', {})
        if integration.get('outcome') != 'success':
            raise ValueError('Post-merge acceptance requires successful integration')
        versions = integration['evidence']['merged_versions']
        options.update(postMerge=True, startupWorkspace=options['workspace'],
                       mergedCheckouts=merged_workspace(options, config, branches, versions))
    else:
        versions = stages['frontend.coding']['evidence']['versions']
        options['startupWorkspace'] = candidate_workspace(options, config, branches, versions, args.candidate_workspace)
    if {row['app'] for row in options['resources']} != versions.keys():
        raise ValueError('Candidate resources must identify every changed frontend application')
    options['candidateVersions'] = versions
    options['candidateBranches'] = branches
    options['attemptId'] = attempt['attempt_id']
    if route['scope'] != 'fullstack' or DEPLOY_STAGE not in route['stages']:
        return {}
    deployed = stages.get(DEPLOY_STAGE, {})
    if deployed.get('outcome') != 'success':
        raise ValueError('Current backend deployment is not verified')
    deployment = deployed['evidence']['services']
    if not deployment:
        return {}
    handoff = stages.get('handoff', {})
    evidence = handoff.get('evidence', {})
    if handoff.get('outcome') != 'success' or evidence.get('deployment') != deployment:
        raise ValueError('Deployed acceptance requires the current handoff and a candidate startup log')
    gateway = deployment_gateway(evidence['gateway_url'])
    if any(service not in targets or targets[service]['gateway_url'].rstrip('/') != gateway.rstrip('/')
           or not deployed_receipt_matches(receipt, service, evidence['backend_versions'].get(service), targets[service])
           for service, receipt in deployment.items()):
        raise ValueError('Deployment versions do not match the common handoff gateway')
    options['gatewayUrl'] = gateway
    if options.get('postMerge'):
        options['gatewayHash'] = hashlib.sha256(gateway.rstrip('/').encode()).hexdigest()
    return deployment


def verify_deployment(deployment):
    from backend_deploy import verify_published_release

    return {service: verify_published_release(receipt) for service, receipt in deployment.items()}


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


def stop_frontend_ports(ports):
    port_numbers = ','.join(str(port) for port in sorted(ports))
    script = f'''
$ErrorActionPreference = 'Stop'
$ports = @({port_numbers})
$listeners = @(Get-NetTCPConnection -State Listen | Where-Object {{ $_.LocalPort -in $ports }})
foreach ($group in @($listeners | Group-Object OwningProcess)) {{
    $owner = [int]$group.Name
    if ($owner -le 4 -or $owner -eq {os.getpid()} -or $owner -eq $PID) {{ throw "Protected listener: $owner" }}
    $process = Get-Process -Id $owner -ErrorAction SilentlyContinue
    if ($null -eq $process) {{ continue }}
    $started = $process.StartTime
    if (-not (Get-NetTCPConnection -State Listen | Where-Object {{ $_.OwningProcess -eq $owner -and $_.LocalPort -in $ports }})) {{ continue }}
    $current = Get-Process -Id $owner -ErrorAction SilentlyContinue
    if ($null -ne $current -and $current.StartTime -eq $started) {{ Stop-Process -InputObject $current -Force }}
}}
for ($i = 0; $i -lt 25; $i++) {{
    if (-not (Get-NetTCPConnection -State Listen | Where-Object {{ $_.LocalPort -in $ports }})) {{
        ConvertTo-Json -InputObject @($listeners | ForEach-Object {{ @{{port = $_.LocalPort; pid = $_.OwningProcess}} }}) -Compress
        exit 0
    }}
    Start-Sleep -Milliseconds 200
}}
throw 'Configured frontend ports remain occupied'
'''
    completed = subprocess.run(['pwsh', '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', script],
                               check=True, capture_output=True, timeout=30)
    return json.loads(completed.stdout)


def get_port(url):
    return url.port or (443 if url.scheme == 'https' else 80)


def prepare_frontend(config, options, startup_log, *, restart=False):
    origin = urlsplit(options['baseUrl'])
    entry_port = get_port(origin)
    gateway = options.get('gatewayUrl')
    candidate = bool(options.get('startupWorkspace') or gateway)
    post_merge = options.get('postMerge', False)
    if not post_merge and port_open(origin.hostname, entry_port):
        return ({'mode': 'start-failed', 'reason': 'candidate-port-in-use'} if candidate else {'mode': 'reused'})

    extra_ports = browser_config(config).get('preflight_startup_ports', [])
    if options.get('resources'):
        resource_origins = [urlsplit(row['url']) for row in options['resources']]
        extra_ports = sorted({get_port(url) for url in resource_origins})
    if not isinstance(extra_ports, list) or any(type(port) is not int or not 1 <= port <= 65535 for port in extra_ports):
        raise ValueError('Preflight startup ports must be valid TCP port numbers')
    ports = dict.fromkeys([entry_port, *extra_ports])
    command = config['start_command']
    if not isinstance(command, str) or not command.strip():
        raise ValueError('Preflight requires a frontend start command')
    start_options = {}
    reclaimed = None
    if post_merge:
        configured_ports = config.get('startup_ports')
        if (not isinstance(configured_ports, list) or not configured_ports
                or any(type(port) is not int or not 1 <= port <= 65535 for port in configured_ports)
                or not ports.keys() <= set(configured_ports)):
            raise ValueError('All formal E2E ports must be in the configured startup ports')
        if (not restart and all(port_open(origin.hostname, port) for port in ports)
                and http_ready(options['baseUrl'] + options['loginPath'])):
            return {'mode': 'reused', **({'gateway': gateway} if gateway else {})}
        try:
            reclaimed = stop_frontend_ports(sorted(set(configured_ports)))
        except (OSError, ValueError, subprocess.SubprocessError):
            return {'mode': 'start-failed', 'reason': 'frontend-ports-not-released', 'ports': sorted(set(configured_ports))}
        witness = Path(__file__).with_name('agentlimb_resource_witness.cjs').resolve()
        environment = dict(os.environ)
        environment['NODE_OPTIONS'] = f'{environment.get("NODE_OPTIONS", "")} --require="{witness.as_posix()}"'.strip()
        witness_config = {'origin': options['baseUrl'], 'resources': options.get('resources', [])}
        if gateway:
            witness_config['gatewayHash'] = options['gatewayHash']
        environment['ZENTAO_E2E_WITNESS'] = json.dumps(witness_config)
        start_options['env'] = environment
    elif candidate and any(port_open(origin.hostname, port) for port in set(extra_ports) - {entry_port}):
        return {'mode': 'start-failed', 'reason': 'candidate-port-in-use'}
    if gateway:
        gateway = deployment_gateway(gateway)
        # Keep the configured command. Only replace its declared gateway option.
        flag = r'\s+--gateway(?:=|\s+)(?:"[^"]*"|\x27[^\x27]*\x27|[^\s"\x27]+)'
        command = re.sub(flag + r'\s*$', '', command)
        if re.search(r'(?<!\S)--gateway\b', command):
            raise ValueError('Use a single terminal --gateway option in the frontend start command')
        command = f"{command.rstrip()} --gateway='{gateway}'"
    with startup_log.open('wb') as output:
        try:
            # The Windows stage runner owns the candidate process tree.
            process = subprocess.Popen(['pwsh', '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', command],
                                       cwd=options.get('startupWorkspace', options['workspace']), stdin=subprocess.DEVNULL,
                                       stdout=output, stderr=subprocess.STDOUT, **start_options)
        except OSError:
            return {'mode': 'start-failed', 'reason': 'start-command-unavailable', 'log': str(startup_log)}
    launched = {'pid': process.pid, 'log': str(startup_log)}
    if reclaimed:
        launched['released_listeners'] = reclaimed
    if gateway:
        launched['gateway'] = gateway
    deadline = monotonic() + 120
    while monotonic() < deadline:
        if (exit_code := process.poll()) is not None:
            return {'mode': 'start-failed', 'reason': 'start-command-exited', 'exit_code': exit_code, **launched}
        if all(port_open(origin.hostname, port) for port in ports) and http_ready(options['baseUrl'] + options['loginPath']):
            return {'mode': 'started', **launched}
        sleep(1)
    return {'mode': 'start-failed', 'reason': 'startup-timeout', **launched}


def browser_result(options, args):
    command = ['node', str(Path(__file__).with_suffix('.cjs'))]
    try:
        completed = subprocess.run(command, input=json.dumps(options).encode('utf-8'), capture_output=True,
                                   timeout=None if args.scenario else 100, cwd=PROJECT_ROOT)
        report = completed.stdout.splitlines()[-1:]
        return json.loads(report[0]) if report else {'status': 'blocked', 'stage': 'browser-runner'}
    except subprocess.TimeoutExpired:
        return {'status': 'blocked', 'stage': 'browser-timeout'}


def run_browser(config, options, args, *, deployment=None):
    frontend = None
    if args.startup_log:
        frontend = prepare_frontend(config['e2e'], options, args.startup_log)
        if frontend['mode'] == 'start-failed':
            return {'status': 'blocked', 'stage': 'frontend-startup', 'frontend': frontend}
    if deployment:
        try:
            backend = verify_deployment(deployment)
        except (OSError, ValueError):
            return {'status': 'blocked', 'stage': 'backend-deployment', 'frontend': frontend,
                    'reason': 'backend-version-unverified'}
    result = browser_result(options, args)
    if (options.get('postMerge') and frontend and frontend['mode'] == 'reused'
            and result.get('status') == 'blocked' and not result.get('candidate_resources')
            and result.get('stage') in ('candidate-resource', 'entry', 'test-data')):
        fallback_stage = result['stage']
        frontend = prepare_frontend(config['e2e'], options, args.startup_log, restart=True)
        frontend['reuse_blocker'] = fallback_stage
        if frontend['mode'] == 'start-failed':
            return {'status': 'blocked', 'stage': 'frontend-startup', 'frontend': frontend}
        result = browser_result(options, args)
    if frontend is not None:
        result['frontend'] = frontend
    if options.get('candidateVersions') and result.get('status') in ('passed', 'failed'):
        from route_evidence import check_candidate

        versions = options['candidateVersions']
        try:
            if options.get('postMerge'):
                for checkout in options['mergedCheckouts'].values():
                    check_merged_checkout(**checkout)
            elif any(check_candidate(options['candidateBranches'][app], sha) for app, sha in versions.items()):
                raise ValueError('Candidate changed during acceptance')
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            return {'status': 'blocked', 'stage': 'candidate-version', 'reason': 'candidate-changed-during-acceptance'}
        result['versions'] = versions
    if deployment:
        result['backend_deployment'] = backend
        if result.get('status') in ('passed', 'failed'):
            try:
                verify_deployment(deployment)
            except (OSError, ValueError):
                return {**result, 'status': 'blocked', 'stage': 'backend-deployment',
                        'reason': 'backend-version-changed-during-acceptance'}
    return result


def run_serialized(config, options, args, deployment):
    import msvcrt

    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with (DATABASE_PATH.parent / 'agentlimb-e2e.lock').open('a+b') as lock:
        if lock.tell() == 0:
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return {'status': 'blocked', 'stage': 'agentlimb-busy'}
        return run_browser(config, options, args, deployment=deployment)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', type=Path)
    parser.add_argument('--scenario', type=Path)
    parser.add_argument('--marker')
    parser.add_argument('--base-url')
    parser.add_argument('--startup-log', type=Path)
    parser.add_argument('--candidate-workspace', type=Path)
    args = parser.parse_args()
    try:
        config = read_config(args.marker)
        options = browser_options(config, args)
        deployment = acceptance_setup(config, options, args) if args.scenario else {}
        if args.candidate_workspace and not args.scenario:
            raise ValueError('Candidate workspace requires formal acceptance')
        if not Path(options['host']).is_file():
            result = {'status': 'blocked', 'stage': 'agentlimb-host', 'reason': 'host-not-installed'}
        else:
            result = run_serialized(config, options, args, deployment)
        if args.scenario:
            result.update(marker=args.marker, attempt_id=options['attemptId'])
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError, sqlite3.Error, subprocess.SubprocessError):
        result = {'status': 'blocked', 'stage': 'runner-configuration'}
    print(json.dumps({'runner': 'agentlimb', **result}, ensure_ascii=False))
    return int(result.get('status') != 'passed')


if __name__ == '__main__':
    raise SystemExit(main())
