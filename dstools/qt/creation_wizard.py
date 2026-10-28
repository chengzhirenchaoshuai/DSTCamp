"""创建服务器存档向导。

三个子页签复用主页组件：服务器配置用 ServerConfigPage（草稿存档在临时目录）、世界设置用 WorldPanel、
Mod 用 ModListPanel 与配置弹窗。
"""

import copy
import os
import webbrowser
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget, QPushButton, QStackedWidget,
    QVBoxLayout, QWidget,
)

from dstools.features.cluster_config.admin_manager import read_adminlist
from dstools.features.cluster_config.config_manager import (
    load_cluster_config, load_shard_config, save_cluster_config, save_shard_config,
    set_cluster_option, set_shard_option,
)
from dstools.features.local_service.dedicated_server import get_documents_dir
from dstools.features.mod import presets
from dstools.features.mod.icons import get_mod_icon_path, load_mod_icon_image
from dstools.features.mod.list_model import build_mod_rows, localize_mod_name, sort_mod_data
from dstools.features.mod.locations import resolve_mod_open_location
from dstools.features.mod.parser import (
    find_mod_folder, list_installed_mod_ids, parse_modinfo, resolve_wegame_client_mods_dir,
    split_installed_mod_counts,
)
from dstools.features.save_browser.cluster_copy import suggest_new_cluster_name
from dstools.features.world import creation, defaults, user_presets
from dstools.features.world.location_profiles import (
    CAVES_SHARD, DEPENDENCY_MOD_NAMES, MASTER_SHARD, MOD_HARD_DEPENDENCIES, dependency_label,
    dependent_mod_ids, find_mod_key, get_location_definition, resolve_world_location_profile,
    three_worlds_layout_available,
)
from dstools.features.world.mod_settings import (
    filter_mod_world_settings, get_mod_categories, get_mod_world_settings,
)
from dstools.features.world.reader import WorldOverride, WorldPreset
from dstools.features.world.value_sets import get_value_set, typed_override_value
from dstools.features.world.view_model import build_world_view_model
from dstools.i18n import t
from dstools.models import Cluster, ModEntry, Platform, SaveSource, Shard
from dstools.shared import app_settings
from dstools.shared.cluster_names import validate_cluster_folder_name
from dstools.shared.discovery import find_klei_root
from dstools.shared.server_ports import (
    allocate_cluster_port_values, allocate_lan_server_ports, collect_cluster_port_claims, find_port_conflicts,
    scan_udp_ports,
)
from dstools.shared.token_manager import read_token
from dstools.qt import dialogs
from dstools.qt.mod_config_dialog import open_mod_config
from dstools.qt.mod_panel import ModListPanel
from dstools.qt.mod_presets_dialogs import SavePresetDialog
from dstools.qt.pages.server_config import ServerConfigPage
from dstools.qt.theme import theme
from dstools.shared.resource_paths import bundled_resource_dir
from dstools.shared.steam_discovery import read_steam_persona_name
from dstools.qt.threads import run_async
from dstools.qt.widgets import Grip, PillTabBar, TitleButton
from dstools.qt.world_panel import WorldPanel
from dstools.qt.world_presets_dialogs import ApplyWorldPresetDialog, SaveWorldPresetDialog

FLASH_MS = 200


class _NullSignal:
    """草稿存档没有任何页面需要被通知——占位对象，接口跟真实 Qt Signal 一致。"""

    def emit(self, *args) -> None:
        pass

    def connect(self, *args) -> None:
        pass


class _DraftContext:
    """ServerConfigPage 需要的最小 ctx 接口。草稿存档从不运行、从不被内网穿透映射；
    跨存档端口冲突检查仍读取真实环境（避免新建存档和已有存档撞端口），因此持有真实 env。"""

    def __init__(self, real_ctx, draft_cluster: Cluster):
        self.env = real_ctx.env
        self._draft_cluster = draft_cluster
        self.mapping_owner = lambda cluster, shard: None
        self.cluster_config_saved = _NullSignal()
        # 全局令牌池弹窗要标出其他存档正在用的令牌；真实 ctx 的 token_uses 由本地服务器页后装上，调用时再取
        self.token_uses = lambda: real_ctx.token_uses()

    def selected_cluster(self):
        return self._draft_cluster

    def cluster_running(self, _cluster) -> bool:
        return False

    def goto_tab(self, _key: str) -> None:
        pass


class DraftServerPanel(ServerConfigPage):
    """向导"服务器配置"子页签——ServerConfigPage 指向一份临时目录里的草稿存档。"""

    def __init__(self, draft_ctx: _DraftContext, cluster_name: str):
        super().__init__(draft_ctx)
        self.stale = False
        self.load()
        self.set_cluster_name(cluster_name)

    def set_cluster_name(self, name: str) -> None:
        name = name.strip()
        if not name:
            return
        for grid in self._cluster_grids:
            editor = grid.editors.get(("NETWORK", "cluster_name"))
            if editor is not None and not editor.spec.readonly:
                editor.widget.setText(name)
                return

    def add_shard(self, shard_name: str, config=None) -> None:
        """新增分片；给出 ``config`` 时按它写 server.ini，已存在的分片也会被覆盖（固定分片编号的布局用）。"""
        cluster = self.ctx.selected_cluster()
        existing = any(s.name == shard_name for s in cluster.shards)
        if existing and config is None:
            return
        # load() 会按草稿文件重建表单，先把未落盘的表单值写进草稿，否则会被冲掉
        self.read_creation_settings()
        from dstools.shared.ini_parser import write_server_ini
        path = cluster.path / shard_name
        if config is None:
            config = creation.default_shard_config(False, shard_name, len(cluster.shards))
        if not existing:
            path.mkdir()
            cluster.shards.append(Shard(name=shard_name, path=path))
        write_server_ini(config, path / "server.ini")
        self.load()
        self._shard_combo.setCurrentText(shard_name)
        self._on_shard_picked(0)

    def remove_shard(self, shard_name: str) -> None:
        cluster = self.ctx.selected_cluster()
        target = next((s for s in cluster.shards if s.name == shard_name), None)
        if target is None:
            return
        self.read_creation_settings()  # 同 add_shard：load() 前先把表单当前值写进草稿
        (target.path / "server.ini").unlink(missing_ok=True)
        target.path.rmdir()
        cluster.shards.remove(target)
        self.load()

    def read_creation_settings(self) -> dict:
        """静默落盘当前表单值并读回给 create_world()；不做整段范围校验（留给创建时的端口检查和开服预检）。"""
        cluster = self.ctx.selected_cluster()
        config = load_cluster_config(cluster.path)
        for grid in self._cluster_grids:
            for (section, key), editor in grid.editors.items():
                if not editor.spec.readonly:
                    set_cluster_option(config, section, key, editor.get())
        save_cluster_config(config, cluster.path)
        if self._shard_grid is not None and self._shard_name:
            shard = next((s for s in cluster.shards if s.name == self._shard_name), None)
            if shard is not None:
                shard_config = load_shard_config(shard.path)
                for (section, key), editor in self._shard_grid.editors.items():
                    if not editor.spec.readonly:
                        set_shard_option(shard_config, section.removeprefix("SHARD_"), key, editor.get())
                save_shard_config(shard_config, shard.path)
        return {
            "cluster_ini": copy.deepcopy(load_cluster_config(cluster.path)),
            "shard_configs": {s.name: copy.deepcopy(load_shard_config(s.path)) for s in cluster.shards},
            "cluster_token": read_token(cluster.token_path),
            "admin_ids": tuple(read_adminlist(cluster.adminlist_path)),
            "block_ids": tuple(read_adminlist(cluster.blocklist_path)),
        }


class _LoadPresetDialog(dialogs.Dialog):
    """创建会话专用的配置集选择器——载入只影响当前创建会话的内存状态，不写任何磁盘存档。"""

    def __init__(self, wizard: "CreationWizardDialog"):
        super().__init__(wizard, t("preset.apply_dialog_title"), 420, confirm_text=t("preset.apply_btn"))
        self.wizard = wizard
        self._presets = presets.list_presets()
        self.body.addWidget(self.text_label(t("preset.apply_pick_hint")))
        self._list = QListWidget()
        self._list.setMinimumHeight(200)
        for preset in self._presets:
            self._list.addItem(f"{preset.name}（{len(preset.mods)}）")
        if self._presets:
            self._list.setCurrentRow(0)
        self.body.addWidget(self._list, 1)
        row = QHBoxLayout()
        delete_btn = dialogs.style_button(QPushButton(t("preset.delete_btn")), "danger")
        delete_btn.clicked.connect(self._delete)
        row.addWidget(delete_btn)
        row.addStretch()
        self.body.addLayout(row)
        self.add_buttons()

    def _delete(self) -> None:
        row = self._list.currentRow()
        if not 0 <= row < len(self._presets):
            return
        preset = self._presets[row]
        if not dialogs.ask_yes_no(self, t("preset.delete_btn"), t("preset.delete_confirm", name=preset.name), danger=True):
            return
        presets.delete_preset(preset.name)
        self._presets = presets.list_presets()
        self._list.clear()
        for item in self._presets:
            self._list.addItem(f"{item.name}（{len(item.mods)}）")

    def accept_if_valid(self) -> None:
        row = self._list.currentRow()
        if not 0 <= row < len(self._presets):
            return
        self.wizard._apply_preset_to_session(self._presets[row])
        self.accept()


