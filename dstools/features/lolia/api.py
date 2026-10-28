"""LoliaFRP 开放 API 客户端与 OAuth2 登录（纯逻辑），接口见 https://api-docs.lolia.link/。

鉴权用 OAuth2 授权码 + PKCE（public 客户端，回调 ``http://127.0.0.1/callback``，回环地址按 RFC 8252
忽略端口，登录时临时监听随机端口）。access_token 24 小时，refresh_token 30 天且刷新时不顺延，到期须重新登录；
令牌存 security/lolia/oauth.json。隧道名由服务端随机生成，归属靠 remark 中的 ``DSTCamp dc_<哈希>`` 标记
（哈希复用樱花的 sanitize_tunnel_name）。
"""

import base64
import hashlib
import http.server
import json
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from dstools import __version__
from dstools.features.lolia.config import LoliaError
from dstools.features.sakura.api import sanitize_tunnel_name
from dstools.shared import app_settings
from dstools.shared.resource_paths import security_dir
from dstools.shared.ssl_context import default_ssl_context

DEFAULT_CLIENT_ID = "b2k7j0aejvrcld3l"  # 内置的 public 客户端，client_id 本身是公开信息
CREATE_APP_URL = "https://dash.lolia.link/dash/oauth/apps/create"
API_BASE = "https://api.lolia.link/api/v1"
AUTHORIZE_URL = "https://dash.lolia.link/oauth/authorize"
TOKEN_URL = f"{API_BASE}/oauth2/token"
SCOPES = "user:read tunnel:read tunnel:write node:read traffic:read"
REDIRECT_PATH = "/callback"
REMARK_PREFIX = "DSTCamp "
_USER_AGENT = f"DSTCamp/{__version__} (+https://github.com/chengzhirenchaoshuai/DSTCamp)"
_REFRESH_MARGIN_SECONDS = 300
_token_lock = threading.Lock()


class LoliaAuthError(LoliaError):
    """未登录或登录已失效（refresh_token 过期/被撤销），需要用户重新登录。"""

    error_code = ""  # 令牌接口返回的 OAuth error 码（如 invalid_client / invalid_grant）


class LoliaClientError(LoliaAuthError):
    """OAuth 应用（client_id）无效：被创建者删除或被平台停用，需要换一个 client_id。"""


def client_id() -> str:
    """用户在引导里填过自己的应用就用它，否则用内置的。"""
    return app_settings.get_lolia_client_id() or DEFAULT_CLIENT_ID


def check_client_id(cid: str) -> bool:
    """用假 refresh_token 请求令牌接口检查 client_id：有效返回 invalid_grant，无效返回 invalid_client（真机验证）。"""
    try:
        _post_form(TOKEN_URL, {"grant_type": "refresh_token", "client_id": cid, "refresh_token": "dstcamp_probe"})
    except LoliaAuthError as exc:
        return exc.error_code != "invalid_client"
    return True


# ── 令牌存储 ────────────────────────────────────────────────────────────
def _token_file():
    return security_dir("lolia") / "oauth.json"


def load_tokens() -> dict | None:
    try:
        data = json.loads(_token_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("refresh_token") else None


def _save_tokens(payload: dict) -> dict:
    tokens = {
        "access_token": payload["access_token"],
        "refresh_token": payload["refresh_token"],
        "expires_at": time.time() + int(payload.get("expires_in") or 86400),
        "scope": payload.get("scope", ""),
    }
    path = _token_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(tokens), encoding="utf-8")
    tmp.replace(path)
    return tokens


def clear_tokens() -> None:
    """只删本地令牌（换了 OAuth 应用后旧令牌无法再刷新时用）。"""
    _token_file().unlink(missing_ok=True)


def is_logged_in() -> bool:
    return load_tokens() is not None


def logout() -> None:
    """删除本地令牌，并尽力在服务端撤销 refresh_token（失败不影响本地退出）。"""
    tokens = load_tokens()
    _token_file().unlink(missing_ok=True)
    if tokens:
        try:
            _post_form(f"{API_BASE}/oauth2/revoke", {
                "token": tokens["refresh_token"], "token_type_hint": "refresh_token", "client_id": client_id(),
            })
        except LoliaError:
            pass


# ── OAuth2 授权码 + PKCE ────────────────────────────────────────────────
def make_pkce() -> tuple[str, str]:
    """返回 (code_verifier, S256 code_challenge)。"""
    verifier = secrets.token_urlsafe(48)[:64]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
    return verifier, challenge


def build_authorize_url(redirect_uri: str, state: str, challenge: str) -> str:
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": client_id(), "redirect_uri": redirect_uri,
        "scope": SCOPES, "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
    })


