"""Publish a ZenTao frontend image from the exact, remotely verified merge commits."""

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile

from backend_deploy import DeploymentError, command, remote_image, required_digest
from deploy_state import COMMIT, FRONTEND_PROFILE, TAG
from rancher_mcp import RancherMcpError, current_image, update_and_verify
from stage_runner import write_json


def git(root, *args, allow_failure=False):
    result = command('git', '-c', 'credential.interactive=never', '-c', 'core.longpaths=true', *args, cwd=root,
                     allow_failure=True, timeout=120)
    if result.returncode and not allow_failure:
        raise DeploymentError(f'git {args[0]} failed (exit {result.returncode})')
    return result


def ancestor(root, before, after):
    result = git(root, 'merge-base', '--is-ancestor', before, after, allow_failure=True)
    if result.returncode not in (0, 1):
        raise DeploymentError('Git could not compare the release commits')
    return result.returncode == 0


def remote_tip(root, ref):
    result = git(root, 'ls-remote', '--exit-code', '--heads', 'origin', ref, allow_failure=True)
    if result.returncode == 2:
        return None
    fields = result.stdout.split()
    if result.returncode or len(fields) != 2 or fields[1] != ref or not COMMIT.fullmatch(fields[0]):
        raise DeploymentError('Could not verify the Git remote branch')
    return fields[0]


def read_remote(root, ref):
    for _ in range(3):
        before = remote_tip(root, ref)
        if before is None:
            return None
        fetched = git(root, 'fetch', '--no-tags', '--no-write-fetch-head', '--refmap=', 'origin', ref,
                      allow_failure=True)
        after = remote_tip(root, ref)
        if before == after:
            if fetched.returncode or git(root, 'cat-file', '-t', after).stdout.strip() != 'commit':
                raise DeploymentError('Could not fetch the verified Git remote commit')
            return after
    raise DeploymentError('Git remote branch changed repeatedly during verification')


def synchronize_branch(root, branch, sha, *, allow_push=True):
    ref = f'refs/heads/{branch}'
    current = git(root, 'rev-parse', '--verify', ref).stdout.strip()
    if not COMMIT.fullmatch(sha) or not ancestor(root, sha, current):
        raise DeploymentError('The local target does not contain the admitted merge SHA')
    fetch_urls = git(root, 'remote', 'get-url', '--all', 'origin').stdout.splitlines()
    push_urls = git(root, 'remote', 'get-url', '--push', '--all', 'origin').stdout.splitlines()
    if len(fetch_urls) != 1 or fetch_urls != push_urls:
        raise DeploymentError('Git origin must have one matching fetch and push endpoint')
    remote = read_remote(root, ref)
    if remote and ancestor(root, sha, remote):
        return remote
    if not allow_push:
        raise DeploymentError('Git remote no longer contains the verified merge SHA')
    if remote and not ancestor(root, remote, sha):
        raise DeploymentError('Git remote has diverged from the admitted merge SHA')
    git(root, 'push', '--no-follow-tags', '--recurse-submodules=no', 'origin', f'{sha}:{ref}')
    remote = read_remote(root, ref)
    if not remote or not ancestor(root, sha, remote):
        raise DeploymentError('Git remote did not confirm the pushed merge SHA')
    return remote


def verify_ports_stopped(ports):
    table = command('netstat', '-ano', timeout=20).stdout
    occupied = []
    for line in table.splitlines():
        fields = line.split()
        if len(fields) != 5 or fields[0] != 'TCP' or fields[2] not in ('0.0.0.0:0', '[::]:0'):
            continue
        number = fields[1].rpartition(':')[2]
        if number.isdecimal() and int(number) in ports:
            occupied.append(f'{number} (PID {fields[-1]})')
    if occupied:
        raise DeploymentError('本地前端端口仍被占用：' + '、'.join(sorted(set(occupied))))


