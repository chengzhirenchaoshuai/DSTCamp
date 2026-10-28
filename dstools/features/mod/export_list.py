"""导出已启用 Mod 列表图片用的条目整理（纯逻辑，不依赖界面）。"""

from dataclasses import dataclass
from pathlib import Path

from dstools.features.mod.list_model import localize_mod_name


@dataclass
class ExportModEntry:
    workshop_id: str  # mod_data 的原始键，用来取图标
    name: str
    author: str
    version: str  # 只放沙箱已确认的版本，其余留空
    id_text: str  # 创意工坊数字 ID；非工坊 Mod 为空
    mod_format: str = ""  # 工坊格式 "V1"/"V2"，判断不了（本地 Mod 等）为空


def build_export_entries(mod_data: dict, mod_infos: dict, formats: dict | None = None) -> list[ExportModEntry]:
    """取已启用的服务端 Mod（排除 client_only），顺序沿用 mod_data 的现有排序。

    ``formats`` 为 Mod 键 → "V1"/"V2"，与 Mod 页列表的标签同一来源。"""
    formats = formats or {}
    entries = []
    for mod_id, mod in mod_data.items():
        if not mod.enabled:
            continue
        info = mod_infos.get(mod_id)
        if info is not None and info.client_only:
            continue
        numeric = str(mod_id).removeprefix("workshop-")
        version = info.version if info is not None and info.version_status == "confirmed" else ""
        entries.append(ExportModEntry(
            workshop_id=mod_id,
            name=localize_mod_name(mod_id, info.name if info else getattr(mod, "name", "")) or numeric,
            author=(info.author if info else "").strip(),
            version=(version or "").strip(),
            id_text=numeric if numeric.isascii() and numeric.isdecimal() else "",
            mod_format=formats.get(mod_id) or "",
        ))
    return entries


def collect_export_data(mod_ids, folders: dict, platform) -> tuple[list[ExportModEntry], dict]:
    """按 Mod 目录直接解析导出条目和图标（供运行中的专服使用，不依赖 Mod 页已加载的数据）。

    会转换图标、跑沙箱补版本号，耗时，只能放后台线程。排除 client_only，按名称排序。
    """
    import functools
    from concurrent.futures import ThreadPoolExecutor

    from dstools.features.mod.icons import get_mod_icon_path, load_mod_icon_image
    from dstools.features.mod.list_model import mod_name_cmp
    from dstools.features.mod.local_version import resolve_local_mod_version
    from dstools.features.mod.parser import engine_folder_name, parse_modinfo

    def one(mod_id):
        folder = folders.get(mod_id)
        info = icon = None
        if folder is not None:
            try:
                info = parse_modinfo(folder)
            except (OSError, ValueError, TypeError):
                info = None
        if info is not None:
            try:
                resolved = resolve_local_mod_version(str(mod_id), folder, engine_folder_name(folder))
                if resolved.name_status == "confirmed":
                    info.name = resolved.name
                if resolved.icon_status == "confirmed":
                    info.icon = resolved.icon
                if resolved.icon_atlas_status == "confirmed":
                    info.icon_atlas = resolved.icon_atlas
                info.version = resolved.version
                info.version_status = resolved.status
            except Exception:
                pass
            try:
                path = get_mod_icon_path(info, folder, platform)
                icon = load_mod_icon_image(path) if path is not None else None
            except Exception:
                icon = None
        return mod_id, info, icon

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(one, mod_ids))
    infos = {mod_id: info for mod_id, info, _icon in results}
    icons = {mod_id: icon for mod_id, _info, icon in results if icon is not None}
    enabled = {mod_id: _Enabled() for mod_id in infos}
    entries = build_export_entries(enabled, infos, {mod_id: loaded_mod_format(mod_id, folders.get(mod_id))
                                                    for mod_id in infos})
    entries.sort(key=functools.cmp_to_key(lambda a, b: mod_name_cmp(a.name, b.name)))
    return entries, icons


def loaded_mod_format(mod_id: str, folder: Path | None) -> str | None:
    """专服实际加载的目录对应的工坊格式：工坊内容目录（以数字 ID 命名）是 V2，
    mods 目录里的 workshop-ID 是 V1 部署出来的副本；本地 Mod 判断不了，返回 None。"""
    numeric = str(mod_id).removeprefix("workshop-")
    if folder is None or not (numeric.isascii() and numeric.isdecimal()):
        return None
    if folder.name == numeric:
        return "V2"
    if folder.name.lower() == f"workshop-{numeric}":
        return "V1"
    return None


class _Enabled:
    """build_export_entries 只读 enabled 与 name 两个属性，这里给运行中已加载的 Mod 用。"""
    enabled = True
    name = ""
