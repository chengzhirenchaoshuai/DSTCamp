"""世界设置面板：分类标题条 + 三列设置块，只绘制可见部分，整体按面板宽度等比缩放。

可编辑面板的每个设置有 < > 箭头，点击发出 value_clicked(key, delta)。
"""

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QAbstractScrollArea

from dstools.features.world.categories import CATEGORY_COLORS
from dstools.features.world.icons import get_icon_path
from dstools.features.world.value_labels import get_value_label
from dstools.features.world.value_sets import get_value_set, is_mod_readonly
from dstools.qt.imaging import pil_to_pixmap
from dstools.qt.theme import theme
from dstools.shared.resource_paths import bundled_resource_dir

BASE_WIDTH = 1300
COLS = 3
PAD_X = 10
ICON_SIZE = 74            # 紧凑布局（主页视口比创建向导小）
BLOCK_PAD_V = 8
ROW_GAP = 12
CAT_HEADER_H = 38
CAT_HEADER_ITEM_GAP = 12
CONTENT_MARGIN = 20
COL_GAP = 16
CAT_GAP_BEFORE = 8
CAT_GAP_AFTER = 10
BLOCK_PAD_H = 16
ARROW_DIR = bundled_resource_dir() / "icons" / "ui"

# 取值对应的强调色：值是调色板键名，或直接是十六进制色（跟主题无关的橙色）
_VALUE_COLORS = {
    "default": "TEXT_MUTED", "never": "ERROR", "rare": "ACCENT", "often": "PRIMARY", "always": "#ff9800",
    "none": "ERROR", "few": "ACCENT", "many": "PRIMARY", "max": "#ff9800",
    "veryslow": "ERROR", "slow": "ACCENT", "fast": "PRIMARY", "veryfast": "#ff9800",
    "nonlethal": "PRIMARY", "force": "#ff9800", "more": "PRIMARY",
    "disabled": "TEXT_MUTED", "enabled": "PRIMARY",
    "uncommon": "ACCENT", "ocean_uncommon": "ACCENT", "mostly": "PRIMARY", "insane": "#ff9800",
    "least": "ERROR", "most": "#ff9800", "True": "PRIMARY", "False": "ERROR",
}


def value_color(raw_value: str) -> QColor:
    entry = _VALUE_COLORS.get(raw_value, "TEXT")
    return QColor(entry) if entry.startswith("#") else theme.color(entry)


def wrap_text(text: str, metrics: QFontMetricsF, max_width: float) -> list[str]:
    """按实际像素宽度逐字换行，完整保留中英文设置名称。"""
    if not text:
        return [""]
    lines, current = [], ""
    for char in text:
        if char == "\n":
            lines.append(current)
            current = ""
            continue
        candidate = current + char
        if current and metrics.horizontalAdvance(candidate) > max_width:
            lines.append(current.rstrip())
            current = char.lstrip() if char.isspace() else char
        else:
            current = candidate
    if current or not lines:
        lines.append(current.rstrip())
    return lines


class _Metrics:
    """一次布局用到的全部尺寸。"""

    def __init__(self, width: float):
        self.s = s = max(0.3, width / BASE_WIDTH)
        self.icon = max(14, round(ICON_SIZE * s))
        self.pad_v = BLOCK_PAD_V * s
        self.pad_h = BLOCK_PAD_H * s
        self.row_h = self.icon + ROW_GAP + 2 * self.pad_v
        self.header_h = CAT_HEADER_H * s
        self.item_gap = CAT_HEADER_ITEM_GAP + self.pad_v
        self.pad_x = PAD_X * s
        margin = CONTENT_MARGIN * s
        self.gutter = COL_GAP + self.pad_h
        self.col_x0 = self.pad_x + margin + self.pad_h
        self.col_w = (width - 2 * self.pad_x - 2 * margin - self.pad_h - (COLS - 1) * self.gutter) / COLS


