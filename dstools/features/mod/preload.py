"""启动后在后台预热已安装 Mod：填磁盘缓存（modinfo 解析、图标 PNG、作者声明的版本号），并留一份扫描快照。

新用户首次打开 Mod 页时，每个 Mod 都要转一次图标、起一个沙箱子进程读版本，几百个 Mod 要几十秒；
预热后都直接命中缓存。快照只交给 Mod 页用一次：先按快照立即显示，再后台重扫核对，Mod 有增删改时以重扫为准。
快照不带图片（首屏图标由 Mod 页从磁盘缓存读），也不发布到 ModCatalogStore（不带图标的快照会让新建存档向导
显示无图标列表）。缺 VC++ 2013 运行库时只跳过图标转换（tex_to_png 自己判断），列表、名称和版本照常预热。
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from dstools.features.mod.icons import get_mod_icon_path
from dstools.features.mod.legacy_v1 import find_legacy_packages, materialize_legacy_package_for_read
from dstools.features.mod.local_version import apply_local_version, resolve_local_version_target
from dstools.features.mod.modinfo_cache import parse_modinfo_cached
from dstools.features.mod.parser import ModInfo, engine_folder_name, find_mod_folder, list_installed_mod_ids
from dstools.models import Platform

# 沙箱每个 Mod 起一个子进程；比 Mod 页的 4 路并发低，避免刚启动时抢满 CPU、引来杀软集中扫描
VERSION_WORKERS = 2


@dataclass
class PreloadSnapshot:
    """一次完整预热的扫描结果；source 与 Mod 页按存档算出的目录参数一致时才可用。"""
    source: tuple
    ids: list[str]
    infos: dict[str, ModInfo | None]
    paths: dict[str, Path]
    icon_paths: dict[str, Path]
    stamps: dict[str, tuple | None]


@dataclass
class PreloadStats:
    mods: int = 0
    icons: int = 0
    versions: int = 0
    cancelled: bool = False
    snapshot: PreloadSnapshot | None = field(default=None, repr=False)


def resolve_readable_mod_folder(wid, platform, wegame_client_mods_dir, steam_runtime_mods_dir,
                                legacy_packages) -> Path | None:
    """Mod 内容目录；只有 V1 Legacy 包时临时解压出只读副本。"""
    text_id = str(wid).removeprefix("workshop-")
    archive = legacy_packages.get(int(text_id)) if text_id.isdigit() else None
    mod_folder = find_mod_folder(wid, platform, wegame_client_mods_dir, steam_runtime_mods_dir)
    if mod_folder is None and archive is not None:
        mod_folder = materialize_legacy_package_for_read(int(text_id), archive)
    return mod_folder


def modinfo_stamp(mod_folder: Path | None) -> tuple | None:
    """判断 Mod 是否变过的轻量指纹：目录加 modinfo.lua 的修改时间与大小。"""
    if mod_folder is None:
        return None
    try:
        stat = (Path(mod_folder) / "modinfo.lua").stat()
    except OSError:
        return (str(mod_folder),)
    return str(mod_folder), stat.st_mtime_ns, stat.st_size


def warm_mod_caches(
    platform: Platform,
    wegame_client_mods_dir: Path | None = None,
    steam_runtime_mods_dir: Path | None = None,
    stop: threading.Event | None = None,
) -> PreloadStats:
    """按与 Mod 页相同的规则枚举 Mod 并逐个填缓存；stop 置位后尽快返回，中止时不产出快照。"""
    stop = stop or threading.Event()
    stats = PreloadStats()
    legacy_packages = find_legacy_packages() if platform == Platform.STEAM else {}
    ids = list_installed_mod_ids(platform, wegame_client_mods_dir, legacy_packages=legacy_packages,
                                 steam_runtime_mods_dir=steam_runtime_mods_dir)
    infos, paths, stamps, icon_paths = {}, {}, {}, {}
    for wid in ids:
        if stop.is_set():
            stats.cancelled = True
            return stats
        try:
            folder = resolve_readable_mod_folder(wid, platform, wegame_client_mods_dir, steam_runtime_mods_dir,
                                                 legacy_packages)
            infos[wid] = parse_modinfo_cached(folder) if folder else None
        except Exception:
            folder = None
            infos[wid] = None
        if folder is not None:
            paths[wid] = folder
        stamps[wid] = modinfo_stamp(folder)
    targets = [(wid, paths[wid], info) for wid, info in infos.items() if info is not None and wid in paths]
    stats.mods = len(targets)

    # 图标先做：单次转换只要零点几秒，用户最先看到的就是图标
    for wid, folder, info in targets:
        if stop.is_set():
            stats.cancelled = True
            return stats
        try:
            icon_path = get_mod_icon_path(info, folder, platform)
        except Exception:
            continue
        if icon_path is not None:
            icon_paths[wid] = icon_path

    def resolve(target):
        if stop.is_set():
            return None
        try:
            return resolve_local_version_target(target)
        except Exception:
            return None

    version_targets = [(wid, folder, engine_folder_name(folder)) for wid, folder, info in targets]
    with ThreadPoolExecutor(max_workers=VERSION_WORKERS) as pool:
        for resolved in pool.map(resolve, version_targets):
            if resolved is not None:
                wid, version = resolved
                apply_local_version(infos[wid], version)
                stats.versions += 1
    stats.icons = len(icon_paths)
    if stop.is_set():
        stats.cancelled = True
        return stats
    stats.snapshot = PreloadSnapshot(
        source=(platform, wegame_client_mods_dir, steam_runtime_mods_dir), ids=list(ids), infos=infos,
        paths=paths, icon_paths=icon_paths, stamps=stamps)
    return stats
