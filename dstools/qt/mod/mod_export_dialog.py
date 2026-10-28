"""导出已启用 Mod 列表为图片：按当前主题绘制卡片网格，预览后可保存为 PNG 或复制到剪贴板。"""

import time
from pathlib import Path

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import (
    QColor, QFont, QFontMetrics, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap,
)
from PySide6.QtWidgets import QFileDialog, QFrame, QLabel, QPushButton, QScrollArea

from dstools.features.mod.export_list import ExportModEntry
from dstools.i18n import t
from dstools.qt import dialogs
from dstools.qt.imaging import pil_to_pixmap
from dstools.qt.mod.mod_panel import _DEFAULT_ICON_PATH
from dstools.qt.theme import theme
from dstools.qt.widgets import mod_format_tag_colors

# 以下尺寸均为逻辑像素，最终按 _RENDER_SCALE 倍输出，分享到聊天软件里放大也清楚。
_RENDER_SCALE = 2
_PAD = 28
_BANNER_H = 104
_BANNER_RADIUS = 20
_CARD_W = 360
_CARD_H = 74
_GAP = 14
_ICON = 54
_CARD_RADIUS = 14
_FRAME_PAD = 18  # 外框与卡片之间的内边距
_FRAME_RADIUS = 22
_SECTION_GAP = 18  # 横幅与卡片外框之间的间距


def _column_count(count: int) -> int:
    if count <= 5:
        return 1
    if count <= 16:
        return 2
    return 3


def _font(px: float, bold: bool = False) -> QFont:
    font = theme.panel_font(px)
    # 像素字体只有 Regular 字重，合成加粗会破坏像素对齐（同 theme.font 的处理）。
    font.setBold(bold and theme.font_style != "pixel")
    return font


def _alpha(key: str, alpha: int) -> QColor:
    color = theme.color(key)
    color.setAlpha(alpha)
    return color


def _icon_pixmap(image) -> QPixmap | None:
    if image is not None:
        pixmap = pil_to_pixmap(image)
    elif _DEFAULT_ICON_PATH.exists():
        pixmap = QPixmap(str(_DEFAULT_ICON_PATH))
    else:
        return None
    return None if pixmap.isNull() else pixmap


def _draw_soft_shadow(painter: QPainter, rect: QRectF, radius: float, offset_y: float = 2.0) -> None:
    """用几圈逐层变淡的圆角矩形模拟模糊阴影（QPainter 没有原生模糊）。"""
    painter.setPen(Qt.PenStyle.NoPen)
    for spread, alpha in ((6, 10), (4, 18), (2, 28), (1, 36)):
        painter.setBrush(_alpha("SHADOW", alpha))
        painter.drawRoundedRect(rect.adjusted(-spread, -spread + offset_y, spread, spread + offset_y),
                                radius + spread, radius + spread)


def _draw_pill(painter: QPainter, rect: QRectF, fill: QColor, text_color: QColor, font: QFont, text: str) -> None:
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(fill)
    painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
    painter.setFont(font)
    painter.setPen(text_color)
    painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)


def _draw_banner(painter: QPainter, rect: QRectF, title: str, chips: list[str]) -> None:
    """顶部主题浅色渐变横幅：深色标题 + 一排白底信息标签，右侧几个淡圆圈做点缀。"""
    path = QPainterPath()
    path.addRoundedRect(rect, _BANNER_RADIUS, _BANNER_RADIUS)
    # 从主题浅色过渡到"浅色混一点主色"，整体保持浅底，标题用深色字保证对比度。
    light, primary = theme.color("PRIMARY_LIGHT"), theme.color("PRIMARY")
    deeper = QColor(*(round(a * 0.7 + b * 0.3) for a, b in (
        (light.red(), primary.red()), (light.green(), primary.green()), (light.blue(), primary.blue()))))
    gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
    gradient.setColorAt(0.0, light)
    gradient.setColorAt(1.0, deeper)
    painter.save()
    painter.setClipPath(path)
    painter.fillPath(path, gradient)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(255, 255, 255, 70))
    painter.drawEllipse(QRectF(rect.right() - 150, rect.top() - 70, 220, 220))
    painter.setBrush(QColor(255, 255, 255, 50))
    painter.drawEllipse(QRectF(rect.right() - 260, rect.bottom() - 46, 120, 120))
    painter.restore()

    title_font, chip_font = _font(24, bold=True), _font(12)
    title_fm, chip_fm = QFontMetrics(title_font), QFontMetrics(chip_font)
    chip_h = chip_fm.height() + 8
    left = rect.left() + 24
    right = rect.right() - 24
    block_h = title_fm.height() + 12 + chip_h
    top = rect.top() + (rect.height() - block_h) / 2
    painter.setFont(title_font)
    painter.setPen(theme.color("HEADING"))
    painter.drawText(QRectF(left, top, right - left, title_fm.height()),
                     Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title)
    x = left
    chip_y = top + title_fm.height() + 12
    for text in chips:
        max_w = right - x
        if max_w < chip_h * 2:
            break
        text = chip_fm.elidedText(text, Qt.TextElideMode.ElideRight, int(max_w - 24))
        chip_w = chip_fm.horizontalAdvance(text) + 24
        _draw_pill(painter, QRectF(x, chip_y, chip_w, chip_h), QColor(255, 255, 255, 190),
                   theme.color("PRIMARY_DARK"), chip_font, text)
        x += chip_w + 8


