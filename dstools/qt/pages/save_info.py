"""存档信息页：存档概览 → 世界选择 → 会话信息 → 玩家角色状态。

取数在后台线程（features/save_browser/view_data.py），连续切换时用"代数"丢弃过期结果。
"""

import os
import zipfile

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget, QWidgetAction,
)

from dstools.features.local_service.backup_manager import (
    create_backup, list_backups, restore_backup, shards_missing_from_backup,
)
from dstools.features.local_service.dedicated_server import detect_external_shard_processes
from dstools.features.save_browser import view_data
from dstools.shared.cluster_names import validate_cluster_folder_name
from dstools.features.save_browser.cluster_copy import (
    copy_local_cluster_to_server, suggest_new_cluster_name,
)
from dstools.features.save_browser.save_bundle import create_save_bundle, default_save_bundle_output_dir
from dstools.features.save_browser.save_delete import recycle_cluster_dir
from dstools.features.save_browser.world_reset import reset_worlds, shards_with_sessions
from dstools.i18n import t
from dstools.models import SaveSource
from dstools.qt import dialogs
from dstools.qt.tunnel.lan_mapping_guard import mapping_method_names
from dstools.qt.pages.base import Page
from dstools.qt.theme import theme
from dstools.qt.threads import run_async, run_async_with_log
from dstools.qt.widgets import Card, FrostedMenu, MenuTextItem, clear_layout
from dstools.shared.app_settings import set_player_note
from dstools.shared.clipboard import copy_file_to_clipboard

AVATAR_SIZE = 72


def _label(text: str = "", size_key: str = "FONT_SIZE_XS", bold: bool = False, muted: bool = True) -> QLabel:
    label = QLabel(text)
    label.setFont(theme.font(size_key, bold))
    label.setProperty("muted", muted)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


