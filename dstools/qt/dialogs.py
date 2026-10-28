"""Qt 通用对话框：消息框、文件位置、日志窗口及存档信息页用到的输入对话框（样式由全局 QSS 提供）。"""

import ctypes
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import (
    QEasingCurve, QPoint, QPropertyAnimation, QRect, QRectF, QSequentialAnimationGroup, Qt, QTimer,
)
from PySide6.QtGui import (
    QBrush, QColor, QFont, QFontMetrics, QGuiApplication, QIntValidator, QKeySequence, QPainter, QPainterPath,
    QPen, QTextCursor,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QGraphicsOpacityEffect, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QListWidget, QMenu, QPushButton, QTableWidget, QTableWidgetItem, QTextEdit, QToolTip, QVBoxLayout,
    QWidget,
)

from dstools.features.local_service.backup_manager import get_backup_summary, is_kept_backup
from dstools.i18n import t
from dstools.qt.theme import theme
from dstools.qt.threads import run_async
from dstools.qt.widgets import ToggleSwitch, section_card
from dstools.shared.app_settings import (
    get_backup_auto_enabled, get_backup_interval_minutes, get_backup_retention, get_lite_mode_enabled,
    set_backup_auto_enabled, set_backup_interval_minutes, set_backup_retention,
)


# ── 弹窗规范 ────────────────────────────────────────────────────────────
# 弹窗宽度只在三档里选：sm 提示/确认短消息；md 表单、列表、较长说明；lg 日志、报告、多段详情。
DIALOG_WIDTHS = {"sm": 420, "md": 560, "lg": 760}
DIALOG_MARGINS = (20, 18, 20, 16)  # 左、上、右、下
DIALOG_SPACING = 10
SCREEN_FILL_RATIO = 0.9  # 大窗口最多占所在显示器工作区的比例，留出边距避免贴边或越界


def screen_work_area(widget: QWidget | None = None) -> QRect:
    """控件所在（或其父窗口所在）显示器的工作区，逻辑像素，已扣除任务栏。"""
    screen = None
    if widget is not None:
        anchor = widget.parentWidget() or widget
        screen = anchor.screen() if anchor.isVisible() else None
        if screen is None:
            screen = QGuiApplication.screenAt(anchor.mapToGlobal(anchor.rect().center()))
    return (screen or QGuiApplication.primaryScreen()).availableGeometry()


def fit_to_screen(widget: QWidget, width: int, height: int, area: QRect | None = None) -> None:
    """按期望尺寸 resize，但不超过工作区的 SCREEN_FILL_RATIO（最小尺寸同样压到工作区内）。

    高缩放时逻辑工作区可能只有 1100~1460×600~800，写死大尺寸会让底部按钮点不到。"""
    area = area or screen_work_area(widget)
    max_w = int(area.width() * SCREEN_FILL_RATIO)
    max_h = int(area.height() * SCREEN_FILL_RATIO)
    minimum = widget.minimumSize()
    if minimum.width() > area.width() or minimum.height() > area.height():
        widget.setMinimumSize(min(minimum.width(), area.width()), min(minimum.height(), area.height()))
    widget.resize(min(width, max_w), min(height, max_h))


def width_tier(min_width: int) -> str:
    """把旧调用方传的 min_width 像素值归到最接近的宽度档位。"""
    if min_width <= 460:
        return "sm"
    if min_width <= 640:
        return "md"
    return "lg"


def style_button(button: QPushButton, variant: str) -> QPushButton:
    """按钮外观：primary 主题色实心（默认）、secondary 浅色描边（取消类）、danger 红色（删除类）。"""
    button.setProperty("variant", variant)
    button.style().unpolish(button)
    button.style().polish(button)
    return button


# ── 消息框 ──────────────────────────────────────────────────────────────

# 图标：底色键（None 表示固定色）、固定色、符号。警告用固定琥珀色，各主题里都醒目。
_MESSAGE_ICONS = {
    "info": ("PRIMARY", "", "i"),
    "warning": (None, "#E6A23C", "!"),
    "error": ("ERROR", "", "×"),
    "question": ("ACCENT", "", "?"),
}


class _MessageIcon(QWidget):
    """消息框左侧的圆形图标，自绘、跟随主题色，不用系统图标。"""

    _SIZE = 34

    def __init__(self, kind: str, parent=None):
        super().__init__(parent)
        self._kind = kind
        self.setFixedSize(self._SIZE, self._SIZE)

    def paintEvent(self, _event):
        color_key, fixed, glyph = _MESSAGE_ICONS.get(self._kind, _MESSAGE_ICONS["info"])
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme.color(color_key) if color_key else QColor(fixed))
        painter.drawEllipse(QRectF(self.rect()).adjusted(1, 1, -1, -1))
        font = theme.font("FONT_SIZE_LG", bold=True)
        painter.setFont(font)
        painter.setPen(QColor("white"))
        painter.drawText(self.rect(), int(Qt.AlignmentFlag.AlignCenter), glyph)


_ZWSP = "​"
_LONG_TOKEN_RE = re.compile(r"\S{30,}")
_PATH_SEP_RE = re.compile(r"([\\/])")


def _breakable_long_tokens(text: str) -> str:
    """长路径没有空格，QLabel 找不到换行点会被截断：在长片段的 \\ / 后插入零宽空格，让它在目录分隔处换行。"""
    return _LONG_TOKEN_RE.sub(lambda m: _PATH_SEP_RE.sub(r"\1" + _ZWSP, m.group()), text)


class _MessageBody(QLabel):
    """消息正文：可选中复制；复制时去掉为换行插入的零宽空格，粘贴出来的路径能直接用。"""

    def _copy(self, text: str) -> None:
        QGuiApplication.clipboard().setText(text.replace(_ZWSP, ""))

    def keyPressEvent(self, event) -> None:
        if event.matches(QKeySequence.StandardKey.Copy) and self.hasSelectedText():
            self._copy(self.selectedText())
            return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        menu.addAction(t("dlg.copy_btn"), lambda: self._copy(self.selectedText() or self.text()))
        menu.exec(event.globalPos())


