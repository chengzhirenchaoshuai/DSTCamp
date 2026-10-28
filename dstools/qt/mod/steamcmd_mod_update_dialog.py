"""SteamCMD 通道「Mod 全局更新」：列出 SteamCMD 工坊目录里已下载的和任一服务器存档用到的工坊 Mod，
对照 Web API 标出未下载 / 有更新 / 最新 / 工坊查不到，V1、V2 都按清单更新时间判断（已实测两者都与 Web API 一致）。

外观与 Steam 客户端通道的 WorkshopUpdateDialog 一致；先读本地列表立即显示（状态为"检查中"），再联网补状态。
「一键更新」只更新已下载且有新版本的；没下载过的由「下载缺失」单独处理，免得一次拉下几十个不用的 Mod。
V1 包下载后，专服 mods 里的展开副本在下次启动前自动部署，这里只提示。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from dstools.features.mod.list_model import localize_mod_name, version_display
from dstools.features.mod.steamcmd_mod_status import (
    SteamcmdModRow, SteamcmdModState, collect_steamcmd_mod_rows, inspect_steamcmd_mods, with_statuses,
)
from dstools.i18n import t
from dstools.qt import dialogs
from dstools.qt.imaging import pil_to_pixmap
from dstools.qt.theme import theme
from dstools.qt.threads import run_async
from dstools.qt.widgets import CachedRowList, Card, PillTabBar, mod_format_tag_colors

_STATE_KEYS = {
    SteamcmdModState.OUTDATED: "steamcmd_update.state_outdated",
    SteamcmdModState.MISSING: "steamcmd_update.state_missing",
    SteamcmdModState.CURRENT: "steamcmd_update.state_current",
    SteamcmdModState.UNAVAILABLE: "steamcmd_update.state_unavailable",
    SteamcmdModState.CLIENT_ONLY: "steamcmd_update.state_client_only",
}
# 状态文字颜色：有更新用强调色，查不到用错误色，最新用成功色，未下载保持默认
_STATE_COLORS = {
    SteamcmdModState.OUTDATED: "ACCENT",
    SteamcmdModState.UNAVAILABLE: "ERROR",
    SteamcmdModState.CURRENT: "SUCCESS",
    SteamcmdModState.CLIENT_ONLY: "TEXT_MUTED",
}
_STATE_ORDER = {SteamcmdModState.OUTDATED: 0, SteamcmdModState.MISSING: 1,
                SteamcmdModState.CURRENT: 2, SteamcmdModState.UNAVAILABLE: 3, SteamcmdModState.CLIENT_ONLY: 4}
_FILTER_ALL, _FILTER_PENDING, _FILTER_CURRENT_SAVE = range(3)
_ROW_H = 84
_ICON = 58
_COLUMNS = (("mod.update_latest_version", 170), ("steamcmd_update.col_used_by", 130),
            ("mod.update_workshop_column", 90), ("mod.update_action", 110))


class SteamcmdModUpdateDialog(QDialog):
    def __init__(self, page):
        super().__init__(page.window())
        self.page = page
        self.setWindowTitle(t("steamcmd_update.title"))
        dialogs.fit_to_screen(self, 980, 680)
        self.setStyleSheet(f"QDialog {{ background: {theme.hex('BG_SOFT')}; }}")
        self._clusters = page.ctx.clusters()
        self._server_mods_root = page._server_mods_root()
        self._current_ids = set(page._current_save_enabled_workshop_ids())
        # 先显示上次查完的结果（按钮可直接用），_reload 在后台复查后原地更新
        self._rows: list[SteamcmdModRow] = list(page.steamcmd_update_rows_snapshot)
        self._selected: set[str] = set()
        self._loading = False
        self._busy = False
        self._check_error = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(*dialogs.DIALOG_MARGINS)
        root.setSpacing(dialogs.DIALOG_SPACING)

        toolbar_card = Card(radius=0)
        root.addWidget(toolbar_card)
        toolbar = QHBoxLayout(toolbar_card)
        toolbar.setContentsMargins(12, 10, 12, 10)
        toolbar.addWidget(QLabel(t("mod.filter")))
        self._search = QLineEdit()
        self._search.setFixedWidth(220)  # 固定宽度：右侧计数文字变长时不挤压搜索框
        self._search.textChanged.connect(self._render_rows)
        toolbar.addWidget(self._search)
        self._filter = PillTabBar(
            [t("mod.show_all"), t("mod.update_filter_needs_update"), t("mod.update_filter_current")],
            height=32, pill_height=24, font_size_key="FONT_SIZE_SM", gap=3, pad=18, uniform_width=True)
        self._filter.current_changed.connect(lambda _i: self._render_rows())
        toolbar.addWidget(self._filter)
        self._refresh_btn = QPushButton(t("mod.update_refresh_states"))
        self._refresh_btn.clicked.connect(self._reload)
        toolbar.addWidget(self._refresh_btn)
        toolbar.addStretch()
        self._count_label = QLabel("")
        self._count_label.setProperty("muted", True)
        toolbar.addWidget(self._count_label)

        self._notice = QLabel(t("steamcmd_update.desc"))
        self._notice.setWordWrap(True)
        self._notice.setProperty("muted", True)
        self._notice.setFont(theme.font("FONT_SIZE_SM"))
        root.addWidget(self._notice)

        header_card = Card(radius=0, alpha=255, fill_key="PRIMARY_LIGHT")
        root.addWidget(header_card)
        header_layout = QHBoxLayout(header_card)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.addWidget(self._make_header())

        list_card = Card(radius=0)
        root.addWidget(list_card, 1)
        list_layout = QVBoxLayout(list_card)
        list_layout.setContentsMargins(0, 0, 0, 0)
        area = QScrollArea()
        area.setObjectName("steamcmdUpdateArea")
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.Shape.NoFrame)
        area.viewport().setAutoFillBackground(False)
        inner = QWidget()
        inner.setObjectName("steamcmdUpdateInner")
        inner.setAutoFillBackground(False)
        # 显式透明：本窗口有自己的样式表，滚动区会露出系统调色板灰底
        area.setStyleSheet("#steamcmdUpdateArea, #steamcmdUpdateArea > QWidget, #steamcmdUpdateInner "
                           "{ background: transparent; border: none; }")
        self._rows_layout = QVBoxLayout(inner)
        self._rows_layout.setContentsMargins(0, 3, 0, 3)
        self._rows_layout.setSpacing(3)
        # 按 Mod 缓存行控件：搜索、筛选、全选不再整表重建
        self._row_list = CachedRowList(self._rows_layout, row_height=_ROW_H)
        self._row_checkboxes: dict[str, QCheckBox] = {}
        area.setWidget(inner)
        list_layout.addWidget(area)

        footer = QHBoxLayout()
        self._update_selected_btn = QPushButton(t("mod.update_selected_btn"))
        self._update_selected_btn.clicked.connect(
            lambda: self._download([w for w in self._ordered_ids() if w in self._selected]))
        footer.addWidget(self._update_selected_btn)
        footer.addStretch()
        self._missing_btn = QPushButton()
        self._missing_btn.clicked.connect(lambda: self._download(self._ids_in(SteamcmdModState.MISSING), confirm=True))
        footer.addWidget(self._missing_btn)
        self._quick_btn = QPushButton()
        self._quick_btn.clicked.connect(lambda: self._download(self._ids_in(SteamcmdModState.OUTDATED)))
        footer.addWidget(self._quick_btn)
        root.addLayout(footer)

        self._render_rows()
        self._reload()

    # ── 数据 ────────────────────────────────────────────────────────────
    def _reload(self) -> None:
        """先读本地列表立即显示，再联网查状态；联网失败时列表保留，状态标为无法检查。"""
        if self._loading:
            return
        self._loading = True
        self._check_error = ""
        if not self._rows:
            self._render_rows()  # 首次打开：列表区显示"正在读取"，不显示"未过滤出"
        else:
            self._refresh_controls()
        self._count_label.setText(t("steamcmd_update.loading_local"))
        clusters, mods_root = self._clusters, self._server_mods_root
        run_async(lambda: collect_steamcmd_mod_rows(clusters, mods_root), self._on_collected, self._on_failed)

    def _on_collected(self, rows) -> None:
        # 刷新时沿用上次查到的状态，免得整列先退回"检查中"再变回来
        previous = {row.workshop_id: row.status for row in self._rows if row.status is not None}
        self._rows = with_statuses(rows, previous)
        self._render_rows()
        self._count_label.setText(t("mod.update_status_checking_hint"))
        ids = [row.workshop_id for row in rows]
        run_async(lambda: inspect_steamcmd_mods(ids), self._on_checked, self._on_failed)

    def _on_checked(self, statuses) -> None:
        self._loading = False
        self._rows = with_statuses(self._rows, statuses)
        self.page.steamcmd_update_rows_snapshot = list(self._rows)
        self._selected &= {row.workshop_id for row in self._rows if row.needs_download}  # 已更新好的取消勾选
        self._render_rows()

    def _on_failed(self, exc: Exception) -> None:
        self._loading = False
        self._check_error = t("steamcmd_mods.check_failed", detail=str(exc))
        self._render_rows()

    def _row_of(self, wid: str) -> SteamcmdModRow | None:
        return next((row for row in self._rows if row.workshop_id == wid), None)

    def _ids_in(self, state: SteamcmdModState) -> list[str]:
        return [row.workshop_id for row in self._rows if row.status is not None and row.status.state is state]

    def _name_for(self, row: SteamcmdModRow) -> str:
        info = self.page._mod_infos.get(f"workshop-{row.workshop_id}")
        raw = (info.name if info else "") or row.name or f"workshop-{row.workshop_id}"
        return str(localize_mod_name(f"workshop-{row.workshop_id}", raw) or row.workshop_id)

    def _ordered_ids(self) -> list[str]:
        def key(row):
            order = _STATE_ORDER[row.status.state] if row.status is not None else 5
            return order, self._name_for(row).casefold()
        return [row.workshop_id for row in sorted(self._rows, key=key)]

    def _filtered_ids(self) -> list[str]:
        needle = self._search.text().strip().casefold()
        mode = self._filter.current_index()
        result = []
        for wid in self._ordered_ids():
            row = self._row_of(wid)
            if mode == _FILTER_PENDING and not row.needs_download:
                continue
            if mode == _FILTER_CURRENT_SAVE and wid not in self._current_ids:
                continue
            if needle and needle not in f"{self._name_for(row)} {wid}".casefold():
                continue
            result.append(wid)
        return result

    # ── 渲染 ────────────────────────────────────────────────────────────
    def _make_header(self) -> QWidget:
        header = QWidget()
        header.setFixedHeight(34)
        layout = QHBoxLayout(header)
        layout.setContentsMargins(12, 4, 12, 4)
        self._select_all_box = QCheckBox(t("mod.update_select_all"))
        self._select_all_box.setFont(theme.font("FONT_SIZE_SM", bold=True))
        self._select_all_box.clicked.connect(self._on_select_all_clicked)
        layout.addWidget(self._select_all_box)
        layout.addStretch()
        for key, width in _COLUMNS:
            label = QLabel(t(key))
            label.setFont(theme.font("FONT_SIZE_SM", bold=True))
            label.setProperty("muted", True)
            label.setFixedWidth(width)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(label)
        return header

    def _on_select_all_clicked(self, checked: bool) -> None:
        selectable = [wid for wid in self._filtered_ids() if self._row_of(wid).needs_download]
        if checked:
            self._selected.update(selectable)
        else:
            self._selected.difference_update(selectable)
        self._render_rows()

    def _row_signature(self, wid: str) -> tuple:
        """行内容依赖的全部数据；变了才重建这一行（勾选和底色由 _place_row 原地更新）。"""
        row = self._row_of(wid)
        info = self.page._mod_infos.get(f"workshop-{wid}")
        image = self.page._icon_imgs.get(f"workshop-{wid}")
        return (row, self._name_for(row), version_display(info), id(image) if image else None,
                self._busy, self._check_error)

    def _place_row(self, card: Card, wid: str, index: int) -> None:
        card.set_fill_key("CARD_BG_ALT" if index % 2 == 0 else "CARD_BG")
        checkbox = self._row_checkboxes.get(wid)
        if checkbox is not None and checkbox.isChecked() != (wid in self._selected):
            checkbox.blockSignals(True)
            checkbox.setChecked(wid in self._selected)
            checkbox.blockSignals(False)

    def _make_empty_label(self) -> QLabel:
        empty = QLabel(t("steamcmd_update.loading_local") if self._loading and not self._rows
                       else t("mod.no_filtered"))
        empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty.setContentsMargins(0, 24, 0, 24)
        return empty

    def _render_rows(self, *_args) -> None:
        pending = sum(1 for row in self._rows if row.needs_download)
        needs_label = t("mod.update_filter_needs_update")
        self._filter.set_labels([t("mod.show_all"), f"{needs_label}（{pending}）" if pending else needs_label,
                                 t("mod.update_filter_current")])
        visible = self._filtered_ids()
        selectable = [wid for wid in visible if self._row_of(wid).needs_download]
        self._select_all_box.setEnabled(bool(selectable) and not self._busy)
        self._select_all_box.setChecked(bool(selectable) and all(wid in self._selected for wid in selectable))
        self._row_list.render(visible, signature=self._row_signature,
                              build=lambda wid: self._make_row(self._row_of(wid), 0),
                              place=self._place_row, empty=self._make_empty_label)
        self._refresh_controls()

    def _refresh_controls(self) -> None:
        missing = len(self._ids_in(SteamcmdModState.MISSING))
        outdated = len(self._ids_in(SteamcmdModState.OUTDATED))
        self._missing_btn.setText(t("steamcmd_update.download_missing_btn", count=missing))
        self._quick_btn.setText(t("mod.update_quick_btn_count", count=outdated) if outdated
                                else t("mod.update_quick_btn"))
        self._missing_btn.setEnabled(missing > 0 and not self._busy)
        self._quick_btn.setEnabled(outdated > 0 and not self._busy)
        self._update_selected_btn.setEnabled(bool(self._selected) and not self._busy)
        self._refresh_btn.setEnabled(not self._loading and not self._busy)
        if self._check_error:
            self._count_label.setText(self._check_error)
        elif not self._loading:
            self._count_label.setText(t("mod.update_selected_count", selected=len(self._selected),
                                        total=len(self._rows)))

    def _make_row(self, row: SteamcmdModRow, index: int) -> QWidget:
        # 与 Steam 客户端通道的更新窗口一致：交替行色的独立卡片（用 Card 自绘，不压掉按钮的全局样式）
        card = Card(radius=0, alpha=255, fill_key="CARD_BG_ALT" if index % 2 == 0 else "CARD_BG")
        card.setFixedHeight(_ROW_H)
        layout = QHBoxLayout(card)
        layout.setContentsMargins(12, 4, 12, 4)
        wid = row.workshop_id
        checkbox = QCheckBox()
        checkbox.setEnabled(row.needs_download and not self._busy)
        checkbox.setChecked(wid in self._selected)
        checkbox.toggled.connect(lambda checked, w=wid: self._on_check(w, checked))
        self._row_checkboxes[wid] = checkbox
        layout.addWidget(checkbox)

        icon_label = QLabel()
        image = self.page._icon_imgs.get(f"workshop-{wid}")
        if image is not None:
            icon_label.setPixmap(pil_to_pixmap(image).scaled(
                _ICON, _ICON, aspectMode=Qt.AspectRatioMode.KeepAspectRatio,
                mode=Qt.TransformationMode.SmoothTransformation))
        icon_label.setFixedSize(_ICON, _ICON)
        layout.addWidget(icon_label)

        text_col = QVBoxLayout()
        text_col.setSpacing(1)
        name_label = QLabel(self._name_for(row))
        name_label.setFont(theme.font("FONT_SIZE_BASE", bold=True))
        text_col.addWidget(name_label)
        id_label = QLabel(f"workshop-{wid}")
        id_label.setProperty("muted", True)
        id_label.setFont(theme.font("FONT_SIZE_SM"))
        text_col.addWidget(id_label)
        info_row = QHBoxLayout()
        info_row.setSpacing(6)
        info = self.page._mod_infos.get(f"workshop-{wid}")
        if info is not None:
            version_label = QLabel(version_display(info))
            version_label.setProperty("muted", True)
            version_label.setFont(theme.font("FONT_SIZE_SM"))
            info_row.addWidget(version_label)
        if row.mod_format:
            info_row.addWidget(self._format_tag(row.mod_format))
        info_row.addStretch()
        text_col.addLayout(info_row)
        layout.addLayout(text_col, 1)

        layout.addWidget(self._state_label(row))

        used_text = ("、".join(row.used_by) if len(row.used_by) <= 2
                     else t("steamcmd_update.used_by_count", count=len(row.used_by)))
        used_label = QLabel(used_text or t("steamcmd_update.used_by_none"))
        used_label.setWordWrap(True)
        used_label.setFixedWidth(_COLUMNS[1][1])
        used_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        used_label.setFont(theme.font("FONT_SIZE_SM"))
        if not row.used_by:
            used_label.setProperty("muted", True)
        used_label.setToolTip("\n".join(row.used_by))
        layout.addWidget(used_label)

        link_label = QLabel(f'<a href="#" style="color:{theme.hex("ACCENT")};">{t("mod.workshop_link_btn")}</a>')
        link_label.setFixedWidth(_COLUMNS[2][1])
        link_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        link_label.linkActivated.connect(lambda _href, w=wid: self.page._on_link(f"workshop-{w}"))
        layout.addWidget(link_label)

        action_box = QWidget()
        action_box.setFixedWidth(_COLUMNS[3][1])
        action_layout = QHBoxLayout(action_box)
        action_layout.setContentsMargins(0, 0, 0, 0)
        if row.needs_download:
            missing = row.status.state is SteamcmdModState.MISSING
            action_btn = QPushButton(t("steamcmd_update.download_one_btn" if missing else "mod.update_one_btn"))
            action_btn.setFont(theme.font("FONT_SIZE_SM"))
            action_btn.setEnabled(not self._busy)
            action_btn.clicked.connect(lambda _c=False, w=wid: self._download([w]))
            action_layout.addWidget(action_btn)
        layout.addWidget(action_box)
        return card

    def _state_label(self, row: SteamcmdModRow) -> QLabel:
        """状态列：检查中 / 无法检查 / 各状态；V1 专服副本待部署时第二行小字说明。"""
        if row.status is None:
            text = t("steamcmd_update.state_check_failed") if self._check_error else t("mod.update_latest_checking")
            color_key = None
        else:
            text = t(_STATE_KEYS[row.status.state])
            color_key = _STATE_COLORS.get(row.status.state)
        html = f'<span style="color:{theme.hex(color_key)};">{text}</span>' if color_key else text
        if row.v1_deploy_pending and row.mod_format:
            html += (f'<br><span style="color:{theme.hex("TEXT_MUTED")}; font-size:small;">'
                     f'{t("steamcmd_update.v1_deploy_pending")}</span>')
        label = QLabel(html)
        label.setWordWrap(True)
        label.setFixedWidth(_COLUMNS[0][1])
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        return label

    @staticmethod
    def _format_tag(mod_format: str) -> QLabel:
        """名称下方的 V1/V2 小标签，样式与 Steam 客户端通道的更新窗口一致；依据是 SteamCMD 工坊目录里的实际文件。"""
        tag = QLabel(mod_format)
        tag.setFont(theme.font("FONT_SIZE_SM", bold=True))
        tag.setToolTip(t("mod.format_tag_v1_tip" if mod_format == "V1" else "mod.format_tag_v2_tip"))
        background, color = mod_format_tag_colors(mod_format)
        # 样式必须用对象名限定：悬停提示框会继承控件样式表，否则提示框也画成圆角、四角露出黑点
        tag.setObjectName("modFormatTag")
        tag.setStyleSheet(f"QLabel#modFormatTag {{ background: {background.name()}; color: {color.name()}; "
                          "border-radius: 4px; padding: 0px 5px; }")
        return tag

    def _on_check(self, wid: str, checked: bool) -> None:
        if checked:
            self._selected.add(wid)
        else:
            self._selected.discard(wid)
        self._render_rows()

    # ── 操作 ────────────────────────────────────────────────────────────
    def _download(self, ids: list[str], confirm: bool = False) -> None:
        if not ids:
            return
        if confirm and not dialogs.ask_yes_no(self, t("steamcmd_update.title"),
                                              t("steamcmd_update.download_missing_confirm", count=len(ids))):
            return
        from dstools.features.local_service import steamcmd_updater
        from dstools.qt.mod.steamcmd_mods import run_steamcmd_mod_update
        if steamcmd_updater.find_steamcmd() is None:
            run_steamcmd_mod_update(self, ids)  # 只会提示 SteamCMD 不可用的原因
            return
        self._busy = True
        self._render_rows()

        def finished() -> None:
            self._busy = False
            self.page.ctx.workshop_mods_changed.emit()
            self._reload()  # 以复查结果为准刷新列表

        run_steamcmd_mod_update(self, ids, on_finished=finished)
