"""SakuraFrp（natfrp.com）开放 API 客户端，接口定义见 https://github.com/natfrp/api。

使用用户后台复制的 API Token（Bearer）认证。DSTCamp 创建的隧道按命名约定在 list_tunnels()
结果中现查，不在本地缓存隧道 ID。
"""

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from dstools import __version__
from dstools.shared.ssl_context import default_ssl_context

_API_BASE = "https://api.natfrp.com/v4"
_USER_AGENT = f"DSTCamp/{__version__} (+https://github.com/chengzhirenchaoshuai/DSTCamp)"

# 坑：樱花的 Cloudflare WAF 会以 403（error code: 1010）拦截默认的 Python-urllib UA，换一个自定义 UA 即可

# /nodes 返回的 flag 位域（见 github.com/natfrp/api 的 openapi.yaml）。
_NODE_FLAG_ALLOW_CREATE = 1 << 2
_NODE_FLAG_MAINLAND = 1 << 3
_NODE_FLAG_UDP = 1 << 5
_NODE_FLAG_OFFLINE = 1 << 9
_NODE_FLAG_PUBLIC_GAME = 1 << 11  # 官方说明："是否允许穿透公开游戏"


def is_valid_token(token: str) -> bool:
    """粗略校验访问密钥：旧版 16 位、新版 36 位都要放行，官方 API 不限定长度，真假交给接口判断。"""
    return len(token) >= 16 and token.isascii() and not any(ch.isspace() for ch in token)


class SakuraApiError(Exception):
    """SakuraFrp API 调用失败，保留原始响应文本方便把真实错误显示给用户。"""

    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


def _request(token: str, method: str, path: str, params: dict | None = None, timeout: float = 10.0):
    """所有公开函数唯一的底层调用点。GET 用 querystring，POST 用
    application/x-www-form-urlencoded（跟官方 OpenAPI 定义一致）。"""
    url = _API_BASE + path
    data = None
    if params:
        encoded = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        if method == "GET":
            url = f"{url}?{encoded}"
        else:
            data = encoded.encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("User-Agent", _USER_AGENT)
    if data is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=default_ssl_context()) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        # 错误响应通常是 {"code", "msg"}，把 msg 放进异常消息供界面直接显示；解析失败退回原始 body/状态码
        detail = body
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict) and parsed.get("msg"):
                detail = parsed["msg"]
        except (json.JSONDecodeError, TypeError):
            pass
        raise SakuraApiError(f"HTTP {e.code}: {detail}" if detail else f"HTTP {e.code}",
                              status=e.code, body=body) from e
    except urllib.error.URLError as e:
        raise SakuraApiError(str(e.reason), status=None, body="") from e
    if not body:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body


def list_tunnels(token: str) -> list[dict]:
    return _request(token, "GET", "/tunnels") or []


def create_tunnel(token: str, *, name: str, type: str, node: int,
                   local_ip: str, local_port: int, note: str | None = None,
                   remote: int | None = None) -> dict:
    """remote 为远程端口，None 时由樱花随机分配；隧道建好后远程端口不能再改（/tunnel/edit 不接受 remote）。"""
    return _request(token, "POST", "/tunnels", {
        "name": name, "type": type, "node": node,
        "local_ip": local_ip, "local_port": local_port, "note": note, "remote": remote,
    })


def edit_tunnel(token: str, tunnel_id: int, **fields) -> dict:
    return _request(token, "POST", "/tunnel/edit", {"id": tunnel_id, **fields})


def tunnel_node_id(tunnel: dict) -> int | None:
    """隧道所在节点 ID。list_tunnels 返回的是整数，创建接口的返回里实测为空。"""
    try:
        return int(tunnel.get("node"))
    except (TypeError, ValueError):
        return None


def migrate_tunnel(token: str, tunnel_id: int, node: int) -> None:
    """把隧道迁移到另一个节点，隧道 ID 不变（成功返回 204）。实测目标节点上原远程端口空闲时端口保持不变，
    端口是否一定保留文档未说明，调用方要重新读取隧道的 remote。"""
    _request(token, "POST", "/tunnel/migrate", {"id": tunnel_id, "node": node})