def parse_callback(path: str, expected_state: str) -> str:
    """解析回调请求路径，返回授权码；用户拒绝或 state 不符时抛异常。"""
    parsed = urllib.parse.urlparse(path)
    query = urllib.parse.parse_qs(parsed.query)
    if parsed.path != REDIRECT_PATH:
        raise LoliaAuthError(f"unexpected callback path: {parsed.path}")
    if query.get("state", [""])[0] != expected_state:
        raise LoliaAuthError("state mismatch")
    if "error" in query:
        raise LoliaAuthError(query.get("error_description", query["error"])[0])
    code = query.get("code", [""])[0]
    if not code:
        raise LoliaAuthError("missing code")
    return code


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802  http.server 约定的方法名
        server = self.server
        try:
            server.result_code = parse_callback(self.path, server.expected_state)
            ok = True
        except LoliaAuthError as exc:
            if urllib.parse.urlparse(self.path).path != REDIRECT_PATH:
                self.send_response(404)
                self.end_headers()
                return  # 浏览器顺带请求的 /favicon.ico 等，忽略
            server.result_error = exc
            ok = False
        body = ("<h3>DSTCamp: Lolia 授权完成，可以关闭此页面返回 DSTCamp。</h3>" if ok else
                "<h3>DSTCamp: Lolia 授权未完成，请返回 DSTCamp 查看原因。</h3>").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):  # 不往控制台打访问日志
        pass


def login(open_browser, cancel_event: threading.Event, timeout: float = 300.0) -> dict:
    """完整登录流程（阻塞，放后台线程）：监听回环端口 → 打开浏览器授权 → 收回调 →
    换令牌并保存。`open_browser(url)` 由调用方提供；`cancel_event` 置位即中止。"""
    if not check_client_id(client_id()):
        raise LoliaClientError("invalid_client")
    server = http.server.HTTPServer(("127.0.0.1", 0), _CallbackHandler)
    server.timeout = 0.5
    server.expected_state = secrets.token_urlsafe(24)
    server.result_code = None
    server.result_error = None
    redirect_uri = f"http://127.0.0.1:{server.server_address[1]}{REDIRECT_PATH}"
    verifier, challenge = make_pkce()
    try:
        open_browser(build_authorize_url(redirect_uri, server.expected_state, challenge))
        deadline = time.monotonic() + timeout
        while server.result_code is None and server.result_error is None:
            if cancel_event.is_set():
                raise LoliaAuthError("cancelled")
            if time.monotonic() > deadline:
                raise LoliaAuthError("timeout")
            server.handle_request()
    finally:
        server.server_close()
    if server.result_error is not None:
        raise server.result_error
    payload = _post_form(TOKEN_URL, {
        "grant_type": "authorization_code", "client_id": client_id(), "code": server.result_code,
        "redirect_uri": redirect_uri, "code_verifier": verifier,
    })
    with _token_lock:
        return _save_tokens(payload)


def _post_form(url: str, fields: dict) -> dict:
    """令牌类接口：RFC 6749 风格，失败返回 {error, error_description}，不带 code/msg 包装。"""
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode("ascii"), method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("User-Agent", _USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=15, context=default_ssl_context()) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        code = ""
        try:
            err = json.loads(body)
            code = str(err.get("error") or "")
            detail = err.get("error_description") or code or body
        except (ValueError, AttributeError):
            detail = body or f"HTTP {e.code}"
        exc = (LoliaClientError if code == "invalid_client" else LoliaAuthError)(detail)
        exc.error_code = code
        raise exc from e
    except urllib.error.URLError as e:
        raise LoliaError(str(e.reason)) from e
    return json.loads(body) if body.strip() else {}


def _access_token() -> str:
    """返回可用的 access_token，快过期时先用 refresh_token 刷新；刷新被拒则清除登录状态。"""
    with _token_lock:
        tokens = load_tokens()
        if tokens is None:
            raise LoliaAuthError("not logged in")
        if tokens.get("expires_at", 0) - time.time() > _REFRESH_MARGIN_SECONDS:
            return tokens["access_token"]
        try:
            payload = _post_form(TOKEN_URL, {
                "grant_type": "refresh_token", "client_id": client_id(), "refresh_token": tokens["refresh_token"],
            })
        except LoliaAuthError:
            _token_file().unlink(missing_ok=True)
            raise
        payload.setdefault("refresh_token", tokens["refresh_token"])  # 官方：刷新时 refresh_token 保持不变
        return _save_tokens(payload)["access_token"]


# ── 资源接口 ────────────────────────────────────────────────────────────
def _request(method: str, path: str, *, query: dict | None = None, body: dict | None = None):
    url = API_BASE + path + ("?" + urllib.parse.urlencode(query) if query else "")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {_access_token()}")
    req.add_header("User-Agent", _USER_AGENT)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15, context=default_ssl_context()) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", errors="replace") if e.fp else ""
        try:
            msg = json.loads(text).get("msg") or text
        except (ValueError, AttributeError):
            msg = text or f"HTTP {e.code}"
        if e.code == 401:
            raise LoliaAuthError(msg) from e
        raise LoliaError(msg) from e
    except urllib.error.URLError as e:
        raise LoliaError(str(e.reason)) from e
    parsed = json.loads(text) if text.strip() else {}
    if isinstance(parsed, dict) and parsed.get("code") not in (None, 200, 201):
        raise LoliaError(parsed.get("msg") or text[:200])
    return parsed.get("data") if isinstance(parsed, dict) else parsed


