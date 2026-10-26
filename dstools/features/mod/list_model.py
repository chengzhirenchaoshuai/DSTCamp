"""主页与创建向导共用的 Mod 列表名称、排序、筛选和行模型。"""

from __future__ import annotations

import functools

from dstools.features.mod.parser import is_custom_steam_mod_id
from dstools.i18n import t
from dstools.models import Platform

# Mod 作者常拿自定义图标字体的私用区字形当装饰（如 U+F000D），任何系统字体都画不出，排序前去掉
_PUA_RANGES = ((0xE000, 0xF8FF), (0xF0000, 0xFFFFD), (0x100000, 0x10FFFD))


def strip_unrenderable(text: str) -> str:
    """去掉私用区码位并收敛多余空格。"""
    collapsed = "".join(
        " " if any(lo <= ord(ch) <= hi for lo, hi in _PUA_RANGES) else ch for ch in text
    )
    return " ".join(collapsed.split())

_strcmplogicalw = None


def version_display(mod_info, include_label: bool = True) -> str:
    if mod_info is None:
        return t("mod.version_unresolved")
    status = getattr(mod_info, "version_status", "pending")
    if status == "confirmed":
        return (
            t("mod.version_value", version=mod_info.version)
            if include_label
            else mod_info.version
        )
    if status == "undeclared":
        return t("mod.version_undeclared")
    if status == "unresolved":
        return t("mod.version_unresolved")
    return t("mod.version_pending")


def _windows_name_cmp(a: str, b: str) -> int:
    global _strcmplogicalw
    if _strcmplogicalw is None:
        import ctypes

        _strcmplogicalw = ctypes.windll.shlwapi.StrCmpLogicalW
        _strcmplogicalw.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
        _strcmplogicalw.restype = ctypes.c_int
    return _strcmplogicalw(a, b)


def _name_bucket(name: str) -> int:
    ch = name[:1]
    if not ch:
        return 4
    if "一" <= ch <= "鿿":
        return 0
    if ch.isdigit():
        return 3
    if ch.isalpha():
        return 2
    if not ch.isalnum():
        return 1
    return 4


def mod_name_cmp(a: str, b: str) -> int:
    ca, cb = _name_bucket(a), _name_bucket(b)
    return ca - cb if ca != cb else _windows_name_cmp(a, b)


def localize_mod_name(mod_id: str, name: str) -> str:
    if not name:
        return name
    try:
        from dstools.i18n import get_lang

        if get_lang() != "zh":
            return name
        from dstools.features.mod.chs_translation import display_name
        from dstools.features.world.mod_settings import MOD_DISPLAY_NAMES

        display = MOD_DISPLAY_NAMES.get(mod_id) or MOD_DISPLAY_NAMES.get(
            str(mod_id).removeprefix("workshop-")
        )
        # 自带登记表优先，其次是用户订阅的汉化 Mod Chinese++ Pro 给出的名称
        return (display.get("zh") if display else "") or display_name(mod_id) or name
    except Exception:
        return name


def sort_mod_data(
    mod_data: dict, mod_infos: dict, *, priority_mod_id: str | None = None,
) -> dict:
    """按启用状态和名称排序，并可把一个页面级优先项固定到最前。"""

    def name_of(mod_id):
        info = mod_infos.get(mod_id)
        raw = (info.name if info else "") or mod_id
        return strip_unrenderable(raw) or raw

    ordered = sorted(
        mod_data,
        key=functools.cmp_to_key(lambda a, b: mod_name_cmp(name_of(a), name_of(b))),
    )
    ordered.sort(key=lambda mod_id: not mod_data[mod_id].enabled)
    if priority_mod_id is not None:
        ordered.sort(key=lambda mod_id: mod_id != priority_mod_id)
    return {mod_id: mod_data[mod_id] for mod_id in ordered}


def merge_visible_mod_ids(installed_ids, configured_mods: dict) -> list[str]:
    """合并要显示的 Mod：已安装的全部显示；存档已启用但本机没有内容的也显示（接手别人的存档时能看到缺失项）；
    已禁用且本地没有内容的旧记录隐藏。"""
    result = []
    seen = set()
    for mod_id in installed_ids:
        if mod_id in seen:
            continue
        seen.add(mod_id)
        result.append(mod_id)
    for mod_id, entry in configured_mods.items():
        if mod_id in seen or not getattr(entry, "enabled", False):
            continue
        seen.add(mod_id)
        result.append(mod_id)
    return result


def referenced_missing_status_text(status) -> str:
    """存档引用但本机没有文件的 Mod 在列表第三行显示原因（未订阅、下载中等）；status 尚未查到时为 None。"""
    from dstools.features.mod.workshop_status import WorkshopModState

    labels = {
        WorkshopModState.UNSUBSCRIBED_REFERENCED: "mod.update_latest_unsubscribed_referenced",
        WorkshopModState.SUBSCRIBED_BY_OTHER_ACCOUNT: "mod.update_latest_other_account",
        WorkshopModState.MISSING: "mod.update_latest_missing",
        WorkshopModState.NOT_INSTALLED: "mod.update_latest_not_installed",
        WorkshopModState.SOURCE_UNAVAILABLE: "mod.update_latest_source_unavailable",
        WorkshopModState.DOWNLOADING: "mod.update_latest_downloading",
        WorkshopModState.DOWNLOAD_PENDING: "mod.update_latest_pending",
    }
    key = labels.get(getattr(status, "state", None))
    return t(key) if key else t("mod.reference_missing_local")


def build_mod_rows(
    mod_data: dict,
    mod_infos: dict,
    query: str,
    show: str,
    platform: Platform,
    *,
    show_local: bool = False,
    separate_client_mods: bool = True,
    locked_mod_id: str | None = None,
) -> list[dict]:
    """从页面独立状态和共享元数据生成统一渲染行。"""
    needle = str(query or "").strip().casefold()
    rows = []
    for mod_id, mod in mod_data.items():
        info = mod_infos.get(mod_id)
        is_local = bool(info and info.client_only)
        if separate_client_mods and show_local != is_local:
            continue
        if show == "enabled" and not mod.enabled:
            continue
        if show == "disabled" and mod.enabled:
            continue
        if show == "custom" and (
            platform != Platform.STEAM or not is_custom_steam_mod_id(mod_id)
        ):
            continue
        raw_name = info.name if info else getattr(mod, "name", "")
        name = localize_mod_name(mod_id, raw_name)
        # 显示译名时原名也要能搜到
        if (
            needle
            and needle not in str(mod_id).casefold()
            and needle not in (name or "").casefold()
            and needle not in (raw_name or "").casefold()
        ):
            continue
        numeric_id = str(mod_id).removeprefix("workshop-")
        rows.append(
            {
                "workshop_id": mod_id,
                "name": name,
                "version_text": version_display(info),
                "enabled": bool(mod.enabled),
                "is_local": is_local,
                "locked": bool(locked_mod_id and mod_id == locked_mod_id),
                "has_config": bool(
                    info and (info.config_options or info.unsupported_schema)
                ),
                "has_link": numeric_id.isascii() and numeric_id.isdecimal(),
            }
        )
    return rows
