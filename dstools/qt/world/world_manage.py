"""管理世界弹窗：列出专服存档的全部世界，改万界之门里的名称/隐藏/人数上限、改文件夹、删除世界。

每项修改都在后台先备份再写盘（业务见 features/world/shard_management.py），完成后重新扫描环境并刷新列表。
用内网穿透映射的世界不能改文件夹或删除：映射记录按分片名保存，先由用户在内网穿透页关闭。
"""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from dstools.features.local_service.backup_manager import create_backup
from dstools.features.world import shard_management
from dstools.features.world.location_profiles import CAVES_SHARD, get_location_definition
from dstools.i18n import get_lang, t
from dstools.models import SaveSource
from dstools.qt import dialogs
from dstools.qt.tunnel.lan_mapping_guard import shard_mapping_name
from dstools.qt.theme import theme
from dstools.qt.threads import run_async
from dstools.qt.widgets import Banner, Card, clear_layout
from dstools.shared import app_settings

MAX_PLAYER_LIMIT = 64  # cluster.ini max_players 的上限


class WorldEditDialog(dialogs.Dialog):
    """编辑一个世界在万界之门里的名称、是否隐藏和人数上限。"""

    def __init__(self, parent, cluster, entry: shard_management.WorldEntry):
        super().__init__(parent, t("world.manage_edit_title", shard=entry.shard_name), "md")
        self._cluster = cluster
        self._shard_name = entry.shard_name
        self.world_name, self.hidden, self.limit = entry.name, entry.hidden, entry.limit
        self.body.addWidget(self.text_label(t("world.add_shard_name_label")))
        self._name_edit = QLineEdit(entry.name or entry.shard_name)
        self._name_edit.setFont(theme.font("FONT_SIZE_BASE"))
        self.body.addWidget(self._name_edit)
        self.body.addWidget(self.text_label(t("world.add_shard_name_hint"), muted=True, size_key="FONT_SIZE_XS"))
        self._hidden_check = QCheckBox(t("world.manage_hidden_label"))
        self._hidden_check.setFont(theme.font("FONT_SIZE_SM"))
        self._hidden_check.setChecked(entry.hidden)
        self.body.addSpacing(6)
        self.body.addWidget(self._hidden_check)
        self.body.addWidget(self.text_label(t("world.manage_hidden_hint"), muted=True, size_key="FONT_SIZE_XS"))
        self.body.addSpacing(6)
        self.body.addWidget(self.text_label(t("world.manage_limit_label")))
        self._limit_edit = QLineEdit(str(entry.limit))
        self._limit_edit.setFixedWidth(90)
        self._limit_edit.setValidator(QIntValidator(0, MAX_PLAYER_LIMIT))
        self.body.addWidget(self._limit_edit)
        self.body.addWidget(self.text_label(t("world.manage_limit_hint"), muted=True, size_key="FONT_SIZE_XS"))
        self._error = self.error_label()
        self.body.addWidget(self._error)
        self.add_buttons()
        self._name_edit.setFocus()
        self._name_edit.selectAll()

    def accept_if_valid(self) -> None:
        world_name = self._name_edit.text().strip()
        error = shard_management.renamed_world_error(self._cluster, self._shard_name, world_name)
        if error:
            self._error.setText(error)
            return
        try:
            limit = int(self._limit_edit.text() or "0")
        except ValueError:
            limit = -1
        if not 0 <= limit <= MAX_PLAYER_LIMIT:
            self._error.setText(t("world.manage_limit_invalid"))
            return
        self.world_name, self.hidden, self.limit = world_name, self._hidden_check.isChecked(), limit
        self.accept()


