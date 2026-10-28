"""Mod 列表面板：图标、名称/ID/版本、开关、配置按钮、创意工坊链接与打开目录。

用 QPainter 在 QAbstractScrollArea 视口上只画可见行（与 world_panel.py 相同思路）。
"""

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QAbstractScrollArea, QToolTip

from dstools.i18n import t
from dstools.qt.imaging import pil_to_pixmap
from dstools.qt.theme import theme
from dstools.shared.resource_paths import bundled_resource_dir

BASE_WIDTH = 1300
PAD_X = 14
ICON_SIZE = 88
ROW_GAP = 8
ROW_H = ICON_SIZE + ROW_GAP
SWITCH_W, SWITCH_H = 76, 34
CFG_W, CFG_H = 116, 40
LINK_W = 100  # 只是列的锚点起始位置，链接文字/图标按实际宽度紧跟着画，不撑满这个宽度
COL_GAP = 16
# 多列时每格的紧凑尺寸（按基准宽度 1300 的像素，随面板宽度同比缩放）
GRID_GAP = 10
GRID_ICON = 64
GRID_SWITCH_W, GRID_SWITCH_H = 54, 26
GRID_CFG_W, GRID_CFG_H = 64, 26
GRID_FOLDER = 18

_OFF_COLOR = QColor("#bdbdbd")
_CFG_DISABLED_BG = QColor("#cfd8dc")
_CFG_DISABLED_FG = QColor("#90a4ae")
_LINK_DISABLED = QColor("#bdbdbd")
_DEFAULT_ICON_PATH = bundled_resource_dir() / "icons" / "ui" / "mod_icon_default.png"
_OPEN_FOLDER_ICON_PATH = bundled_resource_dir() / "icons" / "ui" / "open_file_folder_fluent.png"


def mod_list_height(row_count: int, width: int, columns: int = 1) -> int:
    s = width / BASE_WIDTH
    lines = (row_count + columns - 1) // columns
    return max(int(PAD_X * s + lines * (ROW_H * s + ROW_GAP)), 40)


class _Metrics:
    def __init__(self, width: float):
        self.s = s = max(0.3, width / BASE_WIDTH)
        self.pad_x = PAD_X * s
        self.row_h = ROW_H * s
        self.row_gap = ROW_GAP  # 固定值，不随比例缩放
        self.icon = max(20, round(ICON_SIZE * s))
        self.switch_w, self.switch_h = SWITCH_W * s, SWITCH_H * s
        self.cfg_w, self.cfg_h = CFG_W * s, CFG_H * s
        self.link_w = LINK_W * s
        self.col_gap = COL_GAP * s
        self.row_step = self.row_h + self.row_gap


