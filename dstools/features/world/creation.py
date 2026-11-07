"""与界面无关的世界创建：校验主世界 location，在同级临时目录生成完整的多分片目录后原子移动到位。"""

import copy
from dataclasses import dataclass, field
import os
from pathlib import Path
import secrets
import shutil

from dstools.features.world.location_profiles import (
    get_verified_creation_level_data,
    get_location_definition,
    location_requirements_met,
    normalize_mod_ids,
    resolve_world_location_profile,
    with_required_dependencies,
)
from dstools.features.world.mod_settings import creation_defaults
from dstools.shared.ini_parser import write_cluster_ini, write_server_ini
from dstools.models import ClusterConfig, ShardConfig
from dstools.shared.lua_parser import serialize_lua_table


@dataclass(frozen=True)
class WorldShardPlan:
    location: str
    preset_id: str
    name: str
    description: str = ""
    overrides: dict[str, object] = field(default_factory=dict)
    # leveldataoverride.lua 中除身份字段和 overrides 外的完整 Level 元数据
    # （version、background_node_range、required_prefabs 等，岛屿冒险世界生成依赖它们）
    level_data: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class WorldCreationPlan:
    cluster_name: str
    # 两者都可以为 None：创建向导允许删掉主世界或洞穴（至少保留一个世界）。
    # 哪个分片是主世界以 shard_configs 里 [SHARD] is_master 为准，见 shard_is_master()。
    master: WorldShardPlan | None
    caves: WorldShardPlan | None
    cluster_ini: ClusterConfig = field(default_factory=ClusterConfig)
    mod_ids: frozenset[str] = frozenset()
    # workshop id -> modoverrides.lua 条目，与 ID 集合分开，保留每个 Mod 的配置项而不依赖现有存档
    mod_overrides: dict[str, dict] = field(default_factory=dict)
    shard_configs: dict[str, ShardConfig] = field(default_factory=dict)
    cluster_token: str = ""
    admin_ids: tuple[str, ...] = ()
    block_ids: tuple[str, ...] = ()
    # 额外世界目录名 -> 世界计划。Master/Caves 保持独立字段以兼容现有调用方；
    # 新增的多层世界统一放在这里，目录名同时也是 server.ini 的分片名称。
    extra_shards: dict[str, WorldShardPlan] = field(default_factory=dict)


def _selected_mod_ids(plan: WorldCreationPlan) -> frozenset[str]:
    normalized = set(normalize_mod_ids(plan.mod_ids))
    for value, entry in plan.mod_overrides.items():
        mod_id = str(value).removeprefix("workshop-")
        enabled = not isinstance(entry, dict) or bool(entry.get("enabled", True))
        if enabled:
            normalized.add(mod_id)
        else:
            normalized.discard(mod_id)
    return frozenset(normalized)


def resolve_creation_mods(
    plan: WorldCreationPlan,
) -> tuple[frozenset[str], dict[str, dict]]:
    """补齐硬依赖并返回两个分片应写入的统一 Mod 配置。"""
    selected = _selected_mod_ids(plan)
    effective = with_required_dependencies(selected)
    overrides = copy_mod_overrides(plan.mod_overrides)
    for mod_id in effective:
        overrides.setdefault(mod_id, {"enabled": True})
        overrides[mod_id]["enabled"] = True
    return effective, overrides


def copy_mod_overrides(source: dict[str, dict]) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for value, entry in source.items():
        key = str(value).removeprefix("workshop-")
        result[key] = dict(entry) if isinstance(entry, dict) else {}
    return result


def creation_shards(plan: WorldCreationPlan) -> list[tuple[str, WorldShardPlan]]:
    """按写盘顺序返回实际要创建的分片（目录名, 世界计划），跳过被删掉的 Master/Caves。"""
    fixed = [(name, shard) for name, shard in (("Master", plan.master), ("Caves", plan.caves)) if shard is not None]
    return [*fixed, *plan.extra_shards.items()]


def shard_is_master(plan: WorldCreationPlan, shard_name: str) -> bool:
    """分片是否为主世界：以 shard_configs 中 [SHARD] is_master 为准，未提供时只有 Master 目录是主世界。"""
    config = plan.shard_configs.get(shard_name)
    if config is not None and "is_master" in config.shard:
        return bool(config.shard["is_master"])
    return shard_name == "Master"