def _draw_card(painter: QPainter, rect: QRectF, entry: ExportModEntry, icon, fonts: dict, number: int) -> None:
    """单张 Mod 卡片：阴影浮起，第一行名称 + 版本与 V1/V2 标签，第二行作者（左）与工坊 ID（右）。"""
    _draw_soft_shadow(painter, rect, _CARD_RADIUS)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(theme.color("CARD_BG"))
    painter.drawRoundedRect(rect, _CARD_RADIUS, _CARD_RADIUS)

    icon_rect = QRectF(rect.left() + 11, rect.top() + (rect.height() - _ICON) / 2, _ICON, _ICON)
    pixmap = _icon_pixmap(icon)
    if pixmap is not None:
        _draw_soft_shadow(painter, icon_rect, 10, offset_y=1.5)
        clip = QPainterPath()
        clip.addRoundedRect(icon_rect, 10, 10)
        painter.save()
        painter.setClipPath(clip)
        painter.drawPixmap(icon_rect, pixmap, QRectF(pixmap.rect()))
        painter.restore()

    # 序号压在图标左上角，只画数字：先描一圈卡片底色的边，压在图标上也看得清，方便聊天里说"第几个"。
    badge_font = fonts["badge"]
    number_path = QPainterPath()
    number_path.addText(icon_rect.left() - 4, icon_rect.top() - 4 + QFontMetrics(badge_font).ascent() * 0.75,
                        badge_font, str(number))
    painter.strokePath(number_path, QPen(theme.color("CARD_BG"), 4, Qt.PenStyle.SolidLine,
                                         Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    painter.fillPath(number_path, theme.color("PRIMARY_DARK"))

    name_font, tag_font, meta_font = fonts["name"], fonts["tag"], fonts["meta"]
    name_fm, tag_fm, meta_fm = QFontMetrics(name_font), QFontMetrics(tag_font), QFontMetrics(meta_font)
    text_left = icon_rect.right() + 13
    text_right = rect.right() - 14
    text_w = text_right - text_left
    line_gap = 7
    block_h = name_fm.height() + line_gap + meta_fm.height()
    name_y = rect.top() + (rect.height() - block_h) / 2
    meta_y = name_y + name_fm.height() + line_gap

    # 版本号与 V1/V2 做成小标签跟在名称后（V1/V2 与 Mod 页列表同款配色）；名称过长时先省略名称，保证标签完整。
    version = entry.version.lstrip("vV")
    tag_text = t("mod.export_version", version=version) if version else ""
    tag_w = tag_fm.horizontalAdvance(tag_text) + 14 if tag_text else 0
    fmt_w = tag_fm.horizontalAdvance(entry.mod_format) + 14 if entry.mod_format else 0
    name_max = int(text_w - sum(w + 8 for w in (tag_w, fmt_w) if w))
    name = name_fm.elidedText(entry.name, Qt.TextElideMode.ElideRight, max(20, name_max))
    painter.setFont(name_font)
    painter.setPen(theme.color("HEADING"))
    painter.drawText(QRectF(text_left, name_y, name_max, name_fm.height()),
                     Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)
    tag_h = tag_fm.height() + 4
    tag_x = text_left + min(name_fm.horizontalAdvance(name), name_max) + 8
    tag_y = name_y + (name_fm.height() - tag_h) / 2
    if tag_text:
        _draw_pill(painter, QRectF(tag_x, tag_y, tag_w, tag_h), theme.color("PRIMARY_LIGHT"),
                   theme.color("PRIMARY_DARK"), tag_font, tag_text)
        tag_x += tag_w + 8
    if entry.mod_format:
        background, color = mod_format_tag_colors(entry.mod_format)
        _draw_pill(painter, QRectF(tag_x, tag_y, fmt_w, tag_h), background, color, tag_font, entry.mod_format)

    # 工坊 ID 靠右，作者占剩余宽度，两者不重叠。
    id_text = f"ID {entry.id_text}" if entry.id_text else t("mod.export_non_workshop")
    id_w = meta_fm.horizontalAdvance(id_text)
    painter.setFont(meta_font)
    painter.setPen(_alpha("TEXT_MUTED", 170))
    painter.drawText(QRectF(text_right - id_w, meta_y, id_w, meta_fm.height()),
                     Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, id_text)
    if entry.author:
        author_w = int(text_w - id_w - 12)
        painter.setPen(theme.color("TEXT_MUTED"))
        painter.drawText(QRectF(text_left, meta_y, author_w, meta_fm.height()),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         meta_fm.elidedText(entry.author, Qt.TextElideMode.ElideRight, author_w))


def render_mod_list_image(entries: list[ExportModEntry], icon_images: dict, title: str, subtitle: str) -> QImage:
    """把条目画成一张图片（界面线程调用）。subtitle 为"存档 · 世界 · 数量 · 时间"，从右拆分（只有存档名可能含" · "）。"""
    chips = [part for part in subtitle.rsplit(" · ", 3) if part.strip()]
    cols = _column_count(len(entries))
    rows = max(1, (len(entries) + cols - 1) // cols)
    fonts = {"name": _font(15, bold=True), "tag": _font(11), "meta": _font(12), "badge": _font(14, bold=True)}
    footer_font = _font(11)
    footer_h = QFontMetrics(footer_font).height() + 14
    # 只有一列时卡片加宽到两列的宽度：图片太窄时预览/分享里文字会显得特别大。
    card_w = _CARD_W * 2 + _GAP if cols == 1 else _CARD_W
    grid_w = cols * card_w + (cols - 1) * _GAP
    grid_h = rows * _CARD_H + (rows - 1) * _GAP
    content_w = grid_w + _FRAME_PAD * 2
    width = _PAD * 2 + content_w
    height = _PAD * 2 + _BANNER_H + _SECTION_GAP + _FRAME_PAD * 2 + grid_h + footer_h

    image = QImage(width * _RENDER_SCALE, height * _RENDER_SCALE, QImage.Format.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(_RENDER_SCALE)
    image.fill(theme.color("BG_SOFT"))
    painter = QPainter(image)
    painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
                           | QPainter.RenderHint.TextAntialiasing)

    _draw_banner(painter, QRectF(_PAD, _PAD, content_w, _BANNER_H), title, chips)

    # 全部卡片外面套一圈圆角外框，浅底色把卡片区和横幅分开。
    frame_top = _PAD + _BANNER_H + _SECTION_GAP
    frame = QRectF(_PAD + 1, frame_top + 1, content_w - 2, grid_h + _FRAME_PAD * 2 - 2)
    painter.setPen(QPen(theme.color("CARD_BORDER"), 1.5))
    painter.setBrush(theme.color("CARD_BG_ALT"))
    painter.drawRoundedRect(frame, _FRAME_RADIUS, _FRAME_RADIUS)

    top = frame_top + _FRAME_PAD
    for index, entry in enumerate(entries):
        # 按行优先排：从左到右、从上到下，与序号顺序一致。
        row, col = divmod(index, cols)
        x = _PAD + _FRAME_PAD + col * (card_w + _GAP)
        y = top + row * (_CARD_H + _GAP)
        _draw_card(painter, QRectF(x, y, card_w, _CARD_H), entry, icon_images.get(entry.workshop_id), fonts,
                   index + 1)

    painter.setFont(footer_font)
    painter.setPen(theme.color("TEXT_MUTED"))
    painter.drawText(QRectF(_PAD, height - _PAD - footer_h + 14, width - _PAD * 2, footer_h - 14),
                     Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, t("mod.export_footer"))
    painter.end()
    return image


class ModListImageDialog(dialogs.Dialog):
    """导出图片预览：预览按窗口宽度缩放，保存/复制始终用原始分辨率。"""

    def __init__(self, parent, image: QImage, default_name: str):
        super().__init__(parent, t("mod.export_preview_title"), width="lg")
        self._image = image
        self._default_name = default_name
        self._preview = QLabel()
        self._preview.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self._area = QScrollArea()
        self._area.setFrameShape(QFrame.Shape.NoFrame)
        self._area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._area.setWidgetResizable(True)
        self._area.setWidget(self._preview)
        self.body.addWidget(self._area, 1)

        close = dialogs.style_button(QPushButton(t("dlg.close_btn")), "secondary")
        close.clicked.connect(self.reject)
        copy = dialogs.style_button(QPushButton(t("mod.export_copy_btn")), "secondary")
        copy.clicked.connect(self._copy)
        save = QPushButton(t("mod.export_save_btn"))
        save.clicked.connect(self._save)
        save.setDefault(True)
        self.add_footer([close], [copy, save])

        logical_w = image.width() / image.devicePixelRatio()
        logical_h = image.height() / image.devicePixelRatio()
        dialogs.fit_to_screen(self, int(logical_w) + 64, int(logical_h) + 120)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_preview()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # 构造时布局尚未生效、滚动区还很窄，显示后按实际宽度再算一次
        QTimer.singleShot(0, self._update_preview)

    def _update_preview(self) -> None:
        # 宽度始终扣掉竖向滚动条：滚动条出现/消失不会改变预览宽度，避免来回重排。
        avail = self._area.width() - self._area.verticalScrollBar().sizeHint().width() - 2
        if avail <= 0:
            return
        dpr = self.devicePixelRatioF()
        # 铺满可用宽度，但不超过图片自身的逻辑宽度：放大后 Mod 少时文字会大得突兀。
        target_w = min(avail, self._image.width() / self._image.devicePixelRatio())
        # 按屏幕缩放比缩到物理像素再设 devicePixelRatio，避免先缩到逻辑尺寸再被二次放大。
        scaled = self._image.scaledToWidth(max(1, round(target_w * dpr)), Qt.TransformationMode.SmoothTransformation)
        pixmap = QPixmap.fromImage(scaled)
        pixmap.setDevicePixelRatio(dpr)
        self._preview.setPixmap(pixmap)

    def _copy(self) -> None:
        QGuiApplication.clipboard().setImage(self._image)
        dialogs.show_toast(self, t("mod.export_copied_toast"))

    def _save(self) -> None:
        start = str(Path.home() / "Desktop" / self._default_name)
        path, _filter = QFileDialog.getSaveFileName(self, t("mod.export_save_btn"), start, "PNG (*.png)")
        if not path:
            return
        if not path.lower().endswith(".png"):
            path += ".png"
        if not self._image.save(path, "PNG"):
            dialogs.show_error(self, t("mod.export_preview_title"), t("mod.export_save_failed", path=path))
            return
        dialogs.show_file_location(self, t("mod.export_preview_title"), path,
                                   t("mod.export_saved_location"), "")


def default_file_name(cluster_label: str) -> str:
    safe = "".join("_" if ch in '\\/:*?"<>|' else ch for ch in cluster_label).strip() or "mods"
    return f"{safe}_mods_{time.strftime('%Y%m%d_%H%M')}.png"


def export_running_mods_image(parent, proc, mod_ids, cluster_label: str) -> None:
    """控制台"Mod 全部加载成功"横幅的点击入口：按专服实际加载的 Mod 生成图片并预览。

    解析与图标转换放后台，完成前给出提示，避免重复点击时连开多份。"""
    from dstools.features.local_service.shard_helpers import find_process_mod_folders
    from dstools.features.mod.export_list import collect_export_data
    from dstools.models import Platform
    from dstools.qt.threads import run_async

    if getattr(parent, "_mod_export_running", False):
        return
    parent._mod_export_running = True
    ids = tuple(mod_ids)

    def work():
        return collect_export_data(ids, find_process_mod_folders(proc, ids), Platform.STEAM)

    def done(result) -> None:
        parent._mod_export_running = False
        entries, icons = result
        if not entries:
            dialogs.show_info(parent, t("mod.export_preview_title"), t("mod.export_empty"))
            return
        subtitle = t("mod.export_subtitle", cluster=cluster_label, shard=proc.shard_name,
                     count=len(entries), time=time.strftime("%Y-%m-%d %H:%M"))
        image = render_mod_list_image(entries, icons, t("mod.export_title"), subtitle)
        ModListImageDialog(parent, image, default_file_name(cluster_label)).exec()

    def error(exc: Exception) -> None:
        parent._mod_export_running = False
        dialogs.show_error(parent, t("mod.export_preview_title"), str(exc))

    run_async(work, done, error)
