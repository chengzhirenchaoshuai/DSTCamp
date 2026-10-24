"""管理已有专服存档的世界分片（新增等），调用方负责确认存档已停止。

新增世界只新建分片目录、按需打开多世界设置，不碰已有世界的存档；新分片在下次开服时由游戏生成，
Mod 配置复制主世界的 modoverrides.lua，各世界保持一致。万界之门的 worlds 配置（编号与名称）
在所有世界里写同一份，多文件写入失败时整体恢复原样。
"""

from contextlib import contextmanager
import copy
from dataclasses import dataclass, replace
import os
from pathlib import Path
import re
import secrets
import shutil

from dstools.features.cluster_config.config_manager import CLUSTER_INI_DEFAULTS
from dstools.features.mod.parser import find_workshop_dir, is_mod_subscribed
from dstools.features.mod.sync import get_enabled_mod_ids
from dstools.features.mod.workshop_acf import read_acf_installed_sizes
from dstools.features.world.creation import shard_level_data, WorldShardPlan
from dstools.features.world.defaults import (
    default_plan_for_location,
    default_plans_from_cluster,
    find_verified_template,
    shard_plan_from_template,
)
from dstools.features.world.location_profiles import (
    CAVE_LOCATION,
    FOREST_LOCATION,
    get_location_definition,
    IA_CORE_MOD_ID,
    location_capability_ids,
    location_requirements_met,
    resolve_world_location_profile,
    with_required_dependencies,
)
from dstools.i18n import t
from dstools.models import Cluster, Platform, SaveSource, ShardConfig
from dstools.shared.ini_parser import parse_cluster_ini, parse_server_ini, write_cluster_ini, write_server_ini
from dstools.shared.lua_parser import LuaStringKey, parse_lua_file, serialize_lua_table
from dstools.shared.recycle_bin import move_to_recycle_bin
from dstools.shared.server_ports import (
    DEFAULT_MASTER_PORT,
    DEFAULT_STEAM_AUTH_PORT,
    DEFAULT_STEAM_MASTER_PORT,
    LAN_SERVER_PORT_FALLBACK,
    LAN_SERVER_PORT_MAX,
    LAN_SERVER_PORT_MIN,
    collect_cluster_port_claims,
    next_free_port,
    scan_udp_ports,
)

WORLDGATE_MOD_ID = "3822260794"  # 万界之门(WorldGate) 创意工坊 ID
# 后者是本机开发时放在游戏 mods 目录下的目录名
_WORLDGATE_KEYS = (f"workshop-{WORLDGATE_MOD_ID}", "WorldGate")

_SHARD_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")

# 与创建向导的额外世界命名一致
_SHARD_NAME_BASES = {"forest": "Forest", "cave": "Caves", "porkland": "Porkland",
                     "shipwrecked": "Shipwrecked", "volcanoworld": "Volcano"}

# 打开多世界时 cluster.ini [SHARD] 必须有的字段，缺失时补官方默认值（master_port 另行避开已占用端口）
_SHARD_REQUIRED_KEYS = ("bind_ip", "master_ip", "cluster_key")


def _master_shard(cluster: Cluster):
    """主世界分片：server.ini [SHARD] is_master 为真的那个，没写 is_master 时取 Master 目录。"""
    for shard in cluster.shards:
        if (shard.path / "server.ini").is_file() and parse_server_ini(shard.path / "server.ini").shard.get("is_master"):
            return shard
    return next((shard for shard in cluster.shards if shard.name == "Master"), None)


def worldgate_installed() -> bool:
    """本机的工坊目录里装好了万界之门：要有 modinfo.lua，还要登记在 appworkshop_322330.acf 里。

    实测专服带 -ugc_directory 时，文件夹在但清单没登记的 Mod 不会被加载（日志 "No mods registered"）。"""
    if not is_mod_subscribed(WORLDGATE_MOD_ID):
        return False
    return int(WORLDGATE_MOD_ID) in read_acf_installed_sizes(find_workshop_dir())


def available_locations(cluster: Cluster) -> tuple[str, ...]:
    """按存档已启用的 Mod 列出可以新增的世界类型（与创建向导"添加新的世界"一致）。"""
    profile = resolve_world_location_profile(get_enabled_mod_ids(cluster))
    return tuple(dict.fromkeys(profile.master_locations + profile.caves_locations))


