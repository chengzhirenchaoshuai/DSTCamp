"""通用自绘控件：圆角开关、半透明圆角卡片、胶囊页签条、缩放热区（颜色在 paintEvent 里现查）。"""

from PySide6.QtCore import QEvent, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QFrame, QLabel, QMenu, QScrollArea, QVBoxLayout, QWidget

from dstools.qt.text_halo import draw_adaptive_text
from dstools.qt.theme import theme


class ToggleSwitch(QWidget):
    """绑定布尔值的圆角开关。``toggled(bool)`` 只在用户点击时发出。"""

    toggled = Signal(bool)

    def __init__(self, checked: bool = False, enabled: bool = True, parent=None):
        super().__init__(parent)
        self._checked = checked
        self.setFixedSize(44, 22)
        self.setEnabled(enabled)
        self.setCursor(Qt.CursorShape.PointingHandCursor if enabled else Qt.CursorShape.ArrowCursor)

    def isChecked(self) -> bool:
        return self._checked

    def setChecked(self, checked: bool) -> None:
        if checked != self._checked:
            self._checked = checked
            self.update()

    def mousePressEvent(self, event):
        if self.isEnabled() and event.button() == Qt.MouseButton.LeftButton:
            self._checked = not self._checked
            self.update()
            self.toggled.emit(self._checked)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        if self.isEnabled():
            color = theme.color("PRIMARY") if self._checked else QColor("#bdbdbd")
        else:
            color = QColor("#a5d6a7") if self._checked else QColor("#e0e0e0")
        painter.setBrush(color)
        painter.drawRoundedRect(QRectF(0, 0, 44, 22), 11, 11)
        painter.setBrush(theme.color("CARD_BG"))
        cx = 33 if self._checked else 11
        painter.drawEllipse(QRectF(cx - 8, 3, 16, 16))


class Card(QWidget):
    """圆角半透明卡片：透出下面的背景图，只画描边和淡淡的底色。"""

    def __init__(self, parent=None, radius: int | None = None, alpha: int = 150,
                 fill_key: str = "CARD_BG", border_key: str = "CARD_BORDER", border: bool | None = None):
        super().__init__(parent)
        self._radius = radius
        self._alpha = alpha
        self._fill_key = fill_key
        self._border_key = border_key
        # None：边框随底色显隐；True/False 让边框独立于底色（alpha=0 + True 即"只有描边、内部透明"）
        self._border = border

    def set_fill_key(self, fill_key: str) -> None:
        if fill_key != self._fill_key:
            self._fill_key = fill_key
            self.update()

    def paintEvent(self, _event):
        show_border = self._border if self._border is not None else (self._alpha > 0)
        if self._alpha <= 0 and not show_border:
            return  # 全透明：连描边一起不画，等于没有这张卡片
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._alpha > 0:
            fill = theme.color(self._fill_key)
            fill.setAlpha(self._alpha)
            painter.setBrush(fill)
        else:
            painter.setBrush(Qt.BrushStyle.NoBrush)
        if show_border:
            painter.setPen(QPen(theme.color(self._border_key), 1))
        else:
            painter.setPen(Qt.PenStyle.NoPen)
        radius = self._radius if self._radius is not None else 22
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)


def clear_layout(layout, keep_last: int = 0) -> None:
    """移除并销毁布局里的控件，保留末尾 keep_last 项（如撑底的弹簧）。

    坑：只 deleteLater() 时旧控件要等回到事件循环才消失，期间会和新建的行重叠、占着尺寸；
    必须先 hide() + setParent(None) 同步摘除。"""
    while layout.count() > keep_last:
        widget = layout.takeAt(0).widget()
        if widget is not None:
            widget.hide()
            widget.setParent(None)
            widget.deleteLater()