class _WizardTitleBar(QWidget):
    """创建存档窗口的自绘标题栏：按住拖动（交给系统原生移动，支持贴边吸附），双击最大化/还原。"""

    def __init__(self, wizard: "CreationWizardDialog"):
        super().__init__()
        self._wizard = wizard
        self.setFixedHeight(32)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._wizard.windowHandle().startSystemMove()

    def mouseDoubleClickEvent(self, _event):
        self._wizard._toggle_maximize()


class _AttentionFrame(QWidget):
    """闪烁提示时盖在最上层画高亮边框：窗口外框只留 2px 边距，画在窗口底层会被内容挡住。"""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.hide()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        # 跟平时外框同宽，只换成主题主色，闪烁时像外框亮了一下
        painter.setPen(QPen(theme.color("PRIMARY"), 2))
        painter.drawRect(self.rect().adjusted(1, 1, -1, -1))


class CreationWizardDialog(QDialog):
    def __init__(self, ctx, background=None):
        super().__init__()
        self.ctx = ctx
        # 无父窗口的顶层对话框不会继承主窗口图标，任务栏和左上角都会是空白——显式设置。
        self.setWindowIcon(QIcon(str(bundled_resource_dir() / "icons" / "app" / "icon.png")))
        # 直接复用主窗口已加载的 Background（同一张 QPixmap，不重复读图）；
        # 按窗口尺寸缓存一张缩放好的成品图，重绘时只做贴图，不再每次平滑缩放。
        self._background = background
        self._bg_cache: QPixmap | None = None
        # 主窗口被本窗口模态阻挡时用户点了主窗口：自绘标题栏没有系统的标题栏闪烁，
        # 这里自己闪几下边框和标题栏（见 flash_attention）
        self._attention_ticks = 0
        self._attention_on = False
        self._attention_timer = QTimer(self, interval=70)
        self._attention_timer.timeout.connect(self._attention_step)
        self._attention_frame: _AttentionFrame | None = None
        # 标题（含任务栏显示）只写"创建服务器存档"，不带 DSTCamp 前缀。
        self.setWindowTitle(t("save.create_server_save"))
        # 默认房间名与游戏一致："{Steam 昵称}的世界"（NEWGAME_FMT），读不到昵称时跟随存档名称
        # 本机有多个 Steam 账号时用存档栏选中账号的昵称，读不到再取最近登录账号的
        account = ctx.env.current_account(Platform.STEAM)
        persona = (account.name if account else "") or read_steam_persona_name()
        self._default_room_name = t("world.creation_default_room_name", name=persona) if persona else ""
        dialogs.fit_to_screen(self, 1400, 860)
        # 无边框 + 自绘标题栏（Windows 10 原生标题栏改不了颜色）；保留 MinMax 提示以便从任务栏还原
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowMinMaxButtonsHint)

        self._plan_master: creation.WorldShardPlan | None = None
        self._plan_caves: creation.WorldShardPlan | None = None
        # 用户删掉的 Master/Caves；_reload_template() 不能再按模板把它们补回来。
        self._removed_fixed_shards: set[str] = set()
        # 关闭前确认：世界/Mod 页操作直接置 _dirty，服务器配置字段多，改为与打开时的快照比对
        self._dirty = False
        self._server_baseline: dict | None = None
        self._extra_plans: dict[str, creation.WorldShardPlan] = {}
        self._location_drafts: dict[tuple[str, str], creation.WorldShardPlan] = {}
        self._user_selected_location_shards: set[str] = set()
        self._world_profile = resolve_world_location_profile(set())
        self._mod_settings: dict = {}
        self._active_mod_settings: dict = {}
        self._mod_world_icons: dict = {}
        self._server_root: Path | None = None
        self._template_root: Path | None = None
        # 游戏生成的原版森林/洞穴完整关卡定义（location -> 计划），新增同类世界从这里复制
        self._template_plans: dict[str, creation.WorldShardPlan] = {}
        self._created_path: Path | None = None
        self._launch_requested = False
        self._selected_mod_ids: set[str] = set()
        self._mod_overrides: dict[str, dict] = {}
        self._mod_data: dict[str, ModEntry] = {}
        self._mod_infos: dict = {}
        self._mod_paths: dict = {}
        self._icon_imgs: dict = {}
        self._full_resolved_cache: dict = {}
        self._mod_scan_running = False
        self._mod_scan_generation = 0
        self._mod_scan_platform = Platform.STEAM
        self._mod_scan_client_mods_dir = None
        self._initialized_pages: set[str] = set()
        self._world_stale = False
        self._world_sub_tab = "rules"
        self._flash: tuple[str, int] | None = None
        self._flash_timer = QTimer(self, singleShot=True, interval=FLASH_MS)
        self._flash_timer.timeout.connect(self._clear_flash)

        self._draft_dir_ctx: TemporaryDirectory | None = None
        self._server_panel: DraftServerPanel | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(2, 2, 2, 2)  # 留出 2px 画外框
        outer.setSpacing(0)
        outer.addWidget(self._build_title_bar())
        content = QWidget()
        outer.addWidget(content, 1)
        root = QVBoxLayout(content)
        root.setContentsMargins(*dialogs.DIALOG_MARGINS)
        top = QHBoxLayout()
        top.addWidget(QLabel(t("world.creation_name_label")))
        self._name_edit = QLineEdit(self._default_cluster_name())
        self._name_edit.setFixedWidth(220)
        self._name_edit.textChanged.connect(self._on_name_changed)
        top.addWidget(self._name_edit)
        top.addStretch()
        root.addLayout(top)

        self._tabs = PillTabBar(
            [t("world.creation_server_tab"), t("world.creation_world_tab"), t("world.creation_mod_tab")],
            height=32, pill_height=24, font_size_key="FONT_SIZE_SM")
        self._tabs.current_changed.connect(self._on_tab_changed)
        root.addWidget(self._tabs)
        self._stack = QStackedWidget()
        root.addWidget(self._stack, 1)

        self._server_page = QWidget()
        self._world_page = QWidget()
        self._mod_page = QWidget()
        for page in (self._server_page, self._world_page, self._mod_page):
            self._stack.addWidget(page)

        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self._status_label)
        # "创建存档"是整个向导的最终动作：居中放置，字号和内边距都比普通按钮大一档。
        bottom = QHBoxLayout()
        bottom.addStretch()
        self._create_btn = QPushButton(t("world.creation_create_btn"))
        self._create_btn.setFont(theme.font("FONT_SIZE_MD", bold=True))
        self._create_btn.setStyleSheet("padding: 10px 48px;")
        self._create_btn.clicked.connect(self._create)
        bottom.addWidget(self._create_btn)
        bottom.addStretch()
        root.addLayout(bottom)

        self._ensure_page("server")

    # ── 页签懒加载 ──────────────────────────────────────────────────────
    def _on_tab_changed(self, index: int) -> None:
        self._stack.setCurrentIndex(index)
        key = ("server", "world", "mod")[index]
        self._ensure_page(key)
        if key == "world" and self._world_stale:
            self._world_stale = False
            self._reload_template(apply_profile_defaults=True)
        elif key == "world":
            self._render_world()
        elif key == "mod":
            self._render_list()

    def _ensure_page(self, key: str) -> None:
        if key in self._initialized_pages:
            return
        if key == "server":
            self._build_server_tab()
        elif key == "world":
            self._build_world_tab()
            self._reload_template()
        elif key == "mod":
            self._build_mod_tab()
        self._initialized_pages.add(key)

    def _default_cluster_name(self) -> str:
        """按服务器根目录已占用的编号给出第一个空闲的 Cluster_N 作为默认存档名。"""
        root = find_klei_root()
        if root is None:
            root = get_documents_dir() / "Klei" / "DoNotStarveTogether"
        return suggest_new_cluster_name(root, "Cluster_1")

    def _on_name_changed(self, text: str) -> None:
        self._dirty = True
        if self._server_panel is not None and not self._default_room_name:
            self._server_panel.set_cluster_name(text)

    # ── 服务器配置子页签 ────────────────────────────────────────────────
    def _build_server_tab(self) -> None:
        self._draft_dir_ctx = TemporaryDirectory(prefix=".dstools-create-server-")
        draft_root = Path(self._draft_dir_ctx.name)
        (draft_root / "Master").mkdir(parents=True)
        (draft_root / "Caves").mkdir(parents=True)
        from dstools.shared.ini_parser import write_cluster_ini, write_server_ini
        cluster_name = self._name_edit.text().strip() or "Cluster_New"
        write_cluster_ini(creation.default_cluster_config(cluster_name), draft_root / "cluster.ini")
        write_server_ini(creation.default_shard_config(True), draft_root / "Master" / "server.ini")
        write_server_ini(creation.default_shard_config(False), draft_root / "Caves" / "server.ini")
        (draft_root / "cluster_token.txt").write_text("", encoding="utf-8")
        (draft_root / "adminlist.txt").write_text("", encoding="utf-8")
        (draft_root / "blocklist.txt").write_text("", encoding="utf-8")
        draft_cluster = Cluster(
            name=cluster_name, path=draft_root, source=SaveSource.SERVER, platform=Platform.STEAM,
            shards=[Shard(name=MASTER_SHARD, path=draft_root / "Master"),
                   Shard(name=CAVES_SHARD, path=draft_root / "Caves")],
            adminlist_path=draft_root / "adminlist.txt", blocklist_path=draft_root / "blocklist.txt",
            token_path=draft_root / "cluster_token.txt")
        draft_ctx = _DraftContext(self.ctx, draft_cluster)
        self._server_panel = DraftServerPanel(draft_ctx, self._default_room_name or cluster_name)
        self._server_baseline = self._server_panel.read_creation_settings()
        layout = QVBoxLayout(self._server_page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._server_panel)

    # ── 世界设置子页签 ──────────────────────────────────────────────────
    def _build_world_tab(self) -> None:
        layout = QVBoxLayout(self._world_page)
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel(t("world.creation_world_label")))
        self._shard_combo = QComboBox()
        # 宽度跟最长的世界名走；展开列表用更不透明的底色（QSS 的 #opaquePopup 规则）。
        self._shard_combo.setObjectName("opaquePopup")
        self._shard_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self._shard_combo.addItems([MASTER_SHARD, CAVES_SHARD])
        self._shard_combo.activated.connect(self._on_shard_changed)
        toolbar.addWidget(self._shard_combo)
        toolbar.addWidget(QLabel(t("world.creation_select_world")))
        self._location_combo = QComboBox()
        self._location_combo.setObjectName("opaquePopup")
        self._location_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self._location_combo.activated.connect(self._on_location_changed)
        toolbar.addWidget(self._location_combo)
        add_btn = QPushButton(t("world.creation_add_world"))
        add_btn.clicked.connect(self._add_world)
        toolbar.addWidget(add_btn)
        self._three_worlds_btn = QPushButton(t("world.creation_three_worlds_layout"))
        self._three_worlds_btn.clicked.connect(self._apply_three_worlds_layout)
        self._three_worlds_btn.setVisible(False)
        toolbar.addWidget(self._three_worlds_btn)
        self._remove_world_btn = QPushButton(t("world.creation_remove_world"))
        self._remove_world_btn.clicked.connect(self._remove_world)
        toolbar.addWidget(self._remove_world_btn)
        self._update_remove_world_btn()
        toolbar.addStretch()
        layout.addLayout(toolbar)

        self._world_title_label = QLabel("")
        self._world_title_label.setFont(theme.font("FONT_SIZE_BASE", bold=True))
        self._world_desc_label = QLabel("")
        self._world_desc_label.setWordWrap(True)
        self._world_desc_label.setProperty("muted", True)
        layout.addWidget(self._world_title_label)
        layout.addWidget(self._world_desc_label)

        self._world_sub_tabs = PillTabBar(
            [t("world.creation_rules_tab"), t("world.creation_generation_tab")],
            height=32, pill_height=24, font_size_key="FONT_SIZE_SM")
        self._world_sub_tabs.current_changed.connect(self._on_world_sub_tab_changed)
        layout.addWidget(self._world_sub_tabs)
        self._world_stack = QStackedWidget()
        self._rules_panel = WorldPanel(editable=True, is_rule=True)
        # 新建世界尚未生成，世界生成项与世界设置一样可改（已有存档的世界设置页才只读）
        self._gen_panel = WorldPanel(editable=True, is_rule=False)
        self._rules_panel.value_clicked.connect(self._on_value_clicked)
        self._gen_panel.value_clicked.connect(
            lambda key, delta: self._on_value_clicked(key, delta, is_rule=False))
        self._world_stack.addWidget(self._rules_panel)
        self._world_stack.addWidget(self._gen_panel)
        layout.addWidget(self._world_stack, 1)

        # 游戏的自定义世界预设（与世界设置页同一套），作用于当前选中的世界；跟 Mod 子页一样放左下角
        preset_row = QHBoxLayout()
        save_preset_btn = QPushButton(t("world.creation_save_world_preset"))
        save_preset_btn.setToolTip(t("world.creation_save_world_preset_tip"))
        save_preset_btn.clicked.connect(self._save_world_preset)
        preset_row.addWidget(save_preset_btn)
        apply_preset_btn = QPushButton(t("world.creation_apply_world_preset"))
        apply_preset_btn.setToolTip(t("world.creation_apply_world_preset_tip"))
        apply_preset_btn.clicked.connect(self._apply_world_preset)
        preset_row.addWidget(apply_preset_btn)
        preset_row.addStretch()
        layout.addLayout(preset_row)

    def _on_world_sub_tab_changed(self, index: int) -> None:
        self._world_sub_tab = "rules" if index == 0 else "generation"
        self._world_stack.setCurrentIndex(index)
        self._render_world()

    def _available_locations_for_shard(self, shard: str) -> tuple[str, ...]:
        if shard in (MASTER_SHARD, CAVES_SHARD):
            return self._world_profile.available_locations(shard)
        return tuple(dict.fromkeys(self._world_profile.master_locations + self._world_profile.caves_locations))

    def _plan_for_shard(self, shard: str):
        if shard == CAVES_SHARD:
            return self._plan_caves
        if shard == MASTER_SHARD:
            return self._plan_master
        return self._extra_plans.get(shard)

    def _set_plan_for_shard(self, shard: str, plan) -> None:
        if shard == CAVES_SHARD:
            self._plan_caves = plan
        elif shard == MASTER_SHARD:
            self._plan_master = plan
        else:
            self._extra_plans[shard] = plan

    def _fresh_plan(self, location: str) -> creation.WorldShardPlan:
        """新世界的初始计划：原版森林/洞穴复制游戏生成的模板（Mod 默认值里没有它们的 task_set），其余取已核对的默认值。"""
        template = getattr(self, "_template_plans", {}).get(location)
        return copy.deepcopy(template) if template is not None else defaults.default_plan_for_location(location)

    def _switch_shard_location(self, shard: str, location: str, render: bool = True) -> None:
        if location not in self._available_locations_for_shard(shard):
            return
        current = self._plan_for_shard(shard)
        if current is not None:
            self._location_drafts[(shard, current.location)] = current
        plan = self._location_drafts.get((shard, location))
        if plan is None:
            other_plan = next((draft for (draft_shard, draft_location), draft in self._location_drafts.items()
                              if draft_shard != shard and draft_location == location), None)
            plan = copy.deepcopy(other_plan) if other_plan is not None else None
        if plan is None:
            plan = self._fresh_plan(location)
            self._location_drafts[(shard, location)] = plan
        self._set_plan_for_shard(shard, plan)
        if render:
            self._refresh_location_combo()
            self._render_world()

    def _refresh_location_combo(self) -> None:
        shard = self._shard_combo.currentText() or MASTER_SHARD
        locations = self._available_locations_for_shard(shard)
        self._location_combo.blockSignals(True)
        self._location_combo.clear()
        for location in locations:
            self._location_combo.addItem(get_location_definition(location).name_zh, location)
        plan = self._plan_for_shard(shard)
        location = plan.location if plan else self._world_profile.default_location(shard)
        index = self._location_combo.findData(location)
        if index >= 0:
            self._location_combo.setCurrentIndex(index)
        self._location_combo.blockSignals(False)

    def _live_shard_names(self) -> list[str]:
        """当前保留的全部世界（下拉框顺序）。"""
        return [self._shard_combo.itemText(i) for i in range(self._shard_combo.count())]

    def _live_fixed_shards(self) -> list[str]:
        return [shard for shard in (MASTER_SHARD, CAVES_SHARD) if shard not in self._removed_fixed_shards]

    def _update_remove_world_btn(self) -> None:
        # 任何世界都可以删，只剩最后一个世界时不能再删。
        self._remove_world_btn.setEnabled(self._shard_combo.count() > 1)

    def _on_shard_changed(self, _index: int) -> None:
        self._refresh_location_combo()
        self._update_remove_world_btn()
        self._render_world()

    def _on_location_changed(self, _index: int) -> None:
        shard = self._shard_combo.currentText()
        location = self._location_combo.currentData()
        if not location:
            return
        self._dirty = True
        self._user_selected_location_shards.add(shard)
        self._switch_shard_location(shard, location)

    def _next_extra_shard_name(self, location: str) -> str:
        base = {"forest": "Forest", "cave": "Caves", "porkland": "Porkland",
               "shipwrecked": "Shipwrecked", "volcanoworld": "Volcano"}.get(location, "World")
        occupied = {MASTER_SHARD, CAVES_SHARD, *self._extra_plans}
        if base not in occupied:
            return base
        index = 2
        while f"{base}_{index}" in occupied:
            index += 1
        return f"{base}_{index}"

    def _add_world(self) -> None:
        locations = tuple(dict.fromkeys(self._world_profile.master_locations + self._world_profile.caves_locations))
        if not locations:
            return
        choices = [(t("world.creation_surface_world") if loc == "forest" else get_location_definition(loc).name_zh, loc)
                  for loc in locations]
        location = dialogs.ask_choice(self, t("world.creation_add_world_title"), t("world.creation_add_world_prompt"),
                                      choices, default=locations[0], min_width=420)
        if not location:
            return
        self._dirty = True
        shard_name = self._next_extra_shard_name(location)
        plan = self._fresh_plan(location)
        self._extra_plans[shard_name] = plan
        self._location_drafts[(shard_name, location)] = plan
        if self._server_panel is not None:
            self._server_panel.add_shard(shard_name)
        self._shard_combo.addItem(shard_name)
        self._shard_combo.setCurrentText(shard_name)
        self._update_remove_world_btn()
        self._refresh_location_combo()
        self._render_world()

    def _remove_world(self) -> None:
        shard_name = self._shard_combo.currentText()
        if self._shard_combo.count() <= 1 or not shard_name:
            return
        if not dialogs.ask_yes_no(self, t("world.creation_remove_world"),
                                  t("world.creation_remove_world_confirm", name=shard_name), danger=True):
            return
        self._dirty = True
        self._discard_shard(shard_name)
        self._shard_combo.setCurrentIndex(0)
        self._update_remove_world_btn()
        self._refresh_location_combo()
        self._render_world()

    def _discard_shard(self, shard_name: str) -> None:
        """移除一个世界的计划、草稿、服务器配置和下拉项（不确认、不刷新界面）。"""
        if shard_name == MASTER_SHARD:
            self._plan_master = None
            self._removed_fixed_shards.add(shard_name)
        elif shard_name == CAVES_SHARD:
            self._plan_caves = None
            self._removed_fixed_shards.add(shard_name)
        else:
            self._extra_plans.pop(shard_name, None)
        for key in [key for key in self._location_drafts if key[0] == shard_name]:
            self._location_drafts.pop(key, None)
        self._user_selected_location_shards.discard(shard_name)
        if self._server_panel is not None:
            self._server_panel.remove_shard(shard_name)
        index = self._shard_combo.findText(shard_name)
        if index >= 0:
            self._shard_combo.removeItem(index)

    def _apply_three_worlds_layout(self) -> None:
        """按三合一整合版说明的专服五分片重建世界列表，分片编号固定（世界分组暂停依赖它）。"""
        if not dialogs.ask_yes_no(self, t("world.creation_three_worlds_layout"),
                                  t("world.creation_three_worlds_confirm")):
            return
        self._dirty = True
        for shard_name in list(self._extra_plans):
            self._discard_shard(shard_name)
        for shard_name, plan, config in defaults.three_worlds_layout():
            if shard_name in (MASTER_SHARD, CAVES_SHARD):
                restored = shard_name in self._removed_fixed_shards
                if restored:
                    self._removed_fixed_shards.discard(shard_name)
                    position = 0 if shard_name == MASTER_SHARD else int(self._shard_combo.findText(MASTER_SHARD) >= 0)
                    self._shard_combo.insertItem(position, shard_name)
                # 固定槽位沿用现有计划（优先官方模板），只切换世界类型
                self._switch_shard_location(shard_name, plan.location, render=False)
                self._user_selected_location_shards.add(shard_name)
                # 已有的 Master 保留用户填写的 server.ini，只有被删后恢复的才写默认值
                if shard_name == MASTER_SHARD and not restored:
                    continue
            else:
                self._extra_plans[shard_name] = plan
                self._location_drafts[(shard_name, plan.location)] = plan
                self._shard_combo.addItem(shard_name)
            if self._server_panel is not None:
                self._server_panel.add_shard(shard_name, config)
        self._shard_combo.setCurrentIndex(0)
        self._update_remove_world_btn()
        self._refresh_location_combo()
        self._render_world()

    def _reload_template(self, apply_profile_defaults: bool = False) -> None:
        try:
            root = find_klei_root()
            if root is None:
                root = get_documents_dir() / "Klei" / "DoNotStarveTogether"
            self._server_root = root
            try:
                template_root = defaults.find_verified_template(root, "forest")
                template_plans = defaults.default_plans_from_cluster(template_root)
                self._template_root = template_root
                self._template_plans = {plan.location: plan for plan in template_plans}
            except FileNotFoundError:
                template_plans = (defaults.default_plan_for_location("forest"), defaults.default_plan_for_location("cave"))
                self._template_root = None
                self._template_plans = {}
            missing = [(shard, plan) for shard, plan in zip((MASTER_SHARD, CAVES_SHARD), template_plans)
                       if shard not in self._removed_fixed_shards and self._plan_for_shard(shard) is None]
            for shard, plan in missing:
                self._set_plan_for_shard(shard, plan)
                self._location_drafts[(shard, plan.location)] = plan
            if missing:
                apply_profile_defaults = True

            profile = resolve_world_location_profile(self._selected_mod_ids)
            profile_changed = profile.effective_mod_ids != self._world_profile.effective_mod_ids
            self._world_profile = profile
            self._three_worlds_btn.setVisible(three_worlds_layout_available(profile.effective_mod_ids))
            if apply_profile_defaults and profile_changed:
                for shard in self._live_fixed_shards():
                    if shard not in self._user_selected_location_shards:
                        self._switch_shard_location(shard, profile.default_location(shard), render=False)
            for shard in self._live_fixed_shards():
                plan = self._plan_for_shard(shard)
                if plan and plan.location not in profile.available_locations(shard):
                    self._switch_shard_location(shard, profile.default_location(shard), render=False)

            self._mod_settings = get_mod_world_settings(profile.effective_mod_ids)
            self._refresh_location_combo()
            self._render_world()
            self._status_label.setText(profile.warnings[0] if profile.warnings else "")
        except Exception as exc:
            self._status_label.setText(str(exc))

    def _active_preset(self):
        return self._plan_for_shard(self._shard_combo.currentText())

    def _render_world(self) -> None:
        plan = self._active_preset()
        if not plan:
            return
        preset = WorldPreset(preset_id=plan.preset_id, name=plan.name, description=plan.description,
                             location=plan.location,
                             overrides=[WorldOverride(key, value if isinstance(value, str) else str(value))
                                        for key, value in plan.overrides.items()])
        is_master = self._shard_combo.currentText() == MASTER_SHARD
        self._active_mod_settings = filter_mod_world_settings(self._mod_settings, preset.location, is_master)
        mod_categories = get_mod_categories(self._active_mod_settings)
        view = build_world_view_model(preset, self._active_mod_settings, mod_categories, is_master_world=is_master)
        self._world_title_label.setText(f"{plan.name} ({plan.preset_id})")
        self._world_desc_label.setText(plan.description or "")
        if self._world_sub_tab == "rules":
            self._rules_panel.set_data(view.rule_categories, view.rules_by_category, preset.location,
                                       self._active_mod_settings, self._mod_world_icons)
        else:
            self._gen_panel.set_data(view.generation_categories, view.generation_by_category, preset.location,
                                     self._active_mod_settings, self._mod_world_icons)

    def _on_value_clicked(self, key: str, delta: int, is_rule: bool = True) -> None:
        plan = self._active_preset()
        if not plan:
            return
        self._dirty = True
        values = get_value_set(key, self._active_mod_settings, location=plan.location, is_rule=is_rule)
        # 模板里没有的 Mod 项从其登记的初始值起步（不一定有 "default" 档）；模板值可能是数字/布尔
        mod_info = self._active_mod_settings.get(key)
        raw = plan.overrides.get(key, mod_info.initial_value if mod_info else "default")
        current = raw if isinstance(raw, str) else str(raw)
        idx = values.index(current) if current in values else 0
        plan.overrides[key] = typed_override_value(values[max(0, min(len(values) - 1, idx + delta))], values)
        (self._rules_panel if is_rule else self._gen_panel).set_flash((key, delta))
        self._flash_timer.start()
        self._render_world()

    def _clear_flash(self) -> None:
        self._rules_panel.set_flash(None)
        self._gen_panel.set_flash(None)

    def _preset_user_dir(self, title: str) -> Path | None:
        """自定义预设在账号目录下；向导创建的是 Steam 服务器存档，跟存档栏选中的 Steam 账号走。"""
        account = self.ctx.env.current_account(Platform.STEAM)
        if account is None or not account.path.is_dir():
            dialogs.show_warning(self, title, t("world.preset_no_account"))
            return None
        return account.path

    def _apply_world_preset(self) -> None:
        plan = self._active_preset()
        title = t("world.creation_apply_world_preset")
        user_dir = self._preset_user_dir(title) if plan else None
        if user_dir is None:
            return
        dialog = ApplyWorldPresetDialog(self, user_dir, plan.location, get_location_definition(plan.location).name_zh)
        if dialog.exec() != dialogs.Dialog.DialogCode.Accepted or dialog.selected is None:
            return
        # 计划里的取值直接写进 leveldataoverride.lua，数字、布尔保持预设里的原类型即可
        values, skipped = user_presets.applicable_overrides(dialog.selected.overrides, plan.location,
                                                            self._active_mod_settings)
        changed = [key for key, value in values.items() if plan.overrides.get(key) != value]
        plan.overrides.update(values)
        if changed:
            self._dirty = True
        self._render_world()
        message = t("world.preset_applied_creation", name=dialog.selected.name, count=len(changed))
        if skipped:
            message += t("world.preset_applied_skipped", count=len(skipped), keys="、".join(skipped))
        dialogs.show_info(self, title, message)

    def _save_world_preset(self) -> None:
        plan = self._active_preset()
        title = t("world.creation_save_world_preset")
        user_dir = self._preset_user_dir(title) if plan else None
        if user_dir is None:
            return
        dialog = SaveWorldPresetDialog(self, user_dir)
        if dialog.exec() != dialogs.Dialog.DialogCode.Accepted:
            return
        level_data = creation.shard_level_data(plan)
        try:
            # 未登记的 key 不知道属于哪一类，不写入（has_ocean 等内部字段游戏保存预设时同样不存）
            settings, worldgen, _unknown = user_presets.split_overrides(
                level_data["overrides"], plan.location, self._active_mod_settings)
            settings_base, worldgen_base = user_presets.resolve_base_ids(level_data, plan.location, user_dir)
            user_presets.save_preset(user_dir, dialog.name, dialog.description, settings_base, worldgen_base,
                                     settings, worldgen)
        except (OSError, ValueError, user_presets.UserPresetError) as exc:
            dialogs.show_error(self, title, str(exc))
            return
        dialogs.show_info(self, title, t("world.preset_saved", name=dialog.name))

    # ── Mod 子页签 ──────────────────────────────────────────────────────
    def _build_mod_tab(self) -> None:
        layout = QVBoxLayout(self._mod_page)
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel(t("world.creation_search_mod")))
        self._mod_filter_edit = QLineEdit()
        # 固定宽度：QLineEdit 默认横向可伸缩，右侧扫描状态文字变长/变短时会挤压
        # 搜索框，连带后面的筛选页签和按钮一起左右移动。
        self._mod_filter_edit.setFixedWidth(220)
        self._mod_filter_edit.textChanged.connect(lambda _t: self._render_list())
        filter_row.addWidget(self._mod_filter_edit)
        self._mod_filter_tabs = PillTabBar(
            [t("mod.show_all"), t("mod.show_enabled"), t("mod.show_disabled"), t("mod.show_custom")],
            # 间距参数跟主页面 Mod 管理的同一排筛选页签一致（qt/pages/mod.py）。
            height=32, pill_height=24, font_size_key="FONT_SIZE_SM", gap=2, pad=16, uniform_width=True)
        self._mod_filter_tabs.current_changed.connect(lambda _i: self._render_list())
        filter_row.addWidget(self._mod_filter_tabs)
        rescan_btn = QPushButton(t("world.creation_rescan"))
        rescan_btn.clicked.connect(lambda: self._scan_installed_mods(force=True))
        filter_row.addWidget(rescan_btn)
        # 列表列数切换（1/2/3 列），跟主页面 Mod 管理共用同一个设置。放在扫描状态文字前面，
        # 避免状态文字长短变化时带着它左右移动。
        self._mod_column_choices = app_settings.MOD_LIST_COLUMN_CHOICES
        self._mod_columns_tabs = PillTabBar(
            [t("mod.columns_option", count=n) for n in self._mod_column_choices],
            height=32, pill_height=24, font_size_key="FONT_SIZE_SM", gap=2, pad=12, uniform_width=True)
        self._mod_columns_tabs.setToolTip(t("mod.columns_hint"))
        mod_columns = app_settings.get_mod_list_columns()
        self._mod_columns_tabs.set_current_index(self._mod_column_choices.index(mod_columns))
        self._mod_columns_tabs.current_changed.connect(self._on_mod_columns_changed)
        filter_row.addWidget(self._mod_columns_tabs)
        filter_row.addStretch()
        self._mod_scan_status_label = QLabel("")
        self._mod_scan_status_label.setProperty("muted", True)
        filter_row.addWidget(self._mod_scan_status_label)
        layout.addLayout(filter_row)

        self._mod_list_panel = ModListPanel()
        self._mod_list_panel.toggle_requested.connect(self._toggle_mod)
        self._mod_list_panel.config_requested.connect(self._open_mod_config)
        self._mod_list_panel.link_requested.connect(self._open_mod_link)
        self._mod_list_panel.folder_requested.connect(self._open_mod_folder)
        self._mod_list_panel.copy_id_requested.connect(self._on_copy_id)
        self._mod_list_panel.copy_name_requested.connect(self._on_copy_name)
        self._mod_list_panel.set_column_count(mod_columns)
        layout.addWidget(self._mod_list_panel, 1)

        preset_row = QHBoxLayout()
        save_preset_btn = QPushButton(t("world.creation_save_preset"))
        save_preset_btn.clicked.connect(self._save_creation_preset)
        preset_row.addWidget(save_preset_btn)
        load_preset_btn = QPushButton(t("world.creation_load_preset"))
        load_preset_btn.clicked.connect(self._open_load_preset_dialog)
        preset_row.addWidget(load_preset_btn)
        preset_row.addStretch()
        layout.addLayout(preset_row)

        self._scan_installed_mods()

    def _on_mod_columns_changed(self, index: int) -> None:
        columns = self._mod_column_choices[index]
        app_settings.set_mod_list_columns(columns)
        self._mod_list_panel.set_column_count(columns)

    def _resolve_mod_folder_args(self, _cluster):
        platform = self.ctx.platform
        return platform, resolve_wegame_client_mods_dir(platform)

    def get_cluster(self):
        return None

    def _mark_dirty(self) -> None:
        self._status_label.setText(t("world.creation_mod_dirty_hint"))

    def config_applied(self) -> None:
        """Mod 配置窗口点了"应用"：同步到待创建的 Mod 配置。"""
        self._mark_dirty()
        self._sync_mod_overrides()
        self._render_list()

    def _sync_mod_overrides(self) -> None:
        self._mod_overrides = {}
        for mod_id, mod in self._mod_data.items():
            if not mod.enabled:
                continue
            entry = {"enabled": True}
            if mod.configuration_options:
                entry["configuration_options"] = copy.deepcopy(mod.configuration_options)
            self._mod_overrides[mod_id] = entry

    def _scan_installed_mods(self, force: bool = False) -> None:
        if self._mod_scan_running:
            return
        platform, client_mods_dir = self._resolve_mod_folder_args(None)
        self._mod_scan_platform, self._mod_scan_client_mods_dir = platform, client_mods_dir
        self._mod_scan_generation += 1
        generation = self._mod_scan_generation
        if force:
            self.ctx.mod_catalog.invalidate(platform)
        else:
            snapshot = self.ctx.mod_catalog.get(platform, client_mods_dir)
            if snapshot is not None:
                records = [(mid, snapshot.infos.get(mid), snapshot.icons.get(mid), snapshot.paths.get(mid))
                          for mid in snapshot.mod_ids]
                self._apply_mod_scan_result(generation, records, {}, None)
                self._refresh_world_icons(generation, tuple(snapshot.mod_ids), platform, client_mods_dir)
                return
        self._mod_scan_running = True
        self._mod_scan_status_label.setText(t("mod.loading"))
        self._create_btn.setEnabled(False)

        def work():
            ids, seen = [], set()
            for raw_id in list_installed_mod_ids(platform, client_mods_dir):
                mod_id = str(raw_id)
                if mod_id not in seen:
                    seen.add(mod_id)
                    ids.append(mod_id)
            records = []
            for mod_id in ids:
                folder = find_mod_folder(mod_id, platform, client_mods_dir)
                info = parse_modinfo(folder) if folder else None
                icon = None
                if info and folder:
                    try:
                        icon_path = get_mod_icon_path(info, folder, platform)
                        if icon_path and icon_path.exists():
                            icon = load_mod_icon_image(icon_path)
                    except Exception:
                        pass
                records.append((mod_id, info, icon, folder))
            self.ctx.mod_catalog.publish(
                platform, {mid: info for mid, info, _i, _f in records},
                {mid: folder for mid, _i, _icon, folder in records if folder},
                {mid: icon for mid, _i, icon, _f in records if icon is not None}, client_mods_dir)
            from dstools.features.world.mod_icons import resolve_mod_setting_icons
            installed_settings = get_mod_world_settings(ids)
            try:
                world_icons = resolve_mod_setting_icons(installed_settings, platform, client_mods_dir)
            except Exception:
                world_icons = {}
            return records, world_icons

        def done(result) -> None:
            records, world_icons = result
            self._apply_mod_scan_result(generation, records, world_icons, None)

        def error(exc: Exception) -> None:
            self._apply_mod_scan_result(generation, [], {}, exc)

        run_async(work, done, error)

    def _refresh_world_icons(self, generation: int, mod_ids: tuple, platform, client_mods_dir) -> None:
        """缓存命中路径下补齐世界设置里 Mod 图标的后台刷新，不阻塞已经出来的列表。"""
        from dstools.features.world.mod_icons import resolve_mod_setting_icons

        def work():
            settings = get_mod_world_settings(mod_ids)
            try:
                return resolve_mod_setting_icons(settings, platform, client_mods_dir)
            except Exception:
                return {}

        def done(world_icons) -> None:
            if generation != self._mod_scan_generation:
                return
            self._mod_world_icons = world_icons
            if "world" in self._initialized_pages:
                self._render_world()

        run_async(work, done, lambda _exc: None)

    def _apply_mod_scan_result(self, generation: int, records, world_icons, error) -> None:
        if generation != self._mod_scan_generation:
            return
        self._mod_scan_running = False
        self._create_btn.setEnabled(True)
        if error is not None:
            self._mod_scan_status_label.setText(t("mod.update_status_check_failed", error=str(error)))
            return
        self._mod_data.clear()
        self._mod_infos.clear()
        self._mod_paths.clear()
        self._icon_imgs.clear()
        self._mod_world_icons = world_icons
        for mod_id, info, icon, folder in records:
            self._mod_infos[mod_id] = info
            if folder is not None:
                self._mod_paths[mod_id] = folder
            configured = self._mod_overrides.get(mod_id, {})
            self._mod_data[mod_id] = ModEntry(
                workshop_id=mod_id, enabled=mod_id in self._selected_mod_ids,
                configuration_options=copy.deepcopy(configured.get("configuration_options", {})),
                name=info.name if info else "", description=info.description if info else "")
            if icon is not None:
                self._icon_imgs[mod_id] = icon
        self._ensure_mod_dependencies(show_dialog=False)
        self._mod_data = sort_mod_data(self._mod_data, self._mod_infos)
        regular, custom = split_installed_mod_counts((mid for mid, _i, _icon, _f in records), self._mod_scan_platform)
        self._mod_scan_status_label.setText(t("mod.scan_found_breakdown", regular=regular, custom=custom))
        self._render_list()
        self._resolve_mod_versions(generation)

    def _resolve_mod_versions(self, generation: int) -> None:
        """快速静态解析拿不到版本号的 Mod（version_status 仍为 pending），另起后台
        任务跑沙箱补上并刷新列表——否则首次进入时版本一直停在「版本检查中」。"""
        targets = []
        for mod_id, info in self._mod_infos.items():
            folder = self._mod_paths.get(mod_id)
            if info and folder and info.version_status == "pending":
                targets.append((mod_id, folder, info.workshop_id))
        if not targets:
            return
        platform = self._mod_scan_platform
        client_mods_dir = self._mod_scan_client_mods_dir

        def work():
            from concurrent.futures import ThreadPoolExecutor, as_completed

            from dstools.features.mod.local_version import resolve_local_version_target

            resolved = {}
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(resolve_local_version_target, target) for target in targets]
                for future in as_completed(futures):
                    try:
                        wid, normalized = future.result()
                    except Exception:
                        continue
                    resolved[wid] = normalized
            return resolved

        def done(resolved: dict) -> None:
            if generation != self._mod_scan_generation or not resolved:
                return
            for wid, normalized in resolved.items():
                info = self._mod_infos.get(wid)
                if info is None:
                    continue
                if normalized.name_status == "confirmed":
                    info.name = normalized.name
                info.version = normalized.version
                info.version_status = normalized.status
                info.version_source = normalized.source
                info.version_compatible = normalized.version_compatible
                info.version_compatible_status = normalized.compatible_status
            self.ctx.mod_catalog.publish(platform, self._mod_infos, self._mod_paths, self._icon_imgs, client_mods_dir)
            self._render_list()

        run_async(work, done, lambda _exc: None)

    def _build_mod_rows(self):
        show_map = {0: "all", 1: "enabled", 2: "disabled", 3: "custom"}
        rows = build_mod_rows(self._mod_data, self._mod_infos, self._mod_filter_edit.text(),
                              show_map[self._mod_filter_tabs.current_index()], self._mod_scan_platform,
                              show_local=False, separate_client_mods=True)
        from dstools.features.mod.parser import detect_mod_format, find_workshop_dir

        workshop_root = find_workshop_dir()
        for row in rows:
            path = self._mod_paths.get(row["workshop_id"])
            row["has_folder"] = bool(path and Path(path).is_dir())
            row["mod_format"] = None if row.get("is_local") else detect_mod_format(row["workshop_id"], workshop_root)
        return rows

    def _render_list(self) -> None:
        if not hasattr(self, "_mod_list_panel"):
            return
        self._mod_list_panel.set_rows(self._build_mod_rows(), self._icon_imgs, interactive=True)

    def _toggle_mod(self, mod_id: str) -> None:
        mod = self._mod_data.get(mod_id)
        if mod is None:
            return
        self._dirty = True
        mod.enabled = not mod.enabled
        if mod.enabled and not mod.configuration_options:
            # 还没配置过：套用默认配置（对应游戏主菜单"模组"里的全局配置）
            from dstools.features.mod.config_memory import recall_for
            mod.configuration_options = recall_for(mod_id, self._mod_infos.get(mod_id))
        if mod.enabled:
            self._selected_mod_ids.add(mod_id)
        else:
            self._selected_mod_ids.discard(mod_id)
        if not mod.enabled:
            disabled_children = []
            for child_id in dependent_mod_ids(mod_id):
                child_key = find_mod_key(self._mod_data, child_id)
                child = self._mod_data.get(child_key) if child_key else None
                if child is not None and child.enabled:
                    child.enabled = False
                    self._selected_mod_ids.discard(child_key)
                    disabled_children.append(child_id)
            if disabled_children:
                self._status_label.setText(t("mod.dependents_disabled",
                                             mods=dependency_label(disabled_children, with_id=False)))
        if not self._ensure_mod_dependencies(show_dialog=True):
            self._render_list()
            return
        self._sync_mod_overrides()
        self._world_stale = True
        self._render_list()

    def _ensure_mod_dependencies(self, show_dialog: bool) -> bool:
        """已启用的 Mod 缺硬依赖时一并启用；依赖未安装或用户拒绝时关闭该 Mod，返回是否全部满足。"""
        satisfied = True
        for child_id, dependency_ids in MOD_HARD_DEPENDENCIES.items():
            child_key = find_mod_key(self._mod_data, child_id)
            child = self._mod_data.get(child_key) if child_key else None
            if child is None or not child.enabled:
                continue
            child_name = DEPENDENCY_MOD_NAMES[child_id]
            dependency_keys = {dep: find_mod_key(self._mod_data, dep) for dep in dependency_ids}
            missing = [dep for dep, key in dependency_keys.items() if key is None]
            if missing:
                child.enabled = False
                self._selected_mod_ids.discard(child_key)
                if show_dialog:
                    dialogs.show_error(self, t("mod.dependency_required_title"),
                                       t("mod.dependency_missing", mod=child_name,
                                         dependency=dependency_label(missing)))
                satisfied = False
                continue
            disabled = [dep for dep, key in dependency_keys.items() if not self._mod_data[key].enabled]
            if disabled and show_dialog:
                if not dialogs.ask_yes_no(self, t("mod.dependency_required_title"),
                                          t("mod.dependency_required_confirm", mod=child_name,
                                            dependency=dependency_label(disabled))):
                    child.enabled = False
                    self._selected_mod_ids.discard(child_key)
                    self._status_label.setText(t("mod.dependency_enable_cancelled"))
                    satisfied = False
                    continue
            for key in dependency_keys.values():
                self._mod_data[key].enabled = True
                self._selected_mod_ids.add(key)
            if disabled and show_dialog:
                self._status_label.setText(t("mod.dependency_enabled",
                                             dependency=dependency_label(disabled, with_id=False)))
        return satisfied

    def _open_mod_config(self, mod_id: str) -> None:
        mod = self._mod_data.get(mod_id)
        info = self._mod_infos.get(mod_id)
        if not mod or not info or not (info.config_options or info.unsupported_schema):
            return
        before = copy.deepcopy(mod.configuration_options)
        open_mod_config(self, mod_id, mod, info, read_only=False, read_only_reason="")
        if mod.configuration_options != before:
            self._dirty = True

    def _open_mod_link(self, mod_id: str) -> None:
        numeric_id = str(mod_id).removeprefix("workshop-")
        if not numeric_id.isdigit():
            return
        platform, _ = self._resolve_mod_folder_args(None)
        if platform == Platform.WEGAME:
            webbrowser.open(f"https://www.wegame.com.cn/pc_game/assistant.html#/2000004/newMod/{numeric_id}")
        else:
            webbrowser.open(f"https://steamcommunity.com/sharedfiles/filedetails/?id={numeric_id}")

    def _open_mod_folder(self, mod_id: str) -> None:
        path = resolve_mod_open_location(mod_id, self._mod_paths.get(mod_id))
        if path is None:
            dialogs.show_warning(self, t("env.open_location"), t("mod.open_location_missing"))
            return
        try:
            os.startfile(str(path))
        except OSError as exc:
            dialogs.show_error(self, t("env.open_location"), str(exc))

    def _on_copy_id(self, mod_id: str) -> None:
        numeric_id = mod_id.replace("workshop-", "")
        QGuiApplication.clipboard().setText(numeric_id)
        dialogs.show_toast(self, t("mod.id_copied_toast", id=numeric_id))

    def _on_copy_name(self, mod_id: str) -> None:
        info = self._mod_infos.get(mod_id)
        name = localize_mod_name(mod_id, info.name if info else "") or mod_id
        QGuiApplication.clipboard().setText(name)
        dialogs.show_toast(self, t("mod.name_copied_toast", name=name))

    def _save_creation_preset(self) -> None:
        if self._mod_scan_running:
            dialogs.show_info(self, t("mod.preset_save_btn"), t("mod.loading"))
            return
        if not self._mod_data:
            dialogs.show_warning(self, t("mod.preset_save_btn"), t("preset.no_mods_selected_in_tab"))
            return
        SavePresetDialog(self).exec()

    def _open_load_preset_dialog(self) -> None:
        if self._mod_scan_running:
            dialogs.show_info(self, t("mod.preset_apply_btn"), t("mod.loading"))
            return
        _LoadPresetDialog(self).exec()

    def _apply_preset_to_session(self, preset) -> None:
        self._dirty = True
        self._mod_overrides = copy.deepcopy(preset.mods)
        self._selected_mod_ids = {wid for wid, saved in preset.mods.items()
                                  if isinstance(saved, dict) and bool(saved.get("enabled", True))}
        for mod_id, mod in self._mod_data.items():
            saved = preset.mods.get(mod_id, {})
            mod.enabled = bool(saved.get("enabled", False)) if saved else False
            mod.configuration_options = copy.deepcopy(saved.get("configuration_options", {})) if saved else {}
        self._ensure_mod_dependencies(show_dialog=True)
        if "world" in self._initialized_pages:
            self._reload_template(apply_profile_defaults=True)
        self._scan_installed_mods(force=False)
        self._status_label.setText(t("world.creation_preset_loaded", name=preset.name))

    # ── 创建 ────────────────────────────────────────────────────────────
    def _prepare_unique_creation_ports(self, name, destination, cluster_ini, shard_configs) -> bool:
        shard_names = (*self._live_fixed_shards(), *self._extra_plans)
        for shard_index, shard_name in enumerate(shard_names):
            shard_configs.setdefault(shard_name, creation.default_shard_config(
                shard_name == MASTER_SHARD, shard_name, max(1, shard_index)))
        planned = Cluster(name, destination, source=SaveSource.SERVER, platform=Platform.STEAM,
                          config=cluster_ini,
                          shards=[Shard(shard_name, destination / shard_name, config=shard_configs[shard_name])
                                 for shard_name in shard_names])
        # 新存档自身的端口问题（如离线模式端口越界会被游戏回退成 10999）不影响检查：claims 里已是游戏实际会用的端口
        planned_claims, _issues = collect_cluster_port_claims(planned)
        existing_claims = []
        for cluster in self.ctx.env.clusters:
            if cluster.source != SaveSource.SERVER or cluster.platform != Platform.STEAM:
                continue
            claims, _ = collect_cluster_port_claims(cluster)
            existing_claims.extend(claims)
        planned_keys = {claim.owner_key for claim in planned_claims}
        # 按现有存档汇总与新存档撞上的端口（只算新存档和现有存档之间的冲突）
        conflict_ports: dict[str, tuple[str, set[int]]] = {}
        for conflict in find_port_conflicts(existing_claims + planned_claims):
            if not any(claim.owner_key in planned_keys for claim in conflict.claims):
                continue
            for claim in conflict.claims:
                if claim.owner_key not in planned_keys:
                    conflict_ports.setdefault(claim.cluster_path, (claim.cluster_name, set()))[1].add(conflict.port)
        if not conflict_ports:
            return True
        clusters = sorted(conflict_ports.values(), key=lambda item: item[0].casefold())
        lines = [t("world.create_port_conflict_line", cluster=cluster_name,
                   ports="、".join(str(port) for port in sorted(ports)))
                 for cluster_name, ports in clusters[:8]]
        if len(clusters) > 8:
            lines.append(t("world.create_port_conflict_more", count=len(clusters) - 8))
        choice = dialogs.ask_choice(
            self, t("world.create_port_conflict_title"),
            t("world.create_port_conflict_detail", details="\n".join(lines)),
            [(t("world.allocate_ports_btn"), "allocate"), (t("dlg.cancel_btn"), "cancel"),
             (t("world.create_port_conflict_anyway_btn"), "create")],
            default="allocate", min_width=420)
        if choice not in ("allocate", "create"):  # 取消或直接关闭窗口
            return False
        if choice == "create":
            return True
        used = {claim.port for claim in existing_claims}
        scan = scan_udp_ports()
        if scan.ok:
            used.update(port for ports_for_pid in scan.ports_by_pid.values() for port in ports_for_pid)
        master_port, values = allocate_cluster_port_values(shard_names, used)
        if cluster_ini.network.get("lan_only_cluster") or cluster_ini.network.get("offline_cluster"):
            # 离线/仅局域网时游戏端口必须在 10998..11018，否则游戏会退回 10999，又撞上别的存档
            try:
                lan_ports = allocate_lan_server_ports(shard_names, used)
            except ValueError:
                dialogs.show_warning(self, t("world.create_port_conflict_title"), t("world.create_lan_ports_full"))
                return False
            for shard_name, port in lan_ports.items():
                values[shard_name]["server_port"] = port
        cluster_ini.shard["master_port"] = master_port
        for shard_name, ports_for_shard in values.items():
            config = shard_configs[shard_name]
            config.network["server_port"] = ports_for_shard["server_port"]
            config.steam["master_server_port"] = ports_for_shard["master_server_port"]
            config.steam["authentication_port"] = ports_for_shard["authentication_port"]
        return True

    def _ensure_master_shard(self, shard_configs: dict) -> bool:
        """删掉主世界后，剩下的世界里没有 is_master=true 的分片：提醒并让用户选一个
        设为主世界。只改该世界 server.ini 的 [SHARD] is_master，不改文件夹名。"""
        live = self._live_shard_names()
        if any(shard_configs.get(name) is not None and shard_configs[name].shard.get("is_master")
               for name in live):
            return True
        choice = dialogs.ask_choice(
            self, t("world.creation_no_master_title"), t("world.creation_no_master_prompt"),
            [(name, name) for name in live], default=live[0], min_width=420)
        if not choice:
            return False
        config = shard_configs.get(choice)
        if config is None:
            config = shard_configs[choice] = creation.default_shard_config(True, choice)
        config.shard["is_master"] = True
        return True

    def _create(self) -> None:
        self._ensure_page("server")
        self._ensure_page("world")
        self._ensure_page("mod")
        if self._mod_scan_running:
            self._status_label.setText(t("world.creation_mod_scanning_hint"))
            return
        if any(self._plan_for_shard(shard) is None for shard in self._live_fixed_shards()):
            dialogs.show_error(self, t("world.creation_dialog_title"), t("world.creation_no_default_plan"))
            return
        if not self._server_root and self._template_root is None:
            dialogs.show_error(self, t("world.creation_dialog_title"), t("world.creation_no_template_found"))
            return
        try:
            name = self._name_edit.text().strip()
            root = self._server_root
            if root is None:
                raise FileNotFoundError(t("world.creation_no_root"))
            name_error = validate_cluster_folder_name(name)
            if name_error == "empty":
                dialogs.show_error(self, t("world.creation_dialog_title"), t("world.creation_name_empty"))
                return
            if name_error == "invalid_chars":
                dialogs.show_error(self, t("world.creation_dialog_title"), t("world.creation_name_invalid"))
                return
            destination = root / name
            if destination.exists():
                dialogs.show_error(self, t("world.creation_dialog_title"), t("world.creation_name_exists", name=name))
                return
            self._sync_mod_overrides()
            server_settings = self._server_panel.read_creation_settings() if self._server_panel else {}
            cluster_ini = copy.deepcopy(server_settings.get("cluster_ini") or creation.default_cluster_config(name))
            shard_configs = copy.deepcopy(server_settings.get("shard_configs", {}))
            if not self._ensure_master_shard(shard_configs):
                return
            if not self._prepare_unique_creation_ports(name, destination, cluster_ini, shard_configs):
                return
            out = creation.create_world(
                creation.WorldCreationPlan(
                    name, self._plan_master, self._plan_caves, cluster_ini=cluster_ini,
                    mod_ids=frozenset(self._selected_mod_ids), mod_overrides=copy.deepcopy(self._mod_overrides),
                    shard_configs=shard_configs, cluster_token=server_settings.get("cluster_token", ""),
                    admin_ids=server_settings.get("admin_ids", ()), block_ids=server_settings.get("block_ids", ()),
                    extra_shards=copy.deepcopy(self._extra_plans)),
                root)
            self.ctx.refresh_env()
            self._created_path = out
            self._launch_requested = dialogs.ask_choice(
                self, t("world.creation_dialog_title"),
                t("world.creation_launch_now", name=name),
                [(t("dlg.yes_btn"), "launch"), (t("dlg.no_btn"), "cancel")],
                default="launch", min_width=420) == "launch"
            self.accept()
        except Exception as exc:
            dialogs.show_error(self, t("world.creation_failed_title"), str(exc))

    @property
    def created_path(self) -> Path | None:
        """创建成功后返回新建存档目录（无论是否选择启动）；主窗口据此选中该存档。"""
        return self._created_path

    @property
    def launch_requested(self) -> bool:
        """用户在「是否立即启动」里是否选了「是」。"""
        return self._launch_requested

    # ── 无边框窗口：标题栏、缩放热区、外框 ────────────────────────────────
    def _build_title_bar(self) -> QWidget:
        bar = _WizardTitleBar(self)
        self._title_bar = bar
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(10, 0, 4, 0)
        layout.setSpacing(2)
        icon = QLabel()
        icon.setPixmap(QPixmap(str(bundled_resource_dir() / "icons" / "app" / "icon.png")).scaled(
            18, 18, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        layout.addWidget(icon)
        layout.addSpacing(6)
        layout.addWidget(QLabel(self.windowTitle()))
        layout.addStretch()
        self._max_button = TitleButton("max", self.isMaximized)
        for kind, slot, button in (("min", self.showMinimized, None), ("max", self._toggle_maximize, self._max_button),
                                   ("close", self.close, None)):
            button = button or TitleButton(kind, self.isMaximized)
            button.clicked.connect(slot)
            layout.addWidget(button)
        self._grips = [Grip(self, edges, cursor) for edges, cursor in (
            (Qt.Edge.LeftEdge, Qt.CursorShape.SizeHorCursor), (Qt.Edge.RightEdge, Qt.CursorShape.SizeHorCursor),
            (Qt.Edge.TopEdge, Qt.CursorShape.SizeVerCursor), (Qt.Edge.BottomEdge, Qt.CursorShape.SizeVerCursor),
            (Qt.Edge.LeftEdge | Qt.Edge.TopEdge, Qt.CursorShape.SizeFDiagCursor),
            (Qt.Edge.RightEdge | Qt.Edge.BottomEdge, Qt.CursorShape.SizeFDiagCursor),
            (Qt.Edge.RightEdge | Qt.Edge.TopEdge, Qt.CursorShape.SizeBDiagCursor),
            (Qt.Edge.LeftEdge | Qt.Edge.BottomEdge, Qt.CursorShape.SizeBDiagCursor),
        )]
        return bar

    def _toggle_maximize(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        # setWindowFlags()/resize() 在标题栏建好前就会触发事件，先判空
        if event.type() == event.Type.WindowStateChange and hasattr(self, "_grips"):
            self._max_button.update()  # 最大化/还原图标跟着切换
            for grip in self._grips:
                grip.setVisible(not self.isMaximized())  # 最大化时不允许拖边缩放

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if not hasattr(self, "_grips"):
            return
        w, h, g, c = self.width(), self.height(), 6, 12
        rects = [
            (0, c, g, h - 2 * c), (w - g, c, g, h - 2 * c), (c, 0, w - 2 * c, g), (c, h - g, w - 2 * c, g),
            (0, 0, c, c), (w - c, h - c, c, c), (w - c, 0, c, c), (0, h - c, c, c),
        ]
        for grip, rect in zip(self._grips, rects):
            grip.setGeometry(*rect)
            grip.raise_()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), theme.color("BG_SOFT"))
        if self._background is not None and self._background.active:
            self._paint_background(painter)
        else:
            self._bg_cache = None
        if self._attention_on:
            highlight = theme.color("ACCENT")
            highlight.setAlpha(70)
            painter.fillRect(0, 0, self.width(), self._title_bar.height(), highlight)
        # 外框跟主窗口一致
        painter.setPen(QPen(theme.color("CARD_BORDER"), 2))
        painter.drawRect(self.rect().adjusted(1, 1, -1, -1))

    def flash_attention(self) -> None:
        """仿系统模态窗口被点到时的标题栏闪烁：拉到前台并闪几下边框和标题栏。"""
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()
        if self._attention_timer.isActive():
            return
        self._attention_ticks = 0
        self._attention_step()
        self._attention_timer.start()

    def _attention_step(self) -> None:
        self._attention_ticks += 1
        # 亮、灭交替 4 次，第 8 拍恢复原样
        self._attention_on = self._attention_ticks % 2 == 1 and self._attention_ticks < 8
        if self._attention_ticks >= 8:
            self._attention_timer.stop()
        if self._attention_frame is None:
            self._attention_frame = _AttentionFrame(self)
        self._attention_frame.setGeometry(self.rect())
        self._attention_frame.raise_()
        self._attention_frame.setVisible(self._attention_on)
        self.update()

    def _paint_background(self, painter: QPainter) -> None:
        dpr = self.devicePixelRatioF()
        size = self.size() * dpr
        if self._bg_cache is None or self._bg_cache.size() != size:
            cache = QPixmap(size)
            cache.setDevicePixelRatio(dpr)
            cache.fill(Qt.GlobalColor.transparent)
            cache_painter = QPainter(cache)
            self._background.paint(cache_painter, self.width(), self.height(), smooth=True)
            cache_painter.end()
            self._bg_cache = cache
        painter.drawPixmap(0, 0, self._bg_cache)

    def _has_unsaved_changes(self) -> bool:
        if self._dirty:
            return True
        if self._server_panel is None or self._server_baseline is None:
            return False
        try:
            return self._server_panel.read_creation_settings() != self._server_baseline
        except Exception:
            return True  # 读不出来就当有改动，宁可多问一次

    def reject(self) -> None:
        # 点右上角关闭（QDialog.closeEvent 会转调 reject）和按 Esc 都走这里；
        # 有改动时先确认，避免误关把辛苦配好的东西全丢掉。
        if self._has_unsaved_changes() and not dialogs.ask_yes_no(
                self, t("world.creation_discard_title"), t("world.creation_discard_confirm")):
            return
        super().reject()

    def done(self, result: int) -> None:
        # 创建成功（accept）或确认关闭（reject）最终都经过 done()，在这里清理草稿目录；
        # 不放在 closeEvent 里——用户取消关闭时窗口还开着，草稿不能先被删掉。
        super().done(result)
        if self._draft_dir_ctx is not None:
            self._draft_dir_ctx.cleanup()
            self._draft_dir_ctx = None