def validate_creation_plan(plan: WorldCreationPlan) -> None:
    if not plan.cluster_name or any(ch in plan.cluster_name for ch in '\\/:*?"<>|'):
        raise ValueError("非法存档名称")

    selected = _selected_mod_ids(plan)
    effective = with_required_dependencies(selected)
    profile = resolve_world_location_profile(effective)
    if profile.warnings:
        raise ValueError(profile.warnings[0])

    all_locations = set(profile.master_locations) | set(profile.caves_locations)
    shards = creation_shards(plan)
    if not shards:
        raise ValueError("至少需要保留一个世界")
    if sum(1 for name, _shard in shards if shard_is_master(plan, name)) != 1:
        raise ValueError("必须有且只有一个主世界")
    seen_names: set[str] = set()
    for shard_name, shard_plan in shards:
        if (not shard_name or shard_name in seen_names
                or any(ch in shard_name for ch in '\\/:*?"<>|')):
            raise ValueError(f"非法世界分片名称: {shard_name}")
        seen_names.add(shard_name)
        get_location_definition(shard_plan.location)
        available = (
            profile.available_locations(shard_name)
            if shard_name in ("Master", "Caves") else all_locations
        )
        if shard_plan.location not in available:
            raise ValueError(
                f"{shard_name} 当前不能使用 {shard_plan.location} 世界"
            )
        if not location_requirements_met(shard_plan.location, effective):
            raise ValueError(f"{shard_plan.location} 世界缺少所需 Mod")
        if not shard_plan.preset_id:
            raise ValueError(f"{shard_name} 世界预设不能为空")
        # 游戏把 leveldataoverride.lua 当完整关卡定义整体替换（shardindex.lua SetServerShardData），
        # 缺 task_set 开服就报 "Must specify the task set for a level!"，世界生成不出来
        if "task_set" not in shard_level_data(shard_plan)["overrides"]:
            raise ValueError(
                f"{shard_name} 缺少完整的世界生成数据（task_set），"
                "请先用游戏创建一个带洞穴的世界，作为原版森林/洞穴的模板"
            )


def _write_lua(path: Path, data: dict) -> None:
    path.write_text(serialize_lua_table(data) + "\n", encoding="utf-8")


def default_cluster_config(cluster_name: str = "Cluster_New") -> ClusterConfig:
    """新建专服的 cluster.ini 默认值（已核对）。仅限局域网/离线模式默认关闭，与游戏新建专服一致。"""
    return ClusterConfig(
        gameplay={
            "game_mode": "survival",
            "max_players": 6,
            "pvp": False,
            "pause_when_empty": True,
        },
        network={
            "lan_only_cluster": False,
            "cluster_password": "",
            "cluster_description": "",
            "cluster_name": cluster_name,
            "offline_cluster": False,
            "cluster_language": "zh",
        },
        misc={"console_enabled": True},
        shard={
            "shard_enabled": True,
            "bind_ip": "127.0.0.1",
            "master_ip": "127.0.0.1",
            "master_port": 10888,
            "cluster_key": "defaultPass",
        },
    )


def default_shard_config(
    is_master: bool, shard_name: str = "Caves", shard_index: int = 1,
) -> ShardConfig:
    """Return the verified fresh shard ``server.ini`` defaults."""
    server_port = 10999 if is_master else (10998 if shard_index == 1 else 10998 + shard_index)
    return ShardConfig(
        network={"server_port": server_port},
        shard={"is_master": is_master} if is_master else {
            "is_master": False,
            "name": shard_name,
            # Klei 的从世界必须有集群内唯一编号；真实 Caves 配置和现有
            # 配置编辑器的补全逻辑都从 2 开始分配。
            "id": shard_index + 1,
        },
        account={"encode_user_path": True},
        steam={} if is_master else {
            "master_server_port": 27016 + shard_index,
            "authentication_port": 8766 + shard_index,
        },
    )


def _write_default_server_ini(root: Path) -> None:
    """写入让新分片可被发现的最小 server.ini（其余运行时字段由游戏首次启动补齐），取值与真实新建存档一致。"""
    write_server_ini(
        default_shard_config(root.name.casefold() == "master", root.name),
        root / "server.ini",
    )


