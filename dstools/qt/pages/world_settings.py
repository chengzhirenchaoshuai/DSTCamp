"""世界设置页：世界规则与世界生成都可改值保存。服务器存档写 leveldataoverride.lua；本地存档写游戏真正
读取的各分片 save/shardindex 并同步 leveldataoverride.lua。游戏只在启动时读取并缓存各存档 shardindex，
存档运行时开服进程还会覆盖它：存档可能在运行时拦下，游戏开着时保存前提醒需重启游戏。世界生成只在生成
新世界时生效（已生成的世界需重置），后台加载，面板见 qt/world_panel.py。
"""

import webbrowser

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QStackedWidget, QStyle, QVBoxLayout,
)

from dstools.features.local_service.backup_manager import create_backup
from dstools.features.local_service.steam_backend import SteamBackend, get_backend
from dstools.features.world import page_data, shard_management, user_presets
from dstools.features.world.location_profiles import FOREST_LOCATION, get_location_definition
from dstools.features.mod.shardindex import (
    LOCAL_SAVE_OPEN, LOCAL_SAVE_RUNNING, ShardIndexError, local_save_play_state,
)
from dstools.i18n import get_lang, t
from dstools.qt import dialogs
from dstools.qt.pages.base import Page
from dstools.qt.theme import theme
from dstools.qt.threads import run_async
from dstools.qt.widgets import Banner, Card, PillTabBar
from dstools.qt.world_manage import ManageWorldsDialog
from dstools.qt.world_panel import WorldPanel
from dstools.qt.world_presets_dialogs import ApplyWorldPresetDialog, SaveWorldPresetDialog

FLASH_MS = 200  # 被点的箭头短暂画成"按下"效果，模仿游戏 UI 的点击反馈


class AddShardDialog(dialogs.Dialog):
    """新增世界：填文件夹名和世界名称（都预填默认值），确认前按存档当前状态校验。"""

    def __init__(self, parent, title: str, cluster, location: str, location_label: str, language: str):
        super().__init__(parent, title, "md")
        self._cluster = cluster
        self._location = location
        self._language = language
        self.shard_name = ""
        self.world_name = ""
        default_name, _others = shard_management.planned_world_names(cluster, location, language)
        if not shard_management.worldgate_enabled(cluster):
            # 原本没用万界之门的存档（常见是没开任何 Mod）会多出一个 Mod，放在最上面醒目说明
            banner = Banner()
            banner.set_text(t("world.add_shard_enable_worldgate"))
            self.body.addWidget(banner)
        self.body.addWidget(self.text_label(t("world.add_shard_intro", cluster=cluster.name,
                                              location=location_label), size_key="FONT_SIZE_BASE"))
        self._folder_edit = self._field(t("world.add_shard_folder_label"),
                                        shard_management.next_shard_name(cluster, location),
                                        t("world.add_shard_folder_hint"))
        self._name_edit = self._field(t("world.add_shard_name_label"), default_name, t("world.add_shard_name_hint"))
        self.body.addWidget(self.text_label(t("world.add_shard_notes"), muted=True))
        self._error = self.error_label()
        self.body.addWidget(self._error)
        self.add_buttons()
        self._name_edit.setFocus()
        self._name_edit.selectAll()

    def _field(self, label: str, value: str, hint: str) -> QLineEdit:
        self.body.addWidget(self.text_label(label))
        edit = QLineEdit(value)
        edit.setFont(theme.font("FONT_SIZE_BASE"))
        self.body.addWidget(edit)
        self.body.addWidget(self.text_label(hint, muted=True, size_key="FONT_SIZE_XS"))
        return edit

    def accept_if_valid(self) -> None:
        shard_name = self._folder_edit.text().strip()
        world_name = self._name_edit.text().strip()
        error = (shard_management.shard_name_error(self._cluster, shard_name)
                 or shard_management.world_name_error(self._cluster, self._location, world_name, self._language))
        if error:
            self._error.setText(error)
            return
        self.shard_name, self.world_name = shard_name, world_name
        self.accept()


