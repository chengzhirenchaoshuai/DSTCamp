"""通用自绘控件：圆角开关、半透明圆角卡片、胶囊页签条、缩放热区（颜色在 paintEvent 里现查）。"""

import ctypes
from ctypes import wintypes

from PySide6.QtCore import QEvent, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QComboBox, QFrame, QLabel, QMenu, QScrollArea, QToolTip, QVBoxLayout, QWidget

from dstools.i18n import t
from dstools.qt.text_halo import draw_adaptive_text
from dstools.qt.theme import theme


class ShardCombo(QComboBox):
    """世界（分片）下拉：显示文件夹名，主世界后面加“（主世界）”；世界名存在每一项的 data 里。

    取值、定位一律用 shard_name()/set_current_shard()/find_shard()，不要用 currentText()/findText()
    （主世界那一项的文字带后缀）。切换语言时由 retranslate 调 refresh_labels() 按新语言重拼后缀。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._master = ""

    def showPopup(self) -> None:
        # 坑（离屏点击实测）：子类不重写 showPopup 时，点击展开走不到 theme 打在 QComboBox 上的
        # showPopup 补丁，列表不收窄、没有假透明背景；重写后经 super() 就能走到（wheelEvent/paintEvent 补丁不受影响）
        super().showPopup()

    def _label(self, name: str) -> str:
        return t("world.shard_master_label", name=name) if name and name == self._master else name

    def set_shards(self, names, master: str = "") -> None:
        """整体替换列表（调用方自行 blockSignals）；master 为主世界的文件夹名。"""
        self._master = master
        self.clear()
        for name in names:
            self.addItem(self._label(name), name)

    def set_master(self, master: str) -> None:
        self._master = master
        self.refresh_labels()

    def add_shard(self, name: str) -> None:
        self.addItem(self._label(name), name)

    def insert_shard(self, index: int, name: str) -> None:
        self.insertItem(index, self._label(name), name)

    def refresh_labels(self) -> None:
        for index in range(self.count()):
            self.setItemText(index, self._label(self.itemData(index)))

    def shard_name(self) -> str:
        return self.currentData() or ""

    def shard_names(self) -> list[str]:
        return [self.itemData(index) for index in range(self.count())]

    def find_shard(self, name: str) -> int:
        return self.findData(name)

    def set_current_shard(self, name: str) -> None:
        index = self.findData(name)
        if index >= 0:
            self.setCurrentIndex(index)


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
                 uniform_width: bool = False, bold: bool = False, margin: int = 24):
        super().__init__(parent)
        self._labels = list(labels)
        # 左右留白：顶层页签条两侧要空出来；放进一行控件里的子页签条传小值，免得与左右控件隔得太开
        self._margin = margin
        self._tooltips: list[str] | None = None  # 每个页签各自的悬停说明，None 时沿用整条的 toolTip
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

    def set_tooltips(self, tooltips: list[str]) -> None:
        """给每个页签单独设悬停说明，顺序与标签一致。"""
        self._tooltips = list(tooltips)

    def event(self, event):
        if event.type() == QEvent.Type.ToolTip and self._tooltips is not None:
            pos = QPointF(event.pos())
            index = next((i for i, r in enumerate(self._rects()) if r.contains(pos)), -1)
            if 0 <= index < len(self._tooltips) and self._tooltips[index]:
                # 带上页签矩形：鼠标移出这个页签时提示自动消失，移到别的页签会重新触发
                QToolTip.showText(event.globalPos(), self._tooltips[index], self,
                                  self._rects()[index].toAlignedRect())
            else:
                QToolTip.hideText()
                event.ignore()
            return True
        return super().event(event)

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
        x = float(self._margin)
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
        width = int(rects[-1].right()) + self._margin if rects else 0
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


WM_SETCURSOR, WM_NCHITTEST, WM_NCLBUTTONDOWN, WM_NCLBUTTONDBLCLK = 0x0020, 0x0084, 0x00A1, 0x00A3
HTLEFT, HTRIGHT, HTTOP, HTTOPLEFT, HTTOPRIGHT, HTBOTTOM, HTBOTTOMLEFT, HTBOTTOMRIGHT = range(10, 18)
_RESIZE_EDGE, _RESIZE_CORNER = 6, 12  # 边宽、角边长（逻辑像素）
_E = Qt.Edge
# 命中码 -> (系统光标 IDC_SIZE*，Qt 缩放边)
_RESIZE_HITS = {
    HTLEFT: (32644, _E.LeftEdge), HTRIGHT: (32644, _E.RightEdge),
    HTTOP: (32645, _E.TopEdge), HTBOTTOM: (32645, _E.BottomEdge),
    HTTOPLEFT: (32642, _E.TopEdge | _E.LeftEdge), HTBOTTOMRIGHT: (32642, _E.BottomEdge | _E.RightEdge),
    HTTOPRIGHT: (32643, _E.TopEdge | _E.RightEdge), HTBOTTOMLEFT: (32643, _E.BottomEdge | _E.LeftEdge),
}


class _WinRect(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def resize_border_result(window: QWidget, msg) -> int | None:
    """无边框窗口的边/角缩放热区，在顶层窗口的 nativeEvent 里调用；返回值不为 None 时原样作为消息结果返回。

    边/角在 WM_NCHITTEST 里报成缩放边框（HTLEFT 等），成为非客户区。
    不用子控件 setCursor：Qt 只在光标形状变化且它记录的"鼠标所在窗口"正好是本窗口时才调 SetCursor，
    从其他窗口移入时这份记录可能过时，移回内容区的那次恢复被跳过，"拖动"指针就一直留着。
    报成边框后，鼠标从边框回到内容区会触发离开/进入，Qt 按正常路径重新应用指针。
    Qt 的无边框窗口是 WS_POPUP、没有 WS_THICKFRAME，系统默认处理不会给这些命中码换缩放指针（已实测仍是箭头），
    所以指针在 WM_SETCURSOR 里自己设（每次鼠标移动系统都会发），按下时仍走 startSystemResize。"""
    if msg.message == WM_SETCURSOR and (msg.lParam & 0xFFFF) in _RESIZE_HITS:
        user32 = ctypes.windll.user32
        user32.LoadCursorW.restype = wintypes.HANDLE
        user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
        user32.SetCursor.argtypes = [wintypes.HANDLE]
        user32.SetCursor(user32.LoadCursorW(None, _RESIZE_HITS[msg.lParam & 0xFFFF][0]))
        return 1
    if msg.message == WM_NCLBUTTONDOWN and msg.wParam in _RESIZE_HITS:
        window.windowHandle().startSystemResize(_RESIZE_HITS[msg.wParam][1])
        return 0
    if msg.message == WM_NCLBUTTONDBLCLK and msg.wParam in _RESIZE_HITS:
        return 0  # 双击边框不做任何事（原先的热区也没有双击行为）
    if msg.message != WM_NCHITTEST or not window.isEnabled():
        return None
    rect = _WinRect()
    if not ctypes.windll.user32.GetWindowRect(wintypes.HWND(msg.hWnd), ctypes.byref(rect)):
        return None
    # lParam 是屏幕物理像素坐标，低/高 16 位各为有符号数（副屏在左侧/上方时为负）
    x = ctypes.c_short(msg.lParam & 0xFFFF).value - rect.left
    y = ctypes.c_short((msg.lParam >> 16) & 0xFFFF).value - rect.top
    w, h = rect.right - rect.left, rect.bottom - rect.top
    dpr = window.devicePixelRatioF()
    edge, corner = round(_RESIZE_EDGE * dpr), round(_RESIZE_CORNER * dpr)
    if not (0 <= x < w and 0 <= y < h):
        return None
    left, right = x < corner, x >= w - corner
    top, bottom = y < corner, y >= h - corner
    if top and left:
        return HTTOPLEFT
    if top and right:
        return HTTOPRIGHT
    if bottom and left:
        return HTBOTTOMLEFT
    if bottom and right:
        return HTBOTTOMRIGHT
    if x < edge:
        return HTLEFT
    if x >= w - edge:
        return HTRIGHT
    if y < edge:
        return HTTOP
    if y >= h - edge:
        return HTBOTTOM
    return None


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
