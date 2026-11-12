"""本地服务器页用到的小段纯逻辑。"""

from pathlib import Path

from dstools.features.cluster_config.config_manager import load_cluster_config
from dstools.features.mod.parser import find_game_mods_dir, find_shared_ugc_directory, find_workshop_dir, parse_modinfo
from dstools.features.local_service.dedicated_server import ServerStatus
from dstools.i18n import t
from dstools.shared.shard_roles import master_shard

RUNNING_LIKE = (ServerStatus.STARTING, ServerStatus.RUNNING, ServerStatus.STOPPING)

STATUS_TEXT_KEYS = {
    ServerStatus.STARTING: "local.status_starting",
    ServerStatus.RUNNING: "local.status_running",
    ServerStatus.STOPPING: "local.status_stopping",
    ServerStatus.STOPPED: "local.status_stopped",
    ServerStatus.CRASHED: "local.status_crashed",
}


def ordered_shards(cluster):
    """主世界（按 is_master 判定，见 shard_roles）排在最前面，其余世界保持原有相对顺序。"""
    master = master_shard(cluster)
    return sorted(cluster.shards, key=lambda s: s is not master)


def max_rollback_days(cluster) -> int:
    """游戏保留 max_snapshots 份快照（默认 6），能回退的次数比这个数少一。"""
    config = load_cluster_config(cluster.path)
    try:
        snapshots = int(config.misc.get("max_snapshots", 6))
    except (TypeError, ValueError):
        snapshots = 6
    return max(1, snapshots - 1)


def _process_mod_roots(proc) -> list[Path]:
    """专服进程查找 Mod 的目录顺序：专服 UGC 目录、客户端 Workshop 目录、开服程序 mods 目录、游戏 mods 目录。"""
    roots: list[Path] = []
    if getattr(proc, "ugc_directory", None):
        roots.append(Path(proc.ugc_directory) / "content" / "322330")
    workshop = find_workshop_dir()
    if workshop:
        roots.append(workshop)
    if getattr(proc, "install_dir", None):
        roots.append(Path(proc.install_dir) / "mods")
    game_mods = find_game_mods_dir()
    if game_mods:
        roots.append(game_mods)
    return roots


def find_process_mod_folders(proc, mod_ids) -> dict[str, Path]:
    """按专服进程的查找顺序定位各 Mod 的本地目录（含 modinfo.lua），找不到的不出现在结果里。"""
    roots = _process_mod_roots(proc)
    result: dict[str, Path] = {}
    for mod_id in mod_ids:
        folder_names = [mod_id]
        if mod_id.lower().startswith("workshop-"):
            folder_names.append(mod_id[9:])
        folder = next((root / name for root in roots for name in folder_names
                       if (root / name / "modinfo.lua").is_file()), None)
        if folder is not None:
            result[mod_id] = folder
    return result


def mod_names(folders: dict[str, Path]) -> dict[str, str]:
    """读取各 Mod 目录 modinfo.lua 里的名称，读不到的不出现在结果里。"""
    names: dict[str, str] = {}
    for mod_id, folder in folders.items():
        try:
            info = parse_modinfo(folder)
        except (OSError, ValueError, TypeError):
            continue
        name = (info.name or "").strip() if info else ""
        if name:
            names[mod_id] = name
    return names


def mod_display_names(proc, mod_ids: tuple[str, ...]) -> tuple[str, ...]:
    """把诊断中的 Mod ID 尽力解析成"ID（名称）"，失败时保留 ID。"""
    names = mod_names(find_process_mod_folders(proc, mod_ids))
    return tuple(t("local.mod_id_with_name", id=mod_id, name=names[mod_id]) if mod_id in names else mod_id
                 for mod_id in mod_ids)


__all__ = [
    "RUNNING_LIKE",
    "STATUS_TEXT_KEYS",
    "ordered_shards",
    "max_rollback_days",
    "mod_display_names",
    "find_process_mod_folders",
    "mod_names",
    "find_shared_ugc_directory",
]