def get_user_info() -> dict:
    """关键字段：username、max_tunnel_count、traffic_limit/traffic_used（字节）、
    bandwidth_limit（MB/s，0 不限）、has_qq（未绑 QQ 不能建隧道）、has_kyc。"""
    return _request("GET", "/user/info") or {}


def list_nodes() -> list[dict]:
    """注意：官方要求 POST 且必须带 JSON 体，空体返回 400。"""
    data = _request("POST", "/user/nodes", body={"page": 1, "limit": 1000}) or {}
    return data.get("nodes") or []


def list_tunnels() -> list[dict]:
    tunnels, page = [], 1
    while True:
        data = _request("GET", "/user/tunnel", query={"page": page, "limit": 100}) or {}
        tunnels += data.get("list") or []
        if page >= int(data.get("total_page") or 1):
            return tunnels
        page += 1


def create_udp_tunnel(node_id: int, local_port: int, remark: str, remote_port: int | None = None) -> dict:
    """remote_port 为 None 时不传，由服务端自动分配（须在节点可用范围内且未被占用，否则 409）；
    隧道名由服务端随机生成。"""
    body = {
        "node_id": int(node_id), "type": "udp", "local_ip": "127.0.0.1",
        "local_port": int(local_port), "remark": remark,
    }
    if remote_port is not None:
        body["remote_port"] = int(remote_port)
    return _request("POST", "/user/tunnel", body=body) or {}


def edit_tunnel(tunnel_name: str, **fields) -> dict:
    return _request("PUT", f"/user/tunnel/{urllib.parse.quote(tunnel_name)}", body=fields) or {}


def delete_tunnel(tunnel_name: str) -> None:
    _request("DELETE", f"/user/tunnel/{urllib.parse.quote(tunnel_name)}")


def get_frpc_config_text(tunnel_name: str) -> str:
    """该隧道的原版 frpc 标准配置（官方 `config` 字段，Base64 解码后的 TOML）。"""
    data = _request("GET", "/user/frpc/config", query={"tunnel": tunnel_name}) or {}
    try:
        return base64.b64decode(data.get("config") or "", validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as e:
        raise LoliaError(f"invalid config payload: {e}") from e


def available_traffic(user: dict) -> int:
    """可用流量（字节）。官方文档：traffic_remaining 直接取 traffic_limit、扣费从额度里扣，
    即 traffic_limit 本身就是余额，不能再减 traffic_used。额度靠控制台每日签到获取。"""
    return max(0, int(user.get("traffic_limit") or 0))


def bandwidth_mbps(user: dict) -> int:
    """账号限速换算成 Mbps（控制台显示单位）。接口 bandwidth_limit 单位是 MB/s，0 表示不限；
    真机 10 MB/s，对应官方 FAQ "单隧道限速 80Mbps"。"""
    return int(user.get("bandwidth_limit") or 0) * 8


def node_supports_udp(node: dict) -> bool:
    return "udp" in (node.get("supported_protocols") or []) and node.get("status") != "offline"


REGION_GROUPS = ("cn", "hk_tw", "jp_kr", "other")  # 节点弹窗里的分组顺序：延迟从低到高的大致顺序
_REGION_BY_CODE = {"CN": "cn", "HK": "hk_tw", "MO": "hk_tw", "TW": "hk_tw", "JP": "jp_kr", "KR": "jp_kr"}


def node_region(node: dict) -> str:
    """按节点 region_code（国家/地区代码）归到 REGION_GROUPS 之一。"""
    return _REGION_BY_CODE.get(str(node.get("region_code") or "").upper(), "other")


def node_sort_key(node: dict, eligible: bool) -> tuple:
    """地区分组 → 组内可选的在前 → 名称自然排序（"中国香港-2" 排在 "中国香港-10" 前）。
    不按负载排：负载是实时值，每次刷新顺序都会跳。"""
    name = str(node.get("name") or "")
    natural = tuple((0, int(part), "") if part.isdigit() else (1, 0, part)
                    for part in re.split(r"(\d+)", name) if part)
    return REGION_GROUPS.index(node_region(node)), not eligible, natural


def make_remark(cluster_folder_name: str, shard_name: str, source: str, platform: str,
                cluster_identity: str | None) -> str:
    return REMARK_PREFIX + sanitize_tunnel_name(cluster_folder_name, shard_name, source, platform, cluster_identity)


def find_tunnel(tunnels: list[dict], remark: str) -> dict | None:
    return next((t for t in tunnels if (t.get("remark") or "").strip() == remark), None)