def next_shard_name(cluster: Cluster, location: str) -> str:
    """按世界类型取不重复的目录名：Forest、Forest_2……（不区分大小写）。"""
    base = _SHARD_NAME_BASES.get(location, "World")
    occupied = {path.name.casefold() for path in cluster.path.iterdir()}
    if base.casefold() not in occupied:
        return base
    index = 2
    while f"{base}_{index}".casefold() in occupied:
        index += 1
    return f"{base}_{index}"


def initial_plan(cluster: Cluster, location: str, klei_root: Path | None) -> WorldShardPlan:
    """新世界的初始关卡数据：优先复制本存档同类世界（完整定义，设置也与旧世界一致），
    其次取游戏生成的其他存档模板，Mod 世界用已核对的默认值。"""
    for shard in cluster.shards:
        if shard.leveldata_path is None or not shard.leveldata_path.is_file():
            continue
        try:
            plan = shard_plan_from_template(shard.leveldata_path)
        except ValueError:
            continue
        if plan.location == location:
            return plan
    if klei_root is not None and location in (FOREST_LOCATION, CAVE_LOCATION):
        try:
            master, caves = default_plans_from_cluster(find_verified_template(klei_root, FOREST_LOCATION))
            return master if location == FOREST_LOCATION else caves
        except (FileNotFoundError, ValueError):
            pass
    return default_plan_for_location(location)


def used_ports_elsewhere(clusters, cluster: Cluster) -> set[int]:
    """其他 Steam 专服存档配置的端口加上系统当前占用的 UDP 端口（netstat，较慢，放后台调用）。"""
    used: set[int] = set()
    for other in clusters:
        if other.path == cluster.path or other.source != SaveSource.SERVER or other.platform != Platform.STEAM:
            continue
        claims, _issues = collect_cluster_port_claims(other)
        used.update(claim.port for claim in claims)
    scan = scan_udp_ports()
    if scan.ok:
        used.update(port for ports in scan.ports_by_pid.values() for port in ports)
    return used


def next_shard_id(cluster: Cluster) -> int:
    """新分片的 [SHARD] id：取现有编号最大值加一（主世界固定为 1）。"""
    used = {1}
    for shard in cluster.shards:
        path = shard.path / "server.ini"
        if not path.is_file():
            continue
        try:
            used.add(int(str(parse_server_ini(path).shard.get("id", "")).strip()))
        except ValueError:
            continue
    return max(used) + 1


def shard_name_error(cluster: Cluster, shard_name: str, current: str = "") -> str:
    """世界文件夹名（也是 server.ini 的分片名）的问题，没问题返回空字符串。

    ``current`` 为正在改名的世界，查重时跳过它自己（允许只改大小写）。"""
    # 只放行 ASCII：分片名会出现在启动参数、日志和端口映射里，非 ASCII 是否处处可用没有核实过
    if not _SHARD_NAME_RE.fullmatch(shard_name):
        return t("world.add_shard_folder_invalid")
    folded, skip = shard_name.casefold(), current.casefold()
    # Windows 目录名不区分大小写；server.ini 里的分片名同样要唯一
    if any(path.name.casefold() == folded != skip for path in cluster.path.iterdir()):
        return t("world.add_shard_folder_exists", name=shard_name)
    for shard in cluster.shards:
        path = shard.path / "server.ini"
        if shard.name.casefold() == skip or not path.is_file():
            continue
        if str(parse_server_ini(path).shard.get("name", "")).casefold() == folded:
            return t("world.add_shard_folder_exists", name=shard_name)
    return ""


def planned_world_names(cluster: Cluster, location: str, language: str) -> tuple[str, set[str]]:
    """新世界在万界之门面板上的默认名称，以及新增后其他世界的名称（用于查重）。"""
    master = _master_shard(cluster)
    new_id = next_shard_id(cluster)
    names = _world_names(cluster, master, _existing_worlds(cluster, master), (new_id, location, None), language)
    default = names.pop(str(new_id), {}).get("name", "")
    return default, {entry["name"] for entry in names.values() if entry.get("name")}


