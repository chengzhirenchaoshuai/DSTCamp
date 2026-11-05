"""本地存档（游戏客户端"创建游戏"）的 Mod 启用状态与配置：读写 Master/save/shardindex 的 enabled_mods。

游戏前端选中存档时从 shardindex.enabled_mods 加载 Mod（ShardIndex:LoadEnabledServerMods），开服时再据此
生成各世界的 modoverrides.lua，所以只改 modoverrides.lua 会被覆盖；enabled_mods 与 modoverrides.lua 格式相同
（见游戏脚本 ModIndex:ApplyEnabledOverrides 的注释）。只有 Master 的 shardindex 生效（GetEnabledServerMods）。

游戏前端首次访问某存档时读入整个 shardindex（世界设置与 Mod 同在一个对象里）并一直缓存，只有回到
"登陆中..."开始页面（重启游戏、主菜单模组页点应用、退出别人的服务器）才重读；开存档时会用缓存写回。
所以游戏开着时可以改，但要先让游戏回到开始页面再开这个存档；存档运行时开服进程自动存档会覆盖，不能改
（2026-10-09 用户实测）。见 local_save_play_state()。
"""

import base64
import os
import struct
import zlib
from pathlib import Path

from dstools.features.mod.manager import CLIENT_MODS_DISABLED_KEY
from dstools.models import ModEntry
from dstools.shared.lua_parser import parse_lua_table, serialize_lua_table

# TheSim:SetPersistentString 写入的 11 字节文件头：末位空格为明文，"D" 为 Base64(16 字节头 + zlib)。
_HEADER_LEN = 11
_PLAIN_HEADER = b"KLEI     1 "
_ENCODED_FLAG = b"D"
_KEY = "enabled_mods"


class ShardIndexError(Exception):
    """shardindex 无法读取或不能安全写回。"""


def shardindex_path(cluster_path: Path) -> Path:
    return Path(cluster_path) / "Master" / "save" / "shardindex"


LOCAL_SAVE_CLOSED = "closed"    # 游戏客户端没开，直接改
LOCAL_SAVE_OPEN = "open"        # 游戏开着但存档没运行：可改，需提醒回开始页面后再开存档
LOCAL_SAVE_RUNNING = "running"  # 存档可能正在运行：开服进程会覆盖，不能改
LOCAL_SAVE_HOSTED = "hosted"    # 本地服务器页正开着这个存档（界面层判断）：关服时会回写，不能改


def local_save_play_state(cluster) -> str:
    """判断能否改本地存档的 shardindex（会跑 tasklist/netstat，须在后台线程调用）。

    本地存档只能由游戏客户端拉起，所以客户端开着且有开服进程占用该存档配置的端口时视为运行中；
    同端口的其他专服会被误判为运行中，提示文案按"可能"措辞。"""
    from dstools.features.local_service.dedicated_server import detect_external_shard_processes
    from dstools.features.mod.legacy_v1 import is_dst_client_running

    if not is_dst_client_running():
        return LOCAL_SAVE_CLOSED
    shards = detect_external_shard_processes(cluster) if cluster is not None else {}
    return LOCAL_SAVE_RUNNING if any(info["running"] for info in shards.values()) else LOCAL_SAVE_OPEN


def decode_persistent_bytes(raw: bytes) -> tuple[str, bool]:
    """解出 Klei 持久化文件的文本，返回 (文本, 是否为压缩编码)。"""
    if not raw.startswith(b"KLEI") or len(raw) < _HEADER_LEN:
        return raw.decode("utf-8"), False
    header, body = raw[:_HEADER_LEN], raw[_HEADER_LEN:]
    if header[-1:] != _ENCODED_FLAG:
        return body.decode("utf-8"), False
    try:
        decoded = base64.b64decode(body)
        # 16 字节头为 4 个小端 uint32：(1, 16, 解压后长度, 压缩后长度)
        _, _, plain_size, _ = struct.unpack("<4I", decoded[:16])
        text = zlib.decompress(decoded[16:])
    except (ValueError, struct.error, zlib.error) as exc:
        raise ShardIndexError(f"无法解码 Klei 压缩文件：{exc}") from exc
    if len(text) != plain_size:
        raise ShardIndexError("Klei 压缩文件长度校验不一致")
    return text.decode("utf-8"), True


def load_shardindex_mods(path: Path) -> dict[str, ModEntry]:
    """读取 enabled_mods，返回与 load_mod_overrides() 相同形状的条目；文件缺失或解析失败抛 ShardIndexError。"""
    try:
        text, _ = decode_persistent_bytes(Path(path).read_bytes())
        data = parse_lua_table(text, str(path))
    except ShardIndexError:
        raise
    except Exception as exc:
        raise ShardIndexError(f"无法读取 {path}：{exc}") from exc
    mods = {}
    for key, mod_data in (data.get(_KEY) or {}).items():
        # 禁用客户端 Mod 的开关不是 Mod；游戏开服时按启用的 Mod 重建 enabled_mods，本地存档留不住它
        if key == CLIENT_MODS_DISABLED_KEY or not isinstance(mod_data, dict):
            continue
        # config_data 是游戏的旧格式（见 ShardIndex:LoadEnabledServerMods）
        options = mod_data.get("configuration_options") or mod_data.get("config_data") or {}
        mods[key] = ModEntry(workshop_id=key, enabled=bool(mod_data.get("enabled", False)),
                             configuration_options=dict(options))
    return mods