def verify_toolchain(request):
    tools = Path(request['release_dir'])
    for name in ('bundle-build.cjs', 'patch_image_html_dirs.py'):
        if not (tools / name).is_file():
            raise DeploymentError(f'customized-release 缺少 {name}')
    script = (
        "const m=require('./aggregation-platform-bundle.manifest.cjs');"
        "const b=require('./aggregation-platform-branches.cjs')[process.argv[1]];"
        "console.log(JSON.stringify(Object.fromEntries(m.apps.filter(a=>b?.[a.key])"
        ".map(a=>[a.key,{branch:b[a.key],path:a.localDir}]))));"
    )
    mappings = json.loads(command('node', '-e', script, FRONTEND_PROFILE, cwd=tools, timeout=30).stdout)
    for key, app in request['applications'].items():
        if mappings.get(key) != {'branch': app['branch'], 'path': app['path']}:
            raise DeploymentError(f'{key} 的发布分支或应用目录与打包清单不一致')
        root = Path(app['repository'])
        actual = Path(git(root, 'rev-parse', '--show-toplevel').stdout.strip()).resolve()
        if root != actual:
            raise DeploymentError(f'{key} 不是配置中的独立 Git 仓库')
    command('docker', 'info', '--format', '{{.ServerVersion}}', timeout=30)


def build_archive(request, folder):
    settings = request['settings']
    repo_root = folder / 'source'
    repo_root.mkdir()
    for key, app in sorted(request['applications'].items(), key=lambda pair: pair[1]['path'] != '.'):
        destination = repo_root / app['path']
        if not destination.resolve().is_relative_to(repo_root):
            raise DeploymentError(f'{key} 的工作目录超出发布目录')
        destination.parent.mkdir(parents=True, exist_ok=True)
        git(destination.parent, 'clone', '--no-hardlinks', '--no-checkout', '--config', 'core.longpaths=true', '--',
            app['repository'], str(destination))
        git(destination, 'checkout', '--detach', app['sha'])
        if git(destination, 'rev-parse', 'HEAD').stdout.strip() != app['sha']:
            raise DeploymentError(f'{key} 的发布目录未检出本单版本')
    base = folder / 'base.tar'
    archive = folder / 'frontend.tar'
    command('docker', 'pull', '--platform', settings['platform'], settings['base_image'])
    base_id = required_digest(command('docker', 'image', 'inspect', '--format', '{{.Id}}',
                                      settings['base_image']).stdout.strip())
    command('docker', 'save', '--output', str(base), base_id)
    bundle = Path(request['release_dir'])
    argv = ['node', str(bundle / 'bundle-build.cjs'), '--env', FRONTEND_PROFILE,
            '--repo-root', str(repo_root), '--apps', ','.join(sorted(request['applications'])),
            '--non-interactive', '--skip-checkout', '--skip-pull', '--skip-preselect-fetch',
            '--no-cache', '--install', '--no-push', '--image-tar', str(base),
            '--image-tar-out', str(archive), '--out', str(folder / 'bundle')]
    result = subprocess.run(argv, cwd=bundle, stdin=subprocess.DEVNULL, check=False)
    if result.returncode:
        raise DeploymentError(f'customized-release 构建失败 (exit {result.returncode})')
    return archive


def archive_config(archive):
    with tarfile.open(archive, 'r:') as tar:
        rows = json.load(tar.extractfile('manifest.json'))
        if len(rows) != 1:
            raise DeploymentError('The frontend archive must contain one image')
        config = tar.extractfile(rows[0]['Config']).read()
    return 'sha256:' + sha256(config).hexdigest()


def tag_for(request):
    settings = request['settings']
    versions = {key: app['sha'] for key, app in request['applications'].items()}
    source = [request['marker'], settings['base_image'], settings['platform'], versions]
    fingerprint = sha256(json.dumps(source, sort_keys=True).encode()).hexdigest()[:24]
    return f'{settings["image_repository"]}:{fingerprint}-{FRONTEND_PROFILE}'


