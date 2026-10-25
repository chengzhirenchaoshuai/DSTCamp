"""服务器配置页：房间设置（cluster.ini）、世界设置（各世界 server.ini）、管理员、黑名单、服务器令牌。

取值/校验/端口冲突检查在 features/cluster_config 的纯逻辑模块中；本地存档由游戏客户端管理，房间设置只开放
名称、描述、密码、人数、PVP（写入 Master shardindex，见 local_server_data.py），其余只读展示。
"""

import webbrowser

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QListWidget, QPlainTextEdit, QPushButton, QScrollArea,
    QStackedWidget, QVBoxLayout, QWidget,
)

from dstools.features.cluster_config import form_logic, save_checks
from dstools.features.cluster_config.admin_manager import add_admin, remove_admin
from dstools.features.cluster_config.config_manager import save_cluster_config, save_shard_config
from dstools.features.cluster_config.local_server_data import (
    load_local_server_data, merge_local_values, save_local_server_data, validate_local_values,
)
from dstools.features.mod.shardindex import (
    LOCAL_SAVE_OPEN, LOCAL_SAVE_RUNNING, ShardIndexError, local_save_play_state,
)
from dstools.features.save_browser.reader import list_known_player_ids, refresh_player_registry
from dstools.i18n import t
from dstools.models import SaveSource
from dstools.qt import dialogs
from dstools.qt.forms import FormGrid
from dstools.qt.pages.base import Page
from dstools.qt.theme import theme
from dstools.qt.threads import run_async
from dstools.qt.widgets import Banner, Card, PillTabBar
from dstools.shared.server_ports import (
    collect_cluster_port_claims, format_lan_port_issues, lan_restriction_names,
    rewrite_lan_server_ports_atomic, scan_udp_ports,
)
from dstools.shared.token_manager import KLEI_SERVERS_URL, is_valid_token, mask_token, write_token



def _muted(text: str = "", size_key: str = "FONT_SIZE_SM") -> QLabel:
    label = QLabel(text)
    label.setFont(theme.font(size_key))
    label.setProperty("muted", True)
    label.setWordWrap(True)
    return label