class MessageDialog(QDialog):
    """统一样式的消息框，替代 QMessageBox（会把正文重设为 9pt 系统字体、按钮样式不统一）。

    buttons 为 [(文字, 返回值, 样式)]，样式 primary/secondary/danger；secondary 靠左，其余按顺序靠右。
    关闭窗口/Esc 返回 escape。"""

    def __init__(self, parent, kind: str, title: str, text: str, buttons: list[tuple[str, object, str]],
                 default=None, escape=None, size: str = "sm", rich: bool = False,
                 on_link=None, min_body_height: int = 0):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.result_value = escape
        self._escape = escape

        root = QVBoxLayout(self)
        root.setContentsMargins(*DIALOG_MARGINS)
        root.setSpacing(DIALOG_SPACING + 6)
        top = QHBoxLayout()
        top.setSpacing(14)
        top.addWidget(_MessageIcon(kind), 0, Qt.AlignmentFlag.AlignTop)
        # 纯文本里的长路径加换行点；富文本由调用方自己排版，不改
        self.body = _MessageBody(text if rich else _breakable_long_tokens(text))
        self.body.setObjectName("messageBody")
        self.body.setWordWrap(True)
        self.body.setTextFormat(Qt.TextFormat.RichText if rich else Qt.TextFormat.PlainText)
        self.body.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        if min_body_height:
            self.body.setMinimumHeight(min_body_height)
        if on_link is not None:
            self.body.setOpenExternalLinks(False)
            self.body.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse)
            self.body.linkActivated.connect(lambda _href: on_link())
        else:
            # 错误信息里常有路径/报错原文，允许鼠标选中复制。
            self.body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        top.addWidget(self.body, 1)
        root.addLayout(top)

        row = QHBoxLayout()
        row.setSpacing(8)
        left = [b for b in buttons if b[2] == "secondary"]
        right = [b for b in buttons if b[2] != "secondary"]
        for label, value, variant in left:
            row.addWidget(self._make_button(label, value, variant, default))
        row.addStretch()
        for label, value, variant in right:
            row.addWidget(self._make_button(label, value, variant, default))
        root.addLayout(row)
        # 固定宽度放不下按钮行时按钮会被挤窄、文字截断（如三个长按钮）：换到能放下的最小档位
        width = DIALOG_WIDTHS.get(size, DIALOG_WIDTHS["sm"])
        needed = row.sizeHint().width() + DIALOG_MARGINS[0] + DIALOG_MARGINS[2]
        if needed > width:
            width = next((w for w in sorted(DIALOG_WIDTHS.values()) if w >= needed), needed)
        self.setFixedWidth(width)

    def _make_button(self, label: str, value, variant: str, default) -> QPushButton:
        button = style_button(QPushButton(label), variant)
        button.setAutoDefault(False)
        if value == default:
            button.setDefault(True)
            button.setFocus()
        button.clicked.connect(lambda _c=False, v=value: self._pick(v))
        return button

    def _pick(self, value) -> None:
        self.result_value = value
        self.accept()

    def reject(self) -> None:
        self.result_value = self._escape
        super().reject()

    def ask(self):
        self.exec()
        return self.result_value


def _ok_buttons() -> list[tuple[str, object, str]]:
    return [(t("dlg.confirm_btn"), True, "primary")]


def show_info(parent, title: str, text: str) -> None:
    MessageDialog(parent, "info", title, text, _ok_buttons(), default=True).ask()


def show_warning(parent, title: str, text: str, min_width: int = 0) -> None:
    MessageDialog(parent, "warning", title, text, _ok_buttons(), default=True,
                  size=width_tier(min_width)).ask()


def show_error(parent, title: str, text: str, min_width: int = 0) -> None:
    MessageDialog(parent, "error", title, text, _ok_buttons(), default=True,
                  size=width_tier(min_width)).ask()


def ask_yes_no(parent, title: str, text: str, min_width: int = 0, danger: bool = False,
               rich: bool = False) -> bool:
    """确认/取消。danger=True 时确认按钮用红色（删除等不可轻易撤回的操作），并且回车
    默认落在"取消"上，误按回车不会直接执行危险操作。rich=True 时 text 按 HTML 渲染。"""
    buttons = [(t("dlg.cancel_btn"), False, "secondary"),
               (t("dlg.confirm_btn"), True, "danger" if danger else "primary")]
    return bool(MessageDialog(parent, "question", title, text, buttons, default=not danger, escape=False,
                              size=width_tier(min_width), rich=rich).ask())


def ask_choice(parent, title: str, text: str, choices: list[tuple[str, str]], default: str = "",
               min_width: int = 0, danger_values: tuple[str, ...] = (), rich: bool = False) -> str:
    """多选项询问：choices 为 [(按钮文字, 返回值)]，default 为回车默认项。

    返回值 "cancel" 的选项作为取消按钮放左侧；关闭/Esc 返回 "cancel"（有该项时）或空串。
    danger_values 中的选项用红色按钮。"""
    has_cancel = any(value == "cancel" for _label, value in choices)
    buttons = [(label, value, "secondary" if value == "cancel" else
                "danger" if value in danger_values else "primary") for label, value in choices]
    return MessageDialog(parent, "question", title, text, buttons, default=default,
                         escape="cancel" if has_cancel else "", size=width_tier(min_width), rich=rich).ask()


def ask_unsaved(parent, pages: str) -> str:
    """离开前询问如何处理未保存的改动：返回 "save" / "discard" / "cancel"。"""
    return ask_choice(parent, t("dlg.unsaved_title"), t("dlg.unsaved_msg", pages=pages),
                      [(t("dlg.cancel_btn"), "cancel"), (t("dlg.unsaved_discard_btn"), "discard"),
                       (t("dlg.unsaved_save_btn"), "save")],
                      default="save", danger_values=("discard",)) or "cancel"


