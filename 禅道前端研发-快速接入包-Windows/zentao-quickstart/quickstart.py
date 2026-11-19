"""Windows 快速接入。以当前文件所在目录作为工作区。"""
import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit

from support.client import Zt
from support.win_secret import _dpapi

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / '.agents' / 'zentao-project.json'
sys.path.insert(0, str(ROOT / '.agents' / 'skills' / 'zentao-autodelivery' / 'scripts'))
from branch_check import inspect_repository
from ledger import validate_config


def load_config():
    config = json.loads(CONFIG.read_text(encoding='utf-8-sig'))
    validate_config(config)
    return config


def site(url):
    parts = urlsplit(url)
    if (parts.scheme not in ('http', 'https') or not parts.hostname or parts.username is not None
            or parts.query or parts.fragment or parts.hostname.endswith('.invalid')):
        raise ValueError('请填写实际的 HTTP(S) 地址，地址中只保留主机、端口和部署路径。')
    return url.rstrip('/')


def binding(config, kind):
    if kind == 'zentao':
        section = config['zentao']
        url = section['base_url']
    elif kind == 'e2e':
        section = config['e2e']['login']
        url = config['e2e']['ready_url']
    else:
        raise ValueError('未知凭据类型')
    ref = section['credential_ref']
    if ref != 'windows-dpapi:' + kind:
        raise ValueError('快速接入包的凭据引用应为 windows-dpapi:' + kind)
    fields = [kind, ref, site(url), config['zentao']['project_name'], section.get('username', '')]
    digest = hashlib.sha256(json.dumps(fields).encode()).hexdigest()
    return ROOT / '.local' / 'credentials' / (digest + '.bin')


def save(config, kind, account, password):
    path = binding(config, kind)
    if not account.strip() or not password:
        raise ValueError('账号和密码均须填写。')
    blob = _dpapi(json.dumps([account, password]).encode(), True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        handle.write(blob)
    Path(handle.name).replace(path)


def credential(config, kind):
    path = binding(config, kind)
    if not path.is_file():
        raise ValueError(f'请先执行 credentials 录入 {kind} 凭据。')
    return json.loads(_dpapi(path.read_bytes(), False))


def zen_client(config):
    account, password = credential(config, 'zentao')
    client = Zt(site(config['zentao']['base_url']), account, password)
    try:
        client.login()
    except RuntimeError:
        raise ValueError('禅道登录失败，请核对实例地址、网络、REST API 和账号密码。') from None
    return client


def check(config):
    issues = []
    zen = config['zentao']
    site(zen['base_url'])
    for value in [zen['project_name'], *zen['assigned_to_accounts']]:
        if value.startswith('填写'):
            issues.append('替换禅道项目全名及接单账号的占位文字')
            break
    if not zen['assigned_to_accounts']:
        issues.append('填写至少一个接单账号')
    e2e = config['e2e']
    site(e2e['ready_url'])
    if not e2e['start_command'].strip() or not e2e['platform_entry_path'].startswith('/'):
        issues.append('核对前端启动命令及平台入口路径')
    if e2e.get('login') is not None:
        login = e2e['login']
        if not login['username'].strip() or login['username'].startswith('填写') or not login['path'].startswith('/'):
            issues.append('填写测试账号及登录路径')
    for name in ('requirement', 'frontend'):
        if not (CONFIG.parent / config['workspaces'][name]).is_dir():
            issues.append(f'找不到 {name} 仓库目录，请核对 workspaces')
    frontend = (CONFIG.parent / config['workspaces']['frontend']).resolve()
    bundle = config['development']['frontend_bundle']['applications']
    if not bundle:
        issues.append('请至少配置一个应用及目标分支')
    for app, target in bundle.items():
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', app):
            issues.append(f'应用键 {app} 应使用字母、数字、点、下划线或连字符')
        result = inspect_repository(frontend, target, '', None)
        if result['status'] != 'resolved':
            issues.append(f"{app} 仓库或分支核验失败 ({result['status']})")
    daemon = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
    if not (daemon / 'packages/app-server-daemon/current/bin/codex.exe').is_file():
        issues.append('未发现工单启动器所需的 Codex 桌面 App Server CLI')
    for kind in credential_types(config):
        credential(config, kind)
    if issues:
        raise ValueError('\n'.join(issues))
    print('本机配置检查通过。已验证仓库、目标分支、凭据解密和 CLI 文件。')


def credential_types(config):
    return ['zentao', 'e2e'] if config['e2e'].get('login') is not None else ['zentao']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['credentials', 'check', 'login-test'])
    args = parser.parse_args()
    if os.name != 'nt':
        raise ValueError('工单启动器和凭据存储需要 Windows。')
    config = load_config()
    if args.command == 'credentials':
        if not sys.stdin.isatty():
            raise ValueError('请在同事本机的交互式终端中运行 credentials。')
        for kind in credential_types(config):
            binding(config, kind)
            if kind == 'zentao':
                account = input('禅道登录账号 > ').strip()
            else:
                account = config['e2e']['login']['username']
            print(f'{kind} 账号 {account}')
            save(config, kind, account, getpass.getpass('密码（隐藏输入） > '))
        print('凭据已保存到当前工作区 .local/credentials。')
    elif args.command == 'check':
        if not shutil.which('git'):
            raise ValueError('未找到 git，请安装并重新打开终端。')
        check(config)
    else:
        client = zen_client(config)
        print('禅道登录成功，REST ' + client.version + '。项目归属与分页请在监测聊天中核对。')


if __name__ == '__main__':
    try:
        main()
    except (OSError, KeyError, TypeError, AttributeError, ValueError, subprocess.TimeoutExpired) as exc:
        sys.exit('接入检查未完成：' + str(exc))
