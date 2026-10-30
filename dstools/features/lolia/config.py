"""Lolia 映射的纯逻辑：识别用户粘贴的隧道信息，改写成本地原版 frpc 可用的配置。

主要来源是控制台复制的「原版 frpc 配置」；粘贴「LoliaFRP-CLI 快捷启动」命令时才调用官方免鉴权接口
``GET /api/v1/tunnel/frpc/config?token=<节点Token>&id=<隧道ID>``（文档 https://api-docs.lolia.link/403472745e0.md）
取 Base64 TOML，交给原版 frpc.exe 运行（Lolia 客户端的 ``-t id:token`` 原版不认）。

本地改写两处：
1. 每条代理的 localIP/localPort 改为 127.0.0.1 + 远程端口（与樱花/自建一致，世界 server_port 改为远程端口，
   对外声明的端口才与公网一致；localPort 只在客户端生效）；
2. 补 ``transport.tls.serverName = " "``（官方 FAQ：原版 frpc 不加会出现 EOF/session shutdown）。
"""

import base64
import binascii
import json
import re
import urllib.error
import urllib.parse
import urllib.request

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 没有 tomllib；打包版用的是 3.11+
    tomllib = None

from dstools import __version__
from dstools.shared import frpc_diagnosis
from dstools.shared.ssl_context import default_ssl_context

API_BASE = "https://api.lolia.link/api/v1"
DASHBOARD_URL = "https://dash.lolia.link"
_USER_AGENT = f"DSTCamp/{__version__} (+https://github.com/chengzhirenchaoshuai/DSTCamp)"
_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


class LoliaError(Exception):
    """拉取或解析配置失败；消息尽量是 Lolia 返回的原话，界面直接显示 str(e)。"""


