"""开服场景：在自己电脑上开服，或在云服务器上开服。

场景不单独存储，就是 Steam 通道的另一种说法，免得两个设置互相矛盾：
- 自己电脑：Steam 客户端通道，开服程序默认用游戏本体自带的（与游戏同步更新、不用另装约 4.5 GB 的独立专服）；
- 云服务器：SteamCMD 通道，匿名安装独立专服、下载 Mod，不需要游戏本体和 Steam 客户端。

从没选过 Steam 通道即"还没选场景"，启动时弹窗让用户选。
"""

from __future__ import annotations

from enum import Enum

from dstools.features.local_service.server_runtime import (
    RuntimeKind, RuntimeMode, find_runtime_of_kind, migrate_runtime_settings, set_runtime_mode,
)
from dstools.features.local_service.steam_backend import SteamBackend, get_backend, set_backend
from dstools.shared import app_settings


class UsageScenario(str, Enum):
    LOCAL = "local"
    CLOUD = "cloud"


def backend_for(scenario: UsageScenario) -> SteamBackend:
    return SteamBackend.STEAMCMD if scenario is UsageScenario.CLOUD else SteamBackend.CLIENT


def current_scenario() -> UsageScenario:
    return UsageScenario.CLOUD if get_backend() is SteamBackend.STEAMCMD else UsageScenario.LOCAL


def scenario_chosen() -> bool:
    return app_settings.get_steam_backend() is not None


def suggested_scenario() -> UsageScenario:
    """弹窗预选：本机找不到 Steam.exe（典型的云服务器）预选云服务器，否则预选自己电脑。"""
    from dstools.features.local_service.steam_client_updater import find_steam_executable
    return UsageScenario.LOCAL if find_steam_executable() is not None else UsageScenario.CLOUD


def migrate_usage_scenario() -> None:
    """引入开服场景时对老用户的一次性迁移；须在启动早期、写入任何设置之前调用（靠设置文件是否为空区分新老用户）。

    - 旧版"自动"优先独立专服，现已去掉"自动"、没选过的优先游戏专服；原来自动选中独立专服的老用户固定为"独立专服"：
      不悄悄换开服程序，否则放在专服 mods 里的 Mod、LuaJIT 副本都会像是丢了；
    - 没选过 Steam 通道但本机有 Steam 的老用户直接定为自己电脑，不再弹窗；找不到 Steam 的照常弹窗确认。
    """
    if app_settings.get_usage_scenario_migrated():
        return
    if app_settings.load_settings():
        migrate_runtime_settings()
        # 读原始值：get_runtime_mode() 会把"自动"按新规则（优先游戏专服）定下来，这里要按旧规则判断
        if (get_backend() is SteamBackend.CLIENT and app_settings.get_server_runtime_mode() in (None, "auto")
                and find_runtime_of_kind(RuntimeKind.DEDICATED) is not None
                and find_runtime_of_kind(RuntimeKind.CLIENT) is not None):
            set_runtime_mode(RuntimeMode.DEDICATED)
        if not scenario_chosen() and suggested_scenario() is UsageScenario.LOCAL:
            set_backend(SteamBackend.CLIENT)
    app_settings.set_usage_scenario_migrated()
