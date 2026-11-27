"""Run the project's browser acceptance through a paired AgentLimb Chrome profile."""

import argparse
from contextlib import closing
import hashlib
from http.client import HTTPConnection, HTTPException, HTTPSConnection
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
from time import monotonic, sleep
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

from portable_lock import file_lock
from runtime_paths import CONFIG_PATH, DATABASE_PATH, LOCAL_DIRECTORY, PROJECT_ROOT
from service_runtime import command_args, resolve_command
from merged_e2e_sources import RuntimeSourcesChanged, merged_workspace, prepare_committed_sources, verify_runtime_sources


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
    with closing(sqlite3.connect(DATABASE_PATH.as_uri() + '?mode=ro', uri=True)) as conn:
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
    if e2e.get('work_area_path'):
        options['workAreaPath'] = relative_url(e2e['work_area_path'])
        options['loginFile'] = str(LOCAL_DIRECTORY / 'e2e-login.json')
    if args.scenario:
        scenario = args.scenario.resolve(strict=True)
        item_dir = DATABASE_PATH.parent / 'items' / (args.marker or '')
        local_scenario = scenario.is_relative_to(PROJECT_ROOT) or (
            re.fullmatch(r'HNF-[0-9a-f]{20}', args.marker or '') and scenario.is_relative_to(item_dir))
        automatic = config.get('e2e_attestation') == 'launch' and e2e.get('after_integration') is True
        if not (args.marker and args.base_url and args.startup_log and local_scenario and (checks or automatic)):
            raise ValueError('Acceptance requires a work item, candidate startup and a local scenario')
        options['scenario'] = str(scenario)
        if automatic:
            if not profile.get('entry_path'):
                raise ValueError('Specify the entry page in the scenario')
            options['automaticAttestation'] = True
        else:
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
    if options.get('automaticAttestation'):
        if not options.get('postMerge'):
            raise ValueError('Automatic version proof requires isolated merged sources')
        applications = config['development']['frontend_bundle']['applications']
        options['appPaths'] = {app: applications[app]['repository'] for app in versions}
    elif {row['app'] for row in options['resources']} != versions.keys():
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
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def port_in_use(host, port):
    if os.name != 'nt':
        return port_open(host, port)
    # Windows can briefly report a successful connection to a stopped listener.
    # An exclusive bind checks whether the next server can actually claim the port.
    for family, kind, protocol, _, address in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        with socket.socket(family, kind, protocol) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                probe.bind(address)
            except OSError as error:
                if error.winerror in (10013, 10048):  # WSAEACCES, WSAEADDRINUSE
                    return True
                raise
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


def get_port(url):
    return url.port or (443 if url.scheme == 'https' else 80)