def delete_tunnel(token: str, tunnel_ids: list[int]) -> dict:
    ids = ",".join(str(i) for i in tunnel_ids[:10])
    return _request(token, "POST", "/tunnel/delete", {"ids": ids})


def get_traffic(token: str, tunnel_id: int) -> dict:
    """返回 {时间戳: 流量字节数}，单条隧道范围内，不是账号总量。"""
    return _request(token, "GET", "/tunnel/traffic", {"id": tunnel_id}) or {}


def get_user_info(token: str) -> dict:
    """``GET /user/info``：tunnels（隧道数上限）、group.level（与节点 vip 字段比较判断可用性）、
    traffic（[今日已用, 总剩余] 字节）。"""
    return _request(token, "GET", "/user/info") or {}


def list_nodes(token: str) -> dict:
    """返回 {节点ID(字符串): {name, host, description, vip, flag}}。"""
    return _request(token, "GET", "/nodes") or {}


def node_supports_udp(node: dict) -> bool:
    flag = node.get("flag", 0)
    return bool(flag & _NODE_FLAG_UDP) and not (flag & _NODE_FLAG_OFFLINE)


def node_accepts_new_tunnel(node: dict) -> bool:
    flag = node.get("flag", 0)
    return bool(flag & _NODE_FLAG_ALLOW_CREATE) and not (flag & _NODE_FLAG_OFFLINE)


def node_is_mainland(node: dict) -> bool:
    return bool(node.get("flag", 0) & _NODE_FLAG_MAINLAND)


def node_allows_public_game(node: dict) -> bool:
    return bool(node.get("flag", 0) & _NODE_FLAG_PUBLIC_GAME)


def get_node_loads(token: str) -> dict[int, float]:
    """``GET /node/stats``：{节点ID: 负载百分比}，离线（online 为 -1）的节点不返回。"""
    data = _request(token, "GET", "/node/stats") or {}
    loads = {}
    for item in (data.get("nodes") or []) if isinstance(data, dict) else []:
        load = item.get("load")
        if item.get("online", 0) >= 0 and isinstance(load, (int, float)):
            loads[int(item["id"])] = float(load)
    return loads


def node_sort_key(node: dict, eligible: bool) -> tuple:
    """中国大陆在前 → 组内可选的在前 → VIP 等级从低到高 → 名称自然排序（"PLUS2" 排在 "PLUS10" 前）。
    不按负载排：负载是实时值，每次刷新顺序都会跳。"""
    name = str(node.get("name") or "")
    natural = tuple((0, int(part), "") if part.isdigit() else (1, 0, part)
                    for part in re.split(r"(\d+)", name) if part)
    return not node_is_mainland(node), not eligible, node.get("vip", 0), natural


def sanitize_tunnel_name(cluster_folder_name: str, shard_name: str, source: str, platform: str,
                         cluster_identity: str | None = None) -> str:
    """生成确定性的隧道名（短哈希）。

    坑：隧道名只允许 3-20 个字母数字下划线（不允许短横线），存档目录名不可控，所以用哈希。
    source 和 platform 必须参与哈希：不同来源/平台的存档可能同名，只按目录名+世界名会互相冒充映射状态。"""
    identity = cluster_identity or cluster_folder_name
    digest = hashlib.sha1(f"{platform}:{source}:{identity}:{shard_name}".encode("utf-8")).hexdigest()
    return f"dc_{digest[:12]}"


def find_dstcamp_tunnel(tunnels: list[dict], cluster_folder_name: str, shard_name: str,
                         source: str, platform: str, cluster_identity: str | None = None,
                         allow_legacy: bool = True) -> dict | None:
    """按命名约定在 list_tunnels() 结果里找这个 (存档, 世界) 对应的隧道。"""
    names = [sanitize_tunnel_name(
        cluster_folder_name, shard_name, source, platform, cluster_identity,
    )]
    if cluster_identity is not None and allow_legacy:
        names.append(sanitize_tunnel_name(cluster_folder_name, shard_name, source, platform))
    for t in tunnels:
        if t.get("name") in names:
            return t
    return None
