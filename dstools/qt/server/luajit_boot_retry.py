"""专服 LuaJIT 注入器启动即崩后的自动清理重试（诊断见 server_diagnostics 的 luajit_boot_crash）：

1. 等同一存档里还在启动的世界都结束（多个世界通常几秒内相继崩溃），一次收齐；
2. 后台强制清一次 LuaJIT 运行时缓存，成功就用原令牌拉起崩溃的世界；缓存被游戏或其它服务器占用时不重试；
3. 每个存档清理重试一次，主世界注册成功后才恢复下一次机会；再崩只留控制台横幅的处理建议。
不弹模态框，结果用轻提示告知。
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QTimer

from dstools.features.local_service import luajit_injector
from dstools.features.local_service.dedicated_server import ServerStatus, resolve_storage_args
from dstools.features.local_service.shard_helpers import RUNNING_LIKE, ordered_shards
from dstools.i18n import t
from dstools.qt import dialogs
from dstools.qt.threads import run_async

_SETTLE_MS = 3000        # 每隔多久看一次同存档的世界是否都已结束启动
_MAX_SETTLE_CHECKS = 20  # 最多等约一分钟，仍有世界在启动就放弃本次重试


class LuajitBootRetry(QObject):
    def __init__(self, page):
        super().__init__(page)
        self._page = page
        self._pending: dict[str, set[str]] = {}   # 存档路径 -> 待拉起的崩溃世界
        self._checks: dict[str, int] = {}
        self._used: set[str] = set()              # 已清理重试过、尚未注册成功的存档

    def on_failure(self, proc, report) -> None:
        if report.category != "luajit_boot_crash":
            return
        key = str(proc.cluster_path)
        if key in self._used:
            return
        first = key not in self._pending
        self._pending.setdefault(key, set()).add(proc.shard_name)
        if first:
            self._checks[key] = 0
            QTimer.singleShot(_SETTLE_MS, self, lambda: self._settle(key))

    def on_registered(self, proc) -> None:
        self._used.discard(str(proc.cluster_path))

    def _cluster(self, key: str):
        return next((c for c in self._page.ctx.env.clusters if str(c.path) == key), None)

    def _settle(self, key: str) -> None:
        cluster = self._cluster(key)
        if cluster is None or self._page._install_dir is None:
            self._drop(key)
            return
        starting = any(
            (proc := self._page.manager.get(cluster.path, shard.name)) is not None
            and proc.status == ServerStatus.STARTING
            for shard in cluster.shards
        )
        if starting:
            self._checks[key] += 1
            if self._checks[key] >= _MAX_SETTLE_CHECKS:
                self._drop(key)
                return
            QTimer.singleShot(_SETTLE_MS, self, lambda: self._settle(key))
            return
        install_dir = self._page._install_dir
        run_async(lambda: luajit_injector.reset_after_boot_crash(install_dir),
                  lambda ok: self._after_reset(key, ok), lambda _exc: self._after_reset(key, False))

    def _after_reset(self, key: str, ok: bool) -> None:
        names = self._drop(key)
        cluster = self._cluster(key)
        if cluster is None:
            return
        window = self._page.window()
        if not ok:
            dialogs.show_toast(window, t("local.luajit_boot_retry_skipped"), ms=4000)
            return
        self._used.add(key)
        page = self._page
        # 注册前就崩溃，Klei 端没有房间要释放，沿用原令牌
        if not page._choose_start_token(cluster, allow_switch=False, retry_current=True):
            dialogs.show_toast(window, t("local.luajit_boot_retry_no_token"), ms=4000)
            return
        targets = [shard for shard in ordered_shards(cluster) if shard.name in names and not self._running(cluster, shard)]
        if not targets:
            return
        dialogs.show_toast(window, t("local.luajit_boot_retry_started"), ms=4000)
        storage_args = resolve_storage_args(page.ctx.env.klei_root)
        keys = {(key, shard.name) for shard in targets}
        page._launching_keys.update(keys)

        def after_accel(accel_ok, detail):
            page._launching_keys.difference_update(keys)
            if not accel_ok:
                page._release_token_reservation_if_stopped(cluster.path)
                dialogs.show_toast(window, t("selfhost.lobby_accel_start_failed", detail=detail), ms=4000)
                return
            # 等加速期间用户可能已手动开服
            for shard in targets:
                if not self._running(cluster, shard):
                    page._continue_start_shard(cluster, shard, storage_args)
            page._select_master_console_tab(cluster)

        page.ctx.ensure_lobby_accel(cluster, after_accel)

    def _running(self, cluster, shard) -> bool:
        proc = self._page.manager.get(cluster.path, shard.name)
        return proc is not None and proc.status in RUNNING_LIKE

    def _drop(self, key: str) -> set[str]:
        self._checks.pop(key, None)
        return self._pending.pop(key, set())