def _scroll(inner: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.viewport().setAutoFillBackground(False)
    area.setWidget(inner)
    inner.setAutoFillBackground(False)  # QScrollArea.setWidget 会把它改成 True，必须在之后再关
    return area


class IdListPanel(QWidget):
    """管理员/黑名单共用："每行一个 Klei ID"的纯文本文件，区别只在路径存在 Cluster 的哪个属性上。"""

    def __init__(self, ctx, title_key: str, path_attr: str, default_filename: str):
        super().__init__()
        self._ctx, self._title_key = ctx, title_key
        self._path_attr, self._default_filename = path_attr, default_filename
        self._row_ids: list[str | None] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)
        self._title = QLabel()
        self._title.setFont(theme.font("FONT_SIZE_BASE", bold=True))
        self._list = QListWidget()
        self._list.setFont(theme.font("FONT_SIZE_SM"))
        # 管理员/黑名单列表全透明透出背景图（只覆盖这里，其他列表仍需全局的半透明底色）；
        # 去掉列表自身的边框与焦点框，切页时也不自动抢焦点
        self._list.setStyleSheet("QListWidget { background: transparent; border: none; outline: 0; }")
        self._list.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self._list.itemSelectionChanged.connect(self._sync_remove_state)
        row = QHBoxLayout()
        self._add = QPushButton()
        self._remove = QPushButton()
        self._pick = QPushButton()
        self._add.clicked.connect(self._on_add)
        self._remove.clicked.connect(self._on_remove)
        self._pick.clicked.connect(self._on_pick)
        for button in (self._add, self._remove, self._pick):
            row.addWidget(button)
        row.addStretch()
        self._status = _muted()
        layout.addWidget(self._title)
        layout.addWidget(self._list, 1)
        layout.addLayout(row)
        layout.addWidget(self._status)
        self.retranslate()

    def retranslate(self) -> None:
        self._title.setText(t(self._title_key))
        self._add.setText(t("admin.add"))
        self._remove.setText(t("admin.remove"))
        self._pick.setText(t("admin.pick_from_save"))

    def load(self, cluster) -> None:
        id_list = form_logic.load_id_list(cluster, self._path_attr)
        self._list.clear()
        self._list.addItems(id_list.labels)
        self._row_ids = id_list.row_ids
        self._sync_remove_state()

    def _sync_remove_state(self) -> None:
        row = self._list.currentRow()
        # 选中提示行/空状态行（没有真实 ID）时不能删，避免用户点了却没反应
        self._remove.setEnabled(0 <= row < len(self._row_ids) and self._row_ids[row] is not None)

    def _cluster(self):
        return self._ctx.selected_cluster()

    def _on_add(self) -> None:
        cluster = self._cluster()
        if cluster is None:
            return
        dialog = dialogs.TextInputDialog(self.window(), t("admin.add"), t("admin.add_prompt"))
        if not dialog.exec() or not dialog.result_text:
            return
        kid = dialog.result_text.strip()
        if not form_logic.is_valid_dst_user_id(kid):  # 简单校验 KU_/OU_ 前缀，防止手滑填入游戏不认识的 ID
            self._status.setText(t("admin.invalid_format"))
            return
        self._commit_add(cluster, kid)

    def _on_pick(self) -> None:
        cluster = self._cluster()
        if cluster is None:
            return

        def work():
            # 点"从存档选择"是主动更新登记簿的时机：把本机所有存档的日志合并进跨存档共享的登记簿
            refresh_player_registry([s for other in self._ctx.env.clusters for s in other.shards])
            return list_known_player_ids(cluster.shards)

        def done(candidates) -> None:
            if not candidates:
                dialogs.show_info(self.window(), t("admin.pick_save_title"), t("admin.pick_save_empty"))
                return
            dialog = dialogs.SaveUserPickDialog(self.window(), candidates)
            if dialog.exec() and dialog.result_id:
                self._commit_add(cluster, dialog.result_id)

        run_async(work, done, lambda exc: dialogs.show_error(self.window(), t("admin.pick_save_title"), str(exc)))

    def _commit_add(self, cluster, kid: str) -> None:
        path = getattr(cluster, self._path_attr) or (cluster.path / self._default_filename)
        if add_admin(path, kid):
            setattr(cluster, self._path_attr, path)
            self._status.setText(t("admin.added", id=kid))
        else:
            self._status.setText(t("admin.already_exists"))
            dialogs.show_info(self.window(), t("admin.duplicate_title"), t("admin.already_exists_msg", id=kid))
        self.load(cluster)

    def _on_remove(self) -> None:
        cluster = self._cluster()
        path = getattr(cluster, self._path_attr, None) if cluster else None
        row = self._list.currentRow()
        if not path or not 0 <= row < len(self._row_ids) or not self._row_ids[row]:
            return
        kid = self._row_ids[row]
        if remove_admin(path, kid):
            self._status.setText(t("admin.removed", id=kid))
        self.load(cluster)


