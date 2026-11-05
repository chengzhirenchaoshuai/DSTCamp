"""本地存档（游戏客户端"创建游戏"）的房间设置：读写 Master/save/shardindex 的顶层 server 表。

游戏前端选中存档时，服务器设置页从 shardindex.server 读名称、密码、描述、人数、PVP 等
（ServerSettingsTab:SetDataForSlot），开服时再据此重写 cluster.ini 的对应字段
（ServerCreationScreen:Create 的 cluster_info.settings），所以本地存档只改 cluster.ini 会被覆盖。
只有 Master 的 shardindex 存 server（ShardIndex:SetServerData 对非主世界直接返回）。

游戏缓存与运行时覆盖的限制同 features/mod/shardindex.py 模块说明，保存前须用 local_save_play_state() 判断。
"""

import os
from pathlib import Path

from dstools.features.mod.shardindex import (
    ShardIndexError,
    _HEADER_LEN,
    _PLAIN_HEADER,
    _find_top_level_value,
    decode_persistent_bytes,
    shardindex_path,
)
from dstools.i18n import t
from dstools.shared.lua_parser import parse_lua_table, serialize_lua_table

_KEY = "server"
# cluster.ini 字段 -> shardindex.server 的键：游戏服务器设置页里能改、开服时原样写进 cluster.ini 的几项。
# game_mode、online_mode 建档后游戏界面锁定；隐私类型与 Steam 群组的映射不是一一对应，暂不开放
LOCAL_SERVER_FIELDS = {
    ("NETWORK", "cluster_name"): "name",
    ("NETWORK", "cluster_password"): "password",
    ("NETWORK", "cluster_description"): "description",
    ("GAMEPLAY", "max_players"): "max_players",
    ("GAMEPLAY", "pvp"): "pvp",
}
# 游戏内建服人数上限（scripts/tuning.lua 的 TUNING.MAX_SERVER_SIZE），专服才能到 64
LOCAL_MAX_PLAYERS_LIMITS = (1, 6)
# server.privacy_type 取值（scripts/constants.lua 的 PRIVACY_TYPE）：PUBLIC=0, FRIENDS=1, LOCAL=2, CLAN=3
PRIVACY_PUBLIC = 0
PRIVACY_LOCAL = 2


def load_local_server_data(cluster_path: Path) -> dict:
    """读取 Master shardindex 的 server 表；文件缺失、损坏或没有 server 表抛 ShardIndexError。"""
    path = shardindex_path(cluster_path)
    try:
        text, _ = decode_persistent_bytes(path.read_bytes())
        data = parse_lua_table(text, str(path))
    except ShardIndexError:
        raise
    except Exception as exc:
        raise ShardIndexError(f"无法读取 {path}：{exc}") from exc
    server = data.get(_KEY)
    if not isinstance(server, dict) or not server:
        raise ShardIndexError(f"{path} 中没有服务器设置（server）")
    return server


def local_form_values(server: dict) -> dict[tuple[str, str], object]:
    """把 server 表换成表单字段值（只含 LOCAL_SERVER_FIELDS 中 server 表里有的项）。"""
    return {field: server[key] for field, key in LOCAL_SERVER_FIELDS.items() if key in server}


def validate_local_values(values: dict) -> str | None:
    """按游戏服务器设置页的规则校验，返回第一条错误文案，全部合法返回 None。"""
    name = values.get(("NETWORK", "cluster_name"))
    if name is not None and not str(name).strip():
        return t("cluster.local_name_empty")
    password = values.get(("NETWORK", "cluster_password"))
    # 游戏不接受首尾带空白的密码（ServerSettingsTab:VerifyValidPassword）
    if password is not None and str(password) != str(password).strip():
        return t("cluster.local_password_space")
    raw = values.get(("GAMEPLAY", "max_players"))
    if raw is not None:
        lo, hi = LOCAL_MAX_PLAYERS_LIMITS
        try:
            number = int(raw)
        except (TypeError, ValueError):
            number = None
        if number is None or not lo <= number <= hi:
            return t("cluster.local_max_players_range", min=lo, max=hi)
    return None


def merge_local_values(server: dict, values: dict) -> dict:
    """在原 server 表上按表单值改出新表，不认识的键（playstyle、clan 等）原样保留。"""
    merged = dict(server)
    for field, key in LOCAL_SERVER_FIELDS.items():
        if field not in values:
            continue
        raw = values[field]
        if key == "max_players":
            merged[key] = int(raw)
        elif key == "pvp":
            merged[key] = bool(raw)
        else:
            merged[key] = str(raw)
    return merged


def is_local_save_offline(cluster_path: Path) -> bool:
    """建档时选了离线模式（游戏里建档后不能再改）；离线和在线的玩家身份不同，改模式后原角色对不上。"""
    return load_local_server_data(cluster_path).get("online_mode") is False


def make_local_save_public(cluster_path: Path) -> bool:
    """把"仅限本地"改成"公共"（内网穿透映射要求关闭仅限局域网）；返回是否改动。
    只改 shardindex：开服前同步与游戏开服都按它重写 cluster.ini，只改 cluster.ini 会被改回去。"""
    server = load_local_server_data(cluster_path)
    if server.get("privacy_type") != PRIVACY_LOCAL:
        return False
    save_local_server_data(cluster_path, {**server, "privacy_type": PRIVACY_PUBLIC})
    return True


def save_local_server_data(cluster_path: Path, server: dict) -> None:
    """只替换 server 一段，文件其余内容逐字节保留；写前重新解析校验，原子替换。"""
    path = shardindex_path(cluster_path)
    raw = path.read_bytes()
    if not raw.startswith(_PLAIN_HEADER):
        raise ShardIndexError("shardindex 不是明文格式，不能安全写回")
    text = raw[_HEADER_LEN:].decode("utf-8")
    span = _find_top_level_value(text, _KEY)
    if span is None:
        raise ShardIndexError("shardindex 中没有服务器设置（server）")
    value = serialize_lua_table(server).removeprefix("return ")
    new_text = text[:span[0]] + value + text[span[1]:]

    before = parse_lua_table(text, str(path))
    after = parse_lua_table(new_text, str(path))
    before.pop(_KEY, None)
    written = after.pop(_KEY, None)
    if before != after or written != server:
        raise ShardIndexError("写回校验失败：服务器设置以外的内容发生了变化")

    tmp = path.with_name(path.name + ".dstcamp.tmp")
    tmp.write_bytes(_PLAIN_HEADER + new_text.encode("utf-8"))
    os.replace(tmp, path)
