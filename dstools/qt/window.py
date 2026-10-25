"""主窗口外壳：无边框窗口 + 自绘标题栏/菜单条 + 存档选择栏 + 胶囊页签 + 状态栏 + 托盘。

缩放和移动交给系统原生处理（startSystemResize/startSystemMove），只在 WM_SIZING/WM_MOVING
里改写矩形来锁定 16:9 并限制不越出桌面。
"""

import ctypes
import math
import os
from ctypes import wintypes
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction, QActionGroup, QGuiApplication, QIcon, QImage, QKeySequence, QPainter, QPen, QPixmap,
)
from PySide6.QtWidgets import (
    QApplication, QComboBox, QHBoxLayout, QLabel, QMenu, QPushButton, QStackedWidget,
    QProgressBar, QSizePolicy, QStyle, QStyleOptionComboBox,
    QSystemTrayIcon, QToolTip, QVBoxLayout, QWidget, QWidgetAction,
)

from dstools import __version__
from dstools.i18n import get_lang, set_lang, t
from dstools.models import Platform
from dstools.qt import dialogs
from dstools.qt.background import Background
from dstools.qt.text_halo import TextHaloManager
from dstools.qt.context import AppContext
from dstools.qt.ornaments import WindowOrnaments, paint_frame_accent
from dstools.qt.pages.local_service import LocalServicePage
from dstools.qt.pages.mod import ModPage
from dstools.qt.pages.sakura import SakuraPage
from dstools.qt.pages.save_info import SaveInfoPage
from dstools.qt.pages.server_config import ServerConfigPage
from dstools.qt.pages.world_settings import WorldSettingsPage
from dstools.qt.theme import THEME_NAMES, theme
from dstools.qt.threads import post_to_ui, run_async
from dstools.qt.self_update import SelfUpdater, is_update_available
from dstools.qt.widgets import CheckMenuItem, FrostedMenu, Grip, PillTabBar, ThemeMenuItem, TitleButton
from dstools.shared.app_settings import (
    get_creation_wizard_size, get_minimize_on_close, get_remind_update_enabled,
    get_window_ornaments_enabled, get_window_position, get_window_size, set_creation_wizard_size,
    set_minimize_on_close, set_window_ornaments_enabled, set_window_position,
    set_window_size,
)
from dstools.shared.resource_paths import bundled_resource_dir
from dstools.shared.single_instance import activate_message_id, version_code

TAB_KEYS = ["local", "world", "mods", "server", "saves", "sakura"]
BASE_W, BASE_H = 1600, 900  # 默认尺寸（逻辑像素，Qt 自动按显示器缩放，不需要 DPI 补丁）
MIN_W, MIN_H = 960, 540
ASPECT = BASE_W / BASE_H
START_FILL = 0.8  # 首次启动默认尺寸最多占工作区的比例（宽高各自计）；之后沿用上次关闭时的尺寸
MIN_VISIBLE = 100  # 窗口挪到桌面边缘时至少留这么多像素在屏幕里
# 窗口低于这个高度时收窄各页签共用的固定区域；页面上下外边距常规/收窄值（各页签外层统一用 12）
COMPACT_CHROME_H = 800
PAGE_MARGIN_V, COMPACT_PAGE_MARGIN_V = 12, 6

WM_SIZING, WM_MOVING = 0x0214, 0x0216
# 客户区/非客户区的左、右、中键按下：主窗口被模态窗口阻挡时用来提醒用户
_MOUSE_DOWN_MESSAGES = {0x0201, 0x0204, 0x0207, 0x00A1, 0x00A4, 0x00A7}
# 模态期间 Qt 会禁用主窗口，真实点击不再产生上面的消息，系统只发 WM_SETCURSOR：
# lParam 低位是命中码 HTERROR（-2），高位是触发它的鼠标消息
WM_SETCURSOR, HTERROR = 0x0020, 0xFFFE
# 重复启动时第二个进程发来的"恢复已有窗口"消息（见 shared/single_instance.py）
WM_DSTCAMP_ACTIVATE = activate_message_id()
(WMSZ_LEFT, WMSZ_RIGHT, WMSZ_TOP, WMSZ_TOPLEFT, WMSZ_TOPRIGHT,
 WMSZ_BOTTOM, WMSZ_BOTTOMLEFT, WMSZ_BOTTOMRIGHT) = range(1, 9)


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def virtual_desktop() -> tuple[int, int, int, int]:
    """整个虚拟桌面（横跨全部显示器）的物理像素范围 (left, top, right, bottom)。"""
    user32 = ctypes.windll.user32
    left, top = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
    return left, top, left + user32.GetSystemMetrics(78), top + user32.GetSystemMetrics(79)


def enforce_aspect(edge: int, rect: RECT) -> None:
    """按被拖动的边把矩形改成 16:9；左右边/角由宽度推高度，上下边由高度推宽度。"""
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if edge in (WMSZ_TOP, WMSZ_BOTTOM):
        rect.right = rect.left + round(height * ASPECT)
        return
    new_height = round(width / ASPECT)
    if edge in (WMSZ_TOPLEFT, WMSZ_TOPRIGHT):
        rect.top = rect.bottom - new_height
    else:
        rect.bottom = rect.top + new_height


def fit_desktop(edge: int, rect: RECT) -> None:
    """被拖动的边越出桌面时，按比例整体缩小（没被拖动的一侧保持不动）。

    只看被拖动的边：窗口原本就有一部分在屏幕外时，不会因为拖别的边被强行缩回来。"""
    left, top, right, bottom = virtual_desktop()
    moves_l = edge in (WMSZ_LEFT, WMSZ_TOPLEFT, WMSZ_BOTTOMLEFT)
    moves_r = edge in (WMSZ_RIGHT, WMSZ_TOPRIGHT, WMSZ_BOTTOMRIGHT)
    moves_t = edge in (WMSZ_TOP, WMSZ_TOPLEFT, WMSZ_TOPRIGHT)
    moves_b = edge in (WMSZ_BOTTOM, WMSZ_BOTTOMLEFT, WMSZ_BOTTOMRIGHT)
    if not ((moves_l and rect.left < left) or (moves_t and rect.top < top)
            or (moves_r and rect.right > right) or (moves_b and rect.bottom > bottom)):
        return
    width, height = rect.right - rect.left, rect.bottom - rect.top
    max_w = (rect.right - left) if moves_l else (right - rect.left)
    max_h = (rect.bottom - top) if moves_t else (bottom - rect.top)
    if width <= 0 or height <= 0 or max_w <= 0 or max_h <= 0:
        return
    scale = min(1.0, max_w / width, max_h / height)
    new_w = int(width * scale)
    new_h = round(new_w / ASPECT)
    if moves_l:
        rect.left = rect.right - new_w
    else:
        rect.right = rect.left + new_w
    if moves_t:
        rect.top = rect.bottom - new_h
    else:
        rect.bottom = rect.top + new_h


def keep_on_desktop(rect: RECT) -> None:
    """窗口移动时：顶边不能高于桌面上沿，左右/底部至少留 MIN_VISIBLE 在屏幕里。"""
    left, top, right, bottom = virtual_desktop()
    width, height = rect.right - rect.left, rect.bottom - rect.top
    x = max(left - width + MIN_VISIBLE, min(rect.left, right - MIN_VISIBLE))
    y = max(top, min(rect.top, bottom - MIN_VISIBLE))
    rect.left, rect.top, rect.right, rect.bottom = x, y, x + width, y + height