class _Toast(QWidget):
    """手工绘制圆角底色、边框和文字。

    坑：QSS 的 border-radius 画在矩形原生窗口上，圆角外侧会露出黑块；这里开 WA_TranslucentBackground
    配合手绘圆角才能真正透明。整窗自绘的简单场景开这个属性是安全的，与下拉弹出容器发黑不是同一问题。"""

    _PAD_X, _PAD_Y, _RADIUS = 18, 8, 8

    def __init__(self, parent, text: str):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._text = text
        self._font = theme.font("FONT_SIZE_BASE")
        self._bg_pixmap = None
        bounds = QFontMetrics(self._font).boundingRect(text)
        self.resize(bounds.width() + self._PAD_X * 2, bounds.height() + self._PAD_Y * 2)

    def set_background_snapshot(self, pixmap) -> None:
        self._bg_pixmap = pixmap
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), self._RADIUS, self._RADIUS)
        painter.setClipPath(path)
        if self._bg_pixmap is not None:
            painter.drawPixmap(self.rect(), self._bg_pixmap)
        else:
            painter.fillPath(path, QBrush(theme.color("CARD_BG")))
        painter.setClipping(False)
        painter.setPen(QPen(theme.color("CARD_BORDER"), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        painter.setPen(theme.color("TEXT"))
        painter.setFont(self._font)
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._text)


def _toast_pos_near(anchor, toast, near) -> QPoint:
    """紧贴 near 控件上方、与它左对齐，上方放不下就放下方；整体限制在 anchor 窗口内。"""
    gap = 4
    top_left = near.mapTo(anchor, QPoint(0, 0))
    x = top_left.x()
    y = top_left.y() - toast.height() - gap
    if y < 0:
        y = top_left.y() + near.height() + gap
    x = max(0, min(x, anchor.width() - toast.width()))
    y = max(0, min(y, anchor.height() - toast.height()))
    return anchor.mapToGlobal(QPoint(x, y))


def show_toast(parent, text: str, ms: int = 1400, near=None) -> None:
    """轻提示：浮在父窗口中央（给了 near 控件时贴在它旁边），淡入淡出后自动消失，不抢焦点。

    对 QGraphicsOpacityEffect.opacity 做属性动画；背景用截取父窗口并压淡的"假透明"。"""
    anchor = parent.window() if parent is not None else None
    toast = _Toast(anchor, text)
    if anchor is not None:
        if near is not None:
            toast.move(_toast_pos_near(anchor, toast, near))
        else:
            center = anchor.mapToGlobal(anchor.rect().center())
            toast.move(center.x() - toast.width() // 2, center.y() - toast.height() // 2)
        top_left_local = anchor.mapFromGlobal(toast.mapToGlobal(QPoint(0, 0)))
        grab_rect = QRect(top_left_local, toast.size()).intersected(anchor.rect())
        if not grab_rect.isEmpty():
            pixmap = anchor.grab(grab_rect)
            if not pixmap.isNull():
                _apply_toast_fade(toast, pixmap)

    if get_lite_mode_enabled():
        # 轻量模式不做淡入淡出：透明度特效每帧离屏重绘，远程桌面下还要逐帧传输
        toast.show()
        QTimer.singleShot(ms, toast, toast.close)
        return

    effect = QGraphicsOpacityEffect(toast)
    effect.setOpacity(0.0)
    toast.setGraphicsEffect(effect)
    toast.show()

    fade_in = QPropertyAnimation(effect, b"opacity", toast)
    fade_in.setDuration(150)
    fade_in.setStartValue(0.0)
    fade_in.setEndValue(1.0)
    fade_in.setEasingCurve(QEasingCurve.Type.OutCubic)
    fade_out = QPropertyAnimation(effect, b"opacity", toast)
    fade_out.setDuration(300)
    fade_out.setStartValue(1.0)
    fade_out.setEndValue(0.0)
    fade_out.setEasingCurve(QEasingCurve.Type.InCubic)

    group = QSequentialAnimationGroup(toast)
    group.addAnimation(fade_in)
    group.addPause(max(0, ms - fade_in.duration() - fade_out.duration()))
    group.addAnimation(fade_out)
    group.finished.connect(toast.close)
    # group 以 toast 为 parent，Qt 的父子对象生命周期管理会让它跟 toast 一起存活
    # 到 close() 触发那一刻，不需要额外在 Python 侧保留引用防止被提前回收。
    group.start()


def _apply_toast_fade(toast: "_Toast", pixmap) -> None:
    """跟下拉展开列表（theme._apply_fake_transparent_popup_bg）同一个压法：白色
    叠 217/255 ≈ 85% 不透明，背景大概还剩 15% 能看出来。"""
    faded = pixmap.copy()
    painter = QPainter(faded)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
    painter.fillRect(faded.rect(), QColor(255, 255, 255, 217))
    painter.end()
    toast.set_background_snapshot(faded)


def _find_explorer_window(folder: Path) -> int:
    """找显示 folder 的资源管理器窗口（窗口类 CabinetWClass），返回 HWND，找不到返回 0。
    标题栏按系统设置显示文件夹名或完整路径，两种都认。"""
    # 单独加载一份 user32：别的模块给 windll.user32.EnumWindows 设过 argtypes（回调
    # 类型不同），共用同一个函数对象会类型不匹配。
    user32 = ctypes.WinDLL("user32")
    titles = {folder.name.casefold(), str(folder).casefold()}
    found: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        class_name = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, class_name, 64)
        if class_name.value != "CabinetWClass":
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        if title.value.casefold() in titles:
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(callback, 0)
    return found[0] if found else 0


def _open_in_explorer_foreground(path: Path) -> None:
    """在资源管理器中选中文件并把窗口提到最上层。

    坑：/select 由已运行的 explorer 开窗口，它没有前台权限会被压在下面（AllowSetForegroundWindow 也无效）；
    改为本进程轮询找到该窗口后自己 SetForegroundWindow，最多等约 3 秒。"""
    subprocess.Popen(["explorer.exe", "/select,", str(path)])
    if sys.platform != "win32":
        return
    folder = path.parent
    attempts = {"left": 20}

    def poll() -> None:
        hwnd = _find_explorer_window(folder)
        if hwnd:
            user32 = ctypes.windll.user32
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            return
        attempts["left"] -= 1
        if attempts["left"] > 0:
            QTimer.singleShot(150, poll)

    # 资源管理器本来就开着这个文件夹时窗口立刻能找到，但它要先处理完 /select，稍等再提。
    QTimer.singleShot(300, poll)


def show_file_location(parent, title: str, path, location_label: str, copied_message: str) -> None:
    """显示文件位置，点链接在资源管理器里选中该文件（并把资源管理器窗口提到最前）。"""
    path = Path(path).resolve()
    link = f'<a href="open" style="color:{theme.hex("PRIMARY")}">点我打开</a>'
    # 每行单独一段、段间留白，比 <br> 硬换行的行距更舒展；文案里的空行跳过。
    lines = [location_label, link, *(line for line in copied_message.splitlines() if line.strip())]
    text = "".join(f'<p style="margin: 0 0 10px 0;">{line}</p>' for line in lines)
    # "打包存档"和"获取日志文件"共用这个弹窗、正文行数不同：同一档宽度 + 正文最小高度，
    # 两处弹出来大小一致。
    MessageDialog(parent, "info", title, text, _ok_buttons(), default=True, size="md", rich=True,
                  on_link=lambda: _open_in_explorer_foreground(path), min_body_height=110).ask()


# ── 基础对话框 ──────────────────────────────────────────────────────────

class Dialog(QDialog):
    """通用小对话框基类：统一宽度档位、边距、字号和底部按钮行（取消靠左、主操作靠右）。

    width 传档位名（"sm"/"md"/"lg"）或像素值（归到最近档位），作为最小宽度，内容更宽时可撑开。"""

    def __init__(self, parent, title: str, width: int | str = "sm", confirm_text: str | None = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        tier = width if isinstance(width, str) else width_tier(width)
        self.setMinimumWidth(DIALOG_WIDTHS.get(tier, DIALOG_WIDTHS["sm"]))
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(*DIALOG_MARGINS)
        self.body.setSpacing(DIALOG_SPACING)
        self._confirm_text = confirm_text or t("dlg.confirm_btn")

    def add_footer(self, left=(), right=()) -> QHBoxLayout:
        """底部按钮行：left 里的按钮靠左（取消、删除这类），right 里的靠右（主操作）。"""
        row = QHBoxLayout()
        row.setSpacing(8)
        for button in left:
            row.addWidget(button)
        row.addStretch()
        for button in right:
            row.addWidget(button)
        self.body.addSpacing(6)
        self.body.addLayout(row)
        return row

    def add_buttons(self) -> QPushButton:
        cancel = style_button(QPushButton(t("dlg.cancel_btn")), "secondary")
        confirm = QPushButton(self._confirm_text)
        cancel.clicked.connect(self.reject)
        confirm.clicked.connect(self.accept_if_valid)
        cancel.setAutoDefault(False)
        confirm.setDefault(True)
        self.add_footer([cancel], [confirm])
        return confirm


    def heading_label(self, text: str) -> QLabel:
        """对话框内的小标题：统一 FONT_SIZE_MD 加粗。"""
        label = QLabel(text)
        label.setWordWrap(True)
        label.setFont(theme.font("FONT_SIZE_MD", bold=True))
        label.setProperty("heading", True)
        return label

    def accept_if_valid(self) -> None:
        self.accept()

    def text_label(self, text: str, muted: bool = False, size_key: str = "FONT_SIZE_SM",
                   wrap: bool = True) -> QLabel:
        label = QLabel(text)
        label.setWordWrap(wrap)
        label.setFont(theme.font(size_key))
        label.setProperty("muted", muted)
        return label

    def error_label(self) -> QLabel:
        label = QLabel()
        label.setWordWrap(True)
        label.setFont(theme.font("FONT_SIZE_SM"))
        label.setStyleSheet(f"color: {theme.hex('ERROR')};")
        return label


class RemotePortDialog(Dialog):
    """内网穿透开启映射前为每个世界填远程端口，留空由服务端分配；只校验格式和互不重复，能否使用交给穿透服务判断。"""

    def __init__(self, parent, shard_names: list[str], confirm_text: str):
        super().__init__(parent, t("dlg.remote_port_title"), "md", confirm_text=confirm_text)
        self.result_ports: dict[str, int | None] = {}
        self.body.addWidget(self.text_label(t("dlg.remote_port_intro")))
        grid = QGridLayout()
        grid.setColumnStretch(1, 1)
        self._edits: dict[str, QLineEdit] = {}
        for row, name in enumerate(shard_names):
            edit = QLineEdit()
            edit.setPlaceholderText(t("dlg.remote_port_placeholder"))
            edit.setMaxLength(5)
            edit.setFont(theme.font("FONT_SIZE_BASE"))
            label = QLabel(name)
            label.setFont(theme.font("FONT_SIZE_BASE"))
            grid.addWidget(label, row, 0)
            grid.addWidget(edit, row, 1)
            self._edits[name] = edit
        self.body.addLayout(grid)
        self.body.addWidget(self.text_label(t("dlg.remote_port_note"), muted=True))
        self._error = self.error_label()
        self._error.hide()
        self.body.addWidget(self._error)
        self.add_buttons()

    def _show_error(self, text: str) -> None:
        self._error.setText(text)
        self._error.show()

    def accept_if_valid(self) -> None:
        ports: dict[str, int | None] = {}
        seen: set[int] = set()
        for name, edit in self._edits.items():
            text = edit.text().strip()
            if not text:
                ports[name] = None
                continue
            if not (text.isascii() and text.isdigit()) or not 1 <= int(text) <= 65535:
                self._show_error(t("dlg.remote_port_invalid", shard=name))
                return
            port = int(text)
            if port in seen:
                self._show_error(t("dlg.remote_port_duplicate", port=port))
                return
            seen.add(port)
            ports[name] = port
        self.result_ports = ports
        self.accept()


class CopyToServerDialog(Dialog):
    """复制为服务器存档：输入目标文件夹名（预填建议值），校验交给调用方传入的 validator。"""

    def __init__(self, parent, source_name: str, suggested_name: str, validator):
        super().__init__(parent, t("save.copy_dialog_title"), 480)
        self._validator = validator
        self.result_name: str | None = None
        self.body.addWidget(self.text_label(t("save.copy_dialog_prompt", name=source_name)))
        self.body.addWidget(self.text_label(t("save.copy_name_label")))
        self._edit = QLineEdit(suggested_name)
        self._edit.setFont(theme.font("FONT_SIZE_BASE"))
        self.body.addWidget(self._edit)
        self.body.addWidget(self.text_label(t("save.copy_name_hint"), muted=True, size_key="FONT_SIZE_XS"))
        self._error = self.error_label()
        self.body.addWidget(self._error)
        self.add_buttons()
        self._edit.setFocus()

    def accept_if_valid(self) -> None:
        name = self._edit.text().strip()
        error = self._validator(name)
        if error:
            self._error.setText(error)
            return
        self.result_name = name
        self.accept()


def format_backup_label(path: Path, cluster_name: str) -> str:
    """备份文件名（{cluster}_{YYYYMMDD_HHMMSS}[_n].zip）转成可读时间，解析失败原样显示文件名。"""
    stem = path.stem
    prefix = f"{cluster_name}_"
    rest = stem[len(prefix):] if stem.startswith(prefix) else stem
    try:
        return datetime.strptime(rest[:15], "%Y%m%d_%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return stem


class RestoreBackupDialog(Dialog):
    """从备份恢复：列出历史备份（新的在前），选中后才现查那一份的详情（需要解压一次，放后台）。"""

    def __init__(self, parent, cluster, backups: list[Path]):
        super().__init__(parent, t("save.restore_backup"), 560, confirm_text=t("save.restore_confirm_btn"))
        self.result_backup: Path | None = None
        self._backups = backups
        self._detail_generation = 0
        self.body.addWidget(self.text_label(t("save.restore_prompt")))
        self._list = QListWidget()
        self._list.setMinimumHeight(260)
        self._list.setFont(theme.font("FONT_SIZE_BASE"))
        for backup in backups:
            label = format_backup_label(backup, cluster.path.name)
            self._list.addItem(label + t("save.backup_kept_tag") if is_kept_backup(backup) else label)
        self.body.addWidget(self._list, 1)
        self._detail = self.text_label("", muted=True, size_key="FONT_SIZE_SM")
        self.body.addWidget(self._detail)
        self.add_buttons()
        self._list.currentRowChanged.connect(self._on_select)
        self._list.setCurrentRow(0)

    def _on_select(self, row: int) -> None:
        self._detail_generation += 1
        generation = self._detail_generation
        if row < 0:
            self._detail.setText("")
            return
        self._detail.setText(t("save.loading"))

        def done(info: dict) -> None:
            if generation != self._detail_generation:
                return
            parts = []
            if info.get("cluster_name"):
                parts.append(str(info["cluster_name"]))
            if info.get("game_mode"):
                max_players = info.get("max_players")
                parts.append(f"{info['game_mode']}/{max_players}{t('save.restore_players_suffix')}"
                             if max_players else str(info["game_mode"]))
            if info.get("summary"):
                parts.append(info["summary"])
            self._detail.setText(" · ".join(parts) if parts else t("save.restore_no_detail"))

        backup = self._backups[row]
        run_async(lambda: get_backup_summary(backup), done, lambda _exc: self._detail.setText(""))

    def accept_if_valid(self) -> None:
        row = self._list.currentRow()
        if row >= 0:
            self.result_backup = self._backups[row]
            self.accept()


class BackupPolicyDialog(Dialog):
    """备份策略：自动备份开关（点击即生效）+ 保留份数（5~99）+ 自动备份间隔分钟（2~30）。"""

    def __init__(self, parent):
        super().__init__(parent, t("save.backup_policy_title"), 420)
        auto_row = QHBoxLayout()
        auto_row.addWidget(self.text_label(t("save.backup_auto_enabled_label"), wrap=False))
        auto_row.addStretch()
        self._auto = ToggleSwitch(get_backup_auto_enabled())
        self._auto.toggled.connect(self._on_auto_toggled)
        auto_row.addWidget(self._auto)
        self.body.addLayout(auto_row)
        self.body.addWidget(self.text_label(t("save.backup_auto_enabled_hint"), muted=True, size_key="FONT_SIZE_SM"))

        self._retention = self._number_row(
            t("save.backup_retention_label"), get_backup_retention(), t("save.backup_retention_hint"))
        self._interval = self._number_row(
            t("save.backup_interval_label"), get_backup_interval_minutes(), t("save.backup_interval_hint"))
        self._interval.setEnabled(self._auto.isChecked())
        self._error = self.error_label()
        self.body.addWidget(self._error)
        self.add_buttons()

    def _number_row(self, label: str, value: int, hint: str) -> QLineEdit:
        row = QHBoxLayout()
        row.addWidget(self.text_label(label, wrap=False))
        row.addStretch()
        edit = QLineEdit(str(value))
        edit.setFixedWidth(90)
        edit.setValidator(QIntValidator(0, 999999))
        row.addWidget(edit)
        self.body.addSpacing(6)
        self.body.addLayout(row)
        self.body.addWidget(self.text_label(hint, muted=True, size_key="FONT_SIZE_SM"))
        return edit

    def _on_auto_toggled(self, enabled: bool) -> None:
        set_backup_auto_enabled(enabled)
        self._interval.setEnabled(enabled)

    def accept_if_valid(self) -> None:
        try:
            retention, interval = int(self._retention.text()), int(self._interval.text())
        except ValueError:
            self._error.setText(t("save.backup_policy_invalid"))
            return
        if not 5 <= retention <= 99:
            self._error.setText(t("save.backup_retention_range_error"))
            return
        if not 2 <= interval <= 30:
            self._error.setText(t("save.backup_interval_range_error"))
            return
        set_backup_retention(retention)
        set_backup_interval_minutes(interval)
        self.accept()


_LOG_TAG_STYLE = {
    # (加粗, 颜色取的调色板键；None 表示跟随默认文字色)
    "section": (True, "HEADING"),
    "detail": (False, None),
    "note": (False, "TEXT_MUTED"),
    "result_success": (True, "ACCENT"),
    "result_warning": (True, "ERROR"),
    "result_error": (True, "ERROR"),
}


class LogDialog(Dialog):
    """实时追加日志的窗口：任务完成前不能关闭，finish() 后才出现"确认"。

    ``closable=True``（如 Steam 更新只是旁观日志）允许提前关闭；``on_cancel`` 额外提供一次性的"取消"按钮。"""

    def __init__(self, parent, title: str, closable: bool = False, on_cancel=None, cancel_text: str | None = None):
        super().__init__(parent, title, 640)
        self._finished = False
        self._closable = closable
        self._on_cancel = on_cancel
        self._view = QTextEdit()
        self._view.setReadOnly(True)
        self._view.setMinimumHeight(280)
        self._view.setFont(theme.font("FONT_SIZE_SM"))
        self.body.addWidget(self._view, 1)
        row = QHBoxLayout()
        row.addStretch()
        if on_cancel is not None:
            self._cancel_btn = style_button(QPushButton(cancel_text or t("dlg.cancel_btn")), "secondary")
            self._cancel_btn.clicked.connect(self._on_cancel_clicked)
            row.addWidget(self._cancel_btn)
        self._close = QPushButton(t("dlg.confirm_btn"))
        self._close.setEnabled(False)
        self._close.clicked.connect(self.accept)
        row.addWidget(self._close)
        self.body.addLayout(row)

    def _on_cancel_clicked(self) -> None:
        self._cancel_btn.setEnabled(False)  # 防止手滑连点多次重复触发 on_cancel
        self._on_cancel()

    def append(self, text: str, tag: str | None = None) -> None:
        bold, color_key = _LOG_TAG_STYLE.get(tag, (False, None))
        color = theme.hex(color_key) if color_key else theme.hex("TEXT")
        escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
        weight = "bold" if bold else "normal"
        self._view.append(f'<div style="color:{color}; font-weight:{weight};">{escaped}</div>')

    def clear(self) -> None:
        self._view.clear()

    def scroll_to_start(self) -> None:
        self._view.moveCursor(QTextCursor.MoveOperation.Start)
        self._view.ensureCursorVisible()

    def finish(self) -> None:
        self._finished = True
        if self._on_cancel is not None:
            self._cancel_btn.setEnabled(False)
        self._close.setEnabled(True)
        self._close.setDefault(True)
        self._close.setFocus()

    def reject(self) -> None:
        if self._finished or self._closable:
            super().reject()

    def closeEvent(self, event):
        if self._finished or self._closable:
            event.accept()
        else:
            event.ignore()



class TextInputDialog(Dialog):
    """单行文本输入（令牌/管理员 ID）：等宽字体，可选校验函数（返回错误文案则不关闭窗口）。"""

    def __init__(self, parent, title: str, prompt: str, initial: str = "", validator=None,
                 width: int = 500):
        super().__init__(parent, title, width)
        self._validator = validator
        self.result_text: str | None = None
        self.body.addWidget(self.text_label(prompt, size_key="FONT_SIZE_MD"))
        self._edit = QLineEdit(initial)
        self._edit.setFont(QFont("Consolas", 12))
        self._edit.returnPressed.connect(self.accept_if_valid)
        self.body.addWidget(self._edit)
        self._error = self.error_label()
        self.body.addWidget(self._error)
        self.add_buttons()
        self._edit.setFocus()

    def accept_if_valid(self) -> None:
        value = self._edit.text().strip()
        if not value:
            return
        error = self._validator(value) if self._validator else None
        if error:
            self._error.setText(error)
            return
        self.result_text = value
        self.accept()


class SaveUserPickDialog(Dialog):
    """从存档/日志/跨存档玩家登记簿里挑一个真实用户；开关可以只看当前存档的用户。"""

    def __init__(self, parent, candidates: list[tuple[str, str, bool]]):
        super().__init__(parent, t("admin.pick_save_title"), 640)
        self._candidates = candidates
        self._shown_ids: list[str] = []
        self.result_id: str | None = None
        self.body.addWidget(self.text_label(t("admin.pick_save_prompt"), size_key="FONT_SIZE_MD"))
        # "只看当前存档的用户"开关只在候选里确实有当前存档用户时才显示——创建向导
        # 的草稿存档没有日志/玩家，永远筛不出东西，直接不显示这一行。
        self._only_current = None
        if any(cur for _pid, _hint, cur in candidates):
            row = QHBoxLayout()
            self._only_current = ToggleSwitch(False)
            self._only_current.toggled.connect(lambda _checked: self._refill())
            row.addWidget(self._only_current)
            row.addWidget(self.text_label(t("admin.pick_save_only_current"), size_key="FONT_SIZE_SM", wrap=False))
            row.addStretch()
            self.body.addLayout(row)
        self._list = QListWidget()
        self._list.setMinimumHeight(340)
        self._list.setFont(theme.font("FONT_SIZE_MD"))
        self._list.itemDoubleClicked.connect(lambda _item: self.accept_if_valid())
        self.body.addWidget(self._list, 1)
        self.add_buttons()
        self._refill()

    def _refill(self) -> None:
        only_current = self._only_current.isChecked() if self._only_current is not None else False
        self._list.clear()
        self._shown_ids = []
        for pid, hint, in_current in self._candidates:
            if only_current and not in_current:
                continue
            self._shown_ids.append(pid)
            self._list.addItem(f"{pid}   ({hint})" if hint else pid)

    def accept_if_valid(self) -> None:
        row = self._list.currentRow()
        if row >= 0:
            self.result_id = self._shown_ids[row]
        self.accept()


class GlobalTokensDialog(Dialog):
    """管理全局令牌池，可把选中的令牌返回给当前存档。令牌始终脱敏显示；悬停令牌文字临时显示完整值，
    点击同一处复制。增删即时写入设置；"使用"只返回选中项，由调用方写进存档的令牌文件。"""

    def __init__(self, parent, token_uses=()):
        from dstools.shared import app_settings
        from dstools.shared.token_manager import mask_token, token_fingerprint
        super().__init__(parent, t("token.set_global_btn"), 760)
        self._app_settings, self._mask, self._fingerprint = app_settings, mask_token, token_fingerprint
        self.result_token: str | None = None
        self._tokens = app_settings.get_global_tokens()
        self._token_uses = tuple(token_uses)
        self._prune_holds()
        self._holds = app_settings.get_token_holds()
        self.body.setSpacing(12)

        # ── 卡片一：令牌池（说明 → 表格 → 增删操作）──
        pool_card, pool = section_card(t("token.pool_section"))
        pool.setSpacing(8)
        pool.addWidget(self.text_label(t("token.global_hint"), muted=True, size_key="FONT_SIZE_SM"))
        pool.addWidget(self.text_label(t("token.kind_hint"), muted=True, size_key="FONT_SIZE_SM"))
        self._table = QTableWidget(0, 3)
        self._style_table()
        self._table.setHorizontalHeaderLabels(
            [t("token.column_token"), t("token.column_kind"), t("token.column_status")])
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setMinimumHeight(220)
        self._table.setMouseTracking(True)
        self._table.itemSelectionChanged.connect(self._update_buttons)
        self._table.cellEntered.connect(self._on_cell_entered)
        self._table.cellClicked.connect(self._on_cell_clicked)
        self._hover_timer = QTimer(self, singleShot=True, interval=350)
        self._hover_timer.timeout.connect(self._show_full_token)
        self._hover_row: int | None = None
        pool.addWidget(self._table, 1)

        # 没有备用令牌（新令牌不足两个且没有旧令牌）时提示再申请一个，崩溃或冲突后才能立即换用
        self._spare_tip = QWidget()
        tip_row = QHBoxLayout(self._spare_tip)
        tip_row.setContentsMargins(0, 0, 0, 0)
        tip_row.setSpacing(8)
        tip_row.addWidget(self.text_label(t("token.spare_hint"), size_key="FONT_SIZE_SM"), 1)
        apply_btn = style_button(QPushButton(t("token.apply")), "secondary")
        apply_btn.setAutoDefault(False)
        apply_btn.clicked.connect(self._on_apply)
        tip_row.addWidget(apply_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        pool.addWidget(self._spare_tip)

        row = QHBoxLayout()
        row.setSpacing(8)
        self._add = QPushButton(t("admin.add"))
        self._remove = style_button(QPushButton(t("admin.remove")), "danger")
        self._release = style_button(QPushButton(t("token.clear_hold")), "secondary")
        self._add.clicked.connect(self._on_add)
        self._remove.clicked.connect(self._on_remove)
        self._release.clicked.connect(self._on_clear_hold)
        for button in (self._add, self._remove, self._release):
            row.addWidget(button)
        row.addStretch()
        pool.addSpacing(2)
        pool.addLayout(row)
        self.body.addWidget(pool_card, 1)

        # ── 卡片二：崩溃自动重启时的换令牌策略（全局设置，改动即时保存）──
        restart_card, restart = section_card(t("token.auto_restart_section"))
        restart.setSpacing(8)
        switch_row = QHBoxLayout()
        switch_row.addWidget(self.text_label(t("token.switch_on_timeout_label"), size_key="FONT_SIZE_BASE", wrap=False))
        switch_row.addStretch()
        self._switch_on_timeout = ToggleSwitch(app_settings.get_token_switch_on_timeout())
        self._switch_on_timeout.toggled.connect(self._on_switch_toggled)
        switch_row.addWidget(self._switch_on_timeout)
        restart.addLayout(switch_row)
        minutes_row = QHBoxLayout()
        minutes_row.addWidget(self.text_label(t("token.switch_after_minutes_label"), size_key="FONT_SIZE_BASE", wrap=False))
        minutes_row.addStretch()
        self._switch_minutes = QLineEdit(str(app_settings.get_token_switch_after_minutes()))
        self._switch_minutes.setFixedWidth(90)
        self._switch_minutes.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._switch_minutes.setValidator(QIntValidator(*app_settings.TOKEN_SWITCH_MINUTES_RANGE))
        self._switch_minutes.editingFinished.connect(self._on_switch_minutes_edited)
        self._switch_minutes.setEnabled(self._switch_on_timeout.isChecked())
        minutes_row.addWidget(self._switch_minutes)
        restart.addLayout(minutes_row)
        low, high = app_settings.TOKEN_SWITCH_MINUTES_RANGE
        restart.addWidget(self.text_label(t("token.switch_on_timeout_hint", low=low, high=high),
                                          muted=True, size_key="FONT_SIZE_SM"))
        self.body.addWidget(restart_card)

        # ── 底部：关闭靠左，主操作"用于当前存档"靠右 ──
        close = style_button(QPushButton(t("dlg.close_btn")), "secondary")
        close.setAutoDefault(False)
        close.clicked.connect(self.reject)
        self._use = QPushButton(t("token.global_use"))
        self._use.clicked.connect(self._on_use)
        self.add_footer([close], [self._use])
        self._refresh()

    def _style_table(self) -> None:
        """表格默认是系统灰表头加网格线，跟主题卡片不搭：去网格、浅色表头、行高放大、主题色选中。"""
        table = self._table
        table.setShowGrid(False)
        table.setFont(theme.font("FONT_SIZE_BASE"))
        table.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # 去掉选中单元格的虚线焦点框
        table.verticalHeader().setDefaultSectionSize(34)
        header = table.horizontalHeader()
        header.setFont(theme.font("FONT_SIZE_SM", bold=True))
        header.setHighlightSections(False)
        header.setMinimumHeight(30)
        table.setStyleSheet(f"""
            QTableWidget {{
                background: rgba(255, 255, 255, 200); border: 1px solid {theme.hex('CARD_BORDER')};
                border-radius: 8px;
            }}
            QTableWidget::item {{ padding: 0 8px; border-bottom: 1px solid {theme.hex('CARD_BG_ALT')}; }}
            QTableWidget::item:selected {{ background: {theme.hex('PRIMARY_LIGHT')}; color: {theme.hex('TEXT')}; }}
            QHeaderView {{ background: transparent; border: none; }}
            QHeaderView::section {{
                background: {theme.hex('CARD_BG_ALT')}; color: {theme.hex('HEADING')};
                border: none; border-bottom: 1px solid {theme.hex('CARD_BORDER')}; padding: 4px 8px;
            }}
            /* 表头方形底色会盖住外框的上两个圆角：首尾两节补上圆角（外框 8px 减去 1px 描边） */
            QHeaderView::section:first {{ border-top-left-radius: 7px; }}
            QHeaderView::section:last {{ border-top-right-radius: 7px; }}
            QHeaderView::section:only-one {{ border-top-left-radius: 7px; border-top-right-radius: 7px; }}
        """)

    def _has_spare(self) -> bool:
        """池里是否有备用令牌：旧令牌可多存档复用，有一个就够；新令牌要有两个以上。"""
        from dstools.shared.token_manager import ServerTokenKind, classify_token
        kinds = [classify_token(token) for token in self._tokens]
        return ServerTokenKind.OLD in kinds or kinds.count(ServerTokenKind.NEW) >= 2

    @staticmethod
    def _on_apply() -> None:
        import webbrowser
        from dstools.shared.token_manager import KLEI_SERVERS_URL
        webbrowser.open(KLEI_SERVERS_URL)

    def _row_texts(self, token: str) -> tuple[str, str, str, str]:
        """返回 (脱敏令牌, 类型, 状态文字, 状态颜色键)。"""
        from dstools.shared.token_manager import ServerTokenKind, classify_token, is_valid_token
        kind_key = {ServerTokenKind.OLD: "token.kind_old", ServerTokenKind.NEW: "token.kind_new",
                    ServerTokenKind.UNKNOWN: "token.kind_unknown"}[classify_token(token)]
        fingerprint = self._fingerprint(token)
        users = sorted({use.cluster_name for use in self._token_uses
                        if self._fingerprint(use.token) == fingerprint})
        hold = self._holds.get(fingerprint)
        if not is_valid_token(token):
            status, color = t("token.status_invalid"), "ERROR"
        elif hold and self._blocking(hold):
            conflict = hold["state"] == "conflict"
            status = t("token.status_conflict" if conflict else "token.status_waiting",
                       name=hold.get("cluster_name") or "-",
                       time=time.strftime("%H:%M", time.localtime(hold["retry_at"])))
            color = "ERROR" if conflict else "BANNER_TEXT"
        elif users:
            status, color = t("token.status_in_use", names="、".join(users)), "ACCENT"
        elif hold:
            # 等待期已过：启动时会照常选用，只提示上次异常，不再显示成占用
            status, color = t("token.status_retryable", name=hold.get("cluster_name") or "-"), "TEXT_MUTED"
        else:
            status, color = t("token.status_available"), "SUCCESS"
        return self._mask(token), t(kind_key), status, color

    def _on_switch_toggled(self, enabled: bool) -> None:
        self._app_settings.set_token_switch_on_timeout(enabled)
        self._switch_minutes.setEnabled(enabled)

    def _on_switch_minutes_edited(self) -> None:
        # 校验器允许中途输入越界的数字，保存时夹到合法范围并回显实际值
        text = self._switch_minutes.text().strip()
        value = self._app_settings.set_token_switch_after_minutes(
            int(text) if text.isdigit() else self._app_settings.TOKEN_SWITCH_MINUTES_DEFAULT)
        self._switch_minutes.setText(str(value))

    @staticmethod
    def _blocking(hold: dict) -> bool:
        return hold["retry_at"] > time.time()

    def _prune_holds(self) -> None:
        from dstools.features.local_service.auto_restart import TOKEN_WAIT_LIMIT
        self._app_settings.prune_token_holds(self._tokens, expire_before=time.time() - TOKEN_WAIT_LIMIT)

    def _selected(self) -> int | None:
        rows = self._table.selectionModel().selectedRows()
        return rows[0].row() if rows and rows[0].row() < len(self._tokens) else None

    def _refresh(self, select: int | None = None) -> None:
        if select is None:
            select = self._selected()
        self._holds = self._app_settings.get_token_holds()
        self._table.blockSignals(True)
        self._table.clearSpans()
        self._table.setRowCount(0)
        if not self._tokens:
            # 空池提示横跨三列居中、灰色，且不可选中，免得像一条真实令牌
            self._table.setRowCount(1)
            self._table.setSpan(0, 0, 1, 3)
            empty = QTableWidgetItem(t("token.global_empty"))
            empty.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setForeground(theme.color("TEXT_MUTED"))
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self._table.setItem(0, 0, empty)
        for index, token in enumerate(self._tokens):
            self._table.insertRow(index)
            masked, kind, status, color = self._row_texts(token)
            for col, text in enumerate((masked, kind, status)):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if col == 1:
                    item.setForeground(theme.color("TEXT_MUTED"))
                elif col == 2:
                    item.setForeground(theme.color(color))
                self._table.setItem(index, col, item)
        self._table.blockSignals(False)
        self._spare_tip.setVisible(not self._has_spare())
        if self._tokens and select is not None:
            self._table.selectRow(min(select, len(self._tokens) - 1))
        self._update_buttons()

    def _update_buttons(self) -> None:
        index = self._selected()
        self._use.setEnabled(index is not None)
        can_clear = False
        if index is not None:
            fingerprint = self._fingerprint(self._tokens[index])
            active = any(self._fingerprint(use.token) == fingerprint for use in self._token_uses)
            hold = self._holds.get(fingerprint)
            can_clear = hold is not None and self._blocking(hold) and not active
        self._release.setEnabled(can_clear)

    def _over_token_text(self, row: int, col: int, pos: QPoint) -> bool:
        """命中区域只覆盖实际显示的脱敏文字，点击单元格右侧空白不触发复制。"""
        if col != 0 or not 0 <= row < len(self._tokens):
            return False
        rect = self._table.visualItemRect(self._table.item(row, 0))
        text_w = QFontMetrics(self._table.font()).horizontalAdvance(self._mask(self._tokens[row]))
        return abs(pos.x() - rect.center().x()) <= text_w / 2 + 4

    def _cursor_in_viewport(self) -> QPoint:
        return self._table.viewport().mapFromGlobal(self._table.cursor().pos())

    def _on_cell_entered(self, row: int, col: int) -> None:
        over = self._over_token_text(row, col, self._cursor_in_viewport())
        self._table.viewport().setCursor(Qt.CursorShape.PointingHandCursor if over else Qt.CursorShape.ArrowCursor)
        self._hover_row = row if over else None
        if over:
            self._hover_timer.start()
        else:
            self._hover_timer.stop()
            QToolTip.hideText()

    def _show_full_token(self) -> None:
        if self._hover_row is not None and self._hover_row < len(self._tokens):
            QToolTip.showText(self._table.cursor().pos(), self._tokens[self._hover_row], self._table)

    def _on_cell_clicked(self, row: int, col: int) -> None:
        if self._over_token_text(row, col, self._cursor_in_viewport()):
            QToolTip.hideText()
            QGuiApplication.clipboard().setText(self._tokens[row])
            show_toast(self, t("token.copied"))

    def _on_add(self) -> None:
        from dstools.shared.token_manager import is_valid_token
        dialog = TextInputDialog(
            self, t("token.global_add_title"), t("token.prompt"),
            validator=lambda value: None if is_valid_token(value) else t("token.invalid_hint"))
        if not dialog.exec() or dialog.result_text is None:
            return
        if dialog.result_text in self._tokens:
            show_warning(self, t("token.set_global_btn"), t("token.global_duplicate"))
            return
        self._tokens.append(dialog.result_text)
        self._app_settings.set_global_tokens(self._tokens)
        self._refresh(select=len(self._tokens) - 1)

    def _on_remove(self) -> None:
        index = self._selected()
        if index is None:
            return
        fingerprint = self._fingerprint(self._tokens[index])
        if any(self._fingerprint(use.token) == fingerprint for use in self._token_uses):
            show_warning(self, t("token.set_global_btn"), t("token.remove_in_use"))
            return
        hold = self._holds.get(fingerprint)
        if hold and self._blocking(hold) and not ask_yes_no(
                self, t("token.set_global_btn"), t("token.remove_held_confirm")):
            return
        del self._tokens[index]
        self._app_settings.set_global_tokens(self._tokens)
        self._prune_holds()
        self._refresh(select=index if self._tokens else None)

    def _on_clear_hold(self) -> None:
        index = self._selected()
        if index is None or not ask_yes_no(self, t("token.clear_hold"), t("token.clear_hold_confirm")):
            return
        self._app_settings.clear_token_hold(self._fingerprint(self._tokens[index]))
        self._refresh(select=index)

    def _on_use(self) -> None:
        index = self._selected()
        if index is not None:
            self.result_token = self._tokens[index]
            self.accept()