def world_name_error(cluster: Cluster, location: str, world_name: str, language: str) -> str:
    """新世界显示名称的问题，没问题返回空字符串：不能为空，也不能和其他世界重名（玩家靠它分辨世界）。"""
    if not world_name.strip():
        return t("world.add_shard_name_empty")
    if world_name.strip() in planned_world_names(cluster, location, language)[1]:
        return t("world.add_shard_name_exists", name=world_name.strip())
    return ""


def validate_new_shard(cluster: Cluster, shard_name: str, plan: WorldShardPlan) -> None:
    """检查能否给该存档加这个世界，不能时抛出带中文原因的 ValueError。"""
    if cluster.source != SaveSource.SERVER:
        raise ValueError("只有专服存档可以新增世界，本地存档请在游戏内创建")
    if not (cluster.path / "cluster.ini").is_file():
        raise ValueError("存档里没有 cluster.ini")
    if _master_shard(cluster) is None:
        raise ValueError("存档里找不到主世界")
    error = shard_name_error(cluster, shard_name)
    if error:
        raise ValueError(error)
    get_location_definition(plan.location)
    effective = with_required_dependencies(get_enabled_mod_ids(cluster))
    if not location_requirements_met(plan.location, effective):
        raise ValueError(f"{plan.location} 世界缺少所需 Mod，请先在 Mod 页启用")
    if not plan.preset_id:
        raise ValueError("世界预设不能为空")
    # 游戏把 leveldataoverride.lua 当完整关卡定义整体替换，缺 task_set 世界生成不出来
    if "task_set" not in shard_level_data(plan)["overrides"]:
        raise ValueError("缺少完整的世界生成数据（task_set），请先用游戏创建一个带洞穴的世界作为模板")


def plan_shard_config(cluster: Cluster, shard_name: str, used_ports) -> ShardConfig:
    """新分片的 server.ini：编号接在已有分片之后，三个端口避开 used_ports 和本存档已占用的端口。"""
    claims, _issues = collect_cluster_port_claims(cluster)
    occupied = set(used_ports) | {claim.port for claim in claims}
    network = parse_cluster_ini(cluster.path / "cluster.ini").network
    if network.get("lan_only_cluster") or network.get("offline_cluster"):
        # 离线/仅局域网时游戏端口必须在 10998..11018，否则游戏会退回 10999
        candidates = [LAN_SERVER_PORT_FALLBACK, LAN_SERVER_PORT_MIN,
                      *range(LAN_SERVER_PORT_MIN + 2, LAN_SERVER_PORT_MAX + 1)]
        server_port = next((port for port in candidates if port not in occupied), None)
        if server_port is None:
            raise ValueError(f"{LAN_SERVER_PORT_MIN}..{LAN_SERVER_PORT_MAX} 中没有可用端口")
    else:
        server_port = next_free_port(LAN_SERVER_PORT_MIN, occupied)
    occupied.add(server_port)
    master_server_port = next_free_port(DEFAULT_STEAM_MASTER_PORT, occupied)
    occupied.add(master_server_port)
    authentication_port = next_free_port(DEFAULT_STEAM_AUTH_PORT, occupied)
    return ShardConfig(
        network={"server_port": server_port},
        shard={"is_master": False, "name": shard_name, "id": next_shard_id(cluster)},
        account={"encode_user_path": True},
        steam={"master_server_port": master_server_port, "authentication_port": authentication_port},
    )


def _read_mods(path: Path) -> dict:
    """读取 modoverrides.lua；解析失败直接报错，不能当成空配置再写回把原有 Mod 清掉。"""
    if not path.is_file():
        return {}
    data = parse_lua_file(path)
    return data if isinstance(data, dict) else {}


def _write_lua(path: Path, data: dict) -> None:
    path.write_text(serialize_lua_table(data) + "\n", encoding="utf-8")


@contextmanager
def _restore_on_error(paths):
    """块内出错时把 paths 恢复成进入前的内容（原本不存在的删掉），再把异常抛出去。"""
    originals = {path: path.read_bytes() if path.is_file() else None for path in paths}
    try:
        yield
    except Exception:
        for path, content in originals.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(content)
        raise