def shard_level_data(shard: WorldShardPlan, mod_ids=()) -> dict[str, object]:
    """返回该世界最终写入 leveldataoverride.lua 的完整关卡数据。

    ``mod_ids`` 为启用的 Mod：补上缺项时实际行为与游戏默认值不同的 Mod 设置（如猪镇的 DST 船），
    优先级最低，地点默认值和计划里的取值都会覆盖它。"""
    # 最后一层仍合并已核对的 location 默认值，避免旧草稿、调用方手工构造
    # WorldShardPlan 或未来 UI 回归再次写出 overrides={} 的海难/火山。
    raw = get_verified_creation_level_data(shard.location)
    location_overrides = raw.pop("overrides", {})
    raw.update(copy.deepcopy(shard.level_data))
    overrides = creation_defaults(mod_ids, shard.location) if mod_ids else {}
    overrides.update(copy.deepcopy(location_overrides))
    overrides.update(copy.deepcopy(shard.overrides))
    raw.update({
        "id": shard.preset_id,
        "name": shard.name,
        "desc": shard.description,
        "location": shard.location,
        "overrides": overrides,
    })
    return raw


def _write_shard(
    root: Path,
    shard: WorldShardPlan,
    mod_ids: frozenset[str],
    mod_overrides: dict[str, dict],
    shard_config: ShardConfig | None = None,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    if shard_config is None:
        _write_default_server_ini(root)
    else:
        write_server_ini(shard_config, root / "server.ini")
    _write_lua(root / "leveldataoverride.lua", shard_level_data(shard, mod_ids))
    def _mod_key(value) -> str:
        text = str(value)
        if text.startswith("workshop-") or not text.isdigit():
            return text
        # Keep the historical convenience for callers that pass a bare
        # numeric Workshop id, while preserving non-numeric local mod names.
        return f"workshop-{text}"

    overrides = {_mod_key(value): {"enabled": True} for value in mod_ids}
    for value, entry in mod_overrides.items():
        key = _mod_key(value)
        data = dict(entry) if isinstance(entry, dict) else {}
        data.setdefault("enabled", True)
        overrides[key] = data
    _write_lua(root / "modoverrides.lua", overrides)


def create_world(plan: WorldCreationPlan, destination_root: Path) -> Path:
    """Create a new cluster without overwriting an existing directory."""
    validate_creation_plan(plan)
    effective_mod_ids, effective_mod_overrides = resolve_creation_mods(plan)
    destination = destination_root / plan.cluster_name
    if destination.exists():
        raise FileExistsError(destination)
    destination_root.mkdir(parents=True, exist_ok=True)
    # 坑：tempfile.mkdtemp() 在新版 Python/Windows 下会设置仅当前用户可访问的 ACL，管理员运行时
    # os.replace() 后会原样留给正式存档导致普通用户打不开；改用普通 mkdir 继承 Klei 根目录权限
    while True:
        temp_dir = destination_root / f".{plan.cluster_name}.{secrets.token_hex(8)}"
        try:
            temp_dir.mkdir()
            break
        except FileExistsError:
            continue
    try:
        cluster_ini = plan.cluster_ini
        if not any((cluster_ini.gameplay, cluster_ini.network, cluster_ini.misc,
                    cluster_ini.shard, cluster_ini.steam)):
            cluster_ini = default_cluster_config(plan.cluster_name)
        write_cluster_ini(cluster_ini, temp_dir / "cluster.ini")
        (temp_dir / "cluster_token.txt").write_text(plan.cluster_token or "", encoding="utf-8")
        (temp_dir / "adminlist.txt").write_text(
            "\n".join(plan.admin_ids) + ("\n" if plan.admin_ids else ""), encoding="utf-8"
        )
        (temp_dir / "blocklist.txt").write_text(
            "\n".join(plan.block_ids) + ("\n" if plan.block_ids else ""), encoding="utf-8"
        )
        if plan.master is not None:
            _write_shard(
                temp_dir / "Master", plan.master, effective_mod_ids, effective_mod_overrides,
                plan.shard_configs.get("Master"),
            )
        if plan.caves is not None:
            _write_shard(
                temp_dir / "Caves", plan.caves, effective_mod_ids, effective_mod_overrides,
                plan.shard_configs.get("Caves"),
            )
        for shard_index, (shard_name, shard_plan) in enumerate(plan.extra_shards.items(), start=2):
            _write_shard(
                temp_dir / shard_name,
                shard_plan,
                effective_mod_ids,
                effective_mod_overrides,
                plan.shard_configs.get(shard_name)
                or default_shard_config(False, shard_name, shard_index),
            )
        os.replace(temp_dir, destination)
        return destination
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