class ManageWorldsDialog(dialogs.Dialog):
    """世界列表：每个世界一张卡片（名称与标签、文件夹/类型/编号、编辑/改文件夹/删除按钮）。
    各项修改点下去就生效，底部只有「确认」关闭弹窗；新增世界在世界页的按钮里。"""

    def __init__(self, parent, ctx):
        super().__init__(parent, t("world.manage_btn"), "lg")
        self.ctx = ctx
        self._busy = False
        self._intro = self.text_label("", muted=True)
        self.body.addWidget(self._intro)
        self._banner = Banner()
        self.body.addWidget(self._banner)
        host = QWidget()
        self._list = QVBoxLayout(host)
        self._list.setContentsMargins(0, 2, 0, 2)
        self._list.setSpacing(8)
        self.body.addWidget(host)
        confirm = QPushButton(t("dlg.confirm_btn"))
        confirm.clicked.connect(self.accept)
        confirm.setDefault(True)
        self.add_footer([], [confirm])
        self.reload()

    # ── 列表 ────────────────────────────────────────────────────────────
    def reload(self) -> None:
        clear_layout(self._list)
        try:
            self._fill_list()
        finally:
            # 世界数变了（删除等）要按新内容重算高度，否则多出的高度被摊到各控件之间；
            # 新卡片的尺寸要等事件循环处理完布局请求才算得出，所以放到下一轮再调
            QTimer.singleShot(0, self._fit_height)

    def _fit_height(self) -> None:
        self.resize(self.width(), self.sizeHint().height())

    def _fill_list(self) -> None:
        cluster = self.ctx.selected_cluster()
        if cluster is None or cluster.source != SaveSource.SERVER:
            self.reject()
            return
        try:
            entries = shard_management.list_worlds(cluster)
            worldgate = shard_management.worldgate_enabled(cluster)
        except (OSError, ValueError) as exc:  # modoverrides.lua 或 server.ini 读不了
            self._intro.setText("")
            self._banner.set_text(str(exc))
            return
        running = self.ctx.cluster_running(cluster)
        self._intro.setText(t("world.manage_intro", cluster=cluster.name, count=len(entries)))
        self._banner.set_text(t("world.manage_running") if running else
                              "" if worldgate else t("world.manage_no_worldgate"))
        editable = not running and not self._busy
        for entry in entries:
            self._list.addWidget(self._world_card(cluster, entry, editable, worldgate))

    @staticmethod
    def _tag(text: str, color_key: str) -> QLabel:
        """名称后面的小圆角标签（主世界/已隐藏/人数上限）。"""
        tag = QLabel(text)
        tag.setFont(theme.font("FONT_SIZE_XS"))
        color = theme.hex(color_key)
        # 样式用对象名限定：悬停提示框会继承控件样式表，否则提示框也画成圆角、四角露出黑点
        tag.setObjectName("worldTag")
        tag.setStyleSheet(f"QLabel#worldTag {{ color: {color}; border: 1px solid {color}; "
                          "border-radius: 8px; padding: 0px 7px; }")
        return tag

    def _world_card(self, cluster, entry, editable: bool, worldgate: bool) -> Card:
        card = Card(radius=12, alpha=128, fill_key="CARD_BG_ALT")
        row = QHBoxLayout(card)
        row.setContentsMargins(16, 10, 12, 10)
        row.setSpacing(8)
        info = QVBoxLayout()
        info.setSpacing(4)
        title_row = QHBoxLayout()
        title_row.setSpacing(6)
        title = QLabel(entry.name or t("world.manage_unnamed"))
        title.setFont(theme.font("FONT_SIZE_BASE", bold=bool(entry.name)))
        title.setProperty("muted", not entry.name)
        title_row.addWidget(title)
        if entry.is_master:
            title_row.addWidget(self._tag(t("world.manage_master_tag"), "PRIMARY"))
        if entry.hidden:
            title_row.addWidget(self._tag(t("world.manage_hidden_tag"), "TEXT_MUTED"))
        if entry.limit:
            title_row.addWidget(self._tag(t("world.manage_limit_tag", limit=entry.limit), "ACCENT"))
        title_row.addStretch()
        info.addLayout(title_row)
        shard_id = str(entry.shard_id) if entry.shard_id is not None else "-"
        detail = QLabel(t("world.manage_detail", folder=entry.shard_name,
                          location=self._location_text(entry.location), id=shard_id))
        detail.setFont(theme.font("FONT_SIZE_XS"))
        detail.setProperty("muted", True)
        info.addWidget(detail)
        row.addLayout(info, 1)

        edit = dialogs.style_button(QPushButton(t("world.manage_edit_btn")), "secondary")
        edit.setEnabled(editable and worldgate)
        edit.clicked.connect(lambda _c=False, e=entry: self._on_edit(e))
        folder_btn = dialogs.style_button(QPushButton(t("world.manage_folder_btn")), "secondary")
        protected = shard_management.rename_shard_error(cluster, entry.shard_name, entry.shard_name)
        folder_btn.setEnabled(editable and not protected)
        folder_btn.setToolTip(protected)
        folder_btn.clicked.connect(lambda _c=False, e=entry: self._on_rename_folder(e))
        delete = dialogs.style_button(QPushButton(t("world.manage_delete_btn")), "danger")
        delete.setEnabled(editable and not entry.is_master)
        delete.clicked.connect(lambda _c=False, e=entry: self._on_delete(e))
        for button in (edit, folder_btn, delete):
            button.setFont(theme.font("FONT_SIZE_SM"))
            button.setAutoDefault(False)
            row.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)
        return card

    @staticmethod
    def _location_text(location: str | None) -> str:
        if not location:
            return "-"
        try:
            return get_location_definition(location).name(get_lang())
        except ValueError:  # 没登记的 Mod 世界类型，直接显示原值
            return location

    # ── 操作 ────────────────────────────────────────────────────────────
    def _current(self, shard_name: str, mapping_key: str = ""):
        """操作前按最新状态再检查一次：存档已停止；mapping_key 非空时还要求这个世界没有内网穿透映射。"""
        cluster = self.ctx.selected_cluster()
        if cluster is None:
            return None
        if self.ctx.cluster_running(cluster):
            dialogs.show_warning(self, t("world.manage_btn"), t("world.manage_running"))
            self.reload()
            return None
        shard = next((s for s in cluster.shards if s.name == shard_name), None)
        if shard is None:
            self.reload()
            return None
        if mapping_key and (method := shard_mapping_name(self.ctx, cluster, shard)):
            dialogs.show_warning(self, t("world.manage_btn"), t(mapping_key, shard=shard_name, method=method))
            return None
        return cluster

    def _on_edit(self, entry) -> None:
        cluster = self._current(entry.shard_name)
        if cluster is None:
            return
        dialog = WorldEditDialog(self, cluster, entry)
        if dialog.exec() != dialogs.Dialog.DialogCode.Accepted:
            return
        name, hidden, limit, language = dialog.world_name, dialog.hidden, dialog.limit, get_lang()

        def work() -> None:
            create_backup(cluster.path, keep=True)
            shard_management.update_world_entry(cluster, entry.shard_name, name, hidden, limit, language)

        self._run(work, lambda: t("world.manage_saved"))

    def _on_rename_folder(self, entry) -> None:
        cluster = self._current(entry.shard_name, "world.manage_mapping_rename")
        if cluster is None:
            return
        old = entry.shard_name
        dialog = dialogs.TextInputDialog(
            self, t("world.manage_folder_title"), t("world.manage_folder_prompt", shard=old), old,
            validator=lambda value: shard_management.rename_shard_error(cluster, old, value))
        if dialog.exec() != dialogs.Dialog.DialogCode.Accepted or dialog.result_text in (None, old):
            return
        new = dialog.result_text

        def work() -> None:
            create_backup(cluster.path, keep=True)
            shard_management.rename_shard_folder(cluster, old, new)

        def after() -> str:
            # 粘贴过的 Lolia 隧道来源按分片名保存（关闭映射后仍保留），跟着新名字走
            source = app_settings.get_lolia_source(cluster.path, old)
            if source is not None:
                app_settings.set_lolia_source(cluster.path, new, source)
                app_settings.set_lolia_source(cluster.path, old, None)
            return t("world.manage_renamed", shard=new)

        self._run(work, after)

    def _on_delete(self, entry) -> None:
        cluster = self._current(entry.shard_name, "world.manage_mapping_delete")
        if cluster is None:
            return
        shard_name = entry.shard_name
        text = t("world.manage_delete_confirm", name=entry.name or shard_name, shard=shard_name)
        if shard_name == CAVES_SHARD:
            text += t("world.manage_delete_caves_note")
        if not dialogs.ask_yes_no(self, t("world.manage_delete_title"), text, danger=True):
            return
        language = get_lang()

        def work() -> None:
            create_backup(cluster.path, keep=True)
            shard_management.delete_shard(cluster, shard_name, language)

        def after() -> str:
            # 同名新世界不该继承已删世界粘贴过的隧道来源
            app_settings.set_lolia_source(cluster.path, shard_name, None)
            return t("world.manage_deleted", shard=shard_name)

        self._run(work, after)

    def _run(self, work, after) -> None:
        """后台执行 work；成功后在界面线程调用 after() 取完成提示，重新扫描环境并刷新列表。"""
        self._busy = True
        self.reload()

        def done(_result) -> None:
            self._busy = False
            message = after()
            self.ctx.refresh_env()
            self.reload()
            dialogs.show_info(self, t("world.manage_btn"), message)

        def failed(exc: Exception) -> None:
            self._busy = False
            self.reload()
            dialogs.show_error(self, t("world.manage_btn"), str(exc))

        run_async(work, done, failed)
