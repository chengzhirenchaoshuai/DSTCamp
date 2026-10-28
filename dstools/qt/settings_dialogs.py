"""菜单触发的次级设置/信息弹窗：背景图、字体样式、Windows Defender 排除项、缓存目录、关于。"""

import os
import webbrowser
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import (
    QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSizePolicy, QSlider,
    QStyle, QStyleOptionSlider, QVBoxLayout, QWidget,
)

from dstools import __version__
from dstools.i18n import t
from dstools.qt import dialogs
from dstools.qt.theme import FONT_SIZE_LEVELS, freetype_engine_active, theme
from dstools.qt.threads import run_async
from dstools.qt.widgets import ToggleSwitch, section_card
from dstools.shared.discovery import auto_klei_root, list_clusters
from dstools.shared.app_settings import (
    get_custom_bg_opacity, get_klei_root_override, get_lite_mode_enabled, get_remind_update_enabled,
    set_cache_dir_override,
    set_klei_root_override,
    set_custom_bg_opacity, set_remind_update_enabled,
)
from dstools.shared.custom_background import (
    clear_custom_bg_image, get_custom_bg_path, set_custom_bg_image,
)
from dstools.shared.font_styles import FONT_FAMILY_BY_STYLE, FONT_STYLE_NAMES
from dstools.shared.resource_paths import cache_root_dir, default_cache_root_dir, validate_cache_root
from dstools.shared.update_check import check_latest_release, is_newer_version
from dstools.shared.windows_defender import (
    DefenderState, change_defender_exclusion, check_defender_exclusion,
    check_defender_exclusion_elevated, defender_target_is_safe, resolve_defender_targets,
)

_PREVIEW_FONT_SIZE = 12  # 预览行文字偏长，固定字号，不跟随当前字体样式的放大系数


class BackgroundImageDialog(dialogs.Dialog):
    """背景图是跟颜色主题解耦的全局功能：选完图片后不管当前是哪套颜色主题都会
    叠加显示。选图/清除/拖不透明度都是选完/拖完立刻生效，不需要额外的"保存"。"""

    def __init__(self, window):
        super().__init__(window, t("settings.custom_bg_title"), 360)
        self._window = window
        path = get_custom_bg_path()
        self._status = self.text_label(path.name if path else t("settings.custom_bg_none"))
        self.body.addWidget(self._status)
        if get_lite_mode_enabled():
            # 轻量模式不加载背景图，这里照常可选，关掉轻量模式后才显示
            self.body.addWidget(self.text_label(t("settings.custom_bg_lite_hint"), muted=True))

        row1 = QHBoxLayout()
        choose_btn = QPushButton(t("settings.custom_bg_choose"))
        choose_btn.clicked.connect(self._on_choose)
        clear_btn = dialogs.style_button(QPushButton(t("settings.custom_bg_clear")), "secondary")
        clear_btn.clicked.connect(self._on_clear)
        row1.addWidget(choose_btn)
        row1.addWidget(clear_btn)
        row1.addStretch()
        self.body.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(self.text_label(t("settings.custom_bg_opacity_label")))
        self._slider = _JumpSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(0, 100)
        self._slider.setValue(round(get_custom_bg_opacity() * 100))
        self._slider.setFixedWidth(160)
        self._slider.valueChanged.connect(self._on_opacity_change)
        self._slider.sliderReleased.connect(self._on_opacity_release)
        row2.addWidget(self._slider)
        row2.addStretch()
        self.body.addLayout(row2)

        self.add_buttons()

    def _on_choose(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self, t("settings.custom_bg_choose"), "",
            f"{t('settings.custom_bg_filetypes')} (*.png *.jpg *.jpeg *.bmp *.gif)")
        if not path_str:
            return
        set_custom_bg_image(Path(path_str))
        self._status.setText(Path(path_str).name)
        self._refresh_bg()

    def _on_clear(self) -> None:
        clear_custom_bg_image()
        self._status.setText(t("settings.custom_bg_none"))
        self._refresh_bg()

    def _on_opacity_change(self, value: int) -> None:
        # 绘制时现读 opacity，无需节流；松开滑块时再持久化
        self._window.background.opacity = value / 100
        self._window.text_halo.refresh()  # 拉到 0 时卸掉文字描边，跨过明暗临界点时整窗换深浅字
        self._window.update()
        if not self._slider.isSliderDown():  # 点击、键盘直接改值时立即持久化，拖动中等松开
            self._on_opacity_release()

    def _on_opacity_release(self) -> None:
        set_custom_bg_opacity(self._slider.value() / 100)

    def _refresh_bg(self) -> None:
        self._window.background.reload()
        self._window.text_halo.refresh()
        self._window.update()


