"""Authenticated ZenTao client shared by intake and original-item writebacks."""
import json
import sys
from types import SimpleNamespace
from functools import wraps
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'zentao-dev-task-flow' / 'scripts'))
from zentao_dev_task import ZenTaoError, ZenTaoSession
from runtime_paths import LOCAL_DIRECTORY

CREDENTIAL_PATH = LOCAL_DIRECTORY / "zentao-credentials.json"


class RemoteRejected(ValueError):
    """The read failed, or the server rejected the write before applying it."""
    safe_to_retry = True


class _ApiRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A REST redirect is neither a login receipt nor a write acknowledgement.
        return None


class _TaskSession(ZenTaoSession):
    def __init__(self, base_url):
        super().__init__(base_url, timeout=10)
        self._opener = build_opener(_ApiRedirects())

    def _open(self, request):
        try:
            response = self._opener.open(request, timeout=self.timeout)
        except HTTPError as error:
            response = error
        with response:
            if 300 <= response.status < 400:
                raise ZenTaoError('禅道 REST 接口返回重定向', status=response.status)
            return response.status, response.headers, response.read()


def _read(method):
    @wraps(method)
    def invoke(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except RemoteRejected:
            raise
        except Exception:
            # The shared client may include response bodies or credential metadata.
            raise RemoteRejected('禅道读取失败，待重新核对') from None
    return invoke


def _credentials(cfg):
    try:
        record = json.loads(CREDENTIAL_PATH.read_text(encoding='utf-8-sig'))
        if (not isinstance(record, dict) or not isinstance(record.get('base_url'), str)
                or record['base_url'].rstrip('/') != cfg['base_url'].rstrip('/')):
            raise ValueError
        username, password = record['username'], record['password']
        if not isinstance(username, str) or not username.strip() or not isinstance(password, str) or not password:
            raise ValueError
        return username, password
    except (ValueError, KeyError, OSError):
        raise RemoteRejected('Check base_url, username and password in .local/zentao-credentials.json') from None


@_read
def get_remote(config_snapshot):
    cfg = config_snapshot['zentao']
    session = _TaskSession(cfg['base_url'])
    username, password = _credentials(cfg)
    session.login(username, password)
    return SimpleNamespace(session=session, account=username)