def section_card(title: str | None = None) -> tuple[Card, QVBoxLayout]:
    """带可选标题的内容分区卡片，样式跟存档信息页"玩家概览"卡片一致；
    返回 (卡片, 卡片内的纵向布局)，调用方往布局里继续加内容。"""
    card = Card(radius=14, alpha=128, fill_key="CARD_BG_ALT")
    layout = QVBoxLayout(card)
    layout.setContentsMargins(16, 12, 16, 14)
    layout.setSpacing(6)
    if title:
        heading = QLabel(title)
        heading.setProperty("heading", True)
        heading.setFont(theme.font("FONT_SIZE_BASE", bold=True))
        layout.addWidget(heading)
    return card, layout


def mod_format_tag_colors(fmt: str) -> tuple[QColor, QColor]:
    """V1/V2 小标签的（底色, 文字色）。V1 用很浅的琥珀色提醒，V2 用主题浅色再冲淡一半。"""
    if fmt == "V1":
        return QColor("#FBF3DC"), QColor("#8d6e00")
    light = theme.color("PRIMARY_LIGHT")
    background = QColor((light.red() + 255) // 2, (light.green() + 255) // 2, (light.blue() + 255) // 2)
    return background, theme.color("TEXT")


class AutoHideLabel(QLabel):
    """文字为空时自动隐藏的标签：平时为空的错误提示不再白占一行高度。"""

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self.setVisible(bool(text))

    def setText(self, text: str) -> None:
        super().setText(text)
        self.setVisible(bool(text))


class PillTabBar(QWidget):
    """顶层胶囊页签条。``current_changed(index)`` 只在用户切换时发出。"""

    current_changed = Signal(int)

    def __init__(self, labels: list[str], parent=None, height: int = 44, pill_height: int = 34,
                 font_size_key: str | None = None, gap: int = 6, pad: int | None = None,
                 uniform_width: bool = False, bold: bool = False):
        super().__init__(parent)
        self._labels = list(labels)
        self._bold = bold  # 主页签文字加粗，子页签保持常规字重
        self._index = 0
        self._hover = -1
        self._pill_h = pill_height
        self._font_size_key = font_size_key  # None 表示用应用默认字体；子页签条传较小的字号键
        self._gap = gap  # 页签之间的间距
        # 页签文字左右内边距：未选中页签只画文字，视觉间隔主要来自这里而不是 gap
        self._pad = pad if pad is not None else (44 if font_size_key is None else 32)
        # 统一用最宽页签的宽度，避免字数不同时大小不一
        self._uniform_width = uniform_width
        self.setFixedHeight(height)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_labels(self, labels: list[str]) -> None:
        self._labels = list(labels)
        self.updateGeometry()  # 文字变长（如"待更新（83）"）时通知布局按新的 sizeHint 重排，否则会被右侧控件压住
        self.update()

    def current_index(self) -> int:
        return self._index

    def set_current_index(self, index: int) -> None:
        if index != self._index:
            self._index = index
            self.update()

    def _rects(self) -> list[QRectF]:
        # 从左边距起左对齐绘制（不居中），窗口缩放时页签位置不动
        metrics = QFontMetrics(self._font())
        widths = [metrics.horizontalAdvance(text) + self._pad for text in self._labels]
        if self._uniform_width and widths:
            widths = [max(widths)] * len(widths)
        x = 24.0
        top = (self.height() - self._pill_h) / 2
        rects = []
        for width in widths:
            rects.append(QRectF(x, top, width, self._pill_h))
            x += width + self._gap
        return rects

    def _font(self):
        if self._font_size_key:
            return theme.font(self._font_size_key, bold=self._bold)
        font = self.font()
        font.setBold(self._bold)
        return font

    def sizeHint(self) -> QSize:
        # 坑：放进 QHBoxLayout 并与 addStretch() 抢空间时会被挤成 0 宽，必须提供尺寸提示
        rects = self._rects()
        width = int(rects[-1].right()) + 24 if rects else 0
        return QSize(width, self.height())

    def mouseMoveEvent(self, event):
        hover = next((i for i, r in enumerate(self._rects()) if r.contains(event.position())), -1)
        if hover != self._hover:
            self._hover = hover
            self.update()

    def leaveEvent(self, _event):
        self._hover = -1
        self.update()

    def mousePressEvent(self, event):
        for i, rect in enumerate(self._rects()):
            if rect.contains(event.position()) and i != self._index:
                self._index = i
                self.update()
                self.current_changed.emit(i)
                return

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setFont(self._font())
        for i, rect in enumerate(self._rects()):
            selected = i == self._index
            if selected or i == self._hover:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(theme.color("PRIMARY") if selected else theme.color("PRIMARY_LIGHT"))
                painter.drawRoundedRect(rect, self._pill_h / 2, self._pill_h / 2)
            if selected:
                painter.setPen(QColor("#FFFFFF"))
                painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._labels[i])
            else:  # 未选中页签的文字直接压在背景上，自定义背景图时随底下明暗调整
                draw_adaptive_text(painter, self, rect, Qt.AlignmentFlag.AlignCenter, self._labels[i],
                                   theme.color("TEXT_MUTED"))


