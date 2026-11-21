"""leveldataoverride.lua 的读写（只做 Lua I/O，名称、分类、图标见 categories.py / icons.py）。"""

import os
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from dstools.shared.lua_parser import parse_lua_file


# ── 世界数据模型 ────────────────────────────────────────────────────────

@dataclass
class WorldOverride:
    """单条世界生成 override 条目。"""
    key: str
    value: str
    name: str = ""        # 中文名
    description: str = ""  # 描述
    is_rule: bool = False  # True 表示世界规则（可编辑），False 表示世界生成（只读）
    icon: str = ""       # Unicode 图标
    category: str = ""     # 分类 key


@dataclass
class WorldPreset:
    """存档等级 override（世界预设）信息。"""
    preset_id: str = ""     # 例如 "ENDLESS"、"SURVIVAL_TOGETHER"
    name: str = ""          # 例如 "无尽"
    description: str = ""
    location: str = ""      # "forest" / "cave"
    overrides: list[WorldOverride] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


class LeveldataStatus(str, Enum):
    """读取 leveldataoverride.lua 的结果状态。"""

    OK = "ok"
    MISSING = "missing"
    INVALID = "invalid"


@dataclass
class LeveldataLoadResult:
    """保留读取失败原因，供界面和未来的创建流程分别处理。"""

    status: LeveldataStatus
    preset: WorldPreset | None = None
    error: Exception | None = None


def _display_value(value) -> str:
    """界面统一按字符串处理取值；布尔/数字（如 has_ocean=true）保存时据此判断是否被改过。"""
    return value if isinstance(value, str) else str(value)


def load_leveldata(path: Path) -> LeveldataLoadResult:
    """读取一个 leveldataoverride.lua，并区分缺失与格式错误。"""
    if not path.exists():
        return LeveldataLoadResult(LeveldataStatus.MISSING)

    try:
        raw = parse_lua_file(path)
        if not isinstance(raw, dict):
            raise ValueError("leveldataoverride.lua 的根节点必须是 Lua table")
    except Exception as exc:
        return LeveldataLoadResult(LeveldataStatus.INVALID, error=exc)

    preset = WorldPreset(
        preset_id=raw.get("id", ""),
        name=raw.get("name", ""),
        description=raw.get("desc", ""),
        location=raw.get("location", ""),
        raw=raw,
    )
    overrides_raw = raw.get("overrides", {})
    if isinstance(overrides_raw, dict):
        for key, value in overrides_raw.items():
            preset.overrides.append(WorldOverride(key=key, value=_display_value(value)))
    return LeveldataLoadResult(LeveldataStatus.OK, preset=preset)




def _write_text_atomically(path: Path, text: str) -> None:
    """同一目录内临时写入后替换，避免中途失败留下截断的 Lua 文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as temp:
            temp_name = temp.name
            temp.write(text)
            temp.flush()
            os.fsync(temp.fileno())
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def save_leveldata(preset: WorldPreset, path: Path, value_sets: dict[str, list[str]] | None = None,
                   edit_raw=None) -> None:
    """把 overrides 写回 leveldataoverride.lua，其他字段保持不变。

    ``value_sets`` 为 key -> 取值表，取值表全是数字的 key（如热带冒险 world_size_multi）按 Lua 数字写入。
    ``edit_raw`` 为可选回调，写入前就地修改整张表的其他字段（如套用内置选项预设时的 settings_id）。"""
    from dstools.shared.lua_parser import parse_lua_file
    from dstools.shared.lua_parser import serialize_lua_table

    # 读取原文件以保留结构
    if path.exists():
        raw = parse_lua_file(path)
    else:
        raw = {}

    # 用修改后的值更新 overrides
    if "overrides" not in raw:
        raw["overrides"] = {}
    elif not isinstance(raw["overrides"], dict):
        raise ValueError("leveldataoverride.lua 的 overrides 必须是 Lua table")

    raw["overrides"] = merge_overrides(raw["overrides"], preset.overrides, value_sets)
    if edit_raw is not None:
        edit_raw(raw)
    text = serialize_lua_table(raw)
    _write_text_atomically(path, text)


def merge_overrides(existing: dict, overrides: list[WorldOverride],
                    value_sets: dict[str, list[str]] | None = None) -> dict:
    """把界面上的取值合并进已有 overrides 表并返回新表（leveldataoverride 与本地存档 shardindex 共用）。"""
    from dstools.features.world.value_sets import typed_override_value

    merged = dict(existing)
    for ov in overrides:
        original = merged.get(ov.key)
        # 未改动的非字符串原值原样写回，否则 true/1.5 会被写成 "True"/"1.5"，Mod 的 == true 判断随之失效
        if not isinstance(original, str) and original is not None and _display_value(original) == ov.value:
            continue
        merged[ov.key] = typed_override_value(ov.value, (value_sets or {}).get(ov.key, []))
    return merged
