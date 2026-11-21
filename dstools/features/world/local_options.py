"""本地存档（游戏客户端"创建游戏"）的世界设置：读写各分片 save/shardindex 的 world.options.overrides。

游戏前端选中存档时，世界设置页用 ShardSaveGameIndex:GetSlotGenOptions() 读各分片 shardindex 的
world.options，开服时再由界面收集选项重写 leveldataoverride.lua，所以本地存档只改 leveldataoverride.lua
会被覆盖。游戏运行时 shardindex 缓存在内存里、退出前写回，只能在游戏关闭时修改。

shardindex 的格式与定位工具复用 features/mod/shardindex.py（同一文件的 enabled_mods 段由它负责）。
"""

import os
from pathlib import Path

from dstools.features.mod.shardindex import (
    ShardIndexError,
    _HEADER_LEN,
    _PLAIN_HEADER,
    _find_top_level_value,
    decode_persistent_bytes,
)
from dstools.shared.lua_parser import parse_lua_table, serialize_lua_table

_RETURN = "return "
_PATH = ("world", "options", "overrides")


def shard_index_path(shard_dir: Path) -> Path:
    return Path(shard_dir) / "save" / "shardindex"


def _nested_span(text: str, keys: tuple[str, ...]) -> tuple[int, int] | None:
    """返回 return {...} 文本中按 keys 逐层嵌套的表值 [起, 止) 位置；任一层缺失返回 None。"""
    start, end = 0, len(text)
    body = text
    for key in keys:
        span = _find_top_level_value(body, key)
        if span is None:
            return None
        # 把下一层的表当成独立的 return {...} 再定位，偏移量换算回原文
        offset = start - (len(_RETURN) if body is not text else 0)
        start, end = span[0] + offset, span[1] + offset
        body = _RETURN + text[start:end]
    return start, end


def load_local_overrides(shard_dir: Path) -> dict:
    """读取分片 shardindex 的 world.options.overrides；文件缺失、损坏抛 ShardIndexError。"""
    path = shard_index_path(shard_dir)
    try:
        text, _ = decode_persistent_bytes(path.read_bytes())
        data = parse_lua_table(text, str(path))
    except ShardIndexError:
        raise
    except Exception as exc:
        raise ShardIndexError(f"无法读取 {path}：{exc}") from exc
    overrides = ((data.get("world") or {}).get("options") or {}).get("overrides")
    if not isinstance(overrides, dict):
        raise ShardIndexError(f"{path} 中没有世界设置（world.options.overrides）")
    return overrides


def save_local_overrides(shard_dir: Path, overrides: dict, edit_options=None) -> None:
    """只替换 world.options.overrides 一段，文件其余内容逐字节保留；写前重新解析校验，原子替换。

    ``edit_options`` 为可选回调，就地修改 world.options 的其他字段（如套用内置选项预设时的 settings_id）；
    传入时改为替换整个 world.options 表，world.options 以外的内容仍逐字节保留。"""
    path = shard_index_path(shard_dir)
    raw = path.read_bytes()
    if not raw.startswith(_PLAIN_HEADER):
        raise ShardIndexError("shardindex 不是明文格式，不能安全写回")
    text = raw[_HEADER_LEN:].decode("utf-8")
    before = parse_lua_table(text, str(path))
    if edit_options is None:
        target_path, expected = _PATH, overrides
    else:
        target_path = _PATH[:-1]
        expected = dict(before["world"]["options"])
        expected["overrides"] = overrides
        edit_options(expected)
    span = _nested_span(text, target_path)
    if span is None:
        raise ShardIndexError("shardindex 中没有世界设置（world.options.overrides）")
    value = serialize_lua_table(expected).removeprefix(_RETURN) if expected else "{}"
    new_text = text[:span[0]] + value + text[span[1]:]

    after = parse_lua_table(new_text, str(path))
    if edit_options is None:
        written = after["world"]["options"].pop("overrides", None)
        before["world"]["options"].pop("overrides", None)
    else:
        written = after["world"].pop("options", None)
        before["world"].pop("options", None)
    if before != after or written != expected:
        raise ShardIndexError("写回校验失败：世界设置以外的内容发生了变化")

    tmp = path.with_name(path.name + ".dstcamp.tmp")
    tmp.write_bytes(_PLAIN_HEADER + new_text.encode("utf-8"))
    os.replace(tmp, path)
