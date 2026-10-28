"""运行中世界的控制台标签：只读日志、命令输入、快捷指令、搜索栏、崩溃诊断与 Mod 加载提示条（由本地服务器页定时器驱动 pump()）。"""

import os
from pathlib import Path

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor, QGuiApplication, QIcon, QKeySequence, QPainter, QPen, QPixmap, QShortcut, QTextCursor,
)
from PySide6.QtWidgets import (
    QAbstractButton, QComboBox, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
    QScrollArea, QTabBar, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from dstools.features.local_service.dedicated_server import ServerStatus, advance_world_ready_marker
from dstools.features.local_service.server_diagnostics import (
    analyze_mod_loading, contains_runtime_lua_error, contains_server_registration_success,
    contains_startup_failure, contains_token_conflict, diagnose_server_failure,
)
from dstools.features.local_service.shard_helpers import (
    STATUS_TEXT_KEYS, find_process_mod_folders, mod_display_names, mod_names,
)
from dstools.features.mod.icons import get_cached_mod_icon_path
from dstools.features.mod.locations import resolve_mod_open_location
from dstools.features.mod.parser import parse_modinfo
from dstools.i18n import t
from dstools.models import Platform
from dstools.qt import dialogs
from dstools.qt.mod.mod_panel import _DEFAULT_ICON_PATH
from dstools.qt.widgets import Card
from dstools.qt.theme import theme
from dstools.shared.clipboard import copy_file_to_clipboard

_COMMAND_HISTORY_LIMIT = 100
_CONSOLE_MAX_LINES = 5000  # 每个世界控制台保留的最近行数
_SEARCH_HISTORY_LIMIT = 20
_SEARCH_HIGHLIGHT = "#ffd54f"
_SEARCH_HIGHLIGHT_CURRENT = "#ff9800"
_SEARCH_HIGHLIGHT_FG = "#000000"
_SERVER_COLOR = "#2e7d32"


def status_color(status) -> str:
    return {
        ServerStatus.STARTING: theme.hex("ACCENT"),
        ServerStatus.RUNNING: _SERVER_COLOR,
        ServerStatus.STOPPING: theme.hex("ERROR"),
        ServerStatus.STOPPED: theme.hex("TEXT_MUTED"),
        ServerStatus.CRASHED: theme.hex("ERROR"),
    }[status]


class _DiagnosticDetailDialog(QDialog):
    """崩溃/异常诊断详情——非模态，内容可滚动选中复制，重复触发只更新内容并重新置顶。"""

    def __init__(self, parent):
        super().__init__(parent)
        self.setModal(False)
        self.setMinimumWidth(dialogs.DIALOG_WIDTHS["lg"])
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*dialogs.DIALOG_MARGINS)
        layout.setSpacing(dialogs.DIALOG_SPACING)
        self._view = QPlainTextEdit()
        self._view.setReadOnly(True)
        self._view.setMinimumHeight(360)
        self._view.setFont(theme.font("FONT_SIZE_SM"))
        layout.addWidget(self._view)
        self._row = QHBoxLayout()
        self._row.addStretch()
        self._close_btn = QPushButton(t("dlg.confirm_btn"))
        self._close_btn.clicked.connect(self.close)
        self._row.addWidget(self._close_btn)
        layout.addLayout(self._row)
        self._action_btns: list[QPushButton] = []

    def show_detail(self, title: str, detail: str, actions=()) -> None:
        """``actions`` 为 (按钮文字, 回调) 列表，排在“确定”左侧；点击后先关闭弹窗再执行回调。"""
        self.setWindowTitle(title)
        self._view.setPlainText(detail)
        for button in self._action_btns:
            button.setParent(None)
            button.deleteLater()
        self._action_btns = []
        for label, callback in actions:
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, cb=callback: (self.close(), cb()))
            self._row.insertWidget(self._row.count() - 1, button)
            self._action_btns.append(button)
        # 有处理按钮时它是主操作，“确定”退为次要样式
        dialogs.style_button(self._close_btn, "secondary" if self._action_btns else "primary")
        self.show()
        self.raise_()
        self.activateWindow()


