"""从游戏模板或已核对的 Mod 源码加载完整世界默认值。"""

import copy
from dataclasses import replace

from pathlib import Path

from dstools.features.world.creation import WorldShardPlan, default_shard_config
from dstools.features.world.location_profiles import (
    THREE_WORLDS_SHARD_LAYOUT,
    get_location_definition,
    get_verified_creation_level_data,
)
from dstools.features.world.reader import LeveldataStatus, load_leveldata


def find_verified_template(klei_root: Path, location: str) -> Path:
    """Find a real two-shard template automatically; never ask the user for it."""
    candidates = []
    for master_file in klei_root.rglob("Master/leveldataoverride.lua"):
        cluster = master_file.parent.parent
        caves_file = cluster / "Caves" / "leveldataoverride.lua"
        if not caves_file.exists():
            continue
        try:
            master = shard_plan_from_template(master_file)
            caves = shard_plan_from_template(caves_file)
        except ValueError:
            continue
        if caves.location == "cave" and master.location == location:
            candidates.append(cluster)
    if not candidates:
        raise FileNotFoundError(f"未找到已验证的{location}默认世界模板")
    return sorted(candidates, key=lambda p: p.name)[0]


def default_plan_for_location(location: str) -> WorldShardPlan:
    """按已验证预设创建计划；Mod location 必须显式带齐默认生成参数。"""
    definition = get_location_definition(location)
    level_data = get_verified_creation_level_data(location)
    overrides = level_data.pop("overrides", {})
    return WorldShardPlan(
        location=location,
        preset_id=definition.default_preset_id,
        name=definition.name_zh,
        description=definition.description_zh,
        overrides=overrides,
        level_data=level_data,
    )


def three_worlds_layout() -> list[tuple[str, WorldShardPlan, object]]:
    """三合一整合版五分片：[(目录名, 世界计划, server.ini 配置)]，分片 ID 按 THREE_WORLDS_SHARD_LAYOUT 固定。"""
    result = []
    for shard_name, location, shard_id, extra_overrides in THREE_WORLDS_SHARD_LAYOUT:
        plan = default_plan_for_location(location)
        if extra_overrides:
            plan = replace(plan, overrides={**plan.overrides, **extra_overrides})
        # default_shard_config 的分片 ID = shard_index + 1，端口也按 shard_index 错开
        config = default_shard_config(shard_id is None, shard_name, (shard_id or 1) - 1)
        result.append((shard_name, plan, config))
    return result


def shard_plan_from_template(path: Path) -> WorldShardPlan:
    """把游戏真实生成的 leveldataoverride.lua 转成创建计划；模板缺失或损坏直接报错，不用手写的不完整默认值兜底。"""
    result = load_leveldata(path)
    if result.status != LeveldataStatus.OK or result.preset is None:
        raise ValueError(f"无法读取官方世界模板: {path}")
    preset = result.preset
    try:
        get_location_definition(preset.location)
    except ValueError:
        raise ValueError(f"模板世界类型无效: {preset.location}")
    raw = copy.deepcopy(preset.raw)
    overrides = raw.pop("overrides", {})
    for key in ("id", "name", "desc", "location"):
        raw.pop(key, None)
    return WorldShardPlan(
        location=preset.location,
        preset_id=preset.preset_id,
        name=preset.name,
        description=preset.description,
        overrides=overrides if isinstance(overrides, dict) else {},
        level_data=raw,
    )


def default_plans_from_cluster(cluster_root: Path) -> tuple[WorldShardPlan, WorldShardPlan]:
    """读取两个官方分片目录的世界计划；location 以文件内容为准，不按目录名推断。"""
    master = shard_plan_from_template(cluster_root / "Master" / "leveldataoverride.lua")
    caves = shard_plan_from_template(cluster_root / "Caves" / "leveldataoverride.lua")
    return master, caves