def save_shardindex_mods(path: Path, mods: dict[str, ModEntry]) -> None:
    """只替换 enabled_mods 一段，文件其余内容逐字节保留；写前重新解析校验，原子替换。"""
    path = Path(path)
    raw = path.read_bytes()
    if not raw.startswith(_PLAIN_HEADER):
        raise ShardIndexError("shardindex 不是明文格式，不能安全写回")
    text = raw[_HEADER_LEN:].decode("utf-8")
    value = serialize_lua_table({
        key: {"configuration_options": entry.configuration_options, "enabled": entry.enabled}
        for key, entry in mods.items()
    }).removeprefix("return ")
    span = _find_top_level_value(text, _KEY)
    if span is not None:
        new_text = text[:span[0]] + value + text[span[1]:]
    else:
        brace = _top_level_open_brace(text)
        new_text = text[:brace + 1] + f"\n  {_KEY}={value}," + text[brace + 1:]

    before = parse_lua_table(text, str(path))
    after = parse_lua_table(new_text, str(path))
    before.pop(_KEY, None)
    after_mods = after.pop(_KEY, None)
    if before != after or len(after_mods or {}) != len(mods):
        raise ShardIndexError("写回校验失败：enabled_mods 以外的内容发生了变化")

    tmp = path.with_name(path.name + ".dstcamp.tmp")
    tmp.write_bytes(_PLAIN_HEADER + new_text.encode("utf-8"))
    os.replace(tmp, path)


# ── 定位 Lua 文本中的顶层键 ─────────────────────────────────────────────

def _skip_string(text: str, i: int) -> int:
    """i 指向引号或长括号起点，返回字符串结束后的位置。"""
    if text[i] in "\"'":
        quote, i = text[i], i + 1
        while i < len(text) and text[i] != quote:
            i += 2 if text[i] == "\\" else 1
        return i + 1
    level_end = text.index("[", i + 1)
    close = "]" + "=" * (level_end - i - 1) + "]"
    end = text.find(close, level_end + 1)
    if end < 0:
        raise ShardIndexError("Lua 长字符串未闭合")
    return end + len(close)


def _is_long_bracket(text: str, i: int) -> bool:
    j = i + 1
    while j < len(text) and text[j] == "=":
        j += 1
    return text[i] == "[" and j < len(text) and text[j] == "["


def _skip_trivia(text: str, i: int) -> int:
    """跳过空白与注释。"""
    while i < len(text):
        if text[i].isspace():
            i += 1
        elif text.startswith("--", i):
            if _is_long_bracket(text, i + 2):
                i = _skip_string(text, i + 2)
            else:
                end = text.find("\n", i)
                i = len(text) if end < 0 else end + 1
        else:
            break
    return i


def _matching_brace(text: str, i: int) -> int:
    """i 指向 "{"，返回与之匹配的 "}" 之后的位置。"""
    depth = 0
    while i < len(text):
        i = _skip_trivia(text, i)
        if i >= len(text):
            break
        ch = text[i]
        if ch in "\"'" or _is_long_bracket(text, i):
            i = _skip_string(text, i)
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ShardIndexError("Lua 表括号不匹配")


def _top_level_open_brace(text: str) -> int:
    i = _skip_trivia(text, 0)
    if not text.startswith("return", i):
        raise ShardIndexError("shardindex 不是 return {...} 格式")
    i = _skip_trivia(text, i + len("return"))
    if i >= len(text) or text[i] != "{":
        raise ShardIndexError("shardindex 不是 return {...} 格式")
    return i


def _find_top_level_value(text: str, key: str) -> tuple[int, int] | None:
    """返回顶层表中 key 对应的表值在 text 中的 [起, 止) 位置；键不存在返回 None。"""
    i = _top_level_open_brace(text) + 1
    end = _matching_brace(text, i - 1) - 1
    while True:
        i = _skip_trivia(text, i)
        if i >= end:
            return None
        # 读一个字段的键：标识符或 ["字符串"]
        name = None
        if text[i] == "[" and not _is_long_bracket(text, i):
            j = _skip_trivia(text, i + 1)
            if text[j] in "\"'":
                k = _skip_string(text, j)
                name = text[j + 1:k - 1]
                j = k
            close = text.index("]", j)
            i = close + 1
        elif text[i].isalpha() or text[i] == "_":
            j = i
            while j < len(text) and (text[j].isalnum() or text[j] == "_"):
                j += 1
            name, i = text[i:j], j
        i = _skip_trivia(text, i)
        if i < end and text[i] == "=":
            i = _skip_trivia(text, i + 1)
        # 跳过值，定位到下一个逗号/分号
        value_start = i
        while i < end and text[i] not in ",;":
            if text[i] == "{":
                i = _matching_brace(text, i)
            elif text[i] in "\"'" or _is_long_bracket(text, i):
                i = _skip_string(text, i)
            else:
                i += 1
        if name == key:
            value_end = i
            while value_end > value_start and text[value_end - 1].isspace():
                value_end -= 1
            if text[value_start:value_start + 1] != "{":
                raise ShardIndexError(f"{key} 不是表")
            return value_start, value_end
        i += 1