def worldgate_key(mods_list) -> str:
    """存档里已启用的万界之门条目键名（本机开发用的目录名优先保留），都没有时用工坊 ID。"""
    for mods in mods_list:
        for key in _WORLDGATE_KEYS:
            entry = mods.get(key)
            if isinstance(entry, dict) and entry.get("enabled", True):
                return key
    return _WORLDGATE_KEYS[0]


def _shard_id(shard, master) -> int | None:
    """世界编号：主世界固定为 1，其余取 server.ini [SHARD] id，没写时返回 None。"""
    if shard is master:
        return 1
    path = shard.path / "server.ini"
    try:
        return int(str(parse_server_ini(path).shard.get("id", "")).strip())
    except (OSError, ValueError):
        return None


def _load_worldgate(cluster: Cluster, master) -> tuple[dict, str, dict]:
    """各世界的 modoverrides（按分片名）、万界之门条目键名，以及以主世界为准的条目副本（已设为启用）。"""
    mods_by_shard = {shard.name: _read_mods(shard.path / "modoverrides.lua") for shard in cluster.shards}
    key = worldgate_key(mods_by_shard.values())
    entry = mods_by_shard[master.name].get(key)
    entry = copy.deepcopy(entry) if isinstance(entry, dict) else {}
    entry["enabled"] = True
    if not isinstance(entry.get("configuration_options"), dict):
        entry["configuration_options"] = {}
    return mods_by_shard, key, entry


def _apply_worldgate(mods_by_shard: dict, key: str, entry: dict) -> None:
    """所有世界写同一份万界之门条目：worlds、语言等配置各世界不一致时玩家换世界会看到不同内容。"""
    for mods in mods_by_shard.values():
        mods[key] = copy.deepcopy(entry)


def _existing_worlds(cluster: Cluster, master) -> dict:
    """主世界万界之门配置里已有的 worlds；没有或不是表（Mod 页默认值是空字符串）时返回空。"""
    worlds = _load_worldgate(cluster, master)[2]["configuration_options"].get("worlds")
    return worlds if isinstance(worlds, dict) else {}


def _shard_location(shard) -> str | None:
    """世界类型（leveldataoverride.lua 的 location），读不出时返回 None。"""
    try:
        return shard_plan_from_template(shard.leveldata_path).location if shard.leveldata_path else None
    except (OSError, ValueError):
        return None


def _world_names(cluster: Cluster, master, existing, new_world: tuple[int, str, str | None] | None,
                 language: str) -> dict:
    """万界之门的 worlds 配置：保留现有世界的条目（含名称、人数上限）；新世界用指定名称，
    没指定的世界按类型取名，同名时加序号。"""
    locations: dict[int, str | None] = {}
    for shard in cluster.shards:
        shard_id = _shard_id(shard, master)
        if shard_id is not None:
            locations[shard_id] = _shard_location(shard)
    # 只留存档里还在的世界：删掉的世界留下的条目会占着名字，新世界复用它的编号时还会继承旧名称
    current = {str(shard_id) for shard_id in locations}
    result = {str(k): copy.deepcopy(v) for k, v in existing.items() if isinstance(v, dict) and str(k) in current} \
        if isinstance(existing, dict) else {}
    if new_world is not None:
        locations[new_world[0]] = new_world[1]
        if new_world[2]:
            result[str(new_world[0])] = {"name": new_world[2]}
    used_names = {v.get("name") for v in result.values()}
    for shard_id in sorted(locations):
        entry = result.setdefault(str(shard_id), {})
        if entry.get("name") or locations[shard_id] is None:
            continue
        base = get_location_definition(locations[shard_id]).name(language)
        name, index = base, 2
        while name in used_names:
            name, index = f"{base} {index}", index + 1
        entry["name"] = name
        used_names.add(name)
    # 没取到类型又没有名称的世界不写空条目，由 Mod 按分片名兜底显示；
    # 键必须写成字符串（["504115402"]），写成数字键会让新世界生成时加载不到任何 Mod
    return {LuaStringKey(k): v for k, v in result.items() if v}