class Grip(QWidget):
    """窗口边/角的缩放热区：交给系统原生缩放（startSystemResize），不自己算几何。"""

    def __init__(self, parent, edges, cursor):
        super().__init__(parent)
        self._edges = edges
        self.setCursor(cursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.window().windowHandle().startSystemResize(self._edges)


class Banner(QWidget):
    """醒目的提示条（本地存档只读、没有存档等）。文字为空时隐藏；文字自动折行，
    高度跟着当前宽度和内容重新计算——放在窄的侧栏里的长提示文字也不会被裁掉。"""

    _MIN_HEIGHT = 34
    _PAD_X, _PAD_Y = 14, 8
    clicked = Signal()  # 仅 set_clickable(True) 后发出

    def __init__(self, parent=None):
        super().__init__(parent)
        self._text = ""
        self._clickable = False
        self.setVisible(False)
        self.setFixedHeight(self._MIN_HEIGHT)

    def set_clickable(self, clickable: bool) -> None:
        self._clickable = clickable
        self.setCursor(Qt.CursorShape.PointingHandCursor if clickable else Qt.CursorShape.ArrowCursor)

    def mouseReleaseEvent(self, event):
        if (self._clickable and event.button() == Qt.MouseButton.LeftButton
                and self.rect().contains(event.position().toPoint())):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def text(self) -> str:
        return self._text

    def set_text(self, text: str) -> None:
        self._text = text
        # 还没放进布局时不能 setVisible(True)：无父控件会成为顶层窗口在屏幕中间闪一下，等 ParentChange 再决定显隐
        if self.parentWidget() is not None:
            self.setVisible(bool(text))
        self._relayout()
        self.update()

    def event(self, event):
        if event.type() == QEvent.Type.ParentChange and self.parentWidget() is not None:
            self.setVisible(bool(self._text))
        return super().event(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._relayout()

    def _relayout(self) -> None:
        if not self._text or self.width() <= 2 * self._PAD_X:
            self.setFixedHeight(self._MIN_HEIGHT)
            return
        metrics = QFontMetrics(theme.font("FONT_SIZE_SM", bold=True))
        bounds = metrics.boundingRect(
            QRect(0, 0, self.width() - 2 * self._PAD_X, 0),
            Qt.TextFlag.TextWordWrap | int(Qt.AlignmentFlag.AlignLeft), self._text)
        self.setFixedHeight(max(self._MIN_HEIGHT, bounds.height() + 2 * self._PAD_Y))

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme.color("BANNER_BG"))
        painter.drawRoundedRect(QRectF(self.rect()), 8, 8)
        painter.setFont(theme.font("FONT_SIZE_SM", bold=True))
        painter.setPen(theme.color("BANNER_TEXT"))
        rect = self.rect().adjusted(self._PAD_X, self._PAD_Y, -self._PAD_X, -self._PAD_Y)
        painter.drawText(rect, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter) | int(Qt.TextFlag.TextWordWrap),
                          self._text)


