"""Mod 默认配置与游戏全局配置（主菜单"模组"）的同步入口。

启动、打开 Mod 页、保存默认配置后触发，在后台执行；同一时间只跑一个，运行中再次触发则结束后补跑一次。
只用 Steam 账号目录（WeGame 客户端进程名未核实，无法确认游戏已关闭），本机有多个账号时跟随存档栏选中的账号。
不弹模态框，结果写 sync.log 并轻提示。
"""

from pathlib import Path

from dstools.features.mod.config_memory import append_log, sync_with_game
from dstools.features.mod.legacy_v1 import is_dst_client_running
from dstools.i18n import t
from dstools.qt import dialogs
from dstools.qt.threads import run_async
from dstools.shared import app_settings

_state = {"running": False, "rerun": False, "parent": None, "callbacks": [], "account": ""}
# 最近一次同步得到的各 Mod 状态（mod_key -> synced/pending/error），供默认配置列表显示
last_states: dict[str, str] = {}


def game_account(ctx) -> tuple[Path, str] | None:
    """当前 Steam 账号目录与账号 ID；没有时返回 None。"""
    env = ctx.env
    if not env.client_klei_root or not env.user_id:
        return None
    path = Path(env.client_klei_root) / env.user_id
    return (path, env.user_id) if path.is_dir() else None


def steamcmd_channel() -> bool:
    """Steam 通道为 SteamCMD：这种组合一般没有游戏客户端，不与游戏同步全局配置。"""
    from dstools.features.local_service.steam_backend import SteamBackend, get_backend
    return get_backend() is SteamBackend.STEAMCMD


def sync_available(ctx) -> bool:
    return (app_settings.get_mod_config_sync_enabled() and not steamcmd_channel()
            and game_account(ctx) is not None)


def sync_off_text() -> str:
    """同步不可用时的说明：SteamCMD 通道单独说明原因。"""
    return t("mod.default_config_sync_steamcmd" if steamcmd_channel() else "mod.default_config_sync_off")


def _client_only_keys() -> set[str]:
    """本机已安装的 client_only Mod：它们的全局配置文件不带 _CLIENT 后缀。"""
    from dstools.features.mod.modinfo_cache import parse_modinfo_cached
    from dstools.features.mod.parser import find_mod_folder, list_installed_mod_ids

    keys = set()
    for wid in list_installed_mod_ids():
        try:
            folder = find_mod_folder(wid)
            info = parse_modinfo_cached(folder) if folder else None
        except Exception:
            continue
        if info and info.client_only:
            keys.add(wid)
    return keys


def start_sync(ctx, parent=None, on_done=None) -> bool:
    """后台同步一次；parent 不为空时在其窗口上提示结果。on_done(report) 在界面线程回调，失败时 report 为 None、
    原因见 last_error。返回 False 表示同步不可用（未开启或找不到账号目录），此时不会回调。"""
    if not sync_available(ctx):
        return False
    if on_done is not None:
        _state["callbacks"].append(on_done)
    if _state["running"]:
        _state["rerun"] = True
        _state["parent"] = parent if parent is not None else _state["parent"]
        return True
    user_dir, account = game_account(ctx)
    if account != _state["account"]:
        # 换了账号：上一个账号的同步状态不再适用，等本轮结果
        last_states.clear()
        _state["account"] = account
    _state["running"] = True
    # 本轮只回调开始前登记的；运行中新登记的留给补跑那一轮（它们要看到补跑后的状态）
    run_callbacks, _state["callbacks"] = _state["callbacks"], []

    def work():
        report = sync_with_game(user_dir, account, _client_only_keys(), can_write=not is_dst_client_running())
        append_log(report)
        return report

    def finish(report) -> None:
        _state["running"] = False
        if report is not None:
            _state["last_error"] = ""
            last_states.clear()
            last_states.update(report.states)
            if parent is not None:
                _notify(parent, report)
        for callback in run_callbacks:
            callback(report)
        if _state["rerun"]:
            rerun_parent = _state["parent"]
            _state["rerun"], _state["parent"] = False, None
            start_sync(ctx, rerun_parent)

    def fail(exc: Exception) -> None:
        _state["last_error"] = str(exc) or type(exc).__name__
        finish(None)

    run_async(work, finish, fail)
    return True


def sync_if_account_changed(ctx) -> None:
    """环境重新扫描后，当前账号与上次同步的不同（切换了账号）才补同步；账号没变不重复同步。"""
    account = game_account(ctx)
    if account is not None and _state["account"] and account[1] != _state["account"]:
        start_sync(ctx)


def last_error() -> str:
    """最近一次同步失败的原因（成功后清空）。"""
    return _state.get("last_error", "")


def _notify(parent, report) -> None:
    parts = []
    if report.pulled:
        parts.append(t("mod.config_sync_pulled", count=len(report.pulled)))
    if report.pushed:
        parts.append(t("mod.config_sync_pushed", count=len(report.pushed)))
    if report.pending:
        parts.append(t("mod.config_sync_pending", count=len(report.pending)))
    if parts:
        dialogs.show_toast(parent, t("mod.config_sync_toast", detail=" · ".join(parts)), ms=3000)
