"""主题窗口点缀：伸出主窗口外框的小图，每张由一个跟随主窗口的透明小窗承载；另在外框上画一道渐隐描边。

素材在 icons/ornaments/（来源见 *_SOURCES.txt），按 3 倍屏预渲染；坐标由 reference/dev_scripts/ornaments/ 的排版脚本输出。
主窗口不开 WA_TranslucentBackground（会拖慢绘制和缩放），只有小窗透明。
"""

import ctypes
import math
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication, QImage, QImageReader, QLinearGradient, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QWidget

from dstools.qt.theme import theme
from dstools.shared.resource_paths import bundled_resource_dir

_ASSET_DIR = bundled_resource_dir() / "icons" / "ornaments"


@dataclass(frozen=True)
class Sprite:
    image: str  # icons/ornaments/ 下的文件名
    fx: float  # 锚点在主窗口上的相对位置：0 左/上缘，1 右/下缘
    fy: float
    x: float  # 图片中心相对锚点的偏移（逻辑像素）
    y: float
    height: float  # 显示高度（逻辑像素），宽度按原图比例


# 构图：左上主角，沿上边、左边各一串渐稀；右上呼应，右下收尾，左下留白。
# 同一主题里后面的小窗叠在前面的上面。标题栏高 32，主角可以压住图标和标题开头，不挡菜单和按钮。
_SPRITES: dict[str, tuple[Sprite, ...]] = {
    "campfire": (
        Sprite("campfire_spray_right.png", 0, 0, 310.5, -16.5, 63),  # 上边秋叶枝
        Sprite("campfire_spray_down.png", 0, 0, -10.5, 242, 500),  # 左边秋叶枝
        Sprite("campfire_cluster.png", 0, 0, 8, 1.5, 93),  # 左上大叶簇
        Sprite("campfire_spray_corner.png", 1, 0, -155.5, 91.5, 259),  # 右上枫叶
        Sprite("campfire_fire.png", 1, 1, -151.5, -155, 328),  # 右下篝火与火星
    ),
    "mint": (
        Sprite("mint_ivy_strand.png", 0, 0, -45.5, 138, 300),  # 左边常春藤，压在最下层
        Sprite("mint_spray_right.png", 0, 0, 228, -26, 70),  # 上边草丛
        Sprite("mint_spray_down.png", 0, 0, -16.5, 130.5, 273),  # 左边草丛
        Sprite("mint_cluster.png", 0, 0, 2, -21.5, 101),  # 左上鸟窝与绿绣眼
        Sprite("mint_spray_corner.png", 1, 0, -68.5, 65, 256),  # 右上蕨叶与常春藤
        Sprite("mint_spray_bottom.png", 1, 1, -196.5, -50.5, 101),  # 右下小草
    ),
    "sakura": (
        Sprite("sakura_spray_right.png", 0, 0, 282.5, -14.5, 63),  # 上边花串
        Sprite("sakura_spray_down.png", 0, 0, -18, 218, 458),  # 左边花串
        Sprite("sakura_cluster.png", 0, 0, 12, -2, 92),  # 左上花团
        Sprite("sakura_spray_corner.png", 1, 0, -228.5, 132, 322),  # 右上大花与花串
        Sprite("sakura_spray_bottom.png", 1, 1, -180.5, -84.5, 185),  # 右下落花
    ),
    "sky": (
        Sprite("sky_spray_right.png", 0, 0, 259, -31.5, 63),  # 上边积云与雁阵
        Sprite("sky_spray_down.png", 0, 0, -35.5, 250, 312),  # 左边积云
        Sprite("sky_cluster.png", 0, 0, 23.5, 51, 270),  # 左上大积云与雨帘
        Sprite("sky_spray_corner.png", 1, 0, -123, 118, 372),  # 右上风筝与云
        Sprite("sky_bottom.png", 1, 1, -140.5, -73, 190),  # 右下云带与飞鸟
    ),
    "twilight": (
        Sprite("twilight_spray_right.png", 0, 0, 396, -30, 84),  # 上边银河与北斗
        Sprite("twilight_spray_down.png", 0, 0, -29, 318.5, 533),  # 左边银河
        Sprite("twilight_cluster.png", 0, 0, -6, -24.5, 219),  # 左上月亮与夜云
        Sprite("twilight_spray_corner.png", 1, 0, -36, 14.5, 185),  # 右上流星
        Sprite("twilight_bottom.png", 1, 1, -116, -116, 242),  # 右下松林与帐篷
    ),
}

_image_sizes: dict[str, tuple[int, int]] = {}
# (文件名, 缩放比) → 缩到物理像素的图；只留当前主题，切换主题或关闭点缀时清空，原图解码后即释放
_pixmaps: dict[tuple[str, float], QPixmap] = {}


