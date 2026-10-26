"""Mod 管理页。

列表使用 qt/mod_panel.py 的自绘面板；配置编辑、配置集、推荐订阅、Workshop 更新拆在同目录独立模块。
本地存档（Steam）的 Mod 状态以 Master/save/shardindex 为准，游戏关闭时可编辑，保存时同步写各世界 modoverrides.lua；
WeGame 本地存档与未选存档时整页只读。
"""

import copy
import os
import threading
import time
import webbrowser
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout,
)

from dstools.features.local_service import luajit_injector
from dstools.features.local_service.dedicated_server import detect_external_shard_processes, find_bin64_dir
from dstools.features.mod import chs_translation
from dstools.features.mod.cache import load_cached_result, save_result
from dstools.features.mod.config_memory import recall, recall_for, remember
from dstools.features.mod.icons import get_cached_mod_icon_path, get_mod_icon_path, load_mod_icon_image
from dstools.features.mod.legacy_v1 import find_legacy_packages
from dstools.features.mod.list_model import (
    build_mod_rows, localize_mod_name, merge_visible_mod_ids, mod_name_cmp, referenced_missing_status_text,
    sort_mod_data,
)
from dstools.features.mod.local_version import apply_local_version, resolve_local_version_target
from dstools.features.mod.locations import resolve_mod_open_location
from dstools.features.mod.manager import (
    CLIENT_MODS_DISABLED_KEY, enable_mod, load_mod_overrides, save_mod_overrides, sync_mods,
)
from dstools.features.mod.modinfo_cache import parse_modinfo_cached
from dstools.features.mod.parser import (
    find_game_mods_dir, find_wegame_client_dir, find_wegame_server_dir,
    is_dedicated_server_mods_dir, list_installed_mod_ids, resolve_full_modinfo,
    resolve_wegame_client_mods_dir,
)
from dstools.features.mod.preload import modinfo_stamp, resolve_readable_mod_folder
from dstools.features.mod.sandbox_apply import apply_full_sandbox_result
from dstools.features.mod.shardindex import ShardIndexError, load_shardindex_mods, save_shardindex_mods, shardindex_path
from dstools.features.mod.sync import apply_mod_sync, detach_mod_sync_junction, get_enabled_mod_ids, plan_mod_sync
from dstools.features.world.location_profiles import IA_CORE_MOD_ID, IA_SHIPWRECKED_MOD_ID, find_mod_key
from dstools.i18n import t
from dstools.models import ModEntry, ModOverrides, Platform, SaveSource
from dstools.qt import dialogs
from dstools.qt.mod_config_dialog import open_mod_config
from dstools.qt.mod_config_sync import start_sync as start_config_sync
from dstools.qt.mod_panel import ModListPanel
from dstools.qt.mod_preload import cancel as cancel_mod_preload
from dstools.qt.mod_preload import take_snapshot as take_mod_preload_snapshot
from dstools.qt.mod_defaults_dialog import GlobalDefaultsDialog
from dstools.qt.mod_presets_dialogs import ApplyPresetDialog, SavePresetDialog
from dstools.qt.mod_recommend_dialog import RecommendModsDialog
from dstools.qt.pages.base import Page
from dstools.qt.theme import theme
from dstools.qt.threads import post_to_ui, run_async
from dstools.qt.widgets import Banner, Card, PillTabBar
from dstools.shared import app_settings, tex_convert
from dstools.shared.resource_paths import bundled_resource_dir

_FILTER_DEBOUNCE_MS = 150
# 工坊更新状态的复用有效期（秒）；更新、清理等操作后会立即作废
_WORKSHOP_STATUS_TTL = 300.0
_CLIENT_MODS_ICON_PATH = bundled_resource_dir() / "icons" / "ui" / "client_mods_disabled.png"
_client_mods_icon = None


def _client_mods_disabled_icon():
    """"禁用客户端模组"行的图标（PIL 图像，首次用到时读取）；读不到时返回 None，面板画默认图标。"""
    global _client_mods_icon
    if _client_mods_icon is None:
        from PIL import Image
        try:
            with Image.open(_CLIENT_MODS_ICON_PATH) as image:
                _client_mods_icon = image.convert("RGBA")
        except OSError:
            _client_mods_icon = False
    return _client_mods_icon or None


def _expand_workshop_candidate_ids(list_ids, cluster_ids) -> list[int]:
    """在列表与存档引用的 Mod 之外补上 V1 包和残留目录（要扫磁盘，可在后台线程调用）。"""
    from dstools.features.mod.legacy_v1 import find_legacy_packages, find_legacy_runtime_residual_dirs
    from dstools.features.mod.parser import find_workshop_residual_dirs
    ids = [*list_ids, *find_legacy_packages(), *cluster_ids, *find_workshop_residual_dirs(),
           *find_legacy_runtime_residual_dirs()]
    return list(dict.fromkeys(item for item in ids if item > 0))