class TokenPanel(QWidget):
    """服务器令牌：脱敏显示/显示完整、复制、更换、申请，以及全局令牌池入口。"""

    def __init__(self, ctx):
        super().__init__()
        self._ctx = ctx
        self._raw = ""
        self._visible = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        self._title = QLabel()
        self._title.setFont(theme.font("FONT_SIZE_SM", bold=True))
        self._display = QPlainTextEdit()
        self._display.setReadOnly(True)
        self._display.setFont(theme.font("FONT_SIZE_SM"))
        self._display.setFixedHeight(self._display.fontMetrics().lineSpacing() * 3 + 18)
        row = QHBoxLayout()
        self._show, self._copy = QPushButton(), QPushButton()
        self._change, self._apply = QPushButton(), QPushButton()
        self._show.clicked.connect(self._toggle)
        self._copy.clicked.connect(self._on_copy)
        self._change.clicked.connect(self._on_change)
        self._apply.clicked.connect(lambda: webbrowser.open(KLEI_SERVERS_URL))
        for button in (self._show, self._copy, self._change):
            row.addWidget(button)
        row.addStretch()
        row.addWidget(self._apply)
        # 全局令牌池：所有存档共享，"复制为服务器存档"新建存档时自动取第一个填上
        pool_row = QHBoxLayout()
        self._pool = QPushButton()
        self._pool.clicked.connect(self._on_pool)
        pool_row.addWidget(self._pool)
        pool_row.addStretch()
        self._hint = _muted(size_key="FONT_SIZE_XS")
        self._hint.setMaximumWidth(560)
        layout.addWidget(self._title)
        layout.addWidget(self._display)
        layout.addLayout(row)
        layout.addSpacing(10)
        layout.addLayout(pool_row)
        layout.addWidget(self._hint)
        layout.addStretch()
        self.retranslate()

    def retranslate(self) -> None:
        self._title.setText(t("token.current_title"))
        self._show.setText(t("token.hide") if self._visible else t("token.show"))
        self._copy.setText(t("token.copy"))
        self._change.setText(t("token.change"))
        self._apply.setText(t("token.apply"))
        self._pool.setText(t("token.set_global_btn"))
        self._hint.setText(t("token.global_hint"))

    def load(self, cluster) -> None:
        self._visible = False
        self._raw = form_logic.load_cluster_token(cluster)
        self._render()

    def _render(self) -> None:
        if not self._raw:
            text = t("token.empty")
        else:
            text = self._raw if self._visible else mask_token(self._raw)
        self._display.setPlainText(text)
        self._show.setText(t("token.hide") if self._visible and self._raw else t("token.show"))

    def _toggle(self) -> None:
        self._visible = not self._visible
        self._render()

    def _on_copy(self) -> None:
        if self._raw:
            QGuiApplication.clipboard().setText(self._raw)
            dialogs.show_info(self.window(), "", t("token.copied"))

    def _on_change(self) -> None:
        cluster = self._ctx.selected_cluster()
        if cluster is None:
            return
        dialog = dialogs.TextInputDialog(
            self.window(), t("token.change"), t("token.prompt"),
            validator=lambda value: None if is_valid_token(value) else t("token.invalid_hint"))
        if not dialog.exec() or dialog.result_text is None:
            return
        # cluster_token.txt 可能还不存在（离线/本地存档通常没有），write_token 会自己创建
        path = cluster.token_path or (cluster.path / "cluster_token.txt")
        write_token(path, dialog.result_text)
        cluster.token_path = path
        self.load(cluster)

    def _on_pool(self) -> None:
        dialog = dialogs.GlobalTokensDialog(self.window(), token_uses=self._ctx.token_uses())
        dialog.exec()
        cluster = self._ctx.selected_cluster()
        if dialog.result_token is not None and cluster:
            path = cluster.token_path or (cluster.path / "cluster_token.txt")
            write_token(path, dialog.result_token)
            cluster.token_path = path
        if cluster:  # 关掉弹窗后当前存档若还缺令牌，load 会顺手从刚设置好的令牌池自动补上
            self.load(cluster)