class CachedRowList:
    """长列表的行控件管理：按 key 缓存复用，签名不变就不重建（搜索、筛选、全选只调整显隐和顺序）；
    新建的行先同步建第一屏，其余在事件循环空闲时分批补齐，不卡住界面。

    签名用 == 比较：任何会改变行内容的数据都要放进签名；勾选、底色等可原地改的状态交给 place。
    """

    def __init__(self, layout: QVBoxLayout, *, row_height: int | None = None, first_batch: int = 12, batch: int = 30):
        self._layout = layout
        self._row_height = row_height
        self._first_batch = first_batch
        self._batch = batch
        self._cache: dict[str, tuple[object, QWidget]] = {}
        self._generation = 0

    def render(self, keys, *, signature, build, place, empty=None) -> None:
        """按 keys 顺序显示各行。signature(key) 给出行内容签名，build(key) 新建行，
        place(widget, key, index) 在每次摆放时同步可原地更新的状态；没有行时显示 empty() 返回的控件。"""
        self._generation += 1
        cached = {id(widget) for _sig, widget in self._cache.values()}
        while self._layout.count():
            widget = self._layout.takeAt(0).widget()
            if widget is None:
                continue  # 弹簧
            widget.hide()
            if id(widget) not in cached:  # 空列表提示等临时控件
                widget.setParent(None)
                widget.deleteLater()
        keys = list(keys)
        if self._row_height is not None:
            # 先把容器撑到最终高度：分批补齐时滚动区要过几轮事件才放大容器，期间布局会把行压扁
            # （真机反馈：打开窗口时各行挤成一团，补齐后才恢复）；设最小高度会同步放大容器
            margins = self._layout.contentsMargins()
            self._layout.parentWidget().setMinimumHeight(
                margins.top() + margins.bottom() + len(keys) * (self._row_height + self._layout.spacing())
                if keys else 0)
        if not keys and empty is not None:
            self._layout.addWidget(empty())
        self._layout.addStretch()
        self._fill(self._generation, keys, 0, signature, build, place, self._first_batch)

    def clear(self) -> None:
        """丢弃全部缓存的行（数据整体换掉时用），下次 render 全部重建。"""
        for _sig, widget in self._cache.values():
            widget.setParent(None)
            widget.deleteLater()
        self._cache.clear()

    def _fill(self, generation, keys, start, signature, build, place, budget) -> None:
        if generation != self._generation:
            return  # 中途又重新 render 过，旧的补齐任务作废
        created = 0
        index = start
        while index < len(keys) and created < budget:
            key = keys[index]
            sig = signature(key)
            entry = self._cache.get(key)
            if entry is not None and entry[0] == sig:
                widget = entry[1]
            else:
                if entry is not None:
                    entry[1].setParent(None)
                    entry[1].deleteLater()
                widget = build(key)
                self._cache[key] = (sig, widget)
                created += 1
            place(widget, key, index)
            self._layout.insertWidget(self._layout.count() - 1, widget)  # 插在末尾弹簧之前
            widget.show()
            index += 1
        if index < len(keys):
            # 以布局所在控件为上下文：窗口关闭销毁后定时器自动作废
            QTimer.singleShot(0, self._layout.parentWidget(), lambda: self._fill(
                generation, keys, index, signature, build, place, self._batch))