class WorldPanel(QAbstractScrollArea):
    value_clicked = Signal(str, int)

    def __init__(self, editable: bool, is_rule: bool, parent=None):
        super().__init__(parent)
        self._editable = editable
        self._is_rule = is_rule
        self._categories: list[tuple[str, str]] = []
        self._grouped: dict = {}
        self._location = "forest"
        self._mod_settings: dict = {}
        self._mod_icons: dict = {}
        self._flash: tuple[str, int] | None = None
        self._layout_width = -1
        self._layout: list = []
        self._total_h = 0
        self._icon_cache: dict = {}
        self._arrow_cache: dict = {}
        self._value_sets: dict = {}
        self.setFrameShape(QAbstractScrollArea.Shape.NoFrame)
        self.viewport().setAutoFillBackground(False)
        self.viewport().setMouseTracking(True)
        self.verticalScrollBar().setSingleStep(40)
        self.horizontalScrollBar().setEnabled(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    # ── 数据 ────────────────────────────────────────────────────────────
    def set_data(self, categories, grouped, location: str, mod_settings: dict, mod_icons: dict) -> None:
        self._categories, self._grouped = categories, grouped
        self._location, self._mod_settings, self._mod_icons = location, mod_settings, mod_icons
        self._icon_cache.clear()
        self._value_sets.clear()
        self._layout_width = -1
        self._update_scrollbar()
        self.viewport().update()

    def clear(self) -> None:
        self.set_data([], {}, "forest", {}, {})

    def set_editable(self, editable: bool) -> None:
        """切换是否显示箭头并响应点击（如世界生成只在服务器存档上可改）；行内几何随之变化，需重排。"""
        if editable == self._editable:
            return
        self._editable = editable
        self._layout_width = -1
        self.viewport().update()

    def set_flash(self, flash) -> None:
        self._flash = flash
        self.viewport().update()

    def refresh_values(self) -> None:
        """取值变了（点击箭头）：只需重绘，版式不变。"""
        self.viewport().update()

    # ── 版式 ────────────────────────────────────────────────────────────
    def _ensure_layout(self) -> None:
        width = self.viewport().width()
        if width == self._layout_width:
            return
        self._layout_width = width
        m = _Metrics(width)
        y = m.pad_x
        layout = []
        for key, name in self._categories:
            items = self._grouped.get(key)
            if not items:
                continue
            y += CAT_GAP_BEFORE * m.s
            box_top = y
            items_top = y + m.header_h + m.item_gap
            box_bottom = items_top + ((len(items) + COLS - 1) // COLS) * m.row_h
            layout.append((key, name, items, box_top, items_top, box_bottom))
            y = box_bottom + CAT_GAP_AFTER * m.s
        self._layout = layout
        self._total_h = max(int(y), int(40 * m.s)) if layout else 0

    def _update_scrollbar(self) -> None:
        self._ensure_layout()
        bar = self.verticalScrollBar()
        bar.setRange(0, max(0, self._total_h - self.viewport().height()))
        bar.setPageStep(self.viewport().height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_width = -1
        self._update_scrollbar()

    # ── 资源 ────────────────────────────────────────────────────────────
    def _icon_pixmap(self, key: str, size: int) -> QPixmap | None:
        cache_key = (key, size)
        if cache_key in self._icon_cache:
            return self._icon_cache[cache_key]
        pixmap = None
        path = get_icon_path(key, self._location)
        if path is not None:
            pixmap = QPixmap(str(path))
        elif key in self._mod_icons:
            pixmap = pil_to_pixmap(self._mod_icons[key])
        if pixmap is not None and not pixmap.isNull():
            dpr = self.devicePixelRatioF()
            pixmap = pixmap.scaled(round(size * dpr), round(size * dpr), Qt.AspectRatioMode.IgnoreAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation)
            pixmap.setDevicePixelRatio(dpr)
        else:
            pixmap = None
        if len(self._icon_cache) > 600:
            self._icon_cache.clear()
        self._icon_cache[cache_key] = pixmap
        return pixmap

    def _arrow_pixmap(self, direction: str, pressed: bool, height: float) -> QPixmap | None:
        name = f"arrow_{direction}" + ("_down" if pressed else "")
        cache_key = (name, round(height))
        if cache_key not in self._arrow_cache:
            pixmap = QPixmap(str(ARROW_DIR / f"{name}.png"))
            if pixmap.isNull():
                self._arrow_cache[cache_key] = None
            else:
                dpr = self.devicePixelRatioF()
                scaled = pixmap.scaledToHeight(max(1, round(height * dpr)), Qt.TransformationMode.SmoothTransformation)
                scaled.setDevicePixelRatio(dpr)
                self._arrow_cache[cache_key] = scaled
        return self._arrow_cache[cache_key]

    def _row_editable(self, key: str) -> bool:
        # 面板可编辑时，Mod 登记了但没给取值列表的条目仍只展示，不画箭头也不响应点击
        return self._editable and not is_mod_readonly(key, self._mod_settings)

    def _value_set(self, key: str) -> list[str]:
        if key not in self._value_sets:
            self._value_sets[key] = get_value_set(
                key, self._mod_settings, location=self._location, is_rule=self._is_rule)
        return self._value_sets[key]

    # ── 行内几何（绘制与点击共用）─────────────────────────────────────
    def _row_geometry(self, m: _Metrics, value_half_w: float, idx: int, items_top: float):
        col = idx % COLS
        top = items_top + (idx // COLS) * m.row_h
        cx = m.col_x0 + col * (m.col_w + m.gutter)
        icon_cy = top + m.icon / 2
        arrow_h, arrow_pad = 22 * m.s, 12 * m.s
        if self._editable:
            val_x = cx + m.col_w - 100 * m.s
            bx1 = val_x - value_half_w - arrow_pad - arrow_h / 2
            bx2 = val_x + value_half_w + arrow_pad + arrow_h / 2
            return dict(cx=cx, top=top, icon_cy=icon_cy, val_x=val_x, bx1=bx1, bx2=bx2,
                        arrow_h=arrow_h, text_x_end=bx1 - arrow_pad)
        value_cx = cx + m.col_w - value_half_w - 8 * m.s
        return dict(cx=cx, top=top, icon_cy=icon_cy, val_x=value_cx, bx1=None, bx2=None,
                    arrow_h=arrow_h, text_x_end=value_cx - value_half_w - 8 * m.s)

    def _fonts(self, s: float):
        def make(px: float, bold: bool = False) -> QFont:
            font = theme.panel_font(px)
            font.setBold(bold)
            return font
        return make(16 * s), make(16 * s), make(22 * s)

    # ── 绘制 ────────────────────────────────────────────────────────────
    def paintEvent(self, _event):
        self._ensure_layout()
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # 像素字体清晰渲染时由字体自身的 NoAntialias 策略关抗锯齿，此处统一开文字抗锯齿。
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        offset = self.verticalScrollBar().value()
        view_h = self.viewport().height()
        painter.translate(0, -offset)
        width = self.viewport().width()
        m = _Metrics(width)
        name_font, value_font, header_font = self._fonts(m.s)
        name_fm, value_fm = QFontMetricsF(name_font), QFontMetricsF(value_font)
        value_text_w = value_fm.horizontalAdvance("汉字汉字")
        half_w = value_text_w / 2

        for key, name, items, box_top, items_top, box_bottom in self._layout:
            if box_bottom < offset or box_top > offset + view_h:
                continue
            color = QColor(CATEGORY_COLORS.get(key, theme.hex("TEXT_MUTED")))
            self._paint_header(painter, m, header_font, box_top, f"{name} ({len(items)})", color, width)
            for idx, override in enumerate(items):
                geo = self._row_geometry(m, half_w, idx, items_top)
                if geo["top"] + m.icon + m.pad_v < offset or geo["top"] - m.pad_v > offset + view_h:
                    continue
                self._paint_row(painter, m, override, geo, name_font, value_font, name_fm, value_fm, value_text_w, half_w)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(color, 2))
            painter.drawRoundedRect(QRectF(m.pad_x, box_top, width - 2 * m.pad_x, box_bottom - box_top),
                                    10 * m.s, 10 * m.s)

    def _paint_header(self, painter, m, font, box_top, text, color, width) -> None:
        rect = QRectF(m.pad_x, box_top, width - 2 * m.pad_x, m.header_h)
        path = QPainterPath()  # 顶部两角圆、底部直角：画一个更高的圆角矩形再裁成标题条高度
        path.addRoundedRect(QRectF(rect.x(), rect.y(), rect.width(), rect.height() + 20 * m.s), 10 * m.s, 10 * m.s)
        painter.save()
        painter.setClipRect(rect)
        painter.setBrush(theme.color("CARD_BG_ALT"))
        painter.setPen(QPen(theme.color("CARD_BORDER"), 1))
        painter.drawPath(path)
        painter.restore()
        painter.setFont(font)
        painter.setPen(color)
        painter.drawText(QRectF(rect.x() + 10 * m.s, rect.y(), rect.width() - 20 * m.s, rect.height()),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)

    def _paint_row(self, painter, m, override, geo, name_font, value_font, name_fm, value_fm, value_text_w, half_w):
        cx, top, icon_cy = geo["cx"], geo["top"], geo["icon_cy"]
        block = QRectF(max(0.0, cx - m.pad_h), top - m.pad_v, cx + m.col_w - max(0.0, cx - m.pad_h),
                       m.icon + 2 * m.pad_v)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme.color("PRIMARY_LIGHT"))
        painter.drawRoundedRect(block, 10 * m.s, 10 * m.s)

        icon = self._icon_pixmap(override.key, m.icon)
        if icon is not None:
            painter.drawPixmap(QPointF(cx, top), icon)

        text_x0 = cx + m.icon + 8 * m.s
        slot_w = max(10.0, geo["text_x_end"] - text_x0)
        painter.setFont(name_font)
        painter.setPen(theme.color("TEXT"))
        self._draw_centered(painter, wrap_text(override.name or override.key, name_fm, slot_w),
                            text_x0 + slot_w / 2, icon_cy, name_fm, m.s)

        value = override.value
        painter.setFont(value_font)
        painter.setPen(value_color(value))
        self._draw_centered(painter, wrap_text(get_value_label(override.key, value, self._mod_settings), value_fm, value_text_w),
                            geo["val_x"], icon_cy, value_fm, m.s)
        if self._row_editable(override.key):
            values = self._value_set(override.key)
            try:
                idx = values.index(value)
                at_min, at_max = idx <= 0, idx >= len(values) - 1
            except ValueError:
                at_min = at_max = False
            for direction, bx, disabled in (("left", geo["bx1"], at_min), ("right", geo["bx2"], at_max)):
                pressed = self._flash == (override.key, -1 if direction == "left" else 1)
                self._paint_arrow(painter, direction, bx, icon_cy, geo["arrow_h"], disabled, pressed)

    @staticmethod
    def _draw_centered(painter, lines: list[str], cx: float, cy: float, metrics: QFontMetricsF, s: float) -> None:
        spacing = max(1.0, round(2 * s))
        line_h = metrics.height()
        y = cy - (len(lines) * line_h + (len(lines) - 1) * spacing) / 2
        for line in lines:
            painter.drawText(QRectF(cx - 400, y, 800, line_h), Qt.AlignmentFlag.AlignCenter, line)
            y += line_h + spacing

    def _paint_arrow(self, painter, direction, cx, cy, height, disabled, pressed) -> None:
        # 按下时换游戏自己的 _down 贴图并放大一点，点击瞬间才看得出"点到了"；到头的一侧淡出
        pixmap = self._arrow_pixmap(direction, pressed, height * 1.3 if pressed else height)
        if pixmap is None:
            return
        dpr = pixmap.devicePixelRatio()
        w, h = pixmap.width() / dpr, pixmap.height() / dpr
        painter.save()
        if disabled:
            painter.setOpacity(0.32)
        painter.drawPixmap(QPointF(cx - w / 2, cy - h / 2), pixmap)
        painter.restore()

    # ── 点击 ────────────────────────────────────────────────────────────
    def _hit_test(self, pos) -> tuple[str, int] | None:
        """返回点中的 (key, delta)；箭头到头（淡出）时不响应。"""
        if not self._editable:
            return None
        self._ensure_layout()
        y = pos.y() + self.verticalScrollBar().value()
        m = _Metrics(self.viewport().width())
        _, value_font, _ = self._fonts(m.s)
        half_w = QFontMetricsF(value_font).horizontalAdvance("汉字汉字") / 2
        for _key, _name, items, _box_top, items_top, box_bottom in self._layout:
            if not (items_top <= y <= box_bottom):
                continue
            row = int((y - items_top) // m.row_h)
            for col in range(COLS):
                idx = row * COLS + col
                if idx >= len(items):
                    break
                geo = self._row_geometry(m, half_w, idx, items_top)
                if not (geo["top"] <= y <= geo["top"] + m.icon):
                    continue
                override = items[idx]
                if not self._row_editable(override.key):
                    continue
                values = self._value_set(override.key)
                try:
                    vi = values.index(override.value)
                    at_min, at_max = vi <= 0, vi >= len(values) - 1
                except ValueError:
                    at_min = at_max = False
                half = geo["arrow_h"] / 2
                if abs(pos.x() - geo["bx1"]) <= half and not at_min:
                    return override.key, -1
                if abs(pos.x() - geo["bx2"]) <= half and not at_max:
                    return override.key, 1
        return None

    def mouseMoveEvent(self, event):
        hit = self._hit_test(event.position()) is not None
        self.viewport().setCursor(Qt.CursorShape.PointingHandCursor if hit else Qt.CursorShape.ArrowCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            hit = self._hit_test(event.position())
            if hit is not None:
                self.value_clicked.emit(*hit)
