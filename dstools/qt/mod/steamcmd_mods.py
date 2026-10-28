"""SteamCMD 通道的工坊 Mod 下载/更新流程：检查 → 下载缺失和有更新的 → 复查，全程在日志窗口里显示。

与 Steam 客户端通道的 WorkshopUpdateDialog 互不混用；Mod 页更新、启动前补下载缺失 Mod 都走这里。
"""

from __future__ import annotations

import re
import threading

from dstools.features.local_service import steamcmd_updater
from dstools.features.local_service.steam_backend import steamcmd_unavailable_reason
from dstools.features.mod.steamcmd_mod_status import SteamcmdModState, inspect_steamcmd_mods
from dstools.i18n import t
from dstools.qt import dialogs
from dstools.qt.threads import run_async_with_log
from dstools.shared.app_settings import get_steamcmd_account

# workshop_log 的结果行（实测）：Download item <ID> result : OK / File Not Found
_RESULT_RE = re.compile(r"Download item (\d+) result : (.+)$")
# 复查后仍未装好/不是最新的再整体重下几轮（大 Mod 超时、断线等原因不看输出文字，只看复查结果）
_DOWNLOAD_ROUNDS = 2
_STATE_KEYS = {
    SteamcmdModState.MISSING: "steamcmd_mods.state_missing",
    SteamcmdModState.OUTDATED: "steamcmd_mods.state_outdated",
}


def _label(workshop_id: str, title: str) -> str:
    return f"workshop-{workshop_id}" + (f"（{title}）" if title else "")


def run_steamcmd_mod_update(parent, workshop_ids, *, on_finished=None) -> None:
    """检查并下载/更新给定的工坊 Mod；没有可用的 SteamCMD 时直接提示原因。"""
    ids = [str(w) for w in dict.fromkeys(workshop_ids) if str(w).isdigit()]
    title = t("steamcmd_mods.title")
    steamcmd = steamcmd_updater.find_steamcmd()
    if steamcmd is None:
        dialogs.show_warning(parent, title, t(steamcmd_unavailable_reason()))
        return
    account = get_steamcmd_account()
    cancel_event = threading.Event()
    dialog = dialogs.LogDialog(parent, title, on_cancel=cancel_event.set)
    dialogs.fit_to_screen(dialog, dialogs.DIALOG_WIDTHS["lg"], 620)
    dialog.show()

    def work(emit):
        emit(t("steamcmd_mods.checking", count=len(ids),
               account=account or t("local.steamcmd_account_anonymous")))
        try:
            statuses = inspect_steamcmd_mods(ids)
        except OSError as exc:
            emit(t("steamcmd_mods.check_failed", detail=str(exc)))
            return None
        titles = {wid: status.title for wid, status in statuses.items()}
        targets = [wid for wid, status in statuses.items() if status.needs_download]
        unavailable = [wid for wid, status in statuses.items() if status.state is SteamcmdModState.UNAVAILABLE]
        for wid in targets:
            emit(f"· {_label(wid, titles[wid])}：{t(_STATE_KEYS[statuses[wid].state])}")
        if unavailable:
            emit(t("steamcmd_mods.unavailable", mods="、".join(f"workshop-{wid}" for wid in unavailable)))
        client_only = [wid for wid, status in statuses.items() if status.state is SteamcmdModState.CLIENT_ONLY]
        if client_only:
            emit(t("steamcmd_mods.client_only_skipped", mods="、".join(_label(w, titles[w]) for w in client_only)))
        if not targets:
            emit(t("steamcmd_mods.all_current"))
            return 0, 0

        emit(t("steamcmd_mods.downloading", count=len(targets)))

        def on_line(line: str) -> None:
            match = _RESULT_RE.search(line)
            if match:
                wid, result = match.groups()
                emit(t("steamcmd_mods.item_result", mod=_label(wid, titles.get(wid, "")), result=result.strip()))

        failed = targets
        for round_no in range(_DOWNLOAD_ROUNDS):
            if round_no:
                emit(t("steamcmd_mods.retrying", count=len(failed),
                       mods="、".join(_label(w, titles.get(w, "")) for w in failed)))
            try:
                steamcmd_updater.download_workshop_items(steamcmd, failed, on_line=on_line, account=account,
                                                         cancel_event=cancel_event)
            except steamcmd_updater.SteamcmdLoginError:
                emit(t("local.steamcmd_login_expired", user=account))
                return None
            # 以复查结果为准：下载"成功"但仍不是最新的也算失败
            try:
                after = inspect_steamcmd_mods(failed)
            except OSError as exc:
                emit(t("steamcmd_mods.check_failed", detail=str(exc)))
                return None
            failed = [wid for wid in failed if after[wid].state is not SteamcmdModState.CURRENT]
            if not failed:
                break
        if failed:
            emit(t("steamcmd_mods.failed_list", mods="、".join(_label(w, titles.get(w, "")) for w in failed)))
        return len(targets) - len(failed), len(failed)

    def done(result) -> None:
        if result is not None:
            ok, failed = result
            dialog.append(t("steamcmd_mods.done", ok=ok, failed=failed),
                          "result_warning" if failed else "result_success")
        dialog.finish()
        if on_finished is not None:
            on_finished()

    def error(exc: Exception) -> None:
        if isinstance(exc, InterruptedError):
            dialog.append(t("steamcmd_mods.cancelled"), "result_warning")
        else:
            dialog.append(t("steamcmd_mods.error", detail=f"{type(exc).__name__}: {exc}"), "result_error")
        dialog.finish()
        if on_finished is not None:
            on_finished()  # 中途出错时可能已有部分 Mod 下载完成

    run_async_with_log(work, dialog.append, done, error)
