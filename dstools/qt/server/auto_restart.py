"""本地服务器页的崩溃自动重启调度（判定规则见 features/local_service/auto_restart.py）：

1. 世界运行后崩溃，等 CRASH_RESTART_DELAY 秒再拉起，只重启崩溃的世界。主世界崩溃时洞穴等从世界保持运行：
   饥荒会让它暂停并每 10 秒左右重连，主世界回来后自动回退到对应快照（"Synchronizing backward to master
   snapshot"），存档保持一致，无需重启从世界；
2. 拉起前：原令牌仍在等待期、开启换令牌且已过设定分钟数（默认 0）、池中有空闲令牌时直接换用；
   否则用原令牌拉起（忽略其等待标记）；
3. 注册冲突时不停服，等专服自行重试成功；设定了等待分钟数时，到点仍冲突且池中有替代令牌才停服换令牌重启；
   超过 TOKEN_WAIT_LIMIT 不再管理。注册成功即结束本轮并记录用时。
全程不弹模态框（用户可能不在电脑前），失败原因显示在横幅并发托盘通知。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime

from PySide6.QtCore import QObject, QTimer

from dstools.features.cluster_config.config_manager import load_cluster_config
from dstools.features.local_service import luajit_injector
from dstools.features.local_service.auto_restart import (
    CRASH_RESTART_DELAY, MAX_CRASH_RESTARTS, TOKEN_BUSY_RECHECK, TOKEN_WAIT_LIMIT, CrashBudget, append_log,
    is_restartable,
)
from dstools.features.local_service.dedicated_server import resolve_storage_args
from dstools.features.local_service.shard_helpers import RUNNING_LIKE, ordered_shards
from dstools.i18n import t
from dstools.shared.app_settings import (
    get_auto_restart_enabled, get_token_switch_after_minutes, get_token_switch_on_timeout,
)
from dstools.shared.resource_paths import data_dir
from dstools.shared.token_manager import read_token, token_fingerprint


@dataclass
class _ClusterState:
    budget: CrashBudget = field(default_factory=CrashBudget)
    phase: str = "idle"          # idle / scheduled / waiting_token / starting / waiting_release / gave_up
    due: float = 0.0             # 下一次动作的时刻（time.time()）
    full: bool = False           # True：整组重启 group（只在超时换令牌时）；False：只重启 shards
    shards: set[str] = field(default_factory=set)
    group: set[str] = field(default_factory=set)  # 崩溃时实际在运行的世界（含崩溃的那个），整组重启的范围
    crashed_at: float = 0.0
    attempts: int = 0            # 本轮崩溃后已拉起几次
    conflicts: int = 0           # 本轮注册冲突次数
    registered: bool = False
    switched: bool = False       # 本轮已经（尝试）换过令牌，不再换第二次
    switch_pending: bool = False  # 下一次拉起前换用池中其它令牌
    reason: str = ""             # gave_up 的原因
    timer: QTimer | None = None


class AutoRestartController(QObject):
    def __init__(self, page):
        super().__init__(page)
        self._page = page
        self._states: dict[str, _ClusterState] = {}
        self._log_path = data_dir("auto_restart") / "auto_restart.log"

    # ── 页面回调 ────────────────────────────────────────────────────────
    def on_failure(self, proc, report) -> None:
        """控制台诊断出一次失败（每个进程最多一次）。页面已先更新令牌等待标记。"""
        cluster = self._page._cluster_for_running_process(proc, None)
        if cluster is None or not get_auto_restart_enabled(str(cluster.path)):
            return
        state = self._state(cluster)
        now = time.time()
        in_attempt = (state.phase in ("starting", "waiting_release")
                      and proc.shard_name in (state.group if state.full else state.shards))
        if report.category == "token_conflict":
            if in_attempt:
                state.conflicts += 1
                due = self._release_deadline(state)
                self._log(cluster, f"{proc.shard_name} 注册冲突（E_ROWID_EXIST），距崩溃 {self._elapsed(state)}，"
                                   f"保持运行等专服自行重试，{datetime.fromtimestamp(due):%H:%M:%S} 再检查")
                self._schedule(cluster, state, "waiting_release", due)
            return
        if not is_restartable(report.category, proc.world_ready, in_attempt):
            if in_attempt:
                self._give_up(cluster, state, report.title)
            return
        if state.phase in ("scheduled", "waiting_token"):
            # 已在排队：又有世界崩溃，一并重启它
            state.shards.add(proc.shard_name)
            state.group.add(proc.shard_name)
            return
        if not state.budget.allow(now):
            self._give_up(cluster, state, t("local.auto_restart_reason_limit", count=MAX_CRASH_RESTARTS))
            return
        state.crashed_at = now
        state.full = False
        state.shards = {proc.shard_name}
        # 崩溃的进程已不算"运行中"，要单独补进去；没开过的世界（如用户只开了地上）不在范围内
        state.group = {s.name for s in cluster.shards if self._running(cluster, s.name)} | {proc.shard_name}
        state.attempts = state.conflicts = 0
        state.registered = state.switched = state.switch_pending = False
        self._schedule(cluster, state, "scheduled", now + CRASH_RESTART_DELAY)
        self._log(cluster, f"{proc.shard_name} 崩溃（{report.title}），{int(CRASH_RESTART_DELAY)} 秒后自动重启，"
                           f"30 分钟内第 {len(state.budget.times)} 次")
        self._notify(t("local.auto_restart_tray_crashed", cluster=cluster.name, shard=proc.shard_name))

    def on_registered(self, proc) -> None:
        state = self._states.get(str(proc.cluster_path))
        if state is None or state.phase not in ("starting", "waiting_release"):
            return
        if state.phase == "waiting_release":
            self._stop_timer(state)
            state.phase = "starting"
        state.registered = True
        cluster = self._page._cluster_for_running_process(proc, None)
        if cluster is not None:
            self._log(cluster, f"注册成功，距崩溃 {self._elapsed(state)}，共拉起 {state.attempts} 次")
        self._finish_if_ready(str(proc.cluster_path), state)

    def poll(self) -> None:
        for key, state in list(self._states.items()):
            if state.phase == "starting":
                self._finish_if_ready(key, state)

    def cancel(self, cluster) -> None:
        """用户手动启停/重启或关闭开关：取消排队中的自动重启，清掉放弃提示。"""
        state = self._states.get(str(cluster.path))
        if state is None or state.phase == "idle":
            return
        if state.phase in ("scheduled", "waiting_token", "starting", "waiting_release"):
            self._log(cluster, "用户手动操作，取消本轮自动重启")
        self._stop_timer(state)
        state.phase = "idle"

    def banner_text(self, cluster) -> str:
        state = self._states.get(str(cluster.path)) if cluster else None
        if state is None:
            return ""
        if state.phase == "scheduled":
            return t("local.auto_restart_scheduled", seconds=max(0, int(state.due - time.time())))
        if state.phase == "waiting_token":
            return t("local.auto_restart_waiting_token", time=datetime.fromtimestamp(state.due).strftime("%H:%M"),
                     minutes=int((time.time() - state.crashed_at) // 60))
        if state.phase == "starting":
            return t("local.auto_restart_starting", attempt=state.attempts)
        if state.phase == "waiting_release":
            minutes = int((time.time() - state.crashed_at) // 60)
            if state.due < state.crashed_at + TOKEN_WAIT_LIMIT:
                return t("local.auto_restart_waiting_release_switch", minutes=minutes,
                         time=datetime.fromtimestamp(state.due).strftime("%H:%M"))
            return t("local.auto_restart_waiting_release", minutes=minutes)
        if state.phase == "gave_up":
            return t("local.auto_restart_gave_up", reason=state.reason)
        return ""

    # ── 流程 ────────────────────────────────────────────────────────────
    def _run(self, key: str) -> None:
        state = self._states.get(key)
        cluster = next((c for c in self._page.ctx.env.clusters if str(c.path) == key), None)
        if state is None or state.phase not in ("scheduled", "waiting_token", "waiting_release"):
            return
        if cluster is None or not get_auto_restart_enabled(key):
            state.phase = "idle"
            return
        if state.phase == "waiting_release":
            self._on_release_deadline(cluster, state)
            return
        state.phase = "starting"
        master = self._page._master_shard(cluster)
        master_running = master is not None and self._running(cluster, master.name)
        if master is not None and not master_running and master.name in state.group:
            state.shards.add(master.name)  # 从世界崩溃时主世界也已经掉了：从世界单独起来连不上，带上主世界
        targets = self._target_shards(cluster, state)
        stop_first = [s for s in targets if self._running(cluster, s.name)]
        self._page._stop_shards_and_then(cluster, stop_first, lambda: self._start(cluster, state, targets))

    def _start(self, cluster, state: _ClusterState, targets) -> None:
        if state.phase != "starting":
            return  # 停服期间被用户取消
        page = self._page
        switch_now = state.switch_pending or self._switch_due_before_start(cluster, state)
        switched = switch_now and page._switch_to_alternative_token(cluster)
        state.switch_pending = False
        if switched:
            state.switched = True
        if not switched and not page._choose_start_token(cluster, allow_switch=False, retry_current=True):
            self._wait_for_token(cluster, state)
            return
        if page._install_dir is None:
            page._detect_install_dir()
        if page._install_dir is None:
            self._give_up(cluster, state, t("local.auto_restart_reason_install"))
            return
        if page._steamcmd_update_running():
            self._give_up(cluster, state, t("local.auto_restart_reason_steamcmd_update"))
            return
        storage_args = resolve_storage_args(page.ctx.env.klei_root)
        if luajit_injector.needs_regeneration(page._install_dir):
            self._give_up(cluster, state, t("local.auto_restart_reason_luajit"))
            return
        keys = {(str(cluster.path), shard.name) for shard in targets}
        page._launching_keys.update(keys)

        def after_accel(ok, detail):
            page._launching_keys.difference_update(keys)
            if state.phase != "starting":
                page._release_token_reservation_if_stopped(cluster.path)
                return
            if not ok:
                self._give_up(cluster, state, t("selfhost.lobby_accel_start_failed", detail=detail))
                return
            state.attempts += 1
            token = read_token(cluster.token_path or (cluster.path / "cluster_token.txt"))
            self._log(cluster, f"第 {state.attempts} 次拉起 {'、'.join(s.name for s in targets)}，"
                               f"令牌 {token_fingerprint(token)[:8] if token else '-'}，距崩溃 {self._elapsed(state)}")
            for shard in targets:
                page._continue_start_shard(cluster, shard, storage_args)
            page._select_master_console_tab(cluster)

        page.ctx.ensure_lobby_accel(cluster, after_accel)

    def _switch_due_before_start(self, cluster, state: _ClusterState) -> bool:
        """拉起前是否该换令牌：开启换令牌、本轮没换过、已过设定分钟数，且原令牌仍在 Klei 释放等待期内。

        原令牌没有等待标记（例如注册前就崩溃，Klei 端没有房间要释放）时照常用原令牌。"""
        return (get_token_switch_on_timeout() and not state.switched
                and time.time() - state.crashed_at >= get_token_switch_after_minutes() * 60
                and self._page._current_token_hold(cluster) is not None)

    def _release_deadline(self, state: _ClusterState) -> float:
        """注册冲突后下一次检查的时刻：还能换令牌时是换令牌的时刻，否则是总等待上限。"""
        switch_at = state.crashed_at + get_token_switch_after_minutes() * 60
        if not state.switched and get_token_switch_on_timeout() and time.time() < switch_at:
            return switch_at
        return state.crashed_at + TOKEN_WAIT_LIMIT

    def _on_release_deadline(self, cluster, state: _ClusterState) -> None:
        """冲突等到检查时刻仍未注册成功：能换令牌就停服换令牌重启，否则停止管理但不关服。"""
        now = time.time()
        if (not state.switched and get_token_switch_on_timeout()
                and now < state.crashed_at + TOKEN_WAIT_LIMIT):
            state.switched = True
            token = self._page._alternative_start_token(cluster)
            if token:
                self._log(cluster, f"距崩溃 {self._elapsed(state)} 仍注册冲突，"
                                   f"换用令牌 {token_fingerprint(token)[:8]} 重启")
                state.phase, state.full, state.switch_pending = "starting", True, True
                # 令牌是整个存档共用的：此刻在运行的世界都要停下换新令牌再拉起，并入重启范围
                state.group |= {s.name for s in cluster.shards if self._running(cluster, s.name)}
                targets = self._target_shards(cluster, state)
                running = [s for s in targets if self._running(cluster, s.name)]
                self._page._stop_shards_and_then(cluster, running, lambda: self._start(cluster, state, targets))
                return
            self._log(cluster, f"距崩溃 {self._elapsed(state)} 仍注册冲突，令牌池没有可换的令牌，继续等原令牌")
            self._schedule(cluster, state, "waiting_release", state.crashed_at + TOKEN_WAIT_LIMIT)
            return
        self._give_up(cluster, state, t("local.auto_restart_reason_release_timeout",
                                        hours=TOKEN_WAIT_LIMIT // 3600))

    def _wait_for_token(self, cluster, state: _ClusterState) -> None:
        """令牌暂时拉不起（正被别的存档使用，或令牌池没有可用令牌）：隔一会儿再查；超过总时长就放弃。"""
        due = time.time() + TOKEN_BUSY_RECHECK
        if due - state.crashed_at > TOKEN_WAIT_LIMIT:
            self._give_up(cluster, state, t("local.auto_restart_reason_token_timeout",
                                             hours=TOKEN_WAIT_LIMIT // 3600))
            return
        self._schedule(cluster, state, "waiting_token", due)
        self._log(cluster, f"暂无可拉起的令牌（可能正被其它存档使用），{datetime.fromtimestamp(due):%H:%M:%S} 再试")

    def _finish_if_ready(self, key: str, state: _ClusterState) -> None:
        cluster = next((c for c in self._page.ctx.env.clusters if str(c.path) == key), None)
        if cluster is None:
            state.phase = "idle"
            return
        names = [s.name for s in self._target_shards(cluster, state)]
        procs = [self._page.manager.get(cluster.path, name) for name in names]
        if not procs or any(proc is None or proc.status not in RUNNING_LIKE or not proc.world_ready for proc in procs):
            return
        master = self._page._master_shard(cluster)
        needs_registration = master is not None and master.name in names \
            and not load_cluster_config(cluster.path).network.get("offline_cluster", False)
        if needs_registration and not state.registered:
            return  # 主世界要等 Klei 注册成功才算恢复，注册冲突可能在世界就绪之后才出现
        state.phase = "idle"
        self._log(cluster, f"自动重启完成，距崩溃 {self._elapsed(state)}")
        self._notify(t("local.auto_restart_tray_done", cluster=cluster.name))

    # ── 工具 ────────────────────────────────────────────────────────────
    def _state(self, cluster) -> _ClusterState:
        return self._states.setdefault(str(cluster.path), _ClusterState())

    @staticmethod
    def _target_shards(cluster, state: _ClusterState) -> list:
        """本轮要重启的世界（按启动顺序）：整组重启是崩溃时在运行的那组，否则只是崩溃的世界。"""
        names = state.group if state.full else state.shards
        return [shard for shard in ordered_shards(cluster) if shard.name in names]

    def _running(self, cluster, shard_name: str) -> bool:
        proc = self._page.manager.get(cluster.path, shard_name)
        return proc is not None and proc.status in RUNNING_LIKE

    def _schedule(self, cluster, state: _ClusterState, phase: str, due: float) -> None:
        self._stop_timer(state)
        state.phase, state.due = phase, due
        key = str(cluster.path)
        timer = QTimer(self, singleShot=True)
        timer.timeout.connect(lambda: self._run(key))
        timer.start(max(0, int((due - time.time()) * 1000)))
        state.timer = timer

    @staticmethod
    def _stop_timer(state: _ClusterState) -> None:
        if state.timer is not None:
            state.timer.stop()
            state.timer.deleteLater()
            state.timer = None

    def _give_up(self, cluster, state: _ClusterState, reason: str) -> None:
        self._stop_timer(state)
        state.phase, state.reason = "gave_up", reason
        self._page._release_token_reservation_if_stopped(cluster.path)
        self._log(cluster, f"放弃自动重启：{reason}")
        self._notify(t("local.auto_restart_tray_gave_up", cluster=cluster.name, reason=reason))

    @staticmethod
    def _elapsed(state: _ClusterState) -> str:
        seconds = int(time.time() - state.crashed_at)
        return f"{seconds // 60} 分 {seconds % 60} 秒"

    def _log(self, cluster, message: str) -> None:
        append_log(self._log_path, cluster.name, message)

    def _notify(self, message: str) -> None:
        tray = getattr(self._page.window(), "tray", None)
        if tray is not None and tray.isVisible():
            tray.showMessage(t("local.auto_restart_tray_title"), message)