class FrostedMenu(QMenu):
    """"假透明"菜单：弹出时截取主窗口对应区域压淡后画在底层，菜单半透明底色叠在上面。

    不开 WA_TranslucentBackground（Windows 上会整块发黑）；截图失败时退回实色底。"""

    def __init__(self, *args):
        super().__init__(*args)
        self._snapshot: QPixmap | None = None

    def showEvent(self, event):
        self._snapshot = None
        anchor = self.parentWidget()
        window = anchor.window() if anchor is not None else None
        if window is not None and window is not self:
            top_left = window.mapFromGlobal(self.pos())
            grab_rect = QRect(top_left, self.size()).intersected(window.rect())
            if not grab_rect.isEmpty():
                pixmap = window.grab(grab_rect)
                if not pixmap.isNull():
                    from dstools.qt.theme import _faded_popup_bg_pixmap
                    self._snapshot = (_faded_popup_bg_pixmap(pixmap), grab_rect.topLeft() - top_left)
        super().showEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), theme.color("CARD_BG"))
        if self._snapshot is not None:
            pixmap, offset = self._snapshot
            painter.drawPixmap(offset, pixmap)
        painter.end()
        super().paintEvent(event)


class CheckMenuItem(QWidget):
    """菜单里一项自绘的文字项，左侧固定一列放勾选标记。原生 QAction 的文字起点受勾选列、
    QSS 内边距共同影响，跟自绘项对不齐；同一菜单里的项都用这个类，文字起点就一致。"""

    _PAD_LEFT = 24   # 左侧勾选标记列宽，也是文字起点
    _PAD_RIGHT = 24

    def __init__(self, menu: QMenu, text: str, on_click, is_checked=None):
        super().__init__(menu)
        self._menu = menu
        self._text = text
        self._on_click = on_click
        # 现查是否打勾，不缓存选中态
        self._is_checked = is_checked or (lambda: False)
        self._hover = False
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)

    def setText(self, text: str) -> None:
        self._text = text
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:
        metrics = QFontMetrics(self._item_font())
        return QSize(metrics.horizontalAdvance(self._text) + self._PAD_LEFT + self._PAD_RIGHT,
                     metrics.height() + 12)

    def _item_font(self):
        return self.font()

    def _text_color(self) -> QColor:
        return theme.color("TEXT")

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self._menu.hide()
            self._on_click()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._hover:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(theme.color("PRIMARY_LIGHT"))
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 0, -1, 0), 4, 4)
        painter.setPen(self._text_color())
        painter.setFont(self._item_font())
        if self._is_checked():
            painter.drawText(QRect(0, 0, self._PAD_LEFT, self.height()), int(Qt.AlignmentFlag.AlignCenter), "✓")
        painter.drawText(self.rect().adjusted(self._PAD_LEFT, 0, 0, 0),
                         int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), self._text)


class ThemeMenuItem(CheckMenuItem):
    """"主题"菜单里的一项：文字用该主题自己的主色画，当前主题前面打勾加粗。
    QAction 没法单独设文字颜色，所以用自绘项。"""

    def __init__(self, menu: QMenu, name: str, text: str, on_pick):
        super().__init__(menu, text, lambda: on_pick(name), lambda: name == theme.name)
        self._name = name

    def _item_font(self):
        font = self.font()
        font.setBold(self._name == theme.name)
        return font

    def _text_color(self) -> QColor:
        from dstools.shared import palettes

        return QColor(palettes.THEMES[self._name]["PRIMARY_DARK"])


class MenuTextItem(QWidget):
    """菜单里一项可点击的纯文字（默认居中）。QSS 改不了 QMenu 菜单项的文字对齐，
    需要居中时用 QWidgetAction 包这个自绘控件。"""

    _PAD_X = 20

    def __init__(self, menu: QMenu, text: str, on_click,
                 align: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignHCenter):
        super().__init__(menu)
        self._menu = menu
        self._text = text
        self._on_click = on_click
        self._align = align
        self._hover = False
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)

    def setText(self, text: str) -> None:
        self._text = text
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:
        metrics = QFontMetrics(self.font())
        return QSize(metrics.horizontalAdvance(self._text) + 2 * self._PAD_X, metrics.height() + 12)

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self._menu.hide()
            self._on_click()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._hover:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(theme.color("PRIMARY_LIGHT"))
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 0, -1, 0), 4, 4)
        painter.setPen(theme.color("TEXT"))
        painter.setFont(self.font())
        painter.drawText(self.rect().adjusted(self._PAD_X, 0, -self._PAD_X, 0),
                         int(self._align | Qt.AlignmentFlag.AlignVCenter), self._text)