def fetch_config_text(token: str, tunnel_ids: list[int], timeout: float = 10.0) -> str:
    """调免鉴权接口，返回解码后的 TOML 文本。多个 ID 由服务端合并成一份配置。"""
    query = urllib.parse.urlencode({"token": token, "id": ",".join(str(i) for i in tunnel_ids)})
    req = urllib.request.Request(f"{API_BASE}/tunnel/frpc/config?{query}", method="GET")
    req.add_header("User-Agent", _USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=default_ssl_context()) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        # 错误时 HTTP 状态码和 body 里的 code 一致（实测 Token 错误返回 404 + {"code":404,"msg":"Token无效"}）
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        raise LoliaError(_error_message(body) or f"HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise LoliaError(str(e.reason)) from e
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as e:
        raise LoliaError(body[:200] or "empty response") from e
    if not isinstance(parsed, dict) or parsed.get("code") != 200:
        raise LoliaError(_error_message(body) or body[:200])
    raw = (parsed.get("data") or {}).get("config") or ""
    try:
        return base64.b64decode(raw, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as e:
        raise LoliaError(f"invalid config payload: {e}") from e


def _error_message(body: str) -> str:
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return ""
    return str(parsed.get("msg") or "") if isinstance(parsed, dict) else ""


def parse_toml(text: str) -> dict:
    if tomllib is None:
        raise LoliaError("Python 3.11+ required (tomllib)")
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise LoliaError(f"invalid frpc config: {e}") from e


def single_proxy_name(config: dict) -> str:
    """单个隧道 ID 拉回的配置里应当恰好一条代理，返回它的 name，用来在合并配置里认领。"""
    proxies = config.get("proxies") or []
    if len(proxies) != 1 or not proxies[0].get("name"):
        raise LoliaError(f"expected exactly one proxy, got {len(proxies)}")
    return proxies[0]["name"]


def build_local_config(merged: dict, names_by_shard: dict[str, str]) -> tuple[dict, dict[str, int]]:
    """把服务端配置改写成本地配置。``names_by_shard`` 为 {世界名: 代理 name}，返回 (配置, {世界名: 远程端口})。
    非 UDP、缺远程端口、两个世界用同一条隧道时报错。"""
    proxies = merged.get("proxies") or []
    by_name = {p.get("name"): p for p in proxies}
    if len(set(names_by_shard.values())) != len(names_by_shard):
        raise LoliaError("duplicate tunnel")
    ports: dict[str, int] = {}
    for shard, name in names_by_shard.items():
        proxy = by_name.get(name)
        if proxy is None:
            raise LoliaError(f"proxy not found: {name}")
        if proxy.get("type") != "udp":
            raise LoliaError(f"{shard}: tunnel type is {proxy.get('type')}, UDP required")
        remote = proxy.get("remotePort")
        if not isinstance(remote, int) or not (1 <= remote <= 65535):
            raise LoliaError(f"{shard}: invalid remotePort {remote!r}")
        proxy["localIP"] = "127.0.0.1"
        proxy["localPort"] = remote
        ports[shard] = remote
    if not isinstance(merged.get("serverAddr"), str) or not merged["serverAddr"]:
        raise LoliaError("serverAddr missing")
    tls = merged.setdefault("transport", {}).setdefault("tls", {})
    tls.setdefault("serverName", " ")
    return merged, ports


def parse_source(text: str) -> dict:
    """识别用户给一个世界粘贴的内容：

    - 「LoliaFRP-CLI 快捷启动」命令（``frpc -t <隧道ID>:<节点Token>``）→ {"kind": "cli", "id", "token"}，开启时联网拉配置；
    - 「原版 frpc 配置」TOML → {"kind": "config", "text"}，直接使用（坑：其中只有隧道名没有数字 ID，
      且 metadatas.token 不能用于免鉴权接口）。
    格式不对或不是单条 UDP 隧道时抛 LoliaError。"""
    text = text.strip()
    pairs = re.findall(r"(?:^|\s)-t\s+(\d+):(\S+)", text) or re.findall(r"^(\d+):(\S+)$", text)
    if pairs:
        if len(pairs) != 1:
            raise LoliaError("one tunnel per world")
        return {"kind": "cli", "id": int(pairs[0][0]), "token": pairs[0][1]}
    config = parse_toml(text)
    proxies = config.get("proxies") or []
    if len(proxies) != 1:
        raise LoliaError(f"expected exactly one proxy, got {len(proxies)}")
    if proxies[0].get("type") != "udp":
        raise LoliaError(f"tunnel type is {proxies[0].get('type')}, UDP required")
    return {"kind": "config", "text": text}


def _source_config(source: dict) -> dict:
    if source.get("kind") == "cli":
        return parse_toml(fetch_config_text(source["token"], [int(source["id"])]))
    return parse_toml(source["text"])


def prepare_shard(source: dict) -> tuple[str, int, str]:
    """把一个世界的来源转成本地配置（快捷启动命令会联网，须在后台线程）。

    每个世界一份配置一个 frpc 进程：主世界与洞穴的隧道可能在不同节点，无法合并。
    返回 (本地 TOML, 远程端口, 节点地址)。"""
    return local_config_from(_source_config(source))


def local_config_from(config: dict) -> tuple[str, int, str]:
    """单隧道配置 → (本地 TOML 文本, 远程端口, 节点地址)。OAuth 方式拉回的配置也走这里。"""
    local, ports = build_local_config(config, {"_": single_proxy_name(config)})
    return dump_toml(local), ports["_"], local["serverAddr"]


def frpc_failure_reason(lines: list[str]) -> str:
    """从 frpc 输出里提取最后一条失败原因；实现已移到 shared/frpc_diagnosis，三种穿透共用。"""
    return frpc_diagnosis.failure_reason(lines)


def dump_toml(data: dict) -> str:
    """最小 TOML 序列化：顶层标量、`[表]`、`[[表数组]]`，更深层的表写成内联表。
    只覆盖 frpc 配置会用到的类型（字符串/整数/浮点/布尔/数组/表）。"""
    scalars, tables, arrays = [], [], []
    for key, value in data.items():
        if isinstance(value, dict):
            tables.append((key, value))
        elif isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
            arrays.append((key, value))
        else:
            scalars.append(f"{_key(key)} = {_value(value)}")
    lines = list(scalars)
    for key, table in tables:
        lines += ["", f"[{_key(key)}]"] + [f"{_key(k)} = {_value(v)}" for k, v in table.items()]
    for key, items in arrays:
        for item in items:
            lines += ["", f"[[{_key(key)}]]"] + [f"{_key(k)} = {_value(v)}" for k, v in item.items()]
    return "\n".join(lines).lstrip("\n") + "\n"


def _key(key: str) -> str:
    return key if _BARE_KEY.match(key) else json.dumps(key, ensure_ascii=False)


def _value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        # JSON 字符串转义是 TOML 基本字符串转义的子集
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{_key(k)} = {_value(v)}" for k, v in value.items()) + " }"
    raise LoliaError(f"unsupported TOML value: {type(value).__name__}")