def prepare_frontend(config, options, startup_log):
    origin = urlsplit(options['baseUrl'])
    entry_port = get_port(origin)
    formal = bool(options.get('startupWorkspace') or options.get('gatewayUrl'))
    required = browser_config(config).get('preflight_startup_ports', [])
    if options.get('automaticAttestation'):
        required = []
    elif options.get('resources'):
        required = [get_port(urlsplit(row['url'])) for row in options['resources']]
    if not isinstance(required, list) or any(type(p) is not int or not 1 <= p <= 65535 for p in required):
        raise ValueError('Invalid frontend readiness ports')
    ready_ports = sorted({entry_port, *required})
    startup_ports = config.get('startup_ports', ready_ports)
    services = options.get('startupServices')
    if services:
        startup_ports = sorted({port for _, _, port in services})
        ready_ports = sorted({entry_port, *startup_ports})
    if (not isinstance(startup_ports, list) or any(type(p) is not int or not 1 <= p <= 65535 for p in startup_ports)
            or not set(ready_ports).issubset(startup_ports)):
        raise ValueError('All frontend readiness ports must be in the configured startup ports')
    if not formal and port_in_use(origin.hostname, entry_port) and port_open(origin.hostname, entry_port):
        return {'mode': 'reused'}
    occupied = [port for port in sorted(set(startup_ports)) if port_in_use(origin.hostname, port)]
    if occupied:
        return {'mode': 'start-failed', 'reason': 'candidate-port-in-use', 'ports': occupied}

    gateway = options.get('gatewayUrl')
    argv = command_args(config['start_command'])
    if gateway:
        gateway = deployment_gateway(gateway)
        if argv[-1].startswith('--gateway='):
            argv.pop()
        elif len(argv) >= 2 and argv[-2] == '--gateway':
            del argv[-2:]
        if any(arg == '--gateway' or arg.startswith('--gateway=') for arg in argv):
            raise ValueError('Use a single terminal --gateway option')
        argv.append('--gateway=' + gateway)
    if services:
        configured_gateway = next((arg.split('=', 1)[1] for arg in argv if arg.startswith('--gateway=')), None)
    workspace = options.get('startupWorkspace', options['workspace'])
    environment = dict(os.environ)
    environment['npm_config_cache'] = str(LOCAL_DIRECTORY / 'npm-cache')
    source_workspace = None
    if options.get('postMerge'):
        if options.get('mergedCheckouts'):
            try:
                source_workspace = prepare_committed_sources(options, startup_log.resolve().parent)
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                return {'mode': 'start-failed', 'reason': 'merged-source-preparation', 'detail': str(error)}
            workspace = source_workspace
        witness = Path(__file__).with_name('agentlimb_resource_witness.cjs').resolve()
        environment['NODE_OPTIONS'] = f'{environment.get("NODE_OPTIONS", "")} --require="{witness.as_posix()}"'.strip()
        witness_config = {'origin': options['baseUrl'], 'resources': options.get('resources', [])}
        if options.get('automaticAttestation'):
            roots = {}
            for app, path in options['appPaths'].items():
                actual = (Path(source_workspace) / path).resolve(strict=True)
                if not actual.is_relative_to(Path(source_workspace)) or str(actual) in roots:
                    raise ValueError('The candidate application needs a unique isolated source directory')
                roots[str(actual)] = app
            options['launchProof'] = {'token': uuid4().hex, 'apps': sorted(roots.values()),
                                      'ports': sorted(set(startup_ports))}
            witness_config['launch'] = {**options['launchProof'], 'roots': roots}
        if gateway:
            witness_config['gatewayHash'] = options['gatewayHash']
        environment['ZENTAO_E2E_WITNESS'] = json.dumps(witness_config)

    with startup_log.open('wb') as output:
        try:
            if services:
                if configured_gateway:
                    environment.update(GATEWAY_URL=configured_gateway, npm_config_gateway=configured_gateway)
                processes = []
                for directory, script, _ in services:
                    processes.append(launch_frontend(['npm', 'run', script], Path(workspace) / directory,
                                                     output, {'env': environment}))
            else:
                processes = [launch_frontend(argv, workspace, output, {'env': environment})]
        except OSError:
            return {'mode': 'start-failed', 'reason': 'start-command-unavailable', 'log': str(startup_log)}
    launched = {'pid': processes[0].pid, 'log': str(startup_log)}
    if services:
        launched['services'] = [directory for directory, _, _ in services]
    if source_workspace:
        launched['source_workspace'] = source_workspace
    if gateway:
        launched['gateway'] = gateway
    deadline = monotonic() + 120
    while monotonic() < deadline:
        for process in processes:
            if (exit_code := process.poll()) is not None:
                return {'mode': 'start-failed', 'reason': 'start-command-exited', 'exit_code': exit_code, **launched}
        if all(port_open(origin.hostname, port) for port in ready_ports) and http_ready(options['baseUrl'] + options['loginPath']):
            return {'mode': 'started', **launched}
        sleep(1)
    return {'mode': 'start-failed', 'reason': 'startup-timeout', **launched}


def launch_frontend(argv, workspace, output, start_options):
    return subprocess.Popen(resolve_command(argv), cwd=workspace, stdin=subprocess.DEVNULL, stdout=output,
                            stderr=subprocess.STDOUT, **start_options)


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
    if frontend is not None:
        result['frontend'] = frontend
    if options.get('candidateVersions') and result.get('status') in ('passed', 'failed'):
        from route_evidence import check_candidate

        versions = options['candidateVersions']
        try:
            if options.get('postMerge'):
                verify_runtime_sources(options, (frontend or {}).get('source_workspace'))
            elif any(check_candidate(options['candidateBranches'][app], sha) for app, sha in versions.items()):
                raise ValueError('Candidate changed during acceptance')
        except RuntimeSourcesChanged:
            return {'status': 'blocked', 'stage': 'candidate-version', 'reason': 'runtime-sources-changed'}
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
    with file_lock(DATABASE_PATH.parent / 'agentlimb-e2e.lock') as acquired:
        if not acquired:
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