class _JumpSlider(QSlider):
    """点击滑槽直接跳到点击位置并可接着拖动（默认只按页步进一点）。"""

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            option = QStyleOptionSlider()
            self.initStyleOption(option)
            style = self.style()
            groove = style.subControlRect(QStyle.ComplexControl.CC_Slider, option,
                                          QStyle.SubControl.SC_SliderGroove, self)
            handle = style.subControlRect(QStyle.ComplexControl.CC_Slider, option,
                                          QStyle.SubControl.SC_SliderHandle, self)
            if not handle.contains(event.position().toPoint()):
                span = groove.width() - handle.width()
                pos = round(event.position().x()) - groove.x() - handle.width() // 2
                self.setValue(QStyle.sliderValueFromPosition(
                    self.minimum(), self.maximum(), pos, max(1, span), option.upsideDown))
        super().mousePressEvent(event)  # 此时手柄已在指针下，交给默认处理即开始拖动


class _FontChoiceCard(QWidget):
    """字体选择卡片：上面用该字体写字体名，下面用该字体写一行示例；选中时主题色描边 + 右上角勾，
    悬停时浅色底。三张卡片同样大小。"""

    clicked = Signal(str)

    _SAMPLE = "饥荒 Aa 123"

    def __init__(self, style: str):
        super().__init__()
        self._style = style
        theme.ensure_font_registered(style)  # 预览要用该字体绘制，启动时只注册了当前样式的字体
        self._selected = False
        self._hover = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setMinimumSize(150, 86)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self.update()

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
            self.clicked.emit(self._style)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        background = theme.color("PRIMARY_LIGHT") if (self._selected or self._hover) else theme.color("CARD_BG")
        if not self._selected and self._hover:
            background.setAlpha(140)
        painter.setBrush(background)
        painter.setPen(QPen(theme.color("PRIMARY" if self._selected else "CARD_BORDER"), 2 if self._selected else 1))
        painter.drawRoundedRect(rect, 10, 10)

        family = FONT_FAMILY_BY_STYLE[self._style]
        # 麦圆体在 DirectWrite 默认 hinting 下笔画粗细不均，与 theme.apply_style_hints() 一致禁用。
        hinting = (QFont.HintingPreference.PreferNoHinting if self._style == "cute"
                   else QFont.HintingPreference.PreferDefaultHinting)
        name_font = QFont(family, theme.palette["FONT_SIZE_MD"])
        name_font.setHintingPreference(hinting)
        name_font.setBold(True)
        painter.setFont(name_font)
        painter.setPen(theme.color("TEXT"))
        painter.drawText(rect.adjusted(12, 10, -28, -rect.height() / 2),
                         int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                         t(f"settings.font_style_{self._style}"))
        sample_font = QFont(family, theme.palette["FONT_SIZE_BASE"])
        sample_font.setHintingPreference(hinting)
        painter.setFont(sample_font)
        painter.setPen(theme.color("TEXT_MUTED"))
        painter.drawText(rect.adjusted(12, rect.height() / 2, -12, -8),
                         int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), self._SAMPLE)
        if self._selected:
            badge = QRectF(rect.right() - 24, rect.top() + 8, 16, 16)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(theme.color("PRIMARY"))
            painter.drawEllipse(badge)
            pen = QPen(QColor("white"), 2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawPolyline([QPointF(badge.left() + 4, badge.center().y()),
                                  QPointF(badge.left() + 7, badge.bottom() - 4.5),
                                  QPointF(badge.right() - 3.5, badge.top() + 5)])


class FontSettingsDialog(dialogs.Dialog):
    """字体设置：三种字体样式做成同样大小的卡片，卡片里直接用该字体渲染（所见即所选）。
    点卡片立即生效便于对比；"取消"恢复成打开窗口前的字体，"确定"保留当前选择。"""

    def __init__(self, window):
        super().__init__(window, t("settings.font_settings_title"), "md")
        self._original_style = theme.font_style
        self._original_level = theme.font_size_level
        self._cards: dict[str, _FontChoiceCard] = {}
        self.body.addWidget(self.heading_label(t("settings.font_choose_label")))
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        for column, style in enumerate(FONT_STYLE_NAMES):
            card = _FontChoiceCard(style)
            card.set_selected(style == theme.font_style)
            card.clicked.connect(self._on_select)
            grid.addWidget(card, 0, column)
            grid.setColumnStretch(column, 1)  # 三张卡片等宽
            self._cards[style] = card
        self.body.addLayout(grid)
        # 字体引擎只能在启动时选（像素字体用 FreeType，其它用系统 DirectWrite），当前
        # 样式与引擎不匹配时提示重启，见 app.py。
        self._restart_hint = self.text_label(t("settings.font_restart_hint"), muted=True, size_key="FONT_SIZE_XS")
        self.body.addWidget(self._restart_hint)
        self._refresh_restart_hint()

        # 字体大小档位：缩放系数作用于全部字体样式，像素字体接近 12 整数倍物理像素时再吸附。
        self.body.addWidget(self.heading_label(t("settings.font_size_label")))
        level_row = QHBoxLayout()
        self._level_buttons: dict[str, QPushButton] = {}
        for level_key, _scale in FONT_SIZE_LEVELS:
            btn = QPushButton(t(f"settings.font_size_{level_key}"))
            btn.setCheckable(True)
            btn.setChecked(level_key == theme.font_size_level)
            # 选中档位用实心 PRIMARY、未选中用 secondary 透明描边，一眼看出当前大小。
            btn.setProperty("variant", "" if level_key == theme.font_size_level else "secondary")
            btn.clicked.connect(lambda _checked, key=level_key: self._on_select_level(key))
            level_row.addWidget(btn)
            self._level_buttons[level_key] = btn
        self.body.addLayout(level_row)

        preview_card, preview_layout = section_card(t("settings.font_preview_title"))
        self._preview = QLabel(t("settings.font_preview_text"))
        self._preview.setWordWrap(True)
        preview_layout.addWidget(self._preview)
        self._refresh_preview()
        self.body.addSpacing(4)
        self.body.addWidget(preview_card)

        self.add_buttons()

    def _on_select(self, style: str) -> None:
        theme.set_font_style(style)
        for name, card in self._cards.items():
            card.set_selected(name == style)
        self._refresh_preview()
        self._refresh_restart_hint()

    def _refresh_restart_hint(self) -> None:
        self._restart_hint.setVisible((theme.font_style == "pixel") != freetype_engine_active())

    def _on_select_level(self, level_key: str) -> None:
        theme.set_font_size_level(level_key)
        for key, btn in self._level_buttons.items():
            btn.setChecked(key == level_key)
            btn.setProperty("variant", "" if key == level_key else "secondary")
            btn.style().unpolish(btn)
            btn.style().polish(btn)
        self._refresh_preview()

    def _refresh_preview(self) -> None:
        if theme.font_style == "pixel":
            # 像素字体要按 12 整数倍吸附并配套抗锯齿策略，用正文字号预览才与实际一致。
            self._preview.setFont(theme.font("FONT_SIZE_BASE"))
            return
        px = max(6, round(_PREVIEW_FONT_SIZE * theme.font_size_scale))
        font = QFont(theme.font_family, px)
        theme.apply_style_hints(font)
        self._preview.setFont(font)

    def reject(self) -> None:
        # 取消：恢复打开窗口时的字体与字号档位
        if theme.font_style != self._original_style:
            theme.set_font_style(self._original_style)
        if theme.font_size_level != self._original_level:
            theme.set_font_size_level(self._original_level)
        super().reject()


_DEFENDER_TARGET_LABEL_KEYS = {
    "file": "settings.defender_target_file",
    "legacy_zip_folder": "settings.defender_target_legacy_zip_folder",
    "runtime_tools": "settings.defender_target_runtime_tools",
    "on_demand_tools": "settings.defender_target_on_demand_tools",
    "temp_wildcard": "settings.defender_target_temp_wildcard",
}

_DEFENDER_STATE_LABELS = {
    "excluded": ("settings.defender_excluded", "ACCENT"),
    "not_excluded": ("settings.defender_not_excluded", "TEXT"),
    "partial": ("settings.defender_partial", "TEXT"),
    "unknown": ("settings.defender_unknown", "TEXT_MUTED"),
    "unavailable": ("settings.defender_unavailable", "TEXT_MUTED"),
    "cancelled": ("settings.defender_check_cancelled", "TEXT_MUTED"),
    "unsafe": ("settings.defender_unsafe_target", "ERROR"),
    "error": ("settings.defender_check_failed", "ERROR"),
}


def _defender_aggregate_state(states: list[DefenderState]) -> DefenderState:
    """把每个目标各自的状态合并成一个用于展示/控制按钮的整体状态——只要有一个
    目标状态不确定，整体就不确定；全部确定但结果不一致才归到 partial。"""
    if not states:
        return DefenderState("unavailable")
    statuses = {state.status for state in states}
    if statuses == {"excluded"}:
        return DefenderState("excluded")
    if statuses == {"not_excluded"}:
        return DefenderState("not_excluded")
    for uncertain in ("error", "cancelled", "unavailable", "unknown"):
        if uncertain in statuses:
            return next(state for state in states if state.status == uncertain)
    return DefenderState("partial")


class WindowsDefenderDialog(dialogs.Dialog):
    """"Windows Defender 排除项"：可检测、可撤销、须经用户确认，只针对最小范围的已核对目标。"""

    def __init__(self, window):
        super().__init__(window, t("settings.defender_title"), 660)
        self._targets = resolve_defender_targets()
        self._targets_are_safe = bool(self._targets) and all(
            defender_target_is_safe(target) for target in self._targets)
        self._busy = False
        self._state: DefenderState | None = None

        heading = QLabel(t("settings.defender_heading"))
        heading.setFont(theme.font("FONT_SIZE_LG", bold=True))
        self.body.addWidget(heading)
        self.body.addWidget(self.text_label(t("settings.defender_intro"), muted=True, size_key="FONT_SIZE_SM"))

        target_label = QLabel(t("settings.defender_target_label"))
        target_label.setFont(theme.font("FONT_SIZE_BASE", bold=True))
        self.body.addWidget(target_label)
        targets_layout = QVBoxLayout()
        if self._targets:
            for target in self._targets:
                row = QHBoxLayout()
                kind_label = QLabel(t(_DEFENDER_TARGET_LABEL_KEYS.get(target.kind, "settings.defender_target_file")))
                kind_label.setFont(theme.font("FONT_SIZE_SM"))
                kind_label.setProperty("muted", True)
                kind_label.setFixedWidth(90)
                path_edit = QLineEdit(str(target.path))
                path_edit.setReadOnly(True)
                row.addWidget(kind_label)
                row.addWidget(path_edit, 1)
                targets_layout.addLayout(row)
        else:
            empty_edit = QLineEdit("—")
            empty_edit.setReadOnly(True)
            targets_layout.addWidget(empty_edit)
        self.body.addLayout(targets_layout)

        scope_key = ("settings.defender_folder_scope"
                    if len(self._targets) == 1 and self._targets[0].kind == "legacy_zip_folder"
                    else "settings.defender_file_scope" if self._targets else "settings.defender_source_scope")
        self.body.addWidget(self.text_label(t(scope_key), muted=True, size_key="FONT_SIZE_SM"))

        self._status = self.text_label("", size_key="FONT_SIZE_BASE")
        self._status.setFont(theme.font("FONT_SIZE_BASE", bold=True))
        self.body.addWidget(self._status)
        warning = self.text_label(t("settings.defender_warning"), size_key="FONT_SIZE_SM")
        warning.setStyleSheet(f"color: {theme.hex('ERROR')};")
        self.body.addWidget(warning)

        actions = QHBoxLayout()
        self._add_button = QPushButton(t("settings.defender_add"))
        self._add_button.clicked.connect(lambda: self._start_change(True))
        self._remove_button = QPushButton(t("settings.defender_remove"))
        self._remove_button.clicked.connect(lambda: self._start_change(False))
        self._refresh_button = QPushButton(t("settings.defender_refresh"))
        self._refresh_button.clicked.connect(
            lambda: self._start_check(elevated=bool(self._state and self._state.status in {"unknown", "cancelled"})))
        close_button = dialogs.style_button(QPushButton(t("dlg.close_btn")), "secondary")
        close_button.clicked.connect(self.accept)
        actions.addWidget(self._add_button)
        actions.addWidget(self._remove_button)
        actions.addWidget(self._refresh_button)
        actions.addStretch()
        actions.addWidget(close_button)
        self.body.addLayout(actions)

        if not self._targets:
            self._show_state(DefenderState("unavailable"))
            self._status.setText(t("settings.defender_source_unavailable"))
        elif not self._targets_are_safe:
            self._show_state(DefenderState("unsafe"))
        else:
            self._start_check()

    # ── 状态展示 ────────────────────────────────────────────────────────
    def _show_state(self, state: DefenderState) -> None:
        self._state = state
        key, color_key = _DEFENDER_STATE_LABELS[state.status]
        self._status.setText(t(key, error=state.detail))
        self._status.setStyleSheet(f"color: {theme.hex(color_key)};")
        self._set_buttons()

    def _set_buttons(self) -> None:
        state = self._state
        actionable = self._targets_are_safe and not self._busy
        self._add_button.setEnabled(bool(actionable and state and state.status in {"not_excluded", "partial"}))
        self._remove_button.setEnabled(bool(actionable and state and state.status in {"excluded", "partial"}))
        self._refresh_button.setEnabled(actionable)
        self._refresh_button.setText(
            t("settings.defender_admin_check") if state and state.status in {"unknown", "cancelled"}
            else t("settings.defender_refresh"))

    # ── 检测 ────────────────────────────────────────────────────────────
    def _start_check(self, *, elevated: bool = False) -> None:
        if not self._targets_are_safe or self._busy:
            return
        self._busy = True
        self._status.setText(t("settings.defender_checking"))
        self._status.setStyleSheet(f"color: {theme.hex('TEXT_MUTED')};")
        self._set_buttons()
        targets = self._targets

        def work():
            check = check_defender_exclusion_elevated if elevated else check_defender_exclusion
            return _defender_aggregate_state(check(targets))

        def done(state: DefenderState) -> None:
            self._busy = False
            self._show_state(state)
            if state.status == "unknown" and not elevated:
                # 普通权限查不准，直接自动转去提权检测、弹 UAC，不用用户再多点一次。
                self._start_check(elevated=True)

        def error(exc: Exception) -> None:
            self._busy = False
            self._show_state(DefenderState("error", detail=str(exc)))

        run_async(work, done, error)

    # ── 修改 ────────────────────────────────────────────────────────────
    def _start_change(self, enabled: bool) -> None:
        if not self._targets_are_safe or self._busy:
            return
        confirm_key = "settings.defender_confirm_add" if enabled else "settings.defender_confirm_remove"
        joined_paths = "\n".join(str(target.path) for target in self._targets)
        if not dialogs.ask_yes_no(self, t("settings.defender_title"), t(confirm_key, path=joined_paths),
                                  min_width=620):
            return
        self._busy = True
        self._status.setText(t("settings.defender_adding" if enabled else "settings.defender_removing"))
        self._status.setStyleSheet(f"color: {theme.hex('TEXT_MUTED')};")
        self._set_buttons()
        targets = self._targets

        def work():
            return change_defender_exclusion(targets, enabled=enabled)

        def done(changed) -> None:
            self._busy = False
            expected = "excluded" if enabled else "not_excluded"
            state = DefenderState(expected) if changed.success else None
            if changed.success and state is not None:
                self._show_state(state)
                dialogs.show_info(self, t("settings.defender_title"),
                                  t("settings.defender_add_done" if enabled else "settings.defender_remove_done"))
                return
            self._show_state(self._state or DefenderState("error"))
            key = "settings.defender_uac_cancelled" if changed.cancelled else "settings.defender_change_failed"
            dialogs.show_error(self, t("settings.defender_title"), t(key, error=changed.detail))

        def error(exc: Exception) -> None:
            self._busy = False
            self._show_state(self._state or DefenderState("error"))
            dialogs.show_error(self, t("settings.defender_title"),
                               t("settings.defender_change_failed", error=str(exc)))

        run_async(work, done, error)


class CacheDirDialog(dialogs.Dialog):
    """"缓存目录"：展示当前路径并提供更改、恢复默认、打开目录；更改后可选择立即重启。"""

    def __init__(self, window):
        super().__init__(window, t("settings.cache_dir_label"), 620)
        heading = QLabel(t("settings.cache_dir_current"))
        heading.setFont(theme.font("FONT_SIZE_BASE", bold=True))
        self.body.addWidget(heading)
        self._path_edit = QLineEdit(str(cache_root_dir()))
        self._path_edit.setReadOnly(True)
        self._path_edit.setFont(theme.font("FONT_SIZE_SM"))
        self.body.addWidget(self._path_edit)
        self.body.addWidget(self.text_label(t("settings.cache_dir_restart_hint"), muted=True, size_key="FONT_SIZE_SM"))

        actions = QHBoxLayout()
        change_btn = QPushButton(t("settings.cache_dir_change"))
        change_btn.clicked.connect(self._on_change)
        self._reset_btn = QPushButton(t("settings.cache_dir_reset"))
        self._reset_btn.clicked.connect(self._on_reset)
        open_btn = QPushButton(t("settings.cache_dir_open"))
        open_btn.clicked.connect(self._on_open)
        close_btn = dialogs.style_button(QPushButton(t("dlg.close_btn")), "secondary")
        close_btn.clicked.connect(self.accept)
        actions.addStretch()
        actions.addWidget(change_btn)
        actions.addWidget(self._reset_btn)
        actions.addWidget(open_btn)
        actions.addSpacing(12)
        actions.addWidget(close_btn)
        self.body.addLayout(actions)
        self._refresh_reset_enabled()

    def _refresh_path(self) -> None:
        self._path_edit.setText(str(cache_root_dir()))
        self._refresh_reset_enabled()

    def _refresh_reset_enabled(self) -> None:
        self._reset_btn.setEnabled(cache_root_dir() != default_cache_root_dir())

    def _on_change(self) -> None:
        current = cache_root_dir()
        initial = current if current.is_dir() else current.parent
        if not initial.is_dir():
            initial = Path.home()
        while True:
            chosen = QFileDialog.getExistingDirectory(self, t("settings.cache_dir_picker_title"), str(initial))
            if not chosen:
                return
            path = Path(chosen)
            reason = validate_cache_root(path)
            if reason is not None:
                self._show_cache_dir_error(reason, path)
                if path.is_dir():
                    initial = path
                continue
            set_cache_dir_override(path)
            self._refresh_path()
            self._prompt_restart(t("settings.cache_dir_changed", path=str(path)))
            return

    def _on_reset(self) -> None:
        target = default_cache_root_dir()
        if cache_root_dir() == target:
            dialogs.show_info(self, t("settings.cache_dir_label"),
                              t("settings.cache_dir_already_default", path=str(target)))
            return
        reason = validate_cache_root(target)
        if reason is not None:
            key = {"non_ascii": "settings.cache_dir_default_non_ascii",
                  "not_writable": "settings.cache_dir_default_not_writable"}.get(reason)
            if key is not None:
                dialogs.show_warning(self, t("settings.cache_dir_label"), t(key, path=str(target)))
            else:
                self._show_cache_dir_error(reason, target)
            return
        set_cache_dir_override(None)
        self._refresh_path()
        self._prompt_restart(t("settings.cache_dir_default_restored", path=str(target)))

    def _on_open(self) -> None:
        current = cache_root_dir()
        current.mkdir(parents=True, exist_ok=True)
        os.startfile(str(current))

    def _show_cache_dir_error(self, reason: str, path: Path) -> None:
        key = {"not_absolute": "settings.cache_dir_not_absolute",
              "non_ascii": "settings.cache_dir_non_ascii",
              "not_writable": "settings.cache_dir_not_writable"}.get(reason, "settings.cache_dir_not_writable")
        dialogs.show_error(self, t("settings.cache_dir_label"), t(key, path=str(path)))

    def _prompt_restart(self, message: str) -> None:
        choice = dialogs.ask_choice(
            self, t("settings.cache_dir_label"), message,
            [(t("settings.restart_now"), "restart"), (t("dlg.cancel_btn"), "cancel")],
            default="restart", min_width=520)
        if choice == "restart":
            window = self.parent()
            self.accept()
            if hasattr(window, "restart_app"):
                window.restart_app()


class KleiRootDialog(dialogs.Dialog):
    """"服务器存档目录"：展示当前目录（自动找到或手动指定），可更改、恢复自动、打开目录，改完立即重新扫描。
    只影响根目录下的服务器存档；游戏客户端的账号和本地存档始终在默认位置（见 DSTEnvironment.client_klei_root）。"""

    def __init__(self, window):
        super().__init__(window, t("settings.klei_root_label"), 620)
        self._window = window
        heading = QLabel(t("settings.klei_root_current"))
        heading.setFont(theme.font("FONT_SIZE_BASE", bold=True))
        self.body.addWidget(heading)
        self._path_edit = QLineEdit()
        self._path_edit.setReadOnly(True)
        self._path_edit.setFont(theme.font("FONT_SIZE_SM"))
        self.body.addWidget(self._path_edit)
        self._source_label = self.text_label("", muted=True, size_key="FONT_SIZE_SM")
        self.body.addWidget(self._source_label)
        self.body.addWidget(self.text_label(t("settings.klei_root_hint"), muted=True, size_key="FONT_SIZE_SM"))

        actions = QHBoxLayout()
        change_btn = QPushButton(t("settings.klei_root_change"))
        change_btn.clicked.connect(self._on_change)
        self._reset_btn = QPushButton(t("settings.klei_root_reset"))
        self._reset_btn.clicked.connect(self._on_reset)
        open_btn = QPushButton(t("settings.klei_root_open"))
        open_btn.clicked.connect(self._on_open)
        close_btn = dialogs.style_button(QPushButton(t("dlg.close_btn")), "secondary")
        close_btn.clicked.connect(self.accept)
        actions.addStretch()
        actions.addWidget(change_btn)
        actions.addWidget(self._reset_btn)
        actions.addWidget(open_btn)
        actions.addSpacing(12)
        actions.addWidget(close_btn)
        self.body.addLayout(actions)
        self._refresh()

    @staticmethod
    def _current() -> Path | None:
        return get_klei_root_override() or auto_klei_root()

    def _refresh(self) -> None:
        override = get_klei_root_override()
        current = self._current()
        self._path_edit.setText(str(current) if current else t("env.not_found"))
        self._source_label.setText(t("settings.klei_root_custom" if override else "settings.klei_root_auto"))
        self._reset_btn.setEnabled(override is not None)

    def _blocked(self) -> bool:
        """正在运行的世界用的是当前目录下的存档，运行中不允许换目录。"""
        if self._window.ctx.manager.running():
            dialogs.show_warning(self, t("settings.klei_root_label"), t("settings.klei_root_running"))
            return True
        return False

    def _on_change(self) -> None:
        if self._blocked():
            return
        current = self._current()
        initial = current if current is not None and current.is_dir() else Path.home()
        chosen = QFileDialog.getExistingDirectory(self, t("settings.klei_root_picker_title"), str(initial))
        if not chosen:
            return
        path = Path(chosen)
        if (path / "cluster.ini").exists():
            # 选到了某个存档文件夹本身：根目录应是它的上一级
            dialogs.show_warning(self, t("settings.klei_root_label"),
                                 t("settings.klei_root_is_cluster", parent=str(path.parent)))
            return
        if not list_clusters(path) and not dialogs.ask_yes_no(
                self, t("settings.klei_root_label"), t("settings.klei_root_empty_confirm", path=str(path))):
            return
        auto = auto_klei_root()
        # 选的就是自动找到的目录：等于恢复自动，不另存
        set_klei_root_override(None if auto is not None and path == auto else path)
        self._apply()

    def _on_reset(self) -> None:
        if self._blocked():
            return
        set_klei_root_override(None)
        self._apply()

    def _apply(self) -> None:
        self._window.ctx.refresh_env(rediscover_root=True)
        self._refresh()

    def _on_open(self) -> None:
        current = self._current()
        if current is None or not current.is_dir():
            dialogs.show_warning(self, t("settings.klei_root_label"),
                                 t("settings.klei_root_not_found", path=str(current or "")))
            return
        os.startfile(str(current))


class ManualUpdateDialog(dialogs.Dialog):
    """"关于"弹窗"手动更新"——展示三个下载地址（蓝奏云先复制提取码再跳转），
    以及"从文件导入"：选中网盘下载的离线包后记在 picked_package，由调用方安装。"""

    def __init__(self, parent):
        super().__init__(parent, t("about.manual_update_title"), 480)
        self.picked_package: Path | None = None
        self.body.addWidget(self.text_label(t("about.manual_update_hint")))
        links = (
            (t("about.manual_update_baidu"), "https://pan.baidu.com/s/1hnyarybAGHjOsCCyUNlsmw?pwd=6666", None),
            (t("about.manual_update_quark"), "https://pan.quark.cn/s/80446b171dc7?pwd=SmW6", None),
            (t("about.manual_update_lanzou"), "https://wwblt.lanzout.com/b01euospla", "45w9"),
        )
        for label, url, code in links:
            link = QLabel(f'<a href="{url}">{label}</a>')
            link.setFont(theme.font("FONT_SIZE_BASE"))
            link.linkActivated.connect(lambda _url, u=url, c=code: self._open_link(u, c))
            self.body.addWidget(link)
        import_btn = QPushButton(t("about.manual_update_import_btn"))
        import_btn.clicked.connect(self._pick_package)
        close_btn = dialogs.style_button(QPushButton(t("dlg.close_btn")), "secondary")
        close_btn.clicked.connect(self.reject)
        self.add_footer([import_btn], [close_btn])

    def _pick_package(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(self, t("about.manual_update_import_pick"), "", "ZIP (*.zip)")
        if path:
            self.picked_package = Path(path)
            self.accept()

    def _open_link(self, url: str, code: str | None) -> None:
        if code:
            QGuiApplication.clipboard().setText(code)
            dialogs.show_toast(self, t("about.manual_update_code_copied"))
        webbrowser.open(url)


class AboutDialog(dialogs.Dialog):
    """"关于"：版本、简介、项目地址、提醒更新开关与检查更新（发现新版本时打开与启动时相同的更新窗口）。"""

    def __init__(self, window):
        super().__init__(window, t("menu.about"), 520)
        self._window = window
        message = t("about.message", version=__version__)
        header_text, _, rest = message.partition("\n\n")
        desc_text, _, contact_text = rest.partition("\n\n")

        header = QLabel(header_text)
        header.setFont(theme.font("FONT_SIZE_XL", bold=True))
        header.setStyleSheet(f"color: {theme.hex('PRIMARY')};")
        self.body.addWidget(header)
        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setStyleSheet(f"color: {theme.hex('CARD_BORDER')};")
        self.body.addWidget(separator)
        if desc_text:
            self.body.addWidget(self.text_label(desc_text))

        repo_url = "https://github.com/chengzhirenchaoshuai/DSTCamp"
        repo_row = QHBoxLayout()
        repo_row.addWidget(self.text_label(t("about.repo_label"), size_key="FONT_SIZE_SM"))
        repo_link = QLabel(f'<a href="{repo_url}">{t("about.repo_link_text")}</a>')
        repo_link.setFont(theme.font("FONT_SIZE_SM"))
        repo_link.linkActivated.connect(lambda _url: webbrowser.open(repo_url))
        repo_row.addWidget(repo_link)
        repo_row.addStretch()
        self.body.addLayout(repo_row)

        if contact_text:
            self.body.addWidget(self.text_label(contact_text))

        self._found_release = None
        self._update_status = QLabel("")
        self._update_status.setFont(theme.font("FONT_SIZE_SM"))
        self._update_status.setProperty("muted", True)
        self._update_status.linkActivated.connect(self._open_release_page)
        self.body.addWidget(self._update_status)

        remind_row = QHBoxLayout()
        remind_row.addWidget(self.text_label(t("about.remind_update_label"), size_key="FONT_SIZE_SM"))
        remind_row.addStretch()
        remind_switch = ToggleSwitch(checked=get_remind_update_enabled())
        remind_switch.toggled.connect(set_remind_update_enabled)
        remind_row.addWidget(remind_switch)
        self.body.addLayout(remind_row)

        btn_row = QHBoxLayout()
        self._check_btn = QPushButton(t("about.check_update_btn"))
        self._check_btn.clicked.connect(self._check_update)
        manual_btn = QPushButton(t("about.manual_update_btn"))
        manual_btn.clicked.connect(self._open_manual_update)
        close_btn = QPushButton(t("dlg.confirm_btn"))
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(self._check_btn)
        btn_row.addWidget(manual_btn)
        btn_row.addStretch()
        btn_row.addWidget(close_btn)
        self.body.addLayout(btn_row)

    def _open_manual_update(self) -> None:
        dialog = ManualUpdateDialog(self)
        dialog.exec()
        if dialog.picked_package is not None and hasattr(self._window, "import_offline_package"):
            self.accept()  # 先关掉"关于"，导入过程的提示都挂在主窗口上
            self._window.import_offline_package(dialog.picked_package)

    def _open_release_page(self, _url: str) -> None:
        if self._found_release is None:
            return
        release = self._found_release
        if hasattr(self._window, "open_update_prompt"):
            self.accept()  # 先关掉"关于"，再弹更新窗口
            self._window.open_update_prompt(release)
        else:
            webbrowser.open(release.page_url)

    def _check_update(self) -> None:
        self._check_btn.setEnabled(False)
        self._update_status.setProperty("muted", True)
        self._update_status.setText(t("about.checking_update"))
        self._update_status.style().polish(self._update_status)

        def done(result) -> None:
            self._check_btn.setEnabled(True)
            if result is None:
                self._update_status.setProperty("muted", True)
                self._update_status.setText(t("about.check_update_failed"))
            elif is_newer_version(__version__, result.version):
                self._found_release = result
                self._update_status.setProperty("muted", False)
                self._update_status.setText(
                    f'<a href="{result.page_url}" style="color: {theme.hex("PRIMARY")};">'
                    f'{t("app.update_available", version=result.version)}</a>')
            else:
                self._update_status.setProperty("muted", True)
                self._update_status.setText(t("about.up_to_date"))
            self._update_status.style().polish(self._update_status)

        def error(_exc: Exception) -> None:
            self._check_btn.setEnabled(True)
            self._update_status.setProperty("muted", True)
            self._update_status.setText(t("about.check_update_failed"))
            self._update_status.style().polish(self._update_status)

        run_async(check_latest_release, done, error)
