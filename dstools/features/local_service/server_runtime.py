"""开服程序（运行时）的唯一来源，由"开服程序"选项（server_runtime_mode）决定：
游戏客户端 / 独立专服二选一，固定使用指定的一种，找不到如实报告，绝不偷偷换另一种。

没选过（或旧版本存的"自动"）时按本机情况定一次并保存：有游戏本体用游戏专服（与游戏同步更新、不用另装），
只有独立专服用独立专服，都没有按游戏专服提示安装。之后装卸程序不会自动改选项。

开服目录、mods、更新用 App ID、LuaJIT 副本位置、Steam API DLL 都从 resolve_runtime() 派生，
业务代码不得直接调用 find_dedicated_server_dir() 等探测函数（有测试守护）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from dstools.features.local_service.dedicated_server import (
    CLIENT_APP_ID, DEDICATED_SERVER_APP_ID, find_dedicated_server_dir, find_steamcmd_server_dir,
    is_client_install_dir, is_steam_library_install, is_valid_install_dir,
)
from dstools.shared import app_settings
from dstools.shared.steam_discovery import find_all_steam_libraries

_CLIENT_DIR_NAME = "Don't Starve Together"


class RuntimeMode(str, Enum):
    CLIENT = "client"
    DEDICATED = "dedicated"


class RuntimeKind(str, Enum):
    CLIENT = "client"
    DEDICATED = "dedicated"


@dataclass(frozen=True)
class ServerRuntime:
    """一个确定可以开服的程序目录。"""

    kind: RuntimeKind
    install_dir: Path

    @property
    def is_client(self) -> bool:
        return self.kind is RuntimeKind.CLIENT

    @property
    def app_id(self) -> str:
        """检查和请求更新时使用的 Steam App。"""
        return CLIENT_APP_ID if self.is_client else DEDICATED_SERVER_APP_ID

    @property
    def mods_dir(self) -> Path:
        """服务器实际读取的 mods（V1 与手动安装的 Mod）。"""
        return self.install_dir / "mods"


    @property
    def steam_api_dll(self) -> Path | None:
        """与开服程序同一份的 Steam API DLL，Mod 更新优先用它。"""
        # DSTCamp 是 64 位进程，只能加载 bin64 里的 64 位 DLL。
        candidate = self.install_dir / "bin64" / "steam_api64.dll"
        return candidate if candidate.is_file() else None


@dataclass(frozen=True)
class RuntimeResolution:
    """开服程序选项与实际选中的程序；指定的程序没找到时 runtime 为 None。"""

    mode: RuntimeMode
    runtime: ServerRuntime | None

    @property
    def wanted_kind(self) -> RuntimeKind:
        return RuntimeKind.CLIENT if self.mode is RuntimeMode.CLIENT else RuntimeKind.DEDICATED


def get_runtime_mode() -> RuntimeMode:
    """开服程序选项；没选过或是旧版本的"自动"时按本机情况定一次并保存（见模块说明）。"""
    migrate_runtime_settings()
    try:
        return RuntimeMode(app_settings.get_server_runtime_mode())
    except ValueError:
        pass
    # 只看 Steam 库：SteamCMD 通道固定用独立专服、不读这个选项，选项只对 Steam 客户端通道有意义
    mode = (RuntimeMode.DEDICATED if find_client_install_dir() is None and find_dedicated_server_dir() is not None
            else RuntimeMode.CLIENT)
    set_runtime_mode(mode)
    return mode


def set_runtime_mode(mode: RuntimeMode) -> None:
    app_settings.set_server_runtime_mode(RuntimeMode(mode).value)


def migrate_runtime_settings() -> None:
    """迁移旧设置（幂等）：专服路径实际指向客户端目录时挪到客户端路径并切到客户端模式；
    指向 Steam 库以外（SteamCMD 安装）的独立专服时挪给 SteamCMD 通道，Steam 客户端通道不再使用它。"""
    stored = app_settings.get_dedicated_server_path()
    if stored is None:
        return
    if is_client_install_dir(stored):
        app_settings.set_client_runtime_path(stored)
        app_settings.clear_dedicated_server_path()
        if app_settings.get_server_runtime_mode() is None:
            app_settings.set_server_runtime_mode(RuntimeMode.CLIENT.value)
    elif not is_steam_library_install(stored):
        if app_settings.get_steamcmd_server_path() is None:
            app_settings.set_steamcmd_server_path(stored)
        app_settings.clear_dedicated_server_path()


def find_client_install_dir() -> Path | None:
    """游戏客户端安装目录：手动确认过的 > 各 Steam 库的标准位置。"""
    remembered = app_settings.get_client_runtime_path()
    if remembered and is_client_install_dir(remembered):
        return remembered
    for lib in find_all_steam_libraries():
        install_dir = lib / "steamapps" / "common" / _CLIENT_DIR_NAME
        if is_client_install_dir(install_dir):
            return install_dir
    return None


def _steamcmd_channel() -> bool:
    # 延迟导入：steam_backend 依赖本模块
    from dstools.features.local_service.steam_backend import SteamBackend, get_backend
    return get_backend() is SteamBackend.STEAMCMD


def find_runtime_of_kind(kind: RuntimeKind) -> ServerRuntime | None:
    """独立专服按 Steam 通道各找各的：Steam 客户端通道只认 Steam 库里的，SteamCMD 通道只认它自己的目录。"""
    if kind is RuntimeKind.CLIENT:
        install_dir = find_client_install_dir()
    else:
        install_dir = find_steamcmd_server_dir() if _steamcmd_channel() else find_dedicated_server_dir()
    return ServerRuntime(kind, install_dir) if install_dir is not None else None


def effective_runtime_mode() -> RuntimeMode:
    """实际生效的开服程序选项：Steam 通道为 SteamCMD 时固定用独立专服（SteamCMD 只能匿名安装更新独立专服），
    用户存的选项不改，切回 Steam 客户端通道后照旧生效。"""
    if _steamcmd_channel():
        return RuntimeMode.DEDICATED
    return get_runtime_mode()


def resolve_runtime(mode: RuntimeMode | None = None) -> RuntimeResolution:
    """按开服程序选项选出实际开服程序。"""
    migrate_runtime_settings()  # SteamCMD 通道不读开服程序选项，迁移不能只放在 get_runtime_mode 里
    mode = effective_runtime_mode() if mode is None else RuntimeMode(mode)
    return RuntimeResolution(mode, find_runtime_of_kind(RuntimeResolution(mode, None).wanted_kind))


def current_runtime() -> ServerRuntime | None:
    return resolve_runtime().runtime


def accepts_install_dir(kind: RuntimeKind, path: Path) -> bool:
    """"更换路径"只接受当前模式对应类型的目录，避免路径和模式互相矛盾；
    独立专服还要属于当前 Steam 通道（见 dedicated_dir_wrong_channel）。"""
    if kind is RuntimeKind.CLIENT:
        return is_client_install_dir(path)
    return is_valid_install_dir(path) and not dedicated_dir_wrong_channel(path)


def dedicated_dir_wrong_channel(path: Path) -> bool:
    """是独立专服，但属于另一个 Steam 通道：Steam 库里的归 Steam 客户端，库以外的归 SteamCMD。"""
    return is_valid_install_dir(path) and is_steam_library_install(path) == _steamcmd_channel()


def remember_install_dir(kind: RuntimeKind, path: Path) -> None:
    if kind is RuntimeKind.CLIENT:
        app_settings.set_client_runtime_path(path)
    elif _steamcmd_channel():
        app_settings.set_steamcmd_server_path(path)
    else:
        app_settings.set_dedicated_server_path(path)