class TitleButton(QWidget):
    """标题栏的最小化/最大化/关闭按钮：QPainter 画细线图标，悬停时圆角浅色底，
    关闭按钮悬停为红底白叉；最大化状态下画"还原"图标（两个错开的方框）。"""

    clicked = Signal()

    def __init__(self, kind: str, is_maximized=None):
        super().__init__()
        self._kind = kind  # "min" / "max" / "close"
        # 返回窗口当前是否处于最大化（决定画"最大化"还是"还原"图标）；
        # 主窗口是保持 16:9 的伪最大化，由调用方自己提供判断。
        self._is_maximized = is_maximized or (lambda: False)
        self._hover = False
        self._pressed = False
        self.setFixedSize(40, 28)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = self._pressed = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._pressed:
            self._pressed = False
            self.update()
            if self.rect().contains(event.position().toPoint()):
                self.clicked.emit()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        close = self._kind == "close"
        if self._hover:
            if close:
                bg = QColor("#c62828" if self._pressed else "#e53935")
            else:
                bg = theme.color("PRIMARY_DARK" if self._pressed else "PRIMARY_LIGHT")
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(bg)
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(2, 2, -2, -2), 6, 6)
        color = QColor("white") if (close and self._hover) else theme.color("TEXT")
        pen = QPen(color, 1.3)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        cx, cy = self.width() / 2, self.height() / 2
        r = 5.0  # 图标半边长，三个图标同样大小
        if self._kind == "min":
            painter.drawLine(QPointF(cx - r, cy), QPointF(cx + r, cy))
        elif self._kind == "max":
            if self._is_maximized():
                # 还原：后面一个方框只露出上边和右边，前面一个完整方框
                painter.drawRoundedRect(QRectF(cx - r, cy - r + 2.5, 2 * r - 2.5, 2 * r - 2.5), 1.5, 1.5)
                painter.drawLine(QPointF(cx - r + 2.5, cy - r + 2.5), QPointF(cx - r + 2.5, cy - r))
                painter.drawLine(QPointF(cx - r + 2.5, cy - r), QPointF(cx + r, cy - r))
                painter.drawLine(QPointF(cx + r, cy - r), QPointF(cx + r, cy + r - 2.5))
                painter.drawLine(QPointF(cx + r, cy + r - 2.5), QPointF(cx + r - 2.5, cy + r - 2.5))
            else:
                painter.drawRoundedRect(QRectF(cx - r, cy - r, 2 * r, 2 * r), 1.5, 1.5)
        else:
            painter.drawLine(QPointF(cx - r, cy - r), QPointF(cx + r, cy + r))
            painter.drawLine(QPointF(cx + r, cy - r), QPointF(cx - r, cy + r))


class VScrollArea(QScrollArea):
    """只纵向滚动的透明滚动区：窗口矮时内容出现滚动条，而不是被压到比自身最小高度还矮、按钮和文字被挤没。

    最小宽度跟随内容并预留滚动条宽度，窗口变窄时不横向裁字。"""

    def __init__(self, inner: QWidget):
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.viewport().setAutoFillBackground(False)
        self.setWidget(inner)
        inner.setAutoFillBackground(False)  # QScrollArea.setWidget 会把它改成 True，必须在之后再关

    def minimumSizeHint(self) -> QSize:
        width = self.widget().minimumSizeHint().width() + self.verticalScrollBar().sizeHint().width()
        return QSize(width, super().minimumSizeHint().height())
