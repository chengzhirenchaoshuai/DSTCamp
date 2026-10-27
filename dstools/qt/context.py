"""应用级状态与信号：环境扫描结果、当前平台筛选、当前选中的存档（不含控件，页面只依赖它而不依赖主窗口）。"""

from pathlib import Path

from PySide6.QtCore import QObject, Signal

from dstools.features.local_service.dedicated_server import ServerManager, ServerStatus
from dstools.features.local_service.steam_backend import SteamBackend, get_backend
from dstools.features.mod.catalog import ModCatalogStore
from dstools.i18n import t
from dstools.models import Account, Cluster, Platform, SaveSource
from dstools.shared.app_settings import (
    get_last_cluster_path, get_last_platform, get_lobby_accel_enabled,
    set_last_cluster_path, set_last_platform, set_selected_account,
)
from dstools.qt.threads import run_async
from dstools.shared.discovery import discover_environment


class AppContext(QObject):
    env_changed = Signal()
    platform_changed = Signal()
    cluster_changed = Signal(object)  # 当前选中的 Cluster，可能是 None
    cluster_config_saved = Signal(object)  # 某个存档的 cluster.ini/server.ini 被保存（本地服务器页据此刷新直连代码）
    tab_requested = Signal(str)            # 页面请求跳到另一个页签（如"去内网穿透页处理端口映射"）
    workshop_mods_changed = Signal()       # 本机 Workshop Mod 有增减（如启动前一键订阅下载完成），Mod 页据此重新扫描
    server_runtime_changed = Signal()      # 开服程序选项或程序位置变了（本地服务器页发出），Mod 页据此按新的 mods/LuaJIT 刷新
    unsaved_changed = Signal()             # 某页"有未保存改动"的状态变了，主窗口据此给页签加标记

    def __init__(self, klei_path: Path | None = None):
        super().__init__()
        self.env = discover_environment(klei_path)
        # 本进程启动的专服子进程集合，本地服务器页和备份恢复等跨页检查共用这一个
        self.manager = ServerManager()
        # 跨页钩子：由对应页面迁移后接管，默认值等价于"没有这个功能在占用"
        self.mapping_owner = lambda cluster, shard: None   # 端口是否被映射接管："sakura"/"lolia"/"selfhost"/None
        self.token_uses = lambda: ()                        # 各存档正在使用的令牌（本地服务器页提供）
        # 大厅加速：内网穿透页迁移后接管这两个钩子。关闭时视为"已就绪"直接放行；
        # 开启但穿透页尚未迁移时明确失败，不能悄悄跳过用户已经打开的功能。
        self.ensure_lobby_accel = lambda cluster, on_done: on_done(
            not get_lobby_accel_enabled(),
            "" if not get_lobby_accel_enabled() else "内网穿透管理器尚未就绪",
        )
        self.stop_lobby_accel_async = lambda: None
        self.poll_lobby_accel = lambda: None
        # 内网穿透页迁移后接管：某个世界停止后顺带停掉它的 frpc 客户端；某个世界启动后
        # 按已有映射顺带拉起 frpc；这个世界的 frpc 是否正在转发（直连代码就绪判断用）。
        self.stop_frpc_for_shard = lambda cluster, shard, on_done: on_done()
        self.maybe_start_frpc = lambda cluster, shard: None
        self.frpc_ready = lambda cluster: False
        # 内网穿透页接管：删除存档（remote=True）或新存档撞上残留记录（remote=False）时关闭这个存档的全部映射，
        # 本地记录立即清掉，返回在后台停 frpc、删远程隧道的函数（结果是未能删除的隧道说明列表）
        self.release_cluster_mappings = lambda cluster, remote: (lambda: [])
        # 跨页共享的已安装 Mod 元数据快照（图标/路径/解析结果，不含每存档的启用状态）。
        self.mod_catalog = ModCatalogStore()
        # Mod 页尚未保存的启用集合预览，供世界设置页即时显示"来自 Mod"的设置；None 表示按磁盘已保存内容
        self.pending_enabled_mod_ids = lambda cluster: None
        # 主窗口接管：离开当前存档（开服、切换存档、退出等）前，有页面未保存时询问保存/不保存/取消；
        # cluster 给出时只在它就是当前选中存档时询问。返回 False 表示用户取消
        self.confirm_unsaved = lambda cluster=None: True
        self._platform = Platform.WEGAME if get_last_platform() == "WeGame" else Platform.STEAM
        self._selected: Cluster | None = None
        self._restore_selection(Path(get_last_cluster_path()) if get_last_cluster_path() else None)

    # ── 平台筛选 ────────────────────────────────────────────────────────
    @property
    def platform(self) -> Platform:
        return self._platform

    def set_platform(self, platform: Platform) -> None:
        if platform == self._platform:
            return
        self._platform = platform
        set_last_platform("WeGame" if platform == Platform.WEGAME else "Steam")
        self.platform_changed.emit()
        self._restore_selection(self._selected.path if self._selected else None)
        self.cluster_changed.emit(self._selected)

    # ── 存档 ────────────────────────────────────────────────────────────
    def steamcmd_only(self) -> bool:
        """Steam 平台且 Steam 通道为 SteamCMD：只管服务器存档，不显示本地存档和游戏账号
        （这种组合一般只装了 SteamCMD 与独立专服，没有游戏客户端）。WeGame 平台不受 Steam 通道影响。"""
        return self._platform == Platform.STEAM and get_backend() is SteamBackend.STEAMCMD

    def steam_backend_changed(self) -> None:
        """Steam 通道切换后：本地存档和游戏账号随之显隐，重新选定存档并广播。"""
        self._restore_selection(self._selected.path if self._selected else None)
        self.env_changed.emit()
        self.cluster_changed.emit(self._selected)

    def clusters(self) -> list[Cluster]:
        """当前平台的服务器存档 + 当前账号的本地存档（其他账号的本地存档切换账号后才显示）。"""
        if self.steamcmd_only():
            return [c for c in self.env.clusters if c.platform == self._platform and c.source == SaveSource.SERVER]
        current = self.current_account()
        current_id = current.id if current else ""
        return [c for c in self.env.clusters if c.platform == self._platform
                and (c.source != SaveSource.LOCAL or c.account_id == current_id)]

    # ── 游戏账号 ────────────────────────────────────────────────────────
    def accounts(self) -> list[Account]:
        return [] if self.steamcmd_only() else self.env.accounts_for(self._platform)

    def current_account(self) -> Account | None:
        return None if self.steamcmd_only() else self.env.current_account(self._platform)

    def select_account(self, account_id: str) -> None:
        """切换当前平台的游戏账号：记住手动选择并重新扫描（本地存档、Mod 配置同步都跟随当前账号）。
        同时记下此刻 Steam 登录的账号，Steam 换账号后这个手动选择失效、改为跟随 Steam。"""
        current = self.current_account()
        if current is not None and current.id == account_id:
            return
        steam_active = self.env.steam_active_account if self._platform == Platform.STEAM else ""
        set_selected_account(self._platform.value, account_id, steam_active)
        self.refresh_env()

    @staticmethod
    def account_text(account: Account) -> str:
        return f"{account.name} ({account.id})" if account.name else account.id

    def selected_cluster(self) -> Cluster | None:
        return self._selected

    def select_cluster(self, cluster: Cluster | None) -> None:
        self._selected = cluster
        if cluster is not None:
            set_last_cluster_path(str(cluster.path))
        self.cluster_changed.emit(cluster)

    def _restore_selection(self, preferred_path: Path | None) -> None:
        """按 path 找回同一个存档（重新扫描后拿到的是新的 Cluster 对象），找不到退回第一项。"""
        clusters = self.clusters()
        matched = next((c for c in clusters if preferred_path and c.path == preferred_path), None)
        self._selected = matched or (clusters[0] if clusters else None)
        if self._selected is not None:
            set_last_cluster_path(str(self._selected.path))

    def refresh_env(self, rediscover_root: bool = False) -> None:
        """重新扫描环境（F5/刷新全部）：保持选中同一个存档，广播 env 与 cluster 变化。
        rediscover_root：手动指定或恢复了存档根目录，重新查找根目录（平时沿用已找到的）。"""
        previous = self._selected.path if self._selected else None
        klei_root = None if rediscover_root else self.env.klei_root
        self.env = discover_environment(klei_root, self.env.wegame_klei_root)
        self._restore_selection(previous)
        self.env_changed.emit()
        self.cluster_changed.emit(self._selected)

    # ── 显示文字 ────────────────────────────────────────────────────────
    def cluster_text(self, cluster: Cluster) -> str:
        """存档下拉文字；有世界正在运行时加"运行中"标注。"""
        tag = t("save.server_clusters") if cluster.source == SaveSource.SERVER else t("save.local_clusters")
        text = f"{cluster.name} [{tag}]"
        if any(proc.cluster_path == cluster.path for proc in self.manager.running()):
            text += t("selector.running_suffix")
        return text

    def clear_stale_mappings(self, cluster_path: Path) -> None:
        """新建存档后调用：同一路径上旧存档（在 DSTCamp 之外删除的）残留的映射记录会让新存档被当成已映射，
        清掉本地记录并停掉 frpc。账号上的旧隧道不删：樱花、Lolia 都按存档名和路径识别隧道，新存档开启映射时直接复用。"""
        cluster = next((c for c in self.env.clusters if c.path == cluster_path), None)
        if cluster is not None:
            run_async(self.release_cluster_mappings(cluster, False), lambda _result: None, lambda _exc: None)

    def goto_tab(self, key: str) -> None:
        self.tab_requested.emit(key)

    def cluster_running(self, cluster: Cluster) -> bool:
        return any(str(proc.cluster_path) == str(cluster.path) for proc in self.manager.running())

    def running_shard_names(self, cluster: Cluster) -> list[str]:
        """这个存档下仍在启动/运行/停止中的世界（世界文件被进程占着时不能覆盖或删除）。"""
        active = (ServerStatus.STARTING, ServerStatus.RUNNING, ServerStatus.STOPPING)
        return [shard.name for shard in cluster.shards
                if (proc := self.manager.get(cluster.path, shard.name)) and proc.status in active]

    def status_text(self) -> str:
        """状态栏文字：跟随平台筛选，WeGame 与 Steam 各用各的根目录/用户 ID。"""
        klei_root = self.env.klei_root_for(self._platform)
        account = self.current_account()
        user_id = self.account_text(account) if account else ""
        clusters = self.clusters()
        servers = sum(1 for c in clusters if c.source == SaveSource.SERVER)
        local = sum(1 for c in clusters if c.source == SaveSource.LOCAL)
        klei = str(klei_root) if klei_root else t("env.not_found")
        if self.steamcmd_only():
            # 没有游戏客户端：不显示游戏账号和本地存档数
            return f"{t('status.klei')}: {klei}  |  {t('status.clusters')}: {servers}"
        return (f"{t('status.klei')}: {klei}  |  {t('status.user')}: {user_id or '?'}"
                f"  |  {t('status.clusters')}: {servers}  |  {t('status.local_saves')}: {local}")