class _FailedModsDialog(dialogs.Dialog):
    """加载失败的 Mod 列表：每个 Mod 一张圆角卡片，显示图标、名称、ID 与本地目录，可直接打开所在位置。"""

    _ICON = 44  # 图标边长（逻辑像素）

    def __init__(self, parent, shard_name: str, mod_ids: tuple[str, ...], folders: dict):
        super().__init__(parent, t("local.mods_failed_dialog_title", shard=shard_name), width="lg")
        hint = QLabel(t("local.mods_failed_dialog_hint", count=len(mod_ids)))
        hint.setWordWrap(True)
        hint.setProperty("muted", True)
        self.body.addWidget(hint)

        rows = QWidget()
        rows.setObjectName("failedModsInner")
        rows.setAutoFillBackground(False)
        rows_layout = QVBoxLayout(rows)
        rows_layout.setContentsMargins(0, 0, 0, 0)
        rows_layout.setSpacing(8)
        for mod_id in mod_ids:
            rows_layout.addWidget(self._mod_card(mod_id, folders.get(mod_id)))
        rows_layout.addStretch()
        area = QScrollArea()
        area.setObjectName("failedModsArea")
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setWidgetResizable(True)
        # 视口和内层都不自绘底色，否则会盖出一块灰色底
        area.viewport().setAutoFillBackground(False)
        area.setStyleSheet("#failedModsArea, #failedModsInner { background: transparent; border: none; }")
        area.setWidget(rows)
        self.body.addWidget(area, 1)

        close = QPushButton(t("dlg.close_btn"))
        close.clicked.connect(self.accept)
        self.add_footer(right=[close])
        dialogs.fit_to_screen(self, dialogs.DIALOG_WIDTHS["lg"], 170 + 78 * min(len(mod_ids), 6))

    def _mod_card(self, mod_id: str, folder) -> QWidget:
        info = None
        if folder is not None:
            try:
                info = parse_modinfo(folder)
            except (OSError, ValueError, TypeError):
                info = None
        card = Card(radius=12, alpha=170)
        row = QHBoxLayout(card)
        row.setContentsMargins(12, 10, 12, 10)
        row.setSpacing(12)

        icon = QLabel()
        icon.setFixedSize(self._ICON, self._ICON)
        pixmap = self._icon_pixmap(info, folder)
        if pixmap is not None:
            icon.setPixmap(pixmap)
        row.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)

        text = QVBoxLayout()
        text.setSpacing(3)
        title = QLabel((info.name or "").strip() if info and info.name else mod_id)
        title.setFont(theme.font("FONT_SIZE_MD", bold=True))
        title.setWordWrap(True)
        text.addWidget(title)
        detail = QLabel(f"{mod_id}  ·  {folder}" if folder is not None else t("local.mods_failed_not_found", id=mod_id))
        detail.setFont(theme.font("FONT_SIZE_XS"))
        detail.setWordWrap(True)
        detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        if folder is None:
            detail.setStyleSheet(f"color: {theme.hex('ERROR')};")
        else:
            detail.setProperty("muted", True)
        text.addWidget(detail)
        row.addLayout(text, 1)

        open_btn = dialogs.style_button(QPushButton(t("env.open_location")), "secondary")
        open_btn.setEnabled(folder is not None)
        open_btn.clicked.connect(lambda _checked=False, m=mod_id, f=folder: self._open(m, f))
        row.addWidget(open_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        return card

    def _icon_pixmap(self, info, folder) -> QPixmap | None:
        """优先用已转换好的图标缓存（不在界面线程里跑 ktech 转换），没有时用默认图标。"""
        path = None
        if info is not None and folder is not None:
            try:
                path = get_cached_mod_icon_path(info, folder, Platform.STEAM)
            except OSError:
                path = None
        source = QPixmap(str(path if path is not None else _DEFAULT_ICON_PATH))
        if source.isNull():
            return None
        # 按屏幕缩放比缩到物理像素再设 devicePixelRatio，避免被二次放大发虚
        dpr = self.devicePixelRatioF()
        side = round(self._ICON * dpr)
        pixmap = source.scaled(side, side, Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
        pixmap.setDevicePixelRatio(dpr)
        return pixmap

    def _open(self, mod_id: str, folder) -> None:
        path = resolve_mod_open_location(mod_id, folder)
        if path is None:
            dialogs.show_warning(self, t("env.open_location"), t("mod.open_location_missing"))
            return
        try:
            os.startfile(str(path))
        except OSError as exc:
            dialogs.show_error(self, t("env.open_location"), str(exc))


# 世界页签外边距（逻辑像素），与 theme.py 中 #consoleTabs QTabBar::tab 的 margin 一致：
# 上方留给压在页签右上角的关闭角标；右边距即相邻页签的间隔。
TAB_MARGIN_TOP = 6
TAB_MARGIN_RIGHT = 2


class TabCloseButton(QAbstractButton):
    """压在世界页签右上角的关闭角标：平时淡灰色圆底灰 ×，悬停变红色圆底白 ×。"""

    _SIZE = 16

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(t("local.console_close_btn"))
        self.setFixedSize(self._SIZE, self._SIZE)

    def sizeHint(self) -> QSize:
        return QSize(self._SIZE, self._SIZE)

    def enterEvent(self, event) -> None:
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        hover = self.underMouse()
        # 平时垫一个很淡的灰色圆底；悬停换成红色圆底，提示这是关闭。
        painter.setPen(Qt.PenStyle.NoPen)
        if hover:
            painter.setBrush(theme.color("ERROR"))
        else:
            faint = theme.color("TEXT_MUTED")
            faint.setAlpha(22)
            painter.setBrush(faint)
        painter.drawEllipse(QRectF(0.75, 0.75, self._SIZE - 1.5, self._SIZE - 1.5))
        if hover:
            color = QColor("#FFFFFF")
        else:
            # 平时 × 也半透明，不抢页签文字的视线。
            color = theme.color("TEXT_MUTED")
            color.setAlpha(140)
        painter.setPen(QPen(color, 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        a, b = 5.5, self._SIZE - 5.5
        painter.drawLine(QPointF(a, a), QPointF(b, b))
        painter.drawLine(QPointF(b, a), QPointF(a, b))


class _ConsoleTabBar(QTabBar):
    """世界页签条：关闭角标手动摆到页签右上角（setTabButton 只能放在文字左右），布局变化时重新定位。"""

    def place_close_buttons(self) -> None:
        alive = set()
        for index in range(self.count()):
            button = self.tabData(index)
            if not isinstance(button, TabCloseButton):
                continue
            alive.add(button)
            # tabRect 含 QSS 外边距，扣掉后才是页签本体；角标压在页签右上角，右缘越过页签 5px，
            # 中心在上边往下 2px 处（再往上会超出页签条顶部被裁掉）。
            pill = self.tabRect(index).adjusted(0, TAB_MARGIN_TOP, -TAB_MARGIN_RIGHT, 0)
            half = button.width() / 2
            button.move(round(pill.right() + 1 + 5 - button.width()), round(pill.top() + 2 - half))
            button.raise_()
            button.show()
        # 页签被移除/清空后，tabData 里不再引用的角标一并释放。
        for button in self.findChildren(TabCloseButton):
            if button not in alive:
                button.hide()
                button.deleteLater()

    def sizeHint(self) -> QSize:
        # 角标越过最后一个页签右缘 3px，页签条加宽一点免得被裁掉
        hint = super().sizeHint()
        return QSize(hint.width() + 4, hint.height())

    def tabLayoutChange(self) -> None:
        super().tabLayoutChange()
        self.place_close_buttons()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.place_close_buttons()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.place_close_buttons()


class ConsoleTabWidget(QTabWidget):
    """世界控制台页签：页签右上角带关闭角标（替代原控制台底部的"关闭窗口"按钮）。"""

    _DOT = 8  # 状态圆点直径（逻辑像素）
    # 图标按整体垂直居中摆放，圆点贴着居中时视觉上偏高；图标往下加高这么多，圆点画在下部，整体下移一半。
    _DOT_DROP = 4

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("consoleTabs")  # theme.py 据此让页签贴住日志框
        self.setTabBar(_ConsoleTabBar())
        # 页签不拉伸，页签条多出的宽度留给最后一个角标（须在 setTabBar 之后设，否则被重置）
        self.tabBar().setExpanding(False)
        self.setIconSize(QSize(self._DOT + 2, self._DOT + 2 + self._DOT_DROP))
        self._dot_icons: dict[tuple[str, float], QIcon] = {}

    def add_console_tab(self, pane: "ConsolePane", title: str) -> int:
        index = self.addTab(pane, title)
        bar = self.tabBar()
        button = TabCloseButton(bar)
        # clicked 会带 checked 参数，包一层避免传给 request_close。
        button.clicked.connect(lambda _checked=False: pane.request_close())
        bar.setTabData(index, button)
        bar.place_close_buttons()
        # 切换存档时同一个 pane 会被反复移除/重新加入，信号只连一次。
        if not getattr(pane, "_status_dot_connected", False):
            pane._status_dot_connected = True
            pane.status_changed.connect(lambda status, p=pane: self._set_status_dot(p, status))
        self._set_status_dot(pane, pane.proc.status)
        return index

    def _set_status_dot(self, pane: "ConsolePane", status) -> None:
        """页签名左侧的状态圆点，配色与控制台状态文字一致（运行中绿、正在停止红……）。"""
        index = self.indexOf(pane)
        if index < 0:
            return
        self.setTabIcon(index, self._dot_icon(status_color(status)))

    def _dot_icon(self, color: str) -> QIcon:
        # 按屏幕缩放比画到物理像素再设 devicePixelRatio，避免被二次放大发虚。
        dpr = self.devicePixelRatioF()
        key = (color, dpr)
        if key not in self._dot_icons:
            box_w, box_h = self._DOT + 2, self._DOT + 2 + self._DOT_DROP
            pixmap = QPixmap(round(box_w * dpr), round(box_h * dpr))
            pixmap.setDevicePixelRatio(dpr)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(color))
            painter.drawEllipse(QRectF(1, 1 + self._DOT_DROP, self._DOT, self._DOT))
            painter.end()
            self._dot_icons[key] = QIcon(pixmap)
        return self._dot_icons[key]


class ConsolePane(QWidget):
    # 世界运行状态变化时发出（只在变化时发，定时 pump 不会重复触发），供页签状态圆点刷新。
    status_changed = Signal(object)

    def __init__(self, proc, on_close, on_rollback, on_failure=None, on_registered=None, on_export_mods=None,
                 token_conflict_actions=None, parent=None):
        super().__init__(parent)
        self._on_export_mods = on_export_mods
        # 令牌冲突诊断弹窗里的处理按钮：proc -> [(文字, 回调)]，每次弹出时按当时的令牌池现算
        self._token_conflict_actions = token_conflict_actions
        # Mod 横幅点击时的去向：全部成功→导出图片；有失败→失败列表
        self._mod_failed_ids: tuple[str, ...] = ()
        self._diagnostic_category = ""
        self._last_status = None
        self.proc = proc
        self._on_close = on_close
        self._on_rollback = on_rollback
        self._on_failure = on_failure
        self._on_registered = on_registered

        self._mod_check_reported = False
        self._mod_check_real_start_seen = False
        self._mod_check_ready_seen = False
        self._diagnostic_reported = False
        self._registration_reported = False
        self._diagnostic_detail = ""
        self._diagnostic_detail_title = ""
        self._detail_dialog: _DiagnosticDetailDialog | None = None
        self._command_history: list[str] = []
        self._command_history_index = 0
        self._command_draft = ""
        self._search_matches: list[tuple[int, int]] = []
        self._search_index = -1

        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(4)

        self._mod_status_label = self._banner_label(clickable=True)
        self._mod_status_label.mousePressEvent = self._on_mod_status_clicked
        self._diagnostic_label = self._banner_label(clickable=True)
        self._diagnostic_label.mousePressEvent = self._show_diagnostic_detail
        outer.addWidget(self._mod_status_label)
        outer.addWidget(self._diagnostic_label)

        self._search_bar = QWidget()
        search_row = QHBoxLayout(self._search_bar)
        search_row.setContentsMargins(0, 0, 0, 4)
        # 搜索框用可编辑 QComboBox，既保留实时搜索，又能在下拉里留存历史搜索词
        self._search_edit = QComboBox()
        self._search_edit.setEditable(True)
        self._search_edit.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._search_line = self._search_edit.lineEdit()
        self._search_line.setFont(theme.font("FONT_SIZE_SM"))
        self._search_line.setPlaceholderText(t("local.console_search_placeholder"))
        self._search_line.textChanged.connect(self._run_search)
        self._search_line.returnPressed.connect(lambda: self._search_step(1))
        self._search_count = QLabel()
        self._search_count.setFont(theme.font("FONT_SIZE_SM"))
        self._search_count.setProperty("muted", True)
        up_btn, down_btn, close_btn = QPushButton("↑"), QPushButton("↓"), QPushButton("×")
        up_btn.setToolTip(t("local.console_search_prev"))
        down_btn.setToolTip(t("local.console_search_next"))
        close_btn.setToolTip(t("local.console_search_clear"))
        for button, handler in ((up_btn, lambda: self._search_step(-1)),
                                 (down_btn, lambda: self._search_step(1)),
                                 (close_btn, self._close_search)):
            button.setFixedWidth(28)
            dialogs.style_button(button, "secondary")
            # 浅色描边按钮默认左右内边距 15px，28px 宽的小按钮会把 ↑ ↓ × 挤没，这里清零
            button.setStyleSheet("QPushButton { padding: 0px; }")
            button.clicked.connect(handler)
        search_row.addWidget(self._search_edit, 1)
        search_row.addWidget(self._search_count)
        search_row.addWidget(up_btn)
        search_row.addWidget(down_btn)
        search_row.addWidget(close_btn)
        # 搜索栏常驻显示，提供可见入口（也可 Ctrl+F）
        # Shift+Enter 跳到上一个。可编辑 QComboBox 的焦点实际在下拉框本身，按键由它直接转给
        # 内部输入框，不经过输入框上的过滤器，所以下拉框也要装（只装输入框时 Shift+Enter 仍是下一个）
        self._search_edit.installEventFilter(self)
        self._search_line.installEventFilter(self)
        outer.addWidget(self._search_bar)

        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        # 真实专服日志实测：每 2 万行约 27 MB、5000 行约 8 MB，五分片长时间运行差出上百 MB；
        # 完整日志在各世界的 server_log.txt，这里只留最近的供查看和 Ctrl+F 搜索
        self.text.setMaximumBlockCount(_CONSOLE_MAX_LINES)
        self.text.setFont(theme.font("FONT_SIZE_SM"))
        outer.addWidget(self.text, 1)

        for shortcut_target in (self, self.text):
            shortcut = QShortcut(QKeySequence("Ctrl+F"), shortcut_target)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(self._open_search)
        escape = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        escape.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        escape.activated.connect(self._close_search)

        bottom = QHBoxLayout()
        self.status_label = QLabel()
        self.status_label.setFont(theme.font("FONT_SIZE_SM", bold=True))
        self.cmd_edit = QLineEdit()
        self.cmd_edit.setToolTip(t("local.console_placeholder"))
        self.cmd_edit.returnPressed.connect(self._send)
        self.cmd_edit.installEventFilter(self)
        self.send_btn = QPushButton(t("local.console_send_btn"))
        self.send_btn.setFont(theme.font("FONT_SIZE_SM"))
        self.send_btn.clicked.connect(self._send)
        bottom.addWidget(self.status_label)
        bottom.addWidget(self.cmd_edit, 1)
        bottom.addWidget(self.send_btn)
        outer.addLayout(bottom)

        quick = QHBoxLayout()
        self.announce_btn = QPushButton(t("local.console_announce_btn"))
        self.announce_btn.clicked.connect(self._announce)
        self.list_players_btn = QPushButton(t("local.console_list_players_btn"))
        self.list_players_btn.clicked.connect(lambda: self.proc.send_command("c_listallplayers()"))
        quick.addWidget(self.announce_btn)
        quick.addWidget(self.list_players_btn)
        self.rollback_btn = None
        self.reset_world_btn = None
        self.save_btn = None
        if getattr(proc, "is_master", True):
            self.rollback_btn = QPushButton(t("local.rollback_btn"))
            self.rollback_btn.clicked.connect(self._on_rollback)
            quick.addWidget(self.rollback_btn)
            self.reset_world_btn = QPushButton(t("local.console_reset_world_btn"))
            self.reset_world_btn.clicked.connect(self._reset_world)
            quick.addWidget(self.reset_world_btn)
            self.save_btn = QPushButton(t("local.console_save_btn"))
            self.save_btn.clicked.connect(lambda: self.proc.send_command("c_save()"))
            quick.addWidget(self.save_btn)
        self.copy_log_btn = QPushButton(t("local.console_copy_log_btn"))
        self.copy_log_btn.clicked.connect(self._copy_world_log)
        quick.addWidget(self.copy_log_btn)
        quick.addStretch()
        outer.addLayout(quick)
        for button in (self.announce_btn, self.list_players_btn, self.rollback_btn, self.reset_world_btn,
                      self.save_btn, self.copy_log_btn):
            if button is not None:
                button.setFont(theme.font("FONT_SIZE_SM"))

        self.pump()

    def _banner_label(self, clickable: bool = False) -> QLabel:
        label = QLabel("")
        label.setFont(theme.font("FONT_SIZE_SM", bold=True))
        label.setContentsMargins(10, 2, 10, 2)
        label.setVisible(False)
        if clickable:
            label.setCursor(Qt.CursorShape.PointingHandCursor)
        return label

    # ── 搜索 ────────────────────────────────────────────────────────────
    def _open_search(self) -> None:
        self._search_bar.setVisible(True)
        self._search_line.setFocus()
        self._search_line.selectAll()
        self._run_search()

    def _close_search(self) -> None:
        """× / Esc：清空搜索词和高亮（搜索栏常驻）。"""
        self._remember_search()
        self._search_edit.blockSignals(True)
        self._search_line.blockSignals(True)
        self._search_edit.clearEditText()
        self._search_edit.blockSignals(False)
        self._search_line.blockSignals(False)
        self._search_count.setText("")
        self.text.setExtraSelections([])
        self._search_matches = []
        self._search_index = -1
        self.text.setFocus()

    def _remember_search(self) -> None:
        """把当前搜索词加入下拉历史（去重、置顶、限量），供下次快速重选。"""
        text = self._search_edit.currentText().strip()
        if not text:
            return
        self._search_edit.blockSignals(True)
        self._search_line.blockSignals(True)
        try:
            index = self._search_edit.findText(text)
            if index >= 0:
                self._search_edit.removeItem(index)
            self._search_edit.insertItem(0, text)
            while self._search_edit.count() > _SEARCH_HISTORY_LIMIT:
                self._search_edit.removeItem(self._search_edit.count() - 1)
            # insertItem 会改变 currentIndex（把输入框显示带成相邻历史词），这里拉回当前搜索词
            self._search_edit.setCurrentText(text)
        finally:
            self._search_edit.blockSignals(False)
            self._search_line.blockSignals(False)

    def _run_search(self) -> None:
        query = self._search_edit.currentText()
        self._search_matches = []
        self._search_index = -1
        if not query:
            self._search_count.setText("")
            self.text.setExtraSelections([])
            return
        cursor = QTextCursor(self.text.document())
        while True:
            cursor = self.text.document().find(query, cursor)
            if cursor.isNull():
                break
            self._search_matches.append((cursor.selectionStart(), cursor.selectionEnd()))
        if self._search_matches:
            self._search_index = 0
            self._show_matches()
        else:
            self._search_count.setText(t("local.console_search_no_match"))

    def _search_step(self, direction: int) -> None:
        self._remember_search()
        if not self._search_matches:
            return
        self._search_index = (self._search_index + direction) % len(self._search_matches)
        self._show_matches()

    def _show_matches(self) -> None:
        selections = []
        for index, (start, end) in enumerate(self._search_matches):
            selection = QTextEdit.ExtraSelection()  # PySide6 里 ExtraSelection 只挂在 QTextEdit 上
            cursor = QTextCursor(self.text.document())
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            selection.cursor = cursor
            current = index == self._search_index
            selection.format.setBackground(QColor(_SEARCH_HIGHLIGHT_CURRENT if current else _SEARCH_HIGHLIGHT))
            selection.format.setForeground(QColor(_SEARCH_HIGHLIGHT_FG))
            selections.append(selection)
        self.text.setExtraSelections(selections)
        if self._search_index >= 0:
            start, _end = self._search_matches[self._search_index]
            cursor = self.text.textCursor()
            cursor.setPosition(start)
            self.text.setTextCursor(cursor)
            self.text.ensureCursorVisible()
        self._search_count.setText(
            t("local.console_search_count", current=self._search_index + 1, total=len(self._search_matches)))

    # ── 命令输入 ────────────────────────────────────────────────────────
    def eventFilter(self, watched, event):
        if (watched in (self._search_edit, self._search_line) and event.type() == QEvent.Type.KeyPress
                and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self._search_step(-1)
            return True
        # 搜索框的过滤器在构造早期就装上了，那时命令输入框还没建
        if watched is getattr(self, "cmd_edit", None) and event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Up:
                self._browse_history(-1)
                return True
            if event.key() == Qt.Key.Key_Down:
                self._browse_history(1)
                return True
        return super().eventFilter(watched, event)

    def request_close(self) -> None:
        """页签上的 × 调用：走与原"关闭窗口"按钮相同的关闭流程（运行中会先确认）。"""
        self._on_close()

    def _send(self) -> None:
        cmd = self.cmd_edit.text().strip()
        if cmd and self.proc.send_command(cmd):
            if not self._command_history or self._command_history[-1] != cmd:
                self._command_history.append(cmd)
                del self._command_history[:-_COMMAND_HISTORY_LIMIT]
            self._command_history_index = len(self._command_history)
            self._command_draft = ""
            self.cmd_edit.setText("")

    def _browse_history(self, direction: int) -> None:
        if not self._command_history:
            return
        history_len = len(self._command_history)
        if direction < 0:
            if self._command_history_index >= history_len:
                self._command_draft = self.cmd_edit.text()
            if self._command_history_index > 0:
                self._command_history_index -= 1
        elif self._command_history_index < history_len - 1:
            self._command_history_index += 1
        elif self._command_history_index == history_len - 1:
            self._command_history_index = history_len
        value = (self._command_history[self._command_history_index]
                 if self._command_history_index < history_len else self._command_draft)
        self.cmd_edit.setText(value)
        self.cmd_edit.end(False)

    # ── 常用指令 ────────────────────────────────────────────────────────
    def _announce(self) -> None:
        dialog = dialogs.TextInputDialog(
            self.window(), t("local.console_announce_btn"), t("local.console_announce_prompt"))
        if not dialog.exec() or not dialog.result_text:
            return
        text = dialog.result_text.strip()
        if not text:
            return
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        self.proc.send_command(f'c_announce("{escaped}")')

    def _reset_world(self) -> None:
        if not dialogs.ask_yes_no(
                self.window(), t("local.console_reset_world_confirm_title"),
                t("local.console_reset_world_confirm_msg"), min_width=520, danger=True):
            return
        self.proc.send_command("c_regenerateworld()")

    def _copy_world_log(self) -> None:
        log_path = Path(self.proc.cluster_path) / self.proc.shard_name / "server_log.txt"
        if not log_path.is_file():
            dialogs.show_warning(
                self.window(), t("local.console_copy_log_btn"),
                t("local.console_log_not_found", path=str(log_path)))
            return
        copied = copy_file_to_clipboard(log_path)
        if not copied:
            QGuiApplication.clipboard().setText(str(log_path))
        dialogs.show_toast(
            self, t("local.console_log_copied" if copied else "local.console_log_path_copied"))

    # ── 诊断详情 ────────────────────────────────────────────────────────
    def _show_diagnostic_detail(self, _event=None) -> None:
        if not self._diagnostic_detail:
            return
        if self._detail_dialog is None:
            self._detail_dialog = _DiagnosticDetailDialog(self.window())
        actions = ()
        if self._diagnostic_category == "token_conflict" and self._token_conflict_actions is not None:
            actions = self._token_conflict_actions(self.proc)
        self._detail_dialog.show_detail(self._diagnostic_detail_title, self._diagnostic_detail, actions)

    def _show_report(self, report) -> None:
        """把诊断结果写到横幅和详情，并自动弹出一次详情窗。"""
        self._diagnostic_category = report.category
        self._diagnostic_label.setText(t("local.console_diag_banner", title=report.title))
        self._diagnostic_label.setStyleSheet(
            f"background: {theme.hex('BANNER_BG')}; color: {theme.hex('BANNER_TEXT')};")
        self._diagnostic_label.setVisible(True)
        detail = report.summary + "\n\n" + t("local.console_diag_tips") + "\n" + "\n".join(
            f"{index}. {suggestion}" for index, suggestion in enumerate(report.suggestions, 1))
        if report.related_mods:
            related = mod_display_names(self.proc, report.related_mods)
            detail += "\n\n" + t("local.console_diag_related_mods") + "\n" + "\n".join(related[:8])
            if len(related) > 8:
                detail += "\n" + t("local.console_diag_more_mods", count=len(related) - 8)
        if report.evidence:
            detail += "\n\n" + t("local.console_diag_evidence") + "\n" + "\n".join(report.evidence)
        self._diagnostic_detail_title = report.title
        self._diagnostic_detail = detail
        QTimer.singleShot(0, self._show_diagnostic_detail)

    def _on_mod_status_clicked(self, _event=None) -> None:
        """Mod 横幅点击：有失败时列出失败的 Mod，全部成功时导出已加载 Mod 的图片。"""
        if self._mod_failed_ids:
            ids = self._mod_failed_ids
            folders = find_process_mod_folders(self.proc, ids)
            _FailedModsDialog(self.window(), self.proc.shard_name, ids, folders).exec()
        elif self._on_export_mods is not None:
            self._on_export_mods(self.proc)

    def _diagnostic_log_lines(self) -> tuple[str, ...]:
        """合并管道日志和 server_log.txt，覆盖专服 stdout 缓冲导致的漏行。"""
        lines = list(self.proc.recent_log_lines)
        try:
            log_path = Path(self.proc.cluster_path) / self.proc.shard_name / "server_log.txt"
            if log_path.is_file():
                file_lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-500:]
                for line in file_lines:
                    if line not in lines:
                        lines.append(line)
        except (OSError, UnicodeError):
            pass
        return tuple(lines[-700:])

    # ── 生命周期 ────────────────────────────────────────────────────────
    def rebind(self, proc) -> None:
        """同一个世界停止后重新启动时复用这个标签页，而不是每次都开一个新的。"""
        self.proc = proc
        self._close_search()
        self.text.clear()
        self._mod_status_label.setVisible(False)
        self._mod_failed_ids = ()
        self._diagnostic_category = ""
        self._mod_check_reported = False
        self._mod_check_real_start_seen = False
        self._mod_check_ready_seen = False
        self._diagnostic_reported = False
        self._registration_reported = False
        self._diagnostic_label.setVisible(False)
        self._diagnostic_detail = ""
        self._diagnostic_detail_title = ""
        if self._detail_dialog is not None:
            self._detail_dialog.close()
            self._detail_dialog = None
        self.pump()

    def pump(self) -> None:
        """轮询一次：把新到的输出行追加到日志，同步状态徽标/命令框可用性。"""
        lines = self.proc.read_available_lines(max_lines=500)
        if lines:
            for line in lines:
                self._mod_check_real_start_seen, ready_now = advance_world_ready_marker(
                    line, self.proc.is_master, self._mod_check_real_start_seen)
                self._mod_check_ready_seen |= ready_now
            scrollbar = self.text.verticalScrollBar()
            at_bottom = scrollbar.value() >= scrollbar.maximum() - 2 or not self.isVisible()
            self.text.appendPlainText("\n".join(lines))
            if at_bottom:
                scrollbar.setValue(scrollbar.maximum())

        expected_shutdown = getattr(self.proc, "intentional_shutdown", False)
        token_conflict_now = (not expected_shutdown and not self._diagnostic_reported
                               and contains_token_conflict(lines))
        runtime_lua_error_now = (not expected_shutdown and self.proc.world_ready
                                  and not self._diagnostic_reported and contains_runtime_lua_error(lines))
        startup_failed_now = (not expected_shutdown and not self.proc.world_ready
                               and not self._diagnostic_reported and contains_startup_failure(lines))
        exit_code = self.proc.sync_expected_exit()
        status = self.proc.status
        crashed_now = False
        if status in (ServerStatus.STARTING, ServerStatus.RUNNING) and exit_code is not None:
            self.proc.status = ServerStatus.CRASHED
            status = ServerStatus.CRASHED
            crashed_now = True
        first_report = (crashed_now or startup_failed_now or token_conflict_now or runtime_lua_error_now) \
            and not self._diagnostic_reported
        # 令牌冲突不会让进程退出，之后真崩溃时横幅要改成崩溃原因；失败回调（令牌等待、自动重启）仍只触发一次
        crash_after_conflict = crashed_now and self._diagnostic_category == "token_conflict"
        if first_report or crash_after_conflict:
            self._diagnostic_reported = True
            report = diagnose_server_failure(
                shard_name=getattr(self.proc, "shard_name", t("local.console_current_world")),
                exit_code=exit_code, world_ready=self.proc.world_ready,
                log_lines=self._diagnostic_log_lines(),
                enabled_mods=self.proc.mods_enabled, loaded_mods=self.proc.mods_loaded,
                ignore_token_conflict=crash_after_conflict)
            if report is not None:
                if first_report and self._on_failure is not None:
                    self._on_failure(self.proc, report)
                self._show_report(report)
        registration_succeeded_now = (
            getattr(self.proc, "is_master", True) and not self._registration_reported
            and not token_conflict_now and contains_server_registration_success(lines))
        if registration_succeeded_now:
            self._registration_reported = True
            if self._on_registered is not None:
                self._on_registered(self.proc)

        self.status_label.setText(t(STATUS_TEXT_KEYS[status]))
        self.status_label.setStyleSheet(f"color: {status_color(status)};")
        if status != self._last_status:
            self._last_status = status
            self.status_changed.emit(status)
        can_send = status == ServerStatus.RUNNING
        world_ready = can_send and self.proc.world_ready
        self.cmd_edit.setEnabled(can_send)
        self.send_btn.setEnabled(can_send)
        self.announce_btn.setEnabled(world_ready)
        self.list_players_btn.setEnabled(world_ready)
        hint = t("local.world_not_ready_hint") if can_send and not world_ready else ""
        self.announce_btn.setToolTip(hint)
        self.list_players_btn.setToolTip(hint)
        if self.rollback_btn is not None:
            self.rollback_btn.setEnabled(world_ready)
        has_log = (Path(self.proc.cluster_path) / self.proc.shard_name / "server_log.txt").is_file()
        self.copy_log_btn.setEnabled(has_log)
        if self.reset_world_btn is not None:
            self.reset_world_btn.setEnabled(world_ready)
            self.reset_world_btn.setToolTip(hint or t("local.console_reset_world_hover"))
        if self.save_btn is not None:
            self.save_btn.setEnabled(world_ready)
            self.save_btn.setToolTip(hint)

        if (world_ready and self._mod_check_ready_seen and not self._mod_check_reported
                and self.proc.missing_mods is not None):
            self._mod_check_reported = True
            mod_status = analyze_mod_loading(
                enabled_mods=self.proc.mods_enabled, loaded_mods=self.proc.mods_loaded,
                failed_mods=getattr(self.proc, "mods_failed", ()), visible_mod_count=self.proc.visible_mod_count)
            if mod_status.failed_mods:
                self._mod_failed_ids = mod_status.failed_mods
                # 横幅只列前 3 个名称（读不到名称时用 ID），完整列表点击后查看
                names = mod_names(find_process_mod_folders(self.proc, mod_status.failed_mods[:3]))
                shown = ", ".join(names.get(mod_id, mod_id) for mod_id in mod_status.failed_mods[:3])
                if len(mod_status.failed_mods) > 3:
                    shown += " …"
                self._mod_status_label.setText(t(
                    "local.mods_check_failed", failed_count=len(mod_status.failed_mods), ids=shown))
                self._mod_status_label.setStyleSheet(
                    f"background: {theme.hex('BANNER_BG')}; color: {theme.hex('BANNER_TEXT')};")
                self._mod_status_label.setVisible(True)
            elif mod_status.visible_mod_count:
                self._mod_status_label.setText(t("local.mods_check_ok", count=mod_status.visible_mod_count))
                self._mod_status_label.setStyleSheet(f"color: {_SERVER_COLOR};")
                self._mod_status_label.setVisible(True)