class ModListPanel(QAbstractScrollArea):
    toggle_requested = Signal(str)
    config_requested = Signal(str)
    link_requested = Signal(str)
    folder_requested = Signal(str)
    copy_id_requested = Signal(str)
    copy_name_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[dict] = []
        self._icon_images: dict = {}
        self._icon_pixmap_cache: dict = {}
        self._interactive = True
        self._column_count = 1  # 1 列为原横排；2/3 列为紧凑格子（Mod 管理页可切换）
        self._layout_width = -1
        self._total_h = 0
        self._default_icon_cache: dict[int, QPixmap] = {}
        self._folder_icon_cache: dict[int, QPixmap] = {}
        self._center_message = ""  # 列表为空时画在首行位置的提示（如"正在加载 Mod 列表..."）
        # 锁定开关的"LuaJIT 补丁生效中"提示：悬停满 0.7 秒才弹，提前移开即取消（自绘提示，不走 QToolTip 延迟）
        self._locked_tip_timer = QTimer(self)
        self._locked_tip_timer.setSingleShot(True)
        self._locked_tip_timer.setInterval(700)
        self._locked_tip_timer.timeout.connect(self._show_locked_tip)
        self._locked_tip_pos = None
        self.setFrameShape(QAbstractScrollArea.Shape.NoFrame)
        self.viewport().setAutoFillBackground(False)
        self.viewport().setMouseTracking(True)
        self.verticalScrollBar().setSingleStep(40)
        self.horizontalScrollBar().setEnabled(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    # ── 数据 ────────────────────────────────────────────────────────────
    def set_rows(self, rows: list[dict], icon_images: dict, interactive: bool = True) -> None:
        keep_scroll = self._rows and rows
        offset = self.verticalScrollBar().value() if keep_scroll else 0
        self._rows = rows
        self._icon_images = icon_images
        self._interactive = interactive
        self._icon_pixmap_cache.clear()
        self._layout_width = -1
        self._update_scrollbar()
        if keep_scroll:
            self.verticalScrollBar().setValue(min(offset, self.verticalScrollBar().maximum()))
        self.viewport().update()

    def clear(self) -> None:
        self.set_rows([], {})

    def set_column_count(self, count: int) -> None:
        """切换显示列数；顺序按从左到右、从上到下排。"""
        count = max(1, int(count))
        if count == self._column_count:
            return
        self._column_count = count
        self._layout_width = -1
        self._update_scrollbar()
        self.viewport().update()

    def first_screen_count(self, width: int | None = None, height: int | None = None) -> int:
        """不滚动时首屏能显示几项（含最后一行露出一部分的），按当前列数；1 列和多列格子行距相同。
        控件还没显示、尺寸未定时由调用方传入估计的宽高。"""
        m = _Metrics(width or self.viewport().width())
        view_h = height or self.viewport().height()
        lines = int(max(0.0, view_h - m.pad_x) // m.row_step) + 1
        return lines * self._column_count

    def set_center_message(self, text: str) -> None:
        """设置列表为空时首行位置显示的提示文字；传空串取消。"""
        if text != self._center_message:
            self._center_message = text
            self.viewport().update()

    def _ensure_layout(self) -> None:
        width = self.viewport().width()
        if width == self._layout_width:
            return
        self._layout_width = width
        self._total_h = mod_list_height(len(self._rows), max(1, width), self._column_count)

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
    def _icon_pixmap(self, workshop_id: str, size: int) -> QPixmap | None:
        cache_key = (workshop_id, size)
        if cache_key in self._icon_pixmap_cache:
            return self._icon_pixmap_cache[cache_key]
        image = self._icon_images.get(workshop_id)
        pixmap = None
        if image is not None:
            pixmap = pil_to_pixmap(image)
        else:
            pixmap = self._default_icon(size)
        if pixmap is not None and not pixmap.isNull():
            dpr = self.devicePixelRatioF()
            pixmap = pixmap.scaled(round(size * dpr), round(size * dpr), Qt.AspectRatioMode.IgnoreAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation)
            pixmap.setDevicePixelRatio(dpr)
        if len(self._icon_pixmap_cache) > 300:
            self._icon_pixmap_cache.clear()
        self._icon_pixmap_cache[cache_key] = pixmap
        return pixmap

    def _default_icon(self, size: int) -> QPixmap | None:
        if size in self._default_icon_cache:
            return self._default_icon_cache[size]
        pixmap = QPixmap(str(_DEFAULT_ICON_PATH)) if _DEFAULT_ICON_PATH.exists() else None
        if pixmap is not None and pixmap.isNull():
            pixmap = None
        self._default_icon_cache[size] = pixmap
        return pixmap

    def _folder_icon(self, size: int) -> QPixmap | None:
        if size in self._folder_icon_cache:
            return self._folder_icon_cache[size]
        pixmap = QPixmap(str(_OPEN_FOLDER_ICON_PATH)) if _OPEN_FOLDER_ICON_PATH.exists() else None
        if pixmap is not None and not pixmap.isNull():
            dpr = self.devicePixelRatioF()
            pixmap = pixmap.scaled(round(size * dpr), round(size * dpr), Qt.AspectRatioMode.KeepAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation)
            pixmap.setDevicePixelRatio(dpr)
        else:
            pixmap = None
        self._folder_icon_cache[size] = pixmap
        return pixmap

    # ── 行几何（绘制与点击共用）───────────────────────────────────────────
    def _columns(self, m: _Metrics, width: float):
        link_x2 = width - m.pad_x
        link_x1 = link_x2 - m.link_w
        cfg_x2 = link_x1 - m.col_gap
        cfg_x1 = cfg_x2 - m.cfg_w
        switch_x2 = cfg_x1 - m.col_gap
        switch_x1 = switch_x2 - m.switch_w
        icon_x = m.pad_x + 10 * m.s
        name_x = icon_x + m.icon + 14 * m.s
        name_w = max(30.0, switch_x1 - m.col_gap - name_x)
        return dict(icon_x=icon_x, name_x=name_x, name_w=name_w,
                   switch_x1=switch_x1, switch_x2=switch_x2,
                   cfg_x1=cfg_x1, cfg_x2=cfg_x2, link_x1=link_x1, link_x2=link_x2)

    def _fonts(self, s: float):
        name_font = theme.panel_font(15 * s)
        id_font = theme.panel_font(12 * s)
        btn_font = theme.panel_font(13 * s)
        # "配置"按钮文字用 14 号，比链接文字（13 号）更醒目
        cfg_font = theme.panel_font(14 * s)
        return name_font, id_font, btn_font, cfg_font

    # ── 绘制 ────────────────────────────────────────────────────────────
    def paintEvent(self, _event):
        self._ensure_layout()
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # 像素字体清晰渲染时由字体自身的 NoAntialias 策略关抗锯齿，此处统一开文字抗锯齿。
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        if not self._rows and self._center_message:
            # 没有行可画时（加载中），在列表首行位置水平居中画一行加粗提示。
            painter.setFont(theme.font("FONT_SIZE_MD", bold=True))
            painter.setPen(theme.color("TEXT_MUTED"))
            painter.drawText(QRectF(self.viewport().rect()).adjusted(12, 16, -12, 0),
                             int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop
                                 | Qt.TextFlag.TextWordWrap),
                             self._center_message)
            return
        offset = self.verticalScrollBar().value()
        view_h = self.viewport().height()
        width = self.viewport().width()
        painter.translate(0, -offset)
        m = _Metrics(width)
        cols = self._columns(m, width)
        name_font, id_font, btn_font, cfg_font = self._fonts(m.s)
        name_fm, id_fm = QFontMetricsF(name_font), QFontMetricsF(id_font)

        if self._column_count > 1:
            self._paint_grid(painter, m, width, offset, view_h)
            return
        first = max(0, int((offset - m.pad_x) // m.row_step) - 1)
        last = min(len(self._rows), int((offset + view_h - m.pad_x) // m.row_step) + 2)
        for i in range(first, last):
            row = self._rows[i]
            top = m.pad_x + i * m.row_step
            if top + m.row_h < offset or top > offset + view_h:
                continue
            self._paint_row(painter, m, cols, row, i, top, width, name_font, id_font, btn_font, cfg_font, name_fm, id_fm)

    @staticmethod
    def _paint_format_tag(painter, fmt, version_rect: QRectF, version_text: str, id_font, id_fm, s: float) -> None:
        """在版本号文字后面画 V1/V2 小圆角标签（跟更新 Mod 窗口同款颜色）；没有格式信息不画。"""
        if fmt not in ("V1", "V2"):
            return
        from dstools.qt.widgets import mod_format_tag_colors

        tag_font = QFont(id_font)  # 字号跟版本号文字一致，只加粗
        tag_font.setBold(True)
        tag_fm = QFontMetricsF(tag_font)
        pad_x = 5 * s
        tag_w = tag_fm.horizontalAdvance(fmt) + 2 * pad_x
        tag_h = tag_fm.height() + 3 * s
        x = version_rect.left() + id_fm.horizontalAdvance(version_text) + 6 * s
        if x + tag_w > version_rect.right():
            return  # 版本号太长被省略时不硬塞标签
        rect = QRectF(x, version_rect.center().y() - tag_h / 2, tag_w, tag_h)
        background, color = mod_format_tag_colors(fmt)
        painter.save()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(background)
        painter.drawRoundedRect(rect, 4 * s, 4 * s)
        painter.setPen(color)
        painter.setFont(tag_font)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, fmt)
        painter.restore()

    def _paint_row(self, painter, m, cols, row, index, top, width, name_font, id_font, btn_font, cfg_font, name_fm, id_fm):
        wid = row["workshop_id"]
        bg = theme.color("CARD_BG_ALT") if index % 2 == 0 else theme.color("CARD_BG")
        painter.setPen(QPen(theme.color("CARD_BORDER"), 1))
        painter.setBrush(bg)
        painter.drawRect(QRectF(m.pad_x, top, width - 2 * m.pad_x, m.row_h))
        cy = top + m.row_h / 2

        icon = self._icon_pixmap(wid, m.icon)
        if icon is not None:
            dpr = icon.devicePixelRatio()
            painter.drawPixmap(QPointF(cols["icon_x"], top + (m.row_h - icon.height() / dpr) / 2), icon)

        # 名字 / workshop id / 版本，三行
        painter.setFont(name_font)
        painter.setPen(theme.color("TEXT"))
        full_name = row["name"] or wid
        name_text = self._elide(full_name, name_fm, cols["name_w"])
        painter.drawText(QRectF(cols["name_x"], top + m.row_h * 0.08, cols["name_w"], m.row_h * 0.3),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name_text)
        painter.setFont(id_font)
        painter.setPen(theme.color("TEXT_MUTED"))
        painter.drawText(QRectF(cols["name_x"], top + m.row_h * 0.4, cols["name_w"], m.row_h * 0.28),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, str(wid))
        version_text = self._elide(row.get("version_text", ""), id_fm, cols["name_w"])
        version_rect = QRectF(cols["name_x"], top + m.row_h * 0.68, cols["name_w"], m.row_h * 0.28)
        painter.drawText(version_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, version_text)
        self._paint_format_tag(painter, row.get("mod_format"), version_rect, version_text, id_font, id_fm, m.s)

        # 开关 / 本地徽章
        if row.get("is_local"):
            self._paint_badge(painter, cols["switch_x1"], cy, m.switch_w, m.switch_h, t("mod.local_badge"), id_font)
        else:
            self._paint_switch(painter, cols["switch_x1"], cy, m.switch_w, m.switch_h, row["enabled"],
                               locked=bool(row.get("locked")))

        # 配置按钮
        self._paint_pill(painter, cols["cfg_x1"], cy - m.cfg_h / 2, m.cfg_w, m.cfg_h,
                         t("mod.config_btn"), cfg_font, enabled=row.get("has_config", False))

        # 打开目录图标紧跟链接文字实际宽度之后绘制，而不是固定在列最右端
        has_link = row.get("has_link", False)
        link_color = theme.color("ACCENT") if has_link else _LINK_DISABLED
        link_text = t("mod.workshop_link_btn") if has_link else t("mod.no_workshop_link")
        painter.setFont(btn_font)
        painter.setPen(link_color)
        link_fm = QFontMetricsF(btn_font)
        link_tw = link_fm.horizontalAdvance(link_text)
        link_rect = QRectF(cols["link_x1"], top, link_tw + 4 * m.s, m.row_h)
        painter.drawText(link_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, link_text)
        if has_link:
            painter.drawLine(QPointF(cols["link_x1"], cy + 9 * m.s), QPointF(cols["link_x1"] + link_tw, cy + 9 * m.s))
        if row.get("has_folder"):
            folder_size = round(22 * m.s)
            folder_icon = self._folder_icon(folder_size)
            folder_x = cols["link_x1"] + link_tw + 10 * m.s
            if folder_icon is not None:
                dpr = folder_icon.devicePixelRatio()
                painter.drawPixmap(QPointF(folder_x, cy - folder_icon.height() / dpr / 2), folder_icon)

    # ── 多列紧凑格子 ────────────────────────────────────────────────────
    def _grid_fonts(self, s: float):
        name_font = theme.panel_font(14 * s)
        meta_font = theme.panel_font(11 * s)
        btn_font = theme.panel_font(12 * s)
        return name_font, meta_font, btn_font

    def _cell_rect(self, m: _Metrics, width: float, index: int) -> QRectF:
        n = self._column_count
        gap = GRID_GAP * m.s
        cell_w = (width - 2 * m.pad_x - (n - 1) * gap) / n
        line, col = divmod(index, n)
        return QRectF(m.pad_x + col * (cell_w + gap), m.pad_x + line * m.row_step, cell_w, m.row_h)

    def _cell_geometry(self, m: _Metrics, cell: QRectF, row: dict, fonts) -> dict:
        """一格内各元素的位置（绘制与点击共用），左右分区：
        左边图标；中间三行文字（名称 / ID·版本 / 创意工坊链接 + 打开目录）；
        右边单独一列，开关在上、配置按钮在下，都垂直居中——主要操作靠右，跟单列一致。"""
        s = m.s
        name_font, meta_font, btn_font = fonts
        pad = 12 * s
        icon = max(16, round(GRID_ICON * s))
        icon_x = cell.left() + pad
        cy = cell.center().y()

        # 右侧操作列：开关 + 配置上下叠放，按两者较宽的那个定列宽，各自水平居中。
        side_w = max(GRID_SWITCH_W, GRID_CFG_W) * s
        side_x = cell.right() - pad - side_w
        gap = 8 * s
        stack_h = GRID_SWITCH_H * s + gap + GRID_CFG_H * s
        switch = QRectF(side_x + (side_w - GRID_SWITCH_W * s) / 2, cy - stack_h / 2,
                        GRID_SWITCH_W * s, GRID_SWITCH_H * s)
        cfg = QRectF(side_x + (side_w - GRID_CFG_W * s) / 2, switch.bottom() + gap, GRID_CFG_W * s, GRID_CFG_H * s)

        # 中间文字区：三行均分格子高度（上下各留一点边），名称最醒目。
        text_x = icon_x + icon + pad
        text_right = side_x - pad
        text_w = max(20.0, text_right - text_x)
        line_h = (cell.height() - 2 * 8 * s) / 3
        top = cell.top() + 8 * s
        name_rect = QRectF(text_x, top, text_w, line_h)
        meta_rect = QRectF(text_x, top + line_h, text_w, line_h)
        link_cy = top + line_h * 2.5
        has_link = row.get("has_link", False)
        link_text = t("mod.workshop_link_btn") if has_link else t("mod.no_workshop_link")
        link_w = QFontMetricsF(btn_font).horizontalAdvance(link_text)
        link = QRectF(text_x, top + line_h * 2, link_w, line_h)
        folder_size = round(GRID_FOLDER * s)
        folder = QRectF(link.right() + 8 * s, link_cy - folder_size / 2, folder_size, folder_size)
        return dict(icon=icon, icon_x=icon_x, name_rect=name_rect, meta_rect=meta_rect, action_cy=cy,
                    link_cy=link_cy, text_right=text_right, switch=switch, cfg=cfg, link=link,
                    link_text=link_text, folder=folder, folder_size=folder_size)

    def _paint_grid(self, painter, m: _Metrics, width: float, offset: float, view_h: float) -> None:
        n = self._column_count
        fonts = self._grid_fonts(m.s)
        name_font, meta_font, btn_font = fonts
        name_fm, meta_fm = QFontMetricsF(name_font), QFontMetricsF(meta_font)
        first_line = max(0, int((offset - m.pad_x) // m.row_step) - 1)
        last_line = int((offset + view_h - m.pad_x) // m.row_step) + 2
        for i in range(first_line * n, min(len(self._rows), last_line * n)):
            cell = self._cell_rect(m, width, i)
            if cell.bottom() < offset or cell.top() > offset + view_h:
                continue
            self._paint_cell(painter, m, cell, self._rows[i], i // n, fonts, name_fm, meta_fm)

    def _paint_cell(self, painter, m, cell: QRectF, row: dict, line: int, fonts, name_fm, meta_fm) -> None:
        name_font, meta_font, btn_font = fonts
        s = m.s
        wid = row["workshop_id"]
        geo = self._cell_geometry(m, cell, row, fonts)
        painter.setPen(QPen(theme.color("CARD_BORDER"), 1))
        painter.setBrush(theme.color("CARD_BG_ALT") if line % 2 == 0 else theme.color("CARD_BG"))
        painter.drawRoundedRect(cell, 8 * s, 8 * s)

        icon = self._icon_pixmap(wid, geo["icon"])
        if icon is not None:
            dpr = icon.devicePixelRatio()
            painter.drawPixmap(QPointF(geo["icon_x"], cell.top() + (cell.height() - icon.height() / dpr) / 2), icon)

        name_rect = geo["name_rect"]
        painter.setFont(name_font)
        painter.setPen(theme.color("TEXT"))
        painter.drawText(name_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         self._elide(row["name"] or wid, name_fm, name_rect.width()))

        # 第二行：ID · 版本（版本后面照样跟 V1/V2 标签，放不下就不画）
        meta_rect = geo["meta_rect"]
        painter.setFont(meta_font)
        painter.setPen(theme.color("TEXT_MUTED"))
        id_text = self._elide(str(wid), meta_fm, meta_rect.width())
        painter.drawText(meta_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, id_text)
        version = row.get("version_text", "")
        rest_x = meta_rect.left() + meta_fm.horizontalAdvance(id_text)
        rest_w = meta_rect.right() - rest_x
        if version and rest_w > 20 * s:
            version_text = self._elide(f" · {version}", meta_fm, rest_w)
            version_rect = QRectF(rest_x, meta_rect.top(), rest_w, meta_rect.height())
            painter.drawText(version_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, version_text)
            self._paint_format_tag(painter, row.get("mod_format"), version_rect, version_text, meta_font, meta_fm, s)

        # 右侧：开关 / 本地徽章、配置；文字区第三行：创意工坊链接、打开目录
        switch = geo["switch"]
        switch_cy = switch.center().y()
        if row.get("is_local"):
            self._paint_badge(painter, switch.left(), switch_cy, switch.width(), switch.height(),
                              t("mod.local_badge"), meta_font)
        else:
            self._paint_switch(painter, switch.left(), switch_cy, switch.width(), switch.height(), row["enabled"],
                               locked=bool(row.get("locked")))
        cfg = geo["cfg"]
        self._paint_pill(painter, cfg.left(), cfg.top(), cfg.width(), cfg.height(), t("mod.config_btn"),
                         btn_font, enabled=row.get("has_config", False))
        has_link = row.get("has_link", False)
        link = geo["link"]
        link_cy = geo["link_cy"]
        if link.right() <= geo["text_right"]:
            painter.setFont(btn_font)
            painter.setPen(theme.color("ACCENT") if has_link else _LINK_DISABLED)
            painter.drawText(link, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, geo["link_text"])
            if has_link:
                underline_y = link_cy + QFontMetricsF(btn_font).height() / 2 - 1 * s
                painter.drawLine(QPointF(link.left(), underline_y), QPointF(link.right(), underline_y))
        folder = geo["folder"]
        if row.get("has_folder") and folder.right() <= geo["text_right"]:
            folder_icon = self._folder_icon(geo["folder_size"])
            if folder_icon is not None:
                dpr = folder_icon.devicePixelRatio()
                painter.drawPixmap(QPointF(folder.left(), link_cy - folder_icon.height() / dpr / 2), folder_icon)

    def _hit_test_grid(self, x: float, y: float):
        width = self.viewport().width()
        m = _Metrics(width)
        n = self._column_count
        line = int((y - m.pad_x) // m.row_step)
        if line < 0:
            return None
        gap = GRID_GAP * m.s
        cell_w = (width - 2 * m.pad_x - (n - 1) * gap) / n
        col = int((x - m.pad_x) // (cell_w + gap))
        if not 0 <= col < n:
            return None
        index = line * n + col
        if index >= len(self._rows):
            return None
        cell = self._cell_rect(m, width, index)
        if not cell.contains(QPointF(x, y)):
            return None
        row = self._rows[index]
        wid = row["workshop_id"]
        fonts = self._grid_fonts(m.s)
        geo = self._cell_geometry(m, cell, row, fonts)
        point = QPointF(x, y)
        name_fm, meta_fm = QFontMetricsF(fonts[0]), QFontMetricsF(fonts[1])
        name_rect, meta_rect = geo["name_rect"], geo["meta_rect"]
        name_text = self._elide(row["name"] or wid, name_fm, name_rect.width())
        if name_rect.contains(point) and x <= name_rect.left() + name_fm.horizontalAdvance(name_text) + 6 * m.s:
            return ("copy_name", wid)
        if meta_rect.contains(point) and x <= meta_rect.left() + meta_fm.horizontalAdvance(str(wid)) + 6 * m.s:
            return ("copy_id", wid)
        if geo["switch"].contains(point):
            if row.get("is_local"):
                return None
            if row.get("locked"):
                return ("locked", wid)
            return ("toggle", wid) if self._interactive else None
        if geo["cfg"].contains(point) and row.get("has_config"):
            return ("config", wid)
        visible_right = geo["text_right"]
        if row.get("has_link") and geo["link"].right() <= visible_right and geo["link"].contains(point):
            return ("link", wid)
        if row.get("has_folder") and geo["folder"].right() <= visible_right and geo["folder"].contains(point):
            return ("folder", wid)
        return None

    @staticmethod
    def _elide(text: str, metrics: QFontMetricsF, max_width: float) -> str:
        if not text:
            return ""
        if metrics.horizontalAdvance(text) <= max_width:
            return text
        while text and metrics.horizontalAdvance(text + "…") > max_width:
            text = text[:-1]
        return (text + "…") if text else "…"

    @staticmethod
    def _paint_badge(painter, x, cy, w, h, text, font) -> None:
        r = h / 2
        painter.setPen(QPen(theme.color("CARD_BORDER"), 1))
        painter.setBrush(theme.color("CARD_BG_ALT"))
        painter.drawRoundedRect(QRectF(x, cy - r, w, h), r, r)
        painter.setFont(font)
        painter.setPen(theme.color("TEXT_MUTED"))
        painter.drawText(QRectF(x, cy - r, w, h), Qt.AlignmentFlag.AlignCenter, text)

    @staticmethod
    def _paint_switch(painter, x, cy, w, h, on: bool, locked: bool = False) -> None:
        r = h / 2
        color = _CFG_DISABLED_BG if locked else (theme.color("PRIMARY") if on else _OFF_COLOR)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawRoundedRect(QRectF(x, cy - r, w, h), r, r)
        knob_r = r - 3
        knob_cx = x + w - r if on else x + r
        painter.setBrush(theme.color("CARD_BG"))
        painter.drawEllipse(QPointF(knob_cx, cy), knob_r, knob_r)

    @staticmethod
    def _paint_pill(painter, x, y, w, h, text, font, enabled: bool = True) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme.color("PRIMARY") if enabled else _CFG_DISABLED_BG)
        painter.drawRoundedRect(QRectF(x, y, w, h), h / 2, h / 2)
        painter.setFont(font)
        painter.setPen(theme.color("CARD_BG") if enabled else _CFG_DISABLED_FG)
        painter.drawText(QRectF(x, y, w, h), Qt.AlignmentFlag.AlignCenter, text)

    # ── 命中测试 ────────────────────────────────────────────────────────
    def _row_at(self, y: float):
        m = _Metrics(self.viewport().width())
        index = int((y - m.pad_x) // m.row_step)
        if not (0 <= index < len(self._rows)):
            return None, None, None
        top = m.pad_x + index * m.row_step
        if not (top <= y <= top + m.row_h):
            return None, None, None
        return index, self._rows[index], top

    def _hit_test(self, pos):
        """返回 (kind, workshop_id)，kind 取值 toggle/config/link/folder/copy_id/copy_name/locked。"""
        offset = self.verticalScrollBar().value()
        y = pos.y() + offset
        if self._column_count > 1:
            return self._hit_test_grid(pos.x(), y)
        width = self.viewport().width()
        m = _Metrics(width)
        cols = self._columns(m, width)
        _index, row, top = self._row_at(y)
        if row is None:
            return None
        wid = row["workshop_id"]
        x = pos.x()
        name_font, id_font, btn_font, _cfg_font = self._fonts(m.s)
        if cols["name_x"] <= x <= cols["name_x"] + cols["name_w"]:
            name_fm = QFontMetricsF(name_font)
            full_name = row["name"] or wid
            name_text = self._elide(full_name, name_fm, cols["name_w"])
            name_w = name_fm.horizontalAdvance(name_text)
            if top + m.row_h * 0.08 <= y <= top + m.row_h * 0.38 and x <= cols["name_x"] + name_w + 10 * m.s:
                return ("copy_name", wid)
            id_fm = QFontMetricsF(id_font)
            id_w = id_fm.horizontalAdvance(str(wid))
            if top + m.row_h * 0.4 <= y <= top + m.row_h * 0.68 and x <= cols["name_x"] + id_w + 10 * m.s:
                return ("copy_id", wid)
        if cols["switch_x1"] <= x <= cols["switch_x2"]:
            if row.get("is_local"):
                return None
            if row.get("locked"):
                return ("locked", wid)
            if self._interactive:
                return ("toggle", wid)
            return None
        if cols["cfg_x1"] <= x <= cols["cfg_x2"] and row.get("has_config"):
            return ("config", wid)
        # 命中范围贴合实际文字宽度（与 _paint_row 同一次量宽），文字右侧空白不可点
        link_text = t("mod.workshop_link_btn") if row.get("has_link") else t("mod.no_workshop_link")
        link_w = QFontMetricsF(btn_font).horizontalAdvance(link_text)
        if row.get("has_link") and cols["link_x1"] <= x <= cols["link_x1"] + link_w:
            return ("link", wid)
        folder_x = cols["link_x1"] + link_w + 10 * m.s
        if row.get("has_folder") and folder_x <= x <= folder_x + 22 * m.s:
            return ("folder", wid)
        return None

    def mouseMoveEvent(self, event):
        hit = self._hit_test(event.position())
        if hit and hit[0] == "locked":
            self.viewport().setCursor(Qt.CursorShape.ArrowCursor)
            self._locked_tip_pos = event.globalPosition().toPoint()
            if not self._locked_tip_timer.isActive():
                self._locked_tip_timer.start()
        else:
            self._locked_tip_timer.stop()
            QToolTip.hideText()
            if hit and hit[0] != "locked":
                self.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
            else:
                self.viewport().setCursor(Qt.CursorShape.ArrowCursor)

    def _show_locked_tip(self) -> None:
        if self._locked_tip_pos is not None:
            QToolTip.showText(self._locked_tip_pos, t("mod.locked_switch_hover"), self)

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        hit = self._hit_test(event.position())
        if hit is None or hit[0] == "locked":
            return
        kind, wid = hit
        signal = {
            "toggle": self.toggle_requested, "config": self.config_requested,
            "link": self.link_requested, "folder": self.folder_requested,
            "copy_id": self.copy_id_requested, "copy_name": self.copy_name_requested,
        }[kind]
        signal.emit(wid)