class ModPage(Page):
    def __init__(self, ctx):
        super().__init__(ctx)
        self._mod_data: dict[str, ModEntry] = {}
        self._mod_infos: dict = {}
        self._mod_paths: dict[str, Path] = {}
        self._icon_imgs: dict = {}
        self._full_resolved_cache: dict = {}
        self._mod_stamps: dict = {}  # 上次加载时各 Mod 的 modinfo 指纹，变了就丢掉内存里的完整解析结果
        self._luajit_mod_locked = False
        self._dirty = False
        self._loading = False
        self._loading_key = None
        self._loading_full = False
        self._mods_loaded = False
        self._refresh_gen = 0
        self._sync_check_gen = 0
        self._filter_timer = QTimer(self, singleShot=True, interval=_FILTER_DEBOUNCE_MS)
        self._filter_timer.timeout.connect(self._render_list)
        self._current_shard_name = ""
        self._global_defaults_dialog = None
        # 列表里"禁用客户端模组"开关行：当前世界 modoverrides.lua 的状态（含未保存改动），随"保存"写入；
        # 排序位置按加载时的状态定，跟其它 Mod 一样切换开关不重排
        self._client_mods_disabled = False
        self._client_mods_sort_enabled = False
        self._edited_cluster_path = ""  # 当前列表内容来自哪个存档，用于判断重新加载时能否保留未保存改动
        # 加载排序时已启用的 Mod：开关行按它们定位，不受之后切换的影响
        self._sort_enabled_ids: frozenset = frozenset()
        # 与磁盘一致时的勾选和配置（加载、保存后记下），"取消修改"据此原地恢复，不重新扫描
        self._saved_mod_state: dict = {}
        self._saved_client_mods_disabled = False

        # 更新状态（供 Workshop 更新弹窗共享，避免重复扫描）
        self._workshop_update_running = False
        self._workshop_status_cache: dict = {}
        self._workshop_title_cache: dict[str, str] = {}
        self._workshop_status_checked_at = 0.0
        self._workshop_status_refreshing = False
        self._workshop_status_error = ""
        # 上次查更新状态时扫描到的候选（含 V1 包、残留目录）及当时的检查时刻，状态有效期内复用
        self._workshop_scan_ids: tuple[float, list[int]] = (0.0, [])
        # SteamCMD「更新 Mod」窗口上次查完的列表：再次打开先显示它，后台照常复查后原地更新
        self.steamcmd_update_rows_snapshot: list = []
        self._steamcmd_pending_count: int | None = None  # SteamCMD 通道需要下载的 Mod 数；其它通道为 None
        self._workshop_log_dialog = None

        # 与其他主页签相同的外壳：主题色圆角边框、内部透明
        page_layout = QVBoxLayout(self)
        page_layout.setContentsMargins(24, 12, 24, 12)
        card = Card(radius=15, alpha=0, border=True)
        page_layout.addWidget(card)
        root = QVBoxLayout(card)
        # 跟其它主页签统一的内边距。
        root.setContentsMargins(15, 13, 15, 13)

        location_row = QHBoxLayout()
        location_row.addWidget(QLabel(t("mod.location_label")))
        self._location_label = QLabel()
        self._location_label.setProperty("muted", True)
        location_row.addWidget(self._location_label, 1)
        self._location_change_btn = QPushButton(t("local.install_change_btn"))
        self._location_change_btn.clicked.connect(self._change_mod_location)
        location_row.addWidget(self._location_change_btn)
        self._sync_btn = QPushButton(t("local.sync_mods_btn"))
        self._sync_btn.clicked.connect(self._sync_mods_to_server)
        location_row.addWidget(self._sync_btn)
        root.addLayout(location_row)

        tool_row = QHBoxLayout()
        tool_row.addWidget(QLabel(t("mod.shard")))
        self._shard_combo = QComboBox()
        self._shard_combo.setMinimumWidth(160)
        self._shard_combo.activated.connect(self._on_shard_select)
        tool_row.addWidget(self._shard_combo)
        self._global_defaults_btn = QPushButton(t("mod.global_defaults_btn"))
        self._global_defaults_btn.clicked.connect(self._open_global_defaults)
        tool_row.addWidget(self._global_defaults_btn)
        tool_row.addStretch()
        root.addLayout(tool_row)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel(t("mod.filter")))
        self._filter_edit = QLineEdit()
        self._filter_edit.setFixedWidth(200)
        self._filter_edit.textChanged.connect(lambda _t: self._filter_timer.start())
        filter_row.addWidget(self._filter_edit)
        self._filter_tabs = PillTabBar(
            [t("mod.show_all"), t("mod.show_enabled"), t("mod.show_disabled"), t("mod.show_custom"),
             t("mod.show_client")],
            # 不统一宽度："客户端模组"较长，统一后其余短标签两侧空白过宽
            height=32, pill_height=24, font_size_key="FONT_SIZE_SM", gap=2, pad=16)
        self._filter_tabs.current_changed.connect(lambda _i: self._render_list())
        filter_row.addWidget(self._filter_tabs)
        self._recommend_btn = QPushButton(t("mod.recommend_btn"))
        self._recommend_btn.clicked.connect(self._open_recommend_mods)
        filter_row.addWidget(self._recommend_btn)
        self._download_by_id_btn = QPushButton(t("mod.download_by_id_btn"))
        self._download_by_id_btn.clicked.connect(self._open_download_by_id)
        filter_row.addWidget(self._download_by_id_btn)
        filter_row.addStretch()
        # 列表列数切换（1/2/3 列），选择记在设置里，下次打开沿用。
        self._column_choices = app_settings.MOD_LIST_COLUMN_CHOICES
        self._columns_tabs = PillTabBar(
            [t("mod.columns_option", count=n) for n in self._column_choices],
            height=32, pill_height=24, font_size_key="FONT_SIZE_SM", gap=2, pad=12, uniform_width=True)
        self._columns_tabs.setToolTip(t("mod.columns_hint"))
        columns = app_settings.get_mod_list_columns()
        self._columns_tabs.set_current_index(self._column_choices.index(columns))
        self._columns_tabs.current_changed.connect(self._on_columns_changed)
        filter_row.addWidget(self._columns_tabs)
        root.addLayout(filter_row)

        status_row = QHBoxLayout()
        self._enabled_count_label = QLabel(t("mod.enabled_count", count=0))
        status_row.addWidget(self._enabled_count_label)
        status_row.addSpacing(10)
        self._export_image_btn = QPushButton(t("mod.export_image_btn"))
        self._export_image_btn.clicked.connect(self._export_mod_list_image)
        status_row.addWidget(self._export_image_btn)
        status_row.addSpacing(10)
        self._unsaved_label = QLabel(t("dlg.unsaved_hint"))
        self._unsaved_label.setProperty("accent", True)
        self._unsaved_label.setVisible(False)
        status_row.addWidget(self._unsaved_label)
        status_row.addStretch()
        self._scan_status_label = QLabel("")
        self._scan_status_label.setProperty("muted", True)
        status_row.addWidget(self._scan_status_label)
        self._reload_full_btn = QPushButton(t("mod.reload_full"))
        self._reload_full_btn.clicked.connect(self._reload_full)
        status_row.addWidget(self._reload_full_btn)
        root.addLayout(status_row)

        self._local_banner = Banner()
        root.addWidget(self._local_banner)
        self._wegame_banner = Banner()
        root.addWidget(self._wegame_banner)
        self._runtime_banner = Banner()
        self._runtime_banner.set_clickable(True)
        self._runtime_banner.clicked.connect(lambda: self.window().install_vcredist())
        root.addWidget(self._runtime_banner)

        self._list_panel = ModListPanel()
        self._list_panel.toggle_requested.connect(self._on_toggle)
        self._list_panel.config_requested.connect(self._on_config)
        self._list_panel.link_requested.connect(self._on_link)
        self._list_panel.folder_requested.connect(self._on_open_mod_folder)
        self._list_panel.copy_id_requested.connect(self._on_copy_id)
        self._list_panel.copy_name_requested.connect(self._on_copy_name)
        self._list_panel.set_column_count(columns)
        root.addWidget(self._list_panel, 1)

        # 左、中、右三栏，左右两栏拉伸系数相同、等宽：右侧更新提示文字出现或变长时，中间的保存按钮不被挤偏
        bottom_row = QHBoxLayout()
        left_group, center_group, right_group = QHBoxLayout(), QHBoxLayout(), QHBoxLayout()
        bottom_row.addLayout(left_group, 1)
        bottom_row.addLayout(center_group)
        bottom_row.addLayout(right_group, 1)
        self._preset_save_btn = QPushButton(t("mod.preset_save_btn"))
        self._preset_save_btn.clicked.connect(self._save_as_preset)
        left_group.addWidget(self._preset_save_btn)
        self._preset_apply_btn = QPushButton(t("mod.preset_apply_btn"))
        self._preset_apply_btn.clicked.connect(self._apply_preset_dialog)
        left_group.addWidget(self._preset_apply_btn)
        left_group.addStretch()
        self._discard_btn = QPushButton(t("dlg.discard_changes_btn"))
        self._discard_btn.setEnabled(False)
        self._discard_btn.clicked.connect(self._discard_changes)
        center_group.addWidget(self._discard_btn)
        self._save_btn = QPushButton(t("mod.save_btn"))
        self._save_btn.setEnabled(False)
        self._save_btn.clicked.connect(lambda: self._save_mods())
        center_group.addWidget(self._save_btn)
        self._apply_current_btn = QPushButton(t("mod.apply_current"))
        self._apply_current_btn.setEnabled(False)
        self._apply_current_btn.clicked.connect(self._apply_current_shard)
        center_group.addWidget(self._apply_current_btn)
        right_group.addStretch()
        self._update_hint_label = QPushButton("")
        self._update_hint_label.setProperty("flat", True)
        self._update_hint_label.setVisible(False)
        self._update_hint_label.clicked.connect(self._on_mod_update_hint_click)
        right_group.addWidget(self._update_hint_label)
        # SteamCMD 通道才有：它的工坊目录只增不减，Steam 客户端那边靠取消订阅
        self._steamcmd_cleanup_btn = QPushButton(t("mod.steamcmd_cleanup_btn"))
        self._steamcmd_cleanup_btn.clicked.connect(self._open_steamcmd_cleanup_dialog)
        right_group.addWidget(self._steamcmd_cleanup_btn)
        self._workshop_update_btn = QPushButton(t("mod.workshop_update_btn"))
        self._workshop_update_btn.setEnabled(False)
        self._workshop_update_btn.clicked.connect(self._open_workshop_update_dialog)
        right_group.addWidget(self._workshop_update_btn)
        # 跟本地服务器页"全部启动/全部停止/..."一排操作按钮统一字号（方角已经是
        # 全局默认样式，这里只需要再调小字号）。
        for button in (self._preset_save_btn, self._preset_apply_btn, self._discard_btn, self._save_btn,
                      self._apply_current_btn, self._steamcmd_cleanup_btn, self._workshop_update_btn):
            button.setFont(theme.font("FONT_SIZE_SM"))
        root.addLayout(bottom_row)
        self._refresh_steamcmd_cleanup_btn()

        ctx.pending_enabled_mod_ids = self.get_pending_enabled_mod_ids
        ctx.workshop_mods_changed.connect(self._on_workshop_mods_changed)
        ctx.server_runtime_changed.connect(self._on_server_runtime_changed)

    def _on_workshop_mods_changed(self) -> None:
        """别处（如启动前一键订阅）下载了 Mod：作废更新状态缓存，并在切回本页时重新扫描。
        有未保存的勾选改动时不整页重载（会从磁盘重读 modoverrides 冲掉改动），只作废缓存。"""
        self._workshop_status_cache.clear()
        self._workshop_status_checked_at = 0.0
        if self._dirty:
            return
        if self.isVisible():
            self.load()
        else:
            self.stale = True

    def _on_server_runtime_changed(self) -> None:
        """开服程序选项/程序位置变了：服务器读取的 mods 和 LuaJIT 副本跟着换，按新目录重新扫描。"""
        self._update_mod_location_display()
        self.refresh_sync_button_state()
        self._refresh_steamcmd_cleanup_btn()
        self._on_workshop_mods_changed()

    # ── Cluster/世界选择 ────────────────────────────────────────────────
    def get_cluster(self):
        return self.ctx.selected_cluster()

    @staticmethod
    def _local_shardindex(cluster) -> Path | None:
        """可编辑的本地存档返回其 Master shardindex；只支持 Steam（WeGame 客户端进程名未核实，无法确认游戏已关闭）。"""
        if not cluster or cluster.source != SaveSource.LOCAL or cluster.platform != Platform.STEAM:
            return None
        path = shardindex_path(cluster.path)
        return path if path.is_file() else None

    def _editable(self, cluster) -> bool:
        return bool(cluster and (cluster.source == SaveSource.SERVER or self._local_shardindex(cluster)))

    def on_cluster_changed(self, cluster) -> None:
        c = cluster if cluster is not None else self.get_cluster()
        is_server = bool(c and c.source == SaveSource.SERVER)
        self._refresh_steamcmd_cleanup_btn()  # 切换 Steam/WeGame 平台也走这里，按钮显隐随平台变化
        self._mods_loaded = False
        self._update_hint_label.setVisible(False)
        self._refresh_workshop_update_button_state()
        self.refresh_sync_button_state()
        save_state = self._editable(c) and self._dirty
        self._save_btn.setEnabled(save_state)
        self._discard_btn.setEnabled(save_state)
        self._apply_current_btn.setEnabled(save_state and is_server)
        preset_state = is_server
        self._preset_save_btn.setEnabled(preset_state)
        self._preset_apply_btn.setEnabled(preset_state)
        self._export_image_btn.setEnabled(preset_state)
        if is_server:
            self._local_banner.set_text("")
        elif self._local_shardindex(c):
            self._local_banner.set_text(t("mod.local_shardindex_banner"))
        else:
            self._local_banner.set_text(t("mod.no_save_banner") if c is None else t("mod.local_view_only_banner"))
        self._wegame_banner.set_text(t("mod.wegame_root_needed_banner") if self._wegame_root_missing(c) else "")
        self._refresh_runtime_banner()
        self._update_mod_location_display()
        if (self._dirty and c is not None and str(c.path) == self._edited_cluster_path
                and any(s.name == self._current_shard_name for s in c.shards)):
            # 还是同一个存档（别处刷新了存档列表、切回本页等）：保留未保存的改动，不从磁盘重读
            self._mods_loaded = True
            self._refresh_workshop_update_button_state()
            self._render_list()
            return
        self._shard_combo.blockSignals(True)
        self._shard_combo.clear()
        if not c:
            self._shard_combo.blockSignals(False)
            self._current_shard_name = ""
            self._refresh_mods()
            return
        names = [s.name for s in c.shards]
        self._shard_combo.addItems(names)
        if names:
            self._current_shard_name = "Master" if "Master" in names else names[0]
            self._shard_combo.setCurrentText(self._current_shard_name)
        else:
            self._current_shard_name = ""
        self._shard_combo.blockSignals(False)
        self._refresh_mods()

    def load(self) -> None:
        super().load()
        self._refresh_steamcmd_cleanup_btn()
        self._start_config_sync(notify=True)

    def _wegame_root_missing(self, cluster) -> bool:
        if not cluster or cluster.platform != Platform.WEGAME:
            return False
        root = app_settings.get_wegame_root_path()
        return root is None or find_wegame_client_dir(root) is None

    def _on_shard_select(self, _index: int = 0) -> None:
        name = self._shard_combo.currentText()
        if self._dirty:
            if name == self._current_shard_name:
                return
            choice = dialogs.ask_unsaved(self.window(), t("tab.mods").strip())
            if choice == "cancel" or (choice == "save" and not self.save_unsaved_changes()):
                self._shard_combo.setCurrentText(self._current_shard_name)
                return
        self._current_shard_name = name
        self._refresh_mods()

    def _on_columns_changed(self, index: int) -> None:
        columns = self._column_choices[index]
        app_settings.set_mod_list_columns(columns)
        self._list_panel.set_column_count(columns)

    def _reload_full(self) -> None:
        self._refresh_mods(full=True)

    def refresh(self) -> None:
        self.on_cluster_changed(self.get_cluster())

    def retranslate(self) -> None:
        self.on_cluster_changed(self.get_cluster())

    # ── Mod 位置 / 同步到服务器 ──────────────────────────────────────────
    def _resolve_mod_folder_args(self, cluster):
        platform = cluster.platform if cluster else Platform.STEAM
        return platform, resolve_wegame_client_mods_dir(platform)

    def _detect_mod_location(self, platform):
        if platform == Platform.WEGAME:
            root = app_settings.get_wegame_root_path()
            if root:
                client_dir = find_wegame_client_dir(root)
                if client_dir:
                    return client_dir / "mods"
            return None
        return self._server_mods_root()

    def _update_mod_location_display(self) -> None:
        found = self._detect_mod_location(self.ctx.platform)
        self._location_label.setText(str(found) if found else t("mod.location_not_found"))

    def _change_mod_location(self) -> None:
        if self.ctx.platform == Platform.WEGAME:
            self._pick_wegame_root_and_reload()
            return
        picked = QFileDialog.getExistingDirectory(self.window(), t("mod.location_picker_title"))
        if not picked:
            return
        picked_path = Path(picked)
        if is_dedicated_server_mods_dir(picked_path):
            dialogs.show_warning(self.window(), t("mod.location_label"), t("mod.location_server_mods_invalid"))
            return
        app_settings.set_steam_mods_path(picked_path)
        self._update_mod_location_display()
        self.refresh_sync_button_state()
        self._refresh_mods(full=True)

    def _pick_wegame_root_and_reload(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self.window(), t("local.wegame_root_picker_title"))
        if not chosen:
            return
        root = Path(chosen)
        if find_wegame_client_dir(root) is None or find_wegame_server_dir(root) is None:
            dialogs.show_warning(self.window(), t("local.sync_mods_btn"), t("local.wegame_root_picker_invalid"))
            return
        app_settings.set_wegame_root_path(root)
        self.on_cluster_changed(self.get_cluster())

    def _server_running_for(self, cluster) -> bool:
        if not cluster:
            return False
        if any(p.cluster_path == cluster.path for p in self.ctx.manager.running()):
            return True
        return self._external_server_running(cluster)

    @staticmethod
    def _external_server_running(cluster) -> bool:
        """本程序之外启动的专服进程；要起子进程查进程和端口（约 0.4 秒），界面刷新时放后台调用。"""
        try:
            external = detect_external_shard_processes(cluster)
            return any(info.get("running") for info in external.values())
        except (OSError, ValueError, KeyError):
            return False

    @staticmethod
    def _runtime():
        """当前开服程序（server_runtime 唯一来源），跟本地服务器页实际启动用的是同一个。"""
        from dstools.features.local_service.server_runtime import current_runtime
        return current_runtime()

    def _server_mods_root(self) -> Path | None:
        runtime = self._runtime()
        return runtime.mods_dir if runtime else None

    def _local_install_dir(self):
        runtime = self._runtime()
        return runtime.install_dir if runtime else None

    def _sync_install_dir(self):
        """Mod 同步（专服 mods 联接到客户端 mods）只对独立专服有意义；游戏客户端开服直接读客户端 mods。"""
        runtime = self._runtime()
        return runtime.install_dir if runtime and not runtime.is_client else None


    def _passive_sync_dirs(self, cluster):
        if not cluster or cluster.source != SaveSource.SERVER:
            return None, None
        if cluster.platform == Platform.WEGAME:
            root = app_settings.get_wegame_root_path()
            if not root:
                return None, None
            server_dir, client_dir = find_wegame_server_dir(root), find_wegame_client_dir(root)
            if server_dir is None or client_dir is None:
                return None, None
            return server_dir, client_dir / "mods"
        install_dir = self._sync_install_dir()
        if install_dir is None:
            return None, None
        return install_dir, find_game_mods_dir()

    def refresh_sync_button_state(self) -> None:
        c = self.get_cluster()
        is_server = bool(c and c.source == SaveSource.SERVER)
        install_dir, client_mods_dir = self._passive_sync_dirs(c)
        target_is_junction = bool(install_dir and os.path.isjunction(Path(install_dir) / "mods"))
        self._sync_already_linked = bool(
            target_is_junction or (install_dir and client_mods_dir and plan_mod_sync(install_dir, client_mods_dir).already_linked))
        can_create = bool(client_mods_dir and client_mods_dir.is_dir())
        runtime = self._runtime() if c and c.platform != Platform.WEGAME else None
        client_runtime = bool(runtime and runtime.is_client)
        self._sync_btn.setToolTip(t("mod.sync_client_runtime_hint") if client_runtime else "")
        self._sync_btn.setText(t("local.remove_junction_btn") if self._sync_already_linked else t("local.sync_mods_btn"))
        self._sync_check_gen += 1
        gen = self._sync_check_gen
        self._sync_btn.setEnabled(False)
        if not (is_server and can_create and not client_runtime):
            return
        if any(p.cluster_path == c.path for p in self.ctx.manager.running()):
            return

        # 外部专服进程检测放后台，切到本页不再卡界面；查完之前按钮保持禁用，不会在专服运行时误开
        def done(running: bool) -> None:
            if gen == self._sync_check_gen:
                self._sync_btn.setEnabled(not running)

        run_async(lambda: self._external_server_running(c), done, lambda _exc: None)

    def _sync_mods_to_server(self) -> None:
        c = self.get_cluster()
        if not c or c.source != SaveSource.SERVER:
            dialogs.show_warning(self.window(), t("local.sync_mods_btn"), t("local.select_cluster_first"))
            return
        if self._server_running_for(c):
            dialogs.show_warning(self.window(), t("local.sync_mods_btn"), t("local.sync_running_hover"))
            self.refresh_sync_button_state()
            return
        if self._sync_already_linked:
            self._remove_mod_sync_junction(c)
            return
        if not get_enabled_mod_ids(c):
            dialogs.show_info(self.window(), t("local.sync_mods_btn"), t("local.sync_no_mods"))
            return
        if c.platform == Platform.WEGAME:
            install_dir, client_mods_dir = self._passive_sync_dirs(c)
            if install_dir is None:
                dialogs.show_warning(self.window(), t("local.sync_mods_btn"), t("local.wegame_root_picker_prompt"))
                return
        else:
            install_dir = self._sync_install_dir()
            if install_dir is None:
                dialogs.show_warning(self.window(), t("local.sync_mods_btn"), t("mod.sync_client_runtime_hint"))
                return
            client_mods_dir = find_game_mods_dir()

        plan = plan_mod_sync(install_dir, client_mods_dir)
        if plan.client_mods_dir is None:
            dialogs.show_warning(self.window(), t("local.sync_mods_btn"), t("local.sync_no_client_mods_dir"))
            return
        if plan.invalid_reason:
            dialogs.show_warning(self.window(), t("local.sync_mods_btn"), plan.invalid_reason)
            return
        if plan.needs_confirm_delete:
            if plan.lost_on_replace:
                detail = t("local.sync_replace_lost_detail", items="、".join(plan.lost_on_replace))
            elif plan.target_kind == "file":
                detail = t("local.sync_replace_file_detail")
            elif plan.target_kind in {"junction", "link"}:
                detail = t("local.sync_replace_link_detail")
            else:
                detail = t("local.sync_replace_nothing_lost")
            if not dialogs.ask_yes_no(self.window(), t("local.sync_mods_btn"),
                                       t("local.sync_replace_confirm_msg", path=str(Path(install_dir) / "mods"), detail=detail),
                                       min_width=560):
                return

        self._sync_btn.setEnabled(False)
        self._sync_btn.setText(t("local.sync_running_btn"))
        progress = dialogs.LogDialog(self.window(), "")
        progress.show()

        def on_log(line):
            post_to_ui(lambda _a: progress.append(line))

        def work():
            apply_mod_sync(plan, install_dir, on_log=on_log)

        def done(_result) -> None:
            progress.finish()
            self.refresh_sync_button_state()

        def error(exc: Exception) -> None:
            progress.append(t("sync.error_prefix", detail=str(exc)))
            progress.finish()
            self.refresh_sync_button_state()

        run_async(work, done, error)

    def _remove_mod_sync_junction(self, cluster) -> None:
        if not dialogs.ask_yes_no(self.window(), t("local.remove_junction_btn"),
                                   t("local.remove_junction_confirm_msg"), min_width=560):
            return
        install_dir, client_mods_dir = self._passive_sync_dirs(cluster)
        if install_dir is None:
            return
        self._sync_btn.setEnabled(False)
        self._sync_btn.setText(t("local.sync_running_btn"))
        progress = dialogs.LogDialog(self.window(), t("local.remove_junction_btn"))
        progress.show()

        def on_log(line):
            post_to_ui(lambda _a: progress.append(line))

        def work():
            return detach_mod_sync_junction(install_dir, client_mods_dir, on_log=on_log)

        def done(removal) -> None:
            if removal is not None:
                for err in removal.errors:
                    progress.append(t("sync.error_prefix", detail=err))
                if removal.removed and removal.copied:
                    progress.append(t("local.remove_junction_done_detail", count=len(removal.copied_entries)))
            progress.finish()
            self._sync_btn.setEnabled(True)
            self.refresh_sync_button_state()

        def error(exc: Exception) -> None:
            progress.append(t("sync.error_prefix", detail=str(exc)))
            progress.finish()
            self._sync_btn.setEnabled(True)
            self.refresh_sync_button_state()

        run_async(work, done, error)

    # ── 加载 Mod 列表 ────────────────────────────────────────────────────
    def _refresh_mods(self, full: bool | None = None) -> None:
        full = bool(full)
        c = self.get_cluster()
        shard = next((s for s in c.shards if s.name == self._current_shard_name), None) if c else None
        loading_key = (c.name if c else None, shard.name if shard else None)
        if self._loading and loading_key == self._loading_key:
            return
        self._refresh_gen += 1
        gen = self._refresh_gen
        self._edited_cluster_path = str(c.path) if c else ""
        local_index = self._local_shardindex(c)
        if not shard or not (shard.mod_overrides_path or local_index):
            self._client_mods_disabled = self._client_mods_sort_enabled = False
            self._sort_enabled_ids = frozenset()
            self._set_dirty(False)
            self._mod_data.clear()
            self._mod_infos.clear()
            self._icon_imgs.clear()
            self._full_resolved_cache.clear()
            self._mod_stamps.clear()
            self._remember_saved_state()
            self._loading = False
            self._loading_key = None
            self._mods_loaded = True
            self._refresh_workshop_update_button_state()
            self._update_hint_label.setVisible(False)
            self._scan_status_label.setText("")
            self._update_enabled_count(0)
            self._render_list()
            return
        self._loading = True
        self._loading_full = full
        self._loading_key = loading_key
        self._mods_loaded = False
        self._update_enabled_count(0)
        self._refresh_workshop_update_button_state()
        self._update_hint_label.setVisible(False)
        self._scan_status_label.setText(t("mod.loading_full") if full else t("mod.loading"))
        self._render_list()

        platform, wegame_client_mods_dir = self._resolve_mod_folder_args(c)
        steam_runtime_mods_dir = self._server_mods_root() if platform == Platform.STEAM else None
        luajit_bin64_dir = None
        if c and c.source == SaveSource.SERVER and c.platform == Platform.STEAM:
            install_dir = self._local_install_dir()
            if install_dir:
                luajit_bin64_dir = find_bin64_dir(install_dir)
        # 「重新扫描」不信内存里的完整解析结果，按磁盘缓存（内容哈希校验）重来
        full_resolved_cache = {} if full else dict(self._full_resolved_cache)
        overrides_path = shard.mod_overrides_path
        first_screen = (self._row_filter_args(), self._first_screen_count())

        worker_args = (overrides_path, full, platform, wegame_client_mods_dir, steam_runtime_mods_dir,
                       luajit_bin64_dir, full_resolved_cache, local_index, first_screen, dict(self._mod_stamps))
        snapshot = None if full else take_mod_preload_snapshot(
            (platform, wegame_client_mods_dir, steam_runtime_mods_dir))

        def work():
            return self._load_mods_worker(*worker_args, snapshot=snapshot)

        def done(result) -> None:
            self._apply_loaded_mods(gen, result)
            if snapshot is not None:
                self._verify_preloaded_mods(gen, worker_args, result["stamps"])

        def error(exc: Exception) -> None:
            self._loading = False
            self._loading_key = None
            self._mods_loaded = True
            self._scan_status_label.setText(str(exc))
            self._render_list()

        cancel_mod_preload()  # 本页接手转换图标和读版本，停掉启动预热，免得两边同时起沙箱子进程
        run_async(work, done, error)

    def _verify_preloaded_mods(self, gen: int, worker_args: tuple, shown_stamps: dict) -> None:
        """按预热快照显示后，后台照常重扫一遍；Mod 在启动后有增删改时以重扫结果重新显示。
        用户已改了勾选（有未保存改动）时不覆盖，下次加载自然纠正。"""
        def work():
            return self._load_mods_worker(*worker_args)

        def done(result) -> None:
            if gen != self._refresh_gen or self._dirty or result["stamps"] == shown_stamps:
                return
            self._refresh_gen += 1  # 作废按快照启动的图标、版本后台任务
            self._apply_loaded_mods(self._refresh_gen, result)

        run_async(work, done, lambda _exc: None)

    def _load_mods_worker(self, overrides_path, full, platform, wegame_client_mods_dir,
                          steam_runtime_mods_dir, luajit_bin64_dir, full_resolved_cache, local_index=None,
                          first_screen=None, known_stamps=None, snapshot=None):
        """跑在线程池里——不能碰任何 Qt 对象。本地存档以 Master/save/shardindex 为准（游戏开服时据此重写 modoverrides.lua）。

        给了启动预热的 snapshot 时，快照里的 Mod 直接用其目录、解析结果和图标路径，不再扫盘；
        stamps 记录各 Mod 的 modinfo 指纹（快照里的取预热时的值），供事后核对。
        known_stamps 为上次加载时的指纹：与本次不同的 Mod（本地 Mod 改了 modinfo）不用内存里的完整解析结果。"""
        full_resolved_cache = dict(full_resolved_cache)  # 预热核对会用同一份参数再跑一遍，不改调用方的字典
        mod_data, mod_infos, mod_paths, icon_imgs, stamps = {}, {}, {}, {}, {}
        icon_targets = []
        cached_icon_targets = []
        version_targets = []
        luajit_active = False
        if local_index is not None:
            overrides = ModOverrides(path=local_index, mods=load_shardindex_mods(local_index))
        else:
            overrides = load_mod_overrides(overrides_path)
        overrides_dirty = False
        if luajit_bin64_dir is not None:
            luajit_active = (luajit_injector.detect_state(luajit_bin64_dir) is luajit_injector.InjectorState.ACTIVE
                             and luajit_injector.is_workshop_subscribed())
        if luajit_active:
            entry = overrides.mods.get(luajit_injector.WORKSHOP_MOD_KEY)
            if entry is None or not entry.enabled:
                enable_mod(overrides, luajit_injector.WORKSHOP_MOD_KEY)
                overrides_dirty = True
        if overrides_dirty:
            save_mod_overrides(overrides)

        if snapshot is not None:
            # 快照已含 V1 Legacy 包；不在快照里的只会是存档启用、本机没有内容的引用
            legacy_packages = {}
            installed_ids = snapshot.ids
        else:
            legacy_packages = find_legacy_packages() if platform == Platform.STEAM else {}
            installed_ids = list_installed_mod_ids(platform, wegame_client_mods_dir, legacy_packages=legacy_packages,
                                                   steam_runtime_mods_dir=steam_runtime_mods_dir)
        ids = merge_visible_mod_ids(installed_ids, overrides.mods)
        for wid in ids:
            entry = overrides.mods.get(wid) or ModEntry(workshop_id=wid, enabled=False, configuration_options={})
            mod_data[wid] = entry
            known = snapshot is not None and wid in snapshot.infos
            mod_folder = None
            try:
                if known:
                    mod_folder = snapshot.paths.get(wid)
                else:
                    mod_folder = resolve_readable_mod_folder(wid, platform, wegame_client_mods_dir,
                                                             steam_runtime_mods_dir, legacy_packages)
                if mod_folder is not None:
                    mod_paths[wid] = mod_folder
                stamps[wid] = snapshot.stamps.get(wid) if known else modinfo_stamp(mod_folder)
                if mod_folder is None or (known_stamps is not None and known_stamps.get(wid) != stamps[wid]):
                    full_resolved_cache.pop(wid, None)
                cached = full_resolved_cache.get(wid)
                if cached is not None:
                    mod_info = cached
                elif known:
                    mod_info = snapshot.infos[wid]
                else:
                    mod_info = parse_modinfo_cached(mod_folder) if mod_folder else None
                    if full and mod_info and mod_folder:
                        modinfo_path = mod_folder / "modinfo.lua"
                        result = load_cached_result(wid, modinfo_path)
                        if result is None:
                            result = resolve_full_modinfo(mod_folder)
                            save_result(wid, modinfo_path, result)
                        apply_full_sandbox_result(mod_info, result)
                        full_resolved_cache[wid] = mod_info
                mod_infos[wid] = mod_info
                # 静态解析不保证拿到版本号，统一收集后另起后台任务补齐。即使命中完整解析缓存也要收集：
                # Mod 可能在运行期间被 Steam 更新；version_cache 按内容哈希判断，没变时立即返回
                if mod_info and mod_folder and not full:
                    version_targets.append((wid, mod_folder, mod_info.workshop_id))
                if mod_info and mod_folder:
                    # 这里只判断缓存是否有效；读图放到列表显示之后，Mod 多时能少挡几百毫秒
                    if known:
                        cached_icon = snapshot.icon_paths.get(wid)
                    else:
                        cached_icon = get_cached_mod_icon_path(mod_info, mod_folder, platform)
                    if cached_icon is not None:
                        cached_icon_targets.append((wid, cached_icon, mod_info, mod_folder))
                    else:
                        icon_targets.append((wid, mod_info, mod_folder))
            except Exception:
                mod_infos.setdefault(wid, None)
                stamps.setdefault(wid, modinfo_stamp(mod_folder))
        if first_screen is not None and cached_icon_targets:
            self._preload_first_screen_icons(first_screen, mod_data, mod_infos, platform, luajit_active,
                                             icon_imgs, cached_icon_targets)
        self.ctx.mod_catalog.publish(platform, mod_infos, mod_paths, icon_imgs, wegame_client_mods_dir)
        return dict(mod_data=mod_data, mod_infos=mod_infos, mod_paths=mod_paths, icon_imgs=icon_imgs,
                   icon_targets=icon_targets, cached_icon_targets=cached_icon_targets,
                   version_targets=version_targets,
                   full_resolved_cache=full_resolved_cache,
                   luajit_active=luajit_active, platform=platform,
                   wegame_client_mods_dir=wegame_client_mods_dir, stamps=stamps,
                   client_mods_disabled=overrides.client_mods_disabled)

    @staticmethod
    def _preload_first_screen_icons(first_screen, mod_data, mod_infos, platform, luajit_active,
                                    icon_imgs, cached_icon_targets) -> None:
        """按界面同样的排序和筛选算出首屏的 Mod，先读它们已缓存的图标，列表一出现就有图；
        其余留在 cached_icon_targets 里等列表显示后再读。读失败的也留下，由后台任务转换。"""
        filter_args, count = first_screen
        priority_mod_id = luajit_injector.WORKSHOP_MOD_KEY if luajit_active else None
        rows = build_mod_rows(sort_mod_data(mod_data, mod_infos, priority_mod_id=priority_mod_id), mod_infos,
                              platform=platform, locked_mod_id=priority_mod_id, **filter_args)
        wanted = {row["workshop_id"] for row in rows[:count]}
        remaining = []
        for target in cached_icon_targets:
            wid, path = target[0], target[1]
            if wid in wanted:
                try:
                    icon_imgs[wid] = load_mod_icon_image(path)
                    continue
                except Exception:
                    pass
            remaining.append(target)
        cached_icon_targets[:] = remaining

    def _apply_loaded_mods(self, gen: int, result: dict) -> None:
        if gen != self._refresh_gen:
            return
        # 只在重新加载数据时排序；切换启用开关不重排，避免该行立即跳动
        priority_mod_id = luajit_injector.WORKSHOP_MOD_KEY if result["luajit_active"] else None
        self._mod_data = sort_mod_data(result["mod_data"], result["mod_infos"], priority_mod_id=priority_mod_id)
        self._sort_enabled_ids = frozenset(wid for wid, mod in self._mod_data.items() if mod.enabled)
        self._mod_infos = result["mod_infos"]
        self._mod_paths = result["mod_paths"]
        self._icon_imgs = result["icon_imgs"]
        self._full_resolved_cache = result["full_resolved_cache"]
        self._mod_stamps = result["stamps"]
        self._luajit_mod_locked = result["luajit_active"]
        self._client_mods_disabled = self._client_mods_sort_enabled = result["client_mods_disabled"]
        self._set_dirty(False)  # 刚从磁盘读出，跟已保存内容一致
        self._remember_saved_state()
        self._loading = False
        self._loading_key = None
        self._mods_loaded = True
        self._update_scan_status_label()
        self._update_enabled_count()
        self._refresh_workshop_update_button_state()
        self._render_list()
        platform, wegame_dir = result["platform"], result["wegame_client_mods_dir"]

        def apply_icons(icons: dict) -> None:
            self._refresh_runtime_banner()  # 转换中途可能因运行库加载失败而熔断
            if gen != self._refresh_gen or not icons:
                return
            self._icon_imgs.update(icons)
            self.ctx.mod_catalog.update_icons(platform, icons, wegame_dir)
            self._render_list()

        icon_targets = result["icon_targets"]
        cached_icon_targets = result["cached_icon_targets"]
        if cached_icon_targets:
            # 已缓存的图标单独一个任务先读，不排在需要 ktech 转换的图标后面；读失败的转交转换任务
            def load_cached():
                icons, failed = {}, []
                for wid, path, mod_info, mod_folder in cached_icon_targets:
                    try:
                        icons[wid] = load_mod_icon_image(path)
                    except Exception:
                        failed.append((wid, mod_info, mod_folder))
                return icons, failed

            def cached_done(loaded) -> None:
                icons, failed = loaded
                apply_icons(icons)
                if failed:
                    self._convert_icons(gen, failed, platform, apply_icons)

            run_async(load_cached, cached_done, lambda _exc: None)
        if icon_targets:
            self._convert_icons(gen, icon_targets, platform, apply_icons)

        version_targets = result["version_targets"]
        if version_targets:
            def work():
                from concurrent.futures import ThreadPoolExecutor, as_completed

                resolved = {}
                with ThreadPoolExecutor(max_workers=4) as pool:
                    futures = [pool.submit(resolve_local_version_target, target) for target in version_targets]
                    for future in as_completed(futures):
                        try:
                            wid, normalized = future.result()
                        except Exception:
                            continue
                        resolved[wid] = normalized
                return resolved

            def done(resolved: dict) -> None:
                if gen != self._refresh_gen:
                    return
                for wid, normalized in resolved.items():
                    info = self._mod_infos.get(wid)
                    if info is not None:
                        apply_local_version(info, normalized)
                if resolved:
                    self.ctx.mod_catalog.publish(platform, self._mod_infos, self._mod_paths, self._icon_imgs,
                                                 wegame_dir)
                    self._render_list()
                self._refresh_chs_names(gen, platform, wegame_dir)

            run_async(work, done, lambda _exc: self._refresh_chs_names(gen, platform, wegame_dir))
        else:
            self._refresh_chs_names(gen, platform, wegame_dir)
        self._refresh_main_workshop_status(gen)

    def _refresh_chs_names(self, gen: int, platform, wegame_dir) -> None:
        """后台取 Chinese++ Pro 的 Mod 中文名称。翻译要拿原名拼接，须在版本解析写回最终名称之后运行。"""
        if gen != self._refresh_gen:
            return
        infos = dict(self._mod_infos)

        def done(names: dict) -> None:
            if gen != self._refresh_gen:
                return
            if chs_translation.set_display_names(names):
                self._render_list()

        run_async(lambda: chs_translation.resolve_display_names(infos, platform, wegame_dir), done,
                  lambda _exc: None)

    def _refresh_runtime_banner(self) -> None:
        problem, bad_file = tex_convert.ktech_runtime_problem()
        if problem == "missing":
            text = t("mod.ktech_runtime_missing_banner")
        elif problem == "broken":
            detail = t("mod.ktech_runtime_bad_file", path=bad_file) if bad_file else ""
            text = t("mod.ktech_runtime_broken_banner", detail=detail)
        else:
            text = ""
        self._runtime_banner.set_text(text)

    def _convert_icons(self, gen: int, targets: list, platform, on_done) -> None:
        """后台转换（必要时调用 ktech）并读取图标，完成后在界面线程交给 on_done。"""
        if gen != self._refresh_gen:
            return

        def work():
            icons = {}
            for wid, mod_info, mod_folder in targets:
                try:
                    path = get_mod_icon_path(mod_info, mod_folder, platform)
                    if path is not None:
                        icons[wid] = load_mod_icon_image(path)
                except Exception:
                    continue
            return icons

        run_async(work, on_done, lambda _exc: None)

    # ── 渲染 ────────────────────────────────────────────────────────────
    def _build_rows(self):
        cluster = self.get_cluster()
        platform = cluster.platform if cluster else Platform.STEAM
        locked_id = luajit_injector.WORKSHOP_MOD_KEY if self._luajit_mod_locked else None
        rows = build_mod_rows(self._mod_data, self._mod_infos, platform=platform, locked_mod_id=locked_id,
                              **self._row_filter_args())
        from dstools.features.mod.parser import find_workshop_dir

        workshop_root = find_workshop_dir()
        for row in rows:
            path = self._mod_paths.get(row["workshop_id"])
            row["has_folder"] = bool(path and Path(path).is_dir())
            numeric = str(row["workshop_id"]).removeprefix("workshop-")
            status = self._workshop_status_cache.get(int(numeric)) if numeric.isdigit() else None
            row["mod_format"] = None if row.get("is_local") else self._mod_format(row["workshop_id"], workshop_root)
            if not row["has_folder"] and row["enabled"] and self._mod_infos.get(row["workshop_id"]) is None:
                # 存档 modoverrides.lua 启用了、本机却没有文件（多半是没订阅）：读不到 modinfo，
                # 名称和版本都是空的，用户不知道这一行从哪来——写明来源和原因。
                row["name"] = row["name"] or t("mod.reference_missing_name")
                row["version_text"] = referenced_missing_status_text(status)
        switch_row = self._client_mods_switch_row(cluster)
        if switch_row is not None:
            rows.insert(self._client_mods_row_index(rows, switch_row["name"]), switch_row)
        return rows

    def _client_mods_row_index(self, rows: list[dict], name: str) -> int:
        """按 sort_mod_data 的规则（启用在前、再按名称）找开关行的位置，置顶的 LuaJIT 行之后。
        各行的启用状态取加载排序时的快照：行顺序没有随切换重排，用当前状态会让开关行跳到刚关掉的 Mod 前面。"""
        for index, row in enumerate(rows):
            if row.get("locked"):
                continue
            if self._client_mods_sort_enabled != (row["workshop_id"] in self._sort_enabled_ids):
                if self._client_mods_sort_enabled:
                    return index
                continue
            if mod_name_cmp(name, row["name"] or row["workshop_id"]) < 0:
                return index
        return len(rows)

    def _client_mods_switch_row(self, cluster) -> dict | None:
        """服务器存档的"禁用客户端模组"行：借用 Mod 行的样式，开关跟普通 Mod 一样随"保存"写入。
        本地存档开服时游戏会按启用的 Mod 重写 modoverrides.lua，留不住这个键，所以不显示。"""
        if not cluster or cluster.source != SaveSource.SERVER or not self._current_shard_name:
            return None
        filters = self._row_filter_args()
        show = filters["show"]
        if show == "client" or show == ("disabled" if self._client_mods_disabled else "enabled"):
            return None
        name = t("mod.client_mods_disabled")
        needle = filters["query"].strip().casefold()
        if needle and needle not in name.casefold() and needle not in CLIENT_MODS_DISABLED_KEY:
            return None
        return {"workshop_id": CLIENT_MODS_DISABLED_KEY, "name": name,
                "version_text": t("mod.client_mods_disabled_desc"), "enabled": self._client_mods_disabled,
                "is_local": False, "has_config": False, "has_link": False,
                "has_folder": False, "mod_format": None}

    def _mod_format(self, workshop_id: str, workshop_root) -> str | None:
        """列表和导出图片共用的 V1/V2 判断：有 Steam 状态时以它为准，否则看工坊目录内容。"""
        from dstools.features.mod.parser import detect_mod_format

        numeric = str(workshop_id).removeprefix("workshop-")
        status = self._workshop_status_cache.get(int(numeric)) if numeric.isdigit() else None
        steam = status.evidence.steam_state if status is not None and status.evidence is not None else None
        return detect_mod_format(numeric, workshop_root, steam)

    def _row_filter_args(self) -> dict:
        """当前筛选页签与搜索词对应的 build_mod_rows 参数（界面线程读取，可交给后台线程用）。"""
        show_map = {0: "all", 1: "enabled", 2: "disabled", 3: "custom", 4: "client"}
        show = show_map[self._filter_tabs.current_index()]
        # "全部"同时列出客户端模组；"客户端模组"只列它们；其余筛选只看服务端 Mod
        return dict(query=self._filter_edit.text(), show=show,
                    show_local=show == "client", separate_client_mods=show != "all")

    def _first_screen_count(self) -> int:
        """首屏能显示几个 Mod；页面还没显示时列表尺寸未定，按窗口尺寸估计（宁多勿少）。"""
        panel = self._list_panel
        if panel.isVisible() and panel.viewport().height() > 100:
            return panel.first_screen_count()
        window = self.window()
        return panel.first_screen_count(window.width(), window.height())

    def _render_list(self) -> None:
        if self._loading:
            self._list_panel.clear()
            self._list_panel.set_center_message(t("mod.loading_full") if self._loading_full else t("mod.loading"))
            return
        self._list_panel.set_center_message("")
        rows = self._build_rows()
        icons = self._icon_imgs
        if any(row["workshop_id"] == CLIENT_MODS_DISABLED_KEY for row in rows):
            icons = {**icons, CLIENT_MODS_DISABLED_KEY: _client_mods_disabled_icon()}
        self._list_panel.set_rows(rows, icons, interactive=self._editable(self.get_cluster()))

    def _update_enabled_count(self, count: int | None = None) -> None:
        if count is None:
            count = sum(1 for mod in self._mod_data.values() if mod.enabled)
        self._enabled_count_label.setText(t("mod.enabled_count", count=count))

    def _set_dirty(self, dirty: bool) -> None:
        changed = dirty != self._dirty
        self._dirty = dirty
        c = self.get_cluster()
        self._save_btn.setEnabled(dirty)
        self._discard_btn.setEnabled(dirty)
        self._apply_current_btn.setEnabled(dirty and bool(c and c.source == SaveSource.SERVER))
        self._unsaved_label.setVisible(dirty)
        if changed:
            self.ctx.unsaved_changed.emit()

    def _mark_dirty(self) -> None:
        self._set_dirty(True)

    def _remember_saved_state(self) -> None:
        self._saved_mod_state = {wid: (mod.enabled, copy.deepcopy(mod.configuration_options))
                                 for wid, mod in self._mod_data.items()}
        self._saved_client_mods_disabled = self._client_mods_disabled

    def _restore_saved_state(self) -> None:
        """把勾选和配置恢复成上次加载/保存时的样子；只改内存，列表顺序不变。"""
        for wid, mod in self._mod_data.items():
            saved = self._saved_mod_state.get(wid)
            if saved is not None:
                mod.enabled = saved[0]
                mod.configuration_options = copy.deepcopy(saved[1])
        self._client_mods_disabled = self._saved_client_mods_disabled
        self._set_dirty(False)
        self._update_enabled_count()
        self._render_list()

    def _discard_changes(self) -> None:
        """"取消修改"按钮：确认后丢掉未保存的勾选和配置。"""
        if not self._dirty or not dialogs.ask_yes_no(self.window(), t("dlg.discard_changes_btn"),
                                                     t("mod.discard_changes_confirm")):
            return
        self._restore_saved_state()

    def has_unsaved_changes(self) -> bool:
        return self._dirty

    def discard_unsaved_changes(self) -> None:
        self._restore_saved_state()

    def save_unsaved_changes(self) -> bool:
        """离开前保存：跟"保存"按钮写同样的内容，但同步完成、不弹成功提示。"""
        from dstools.features.mod.shardindex import LOCAL_SAVE_OPEN, LOCAL_SAVE_RUNNING, local_save_play_state

        c = self.get_cluster()
        index_path = self._local_shardindex(c)
        if index_path is not None:
            try:
                state = local_save_play_state(c)
            except Exception as exc:
                dialogs.show_error(self.window(), t("mod.save_btn"), str(exc))
                return False
            if state == LOCAL_SAVE_RUNNING:
                dialogs.show_warning(self.window(), t("mod.save_btn"), t("mod.local_save_running"))
                return False
            if state == LOCAL_SAVE_OPEN and not dialogs.ask_yes_no(
                    self.window(), t("mod.save_btn"), t("mod.local_save_game_open_confirm")):
                return False
            if not self._write_local_mods(c, index_path, silent=True):
                return False
        else:
            shard = next((s for s in c.shards if s.name == self._current_shard_name), None) if c else None
            if not c or not shard or not shard.mod_overrides_path or c.source != SaveSource.SERVER:
                dialogs.show_warning(self.window(), t("mod.save_btn"), t("dlg.no_overrides"))
                return False
            try:
                self._write_server_mods(c, shard)
            except OSError as exc:
                dialogs.show_error(self.window(), t("mod.save_btn"), str(exc))
                return False
        self._set_dirty(False)
        return True

    def get_pending_enabled_mod_ids(self, cluster):
        if not self._dirty or self._loading or cluster is None:
            return None
        selected = self.get_cluster()
        if selected is None or getattr(selected, "path", None) != getattr(cluster, "path", None):
            return None
        if self._loading_key and self._loading_key[0] != cluster.name:
            return None
        return frozenset(str(wid).removeprefix("workshop-") for wid, mod in self._mod_data.items() if mod.enabled)

    # ── 行为回调 ────────────────────────────────────────────────────────
    def _on_toggle(self, workshop_id: str) -> None:
        if not self._editable(self.get_cluster()):
            return
        if workshop_id == CLIENT_MODS_DISABLED_KEY:
            self._client_mods_disabled = not self._client_mods_disabled
            self._mark_dirty()
            self._render_list()
            return
        if self._luajit_mod_locked and workshop_id == luajit_injector.WORKSHOP_MOD_KEY:
            return
        mod = self._mod_data.get(workshop_id)
        if not mod:
            return
        mod.enabled = not mod.enabled
        if mod.enabled and not mod.configuration_options:
            # 存档里还没有这个 Mod 的配置：套用默认配置（对应游戏主菜单"模组"里的全局配置）
            mod.configuration_options = recall_for(workshop_id, self._mod_infos.get(workshop_id))
        normalized_id = str(workshop_id).removeprefix("workshop-")
        if normalized_id == IA_SHIPWRECKED_MOD_ID and mod.enabled:
            core_key = find_mod_key(self._mod_data, IA_CORE_MOD_ID)
            core = self._mod_data.get(core_key) if core_key else None
            if core is None:
                mod.enabled = False
                dialogs.show_error(self.window(), "缺少 Mod 依赖",
                                   "岛屿冒险 - 海难缺少依赖 Mod 3435352667，请先订阅并安装核心。")
                self._render_list()
                return
            if not core.enabled:
                if not dialogs.ask_yes_no(self.window(), t("mod.dependency_required_title"),
                                          t("mod.dependency_required_confirm", mod="岛屿冒险 - 海难",
                                            dependency="岛屿冒险 - 核心 (3435352667)")):
                    mod.enabled = False
                    self._render_list()
                    return
                core.enabled = True
        elif normalized_id == IA_CORE_MOD_ID and not mod.enabled:
            child_key = find_mod_key(self._mod_data, IA_SHIPWRECKED_MOD_ID)
            child = self._mod_data.get(child_key) if child_key else None
            if child is not None and child.enabled:
                child.enabled = False
        self._mark_dirty()
        self._update_enabled_count()
        self._render_list()

    def _on_config(self, workshop_id: str) -> None:
        mod = self._mod_data.get(workshop_id)
        mod_info = self._mod_infos.get(workshop_id)
        if not mod or not mod_info:
            return
        if not mod_info.config_options and not mod_info.unsupported_schema:
            return
        if mod_info.client_only:
            # 纯客户端 Mod 的配置只在游戏客户端（不进存档），即它的默认配置
            self._open_default_config(workshop_id)
            return
        read_only = not self._editable(self.get_cluster())
        open_mod_config(self, workshop_id, mod, mod_info, read_only, "local_save")

    def _open_default_config(self, workshop_id: str, mod_info=None) -> None:
        """编辑默认配置；mod_info 由默认配置面板传入（它独立扫描，不依赖当前存档）。"""
        mod_info = mod_info or self._mod_infos.get(workshop_id)
        if not mod_info or (not mod_info.config_options and not mod_info.unsupported_schema):
            return
        entry = ModEntry(workshop_id=workshop_id, configuration_options=recall(workshop_id) or {})
        open_mod_config(self, workshop_id, entry, mod_info, False, "", default_mode=True)

    def _save_default_config(self, workshop_id: str, options: dict) -> None:
        """保存默认配置（整份替换），并触发与游戏的同步（游戏关闭时立即写入游戏全局配置）。"""
        try:
            remember(workshop_id, dict(options), replace=True)
        except OSError as exc:
            dialogs.show_error(self.window(), t("mod.global_defaults_btn"), str(exc))
            return
        self._refresh_global_defaults()
        self._start_config_sync(notify=True)

    def _start_config_sync(self, notify: bool = False) -> bool:
        """发起同步；全局默认配置弹窗开着时显示"同步中"并在结束后展示结果。返回同步是否已启动或排队。"""
        started = start_config_sync(self.ctx, self if notify else None, on_done=self._on_config_sync_done)
        dialog = self._global_defaults_dialog
        if started and dialog is not None and dialog.isVisible():
            dialog.set_syncing()
        return started

    def _on_config_sync_done(self, report) -> None:
        # 不判断可见性：同步中途关掉弹窗，也要复位它的"同步中"状态
        if self._global_defaults_dialog is not None:
            self._global_defaults_dialog.on_sync_done(report)

    def _refresh_global_defaults(self) -> None:
        if self._global_defaults_dialog is not None and self._global_defaults_dialog.isVisible():
            self._global_defaults_dialog.refresh_states()

    def _open_global_defaults(self) -> None:
        """全局默认配置弹窗（非模态：其中打开的配置弹窗挂在主窗口下，模态会挡住它）；只保留一个实例。"""
        if self._global_defaults_dialog is None:
            self._global_defaults_dialog = GlobalDefaultsDialog(self)
        dialog = self._global_defaults_dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        dialog.load()

    def _on_link(self, workshop_id: str) -> None:
        numeric_id = workshop_id.replace("workshop-", "")
        if not numeric_id.isdigit():
            return
        c = self.get_cluster()
        if c and c.platform == Platform.WEGAME:
            webbrowser.open(f"https://www.wegame.com.cn/pc_game/assistant.html#/2000004/newMod/{numeric_id}")
        else:
            webbrowser.open(f"https://steamcommunity.com/sharedfiles/filedetails/?id={numeric_id}")

    def _on_open_mod_folder(self, workshop_id: str) -> None:
        path = resolve_mod_open_location(workshop_id, self._mod_paths.get(workshop_id))
        if path is None:
            dialogs.show_warning(self.window(), t("env.open_location"), t("mod.open_location_missing"))
            return
        try:
            os.startfile(str(Path(path)))
        except OSError as exc:
            dialogs.show_error(self.window(), t("env.open_location"), str(exc))

    def _on_copy_id(self, workshop_id: str) -> None:
        numeric_id = workshop_id.replace("workshop-", "")
        QGuiApplication.clipboard().setText(numeric_id)
        dialogs.show_toast(self.window(), t("mod.id_copied_toast", id=numeric_id))

    def _resolve_mod_display_name(self, workshop_id: str) -> str:
        if workshop_id == CLIENT_MODS_DISABLED_KEY:
            return t("mod.client_mods_disabled")
        info = self._mod_infos.get(workshop_id)
        mod = self._mod_data.get(workshop_id)
        name = localize_mod_name(workshop_id, info.name if info else getattr(mod, "name", ""))
        if not name:
            text_id = str(workshop_id).removeprefix("workshop-")
            title = self._workshop_title_cache.get(text_id, "")
            if title:
                name = localize_mod_name(workshop_id, title)
        return name or workshop_id

    def _on_copy_name(self, workshop_id: str) -> None:
        name = self._resolve_mod_display_name(workshop_id)
        QGuiApplication.clipboard().setText(name)
        dialogs.show_toast(self.window(), t("mod.name_copied_toast", name=name))

    def _open_recommend_mods(self) -> None:
        RecommendModsDialog(self).exec()

    def _open_download_by_id(self) -> None:
        from dstools.qt.steamcmd_mods import run_steamcmd_mod_update
        from dstools.qt.workshop_download_dialog import WorkshopDownloadDialog
        dialog = WorkshopDownloadDialog(self.window())
        if dialog.exec():
            run_steamcmd_mod_update(self.window(), dialog.workshop_ids, on_finished=self.ctx.workshop_mods_changed.emit)

    # ── 保存 / 应用 ─────────────────────────────────────────────────────
    def _write_mod_states(self, overrides) -> None:
        overrides.client_mods_disabled = self._client_mods_disabled
        for wid, mod in self._mod_data.items():
            if wid in overrides.mods:
                overrides.mods[wid].enabled = mod.enabled
                overrides.mods[wid].configuration_options = dict(mod.configuration_options)
            elif mod.enabled or mod.configuration_options:
                config = dict(mod.configuration_options)
                if not config:
                    info = self._mod_infos.get(wid)
                    if info:
                        config = {opt.name: opt.default for opt in info.config_options if not opt.is_header}
                overrides.mods[wid] = ModEntry(workshop_id=wid, enabled=mod.enabled, configuration_options=config)

    def _save_local_mods(self, cluster, index_path: Path) -> None:
        """本地存档：游戏前端缓存 shardindex，回到"登陆中..."页面才重读，存档运行时开服进程还会覆盖。
        后台检测后：运行中拦下；游戏开着时先确认；没开直接存。"""
        from dstools.features.mod.shardindex import (
            LOCAL_SAVE_OPEN, LOCAL_SAVE_RUNNING, local_save_play_state,
        )

        self._save_btn.setEnabled(False)

        def done(state: str) -> None:
            self._save_btn.setEnabled(self._dirty)
            current = self.get_cluster()
            # 检测期间存档列表可能刷新成新对象，按路径判断是否还是同一个存档
            if current is None or str(current.path) != str(cluster.path):
                return
            if state == LOCAL_SAVE_RUNNING:
                dialogs.show_warning(self.window(), t("mod.save_btn"), t("mod.local_save_running"))
                return
            if state == LOCAL_SAVE_OPEN and not dialogs.ask_yes_no(
                    self.window(), t("mod.save_btn"), t("mod.local_save_game_open_confirm")):
                return
            self._write_local_mods(cluster, index_path, silent=False)

        def failed(exc: Exception) -> None:
            self._save_btn.setEnabled(self._dirty)
            dialogs.show_error(self.window(), t("mod.save_btn"), str(exc))

        run_async(lambda: local_save_play_state(cluster), done, failed)

    def _write_local_mods(self, cluster, index_path: Path, silent: bool) -> bool:
        """写 Master shardindex（游戏前端以它为准），再把同一份写进各世界 modoverrides.lua；返回是否成功。"""
        try:
            overrides = ModOverrides(path=index_path, mods=load_shardindex_mods(index_path))
            self._write_mod_states(overrides)
            save_shardindex_mods(index_path, overrides.mods)
        except (OSError, ShardIndexError) as exc:
            dialogs.show_error(self.window(), t("mod.save_btn"), str(exc))
            return False
        for sh in cluster.shards:
            dst = ModOverrides(path=sh.mod_overrides_path or sh.path / "modoverrides.lua")
            sync_mods(overrides, dst)
            save_mod_overrides(dst)
        self._remember_saved_state()
        self.ctx.cluster_config_saved.emit(cluster)
        self._set_dirty(False)
        if not silent:
            enabled_count = sum(1 for m in overrides.mods.values() if m.enabled)
            dialogs.show_info(self.window(), t("dlg.save_ok"), t("mod.local_saved", count=enabled_count))
            self._refresh_mods()
        return True

    def _save_mods(self) -> None:
        c = self.get_cluster()
        index_path = self._local_shardindex(c)
        if index_path is not None:
            self._save_local_mods(c, index_path)
            return
        shard = next((s for s in c.shards if s.name == self._current_shard_name), None) if c else None
        if not c or not shard or not shard.mod_overrides_path or c.source != SaveSource.SERVER:
            dialogs.show_warning(self.window(), t("mod.save_btn"), t("dlg.no_overrides"))
            return
        overrides, other_shards = self._write_server_mods(c, shard)
        enabled_count = sum(1 for m in overrides.mods.values() if m.enabled)
        dialogs.show_info(self.window(), t("dlg.save_ok"), t("dlg.saved_mods", count=enabled_count, shard=shard.name))
        if other_shards:
            dialogs.show_info(self.window(), t("mod.save_btn"), t("dlg.sync_done", count=len(other_shards)))
        self._set_dirty(False)
        self._refresh_mods()

    def config_applied(self) -> None:
        """Mod 配置窗口点了"应用"：配置只改在内存里，跟勾选一样等"保存修改"统一写入。"""
        self._mark_dirty()
        self._render_list()

    def _write_server_mods(self, cluster, shard):
        """写当前世界的 modoverrides.lua 并同步到其他世界；返回（写入内容, 其他世界列表）。"""
        overrides = load_mod_overrides(shard.mod_overrides_path)
        self._write_mod_states(overrides)
        save_mod_overrides(overrides)
        other_shards = [sh for sh in cluster.shards if sh.name != shard.name and sh.mod_overrides_path]
        for sh in other_shards:
            dst = load_mod_overrides(sh.mod_overrides_path)
            sync_mods(overrides, dst)
            save_mod_overrides(dst)
        self._remember_saved_state()
        self.ctx.cluster_config_saved.emit(cluster)
        return overrides, other_shards

    def _apply_current_shard(self) -> None:
        c = self.get_cluster()
        shard = next((s for s in c.shards if s.name == self._current_shard_name), None) if c else None
        if not c or not shard or not shard.mod_overrides_path or c.source != SaveSource.SERVER:
            return
        if not dialogs.ask_yes_no(self.window(), t("mod.apply_current"), t("dlg.apply_current_confirm", shard=shard.name)):
            return
        overrides = load_mod_overrides(shard.mod_overrides_path)
        self._write_mod_states(overrides)
        save_mod_overrides(overrides)
        self.ctx.cluster_config_saved.emit(c)
        dialogs.show_info(self.window(), t("mod.apply_current"), t("dlg.current_saved", shard=shard.name))
        self._refresh_mods()

    def _save_as_preset(self) -> None:
        if self._loading:
            dialogs.show_info(self.window(), t("mod.preset_save_btn"), t("mod.loading"))
            return
        if not self._mod_data:
            dialogs.show_warning(self.window(), t("mod.preset_save_btn"), t("preset.no_mods_selected_in_tab"))
            return
        SavePresetDialog(self).exec()

    def _apply_preset_dialog(self) -> None:
        if self._loading:
            dialogs.show_info(self.window(), t("mod.preset_apply_btn"), t("mod.loading"))
            return
        ApplyPresetDialog(self).exec()

    # ── Workshop 更新 ────────────────────────────────────────────────────
    def _export_mod_list_image(self) -> None:
        """导出当前世界（默认地上）已启用的服务端 Mod 为图片，含未保存的勾选改动。"""
        from dstools.features.mod.export_list import build_export_entries
        from dstools.qt.mod_export_dialog import ModListImageDialog, default_file_name, render_mod_list_image

        cluster = self.get_cluster()
        if cluster is None or self._loading:
            return
        from dstools.features.mod.parser import find_workshop_dir

        workshop_root = find_workshop_dir()
        formats = {wid: self._mod_format(wid, workshop_root) for wid, mod in self._mod_data.items() if mod.enabled}
        entries = build_export_entries(self._mod_data, self._mod_infos, formats)
        if not entries:
            dialogs.show_info(self, t("mod.export_preview_title"), t("mod.export_empty"))
            return
        network = cluster.config.network if cluster.config else {}
        cluster_label = str(network.get("cluster_name") or cluster.name)
        subtitle = t("mod.export_subtitle", cluster=cluster_label, shard=self._current_shard_name or "-",
                     count=len(entries), time=time.strftime("%Y-%m-%d %H:%M"))
        image = render_mod_list_image(entries, self._icon_imgs, t("mod.export_title"), subtitle)
        ModListImageDialog(self, image, default_file_name(cluster_label)).exec()

    def _workshop_mod_ids(self) -> list[int]:
        ids = []
        for raw_id in self._mod_data:
            text_id = str(raw_id).removeprefix("workshop-")
            if text_id.isdigit() and int(text_id) > 0:
                ids.append(int(text_id))
        return ids

    def _workshop_candidate_ids(self) -> list[int]:
        """合并本地目录、V1 包与存档配置中的项目（订阅项由 Steam 补入），覆盖"已安装但未启用""残留文件"
        "V1 包未展开"等不在当前列表中的情况。"""
        list_ids = self._workshop_mod_ids()
        cluster_ids = [int(raw_id) for raw_id in self._current_cluster_workshop_ids()]
        stamp, scanned = self._workshop_scan_ids
        if scanned and stamp == self._workshop_status_checked_at and self.workshop_status_fresh(()):
            # 状态有效期内复用那次扫描的结果，打开「更新 Mod」窗口不再扫一遍磁盘；状态作废时一起作废
            return list(dict.fromkeys(item for item in (*list_ids, *scanned, *cluster_ids) if item > 0))
        return _expand_workshop_candidate_ids(list_ids, cluster_ids)

    def workshop_status_fresh(self, ids) -> bool:
        """更新状态在有效期内查过且覆盖这些 Mod 时直接复用：工具栏提示和「更新 Mod」弹窗共用，免得每次打开都等一遍。"""
        return (time.monotonic() - self._workshop_status_checked_at < _WORKSHOP_STATUS_TTL
                and {int(wid) for wid in ids} <= {int(wid) for wid in self._workshop_status_cache})

    def _current_cluster_workshop_ids(self) -> set[str]:
        """返回当前存档所有世界中出现过的 Workshop Mod key（数字 ID 字符串）。"""
        ids: set[str] = set()
        cluster = self.get_cluster()
        if not cluster:
            return ids
        for shard in cluster.shards:
            if not shard.mod_overrides_path:
                continue
            try:
                overrides = load_mod_overrides(shard.mod_overrides_path)
            except Exception:
                continue
            for raw_id in overrides.mods:
                text_id = str(raw_id).removeprefix("workshop-")
                if text_id.isdigit():
                    ids.add(text_id)
        return ids

    def _update_scan_status_label(self) -> None:
        regular = sum(1 for wid in self._mod_data if not str(wid).removeprefix("workshop-").isdigit()
                     or int(str(wid).removeprefix("workshop-")) <= 0)
        self._scan_status_label.setText(t("mod.scan_found_breakdown", regular=len(self._mod_data) - regular, custom=regular))

    def _refresh_workshop_update_button_state(self) -> None:
        ready = self._mods_loaded and not self._loading and not self._workshop_update_running
        self._workshop_update_btn.setEnabled(ready)

    def _on_mod_update_hint_click(self) -> None:
        pass  # 简化版没有独立的更新日志窗口可恢复

    def _refresh_main_workshop_status(self, gen: int) -> None:
        """列表首帧完成后后台预查一次更新状态，供工具栏提示复用。"""
        if gen != self._refresh_gen or not self._mods_loaded or self._workshop_update_running:
            return
        if self._workshop_status_refreshing:
            return
        local_ids = self._workshop_mod_ids()
        if self._uses_steamcmd():
            self._refresh_steamcmd_mod_status(gen, local_ids)
            return
        self._steamcmd_pending_count = None
        if not local_ids:
            return
        if self.workshop_status_fresh(local_ids):
            self._update_workshop_update_hint()
            return
        self._workshop_status_refreshing = True
        # 与「更新 Mod」弹窗查同一批候选，预查结果打开弹窗时可直接复用；目录扫描放到后台线程
        cluster_ids = [int(raw_id) for raw_id in self._current_cluster_workshop_ids()]

        def work():
            from dstools.features.mod.legacy_v1 import (
                find_legacy_runtime_residual_dirs, is_legacy_read_cache_path, running_dst_processes,
            )
            from dstools.features.mod.parser import (
                find_workshop_content_dirs, find_workshop_dir, find_workshop_residual_dirs,
            )
            from dstools.features.mod.workshop_acf import read_acf_subscribers
            from dstools.features.mod.workshop_status import inspect_workshop_items
            discovered_paths = {
                int(str(wid).removeprefix("workshop-")): path
                for wid, path in self._mod_paths.items()
                if str(wid).removeprefix("workshop-").isdigit() and not is_legacy_read_cache_path(path)}
            candidate_ids = _expand_workshop_candidate_ids(local_ids, cluster_ids)
            try:
                states = inspect_workshop_items(
                    candidate_ids, discovered_paths=discovered_paths,
                    legacy_active_root=self._server_mods_root(), query_source=True, include_subscribed=True,
                    configured_ids=cluster_ids, residual_paths=find_workshop_residual_dirs(),
                    workshop_content_paths=find_workshop_content_dirs(),
                    legacy_runtime_residual_paths=find_legacy_runtime_residual_dirs(),
                    running_dst_processes=running_dst_processes(),
                    acf_subscribers=read_acf_subscribers(find_workshop_dir()))
                return states, "", candidate_ids
            except Exception as exc:
                return {}, str(exc), []

        def done(result) -> None:
            states, error, candidate_ids = result
            self._workshop_status_refreshing = False
            if gen != self._refresh_gen:
                return
            self._workshop_status_cache = dict(states)
            self._workshop_status_error = error
            self._workshop_status_checked_at = time.monotonic()
            self._workshop_scan_ids = (self._workshop_status_checked_at, candidate_ids)
            self._update_workshop_update_hint()

        run_async(work, done, lambda _exc: setattr(self, "_workshop_status_refreshing", False))

    def _current_save_enabled_workshop_ids(self) -> list[str]:
        """当前存档各世界已启用的工坊 Mod，加上本页尚未保存的勾选（刚勾上就点更新也算）。"""
        from dstools.features.mod.sync import get_enabled_mod_ids
        cluster = self.get_cluster()
        ids = set(get_enabled_mod_ids(cluster)) if cluster else set()
        ids |= {str(wid).removeprefix("workshop-") for wid, mod in self._mod_data.items() if mod.enabled}
        return sorted((wid for wid in ids if wid.isdigit() and int(wid) > 0), key=int)

    def _refresh_steamcmd_mod_status(self, gen: int, local_ids: list[int]) -> None:
        """SteamCMD 通道：对照 SteamCMD 工坊目录与 Web API，统计当前存档启用的 Mod 中需要下载的数量
        （与「更新本存档 Mod」范围一致；local_ids 是 Steam 客户端通道用的，这里不用）。"""
        ids = self._current_save_enabled_workshop_ids()
        if not ids:
            self._steamcmd_pending_count = None
            return
        self._workshop_status_refreshing = True

        def work():
            from dstools.features.mod.steamcmd_mod_status import inspect_steamcmd_mods
            try:
                statuses = inspect_steamcmd_mods(ids)
            except OSError as exc:
                return None, str(exc)
            return sum(1 for status in statuses.values() if status.needs_download), ""

        def done(result) -> None:
            count, error = result
            self._workshop_status_refreshing = False
            if gen != self._refresh_gen:
                return
            self._steamcmd_pending_count = count
            self._workshop_status_error = error
            self._workshop_status_checked_at = time.monotonic()
            self._update_workshop_update_hint()

        run_async(work, done, lambda _exc: setattr(self, "_workshop_status_refreshing", False))

    def _update_workshop_update_hint(self) -> None:
        if self._workshop_update_running or not self._mods_loaded:
            self._update_hint_label.setVisible(False)
            return
        if self._workshop_status_error:
            self._update_hint_label.setText(t("mod.update_status_unavailable_hint"))
            self._update_hint_label.setStyleSheet(f"color: {theme.hex('ERROR')};")
        else:
            if self._steamcmd_pending_count is not None:
                count = self._steamcmd_pending_count
            else:
                count = sum(1 for status in self._workshop_status_cache.values() if status.needs_action)
            if count:
                self._update_hint_label.setText(t("mod.update_pending_hint", count=count))
                self._update_hint_label.setStyleSheet(f"color: {theme.hex('ACCENT')};")
            elif self._workshop_status_cache or self._steamcmd_pending_count is not None:
                self._update_hint_label.setText(t("mod.update_all_current_hint"))
                self._update_hint_label.setStyleSheet("color: #2E7D32;")
            else:
                self._update_hint_label.setVisible(False)
                return
        self._update_hint_label.setVisible(True)

    def _uses_steamcmd(self) -> bool:
        """Steam 平台且 Steam 通道为 SteamCMD；WeGame 平台不受 Steam 通道影响。"""
        from dstools.features.local_service.steam_backend import SteamBackend, get_backend
        return self.ctx.platform == Platform.STEAM and get_backend() is SteamBackend.STEAMCMD

    def _refresh_steamcmd_cleanup_btn(self) -> None:
        steamcmd = self._uses_steamcmd()
        self._steamcmd_cleanup_btn.setVisible(steamcmd)
        # Steam 客户端通道在工坊网页订阅即可，只有 SteamCMD 通道需要按 ID 下载
        self._download_by_id_btn.setVisible(steamcmd)
        # 这两个按钮操作的是游戏客户端的 mods 目录，SteamCMD 通道一般没有客户端，专服直接读 SteamCMD 工坊目录
        self._location_change_btn.setVisible(not steamcmd)
        self._sync_btn.setVisible(not steamcmd)

    def _open_steamcmd_cleanup_dialog(self) -> None:
        from dstools.qt.steamcmd_mod_cleanup_dialog import SteamcmdModCleanupDialog
        if SteamcmdModCleanupDialog(self.window(), self.ctx.clusters()).exec():
            self.ctx.workshop_mods_changed.emit()

    def _open_workshop_update_dialog(self) -> None:
        if self._uses_steamcmd():
            # SteamCMD 通道：全局更新已下载的和所有服务器存档用到的工坊 Mod（V1、V2 都按 Web API 判断）
            from dstools.qt.steamcmd_mod_update_dialog import SteamcmdModUpdateDialog
            SteamcmdModUpdateDialog(self).exec()
            return
        from dstools.qt.mod_workshop_update_dialog import WorkshopUpdateDialog
        WorkshopUpdateDialog(self).exec()

    def _update_workshop_mods(self, ids: list[int], expected_versions=None, force_redownload_ids=None,
                              on_item_start=None, on_line=None, on_finish=None) -> None:
        """后台调用 SteamUGC 更新，供 Workshop 更新弹窗复用（弹窗负责进度展示）。

        ``on_finish`` 默认是本页的 _finish_workshop_update，更新弹窗可传入自己的包装版本。"""
        from dstools.features.mod.workshop_api import WorkshopUpdateCancelled, update_workshop_items
        if self._workshop_update_running:
            return
        finish = on_finish or self._finish_workshop_update
        self._workshop_update_running = True
        self._refresh_workshop_update_button_state()
        cancel_event = threading.Event()

        def worker():
            try:
                batch = update_workshop_items(
                    ids, expected_versions=expected_versions or {}, force_redownload_ids=force_redownload_ids or set(),
                    # 接每个 Mod 开始时的事件；下载进度回调每 0.2 秒一次，接它会刷出大量重复行
                    on_item_start=lambda cur, total, wid: on_item_start and post_to_ui(
                        lambda _b: on_item_start(cur, total, wid)),
                    on_item_complete=lambda cur, total, result: on_line and post_to_ui(
                        lambda _b: on_line(cur, total, result)),
                    cancel_event=cancel_event)
                post_to_ui(lambda _a: finish(batch.updated, batch.up_to_date, batch.failed))
            except WorkshopUpdateCancelled:
                post_to_ui(lambda _a: finish(0, 0, 0, cancelled=True))
            except Exception as exc:
                # except 块结束会自动 del 掉 exc；lambda 要等 post_to_ui 转回界面线程才
                # 执行，必须用默认参数立刻捕获当前值，不能等到那时候才引用这个名字。
                post_to_ui(lambda _a, detail=str(exc): finish(0, 0, len(ids), error=detail))

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

    def _finish_workshop_update(self, updated: int, up_to_date: int, failed: int,
                                cancelled: bool = False, error: str | None = None) -> None:
        self._workshop_update_running = False
        self._refresh_workshop_update_button_state()
        self._workshop_status_checked_at = 0.0
        if updated:
            self.ctx.mod_catalog.invalidate(Platform.STEAM)
            self._refresh_mods(full=False)