def add_shard(cluster: Cluster, shard_name: str, plan: WorldShardPlan, used_ports=(), language: str = "zh",
              world_name: str | None = None) -> Path:
    """在已停止的存档里新建一个世界分片目录，返回其路径（调用方负责确认存档未运行）。

    ``used_ports`` 为其他存档和系统已占用的端口；本存档自己的端口会自动避开。所有世界同时启用
    万界之门并写入 worlds（编号与名称），否则玩家到不了新世界：原版洞口只连主世界与 Caves。
    ``language`` 只在存档还没设置万界之门语言时写入（纯专服识别不到中文会落到英文）。
    ``world_name`` 为新世界在万界之门面板上的名称，不传时按类型自动取名。
    """
    validate_new_shard(cluster, shard_name, plan)
    config = plan_shard_config(cluster, shard_name, used_ports)
    master = _master_shard(cluster)

    cluster_ini_path = cluster.path / "cluster.ini"
    cluster_config = parse_cluster_ini(cluster_ini_path)
    new_cluster_config = copy.deepcopy(cluster_config)
    shard_section = new_cluster_config.shard
    shard_section["shard_enabled"] = True
    for key in _SHARD_REQUIRED_KEYS:
        if shard_section.get(key) in (None, ""):
            shard_section[key] = CLUSTER_INI_DEFAULTS[("SHARD", key)]
    if shard_section.get("master_port") in (None, ""):
        claims, _issues = collect_cluster_port_claims(cluster)
        occupied = set(used_ports) | {claim.port for claim in claims if claim.field != "master_port"}
        occupied.update((config.network["server_port"], config.steam["master_server_port"],
                         config.steam["authentication_port"]))
        shard_section["master_port"] = next_free_port(DEFAULT_MASTER_PORT, occupied)
    master_ini_path = master.path / "server.ini"
    master_config = parse_server_ini(master_ini_path)
    master_needs_flag = not master_config.shard.get("is_master")
    if master_needs_flag:
        master_config.shard["is_master"] = True

    # 万界之门：所有世界（含新世界）启用同一份配置，worlds 列出每个世界的编号与名称
    mods_by_shard, key, entry = _load_worldgate(cluster, master)
    options = entry["configuration_options"]
    options["worlds"] = _world_names(cluster, master, options.get("worlds"),
                                     (int(config.shard["id"]), plan.location, (world_name or "").strip() or None),
                                     language)
    options.setdefault("language", language)
    _apply_worldgate(mods_by_shard, key, entry)

    # 先在存档目录里的临时目录写好，server.ini 最后写（没有 server.ini 的目录不会被识别成世界），再整体改名
    while True:
        temp_dir = cluster.path / f".{shard_name}.{secrets.token_hex(8)}"
        try:
            temp_dir.mkdir()
            break
        except FileExistsError:
            continue
    destination = cluster.path / shard_name
    touched = [cluster_ini_path, master_ini_path, *(shard.path / "modoverrides.lua" for shard in cluster.shards)]
    try:
        with _restore_on_error(touched):
            (temp_dir / "leveldataoverride.lua").write_text(
                serialize_lua_table(shard_level_data(plan)) + "\n", encoding="utf-8")
            _write_lua(temp_dir / "modoverrides.lua", mods_by_shard[master.name])
            write_server_ini(config, temp_dir / "server.ini")
            if new_cluster_config != cluster_config:
                write_cluster_ini(new_cluster_config, cluster_ini_path)
            if master_needs_flag:
                write_server_ini(master_config, master_ini_path)
            for shard in cluster.shards:
                _write_lua(shard.path / "modoverrides.lua", mods_by_shard[shard.name])
            os.replace(temp_dir, destination)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    return destination


# ── 管理已有世界 ─────────────────────────────────────────────────────

# 原版洞口与大漩涡按这两个分片名绑定世界（cave_entrance/cave_exit/oceanwhirlbigportal 的 shard_name，
# 每次世界连上时由 Shard_UpdateWorldState 按名字重新绑定），改了名原版通道会断，新名字也不能占用它们
PROTECTED_SHARD_NAMES = ("Master", "Caves")


