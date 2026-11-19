import json
import urllib.error
import urllib.request

def _json(raw):
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return None

class Zt:
    """禅道 API 客户端。自动在 v2 / v1 之间选择可用的接口前缀。

    v2（禅道 18+）：/api.php/v2，支持 /my/* 直接取「我的」数据
    v1（禅道 12~17）：/api.php/v1，无 /my/*，只能按项目/迭代枚举后过滤
    """

    def __init__(self, base, account, password, api_version="auto"):
        self.base = base.rstrip("/")
        self.account = account
        self.password = password
        self.want = (api_version or "auto").lower()
        self.prefix = ""
        self.token = None
        self.version = ""

    # ---------- 登录 ----------
    def login(self):
        order = [("v2", "/api.php/v2"), ("v1", "/api.php/v1")]
        if self.want in ("v1", "v2"):
            order = [o for o in order if o[0] == self.want]

        errors = []
        for ver, prefix in order:
            path = f"{prefix}/users/login" if ver == "v2" else f"{prefix}/tokens"
            st, raw = self._raw_abs(path, {"account": self.account, "password": self.password})
            data = _json(raw) or {}
            # v1 /tokens 成功时返回 201，v2 /users/login 返回 200，统一按 2xx 判断
            if 200 <= st < 300 and data.get("token"):
                self.version = ver
                self.prefix = prefix
                self.token = data["token"]
                return data.get("user", {})
            errors.append(f"{ver} 登录失败（HTTP {st}）：{raw[:120]}")
        raise RuntimeError("禅道登录失败，请检查 base_url / account / password。\n  " + "\n  ".join(errors))

    # ---------- 请求 ----------
    def _raw_abs(self, path, body=None, method=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method or ("POST" if data else "GET"))
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("token", self.token)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            return 0, str(e)

    def get(self, rel_path):
        """rel_path 以 / 开头，自动补接口前缀。"""
        return self._raw_abs(self.prefix + rel_path)

    def has_my_api(self):
        return self.version == "v2"