def release_base(settings, saved):
    target = settings.get('rancher')
    if not target:
        return settings['base_image'], None
    if saved:
        return saved['base_image'], saved['expected_previous_image']
    before = current_image(target, settings['base_image'])
    repo = settings['image_repository']
    if before.startswith(repo + '@'):
        required_digest(before.partition('@')[2])
    elif not TAG.fullmatch(before) or before.rsplit(':', 1)[0] != repo:
        raise DeploymentError('Rancher 当前前端镜像不属于配置的 Harbor 仓库')
    current = remote_image(before, settings['platform'])
    if not current:
        raise DeploymentError('Harbor 中找不到 Rancher 当前前端镜像')
    return repo + '@' + current['digest'], before


def publish(request, journal):
    settings = request['settings']
    versions = {key: app['sha'] for key, app in request['applications'].items()}
    try:
        saved = json.loads(journal.read_text(encoding='utf-8'))
    except FileNotFoundError:
        saved = {}
    base_image, previous_image = release_base(settings, saved)
    settings = {**settings, 'base_image': base_image}
    request = {**request, 'settings': settings}
    image = tag_for(request)
    if saved and (saved.get('image') != image or saved.get('versions') != versions):
        raise DeploymentError('Existing release receipt does not match the requested image or versions')
    expected = saved.get('config_digest')
    result = {'image': image, 'versions': versions, 'remote_versions': {}, 'base_image': base_image}
    if previous_image:
        result['expected_previous_image'] = previous_image
    for key in ('digest', 'mcp_baseline', 'rancher'):
        if key in saved:
            result[key] = saved[key]
    if expected:
        result['config_digest'] = required_digest(expected)
    verify_toolchain(request)
    for key, app in sorted(request['applications'].items()):
        try:
            args = (Path(app['repository']), app['branch'], app['sha'])
            if request.get('git_push_verified'):
                result['remote_versions'][key] = synchronize_branch(*args, allow_push=False)
            else:
                result['remote_versions'][key] = synchronize_branch(*args)
        except DeploymentError as error:
            raise DeploymentError(f'{key}: {error}') from error
        write_json(journal, result)
    verify_ports_stopped(settings['local_ports'])
    result['stopped_ports'] = settings['local_ports']
    write_json(journal, result)
    observed = remote_image(image, settings['platform'])
    if observed and observed['config_digest'] != expected:
        raise DeploymentError('Harbor tag points to an unverified image')
    if not observed:
        if settings.get('image_mode', 'build') == 'reuse':
            raise DeploymentError('复用模式需要本单已有可核验的 Harbor 镜像回执')
        with tempfile.TemporaryDirectory(prefix='ztfe-') as directory:
            archive = build_archive(request, Path(directory))
            expected = archive_config(archive)
            command('docker', 'load', '--input', str(archive))
            loaded = command('docker', 'image', 'inspect', '--format', '{{.Id}} {{.Os}}/{{.Architecture}}',
                             expected).stdout.split()
            if loaded != [expected, settings['platform']]:
                raise DeploymentError('Loaded frontend image does not match the archive or requested platform')
            command('docker', 'tag', expected, image)
            result['config_digest'] = expected
            write_json(journal, result)
            command('docker', 'push', image)
            observed = remote_image(image, settings['platform'])
    if not observed or observed['config_digest'] != expected:
        raise DeploymentError('Harbor did not confirm the frontend image contents')
    if saved.get('digest', observed['digest']) != observed['digest']:
        raise DeploymentError('Harbor image digest changed since the prior publication attempt')
    result.update(status='published', digest=observed['digest'])
    write_json(journal, result)
    if target := settings.get('rancher'):
        ref = settings['image_repository'] + '@' + observed['digest']

        def record(**fields):
            result.update(fields)
            write_json(journal, result)

        proof = update_and_verify(target, ref, result, record)
        record(rancher={'target': target, 'pod_verification': proof})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', type=Path, required=True)
    parser.add_argument('--journal', type=Path, required=True)
    args = parser.parse_args()
    os.environ.update(GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='never')
    try:
        publish(json.loads(args.request.read_text(encoding='utf-8')), args.journal)
    except (DeploymentError, RancherMcpError, OSError, ValueError, KeyError, TypeError, tarfile.TarError) as error:
        print(f'前端镜像发布失败：{error}', flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