@dataclass(frozen=True)
class WorldEntry:
    """管理世界列表里的一行：分片目录信息加上万界之门 worlds 里的设置。"""

    shard_name: str
    shard_id: int | None
    location: str | None
    is_master: bool
    name: str        # 万界之门面板上的名称，没设置时为空（Mod 用分片名兜底）
    hidden: bool
    limit: int       # 人数上限，0 为不限


def worldgate_enabled(cluster: Cluster) -> bool:
    """存档里有世界启用了万界之门（名称、隐藏、人数上限只对它有意义）。"""
    mods_list = [_read_mods(shard.path / "modoverrides.lua") for shard in cluster.shards]
    return any(isinstance(mods.get(key), dict) and mods[key].get("enabled", True)
               for mods in mods_list for key in _WORLDGATE_KEYS)


def unreachable_shards(cluster: Cluster) -> list[str]:
    """只能靠万界之门进入、存档却没启用它的世界，启用了时返回空：主世界与 Caves 以外的森林和洞穴
    （原版洞口只按这两个名字连接；海难、猪镇等 Mod 世界由提供它的 Mod 自己连通，不算在内）。
    岛屿冒险核心（含三合一整合版）把洞口、海洋之椅等改为按世界类型连接，不看分片名，启用时不算。"""
    if cluster.source != SaveSource.SERVER:
        return []
    if IA_CORE_MOD_ID in location_capability_ids(get_enabled_mod_ids(cluster)):
        return []
    master = _master_shard(cluster)
    extra = [shard.name for shard in cluster.shards
             if shard is not master and shard.name not in PROTECTED_SHARD_NAMES
             and _shard_location(shard) in (FOREST_LOCATION, CAVE_LOCATION)]
    if not extra or worldgate_enabled(cluster):
        return []
    return extra


def list_worlds(cluster: Cluster) -> list[WorldEntry]:
    """存档里的全部世界：主世界、Caves（原版洞口绑定、不能改文件夹）在前，其余按编号排序；
    名称等取主世界的万界之门配置。"""
    master = _master_shard(cluster)
    worlds = _existing_worlds(cluster, master) if master is not None else {}
    entries = []
    for shard in cluster.shards:
        shard_id = _shard_id(shard, master)
        entry = worlds.get(str(shard_id)) if shard_id is not None else None
        entry = entry if isinstance(entry, dict) else {}
        try:
            limit = max(0, int(entry.get("limit") or 0))
        except (TypeError, ValueError):
            limit = 0
        entries.append(WorldEntry(shard.name, shard_id, _shard_location(shard), shard is master,
                                  str(entry.get("name") or ""), entry.get("hidden") is True, limit))
    return sorted(entries, key=lambda e: (not e.is_master, e.shard_name not in PROTECTED_SHARD_NAMES,
                                          e.shard_id is None, e.shard_id or 0, e.shard_name))


def _find_shard(cluster: Cluster, shard_name: str):
    shard = next((s for s in cluster.shards if s.name == shard_name), None)
    if shard is None:
        raise ValueError(f"存档里找不到世界 {shard_name}")
    return shard


def _require_server(cluster: Cluster):
    """管理操作的公共前提：专服存档、能找到主世界；返回主世界分片。"""
    if cluster.source != SaveSource.SERVER:
        raise ValueError("只有专服存档可以管理世界，本地存档请在游戏内操作")
    master = _master_shard(cluster)
    if master is None:
        raise ValueError("存档里找不到主世界")
    return master


def renamed_world_error(cluster: Cluster, shard_name: str, world_name: str) -> str:
    """改世界名称的问题，没问题返回空字符串：不能为空，也不能和其他世界重名。"""
    name = world_name.strip()
    if not name:
        return t("world.add_shard_name_empty")
    if any(e.name == name for e in list_worlds(cluster) if e.shard_name != shard_name):
        return t("world.add_shard_name_exists", name=name)
    return ""


