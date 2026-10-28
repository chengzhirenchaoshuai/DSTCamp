"""读取并规范化本地 modinfo.lua 声明的版本，Mod 页、创建向导与 Workshop 状态检测共用同一份可信结果。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dstools.features.mod.version_cache import load_version_result, save_version_result


VERSION_CONFIRMED = "confirmed"
VERSION_UNDECLARED = "undeclared"
VERSION_UNRESOLVED = "unresolved"

_TRAILING_LUA_COMMENT_RE = re.compile(r"\s+--.*$")


def normalize_version_for_compare(value: str) -> str:
    """复刻游戏 ``ModIndex`` 的版本比较预处理：去首尾空白并转小写。"""
    return str(value or "").strip().lower()


def _normalize_field(result: Any) -> tuple[str, str]:
    """把沙箱单字段协议规范化为 ``(原始显示值, 可信状态)``。"""
    if not isinstance(result, dict) or not isinstance(result.get("declared"), bool):
        return "", VERSION_UNRESOLVED
    if not result["declared"]:
        return "", VERSION_UNDECLARED
    value = result.get("value")
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return "", VERSION_UNRESOLVED
    value = str(value).strip()
    # 少数作者把说明写进版本字符串（如 "0.0.6  --版本"），只清理"空白 + --"形式，不影响带连字符的版本号
    value = _TRAILING_LUA_COMMENT_RE.sub("", value).rstrip()
    return (value, VERSION_CONFIRMED) if value else ("", VERSION_UNDECLARED)


@dataclass(frozen=True)
class LocalModVersion:
    """一次本地版本解析结果：显示值与可信状态。"""

    version: str = ""
    status: str = VERSION_UNRESOLVED
    version_compatible: str = ""
    name: str = ""
    name_status: str = VERSION_UNRESOLVED
    icon: str = ""
    icon_status: str = VERSION_UNRESOLVED
    icon_atlas: str = ""
    icon_atlas_status: str = VERSION_UNRESOLVED

    @property
    def compare_version(self) -> str:
        return (
            normalize_version_for_compare(self.version)
            if self.status == VERSION_CONFIRMED
            else ""
        )


def normalize_version_result(result: dict[str, Any] | None) -> LocalModVersion:
    """把双字段沙箱协议转换成稳定的领域对象。"""
    if not isinstance(result, dict):
        return LocalModVersion()
    name, name_status = _normalize_field(result.get("name"))
    icon, icon_status = _normalize_field(result.get("icon"))
    icon_atlas, icon_atlas_status = _normalize_field(result.get("icon_atlas"))
    version, status = _normalize_field(result.get("version"))
    compatible, _ = _normalize_field(result.get("version_compatible"))
    return LocalModVersion(
        name=name,
        name_status=name_status,
        icon=icon,
        icon_status=icon_status,
        icon_atlas=icon_atlas,
        icon_atlas_status=icon_atlas_status,
        version=version,
        status=status,
        version_compatible=compatible,
    )


def resolve_local_mod_version(
    workshop_id: str, mod_folder: Path, folder_name: str | None = None
) -> LocalModVersion:
    """读取一个本地 Mod 的最终版本，优先使用内容指纹完全匹配的缓存。"""
    mod_folder = Path(mod_folder)
    if not folder_name:
        from dstools.features.mod.parser import engine_folder_name

        folder_name = engine_folder_name(mod_folder)
    modinfo_path = mod_folder / "modinfo.lua"
    cached = load_version_result(str(workshop_id), modinfo_path, folder_name)
    if cached is not None:
        return normalize_version_result(cached)
    try:
        text = modinfo_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return LocalModVersion()
    from dstools.features.mod.sandbox import resolve_mod_versions

    result = resolve_mod_versions(text, folder_name=folder_name)
    if result is not None:
        save_version_result(str(workshop_id), modinfo_path, folder_name, result)
    return normalize_version_result(result)


def resolve_local_version_target(target) -> tuple[str, LocalModVersion]:
    """线程池使用的轻量适配器，保留调用方传入的 Mod ID。"""
    workshop_id, mod_folder, folder_name = target
    return workshop_id, resolve_local_mod_version(
        str(workshop_id), Path(mod_folder), str(folder_name)
    )


def apply_local_version(info, version: LocalModVersion) -> None:
    """把沙箱确认的名称、图标声明和版本写回 ModInfo（Mod 页与启动预热共用）。"""
    if version.name_status == VERSION_CONFIRMED:
        info.name = version.name
    if version.icon_status == VERSION_CONFIRMED:
        info.icon = version.icon
    if version.icon_atlas_status == VERSION_CONFIRMED:
        info.icon_atlas = version.icon_atlas
    info.version = version.version
    info.version_status = version.status
    info.version_compatible = version.version_compatible