def _display_size(sprite: Sprite) -> tuple[float, float]:
    """显示宽高（逻辑像素）；只读图片头，不解码。"""
    if sprite.image not in _image_sizes:
        size = QImageReader(str(_ASSET_DIR / sprite.image)).size()
        _image_sizes[sprite.image] = (size.width(), size.height())
    width, height = _image_sizes[sprite.image]
    return sprite.height * width / max(1, height), sprite.height


def _pixmap(sprite: Sprite, dpr: float) -> QPixmap:
    key = (sprite.image, dpr)
    if key not in _pixmaps:
        w, h = _display_size(sprite)
        image = QImage(str(_ASSET_DIR / sprite.image)).scaled(
            round(w * dpr), round(h * dpr), Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        pixmap = QPixmap.fromImage(image)
        pixmap.setDevicePixelRatio(dpr)
        _pixmaps[key] = pixmap
    return _pixmaps[key]


def paint_frame_accent(painter: QPainter, rect: QRectF) -> None:
    """外框主题色描边：从左上角最浓沿对角线渐隐。"""
    strong = QColor(theme.color("ORNAMENT_ACCENT"))
    strong.setAlpha(220)
    middle = QColor(theme.color("ORNAMENT"))
    middle.setAlpha(170)
    faded = QColor(theme.color("ORNAMENT"))
    faded.setAlpha(0)
    gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
    gradient.setColorAt(0.0, strong)
    gradient.setColorAt(0.35, middle)
    gradient.setColorAt(0.75, faded)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    painter.setPen(QPen(gradient, 2))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRect(rect)
    painter.restore()


# ── 批量挪窗 ─────────────────────────────────────────────────────────
_user32 = ctypes.windll.user32
_user32.BeginDeferWindowPos.argtypes = [ctypes.c_int]
_user32.BeginDeferWindowPos.restype = wintypes.HANDLE
_user32.DeferWindowPos.argtypes = [wintypes.HANDLE, wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_uint]
_user32.DeferWindowPos.restype = wintypes.HANDLE
_user32.EndDeferWindowPos.argtypes = [wintypes.HANDLE]
_user32.EndDeferWindowPos.restype = wintypes.BOOL
_SWP_MOVE_ONLY = 0x0001 | 0x0004 | 0x0010 | 0x0200  # NOSIZE | NOZORDER | NOACTIVATE | NOOWNERZORDER


def _to_native(pos: QPoint, widget: QWidget) -> tuple[int, int]:
    """逻辑坐标转物理像素，与 Qt 一致：按所在屏幕左上角为原点缩放，四舍五入同 qRound。"""
    screen = QGuiApplication.screenAt(pos) or widget.screen()
    dpr = screen.devicePixelRatio()
    origin = screen.geometry().topLeft()
    return (math.floor(origin.x() + (pos.x() - origin.x()) * dpr + 0.5),
            math.floor(origin.y() + (pos.y() - origin.y()) * dpr + 0.5))


def _move_together(moves: list[tuple[QWidget, QPoint]]) -> None:
    """一次挪动多个小窗。DeferWindowPos 比逐个 move 快约 3 倍，且所有小窗同时到位；Qt 会从窗口消息同步位置。"""
    if len(moves) > 1:
        hdwp = _user32.BeginDeferWindowPos(len(moves))
        for widget, pos in moves:
            if not hdwp:
                break
            x, y = _to_native(pos, widget)
            hdwp = _user32.DeferWindowPos(hdwp, int(widget.winId()), None, x, y, 0, 0, _SWP_MOVE_ONLY)
        if hdwp and _user32.EndDeferWindowPos(hdwp):
            return
    for widget, pos in moves:
        widget.move(pos)


class _SpriteWindow(QWidget):
    """承载一张精灵的透明小窗：Tool 窗口归主窗口所有，叠在主窗口上，不进任务栏，鼠标穿透。"""

    def __init__(self, owner: QWidget, on_painted: Callable[[], None]):
        super().__init__(owner, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowTransparentForInput | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.sprite: Sprite | None = None
        self.painted = False  # 本次显示后是否已画过一帧
        self._on_painted = on_painted

    def paintEvent(self, _event) -> None:
        if self.sprite is not None:
            painter = QPainter(self)
            painter.drawPixmap(0, 0, _pixmap(self.sprite, self.devicePixelRatioF()))
        if not self.painted:
            self.painted = True
            QTimer.singleShot(0, self._on_painted)  # 下一轮这一帧已推到屏幕上


class WindowOrnaments(QObject):
    """管理点缀小窗：跟随主窗口移动、缩放；主窗口隐藏、最小化或伪最大化时隐藏。
    关闭点缀或换到没有点缀的主题时销毁小窗并清空图片缓存。"""

    def __init__(self, window: QWidget, is_maximized: Callable[[], bool], enabled: bool):
        super().__init__(window)
        self._window = window
        self._is_maximized = is_maximized
        self._enabled = enabled
        self._windows: list[_SpriteWindow] = []
        # 主窗口从隐藏变为显示时，小窗先以全透明显示并画好，等主窗口和小窗都画完一帧再改为不透明。
        # 透明小窗改透明度不用重画，与主窗口几乎同帧出现；直接显示则小窗画得快，会先于主窗口出现
        self._awaiting_paint = False
        self._main_painted = False
        self._reveal_fallback = QTimer(self, singleShot=True, interval=500)
        self._reveal_fallback.timeout.connect(lambda: self._reveal(force=True))
        if enabled:
            window.installEventFilter(self)  # 关闭时不装，主窗口事件不经过这里
        theme.changed.connect(self._on_theme_changed)

    def frame_accent_visible(self) -> bool:
        """是否画外框描边（画在窗口内部，伪最大化时也保留）。"""
        return self._enabled and theme.name in _SPRITES

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        if enabled:
            self._window.installEventFilter(self)
        else:
            self._window.removeEventFilter(self)
            _pixmaps.clear()
        self.refresh()
        self._window.update()

    def follow_native_rect(self, left: int, top: int, right: int, bottom: int) -> None:
        """按 WM_MOVING / WM_SIZING 给出的新矩形（物理像素）提前摆好小窗，和主窗口同帧到位，不闪缝。"""
        if not self._windows:
            return
        screen = self._window.screen()
        if screen is None:
            return
        # 与 Qt 一致：以屏幕左上角为原点缩放
        dpr = screen.devicePixelRatio()
        origin = screen.geometry().topLeft()
        self.refresh(QRect(round(origin.x() + (left - origin.x()) / dpr), round(origin.y() + (top - origin.y()) / dpr),
                           round((right - left) / dpr), round((bottom - top) / dpr)))

    def refresh(self, geometry: QRect | None = None) -> None:
        """按主窗口位置（缺省取当前位置）和状态摆放、隐藏或销毁小窗。"""
        sprites = _SPRITES.get(theme.name, ()) if self._enabled else ()
        while len(self._windows) > len(sprites):
            sprite_window = self._windows.pop()
            sprite_window.hide()
            sprite_window.deleteLater()
        if not sprites:
            return
        window = self._window
        if not window.isVisible() or window.isMinimized() or self._is_maximized():
            for sprite_window in self._windows:
                sprite_window.hide()
            return
        if geometry is None:
            # 拖上边框缩放时，控件 geometry 在移动、缩放两个事件之间是"新位置 + 旧尺寸"，原生窗口的是同步更新的
            handle = window.windowHandle()
            geometry = handle.geometry() if handle is not None else window.geometry()
        while len(self._windows) < len(sprites):
            self._windows.append(_SpriteWindow(window, self._reveal))
        opacity = 0.0 if self._awaiting_paint else 1.0
        moves = []
        for sprite_window, sprite in zip(self._windows, sprites):
            w, h = _display_size(sprite)
            target = QRect(round(geometry.x() + geometry.width() * sprite.fx + sprite.x - w / 2),
                           round(geometry.y() + geometry.height() * sprite.fy + sprite.y - h / 2),
                           math.ceil(w), math.ceil(h))
            if sprite_window.sprite is not sprite:
                sprite_window.sprite = sprite
                sprite_window.update()
            if sprite_window.windowOpacity() != opacity:
                sprite_window.setWindowOpacity(opacity)
            current = sprite_window.geometry()
            if not sprite_window.isVisible():
                sprite_window.painted = False
            if not sprite_window.isVisible() or current.size() != target.size():
                sprite_window.setGeometry(target)
                sprite_window.show()
            elif current.topLeft() != target.topLeft():
                moves.append((sprite_window, target.topLeft()))
        _move_together(moves)

    def _on_theme_changed(self) -> None:
        _pixmaps.clear()
        self.refresh()

    def _reveal(self, force: bool = False) -> None:
        """主窗口与所有小窗都画完一帧（或兜底超时）后，把小窗改为不透明。"""
        if not self._awaiting_paint:
            return
        if not force and not (self._main_painted and all(w.painted for w in self._windows if w.isVisible())):
            return
        self._reveal_fallback.stop()
        self._awaiting_paint = False
        self.refresh()

    def eventFilter(self, obj, event) -> bool:
        if obj is not self._window:
            return False
        kind = event.type()
        if kind == QEvent.Type.Show:
            self._awaiting_paint = True
            self._main_painted = False
            self._reveal_fallback.start()  # 万一收不到绘制事件，也不至于一直透明
        elif kind == QEvent.Type.Paint:
            if self._awaiting_paint and not self._main_painted:
                # 绘制事件在主窗口画之前到达，排到事件循环下一轮，等这一帧推到屏幕上再判断
                self._main_painted = True
                QTimer.singleShot(0, self._reveal)
            return False
        elif kind == QEvent.Type.Hide:
            self._awaiting_paint = False
            self._reveal_fallback.stop()
        if kind in (
            QEvent.Type.Move, QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.Hide,
            QEvent.Type.WindowStateChange,
        ):
            self.refresh()
        return False