class SaveInfoPage(Page):
    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        self._generation = 0
        self._shard = ""

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 12, 24, 12)
        # 外层边框内部全透明，跟其它主页签统一；下面玩家概览/每行玩家卡片是内容级
        # 的 Card，不受这个影响，各自保留自己的底色。
        card = Card(radius=15, alpha=0, border=True)
        outer.addWidget(card)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(22, 14, 22, 14)
        layout.setSpacing(8)

        title_row = QHBoxLayout()
        self._basic_title = _label(size_key="FONT_SIZE_BASE", bold=True, muted=False)
        title_row.addWidget(self._basic_title)
        title_row.addStretch()
        # "打包存档"直接放标题行；立即备份/备份策略/恢复统一收进"备份管理"菜单
        self._bundle_button = QPushButton()
        self._bundle_button.clicked.connect(self._on_bundle)
        # "备份管理"保持主题色按钮外观；展开菜单用假透明底。按钮和菜单取两者
        # 自然宽度的较大值、两边设成同宽（见 _sync_manage_width），菜单文字不被挤占。
        self._manage_button = QPushButton()
        self._manage_menu = FrostedMenu(self._manage_button)
        # 菜单项文字居中：QMenu 原生菜单项改不了对齐，用自绘的 MenuTextItem。
        self._backup_now_item = MenuTextItem(self._manage_menu, "", self._on_backup_now)
        self._backup_policy_item = MenuTextItem(self._manage_menu, "", self._on_backup_policy)
        self._restore_item = MenuTextItem(self._manage_menu, "", self._on_restore)
        for item in (self._backup_now_item, self._backup_policy_item, self._restore_item):
            action = QWidgetAction(self._manage_menu)
            action.setDefaultWidget(item)
            self._manage_menu.addAction(action)
        self._manage_button.setMenu(self._manage_menu)
        title_row.addWidget(self._bundle_button)
        title_row.addWidget(self._manage_button)
        layout.addLayout(title_row)
        self._bundle_running = False
        self._log_dialog = None  # 复制为服务器存档的日志窗口，保持引用避免被回收

        self._overview = Card(radius=14, alpha=128, fill_key="CARD_BG_ALT")
        overview_row = QHBoxLayout(self._overview)
        overview_row.setContentsMargins(14, 8, 14, 8)
        overview_text = QVBoxLayout()
        overview_text.setSpacing(2)
        self._storage_label = _label()
        self._detail_label = _label()
        self._shards_label = _label()
        for label in (self._storage_label, self._detail_label, self._shards_label):
            overview_text.addWidget(label)
        overview_row.addLayout(overview_text, 1)
        self._copy_to_server = QPushButton()
        self._copy_to_server.clicked.connect(self._on_copy_to_server)
        # "打开位置"挪到了顶部存档选择栏（"创建服务器存档"左侧），这里换成"删除存档"。
        self._delete_button = QPushButton()
        self._delete_button.clicked.connect(self._on_delete)
        self._delete_running = False
        self._reset_button = QPushButton()
        self._reset_button.clicked.connect(self._on_reset_world)
        self._reset_running = False
        overview_row.addWidget(self._copy_to_server, 0, Qt.AlignmentFlag.AlignVCenter)
        overview_row.addWidget(self._reset_button, 0, Qt.AlignmentFlag.AlignVCenter)
        overview_row.addWidget(self._delete_button, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self._overview)

        shard_row = QHBoxLayout()
        self._shard_label = _label(size_key="FONT_SIZE_BASE", muted=False)
        self._shard_combo = QComboBox()
        self._shard_combo.setMinimumWidth(180)
        self._shard_combo.activated.connect(self._on_shard_picked)
        shard_row.addWidget(self._shard_label)
        shard_row.addWidget(self._shard_combo)
        shard_row.addStretch()
        layout.addLayout(shard_row)

        self._session_lines = [_label() for _ in range(4)]
        for label in self._session_lines:
            layout.addWidget(label)

        self._players_title = _label(size_key="FONT_SIZE_BASE", bold=True, muted=False)
        layout.addWidget(self._players_title)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.viewport().setAutoFillBackground(False)
        self._rows = QWidget()
        self._rows_layout = QVBoxLayout(self._rows)
        self._rows_layout.setContentsMargins(0, 0, 6, 0)
        self._rows_layout.setSpacing(6)
        self._rows_layout.addStretch()
        self._scroll.setWidget(self._rows)
        self._rows.setAutoFillBackground(False)
        layout.addWidget(self._scroll, 1)

        theme.changed.connect(self._on_theme_changed)
        self.retranslate()

    # ── 文案 ────────────────────────────────────────────────────────────
    def retranslate(self) -> None:
        self._basic_title.setText(t("save.basic_info"))
        self._shard_label.setText(t("save.shard"))
        self._players_title.setText(t("save.players_section"))
        self._delete_button.setText(t("save.delete_busy") if self._delete_running else t("save.delete_btn"))
        self._reset_button.setText(t("save.reset_world_busy") if self._reset_running else t("save.reset_world_btn"))
        self._reset_button.setToolTip(t("save.reset_world_hover"))
        self._copy_to_server.setText(t("save.copy_to_server"))
        self._bundle_button.setText(t("save.bundle_running") if self._bundle_running else t("save.bundle_btn"))
        self._manage_button.setText(t("save.backup_management"))
        self._backup_now_item.setText(t("save.backup_now"))
        self._backup_policy_item.setText(t("save.backup_policy_btn"))
        self._restore_item.setText(t("save.restore_backup"))
        self._sync_manage_width()

    def _sync_manage_width(self) -> None:
        self._manage_button.setMinimumWidth(0)
        self._manage_button.setMaximumWidth(16777215)
        self._manage_menu.setMinimumWidth(0)
        self._manage_menu.setMaximumWidth(16777215)
        width = max(self._manage_button.sizeHint().width(), self._manage_menu.sizeHint().width())
        self._manage_button.setFixedWidth(width)
        self._manage_menu.setFixedWidth(width)

    def _on_theme_changed(self) -> None:
        # 字体样式/字号可能变了：整页重新取数重建，颜色由 QSS/自绘控件自己跟随
        self._sync_manage_width()
        if self.isVisible():
            self.load()
        else:
            self.stale = True

    # ── 加载 ────────────────────────────────────────────────────────────
    def on_cluster_changed(self, cluster) -> None:
        self._generation += 1
        self._shard_combo.blockSignals(True)
        self._shard_combo.clear()
        if cluster is not None:
            for name in view_data.shard_names(cluster):
                self._shard_combo.addItem(name)
            self._shard = view_data.default_shard_name(cluster, self._shard)
            self._shard_combo.setCurrentText(self._shard)
        else:
            self._shard = ""
        self._shard_combo.blockSignals(False)
        self._overview.setVisible(cluster is not None)
        has_cluster = cluster is not None
        self._bundle_button.setEnabled(has_cluster and not self._bundle_running)
        self._manage_button.setEnabled(has_cluster)
        # 只有本地存档才需要"复制为服务器存档"
        self._copy_to_server.setVisible(has_cluster and cluster.source == SaveSource.LOCAL)
        self._load_overview(cluster)
        self._load_session(cluster)

    def _on_shard_picked(self, _index: int) -> None:
        self._shard = self._shard_combo.currentText()
        self._generation += 1
        self._load_session(self.ctx.selected_cluster())

    def _load_overview(self, cluster) -> None:
        if cluster is None:
            return
        generation = self._generation
        for label in (self._storage_label, self._detail_label, self._shards_label):
            label.setText("")
        self._storage_label.setText(t("save.loading"))
        self._delete_button.setEnabled(not self._delete_running)
        self._reset_button.setEnabled(not self._reset_running)

        def done(overview: view_data.ClusterOverview) -> None:
            if generation != self._generation:
                return
            self._storage_label.setText(f"{t('save.storage_location')}: {overview.path}")
            self._detail_label.setText(
                f"{t('env.game_mode')}: {overview.game_mode}   {t('env.max_players')}: {overview.max_players}"
                f"   {t('env.cluster_name')}: {overview.cluster_name}")
            self._shards_label.setText("  ".join(
                f"{s.name}({s.mod_count}{t('env.mods')}/{s.session_count}{t('env.save_sessions')})"
                for s in overview.shards))

        run_async(lambda: view_data.load_cluster_overview(cluster), done, self._show_error)

    def _load_session(self, cluster) -> None:
        generation = self._generation
        self._clear_rows()
        for label in self._session_lines:
            label.setText("")
            label.setVisible(False)
        if cluster is None or not self._shard:
            self._show_line(0, t("save.no_saves"))
            return
        self._show_line(0, t("save.loading"))
        self._add_row_message(t("save.loading"))
        shard = self._shard

        def done(session: view_data.SessionView | None) -> None:
            if generation != self._generation:
                return
            self._clear_rows()
            for label in self._session_lines:
                label.setVisible(False)
            if session is None:
                self._show_line(0, t("save.no_saves"))
                self._add_row_message(t("save.no_players"))
                return
            self._show_line(0, f"{t('save.session_id')}: {session.session_id}")
            self._show_line(1, f"{t('save.summary')}: {session.summary}")
            self._show_line(2, f"{t('save.slots')}: {session.slot_count}    {t('save.size')}: {session.size_mb:.1f} MB")
            if session.extra_sessions > 0:
                self._show_line(3, t("save.extra_sessions", count=session.extra_sessions))
            if not session.players:
                self._add_row_message(t("save.no_players"))
            for player in session.players:
                self._add_player_row(player)

        run_async(lambda: view_data.load_session_view(cluster, shard), done, self._show_error)

    def _show_error(self, exc: Exception) -> None:
        self._clear_rows()
        self._show_line(0, str(exc))

    # ── 控件 ────────────────────────────────────────────────────────────
    def _show_line(self, index: int, text: str) -> None:
        self._session_lines[index].setText(text)
        self._session_lines[index].setVisible(True)

    def _clear_rows(self) -> None:
        clear_layout(self._rows_layout, keep_last=1)  # 最后一项是撑开底部的 stretch

    def _add_row_message(self, text: str) -> None:
        label = _label(text, size_key="FONT_SIZE_BASE")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setContentsMargins(0, 10, 0, 10)
        self._rows_layout.insertWidget(self._rows_layout.count() - 1, label)

    def _add_player_row(self, player: view_data.PlayerView) -> None:
        row = Card(radius=14, alpha=128, fill_key="CARD_BG_ALT")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(12, 8, 12, 8)
        row_layout.setSpacing(10)

        avatar = QLabel()
        avatar.setFixedSize(AVATAR_SIZE, AVATAR_SIZE)
        avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if player.icon_path:
            pixmap = QPixmap(str(player.icon_path))
            if not pixmap.isNull():
                # 按物理像素缩放并设置像素比，避免 Qt 二次放大；只有约 60px 的小头像可用时不放大，按原尺寸显示
                dpr = self.devicePixelRatioF()
                box = round(AVATAR_SIZE * dpr)
                if pixmap.width() > box or pixmap.height() > box:
                    pixmap = pixmap.scaled(box, box, Qt.AspectRatioMode.KeepAspectRatio,
                                           Qt.TransformationMode.SmoothTransformation)
                    pixmap.setDevicePixelRatio(dpr)
                avatar.setPixmap(pixmap)
        row_layout.addWidget(avatar)

        body = QVBoxLayout()
        body.setSpacing(2)
        title = (f"{t('save.player_id_label')}: {player.player_id}" if player.parse_error
                 else player.character_name)
        body.addWidget(_label(title, size_key="FONT_SIZE_BASE", bold=True, muted=False))

        pending = t("save.player_pending")
        account_id, nickname = player.identity or ("", "")
        parts = []
        if player.connection_times:
            parts.append(t("save.player_connection_fragment",
                           last=player.connection_times[-1], count=len(player.connection_times)))
        parts.append(t("save.player_identity_fragment", id=account_id or pending))
        parts.append(t("save.player_nickname_fragment", nickname=nickname or pending))
        body.addWidget(_label("  ·  ".join(parts)))

        id_row = QHBoxLayout()
        id_row.addWidget(_label(f"{t('save.player_id_label')}: {player.player_id}"))
        id_row.addSpacing(12)
        id_row.addWidget(_label(f"{t('save.player_note_label')}:"))
        note = QLineEdit(player.note)
        note.setFixedWidth(180)
        note.setFont(theme.font("FONT_SIZE_XS"))
        # 备注框比全局输入框矮一些：上下内边距收小并固定高度。
        note.setStyleSheet("padding: 1px 8px;")
        note.setFixedHeight(22)
        # 按玩家标识全局存一份备注；回车/失焦提交（输入法组词时 Qt 不会提前触发 editingFinished）
        note.editingFinished.connect(
            lambda pid=player.player_id, edit=note: set_player_note(pid, edit.text().strip()))
        id_row.addWidget(note)
        id_row.addStretch()
        body.addLayout(id_row)

        if not player.parse_error:
            def fmt(value):
                return "?" if value is None else (f"{value:.0f}" if isinstance(value, float) else str(value))
            body.addWidget(_label(
                f"{t('save.stat_health')}: {fmt(player.health)}   {t('save.stat_sanity')}: {fmt(player.sanity)}"
                f"   {t('save.stat_hunger')}: {fmt(player.hunger)}"
                f"   {t('save.stat_temperature')}: {fmt(player.temperature)}"))
        row_layout.addLayout(body, 1)

        open_path = QPushButton(t("save.player_open_path"))
        open_path.setEnabled(bool(player.save_file))
        open_path.clicked.connect(lambda _c=False, p=player.save_file: self._open_path(p.parent if p else None))
        row_layout.addWidget(open_path, 0, Qt.AlignmentFlag.AlignVCenter)
        self._rows_layout.insertWidget(self._rows_layout.count() - 1, row)

    # ── 动作 ────────────────────────────────────────────────────────────
    def _cluster_in_use(self, cluster) -> bool:
        """本程序启动的世界，或外部启动的专服进程正在用这个存档。"""
        if self.ctx.running_shard_names(cluster):
            return True
        try:
            external = detect_external_shard_processes(cluster)
            return any(info.get("running") for info in external.values())
        except (OSError, ValueError, KeyError):
            return False

    def _on_delete(self) -> None:
        """删除当前存档（移到回收站）：可选先打包 ZIP 备份（存到存档目录之外）再删除。"""
        cluster = self.ctx.selected_cluster()
        if cluster is None or self._delete_running:
            return
        title = t("save.delete_title")
        if self._cluster_in_use(cluster):
            dialogs.show_warning(self._window(), title, t("save.delete_running", name=cluster.name))
            return
        message = t("save.delete_confirm", name=cluster.name, dir=str(default_save_bundle_output_dir()))
        methods = mapping_method_names(self.ctx, cluster)
        if methods:
            message += t("save.delete_mapping_note", methods="、".join(methods))
        choice = dialogs.ask_choice(
            self._window(), title, message,
            [(t("save.delete_backup_btn"), "backup"), (t("save.delete_direct_btn"), "delete"),
             (t("dlg.cancel_btn"), "cancel")],
            default="cancel", min_width=420, danger_values=("backup", "delete"))
        if choice not in ("backup", "delete"):
            return
        self._delete_running = True
        self._delete_button.setEnabled(False)
        self._delete_button.setText(t("save.delete_busy"))
        cluster_path = cluster.path

        def work():
            backup = create_save_bundle(cluster_path) if choice == "backup" else None
            recycle_cluster_dir(cluster_path)  # 两种删除方式都是移到回收站
            return backup

        def finish() -> None:
            self._delete_running = False
            self._delete_button.setText(t("save.delete_btn"))
            self._delete_button.setEnabled(True)

        def done(backup) -> None:
            # 存档已移走：关闭它的映射（本地记录要在重新扫描前清掉），远程隧道在后台删完再提示
            release = self.ctx.release_cluster_mappings(cluster, True)
            self.ctx.refresh_env()
            run_async(release, lambda failures: report(backup, failures),
                      lambda exc: report(backup, [str(exc)]))

        def report(backup, failures: list[str]) -> None:
            finish()
            if backup is not None:
                dialogs.show_file_location(self._window(), title, backup,
                                           t("save.delete_done_backup", name=cluster.name, path="").strip(),
                                           "")
            else:
                dialogs.show_info(self._window(), title, t("save.delete_done", name=cluster.name))
            if failures:
                dialogs.show_warning(self._window(), title, t("save.delete_mapping_failed_msg", name=cluster.name,
                                                              items="\n".join(failures)))

        def failed(exc: Exception) -> None:
            finish()
            self.ctx.refresh_env()
            dialogs.show_error(self._window(), title, t("save.delete_failed", error=str(exc)))

        run_async(work, done, failed)

    def _on_reset_world(self) -> None:
        """不启动服务器重置世界：先备份，再删各世界的会话存档（业务见 features/save_browser/world_reset.py）。"""
        cluster = self.ctx.selected_cluster()
        if cluster is None or self._reset_running:
            return
        title = t("save.reset_world_title")
        if self._cluster_in_use(cluster):
            dialogs.show_warning(self._window(), title, t("save.reset_world_running", name=cluster.name))
            return
        shards = shards_with_sessions(cluster.path)
        if not shards:
            dialogs.show_info(self._window(), title, t("save.reset_world_none", name=cluster.name))
            return
        if not dialogs.ask_yes_no(self._window(), title,
                                  t("save.reset_world_confirm", name=cluster.name, shards="、".join(shards)),
                                  danger=True):
            return
        self._reset_running = True
        self._reset_button.setEnabled(False)
        self._reset_button.setText(t("save.reset_world_busy"))

        def work():
            create_backup(cluster.path, keep=True)
            return reset_worlds(cluster.path)

        def finish() -> None:
            self._reset_running = False
            self._reset_button.setText(t("save.reset_world_btn"))
            self._reset_button.setEnabled(True)

        def done(reset: list[str]) -> None:
            finish()
            self.load()
            dialogs.show_info(self._window(), title, t("save.reset_world_done", shards="、".join(reset)))

        def failed(exc: Exception) -> None:
            finish()
            self.load()
            dialogs.show_error(self._window(), title, t("save.reset_world_failed", error=str(exc)))

        run_async(work, done, failed)

    @staticmethod
    def _open_path(path) -> None:
        if path is not None:
            os.startfile(str(path))

    def _window(self):
        return self.window()

    def _on_backup_now(self) -> None:
        cluster = self.ctx.selected_cluster()
        if cluster is None:
            return
        title = t("save.backup_title")
        run_async(
            lambda: create_backup(cluster.path, keep=True),
            lambda _result: dialogs.show_info(self._window(), title, t("save.backup_ok")),
            lambda exc: dialogs.show_error(self._window(), title, t("save.backup_failed", error=str(exc))),
        )

    def _on_backup_policy(self) -> None:
        dialogs.BackupPolicyDialog(self._window()).exec()

    def _on_restore(self) -> None:
        cluster = self.ctx.selected_cluster()
        if cluster is None:
            return
        title = t("save.restore_backup")
        backups = list_backups(cluster.path)
        if not backups:
            dialogs.show_info(self._window(), title, t("save.restore_none"))
            return
        picker = dialogs.RestoreBackupDialog(self._window(), cluster, backups)
        if not picker.exec() or picker.result_backup is None:
            return
        # 世界文件被服务器进程占着时没法覆盖/删除，必须先确认这个存档下所有世界都已停止
        running = self.ctx.running_shard_names(cluster)
        if running:
            dialogs.show_warning(self._window(), title, t("save.restore_shards_running", shards="、".join(running)))
            return
        backup = picker.result_backup
        try:
            extra = shards_missing_from_backup(cluster.path, backup)
        except (OSError, zipfile.BadZipFile) as exc:
            dialogs.show_error(self._window(), title, t("save.restore_failed", error=str(exc)))
            return
        confirm = t("save.restore_confirm")
        if extra:
            confirm += "\n\n" + t("save.restore_removes_shards", shards="、".join(extra))
        if not dialogs.ask_yes_no(self._window(), title, confirm, danger=True):
            return

        def work():
            create_backup(cluster.path, keep=True)  # 恢复前先保险备份一次当前状态，恢复本身也能撤销
            restore_backup(cluster.path, backup)

        def done(_result) -> None:
            dialogs.show_info(self._window(), title, t("save.restore_ok"))
            self.load()

        run_async(work, done, lambda exc: dialogs.show_error(
            self._window(), title, t("save.restore_failed", error=str(exc))))

    def _on_bundle(self) -> None:
        """后台打包当前完整存档，完成后把 ZIP 文件放入系统剪贴板。"""
        cluster = self.ctx.selected_cluster()
        if cluster is None or self._bundle_running:
            return
        self._bundle_running = True
        self._bundle_button.setEnabled(False)
        self._bundle_button.setText(t("save.bundle_running"))
        title = t("save.bundle_title")

        def finish() -> None:
            self._bundle_running = False
            self._bundle_button.setText(t("save.bundle_btn"))
            self._bundle_button.setEnabled(self.ctx.selected_cluster() is not None)

        def done(zip_path) -> None:
            finish()
            if copy_file_to_clipboard(zip_path):
                dialogs.show_file_location(
                    self._window(), title, zip_path, t("save.bundle_location_label"),
                    t("save.bundle_clipboard_hint"))
                return
            QGuiApplication.clipboard().setText(str(zip_path))
            dialogs.show_warning(self._window(), title, t("save.bundle_path_copied", path=str(zip_path)))

        def failed(exc: Exception) -> None:
            finish()
            dialogs.show_error(self._window(), title, t("save.bundle_failed", error=str(exc)))

        run_async(lambda: create_save_bundle(cluster.path), done, failed)

    def _on_copy_to_server(self) -> None:
        """把本地存档整个文件夹复制成一份新的服务器存档，过程日志实时显示在弹窗里。"""
        cluster = self.ctx.selected_cluster()
        if cluster is None or cluster.source != SaveSource.LOCAL:
            return
        klei_root = self.ctx.env.klei_root_for(cluster.platform)
        if not klei_root:
            dialogs.show_error(self._window(), t("save.copy_to_server"), t("save.no_saves"))
            return

        def validate(name: str):
            reason = validate_cluster_folder_name(name)
            if reason:
                return t(f"save.copy_name_{reason}")
            if (klei_root / name.strip()).exists():
                return t("save.copy_name_exists")
            return None

        picker = dialogs.CopyToServerDialog(
            self._window(), cluster.name, suggest_new_cluster_name(klei_root, cluster.name), validate)
        if not picker.exec() or not picker.result_name:
            return
        new_name = picker.result_name

        self._copy_to_server.setEnabled(False)
        log = self._log_dialog = dialogs.LogDialog(self._window(), t("save.copy_result_title"))
        log.setModal(False)
        log.show()

        def work(emit):
            try:
                copy_local_cluster_to_server(cluster.path, klei_root, new_name, on_log=emit)
            except Exception as exc:  # noqa: BLE001 - 原始错误写进日志窗口
                emit(t("sync.error_prefix", detail=str(exc)))

        def done(_result) -> None:
            log.finish()
            self._copy_to_server.setEnabled(True)
            self.ctx.refresh_env()
            self.ctx.clear_stale_mappings(klei_root / new_name)

        run_async_with_log(work, log.append, done)