class TitleBar(QWidget):
    def __init__(self, window: "MainWindow"):
        super().__init__()
        self._window = window
        self.setFixedHeight(32)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 4, 0)
        layout.setSpacing(2)
        icon = QLabel()
        icon.setPixmap(QPixmap(str(bundled_resource_dir() / "icons" / "app" / "icon.png")).scaled(
            18, 18, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        self.title = QLabel()
        layout.addWidget(icon)
        layout.addSpacing(6)
        layout.addWidget(self.title)
        layout.addStretch()
        self.max_button = self._button("max", window.toggle_maximize)
        for button in (self._button("min", window.showMinimized), self.max_button,
                       self._button("close", window.request_close)):
            layout.addWidget(button)
        self.retranslate()

    def _button(self, kind: str, slot) -> TitleButton:
        # 主窗口是保持 16:9 的"伪最大化"，状态看 _restore_geometry，不是 isMaximized()
        button = TitleButton(kind, lambda: self._window._restore_geometry is not None)
        button.clicked.connect(slot)
        button.clicked.connect(button.update)  # 最大化/还原后图标跟着切换
        return button

    def retranslate(self) -> None:
        self.title.setText(f"{t('app.title')} v{__version__}")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._window.windowHandle().startSystemMove()

    def mouseDoubleClickEvent(self, _event):
        self._window.toggle_maximize()


class MenuStrip(QWidget):
    """标题栏下方的文字菜单条：文件 / 主题 / 设置。

    切换语言不重建菜单（重建要重新登记 F5 快捷键，容易重复），保留 QAction 引用逐个 setText()。"""

    def __init__(self, window: "MainWindow"):
        super().__init__()
        self._window = window
        self._layout = QHBoxLayout(self)
        # 底部留 3px 给主题色分隔线（见 paintEvent），菜单项之间拉开一点间距。
        self._layout.setContentsMargins(6, 0, 6, 3)
        self._layout.setSpacing(10)
        self._buttons: dict[str, QPushButton] = {}
        self._theme_actions: dict[str, ThemeMenuItem] = {}
        for key, builder in (("menu.file", self._file_menu), ("menu.theme", self._theme_menu),
                             ("menu.settings", self._settings_menu)):
            button = QPushButton()
            button.setFlat(True)
            button.setFixedHeight(26)
            button.setMenu(builder())
            self._buttons[key] = button
            self._layout.addWidget(button)
        # "关于"不需要子菜单，直接绑命令
        about_button = QPushButton()
        about_button.setFlat(True)
        about_button.setFixedHeight(26)
        about_button.clicked.connect(self._window.show_about_dialog)
        self._buttons["menu.about"] = about_button
        self._layout.addWidget(about_button)
        self._layout.addStretch()
        self.setFixedHeight(31)
        theme.changed.connect(self.update)
        self.retranslate()

    def paintEvent(self, _event):
        # 菜单文字下方一条主题色分隔线，左右与菜单内容边距对齐；颜色和画法跟
        # 主页签外层圆角边框（Card 描边）一致，PRIMARY 实线看起来又粗又深。
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(theme.color("CARD_BORDER"), 1))
        y = self.height() - 0.5
        painter.drawLine(QPointF(8, y), QPointF(self.width() - 8, y))

    def retranslate(self) -> None:
        for key, button in self._buttons.items():
            button.setText(t(key))
        self._refresh_action.setText(t("app.refresh"))
        self._clear_cache_action.setText(t("app.clear_cache_dir"))
        self._vcredist_action.setText(t("app.install_vcredist"))
        self._vcredist_action.setToolTip(t("app.install_vcredist_hint"))
        for name, action in self._theme_actions.items():
            action.setText(t(f"theme.{name}"))
        self._ornaments_action.setText(t("theme.ornaments"))
        self._bg_settings_action.setText(t("theme.custom_bg_settings"))
        self._font_settings_action.setText(t("theme.font_settings"))
        self._minimize_action.setText(t("settings.minimize_on_close_label"))
        self._steam_backend_action.setText(t("settings.steam_backend_label"))
        self._defender_action.setText(t("settings.defender_label"))
        self._cache_dir_action.setText(t("settings.cache_dir_menu"))
        self._lang_menu.setTitle(t("settings.language_label"))
        self._lang_actions["zh"].setText(t("menu.lang_zh"))
        self._lang_actions["en"].setText(t("menu.lang_en"))

    def _file_menu(self) -> QMenu:
        menu = FrostedMenu(self)
        # "安装运行库"的用途提示：不走 Qt 默认的悬停延迟（鼠标稍动就重新计时，
        # 时有时无），悬停到这一项时立即显示，移到别的项或关闭菜单时收起。
        menu.hovered.connect(self._on_file_menu_hovered)
        menu.aboutToHide.connect(QToolTip.hideText)
        refresh = QAction(t("app.refresh"), self)
        refresh.setShortcut(QKeySequence(Qt.Key.Key_F5))
        refresh.triggered.connect(self._window.refresh_all)
        menu.addAction(refresh)
        self._window.addAction(refresh)  # 让 F5 在整个窗口内生效
        self._refresh_action = refresh

        # 跟"刷新全部"一样不依赖当前选没选存档，随时能点——清空的是图标/解析/翻译
        # 这些按需自动重建的缓存，不影响存档数据，不需要重启。
        clear_cache = QAction(t("app.clear_cache_dir"), self)
        clear_cache.triggered.connect(self._window.clear_cache_dir)
        menu.addAction(clear_cache)
        self._clear_cache_action = clear_cache

        # 手动入口：Mod 页会自动探测缺运行库并提示，这里兜底漏检的情况
        vcredist = QAction(t("app.install_vcredist"), self)
        vcredist.setToolTip(t("app.install_vcredist_hint"))
        vcredist.triggered.connect(self._window.install_vcredist)
        menu.addAction(vcredist)
        self._vcredist_action = vcredist
        return menu

    def _on_file_menu_hovered(self, action: QAction) -> None:
        if action is self._vcredist_action:
            menu = self.sender()
            # 提示显示在这一项的右侧，跟菜单项同一高度。
            rect = menu.actionGeometry(action)
            QToolTip.showText(menu.mapToGlobal(rect.topRight()) + QPoint(8, 0), action.toolTip(), menu)
        else:
            QToolTip.hideText()

    def _theme_menu(self) -> QMenu:
        menu = FrostedMenu(self)

        def add_item(item: CheckMenuItem) -> CheckMenuItem:
            action = QWidgetAction(menu)
            action.setDefaultWidget(item)
            menu.addAction(action)
            return item

        # 每个主题名用该主题自己的主色显示（QAction 设不了单独文字颜色，用自绘项）；
        # 当前主题打勾加粗，直接现查 theme.name，不需要 QActionGroup 维护选中态。
        for name in THEME_NAMES:
            self._theme_actions[name] = add_item(ThemeMenuItem(menu, name, t(f"theme.{name}"), theme.set_theme))
        menu.addSeparator()
        # 下面几项也用同款自绘项：原生 QAction 有勾选列，文字起点跟主题项对不齐。
        # 点缀图案随主题，这里是总开关
        self._ornaments_action = add_item(CheckMenuItem(
            menu, t("theme.ornaments"),
            lambda: self._window.set_ornaments_enabled(not get_window_ornaments_enabled()),
            get_window_ornaments_enabled))
        # 背景图/字体样式是跟颜色主题解耦的全局设置，点开只弹设置窗口，不切主题。
        self._bg_settings_action = add_item(CheckMenuItem(
            menu, t("theme.custom_bg_settings"), self._window.show_custom_bg_dialog))
        self._font_settings_action = add_item(CheckMenuItem(
            menu, t("theme.font_settings"), self._window.show_font_settings_dialog))
        return menu

    def _settings_menu(self) -> QMenu:
        menu = FrostedMenu(self)
        lang_menu = FrostedMenu(t("settings.language_label"), self)
        lang_group = QActionGroup(self)
        self._lang_actions: dict[str, QAction] = {}
        for code, key in (("zh", "menu.lang_zh"), ("en", "menu.lang_en")):
            action = QAction(t(key), self, checkable=True)
            action.setChecked(get_lang() == code)
            action.triggered.connect(lambda _checked=False, c=code: self._window.switch_language(c))
            lang_group.addAction(action)
            lang_menu.addAction(action)
            self._lang_actions[code] = action
        menu.addMenu(lang_menu)
        self._lang_menu = lang_menu
        menu.addSeparator()
        steam_backend = QAction(t("settings.steam_backend_label"), self)
        steam_backend.triggered.connect(self._window.show_steam_backend_dialog)
        menu.addAction(steam_backend)
        self._steam_backend_action = steam_backend
        defender = QAction(t("settings.defender_label"), self)
        defender.triggered.connect(self._window.show_defender_dialog)
        menu.addAction(defender)
        self._defender_action = defender
        cache_dir = QAction(t("settings.cache_dir_menu"), self)
        cache_dir.triggered.connect(self._window.show_cache_dir_dialog)
        menu.addAction(cache_dir)
        self._cache_dir_action = cache_dir
        menu.addSeparator()
        minimize = QAction(t("settings.minimize_on_close_label"), self, checkable=True)
        minimize.setChecked(get_minimize_on_close())
        minimize.triggered.connect(lambda checked: set_minimize_on_close(bool(checked)))
        menu.addAction(minimize)
        self._minimize_action = minimize
        return menu


class _RefreshingCombo(QComboBox):
    """每次点开下拉前先通知外面刷新一遍选项文字（运行中标注只在点开时现查，不用轮询）。"""

    about_to_open = Signal()

    def showPopup(self):
        self.about_to_open.emit()
        super().showPopup()


# 账号下拉项上标记"Steam 当前登录"的数据角色
_STEAM_ACTIVE_ROLE = Qt.ItemDataRole.UserRole + 1
_DOT_GAP = 6


def _dot_diameter(metrics) -> float:
    return max(4.0, metrics.height() * 0.3)


def _dot_top(center_y: int, diameter: int, metrics) -> int:
    """绿点上边缘：按行的几何中心放会显得偏上（中文字形视觉重心偏下），向下挪约字高的 8%。"""
    return center_y - diameter // 2 + max(1, round(metrics.height() * 0.08))


def _paint_steam_dot(painter: QPainter, x: float, center_y: float, diameter: float) -> None:
    """画 Steam 当前登录的小绿点（x 为绿点左边缘）。"""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(theme.color("SUCCESS"))
    painter.drawEllipse(QPointF(x + diameter / 2, center_y), diameter / 2, diameter / 2)
    painter.restore()


def _color_distance(a, b) -> int:
    return abs(a.red() - b.red()) + abs(a.green() - b.green()) + abs(a.blue() - b.blue()) + abs(a.alpha() - b.alpha())


def _content_right_edge(image: QImage, area: QRect, edge: int = 0) -> int | None:
    """area 内最右侧与底色明显不同的列的右边界（相对 area 左边）；没有内容返回 None。
    底色取距右边缘 edge 像素的那一列，跳过边框、焦点框之类的竖线。"""
    top, bottom = max(area.top(), 0), min(area.bottom(), image.height() - 1)
    right = min(area.right(), image.width() - 1) - edge
    background = [image.pixelColor(right, y) for y in range(top, bottom + 1)]
    for x in range(right - 1, area.left() - 1, -1):
        if any(_color_distance(image.pixelColor(x, y), background[y - top]) > 60 for y in range(top, bottom + 1)):
            return x + 1 - area.left()
    return None


class _SteamDot(QWidget):
    """盖在下拉框/弹出列表上的小绿点：单独绘制，不改下拉框的列表代理（换代理会让弹出列表外观与其他下拉框不同），
    也不与下拉框自身（及全局绘制补丁）的画笔冲突。"""

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.hide()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        _paint_steam_dot(painter, 0, self.height() / 2, self.width())
        painter.end()


_TIP_PAD = 5  # 展开列表绿点左右各扩出的悬停感应宽度（绿点只有约 6px，难以悬停）


class _SteamTipDot(QWidget):
    """展开列表里 Steam 当前登录那一行的绿点：悬停显示"当前登录"提示。
    感应区比绿点大（左右各扩 _TIP_PAD、与行同高）；鼠标点击/移动事件忽略，交给下面的列表行处理。"""

    def __init__(self, parent):
        super().__init__(parent)
        self.setToolTip(t("selector.account_current_tag"))
        self.hide()
        self.diameter = 6
        self.dot_top = 0

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        _paint_steam_dot(painter, _TIP_PAD, self.dot_top + self.diameter / 2, self.diameter)
        painter.end()

    def mousePressEvent(self, event) -> None:
        event.ignore()

    def mouseReleaseEvent(self, event) -> None:
        event.ignore()

    def mouseMoveEvent(self, event) -> None:
        event.ignore()

    def mouseDoubleClickEvent(self, event) -> None:
        event.ignore()


class _AccountCombo(QComboBox):
    """游戏账号下拉：选择框和展开列表里，Steam 当前登录的账号都在文字后面显示小绿点。"""

    def __init__(self):
        super().__init__()
        self._dot = _SteamDot(self)
        self._popup_dot = _SteamTipDot(self.view().viewport())
        self._closed_text_end: dict[tuple, int | None] = {}
        self._measuring = False  # render() 会触发尺寸事件，防止 refresh_dot 重入
        self.currentIndexChanged.connect(self.refresh_dot)
        theme.changed.connect(self._dot.update)  # 绿点颜色跟随主题

    def _dot_space(self) -> int:
        return _DOT_GAP + round(_dot_diameter(self.fontMetrics())) + 2

    def sizeHint(self) -> QSize:
        size = super().sizeHint()
        return QSize(size.width() + self._dot_space(), size.height())

    def minimumSizeHint(self) -> QSize:
        size = super().minimumSizeHint()
        return QSize(size.width() + self._dot_space(), size.height())

    def refresh_dot(self) -> None:
        """选择框里的绿点：当前项是 Steam 当前登录的账号时，放在文字后面。"""
        if self._measuring:
            return
        index = self.currentIndex()
        if index < 0 or not self.itemData(index, _STEAM_ACTIVE_ROLE):
            self._dot.hide()
            return
        opt = QStyleOptionComboBox()
        self.initStyleOption(opt)
        edit = self.style().subControlRect(QStyle.ComplexControl.CC_ComboBox, opt,
                                           QStyle.SubControl.SC_ComboBoxEditField, self)
        metrics = self.fontMetrics()
        diameter = math.ceil(_dot_diameter(metrics))
        # 文字实际起点受样式内边距和字体补丁影响，按字宽推算会偏左：把选择框画出来实测文字结尾
        key = (self.currentText(), self.size().width(), self.size().height(), self.font().key(), theme.font_style)
        # 只在下拉框和主窗口都已显示后实测：构造期间 render() 会连带触发主窗口的尺寸事件（此时主窗口尚未构造完）。
        # 未显示时先按字宽推算，showEvent 安排的延迟回调会再实测一次
        if key not in self._closed_text_end and self.isVisible() and self.window().isVisible():
            self._dot.hide()
            image = QImage(self.size(), QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(Qt.GlobalColor.transparent)
            self._measuring = True
            try:
                self.render(image)
            finally:
                self._measuring = False
            end = _content_right_edge(image, edit)
            self._closed_text_end[key] = edit.left() + end if end is not None else None
        text_end = self._closed_text_end.get(key)
        if text_end is None:
            text_end = edit.left() + metrics.horizontalAdvance(self.currentText())
        self._dot.setGeometry(text_end + _DOT_GAP, _dot_top(edit.center().y(), diameter, metrics),
                              diameter, diameter)
        self._dot.show()
        self._dot.raise_()

    def showPopup(self) -> None:
        super().showPopup()
        self._place_popup_dot()

    def _place_popup_dot(self) -> None:
        """展开列表里的绿点：放在 Steam 当前登录那一行的文字后面。
        文字位置由列表样式决定，截取这一行真实画出来的像素实测文字结尾（合成参数离屏绘制会和实际字体、边距对不上）。"""
        view = self.view()
        self._popup_dot.hide()
        row = next((i for i in range(self.count()) if self.itemData(i, _STEAM_ACTIVE_ROLE)), -1)
        rect = view.visualRect(self.model().index(row, 0)) if row >= 0 else QRect()
        # 只截可见部分：行矩形可能比被收窄的弹出列表宽，超出部分会被误判成文字结尾
        rect = rect.intersected(view.viewport().rect())
        if rect.isEmpty():
            return
        image = view.viewport().grab(rect).toImage()
        ratio = image.devicePixelRatio() or 1.0
        # 行最右侧可能有 1px 边线（实测离屏为灰色竖线），底色参照往里留几像素
        end = _content_right_edge(image, image.rect(), edge=math.ceil(4 * ratio))
        if end is None:
            return
        metrics = view.fontMetrics()
        diameter = math.ceil(_dot_diameter(metrics))
        left = rect.left() + round(end / ratio) + _DOT_GAP - _TIP_PAD
        self._popup_dot.diameter = diameter
        self._popup_dot.dot_top = _dot_top(rect.center().y(), diameter, metrics) - rect.top()
        self._popup_dot.setGeometry(left, rect.top(), diameter + 2 * _TIP_PAD, rect.height())
        self._popup_dot.show()
        self._popup_dot.raise_()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.refresh_dot()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.refresh_dot()
        # 首次显示时样式表字体可能尚未生效，事件循环回来后再按最终字体定位一次
        QTimer.singleShot(0, self.refresh_dot)

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self.refresh_dot()


class ClusterBar(QWidget):
    """顶部统一存档选择栏：存档类型（Steam/WeGame）+ 游戏账号（本机有多个时）+ 存档下拉 + 刷新。全部页签共用。"""

    _SPACING, _TIGHT_SPACING = 10, 6
    _HEIGHT, _COMPACT_HEIGHT = 56, 42  # 控件约 32 高，窗口矮时去掉多余的上下留白

    def __init__(self, ctx: AppContext, window: "MainWindow"):
        super().__init__()
        self._ctx = ctx
        self._populating = False
        row = QHBoxLayout(self)
        # 右边距与各页面内容区对齐（外壳另有 2px 边框），"刷新"与页面级按钮右对齐
        row.setContentsMargins(24, 4, 8, 4)
        row.setSpacing(self._SPACING)
        self._row = row
        self._platform_label, self._account_label, self._archive_label = QLabel(), QLabel(), QLabel()
        for label in (self._platform_label, self._account_label, self._archive_label):
            label.setProperty("heading", True)
        # 同一平台登录过多个游戏账号时才显示；本地存档和 Mod 配置同步都跟随这里选中的账号
        self._account = _AccountCombo()
        self._account.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self._account.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._platform = QComboBox()
        self._platform.addItems(["Steam", "WeGame"])
        # 宽度至少 110 并随字号自适应（写死在大字号下放不下 "WeGame" 和箭头），Fixed 不随窗口拉伸
        self._platform.setMinimumWidth(110)
        self._platform.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self._platform.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._cluster = _RefreshingCombo()
        self._cluster.about_to_open.connect(self.reload)
        # 按最长的存档名自适应宽度，不随窗口拉伸；窗口窄放不下时可收到最小宽度（存档名被截断），
        # 不再与右侧按钮重叠
        self._cluster.setMinimumWidth(150)
        self._cluster.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self._cluster.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        # "创建服务器存档"作用于整个存档集合，放在全局存档选择器右侧；"打开位置"在其左侧，打开当前存档文件夹
        self._open_location = QPushButton()
        self._open_location.clicked.connect(self._on_open_location)
        self._create_save = QPushButton()
        self._create_save.clicked.connect(window.open_creation_wizard)
        self._refresh = QPushButton()
        self._refresh.clicked.connect(window.refresh_all)
        row.addWidget(self._platform_label)
        row.addWidget(self._platform)
        row.addSpacing(8)
        row.addWidget(self._account_label)
        row.addWidget(self._account)
        row.addWidget(self._archive_label)
        row.addWidget(self._cluster)
        row.addWidget(self._open_location)
        row.addStretch()
        row.addWidget(self._create_save)
        row.addWidget(self._refresh)
        self._platform.activated.connect(self._on_platform)
        self._account.activated.connect(self._on_account)
        self._cluster.activated.connect(self._on_cluster)
        ctx.env_changed.connect(self.reload)
        ctx.platform_changed.connect(self.reload)
        theme.changed.connect(self._fit_spacing)  # 字号变了，各控件理想宽度随之变化
        self.setFixedHeight(self._HEIGHT)
        self.retranslate()
        self.reload()

    def retranslate(self) -> None:
        self._platform_label.setText(t("selector.save_type"))
        self._account_label.setText(t("selector.account"))
        self._account_label.setToolTip(t("selector.account_tip"))
        self._account.setToolTip(t("selector.account_tip"))
        self._archive_label.setText(t("selector.archive"))
        self._open_location.setText(t("env.open_location"))
        self._create_save.setText(t("save.create_server_save"))
        self._refresh.setText(t("save.refresh"))
        # "刷新"至少跟本地服务器页"更换路径"按钮一样宽（约 4 个字），不再只按两个字收窄。
        self._refresh.setMinimumWidth(QPushButton(t("local.install_change_btn")).sizeHint().width())

    def set_compact(self, compact: bool) -> None:
        self.setFixedHeight(self._COMPACT_HEIGHT if compact else self._HEIGHT)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_spacing()

    def _fit_spacing(self) -> None:
        """按常规间距放不下全部控件时收窄间距；按各控件理想宽度判断，不随当前间距变化，不会来回切换。"""
        row = self._row
        widths = 0
        for index in range(row.count()):
            item = row.itemAt(index)
            widget = item.widget()
            if widget is not None:
                widths += 0 if widget.isHidden() else widget.sizeHint().width()
            elif item.spacerItem() is not None:
                widths += item.spacerItem().minimumSize().width()
        margins = row.contentsMargins()
        need = widths + self._SPACING * (row.count() - 1) + margins.left() + margins.right()
        spacing = self._SPACING if self.width() >= need else self._TIGHT_SPACING
        if row.spacing() != spacing:
            row.setSpacing(spacing)

    def _on_open_location(self) -> None:
        cluster = self._ctx.selected_cluster()
        if cluster is not None and cluster.path.is_dir():
            os.startfile(str(cluster.path))

    def reload(self) -> None:
        self._populating = True
        try:
            self._platform.setCurrentText("WeGame" if self._ctx.platform == Platform.WEGAME else "Steam")
            accounts = self._ctx.accounts()
            current = self._ctx.current_account()
            self._account.clear()
            for account in accounts:
                self._account.addItem(self._ctx.account_text(account), account.id)
                self._account.setItemData(self._account.count() - 1, account.steam_active, _STEAM_ACTIVE_ROLE)
                if current is not None and account.id == current.id:
                    self._account.setCurrentIndex(self._account.count() - 1)
            self._account.refresh_dot()
            self._account_label.setVisible(len(accounts) > 1)
            self._account.setVisible(len(accounts) > 1)
            self._cluster.clear()
            selected = self._ctx.selected_cluster()
            for cluster in self._ctx.clusters():
                self._cluster.addItem(self._ctx.cluster_text(cluster), cluster)
                if selected is not None and cluster.path == selected.path:
                    self._cluster.setCurrentIndex(self._cluster.count() - 1)
        finally:
            self._populating = False
        self._fit_spacing()  # 账号框显隐、存档名长短都会改变所需宽度

    def _leave_confirmed(self) -> bool:
        """换平台/账号/存档前处理未保存改动；取消时把选择框恢复成当前选中项。"""
        if self._ctx.confirm_unsaved():
            return True
        self.reload()
        return False

    def _on_platform(self, _index: int) -> None:
        platform = Platform.WEGAME if self._platform.currentText() == "WeGame" else Platform.STEAM
        if platform != self._ctx.platform and not self._leave_confirmed():
            return
        self._ctx.set_platform(platform)

    def _on_account(self, index: int) -> None:
        if not self._populating and index >= 0:
            current = self._ctx.current_account()
            if (current is None or current.id != self._account.itemData(index)) and not self._leave_confirmed():
                return
            self._ctx.select_account(self._account.itemData(index))

    def _on_cluster(self, index: int) -> None:
        if not self._populating and index >= 0:
            cluster = self._cluster.itemData(index)
            selected = self._ctx.selected_cluster()
            if (selected is None or cluster is None or cluster.path != selected.path) and not self._leave_confirmed():
                return
            self._ctx.select_cluster(cluster)


class MainWindow(QWidget):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self.background = Background()
        self.text_halo = TextHaloManager(self.background)
        self.text_halo.watch(self)
        self.text_halo.refresh()
        self._resizing = False
        self._compact_chrome = False
        self._restore_geometry: QRect | None = None
        self._quitting = False
        self._settle = QTimer(self, singleShot=True, interval=150)
        self._settle.timeout.connect(self._end_resize)

        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setWindowTitle(t("app.title"))
        self.setWindowIcon(QIcon(str(bundled_resource_dir() / "icons" / "app" / "icon.png")))
        self._place_initially()

        root = QVBoxLayout(self)
        root.setContentsMargins(2, 2, 2, 2)
        root.setSpacing(0)
        self.titlebar = TitleBar(self)
        self.menu_strip = MenuStrip(self)
        # 主页签显式比子页签（FONT_SIZE_BASE）大一档，保持"主页签 > 子页签 > 正文"的层级
        self.tabbar = PillTabBar([t(f"tab.{key}") for key in TAB_KEYS], font_size_key="FONT_SIZE_MD", bold=True)
        self.cluster_bar = ClusterBar(ctx, self)
        self.stack = QStackedWidget()
        self.status = QLabel()
        self._update_notice = QLabel("")
        self._update_notice.linkActivated.connect(self._open_update_notice)
        self._update_release = None
        # 下载更新时在状态栏右侧显示进度条，平时隐藏
        self._update_progress = QProgressBar()
        self._update_progress.setRange(0, 100)
        self._update_progress.setTextVisible(False)
        self._update_progress.setFixedSize(160, 8)
        self._update_progress.setVisible(False)
        self._updater = SelfUpdater(self)
        status_row = QHBoxLayout()
        # 左边距与页面内容左边缘对齐
        status_row.setContentsMargins(8, 4, 18, 6)
        status_row.addWidget(self.status, 1)
        status_row.addWidget(self._update_progress)
        status_row.addSpacing(8)
        status_row.addWidget(self._update_notice)
        for widget in (self.titlebar, self.menu_strip, self.tabbar, self.cluster_bar):
            root.addWidget(widget)
        root.addWidget(self.stack, 1)
        root.addLayout(status_row)

        self.pages: dict[str, QWidget] = {}
        self._build_pages()
        self.tabbar.current_changed.connect(self._on_tab_changed)
        ctx.cluster_changed.connect(self._on_cluster_changed)
        ctx.unsaved_changed.connect(self._update_tab_labels)
        ctx.confirm_unsaved = self._confirm_unsaved
        ctx.tab_requested.connect(self.goto_tab)
        ctx.platform_changed.connect(self._update_status)
        ctx.env_changed.connect(self._update_status)
        theme.changed.connect(self._on_theme_changed)

        self._grips = []
        for edges, cursor in (
            (Qt.Edge.LeftEdge, Qt.CursorShape.SizeHorCursor), (Qt.Edge.RightEdge, Qt.CursorShape.SizeHorCursor),
            (Qt.Edge.TopEdge, Qt.CursorShape.SizeVerCursor), (Qt.Edge.BottomEdge, Qt.CursorShape.SizeVerCursor),
            (Qt.Edge.LeftEdge | Qt.Edge.TopEdge, Qt.CursorShape.SizeFDiagCursor),
            (Qt.Edge.RightEdge | Qt.Edge.BottomEdge, Qt.CursorShape.SizeFDiagCursor),
            (Qt.Edge.RightEdge | Qt.Edge.TopEdge, Qt.CursorShape.SizeBDiagCursor),
            (Qt.Edge.LeftEdge | Qt.Edge.BottomEdge, Qt.CursorShape.SizeBDiagCursor),
        ):
            self._grips.append(Grip(self, edges, cursor))
        self._ornaments = WindowOrnaments(self, lambda: self._restore_geometry is not None,
                                          get_window_ornaments_enabled())
        self._build_tray()
        self._update_status()

    # ── 装配 ────────────────────────────────────────────────────────────
    def _build_pages(self) -> None:
        for key in TAB_KEYS:
            page = self._make_page(key)
            self.pages[key] = page
            self.stack.addWidget(page)
        self.current_page().load()
        self._fit_chrome_to_height()  # 窗口尺寸在页面建好之前就定了，这里补判一次

    def _make_page(self, key: str):
        page_cls = {
            "local": LocalServicePage, "world": WorldSettingsPage, "mods": ModPage,
            "server": ServerConfigPage, "saves": SaveInfoPage, "sakura": SakuraPage,
        }[key]
        return page_cls(self.ctx)

    def goto_tab(self, key: str) -> None:
        index = TAB_KEYS.index(key)
        self.tabbar.set_current_index(index)
        self._on_tab_changed(index)

    def current_page(self):
        return self.pages[TAB_KEYS[self.tabbar.current_index()]]

    def _on_tab_changed(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        page = self.current_page()
        if page.stale:
            page.load()

    def _on_cluster_changed(self, _cluster) -> None:
        for page in self.pages.values():
            page.stale = True
        self.current_page().load()

    def _update_tab_labels(self) -> None:
        """页签名；有未保存改动的页在名字后加圆点（名字两侧的留白保持不变）。"""
        labels = []
        for key in TAB_KEYS:
            label = t(f"tab.{key}")
            page = self.pages.get(key) if hasattr(self, "pages") else None
            if page is not None and page.has_unsaved_changes():
                tail = label[len(label.rstrip()):]
                label = f"{label.rstrip()} •{tail}"
            labels.append(label)
        self.tabbar.set_labels(labels)

    def _confirm_unsaved(self, cluster=None) -> bool:
        """有页面未保存时询问保存/不保存/取消，返回是否继续；cluster 给出且不是当前存档时不问。"""
        if cluster is not None:
            selected = self.ctx.selected_cluster()
            if selected is None or str(selected.path) != str(cluster.path):
                return True
        keys = [key for key in TAB_KEYS if self.pages[key].has_unsaved_changes()]
        if not keys:
            return True
        sep = "、" if get_lang() == "zh" else ", "
        choice = dialogs.ask_unsaved(self, sep.join(t(f"tab.{key}").strip() for key in keys))
        if choice == "save":
            return all(self.pages[key].save_unsaved_changes() for key in keys)
        if choice == "discard":
            for key in keys:
                self.pages[key].discard_unsaved_changes()
            return True
        return False

    def refresh_all(self) -> None:
        self.ctx.refresh_env()

    def clear_cache_dir(self) -> None:
        """"文件"菜单"清理缓存目录"——只清缓存根目录下的内容，根目录本身留着。
        图标/解析/翻译缓存是按需重建的，清完不需要重启。"""
        import shutil

        from dstools.shared.resource_paths import cache_root_dir

        target = cache_root_dir()
        if not target.is_dir() or not any(target.iterdir()):
            dialogs.show_info(self, t("app.clear_cache_dir"), t("settings.cache_dir_clear_empty"))
            return
        if not dialogs.ask_yes_no(self, t("app.clear_cache_dir"),
                                  t("settings.cache_dir_clear_confirm", path=str(target))):
            return
        failed = []
        for item in target.iterdir():
            try:
                if item.is_dir() and not item.is_symlink():
                    shutil.rmtree(item)
                else:
                    item.unlink()
            except OSError:
                failed.append(item.name)
        if failed:
            dialogs.show_warning(self, t("app.clear_cache_dir"),
                                 t("settings.cache_dir_clear_partial", names="、".join(failed)))
        else:
            dialogs.show_info(self, t("app.clear_cache_dir"), t("settings.cache_dir_clear_done"))

    def install_vcredist(self) -> None:
        """"文件"菜单"安装运行库"——跟 Mod 管理页签自动探测走的是同一个安装器，
        这个入口不依赖任何自动探测，怀疑图标/其它功能异常时能自己主动装一遍。"""
        from dstools.qt.tool_download import download_tool_with_dialog
        from dstools.shared import tool_store
        from dstools.shared.tex_convert import VCREDIST_RELATIVE, launch_vcredist_installer

        if launch_vcredist_installer():
            dialogs.show_info(self, t("app.install_vcredist"), t("mod.vcredist_installer_launched"))
        elif tool_store.store_tool(VCREDIST_RELATIVE) is not None and tool_store.find_stored(VCREDIST_RELATIVE) is None:
            # 发布版不打包安装程序：先从微软官网（备用 Gitee/GitHub）下载，下完再拉起
            download_tool_with_dialog(self, VCREDIST_RELATIVE, t("app.install_vcredist"),
                                      lambda _path: self.install_vcredist())
        else:
            dialogs.show_error(self, t("app.install_vcredist"), t("mod.vcredist_installer_missing"))

    def show_custom_bg_dialog(self) -> None:
        from dstools.qt.settings_dialogs import BackgroundImageDialog

        BackgroundImageDialog(self).exec()

    def show_font_settings_dialog(self) -> None:
        from dstools.qt.settings_dialogs import FontSettingsDialog

        FontSettingsDialog(self).exec()

    def show_defender_dialog(self) -> None:
        from dstools.qt.settings_dialogs import WindowsDefenderDialog

        WindowsDefenderDialog(self).exec()

    def show_steam_backend_dialog(self) -> None:
        # 通道切换牵涉开服程序和运行状态，交给本地服务页处理（页面构造时已检测好开服程序）
        self.pages["local"].choose_backend()

    def show_cache_dir_dialog(self) -> None:
        from dstools.qt.settings_dialogs import CacheDirDialog

        CacheDirDialog(self).exec()

    def show_about_dialog(self) -> None:
        from dstools.qt.settings_dialogs import AboutDialog

        AboutDialog(self).exec()

    def check_cache_dir_on_startup(self) -> None:
        """中文用户名导致默认缓存路径不可用时，在启动后主动引导修复。"""
        from dstools.shared.resource_paths import cache_root_dir, path_is_ascii

        path = cache_root_dir()
        if path_is_ascii(path):
            return
        choice = dialogs.ask_choice(
            self, t("settings.cache_dir_label"), t("settings.cache_dir_startup_warning", path=str(path)),
            [(t("settings.cache_dir_fix_now"), "fix"), (t("settings.cache_dir_later"), "later")],
            default="fix", min_width=520)
        if choice == "fix":
            self.show_cache_dir_dialog()

    def start_update_check(self) -> None:
        """启动时后台查一次最新 Release，失败不重试。有新版本时始终点亮状态栏提示，
        "提醒更新"开关打开（默认）时才额外弹窗并在后台预下载。"""
        from dstools.shared.update_check import check_latest_release

        def done(result) -> None:
            if not is_update_available(result):
                return
            self.show_update_notice(result)
            if get_remind_update_enabled():
                # 先在后台预下载，用户读更新说明时已在下载；关了"提醒更新"就不主动占用流量
                self._updater.prefetch(result)
                self._updater.prompt(result)

        run_async(check_latest_release, done, lambda _exc: None)

    def show_update_notice(self, release) -> None:
        self._update_release = release
        self._update_notice.setText(
            f'<a href="update" style="color: {theme.hex("PRIMARY")};">'
            f'{t("app.update_available", version=release.version)}</a>')

    def import_offline_package(self, package) -> None:
        """"关于 → 手动更新 → 从文件导入"选中的网盘离线包：更新包走更新流程，工具包校验后存入 data/tools。"""
        from dstools.shared import tool_store
        from dstools.shared.auto_update import is_update_package

        title = t("about.manual_update_title")
        if is_update_package(package):
            self._updater.install_from_package(package)
            return
        if not tool_store.is_tools_package(package):
            dialogs.show_error(self, title, t("update.package_unknown"))
            return

        def done(result) -> None:
            imported, rejected = result
            lines = []
            if imported:
                lines.append(t("update.tools_imported", names="、".join(imported)))
            if rejected:
                lines.append(t("update.tools_rejected", names="、".join(rejected)))
            show = dialogs.show_info if imported and not rejected else dialogs.show_warning
            show(self, title, "\n\n".join(lines) or t("update.tools_none"))

        run_async(lambda: tool_store.import_tools_package(package), done,
                  lambda exc: dialogs.show_error(self, title, t("update.import_failed", error=exc)))

    def open_update_prompt(self, release) -> None:
        """"关于"里检查到新版本、或点状态栏提示时调用：弹出更新窗口。"""
        self.show_update_notice(release)
        self._updater.prompt(release)

    def _open_update_notice(self, _url: str) -> None:
        if self._update_release is not None:
            self._updater.prompt(self._update_release)

    def set_update_progress(self, percent: int | None) -> None:
        """下载中显示进度条和百分比；None 恢复成"发现新版本"链接。"""
        if percent is None:
            self._update_progress.setVisible(False)
            if self._update_release is not None:
                self.show_update_notice(self._update_release)
            return
        self._update_progress.setVisible(True)
        self._update_progress.setValue(percent)
        version = self._update_release.version if self._update_release is not None else ""
        self._update_notice.setText(t("update.downloading", version=version, percent=percent))

    def switch_language(self, lang: str) -> None:
        """切换界面语言：标题栏/菜单/存档栏/页签名/托盘立即刷新；页面内容只重建当前页，其余标脏切过去再补。"""
        old_lang = get_lang()
        if old_lang == lang:
            return
        set_lang(lang)
        # 先把所有已创建控件上的静态文字按对照表换掉（各页面构造时写死的按钮/标签），
        # 再走下面各部分自己的 retranslate 和页面刷新，补上带参数的动态文字。
        from dstools.qt.retranslate import retranslate_all_widgets

        retranslate_all_widgets(old_lang, lang)
        self.setWindowTitle(t("app.title"))
        self.titlebar.retranslate()
        self.menu_strip.retranslate()
        self.cluster_bar.retranslate()
        self._update_tab_labels()
        self.tray.setToolTip(t("app.title"))
        self._tray_show.setText(t("tray.show"))
        self._tray_exit.setText(t("tray.exit"))
        for page in self.pages.values():
            page.retranslate()
            page.stale = True
        self.current_page().load()
        self._update_status()

    def open_creation_wizard(self) -> None:
        from dstools.qt.creation_wizard import CreationWizardDialog

        dialog = CreationWizardDialog(self.ctx, background=self.background)
        self.text_halo.watch(dialog)
        # 向导是无父窗口的顶层窗，按主窗口所在显示器重新限尺寸，并把位置夹在工作区内
        area = (self.screen() or QGuiApplication.primaryScreen()).availableGeometry()
        dialogs.fit_to_screen(dialog, 1400, 860, area)
        saved = get_creation_wizard_size()
        if saved is not None:
            # 沿用上次调整过的尺寸，只按当前显示器工作区封顶
            dialog.resize(max(dialog.minimumWidth(), min(saved[0], area.width())),
                          max(dialog.minimumHeight(), min(saved[1], area.height())))
        pos = self.geometry().center() - dialog.rect().center()
        dialog.move(max(area.left(), min(pos.x(), area.right() - dialog.width() + 1)),
                    max(area.top(), min(pos.y(), area.bottom() - dialog.height() + 1)))
        dialog.exec()
        created_path = dialog.created_path
        launch = dialog.launch_requested
        # 最大化关闭时记还原前的尺寸，免得下次一打开就铺满
        size = (dialog.normalGeometry() if dialog.isMaximized() else dialog.geometry()).size()
        set_creation_wizard_size(size.width(), size.height())
        if created_path is not None:
            self._select_created_cluster(created_path, launch)

    def _select_created_cluster(self, path: Path, launch: bool) -> None:
        """创建向导完成后：无论是否启动都选中新建存档并同步顶部选择框；选择「立即启动」
        时再跳到本地服务器页触发启动——走页面完整的令牌/端口/Mod 预检流程。"""
        if not self._confirm_unsaved():
            return
        if self.ctx.platform != Platform.STEAM:
            self.ctx.set_platform(Platform.STEAM)
        cluster = next((c for c in self.ctx.clusters() if c.path == path), None)
        if cluster is None:
            return
        self.ctx.clear_stale_mappings(path)
        self.ctx.select_cluster(cluster)
        self.cluster_bar.reload()
        if launch:
            self.goto_tab("local")
            self.pages["local"].launch_current_cluster()

    def _update_status(self) -> None:
        self.status.setText(self.ctx.status_text())

    def _on_theme_changed(self) -> None:
        self.tabbar.update()
        self.repaint()

    def set_ornaments_enabled(self, enabled: bool) -> None:
        set_window_ornaments_enabled(enabled)
        self._ornaments.set_enabled(enabled)

    # ── 位置/尺寸 ───────────────────────────────────────────────────────
    def _place_initially(self) -> None:
        avail = QGuiApplication.primaryScreen().availableGeometry()
        # 最小尺寸不能大于工作区，否则高缩放屏（如 1080p@200%）窗口一出来就超出屏幕且缩不回去
        self.setMinimumSize(min(MIN_W, avail.width()), min(MIN_H, avail.height()))
        width, height = self._startup_size(avail)
        self.resize(width, height)
        self.move(self._startup_position(width, height))

    def _startup_size(self, avail: QRect) -> tuple[int, int]:
        """有上次保存的尺寸就沿用（放不下时按比例缩到工作区内），否则按工作区的 START_FILL 计算。"""
        saved = get_window_size()
        if saved is not None:
            base_w, base_h, fill = saved[0], saved[1], 1.0
        else:
            base_w, base_h, fill = BASE_W, BASE_H, START_FILL
        shrink = min(1.0, avail.width() * fill / base_w, avail.height() * fill / base_h)
        return (max(self.minimumWidth(), round(base_w * shrink)),
                max(self.minimumHeight(), round(base_h * shrink)))

    def _startup_position(self, width: int, height: int) -> QPoint:
        """优先用上次关闭时保存的坐标（校验仍落在当前显示器布局内），否则屏幕居中。"""
        virtual = QRect()
        for screen in QGuiApplication.screens():
            virtual = virtual.united(screen.geometry())
        dpr = QGuiApplication.primaryScreen().devicePixelRatio()
        saved = get_window_position()  # 物理像素
        if saved is not None:
            x, y = round(saved[0] / dpr), round(saved[1] / dpr)
            if (virtual.left() - width + MIN_VISIBLE <= x <= virtual.right() - MIN_VISIBLE
                    and virtual.top() <= y <= virtual.bottom() - MIN_VISIBLE):
                # 启动时把整窗放进所在显示器工作区，避免上次位置配新尺寸后半截落在屏幕外
                screen = QGuiApplication.screenAt(QPoint(x + MIN_VISIBLE, y)) or QGuiApplication.primaryScreen()
                area = screen.availableGeometry()
                x = max(area.left(), min(x, area.right() - width + 1))
                y = max(area.top(), min(y, area.bottom() - height + 1))
                return QPoint(x, y)
        avail = QGuiApplication.primaryScreen().availableGeometry()
        return QPoint(avail.left() + max(0, (avail.width() - width) // 2),
                      avail.top() + max(0, (avail.height() - height) // 2))

    def toggle_maximize(self) -> None:
        """"伪最大化"：在保持 16:9 的前提下放到当前显示器工作区能放下的最大尺寸并居中，再点还原。"""
        if self._restore_geometry is not None:
            self.setGeometry(self._restore_geometry)
            self._restore_geometry = None
            self._ornaments.refresh()  # 移动事件时仍算最大化，还原后补摆一次
            return
        self._restore_geometry = self.geometry()
        avail = (self.screen() or QGuiApplication.primaryScreen()).availableGeometry()
        height = avail.height()
        width = round(height * ASPECT)
        if width > avail.width():
            width = avail.width()
            height = round(width / ASPECT)
        self.setGeometry(avail.left() + (avail.width() - width) // 2,
                         avail.top() + (avail.height() - height) // 2, width, height)

    def nativeEvent(self, event_type, message):
        if event_type == b"windows_generic_MSG":
            msg = wintypes.MSG.from_address(int(message))
            if WM_DSTCAMP_ACTIVATE and msg.message == WM_DSTCAMP_ACTIVATE:
                # wParam 是发起方版本编码：发起方更新时不弹窗口，由它结束本进程后接管；
                # 否则恢复窗口。窗口可能已 hide() 到托盘，必须走 Qt 自己的显示流程，状态才一致
                own_code = version_code(__version__)
                if msg.wParam <= own_code:
                    self.restore_from_tray()
                return True, own_code
            if msg.message == WM_SIZING:
                rect = RECT.from_address(msg.lParam)
                enforce_aspect(msg.wParam, rect)
                fit_desktop(msg.wParam, rect)
                self._ornaments.follow_native_rect(rect.left, rect.top, rect.right, rect.bottom)
                return True, 1
            if msg.message == WM_MOVING:
                rect = RECT.from_address(msg.lParam)
                keep_on_desktop(rect)
                self._ornaments.follow_native_rect(rect.left, rect.top, rect.right, rect.bottom)
                return True, 1
            blocked_click = (msg.message == WM_SETCURSOR and (msg.lParam & 0xFFFF) == HTERROR
                             and ((msg.lParam >> 16) & 0xFFFF) in _MOUSE_DOWN_MESSAGES)
            if blocked_click or msg.message in _MOUSE_DOWN_MESSAGES:
                # 有系统标题栏的弹窗由 Windows 自己闪烁；自绘标题栏的（如创建存档窗口）
                # 没有这个效果，交给它自己闪
                modal = QApplication.activeModalWidget()
                if modal is not None and modal is not self and hasattr(modal, "flash_attention"):
                    modal.flash_attention()
        return False, 0

    def _end_resize(self) -> None:
        self._resizing = False
        self.update()

    def hasHeightForWidth(self) -> bool:
        # 页内有自动换行文字（如世界页的世界描述）时布局会变成高度随宽度变化，Qt 在 Windows 上
        # 会把顶层窗口高度强制抬到 heightForWidth(宽度)（按各控件理想高度累加，远大于最小高度），
        # 拖窄时高度停住不缩、16:9 被打破。窗口尺寸只由最小尺寸和 16:9 约束，页内换行照常。
        return False

    def _fit_chrome_to_height(self) -> None:
        """窗口矮时收窄各页签共用的固定区域（存档栏留白、页面上下外边距），把高度让给正文。

        只按窗口高度判断，收窄本身不改变窗口高度，不会来回切换。"""
        pages = getattr(self, "pages", None)
        if not pages:  # _place_initially() 在界面装配前就会触发尺寸事件
            return
        compact = self.height() < COMPACT_CHROME_H
        if compact == self._compact_chrome:
            return
        self._compact_chrome = compact
        self.cluster_bar.set_compact(compact)
        gap = COMPACT_PAGE_MARGIN_V if compact else PAGE_MARGIN_V
        for page in pages.values():
            margins = page.layout().contentsMargins()
            page.layout().setContentsMargins(margins.left(), gap, margins.right(), gap)

    def resizeEvent(self, _event):
        self._fit_chrome_to_height()
        self._resizing = True
        self._settle.start()
        w, h, g, c = self.width(), self.height(), 6, 12
        rects = [
            (0, c, g, h - 2 * c), (w - g, c, g, h - 2 * c), (c, 0, w - 2 * c, g), (c, h - g, w - 2 * c, g),
            (0, 0, c, c), (w - c, h - c, c, c), (w - c, 0, c, c), (0, h - c, c, c),
        ]
        for grip, rect in zip(self._grips, rects):
            grip.setGeometry(*rect)
            grip.raise_()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), theme.color("BG_SOFT"))
        self.background.paint(painter, self.width(), self.height(), smooth=not self._resizing)
        painter.setPen(QPen(theme.color("CARD_BORDER"), 2))
        painter.drawRect(self.rect().adjusted(1, 1, -1, -1))
        if self._ornaments.frame_accent_visible():
            paint_frame_accent(painter, QRectF(self.rect().adjusted(1, 1, -1, -1)))

    # ── 托盘与退出 ──────────────────────────────────────────────────────
    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        self.tray.setToolTip(t("app.title"))
        menu = QMenu(self)
        self._tray_show = QAction(t("tray.show"), self)
        self._tray_show.triggered.connect(self.restore_from_tray)
        self._tray_exit = QAction(t("tray.exit"), self)
        self._tray_exit.triggered.connect(self._confirm_and_quit)
        menu.addAction(self._tray_show)
        menu.addAction(self._tray_exit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: self.restore_from_tray() if reason == QSystemTrayIcon.ActivationReason.Trigger else None
        )
        self._tray_menu = menu
        self.tray.show()

    def restore_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def request_close(self) -> None:
        """标题栏关闭按钮：按设置最小化到托盘或直接退出。"""
        if get_minimize_on_close():
            self.hide()
        else:
            self._confirm_and_quit()

    def _confirm_and_quit(self) -> None:
        """还有本地专服在跑时先问一句是否一并关闭；选"否"就是取消退出，不强行杀掉。"""
        if not self._confirm_unsaved():
            return
        running = self.ctx.manager.running()
        if not running:
            self.quit_app()
            return
        world_count = len(running)
        cluster_count = len({str(proc.cluster_path) for proc in running})
        if not dialogs.ask_yes_no(self, t("local.confirm_close_title"),
                                   t("local.confirm_close_msg", cluster_count=cluster_count, world_count=world_count)):
            return
        self.ctx.manager.stop_all(on_all_done=lambda: post_to_ui(lambda _a: self.quit_app()))

    def restart_app(self) -> None:
        """重启 DSTCamp：有专服在跑时先确认并安全关闭，再启动辅助进程（run_gui.py --restart-helper），
        等本进程退出、单实例锁释放后重新启动。"""
        if not self._confirm_unsaved():
            return
        running = self.ctx.manager.running()
        if running:
            world_count = len(running)
            cluster_count = len({str(proc.cluster_path) for proc in running})
            if not dialogs.ask_yes_no(self, t("local.confirm_close_title"),
                                       t("local.confirm_close_msg", cluster_count=cluster_count,
                                         world_count=world_count)):
                return
            self.ctx.manager.stop_all(on_all_done=lambda: post_to_ui(lambda _a: self._spawn_restart_and_quit()))
            return
        self._spawn_restart_and_quit()

    def _spawn_restart_and_quit(self) -> None:
        import subprocess
        import sys
        from pathlib import Path

        original_args = sys.argv[1:]
        if getattr(sys, "frozen", False):
            launcher = [sys.executable]
        else:
            launcher = [sys.executable, str(Path(__file__).resolve().parents[2] / "scripts" / "run_gui.py")]
        env = os.environ.copy()
        if getattr(sys, "frozen", False):
            # 辅助进程比当前进程活得久，不能复用当前单文件 EXE 的解压目录（_MEI），
            # 否则当前进程退出清理时会删掉辅助进程还在用的文件。
            env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        try:
            subprocess.Popen([*launcher, "--restart-helper", str(os.getpid()), *original_args], env=env,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as exc:
            dialogs.show_error(self, t("settings.cache_dir_label"), t("settings.restart_failed", error=str(exc)))
            return
        self.quit_app()

    def quit_app(self) -> None:
        self._quitting = True
        dpr = self.screen().devicePixelRatio() if self.screen() else 1.0
        set_window_position(round(self.x() * dpr), round(self.y() * dpr))
        # "伪最大化"状态下记还原前的尺寸，免得下次一启动就是铺满的
        size = (self._restore_geometry or self.geometry()).size()
        set_window_size(size.width(), size.height())
        self.tray.hide()
        QApplication.quit()

    def closeEvent(self, event):
        if not self._quitting:
            event.ignore()
            self.request_close()
            return
        event.accept()