def update_world_entry(cluster: Cluster, shard_name: str, world_name: str, hidden: bool, limit: int,
                       language: str = "zh") -> None:
    """改一个世界在万界之门里的名称、是否隐藏和人数上限，所有世界的 modoverrides 同步写入。

    只在存档已启用万界之门时可用：给没用它的存档写配置等于悄悄替用户启用了一个 Mod。"""
    master = _require_server(cluster)
    shard = _find_shard(cluster, shard_name)
    if not worldgate_enabled(cluster):
        raise ValueError("这个存档没有启用万界之门，名称、隐藏和人数上限不会生效")
    error = renamed_world_error(cluster, shard_name, world_name)
    if error:
        raise ValueError(error)
    shard_id = _shard_id(shard, master)
    if shard_id is None:
        raise ValueError(f"{shard_name} 的 server.ini 没有世界编号（[SHARD] id）")
    mods_by_shard, key, entry = _load_worldgate(cluster, master)
    options = entry["configuration_options"]
    worlds = _world_names(cluster, master, options.get("worlds"), None, language)
    item = worlds.setdefault(str(shard_id), {})
    item["name"] = world_name.strip()
    # 默认值不写，配置保持精简（Mod 缺省即不隐藏、不限人数）
    if hidden:
        item["hidden"] = True
    else:
        item.pop("hidden", None)
    if limit > 0:
        item["limit"] = int(limit)
    else:
        item.pop("limit", None)
    options["worlds"] = worlds
    options.setdefault("language", language)
    _apply_worldgate(mods_by_shard, key, entry)
    paths = [s.path / "modoverrides.lua" for s in cluster.shards]
    with _restore_on_error(paths):
        for s in cluster.shards:
            _write_lua(s.path / "modoverrides.lua", mods_by_shard[s.name])


def delete_shard(cluster: Cluster, shard_name: str, language: str = "zh", remove=move_to_recycle_bin) -> None:
    """删除一个非主世界：先把其余世界的万界之门 worlds 去掉它，再把目录交给 ``remove``（默认移到回收站），
    移不走时（文件被占用等）配置恢复原样。cluster.ini 的多世界设置不动，只剩主世界也能照常开服。"""
    master = _require_server(cluster)
    shard = _find_shard(cluster, shard_name)
    if shard is master:
        raise ValueError("主世界不能删除")
    remaining = replace(cluster, shards=[s for s in cluster.shards if s is not shard])
    paths = []
    writes = {}
    if worldgate_enabled(remaining):
        mods_by_shard, key, entry = _load_worldgate(remaining, master)
        options = entry["configuration_options"]
        options["worlds"] = _world_names(remaining, master, options.get("worlds"), None, language)
        _apply_worldgate(mods_by_shard, key, entry)
        writes = {s.path / "modoverrides.lua": mods_by_shard[s.name] for s in remaining.shards}
        paths = list(writes)
    with _restore_on_error(paths):
        for path, mods in writes.items():
            _write_lua(path, mods)
        remove(shard.path)


def rename_shard_error(cluster: Cluster, shard_name: str, new_name: str) -> str:
    """改世界文件夹名的问题，没问题返回空字符串。"""
    master = _master_shard(cluster)
    if shard_name in PROTECTED_SHARD_NAMES or (master is not None and master.name == shard_name):
        return t("world.manage_folder_protected", name=shard_name)
    if new_name.casefold() in (name.casefold() for name in PROTECTED_SHARD_NAMES):
        return t("world.manage_folder_reserved", name=new_name)
    if new_name == shard_name:
        return ""
    return shard_name_error(cluster, new_name, current=shard_name)


def rename_shard_folder(cluster: Cluster, shard_name: str, new_name: str) -> Path:
    """改一个世界的文件夹名，server.ini [SHARD] name 同步改成新名字，返回新目录。

    世界编号不变，万界之门 worlds 按编号索引，不用改；DSTCamp 里按分片名记录的内网穿透映射由调用方处理。"""
    _require_server(cluster)
    shard = _find_shard(cluster, shard_name)
    error = rename_shard_error(cluster, shard_name, new_name)
    if error:
        raise ValueError(error)
    if new_name == shard_name:
        return shard.path
    ini_path = shard.path / "server.ini"
    config = parse_server_ini(ini_path)
    config.shard["name"] = new_name
    destination = cluster.path / new_name
    with _restore_on_error([ini_path]):
        write_server_ini(config, ini_path)
        os.replace(shard.path, destination)
    return destination
