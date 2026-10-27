"""Steam 通道（唯一来源）：Steam 客户端或 SteamCMD，由用户在「Steam 通道」里明确切换。

专服安装更新、Mod 下载等所有 Steam 相关操作都按这里的选择走，两条路径互不混用，不会因为找不到一种就偷偷换另一种。
"""

from __future__ import annotations

from enum import Enum

from dstools.features.local_service import steamcmd_updater
from dstools.features.local_service.server_runtime import ServerRuntime
from dstools.shared import app_settings
from dstools.shared.resource_paths import path_is_ascii


class SteamBackend(str, Enum):
    CLIENT = "client"
    STEAMCMD = "steamcmd"


def get_backend() -> SteamBackend:
    try:
        return SteamBackend(app_settings.get_steam_backend() or SteamBackend.CLIENT.value)
    except ValueError:
        return SteamBackend.CLIENT


def set_backend(backend: SteamBackend) -> None:
    app_settings.set_steam_backend(SteamBackend(backend).value)


def steamcmd_unavailable_reason() -> str:
    """找不到可用 steamcmd.exe 时的原因文案键：分清自选的、内置的，非 ASCII 路径单独说明。"""
    custom = steamcmd_updater.custom_steamcmd_path()
    if custom is not None:
        if steamcmd_updater.is_steamcmd_exe(custom) and not path_is_ascii(custom):
            return "local.backend_steamcmd_custom_non_ascii"
        return "local.backend_steamcmd_missing"
    if not path_is_ascii(steamcmd_updater.bundled_steamcmd_path()):
        return "local.backend_steamcmd_cache_non_ascii"
    return "local.backend_steamcmd_extract_failed"


def update_blocker(backend: SteamBackend, runtime: ServerRuntime | None) -> str | None:
    """当前 Steam 通道不能更新这个开服程序时返回原因文案键，可以更新返回 None。"""
    if backend is SteamBackend.STEAMCMD:
        if steamcmd_updater.find_steamcmd() is None:
            return steamcmd_unavailable_reason()
        if runtime is None:
            return None  # 没有开服程序时可以用 SteamCMD 全新安装独立专服
        if runtime.is_client:
            # 游戏本体只能由拥有游戏的账号更新，匿名 SteamCMD 不行
            return "local.backend_steamcmd_client_runtime"
        if steamcmd_updater.is_steam_library_install(runtime.install_dir):
            # 同一份文件不交给两个程序管理，避免 Steam 客户端和 SteamCMD 的清单互相打架
            return "local.backend_steamcmd_library_dir"
        return None
    if runtime is not None and not runtime.is_client \
            and not steamcmd_updater.is_steam_library_install(runtime.install_dir):
        return "local.backend_client_steamcmd_dir"
    return None
