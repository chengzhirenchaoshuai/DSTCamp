"""启动后台预热 Mod 缓存的界面侧入口（业务逻辑见 features/mod/preload.py）。

单独开一个守护线程，不占 run_async 的全局线程池；Mod 页开始自己加载时先 take_snapshot() 取走完整预热的
扫描快照（取不到就自己扫），再调用 cancel() 停掉还没做完的预热，避免两边同时起沙箱子进程。不弹窗、不刷新界面。
"""

import logging
import threading

from dstools.models import Platform

_logger = logging.getLogger(__name__)
_stop = threading.Event()
_started = False
_snapshot = None
_snapshot_lock = threading.Lock()


def start(ctx) -> None:
    """只在本次运行第一次调用时生效；目录参数在界面线程按 Mod 页同样的规则取好再交给后台。"""
    global _started
    if _started or _stop.is_set():
        return
    _started = True
    from dstools.features.local_service.server_runtime import current_runtime
    from dstools.features.mod.parser import resolve_wegame_client_mods_dir

    cluster = ctx.selected_cluster()
    platform = cluster.platform if cluster else Platform.STEAM
    wegame_client_mods_dir = resolve_wegame_client_mods_dir(platform)
    steam_runtime_mods_dir = None
    if platform == Platform.STEAM:
        runtime = current_runtime()
        steam_runtime_mods_dir = runtime.mods_dir if runtime else None

    def work() -> None:
        global _snapshot
        from dstools.features.mod.preload import warm_mod_caches

        try:
            stats = warm_mod_caches(platform, wegame_client_mods_dir, steam_runtime_mods_dir, _stop)
        except Exception:
            _logger.exception("Mod 缓存预热失败")
            return
        with _snapshot_lock:
            if not _stop.is_set():
                _snapshot = stats.snapshot
        _logger.info("Mod 缓存预热%s：%d 个 Mod，图标 %d，版本 %d", "中止" if stats.cancelled else "完成",
                     stats.mods, stats.icons, stats.versions)

    threading.Thread(target=work, name="mod-preload", daemon=True).start()


def cancel() -> None:
    """Mod 页接手加载；尚未开始的预热也不再启动。"""
    _stop.set()


def take_snapshot(source: tuple):
    """取走与 source（平台、WeGame 客户端 mods 目录、专服 mods 目录）一致的预热快照，只给一次；不一致返回 None。"""
    global _snapshot
    with _snapshot_lock:
        snapshot = _snapshot
        if snapshot is None or snapshot.source != source:
            return None
        _snapshot = None
        return snapshot
