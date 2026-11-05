"""本地存档（游戏客户端"创建游戏"的存档）改由 DSTCamp 用专服直接启动：启动参数与开服前同步。

游戏客户端开本地存档时拉起的也是专服进程，命令行为
``-persistent_storage_root APP:Klei/ -conf_dir DoNotStarveTogether -cluster Cluster_1
-ownernetid <SteamID64> -ownerdir <账号ID> -clouddir <账号ID> -token <临时令牌>``（取自真机 server_log），
DSTCamp 照此用 ``-ownerdir`` 定位存档，令牌改用令牌池里的（写进存档的 cluster_token.txt）。

客户端每次点"开始"时会按各世界 save/shardindex 重写 cluster.ini、modoverrides.lua、leveldataoverride.lua
（ServerCreationScreen:Create），专服只读这些文件、运行时再按 modoverrides 回写 shardindex 的 enabled_mods。
所以开服前要先做一遍客户端的同步，否则用户在游戏里改过但没开过服的设置会被专服读旧文件、再写回冲掉。

2026-11-05 真机往返实测（离线/在线存档各一）：专服开 → 玩家进服是原角色 → 客户端再开 → 专服再开，
存档、角色与 Mod 均正常；客户端开服会把端口改回 10999/10998（master_port 10888），故端口每次开服前都要重新写。
"""

import os
import tempfile
from pathlib import Path

from dstools.features.cluster_config.config_manager import (
    get_cluster_option,
    load_cluster_config,
    save_cluster_config,
    set_cluster_option,
)
from dstools.features.cluster_config.local_server_data import LOCAL_SERVER_FIELDS, load_local_server_data
from dstools.features.local_service.dedicated_server import resolve_storage_args
from dstools.features.mod.manager import ModEntry, ModOverrides, load_mod_overrides, save_mod_overrides
from dstools.features.mod.shardindex import load_shardindex_mods, shardindex_path
from dstools.features.world.local_options import load_local_overrides
from dstools.shared.lua_parser import parse_lua_file, serialize_lua_table

# Steam 32 位账号 ID（本地存档的账号目录名）换算 SteamID64 的基数：
# 280257116 + 基数 = 76561198240522844，与客户端开服命令行的 -ownernetid 一致（已实测）
STEAM_ID64_BASE = 76561197960265728

# 几个 Mod 往客户端 Mod 列表注入的"仅服务端"代理名 -> 专服认的工坊文件夹名。
# 客户端在主菜单把 shardindex 里的工坊名换成代理名，开服时再换回来（钩子在各 Mod 里），专服没有这一步：
# 代理名原样写进 modoverrides 会被专服当成不存在的 Mod 静默跳过（已实测）。
# 依据：workshop-3444078585/inject_server_only_mod.lua、workshop-2941527805/main/AddtoServerModList.lua、
# workshop-3750536829/bbgoat_utils/AddClientModToServerModList.lua 的 server_folder_name / folder_name。
PROXY_MOD_NAMES = {
    "DontStarveLuaJIT2-Server": "workshop-3444078585",
    "Chinese-Pro-Server": "workshop-2941527805",
    "bbgoat-utils-server": "workshop-3750536829",
}

# shardindex.server.privacy_type 取值（scripts/constants.lua 的 PRIVACY_TYPE），只有"仅限本地"对应 cluster.ini 字段
_PRIVACY_LOCAL = 2


def owner_steam_id64(account_id: str) -> str | None:
    """本地存档账号目录名（Steam 32 位账号 ID）换算成 SteamID64；不是纯数字（如 WeGame）返回 None。"""
    if not account_id or not account_id.isdigit():
        return None
    return str(STEAM_ID64_BASE + int(account_id))


def local_storage_args(cluster) -> list[str]:
    """本地存档的存储参数：存档根目录取存档自己所在的位置（不受"服务器存档目录"设置影响），再按账号目录定位。"""
    account_dir = cluster.path.parent
    args = resolve_storage_args(account_dir.parent) + ["-ownerdir", account_dir.name]
    steam_id = owner_steam_id64(account_dir.name)
    if steam_id:
        args += ["-ownernetid", steam_id]
    return args


def _server_mods(cluster_path: Path) -> dict[str, ModEntry]:
    """Master shardindex 的 Mod（游戏前端以它为准），代理名换回工坊名；两种名字同时出现时以代理名一条为准。"""
    mods: dict[str, ModEntry] = {}
    for key, entry in load_shardindex_mods(shardindex_path(cluster_path)).items():
        real = PROXY_MOD_NAMES.get(key)
        if real is None:
            mods.setdefault(key, entry)
        else:
            mods[real] = ModEntry(workshop_id=real, enabled=entry.enabled,
                                  configuration_options=dict(entry.configuration_options))
    return mods


def _sync_mods(cluster) -> list[str]:
    mods = _server_mods(cluster.path)
    changed = []
    for shard in cluster.shards:
        path = shard.mod_overrides_path or shard.path / "modoverrides.lua"
        current = load_mod_overrides(path).mods if path.exists() else {}
        if current == mods:
            continue
        target = ModOverrides(path=path)
        target.mods.update(mods)
        save_mod_overrides(target)
        changed.append(f"{shard.name}/modoverrides.lua")
    return changed


def _sync_cluster_ini(cluster) -> list[str]:
    server = load_local_server_data(cluster.path)
    wanted = {field: server[key] for field, key in LOCAL_SERVER_FIELDS.items() if key in server}
    if "privacy_type" in server:
        wanted[("NETWORK", "lan_only_cluster")] = server["privacy_type"] == _PRIVACY_LOCAL
    config = load_cluster_config(cluster.path)
    dirty = False
    for (section, key), value in wanted.items():
        text = str(value).lower() if isinstance(value, bool) else str(value)
        current = get_cluster_option(config, section, key)
        current_text = str(current).lower() if isinstance(current, bool) else ("" if current is None else str(current))
        if current_text != text:
            set_cluster_option(config, section, key, text)
            dirty = True
    if not dirty:
        return []
    save_cluster_config(config, cluster.path)
    return ["cluster.ini"]


def _write_text_atomically(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _sync_leveldata(cluster) -> list[str]:
    changed = []
    for shard in cluster.shards:
        overrides = load_local_overrides(shard.path)
        path = shard.leveldata_path or shard.path / "leveldataoverride.lua"
        raw = parse_lua_file(path) if path.exists() else {}
        if not isinstance(raw, dict):
            raise ValueError(f"{path} 的根节点必须是 Lua table")
        if raw.get("overrides") == overrides:
            continue
        raw["overrides"] = overrides
        _write_text_atomically(path, serialize_lua_table(raw))
        changed.append(f"{shard.name}/leveldataoverride.lua")
    return changed


def sync_local_save_for_launch(cluster) -> list[str]:
    """开服前把 shardindex 里的设置同步到专服读取的文件（相当于客户端点"开始"时做的），返回改动过的文件。

    shardindex 缺失或损坏抛 ShardIndexError，Lua 文件损坏抛 ValueError/LuaParseError，调用方应中止开服。"""
    return _sync_mods(cluster) + _sync_cluster_ini(cluster) + _sync_leveldata(cluster)