class ServerConfigPage(Page):
    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self._cluster_grids: list[FormGrid] = []
        self._shard_form: form_logic.ShardForm | None = None
        self._shard_grid: FormGrid | None = None
        self._shard_name = ""

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 12, 24, 12)
        # 房间设置/世界设置两个子页签内部还各自套了一层 Card（见下方 column/card），
        # 内部全透明（不画底色）；外层这圈边框跟其它主页签统一。
        card = Card(radius=15, alpha=0, border=True)
        outer.addWidget(card)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(15, 13, 15, 13)
        layout.setSpacing(6)
        self._tabs = PillTabBar([""] * 5, height=36, pill_height=28, font_size_key="FONT_SIZE_BASE")
        layout.addWidget(self._tabs)
        self._banner = Banner()
        layout.addWidget(self._banner)
        self._stack = QStackedWidget()
        layout.addWidget(self._stack, 1)
        self._tabs.current_changed.connect(self._stack.setCurrentIndex)

        # 1) 房间设置（cluster.ini）
        self._cluster_inner = QWidget()
        self._cluster_layout = QHBoxLayout(self._cluster_inner)
        self._cluster_layout.setContentsMargins(4, 4, 4, 4)
        self._cluster_layout.setSpacing(16)
        self._cluster_save = QPushButton()
        self._cluster_save.clicked.connect(self._save_cluster_ini)
        self._cluster_scroll = _scroll(self._cluster_inner)
        self._stack.addWidget(self._page_with_save(self._cluster_scroll, self._cluster_save))

        # 2) 世界设置（server.ini）
        shard_holder = QWidget()
        shard_layout = QVBoxLayout(shard_holder)
        shard_layout.setContentsMargins(0, 0, 0, 0)
        selector = QHBoxLayout()
        self._shard_label = QLabel()
        self._shard_label.setFont(theme.font("FONT_SIZE_SM"))
        self._shard_combo = QComboBox()
        self._shard_combo.setMinimumWidth(180)
        self._shard_combo.activated.connect(self._on_shard_picked)
        selector.addWidget(self._shard_label)
        selector.addWidget(self._shard_combo)
        selector.addStretch()
        shard_layout.addLayout(selector)
        self._shard_inner = QWidget()
        self._shard_inner_layout = QVBoxLayout(self._shard_inner)
        self._shard_inner_layout.setContentsMargins(4, 4, 4, 4)
        self._shard_scroll = _scroll(self._shard_inner)
        shard_layout.addWidget(self._shard_scroll, 1)
        self._shard_save = QPushButton()
        self._shard_save.clicked.connect(self._save_shard_ini)
        self._stack.addWidget(self._page_with_save(shard_holder, self._shard_save))

        # 3) 管理员 4) 黑名单 5) 令牌
        self._admin = IdListPanel(ctx, "admin.title", "adminlist_path", "adminlist.txt")
        self._block = IdListPanel(ctx, "blocklist.title", "blocklist_path", "blocklist.txt")
        self._token = TokenPanel(ctx)
        for widget in (self._admin, self._block, self._token):
            self._stack.addWidget(widget)

        # 房间设置/世界设置：常规排版放不下可视高度时切到紧凑排版（见 _update_compact）
        self._cluster_normal_h = 0
        self._shard_normal_h = 0
        for scroll in (self._cluster_scroll, self._shard_scroll):
            scroll.viewport().installEventFilter(self)

        theme.changed.connect(self._on_theme_changed)
        # 内网穿透页开关映射会改变 server_port 的可编辑性；正显示就立即重载，否则标脏
        ctx.cluster_config_saved.connect(self._on_cluster_config_saved_elsewhere)
        self.retranslate()

    def _on_cluster_config_saved_elsewhere(self, cluster) -> None:
        # 只标脏不重载：本页保存时也会触发此槽，并自行做"保留当前世界选择"的精确刷新，整页 load()
        # 会把世界下拉框重置回 Master；外部页触发时本页必然不在前台，标脏即可
        current = self.ctx.selected_cluster()
        if current is not None and cluster is not None and str(current.path) == str(cluster.path):
            self.stale = True

    def eventFilter(self, watched, event) -> bool:
        if event.type() == QEvent.Type.Resize and watched in (
                self._cluster_scroll.viewport(), self._shard_scroll.viewport()):
            self._update_compact()
        return super().eventFilter(watched, event)

    @staticmethod
    def _normal_height(inner: QWidget) -> int:
        """表单刚建好（常规排版）时的内容高度；之后按它判断，不随紧凑与否变化，避免来回切换。"""
        layout = inner.layout()
        for index in range(layout.count()):
            widget = layout.itemAt(index).widget()
            # 刚加进可见布局的控件要等下一轮事件循环才显示，此前布局不计它的高度
            if widget is not None and not widget.testAttribute(Qt.WidgetAttribute.WA_WState_ExplicitShowHide):
                widget.show()
        inner.ensurePolished()
        return inner.sizeHint().height()

    def _update_compact(self) -> None:
        cluster_compact = self._cluster_normal_h > self._cluster_scroll.viewport().height()
        for grid in self._cluster_grids:
            self._set_grid_compact(grid, cluster_compact)
        if self._shard_grid is not None:
            self._set_grid_compact(self._shard_grid, self._shard_normal_h > self._shard_scroll.viewport().height())

    @staticmethod
    def _set_grid_compact(grid: FormGrid, compact: bool) -> None:
        """表单本身收紧之外，外面那层卡片的上下内边距也一起收窄。"""
        grid.set_compact(compact)
        card_layout = grid.parentWidget().layout()
        card_layout.setContentsMargins(14, 4 if compact else 8, 14, 6 if compact else 12)

    @staticmethod
    def _page_with_save(content: QWidget, save_button: QPushButton) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(content, 1)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(save_button)
        layout.addLayout(row)
        return page

    # ── 文案 / 主题 ─────────────────────────────────────────────────────
    def retranslate(self) -> None:
        self._tabs.set_labels([
            t("cluster.tab_cluster_ini"), t("cluster.shard_config"),
            t("admin.title"), t("blocklist.title"), t("token.title"),
        ])
        self._shard_label.setText(t("save.shard"))
        self._cluster_save.setText(t("cluster.save_btn"))
        self._shard_save.setText(t("cluster.save_btn"))
        self._admin.retranslate()
        self._block.retranslate()
        self._token.retranslate()

    def _on_theme_changed(self) -> None:
        if self.isVisible():
            self.load()  # 表单控件的字体是构造时设的，字体样式可能变了：整页重建
        else:
            self.stale = True

    # ── 加载 ────────────────────────────────────────────────────────────
    def on_cluster_changed(self, cluster) -> None:
        if cluster is None:
            self._banner.set_text(t("cluster.no_save_banner"))
            self._clear_cluster_form()
            self._clear_shard_form()
            self._shard_combo.clear()
            for button in (self._cluster_save, self._shard_save):
                button.setEnabled(False)
            self._admin._list.clear()
            self._block._list.clear()
            return
        is_server = cluster.source == SaveSource.SERVER
        local_server = None
        if is_server:
            self._banner.set_text("")
        else:
            # 本地存档的房间设置真正存在 Master shardindex 的 server 表；读不了时整页只读
            try:
                local_server = load_local_server_data(cluster.path)
                self._banner.set_text(t("cluster.local_edit_banner"))
            except ShardIndexError as exc:
                self._banner.set_text(t("cluster.local_index_unreadable", detail=exc))
        self._cluster_save.setEnabled(is_server or local_server is not None)
        self._shard_save.setEnabled(is_server)
        self._rebuild_cluster_form(cluster, local_server)
        self._shard_combo.blockSignals(True)
        self._shard_combo.clear()
        names = [s.name for s in cluster.shards]
        self._shard_combo.addItems(names)
        self._shard_name = "Master" if "Master" in names else (names[0] if names else "")
        self._shard_combo.setCurrentText(self._shard_name)
        self._shard_combo.blockSignals(False)
        self._rebuild_shard_form()
        self._admin.load(cluster)
        self._block.load(cluster)
        self._token.load(cluster)

    def _clear_cluster_form(self) -> None:
        while self._cluster_layout.count():
            widget = self._cluster_layout.takeAt(0).widget()
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        self._cluster_grids = []

    def _rebuild_cluster_form(self, cluster, local_server: dict | None = None) -> None:
        self._clear_cluster_form()
        for groups in form_logic.build_cluster_columns(cluster, local_server):
            column = Card(radius=18, alpha=0, border_key="PRIMARY", border=True)
            column_layout = QVBoxLayout(column)
            column_layout.setContentsMargins(14, 8, 14, 12)
            grid = FormGrid()
            for group in groups:
                grid.add_header(group.title)
                for spec in group.fields:
                    grid.add_field(spec)
            column_layout.addWidget(grid)
            column_layout.addStretch()
            self._cluster_layout.addWidget(column, 1)
            self._cluster_grids.append(grid)
        self._cluster_normal_h = self._normal_height(self._cluster_inner)
        self._update_compact()

    def _cluster_values(self) -> dict:
        values: dict = {}
        for grid in self._cluster_grids:
            values.update(grid.values())
        return values

    # ── 世界设置 ────────────────────────────────────────────────────────
    def _on_shard_picked(self, _index: int) -> None:
        self._shard_name = self._shard_combo.currentText()
        self._rebuild_shard_form()

    def _clear_shard_form(self) -> None:
        while self._shard_inner_layout.count():
            widget = self._shard_inner_layout.takeAt(0).widget()
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        self._shard_grid = None
        self._shard_form = None

    def _rebuild_shard_form(self) -> None:
        self._clear_shard_form()
        cluster = self.ctx.selected_cluster()
        shard = next((s for s in cluster.shards if s.name == self._shard_name), None) if cluster else None
        if shard is None:
            return
        self._shard_form = form_logic.ShardForm(cluster, shard)
        self._render_shard_form()

    def _render_shard_form(self) -> None:
        form = self._shard_form
        while self._shard_inner_layout.count():
            widget = self._shard_inner_layout.takeAt(0).widget()
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        card = Card(radius=18, alpha=0, border_key="PRIMARY", border=True)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(14, 8, 14, 12)
        grid = FormGrid()
        grid.add_header(t("cluster.editing", shard=form.shard.name), size_key="FONT_SIZE_SM", heading=False, top_gap=2)
        for section, fields in form.groups(self.ctx.mapping_owner):
            grid.add_header(f"[{section}]", size_key="FONT_SIZE_XS", heading=False, top_gap=6)
            for spec in fields:
                is_master_field = form.is_server and spec.section == "SHARD_SHARD" and spec.key == "is_master"
                # 世界名称/编号内容很短，输入框固定为与端口输入框相近的宽度
                is_short_text = spec.section == "SHARD_SHARD" and spec.key in ("name", "id")
                grid.add_field(spec, on_toggled=self._on_is_master_toggled if is_master_field else None,
                               fixed_width=160 if is_short_text else None)
        card_layout.addWidget(grid)
        card_layout.addStretch()
        self._shard_inner_layout.addWidget(card)
        self._shard_inner_layout.addStretch()
        self._shard_grid = grid
        self._shard_normal_h = self._normal_height(self._shard_inner)
        self._update_compact()

    def _on_is_master_toggled(self, _checked: bool) -> None:
        # 延到下一个事件循环再重画：别在开关自己的点击回调里销毁开关所在的行
        QTimer.singleShot(0, self._apply_is_master_toggle)

    def _apply_is_master_toggle(self) -> None:
        if self._shard_form is None or self._shard_grid is None:
            return
        values = self._shard_grid.values()
        self._shard_form.snapshot(values)
        self._shard_form.set_is_master(bool(values.get(("SHARD_SHARD", "is_master"), True)))
        self._render_shard_form()

    # ── 保存：房间设置 ──────────────────────────────────────────────────
    def _window(self):
        return self.window()

    def _save_cluster_ini(self) -> None:
        cluster = self.ctx.selected_cluster()
        if cluster is None:
            return
        values = self._cluster_values()
        if cluster.source != SaveSource.SERVER:
            self._save_local_cluster(cluster, values)
            return
        # 所有带范围的字段先整体校验，任何一个越界就中止保存、什么都不写
        error = save_checks.validate_ranges(values, shard=False)
        if error:
            dialogs.show_error(self._window(), t("dlg.save_ok"), error)
            return
        config = save_checks.build_cluster_config(cluster, values)
        _, port_issues = collect_cluster_port_claims(cluster, cluster_config_override=config)
        lan_issues = [issue for issue in port_issues if issue.code == "lan_server_port_range"]
        if lan_issues:
            locked = save_checks.cluster_ports_locked(
                cluster, self.ctx.cluster_running, lambda c, s: bool(self.ctx.mapping_owner(c, s)))
            if not locked:
                self._repair_lan_ports(cluster, config, lan_issues)
                return
            # 运行中或有端口映射时不能改端口；用户选择关闭 LAN 限制后 config 已就地改好，继续常规保存
            if not self._resolve_lan_lock(cluster, config, lan_issues):
                return
        conflict = save_checks.find_cluster_port_conflict(cluster, config)
        if conflict:
            dialogs.show_error(self._window(), t("dlg.save_fail"), conflict)
            return
        cross = save_checks.find_cross_cluster_cluster_port_conflicts(cluster, config, self.ctx.env.clusters)
        if cross and not dialogs.ask_yes_no(
                self._window(), t("cluster.cross_cluster_port_title"),
                t("cluster.cross_cluster_port_confirm", details="\n".join(cross[:12])), min_width=780):
            return
        save_cluster_config(config, cluster.path)
        self._after_cluster_saved(cluster)

    def _after_cluster_saved(self, cluster) -> None:
        dialogs.show_info(self._window(), t("dlg.save_ok"), t("dlg.config_saved", name=cluster.name))
        # cluster_password 会拼进"本地服务器"页的直连代码，改了要通知它刷新
        self.ctx.cluster_config_saved.emit(cluster)
        previous_shard = self._shard_name
        self.on_cluster_changed(cluster)
        if previous_shard in [s.name for s in cluster.shards]:  # 不能因为保存房间设置就把正在看的世界切回 Master
            self._shard_combo.setCurrentText(previous_shard)
            self._on_shard_picked(0)

    def _save_local_cluster(self, cluster, values) -> None:
        """本地存档：先写游戏真正读取的 Master shardindex 的 server 表，再同步写 cluster.ini 保持一致。
        游戏只在回到开始页面时重读 shardindex，存档运行时开服进程自动存档还会覆盖它，进程检测放后台。"""
        title = t("cluster.save_btn")
        error = validate_local_values(values)
        if error:
            dialogs.show_error(self._window(), title, error)
            return
        self._cluster_save.setEnabled(False)

        def done(state: str) -> None:
            self._cluster_save.setEnabled(True)
            current = self.ctx.selected_cluster()
            if current is None or str(current.path) != str(cluster.path):  # 检测期间已切到别的存档
                return
            if state == LOCAL_SAVE_RUNNING:
                dialogs.show_warning(self._window(), title, t("cluster.local_save_running"))
                return
            if state == LOCAL_SAVE_OPEN and not dialogs.ask_yes_no(
                    self._window(), title, t("cluster.local_save_game_open_confirm", name=cluster.name)):
                return
            try:
                server = merge_local_values(load_local_server_data(cluster.path), values)
                save_local_server_data(cluster.path, server)
                save_cluster_config(save_checks.build_cluster_config(cluster, values), cluster.path)
            except (OSError, ShardIndexError) as exc:
                dialogs.show_error(self._window(), t("dlg.save_fail"), str(exc))
                return
            self._after_cluster_saved(cluster)

        def failed(exc: Exception) -> None:
            self._cluster_save.setEnabled(True)
            dialogs.show_error(self._window(), title, str(exc))

        run_async(lambda: local_save_play_state(cluster), done, failed)

    def _resolve_lan_lock(self, cluster, config, issues) -> bool:
        """存档运行中或有映射、无法改端口时，说明原因并给出出路。返回 True 表示用户选择关闭 LAN 限制。"""
        modes = "、".join(lan_restriction_names(cluster, config)) or "离线模式"
        details = format_lan_port_issues(issues)
        title, outcome = t("lan_conflict.title"), t("lan_conflict.outcome_save")
        if self.ctx.cluster_running(cluster):
            dialogs.show_error(
                self._window(), title,
                t("lan_conflict.running_msg", modes=modes, details=details, outcome=outcome), min_width=780)
            return False
        choice = dialogs.ask_choice(
            self._window(), title,
            t("lan_conflict.mapping_msg", modes=modes, details=details, outcome=outcome),
            [(t("lan_conflict.disable_save_btn", modes=modes), "disable"),
             (t("lan_conflict.goto_mapping_btn"), "goto"), (t("dlg.cancel_btn"), "cancel")],
            default="cancel", min_width=780)
        if choice == "goto":
            self.ctx.goto_tab("sakura")
            return False
        if choice != "disable":
            return False
        config.network["lan_only_cluster"] = False
        config.network["offline_cluster"] = False
        return True

    def _repair_lan_ports(self, cluster, config, issues) -> bool:
        """保存 LAN/离线开关时修复全部世界端口；成功时已完成整组写入。调用方保证存档没运行、没映射。"""
        scan = scan_udp_ports()
        if not scan.ok:
            dialogs.show_error(self._window(), t("dlg.save_fail"), t("cluster.lan_port_scan_failed", detail=scan.error))
            return False
        choice = dialogs.ask_choice(
            self._window(), t("cluster.lan_port_title"),
            t("cluster.lan_port_confirm", details=save_checks.format_port_issues(issues)),
            [(t("cluster.allocate_lan_ports_btn"), "allocate"), (t("dlg.cancel_btn"), "cancel")],
            default="cancel", min_width=780)
        if choice != "allocate":
            return False
        try:
            values = rewrite_lan_server_ports_atomic(
                cluster, save_checks.used_ports_for_lan_repair(cluster, scan, self.ctx.env.clusters),
                cluster_config_override=config)
        except (OSError, ValueError) as exc:
            dialogs.show_error(self._window(), t("dlg.save_fail"),
                               t("local.port_repair_failed", detail=f"{type(exc).__name__}: {exc}"))
            return False
        details = "\n".join(f"{name}: server_port={port}" for name, port in values.items())
        dialogs.show_info(self._window(), t("dlg.save_ok"), t("cluster.lan_port_repair_done", details=details))
        self.on_cluster_changed(cluster)
        return True

    # ── 保存：世界设置 ──────────────────────────────────────────────────
    def _save_shard_ini(self) -> None:
        cluster = self.ctx.selected_cluster()
        form = self._shard_form
        if cluster is None or form is None or self._shard_grid is None:
            return
        target = form.shard
        values = self._shard_grid.values()
        error = save_checks.validate_ranges(values, shard=True)
        if error:
            dialogs.show_error(self._window(), t("dlg.save_ok"), error)
            return
        shard_config = save_checks.build_shard_config(target, values)
        cluster_config = save_checks.load_cluster_config(cluster.path)
        _, port_issues = collect_cluster_port_claims(
            cluster, cluster_config_override=cluster_config, shard_config_overrides={target.name: shard_config})
        lan_issues = [issue for issue in port_issues
                      if issue.code == "lan_server_port_range" and issue.shard_name == target.name]
        if lan_issues:
            dialogs.show_error(self._window(), t("dlg.save_fail"), t(
                "cluster.lan_shard_port_invalid", details=save_checks.format_port_issues(lan_issues)))
            return
        conflict = save_checks.find_port_conflict(cluster, target, shard_config)
        if conflict:
            dialogs.show_error(self._window(), t("dlg.save_fail"), conflict)
            return
        cross = save_checks.find_cross_cluster_port_conflicts(cluster, target, shard_config, self.ctx.env.clusters)
        if cross and not dialogs.ask_yes_no(
                self._window(), t("cluster.cross_cluster_port_title"),
                t("cluster.cross_cluster_port_confirm", details="\n".join(cross[:12])), min_width=780):
            return
        save_shard_config(shard_config, target.path)
        dialogs.show_info(self._window(), t("dlg.save_ok"), t("dlg.config_saved", name=f"{cluster.name}/{target.name}"))
        # server_port 会拼进"本地服务器"页的直连代码，改了要通知它刷新
        self.ctx.cluster_config_saved.emit(cluster)
        self._rebuild_shard_form()  # 只重载这个世界自己的字段，不把世界下拉框重置回 Master