class WorldSettingsPage(Page):
    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self._generation = 0
        self._shard = ""
        self._data: page_data.WorldPageData | None = None
        self._dirty = False
        self._generation_changed = False  # 本次未保存的修改是否包含世界生成项（保存后提示重置）
        self._edited_cluster_path = ""  # 当前内容来自哪个存档，用于判断重新加载时能否保留未保存改动
        self._flash_timer = QTimer(self, singleShot=True, interval=FLASH_MS)
        self._flash_timer.timeout.connect(self._clear_flash)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 12, 24, 12)
        card = Card(radius=15, alpha=0, border=True)  # 内部全透明，跟其它主页签统一
        outer.addWidget(card)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 12, 18, 12)
        layout.setSpacing(8)

        shard_row = QHBoxLayout()
        self._shard_label = QLabel()
        self._shard_label.setFont(theme.font("FONT_SIZE_BASE"))
        self._shard_combo = QComboBox()
        self._shard_combo.setMinimumWidth(180)
        self._shard_combo.activated.connect(self._on_shard_picked)
        # 新增、管理两个按钮只对专服存档显示（本地存档由游戏前端管理世界），见 on_cluster_changed
        self._add_shard_button = QPushButton()
        self._add_shard_button.clicked.connect(self._on_add_shard)
        self._manage_button = QPushButton()
        self._manage_button.clicked.connect(self._on_manage_worlds)
        shard_row.addWidget(self._shard_label)
        shard_row.addWidget(self._shard_combo)
        shard_row.addWidget(self._add_shard_button)
        shard_row.addWidget(self._manage_button)
        shard_row.addSpacing(10)
        self._unsaved_label = QLabel(t("dlg.unsaved_hint"))
        self._unsaved_label.setProperty("accent", True)
        self._unsaved_label.setVisible(False)
        shard_row.addWidget(self._unsaved_label)
        shard_row.addStretch()
        layout.addLayout(shard_row)

        self._banner = Banner()
        layout.addWidget(self._banner)
        # Mod 版本与登记表核对版本不同时的提示，和上面按存档类型变化的提示互不覆盖
        self._mod_banner = Banner()
        layout.addWidget(self._mod_banner)

        self._title = QLabel()
        self._title.setFont(theme.font("FONT_SIZE_BASE", bold=True))
        self._desc = QLabel()
        self._desc.setFont(theme.font("FONT_SIZE_XS"))
        self._desc.setProperty("muted", True)
        self._desc.setWordWrap(True)
        layout.addWidget(self._title)
        layout.addWidget(self._desc)

        self._sub_tabs = PillTabBar(["", ""], height=32, pill_height=24, font_size_key="FONT_SIZE_SM")
        self._sub_tabs.current_changed.connect(self._on_sub_tab_changed)
        layout.addWidget(self._sub_tabs)
        # 两个面板都按存档类型在加载后切换可编辑（见 _load_world）：本地存档不显示箭头
        self._rules = WorldPanel(editable=False, is_rule=True)
        self._generation_panel = WorldPanel(editable=False, is_rule=False)
        self._rules.value_clicked.connect(self._on_value_clicked)
        self._generation_panel.value_clicked.connect(
            lambda key, delta: self._on_value_clicked(key, delta, is_rule=False))
        self._stack = QStackedWidget()
        self._stack.addWidget(self._rules)
        self._stack.addWidget(self._generation_panel)
        layout.addWidget(self._stack, 1)

        # 预设按钮读写游戏的自定义世界预设，与保存按钮同一行；加载完成前不可用（见 _load_world）
        self._apply_preset_button = QPushButton()
        self._apply_preset_button.setEnabled(False)
        self._apply_preset_button.clicked.connect(self._on_apply_preset)
        self._save_preset_button = QPushButton()
        self._save_preset_button.setEnabled(False)
        self._save_preset_button.clicked.connect(self._on_save_preset)
        self._save_button = QPushButton()
        self._save_button.setEnabled(False)
        self._save_button.clicked.connect(self._on_save)
        self._discard_button = QPushButton()
        self._discard_button.setEnabled(False)
        self._discard_button.clicked.connect(self._discard_changes)
        # 跟 Mod 页一致：预设按钮在左下角，保存类按钮居中；左栏与右侧空白等宽，保证中间按钮真正居中
        button_row = QHBoxLayout()
        left_group = QHBoxLayout()
        left_group.addWidget(self._save_preset_button)
        left_group.addWidget(self._apply_preset_button)
        left_group.addStretch()
        button_row.addLayout(left_group, 1)
        button_row.addWidget(self._discard_button)
        button_row.addWidget(self._save_button)
        # 右侧只有空白时 Qt 不加它前面的间距，手动补上，否则按钮组偏左几像素
        button_row.addSpacing(self.style().pixelMetric(QStyle.PixelMetric.PM_LayoutHorizontalSpacing))
        button_row.addStretch(1)
        layout.addLayout(button_row)

        theme.changed.connect(self._on_theme_changed)
        # Mod 页的开关/保存/配置集应用都会改变"来自 Mod"这部分显示内容；这个页不一定
        # 正在显示，跟 server_config.py 同一套"正显示就重载，不然只标脏"的规则。
        ctx.cluster_config_saved.connect(self._on_cluster_config_saved_elsewhere)
        self.retranslate()
        self._update_tab_labels()

    def _on_cluster_config_saved_elsewhere(self, cluster) -> None:
        current = self.ctx.selected_cluster()
        if current is None or cluster is None or str(current.path) != str(cluster.path):
            return
        if self.isVisible():
            self.load()
        else:
            self.stale = True

    # ── 文案 ────────────────────────────────────────────────────────────
    def retranslate(self) -> None:
        self._shard_label.setText(t("world.shard"))
        self._add_shard_button.setText(t("world.add_shard_btn"))
        self._add_shard_button.setToolTip(t("world.add_shard_tip"))
        self._manage_button.setText(t("world.manage_btn"))
        self._manage_button.setToolTip(t("world.manage_tip"))
        self._save_button.setText(t("world.save_rules"))
        self._discard_button.setText(t("dlg.discard_changes_btn"))
        self._apply_preset_button.setText(t("world.preset_apply_btn"))
        self._apply_preset_button.setToolTip(t("world.preset_apply_tip"))
        self._save_preset_button.setText(t("world.preset_save_btn"))
        self._save_preset_button.setToolTip(t("world.preset_save_tip"))
        self._update_tab_labels()
        self._update_banner()

    def _on_theme_changed(self) -> None:
        for widget, key, bold in ((self._shard_label, "FONT_SIZE_BASE", False),
                                  (self._title, "FONT_SIZE_BASE", True), (self._desc, "FONT_SIZE_XS", False)):
            widget.setFont(theme.font(key, bold))
        self._rules.refresh_values()
        self._generation_panel.refresh_values()

    def _update_tab_labels(self) -> None:
        data = self._data
        # 不再标注可改/仅查看：本地存档只读由上方横幅说明，面板也不显示箭头
        rules, generation = t("world.rules"), t("world.generation")
        if data is not None and data.status == page_data.STATUS_OK:
            rules += f" ({sum(len(v) for v in data.rules_by_category.values())})"
            generation += f" ({sum(len(v) for v in data.generation_by_category.values())})"
        self._sub_tabs.set_labels([rules, generation])

    def _selected_is_server(self) -> bool:
        from dstools.models import SaveSource
        cluster = self.ctx.selected_cluster()
        return bool(cluster and cluster.source == SaveSource.SERVER)

    def _update_banner(self) -> None:
        cluster = self.ctx.selected_cluster()
        if cluster is None:
            self._banner.set_text(t("world.no_save_banner"))
        elif not self._selected_is_server():
            data = self._data
            if data is not None and data.local_index_error:
                self._banner.set_text(t("world.local_index_unreadable", detail=data.local_index_error))
            elif self._stack.currentIndex() == 1:
                self._banner.set_text(t("world.generation_reset_banner_local"))
            else:
                self._banner.set_text(t("world.local_edit_banner"))
        elif self._stack.currentIndex() == 1:
            self._banner.set_text(t("world.generation_reset_banner"))
        else:
            self._banner.set_text("")

    def _on_sub_tab_changed(self, index: int) -> None:
        self._stack.setCurrentIndex(index)
        self._update_banner()

    # ── 加载 ────────────────────────────────────────────────────────────
    def on_cluster_changed(self, cluster) -> None:
        names = [s.name for s in cluster.shards] if cluster is not None else []
        # 还是同一个存档（别处刷新了存档列表、Mod 页保存等）：保留未保存的改动，不从磁盘重读
        keep = self._dirty and cluster is not None and str(cluster.path) == self._edited_cluster_path \
            and self._shard in names
        self._shard_combo.blockSignals(True)
        self._shard_combo.clear()
        self._shard_combo.addItems(names)
        if not keep:
            self._shard = "Master" if "Master" in names else (names[0] if names else "")
        self._shard_combo.setCurrentText(self._shard)
        self._shard_combo.blockSignals(False)
        self._add_shard_button.setVisible(self._selected_is_server())
        self._manage_button.setVisible(self._selected_is_server())
        if not keep:
            self._load_world()

    def _on_shard_picked(self, _index: int) -> None:
        name = self._shard_combo.currentText()
        if self._dirty:
            if name == self._shard:
                return
            choice = dialogs.ask_unsaved(self.window(), t("tab.world").strip())
            if choice == "cancel" or (choice == "save" and not self.save_unsaved_changes()):
                self._shard_combo.setCurrentText(self._shard)
                return
        self._shard = name
        self._load_world()

    def _on_add_shard(self) -> None:
        """给专服存档新增一个世界：选类型 → 确认 → 后台备份并写分片目录 → 刷新并选中新世界。"""
        cluster = self.ctx.selected_cluster()
        if cluster is None or not self._selected_is_server():
            return
        title = t("world.add_shard_btn")
        if self.ctx.cluster_running(cluster):
            dialogs.show_warning(self.window(), title, t("world.add_shard_running"))
            return
        if not shard_management.worldgate_installed():
            self._guide_worldgate(title)
            return
        try:
            locations = shard_management.available_locations(cluster)
        except ValueError as exc:
            dialogs.show_error(self.window(), title, str(exc))
            return
        if not locations:
            return
        language = get_lang()
        choices = [(t("world.creation_surface_world") if loc == FOREST_LOCATION
                    else get_location_definition(loc).name(language), loc) for loc in locations]
        location = dialogs.ask_choice(self.window(), title, t("world.add_shard_prompt"),
                                      choices, default=locations[0], min_width=420)
        if not location:
            return
        label = dict((loc, text) for text, loc in choices)[location]
        try:
            dialog = AddShardDialog(self.window(), title, cluster, location, label, language)
        except (OSError, ValueError) as exc:  # 读不了已有世界的配置
            dialogs.show_error(self.window(), title, str(exc))
            return
        if dialog.exec() != dialogs.Dialog.DialogCode.Accepted:
            return
        shard_name, world_name = dialog.shard_name, dialog.world_name
        klei_root = self.ctx.env.klei_root_for(cluster.platform)
        clusters = list(self.ctx.env.clusters)
        self._add_shard_button.setEnabled(False)

        def work() -> None:
            plan = shard_management.initial_plan(cluster, location, klei_root)
            # 先校验再备份，避免明知不能加还留下一份备份
            shard_management.validate_new_shard(cluster, shard_name, plan)
            create_backup(cluster.path, keep=True)
            shard_management.add_shard(cluster, shard_name, plan,
                                     shard_management.used_ports_elsewhere(clusters, cluster), language, world_name)

        def done(_result) -> None:
            self._add_shard_button.setEnabled(True)
            self.ctx.refresh_env()
            index = self._shard_combo.findText(shard_name)
            if index >= 0:
                self._shard_combo.setCurrentIndex(index)
                self._on_shard_picked(index)
            dialogs.show_info(self.window(), title, t("world.add_shard_done", shard=world_name))

        def failed(exc: Exception) -> None:
            self._add_shard_button.setEnabled(True)
            dialogs.show_error(self.window(), title, str(exc))

        run_async(work, done, failed)

    def _on_manage_worlds(self) -> None:
        if self._selected_is_server():
            ManageWorldsDialog(self.window(), self.ctx).exec()

    def _guide_worldgate(self, title: str) -> None:
        """本机还没有万界之门：先引导订阅（SteamCMD 通道直接下载），装好后再新增世界。"""
        steamcmd = get_backend() is SteamBackend.STEAMCMD
        action = t("mod.recommend_download" if steamcmd else "mod.recommend_subscribe")
        prompt = t("world.add_shard_need_worldgate_steamcmd" if steamcmd else "world.add_shard_need_worldgate")
        choice = dialogs.ask_choice(self.window(), title, prompt,
                                    [(action, "go"), (t("dlg.cancel_btn"), "cancel")], default="go", min_width=420)
        if choice != "go":
            return
        if steamcmd:
            from dstools.qt.steamcmd_mods import run_steamcmd_mod_update
            run_steamcmd_mod_update(self.window(), [shard_management.WORLDGATE_MOD_ID],
                                    on_finished=self.ctx.workshop_mods_changed.emit)
        else:
            webbrowser.open(f"https://steamcommunity.com/sharedfiles/filedetails/?id={shard_management.WORLDGATE_MOD_ID}")

    def _load_world(self) -> None:
        self._generation += 1
        generation = self._generation
        self._data = None
        self._mod_banner.set_text("")
        self._set_dirty(False)
        self._apply_preset_button.setEnabled(False)
        self._save_preset_button.setEnabled(False)
        self._rules.clear()
        self._generation_panel.clear()
        self._update_banner()
        self._update_tab_labels()
        cluster = self.ctx.selected_cluster()
        self._edited_cluster_path = str(cluster.path) if cluster is not None else ""
        if cluster is None:
            self._show_info("", "")
            return
        self._show_info(t("save.loading"), "")
        shard = self._shard

        def done(data: page_data.WorldPageData) -> None:
            if generation != self._generation:
                return
            self._data = data
            # 本地存档也可改（写游戏真正读取的 shardindex），读不了 shardindex 时只读
            self._rules.set_editable(data.can_save)
            self._generation_panel.set_editable(data.can_save)
            if data.status == page_data.STATUS_OK:
                preset = data.preset
                self._show_info(f"{preset.name} ({preset.preset_id})   {data.location_label}",
                                preset.description or "")
                self._rules.set_data(data.rule_categories, data.rules_by_category, data.location,
                                     data.mod_settings, data.mod_icons)
                self._generation_panel.set_data(data.generation_categories, data.generation_by_category,
                                                data.location, data.mod_settings, data.mod_icons)
                self._save_preset_button.setEnabled(True)
                self._apply_preset_button.setEnabled(data.can_save)
                if data.unverified_mods:
                    mods = "、".join(t("world.mod_unverified_item", name=name, installed=installed, verified=verified)
                                    for name, installed, verified in data.unverified_mods)
                    self._mod_banner.set_text(t("world.mod_unverified_banner", mods=mods))
            elif data.status == page_data.STATUS_INVALID:
                # 具体异常文本一并显示，方便远程排障（编码问题、空文件、语法错误显示的不一样）
                detail = t("world.invalid_leveldata_detail", detail=data.error) if data.error else ""
                self._show_info(t("world.invalid_leveldata"), detail)
            else:
                self._show_info(t("world.no_leveldata"), "")
            self._update_banner()
            self._update_tab_labels()

        # 在界面线程先取 Mod 页未保存的预览集合（它读 Mod 页内存字典，在后台线程读会与开关点击竞态）
        enabled_mod_ids = self.ctx.pending_enabled_mod_ids(cluster)
        run_async(lambda: page_data.load_world_page(cluster, shard, enabled_mod_ids), done,
                  lambda exc: self._show_info(str(exc), "") if generation == self._generation else None)

    def _show_info(self, title: str, desc: str) -> None:
        self._title.setText(title)
        self._desc.setText(desc)
        self._desc.setVisible(bool(desc))

    # ── 编辑 ────────────────────────────────────────────────────────────
    def _set_dirty(self, dirty: bool) -> None:
        changed = dirty != self._dirty
        self._dirty = dirty
        if not dirty:
            self._generation_changed = False
        self._save_button.setEnabled(dirty)
        self._discard_button.setEnabled(dirty)
        self._unsaved_label.setVisible(dirty)
        if changed:
            self.ctx.unsaved_changed.emit()

    def _discard_changes(self) -> None:
        """"取消修改"按钮：确认后丢掉未保存的修改，从磁盘重新读取当前世界。"""
        if not self._dirty or not dialogs.ask_yes_no(self.window(), t("dlg.discard_changes_btn"),
                                                     t("world.discard_changes_confirm")):
            return
        self._load_world()  # 重新读取时会清掉未保存标记

    def has_unsaved_changes(self) -> bool:
        return self._dirty

    def discard_unsaved_changes(self) -> None:
        # 不立即重读：切换存档会照常重新加载；其余情况标为过期，切回本页时再从磁盘读
        self._set_dirty(False)
        self.stale = True

    def save_unsaved_changes(self) -> bool:
        """离开前保存：用户已在询问框选了"保存"，不再二次确认；需要重置世界时照常提示。"""
        data = self._data
        title = t("world.save_rules")
        if data is None or not data.can_save or data.preset is None or data.path is None:
            dialogs.show_warning(self.window(), title, t("world.no_preset"))
            return False
        if not data.is_server:
            try:
                state = local_save_play_state(self.ctx.selected_cluster())
            except Exception as exc:
                dialogs.show_error(self.window(), title, str(exc))
                return False
            if state == LOCAL_SAVE_RUNNING:
                dialogs.show_warning(self.window(), title, t("world.local_save_running"))
                return False
            if state == LOCAL_SAVE_OPEN and not dialogs.ask_yes_no(
                    self.window(), title, t("world.local_save_game_open_confirm", name=data.shard_name)):
                return False
        try:
            page_data.save_world_page(data)
        except (OSError, ValueError, ShardIndexError) as exc:
            dialogs.show_error(self.window(), title, str(exc))
            return False
        need_reset = self._generation_changed and page_data.generation_requires_reset(data)
        self._set_dirty(False)
        if need_reset:
            dialogs.show_info(self.window(), t("dlg.save_ok"),
                              t("world.saved_need_reset" if data.is_server else "world.saved_need_reset_local"))
        return True

    def _on_value_clicked(self, key: str, delta: int, is_rule: bool = True) -> None:
        data = self._data
        # 只读兜底：不可保存时面板不显示箭头，正常点不到这里，这里再挡一道防止别的路径漏调
        if data is None or not data.can_save:
            return
        page_data.step_rule_value(data, key, delta, is_rule=is_rule)
        self._set_dirty(True)
        if not is_rule:
            self._generation_changed = True
        (self._rules if is_rule else self._generation_panel).set_flash((key, delta))
        self._flash_timer.start()

    def _clear_flash(self) -> None:
        self._rules.set_flash(None)
        self._generation_panel.set_flash(None)

    # ── 自定义预设 ──────────────────────────────────────────────────────
    def _preset_user_dir(self, title: str):
        """当前账号目录（游戏把自定义预设存在账号目录下）；没有时提示并返回 None。"""
        cluster = self.ctx.selected_cluster()
        account = self.ctx.env.current_account(cluster.platform) if cluster is not None else None
        if account is None or not account.path.is_dir():
            dialogs.show_warning(self.window(), title, t("world.preset_no_account"))
            return None
        return account.path

    def _on_save_preset(self) -> None:
        data = self._data
        title = t("world.preset_save_btn")
        if data is None or data.status != page_data.STATUS_OK:
            return
        user_dir = self._preset_user_dir(title)
        if user_dir is None:
            return
        dialog = SaveWorldPresetDialog(self.window(), user_dir)
        if dialog.exec() != dialogs.Dialog.DialogCode.Accepted:
            return
        try:
            table = page_data.current_override_table(data)
            # 未登记的 key 不知道属于哪一类，不写入（has_ocean 等内部字段游戏保存预设时同样不存）
            settings, worldgen, _unknown = user_presets.split_overrides(table, data.location, data.mod_settings)
            settings_base, worldgen_base = user_presets.resolve_base_ids(data.preset.raw, data.location, user_dir)
            user_presets.save_preset(user_dir, dialog.name, dialog.description, settings_base, worldgen_base,
                                     settings, worldgen)
        except (OSError, ValueError, ShardIndexError, user_presets.UserPresetError) as exc:
            dialogs.show_error(self.window(), title, str(exc))
            return
        dialogs.show_info(self.window(), title, t("world.preset_saved", name=dialog.name))

    def _on_apply_preset(self) -> None:
        data = self._data
        title = t("world.preset_apply_btn")
        if data is None or not data.can_save:
            return
        user_dir = self._preset_user_dir(title)
        if user_dir is None:
            return
        dialog = ApplyWorldPresetDialog(self.window(), user_dir, data.location, data.location_label)
        if dialog.exec() != dialogs.Dialog.DialogCode.Accepted or dialog.selected is None or data is not self._data:
            return
        preset = dialog.selected
        changed, skipped = page_data.apply_preset_values(data, preset.overrides)
        self._rules.set_data(data.rule_categories, data.rules_by_category, data.location,
                             data.mod_settings, data.mod_icons)
        self._generation_panel.set_data(data.generation_categories, data.generation_by_category,
                                        data.location, data.mod_settings, data.mod_icons)
        self._update_tab_labels()
        if changed:
            self._set_dirty(True)
            if any(key in preset.worldgen for key in changed):
                self._generation_changed = True
        message = t("world.preset_applied", name=preset.name, count=len(changed))
        if skipped:
            message += t("world.preset_applied_skipped", count=len(skipped), keys="、".join(skipped))
        dialogs.show_info(self.window(), title, message)

    def _on_save(self) -> None:
        data = self._data
        if data is None or not data.can_save:
            return
        title = t("world.save_rules")
        if data.preset is None or data.path is None:
            dialogs.show_info(self.window(), title, t("world.no_preset"))
            return
        if data.is_server:
            self._confirm_and_save(data, t("dlg.confirm_save_msg", name=data.shard_name))
            return
        # 本地存档：游戏只在启动时读各存档 shardindex 并一直缓存，存档运行时开服进程自动存档还会覆盖它。
        # 进程/端口检测要跑 tasklist、netstat，放后台
        cluster = self.ctx.selected_cluster()
        self._save_button.setEnabled(False)

        def done(state: str) -> None:
            self._save_button.setEnabled(self._dirty)
            if data is not self._data:
                return
            if state == LOCAL_SAVE_RUNNING:
                dialogs.show_warning(self.window(), title, t("world.local_save_running"))
                return
            key = "world.local_save_game_open_confirm" if state == LOCAL_SAVE_OPEN else "dlg.confirm_save_msg"
            self._confirm_and_save(data, t(key, name=data.shard_name))

        def failed(exc: Exception) -> None:
            self._save_button.setEnabled(self._dirty)
            dialogs.show_error(self.window(), title, str(exc))

        run_async(lambda: local_save_play_state(cluster), done, failed)

    def _confirm_and_save(self, data: page_data.WorldPageData, confirm_text: str) -> None:
        title = t("world.save_rules")
        if not dialogs.ask_yes_no(self.window(), title, confirm_text):
            return
        try:
            page_data.save_world_page(data)
        except (OSError, ValueError, ShardIndexError) as exc:
            dialogs.show_error(self.window(), title, str(exc))
            return
        need_reset = self._generation_changed and page_data.generation_requires_reset(data)
        self._set_dirty(False)
        if not need_reset:
            message = t("world.saved")
        else:
            message = t("world.saved_need_reset" if data.is_server else "world.saved_need_reset_local")
        dialogs.show_info(self.window(), t("dlg.save_ok"), message)
