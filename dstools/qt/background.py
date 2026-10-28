"""自定义背景图：主窗口 paintEvent 整窗只画一次（底色 + 居中裁剪填满的图片按不透明度叠加），子控件透明透出。

缩放过程中用快速变换，停顿后再用平滑变换重画。
"""

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPixmap

from dstools.qt.theme import theme

from dstools.shared.custom_background import get_custom_bg_path
from dstools.shared.app_settings import get_custom_bg_opacity, get_lite_mode_enabled

_MAX_WIDTH = 2560  # 超大图只在加载时缩小一次，绘制时不再处理
_THUMB_WIDTH = 192  # 求图片平均色用的缩略图宽度


class Background:
    def __init__(self):
        self.pixmap: QPixmap | None = None
        self._mean: QColor | None = None
        self._scaled: QPixmap | None = None  # 按窗口物理像素平滑缩放好的图，重绘只需贴图
        self.opacity = 0.0
        self.reload()

    def reload(self) -> None:
        path = get_custom_bg_path()
        self.opacity = float(get_custom_bg_opacity())
        self.pixmap = self._mean = self._scaled = None
        # 轻量模式不加载背景图：整窗贴图和文字描边在远程桌面、无显卡的云服务器上重绘很慢，大图也常驻内存
        if path is None or get_lite_mode_enabled():
            return
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            return
        if pixmap.width() > _MAX_WIDTH:
            pixmap = pixmap.scaledToWidth(_MAX_WIDTH, Qt.TransformationMode.SmoothTransformation)
        self.pixmap = pixmap
        thumb = pixmap.toImage().scaledToWidth(_THUMB_WIDTH, Qt.TransformationMode.SmoothTransformation)
        self._mean = thumb.scaled(1, 1, Qt.AspectRatioMode.IgnoreAspectRatio,
                                  Qt.TransformationMode.SmoothTransformation).pixelColor(0, 0)

    @property
    def active(self) -> bool:
        return self.pixmap is not None and self.opacity > 0

    def backdrop_color(self) -> QColor:
        """整窗实际显示出来的背景平均色：图片平均色按不透明度叠在底色上。整窗统一判断文字明暗用。"""
        bg = theme.color("BG_SOFT")
        if not self.active or self._mean is None:
            return bg
        mean = self._mean
        a = self.opacity * mean.alphaF()
        return QColor.fromRgbF(*(b * (1 - a) + m * a for b, m in zip(
            (bg.redF(), bg.greenF(), bg.blueF()), (mean.redF(), mean.greenF(), mean.blueF()))))

    def paint(self, painter: QPainter, width: int, height: int, smooth: bool) -> None:
        if not self.active:
            return
        sw, sh = self.pixmap.width(), self.pixmap.height()
        scale = max(width / sw, height / sh)
        cw, ch = width / scale, height / scale
        source = QRectF((sw - cw) / 2, (sh - ch) / 2, cw, ch)
        painter.save()
        painter.setOpacity(self.opacity)
        if smooth:
            # 平滑缩放只在窗口尺寸变了时做一次，拖不透明度滑块等整窗重绘只贴图
            ratio = painter.device().devicePixelRatioF()
            pw, ph = round(width * ratio), round(height * ratio)
            if self._scaled is None or (self._scaled.width(), self._scaled.height()) != (pw, ph):
                self._scaled = self.pixmap.copy(source.toAlignedRect()).scaled(
                    pw, ph, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
                self._scaled.setDevicePixelRatio(ratio)
            painter.drawPixmap(0, 0, self._scaled)
        else:
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
            painter.drawPixmap(QRectF(0, 0, width, height), self.pixmap, source)
        painter.restore()
