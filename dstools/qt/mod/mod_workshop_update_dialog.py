"""Workshop 更新选择器：扫描状态、筛选搜索、批量/单个更新，以及残留清理和取消订阅引用移除。

三者共用同一份 WorkshopModStatus 扫描结果，每行按优先级只显示一个操作：能更新显示"更新"，
否则存档仍引用已取消订阅项时显示"移除引用"，否则有残留文件时显示"清理残留"。
"""

import html
import os
import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea, QTextEdit,
    QVBoxLayout, QWidget,
)

from dstools.features.mod.list_model import localize_mod_name, version_display
from dstools.features.mod.manager import load_mod_overrides, save_mod_overrides
from dstools.features.mod.workshop_cleanup import (
    ResidualCleanupContext, build_residual_cleanup_context, delete_legacy_runtime_residual,
    delete_workshop_residual, format_residual_directory_tree,
)
from dstools.features.mod.workshop_status import WorkshopModState
from dstools.i18n import t
from dstools.models import Platform
from dstools.qt import dialogs
from dstools.qt.imaging import pil_to_pixmap
from dstools.qt.theme import theme
from dstools.qt.threads import run_async
from dstools.qt.widgets import CachedRowList, Card, PillTabBar, mod_format_tag_colors

_LATEST_LABELS = {
    WorkshopModState.CURRENT: "mod.update_latest_up_to_date",
    WorkshopModState.UPDATE_AVAILABLE: "mod.update_latest_available",
    WorkshopModState.SUSPECTED_OUTDATED: "mod.update_latest_suspected_outdated",
    WorkshopModState.SHADOWED_BY_V1: "mod.update_latest_v1_shadowed",
    WorkshopModState.MISSING: "mod.update_latest_missing",
    WorkshopModState.SOURCE_UNAVAILABLE: "mod.update_latest_source_unavailable",
    WorkshopModState.INTEGRITY_UNCONFIRMED: "mod.update_latest_integrity_unconfirmed",
    WorkshopModState.UNSUBSCRIBED_REFERENCED: "mod.update_latest_unsubscribed_referenced",
    WorkshopModState.RESIDUAL_FILES: "mod.update_latest_residual_files",
    WorkshopModState.SUBSCRIBED_BY_OTHER_ACCOUNT: "mod.update_latest_other_account",
    WorkshopModState.LEGACY_PACKAGE_READY: "mod.update_latest_legacy_package_ready",
    WorkshopModState.LEGACY_DIR_POLLUTED: "mod.update_latest_legacy_dir_polluted",
    WorkshopModState.LEGACY_RUNTIME_RESIDUAL: "mod.update_latest_legacy_runtime_residual",
    WorkshopModState.LOCAL_FILES: "mod.update_latest_local_files",
    WorkshopModState.NOT_INSTALLED: "mod.update_latest_not_installed",
    WorkshopModState.DOWNLOADING: "mod.update_latest_downloading",
    WorkshopModState.DOWNLOAD_PENDING: "mod.update_latest_pending",
    WorkshopModState.UNKNOWN: "mod.update_latest_unknown",
}
_ROW_H = 84
_ICON = 58


def _cleanup_paths(status) -> tuple:
    evidence = status.evidence if status is not None else None
    content_path = (evidence.workshop_content_path or evidence.residual_path) if evidence is not None else None
    runtime_paths = evidence.legacy_runtime_residual_paths if evidence is not None else ()
    return tuple(path for path in (content_path, *runtime_paths) if path is not None)


def _is_empty_directory(path) -> bool:
    try:
        return path.is_dir() and next(path.iterdir(), None) is None
    except OSError:
        return False


class _ResidualConfirmDialog(dialogs.Dialog):
    """单个 Mod 清理残留前的确认：展示完整确认文案（含浅层目录结构），额外带一个
    "打开文件夹"按钮。"""

    def __init__(self, parent, tree_text: str, folder_to_open):
        super().__init__(parent, t("mod.update_cleanup_residual_title"), 720,
                          confirm_text=t("dlg.confirm_btn"))
        view = QTextEdit()
        view.setReadOnly(True)
        view.setPlainText(t("mod.update_cleanup_residual_confirm", tree=tree_text))
        view.setFont(theme.font("FONT_SIZE_SM"))
        view.setMinimumHeight(280)
        self.body.addWidget(view, 1)
        row = QHBoxLayout()
        open_btn = QPushButton(t("mod.update_cleanup_open_btn"))
        open_btn.clicked.connect(folder_to_open)
        row.addWidget(open_btn)
        row.addStretch()
        self.body.addLayout(row)
        self.add_buttons()


class _BulkResidualChoiceDialog(dialogs.Dialog):
    """一键清理残留前的确认：选"仅清空文件夹"还是"全部清理"，或取消。"""

    def __init__(self, parent, count: int, tree_text: str):
        super().__init__(parent, t("mod.update_cleanup_all_title"), 760)
        self.mode: str | None = None
        view = QTextEdit()
        view.setReadOnly(True)
        view.setPlainText(t("mod.update_cleanup_all_confirm", count=count, tree=tree_text))
        view.setFont(theme.font("FONT_SIZE_SM"))
        view.setMinimumHeight(320)
        self.body.addWidget(view, 1)
        row = QHBoxLayout()
        cancel = dialogs.style_button(QPushButton(t("dlg.cancel_btn")), "secondary")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        row.addStretch()
        empty_btn = QPushButton(t("mod.update_cleanup_empty_only_btn"))
        empty_btn.clicked.connect(lambda: self._choose("empty"))
        row.addWidget(empty_btn)
        all_btn = dialogs.style_button(QPushButton(t("mod.update_cleanup_all_full_btn")), "danger")
        all_btn.clicked.connect(lambda: self._choose("all"))
        row.addWidget(all_btn)
        self.body.addSpacing(8)
        self.body.addLayout(row)

    def _choose(self, mode: str) -> None:
        self.mode = mode
        self.accept()


class WorkshopUpdateDialog(QDialog):
    def __init__(self, page):
        super().__init__(page.window())
        self.page = page
        self.setWindowTitle(t("mod.update_title"))
        dialogs.fit_to_screen(self, 980, 680)
        self.setStyleSheet(f"QDialog {{ background: {theme.hex('BG_SOFT')}; }}")
        self._states: dict[str, object] = {str(k): v for k, v in page._workshop_status_cache.items()}
        self._loading = False
        self._selected: set[str] = set()
        self._cleanup_running: set[str] = set()
        # 全量候选（本地安装/V1 包/存档引用/残留目录），缓存的订阅项也参与首帧，避免刚检测出的缺失项一关窗就消失；
        # 真正刷新时 Steam 重新枚举订阅，已取消订阅的陈旧项自然移除
        local_ids = [str(wid) for wid in page._workshop_candidate_ids()]
        known = set(local_ids)
        self._ids = list(local_ids)
        self._ids.extend(str(wid) for wid in page._workshop_status_cache if str(wid) not in known)
        self._current_ids = page._current_cluster_workshop_ids()

        root = QVBoxLayout(self)
        root.setContentsMargins(*dialogs.DIALOG_MARGINS)
        root.setSpacing(dialogs.DIALOG_SPACING)

        toolbar_card = Card(radius=0)
        root.addWidget(toolbar_card)
        toolbar = QHBoxLayout(toolbar_card)
        toolbar.setContentsMargins(12, 10, 12, 10)
        toolbar.addWidget(QLabel(t("mod.filter")))
        self._search = QLineEdit()
        # 固定宽度：QLineEdit 默认横向可伸缩，点"刷新"后右侧计数文字变成较长的
        # "正在检查..."会挤压搜索框，连带筛选页签和按钮一起移动。
        self._search.setFixedWidth(220)
        self._search.textChanged.connect(self._render_rows)
        toolbar.addWidget(self._search)
        self._status_filter = PillTabBar(
            [t("mod.show_all"), t("mod.update_filter_needs_update"), t("mod.update_filter_current")],
            height=32, pill_height=24, font_size_key="FONT_SIZE_SM", gap=3, pad=18,
            uniform_width=True)  # "全部"两个字太窄，跟"待更新"等统一宽度
        self._status_filter.current_changed.connect(lambda _i: self._render_rows())
        toolbar.addWidget(self._status_filter)
        # 按订阅账号筛选：Steam 当前登录账号的订阅，或 Steam 清单记录的其他账号的订阅
        self._account_filter = QComboBox()
        self._account_filter.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self._account_filter.activated.connect(lambda _i: self._render_rows())
        toolbar.addWidget(self._account_filter)
        self._refresh_account_filter()
        self._refresh_btn = QPushButton(t("mod.update_refresh_states"))
        self._refresh_btn.clicked.connect(lambda: self._reload(force=True))
        toolbar.addWidget(self._refresh_btn)
        toolbar.addStretch()
        self._count_label = QLabel("")
        self._count_label.setProperty("muted", True)
        toolbar.addWidget(self._count_label)

        self._state_notice = QLabel("")
        self._state_notice.setWordWrap(True)
        self._state_notice.setVisible(False)
        self._state_notice.setStyleSheet(
            f"background: {theme.hex('BANNER_BG')}; color: {theme.hex('BANNER_TEXT')}; padding: 8px 12px;")
        root.addWidget(self._state_notice)

        header_card = Card(radius=0, alpha=255, fill_key="PRIMARY_LIGHT")
        root.addWidget(header_card)
        header = self._make_header()
        header_layout = QHBoxLayout(header_card)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.addWidget(header)

        list_card = Card(radius=0)
        root.addWidget(list_card, 1)
        list_layout = QVBoxLayout(list_card)
        list_layout.setContentsMargins(0, 0, 0, 0)
        area = QScrollArea()
        area.setObjectName("updateListArea")
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.Shape.NoFrame)
        area.viewport().setAutoFillBackground(False)
        inner = QWidget()
        inner.setObjectName("updateListInner")
        inner.setAutoFillBackground(False)
        # 显式透明：本窗口有自己的样式表，滚动区会露出系统调色板灰底
        area.setStyleSheet("#updateListArea, #updateListArea > QWidget, #updateListInner "
                           "{ background: transparent; border: none; }")
        self._rows_layout = QVBoxLayout(inner)
        self._rows_layout.setContentsMargins(0, 3, 0, 3)
        self._rows_layout.setSpacing(3)
        # 两百多行逐个建控件要近 1 秒：按 Mod 缓存复用，搜索/筛选/全选不再整表重建
        self._row_list = CachedRowList(self._rows_layout, row_height=_ROW_H)
        self._row_checkboxes: dict[str, QCheckBox] = {}
        area.setWidget(inner)
        list_layout.addWidget(area)

        footer = QHBoxLayout()
        self._update_selected_btn = QPushButton(t("mod.update_selected_btn"))
        self._update_selected_btn.clicked.connect(self._update_selected)
        footer.addWidget(self._update_selected_btn)
        footer.addStretch()
        self._quick_update_btn = QPushButton(t("mod.update_quick_btn"))
        self._quick_update_btn.clicked.connect(self._quick_update)
        footer.addWidget(self._quick_update_btn)
        self._cleanup_all_btn = QPushButton(t("mod.update_cleanup_all_btn"))
        self._cleanup_all_btn.clicked.connect(self._cleanup_all_residuals)
        footer.addWidget(self._cleanup_all_btn)
        root.addLayout(footer)

        self._render_rows()
        # Mod 页刚查过（预查或上次打开本窗口）就直接用，不让用户每次都等；要最新结果可点「刷新状态」
        if not page.workshop_status_fresh(self._ids):
            self._reload(force=False)

    # ── 数据 ────────────────────────────────────────────────────────────
    def _name_for(self, wid: str) -> str:
        info = self.page._mod_infos.get(f"workshop-{wid}") or self.page._mod_infos.get(wid)
        raw = (info.name if info else "") or self.page._workshop_title_cache.get(wid, "") or f"workshop-{wid}"
        return str(localize_mod_name(f"workshop-{wid}", raw) or wid)

    def _can_select(self, wid: str) -> bool:
        status = self._states.get(wid)
        return bool(status is not None and status.can_update)

    def _can_check(self, wid: str) -> bool:
        """能勾选：可以更新的，或其他账号订阅、可以强制清理的（勾选后由"一键清理残留"批量清理）。"""
        status = self._states.get(wid)
        return self._can_select(wid) or (status is not None and self._can_force_cleanup(status))

    def _owners(self, wid: str) -> set[str]:
        """Mod 的订阅账号（可能多个）：Steam 清单记录的订阅者，当前 Steam 账号订阅的再加上 Steam 当前登录账号。"""
        status = self._states.get(wid)
        evidence = status.evidence if status is not None else None
        if evidence is None:
            return set()
        owners = set(evidence.acf_subscribers)
        active = self.page.ctx.env.steam_active_account
        if active and evidence.steam_state is not None and evidence.steam_state.subscribed:
            owners.add(active)
        return owners

    def _refresh_account_filter(self) -> None:
        """按当前状态重建账号筛选项，保持原来选中的账号；Steam 当前登录账号排在最前。"""
        selected = self._account_filter.currentData()
        active = self.page.ctx.env.steam_active_account
        owners = set().union(*(self._owners(wid) for wid in self._ids))
        if active:
            owners.add(active)
        ordered = sorted(owners, key=lambda account: (account != active, account))
        self._account_filter.clear()
        self._account_filter.addItem(t("mod.update_account_filter_all"), "")
        for account in ordered:
            self._account_filter.addItem(self._subscriber_text(account), account)
        index = self._account_filter.findData(selected) if selected else 0
        self._account_filter.setCurrentIndex(max(index, 0))

    def _pending_count(self) -> int:
        return sum(1 for wid in self._ids if wid in self._states and self._states[wid].needs_action)

    def _filtered_ids(self) -> list[str]:
        needle = self._search.text().strip().casefold()
        mode = self._status_filter.current_index()
        account = self._account_filter.currentData()
        result = []
        for wid in self._ids:
            if account and account not in self._owners(wid):
                continue
            if mode == 1 and not (wid in self._states and self._states[wid].needs_action):
                continue
            if mode == 2 and wid not in self._current_ids:
                continue
            name = self._name_for(wid)
            if needle and needle not in f"{name} {wid}".casefold():
                continue
            result.append(wid)
        priority = {WorkshopModState.DOWNLOADING: 0, WorkshopModState.DOWNLOAD_PENDING: 0,
                   WorkshopModState.UPDATE_AVAILABLE: 1, WorkshopModState.SUSPECTED_OUTDATED: 1,
                   WorkshopModState.SHADOWED_BY_V1: 1, WorkshopModState.LEGACY_DIR_POLLUTED: 1,
                   WorkshopModState.MISSING: 2}
        return sorted(result, key=lambda wid: (
            priority.get(self._states[wid].state, 5) if wid in self._states else 5, self._name_for(wid).casefold()))

    def _reload(self, force: bool) -> None:
        if self._loading:
            return
        self._loading = True
        self._refresh_btn.setEnabled(False)
        self._count_label.setText(t("mod.update_status_checking_hint"))
        ids = [int(wid) for wid in self._ids]
        configured_ids = [int(wid) for wid in self._current_ids]

        def work():
            from dstools.features.mod.legacy_v1 import (
                find_legacy_runtime_residual_dirs, is_legacy_read_cache_path, running_dst_processes,
            )
            from dstools.features.mod.parser import (
                find_workshop_content_dirs, find_workshop_dir, find_workshop_residual_dirs,
            )
            from dstools.features.mod.workshop_acf import read_acf_subscribers
            from dstools.features.mod.workshop_status import inspect_workshop_items
            discovered_paths = {
                int(str(wid).removeprefix("workshop-")): path
                for wid, path in self.page._mod_paths.items()
                if str(wid).removeprefix("workshop-").isdigit() and not is_legacy_read_cache_path(path)}
            return inspect_workshop_items(
                ids, discovered_paths=discovered_paths, legacy_active_root=self.page._server_mods_root(),
                query_source=True, include_subscribed=True, configured_ids=configured_ids,
                residual_paths=find_workshop_residual_dirs(), workshop_content_paths=find_workshop_content_dirs(),
                legacy_runtime_residual_paths=find_legacy_runtime_residual_dirs(),
                running_dst_processes=running_dst_processes(),
                acf_subscribers=read_acf_subscribers(find_workshop_dir()))

        def done(states) -> None:
            self._loading = False
            self._refresh_btn.setEnabled(True)
            self._states = {str(k): v for k, v in states.items()}
            self.page._workshop_status_cache = dict(states)
            self.page._workshop_status_checked_at = time.monotonic()
            self.page._workshop_scan_ids = (self.page._workshop_status_checked_at, [int(wid) for wid in self._ids])
            self.page._update_workshop_update_hint()
            self._refresh_account_filter()
            self._render_rows()

        def error(exc: Exception) -> None:
            self._loading = False
            self._refresh_btn.setEnabled(True)
            self._count_label.setText(str(exc))

        run_async(work, done, error)

    # ── 渲染 ────────────────────────────────────────────────────────────
    def _make_header(self) -> QWidget:
        header = QWidget()
        header.setFixedHeight(34)
        layout = QHBoxLayout(header)
        layout.setContentsMargins(12, 4, 12, 4)
        select_all = QCheckBox(t("mod.update_select_all"))
        select_all.setTristate(False)
        select_all.setFont(theme.font("FONT_SIZE_SM", bold=True))
        select_all.clicked.connect(self._on_select_all_clicked)
        self._select_all_box = select_all
        layout.addWidget(select_all)
        layout.addStretch()
        for text, width in ((t("mod.update_latest_version"), 150), (t("mod.update_workshop_column"), 90),
                            (t("mod.update_action"), 110)):
            label = QLabel(text)
            label.setFont(theme.font("FONT_SIZE_SM", bold=True))
            label.setProperty("muted", True)
            label.setFixedWidth(width)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(label)
        return header

    def _on_select_all_clicked(self, checked: bool) -> None:
        selectable = [wid for wid in self._filtered_ids() if self._can_check(wid)]
        if checked:
            self._selected.update(selectable)
        else:
            self._selected.difference_update(selectable)
        self._render_rows()

    def _row_signature(self, wid: str) -> tuple:
        """行内容依赖的全部数据；变了才重建这一行（勾选和底色由 _place_row 原地更新）。"""
        info = self.page._mod_infos.get(f"workshop-{wid}") or self.page._mod_infos.get(wid)
        image = self.page._icon_imgs.get(f"workshop-{wid}") or self.page._icon_imgs.get(wid)
        account = self.page.ctx.env.current_account(Platform.STEAM)
        return (self._states.get(wid), self._name_for(wid), version_display(info), id(image) if image else None,
                wid in self._cleanup_running, account.id if account is not None else None)

    def _place_row(self, row: Card, wid: str, index: int) -> None:
        row.set_fill_key("CARD_BG_ALT" if index % 2 == 0 else "CARD_BG")
        checkbox = self._row_checkboxes.get(wid)
        if checkbox is not None and checkbox.isChecked() != (wid in self._selected):
            checkbox.blockSignals(True)
            checkbox.setChecked(wid in self._selected)
            checkbox.blockSignals(False)

    def _render_rows(self) -> None:
        pending = self._pending_count()
        needs_update_label = t("mod.update_filter_needs_update")
        self._status_filter.set_labels([
            t("mod.show_all"),
            f"{needs_update_label}（{pending}）" if pending else needs_update_label,
            t("mod.update_filter_current"),
        ])
        visible_ids = self._filtered_ids()
        self._count_label.setText(t("mod.update_selected_count", selected=len(self._selected), total=len(self._ids)))
        selectable = [wid for wid in visible_ids if self._can_check(wid)]
        self._select_all_box.setEnabled(bool(selectable))
        self._select_all_box.setChecked(bool(selectable) and all(wid in self._selected for wid in selectable))
        self._row_list.render(visible_ids, signature=self._row_signature, build=lambda wid: self._make_row(wid, 0),
                              place=self._place_row, empty=self._make_empty_label)
        quick = self._quick_update_ids()
        self._quick_update_btn.setText(
            t("mod.update_quick_btn_count", count=len(quick)) if quick else t("mod.update_quick_btn"))
        self._quick_update_btn.setEnabled(bool(quick) and not self._loading)
        self._update_cleanup_all_btn()

    def _selected_force_ids(self) -> list[str]:
        """勾选中可以强制清理的其他账号 Mod。"""
        return [wid for wid in self._ids if wid in self._selected
                and (status := self._states.get(wid)) is not None and self._can_force_cleanup(status)]

    def _update_cleanup_all_btn(self) -> None:
        has_residual = any(status.can_cleanup_residual and status.state != WorkshopModState.UNSUBSCRIBED_REFERENCED
                           for status in self._states.values())
        self._cleanup_all_btn.setEnabled((has_residual or bool(self._selected_force_ids()))
                                         and not self._cleanup_running)

    @staticmethod
    def _make_empty_label() -> QLabel:
        empty = QLabel(t("mod.no_filtered"))
        empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty.setContentsMargins(0, 24, 0, 24)
        return empty

    def _make_row(self, wid: str, index: int) -> QWidget:
        # 交替行色与主 Mod 列表一致（CARD_BG/CARD_BG_ALT），每行一个 Card 带描边形成独立卡片。
        # 坑：用 Card 自绘而不是给容器设无选择器的 styleSheet，后者会压掉子 QPushButton 的全局样式
        row = Card(radius=0, alpha=255, fill_key="CARD_BG_ALT" if index % 2 == 0 else "CARD_BG")
        row.setFixedHeight(_ROW_H)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(12, 4, 12, 4)
        can_select = self._can_check(wid)
        checkbox = QCheckBox()
        checkbox.setEnabled(can_select)
        if not can_select:
            checkbox.setToolTip(self._force_block_reason(self._states.get(wid)))
        checkbox.setChecked(wid in self._selected)
        checkbox.toggled.connect(lambda checked, w=wid: self._on_check(w, checked))
        self._row_checkboxes[wid] = checkbox
        layout.addWidget(checkbox)

        image = self.page._icon_imgs.get(f"workshop-{wid}") or self.page._icon_imgs.get(wid)
        icon_label = QLabel()
        if image is not None:
            icon_label.setPixmap(pil_to_pixmap(image).scaled(
                _ICON, _ICON, aspectMode=Qt.AspectRatioMode.KeepAspectRatio,
                mode=Qt.TransformationMode.SmoothTransformation))
        icon_label.setFixedSize(_ICON, _ICON)
        layout.addWidget(icon_label)

        text_col = QVBoxLayout()
        text_col.setSpacing(1)
        name_label = QLabel(self._name_for(wid))
        name_label.setFont(theme.font("FONT_SIZE_BASE", bold=True))
        text_col.addWidget(name_label)
        id_label = QLabel(f"workshop-{wid}")
        id_label.setProperty("muted", True)
        id_label.setFont(theme.font("FONT_SIZE_SM"))
        text_col.addWidget(id_label)
        info = self.page._mod_infos.get(f"workshop-{wid}") or self.page._mod_infos.get(wid)
        version_label = QLabel(version_display(info))
        version_label.setProperty("muted", True)
        version_label.setFont(theme.font("FONT_SIZE_SM"))
        version_row = QHBoxLayout()
        version_row.setSpacing(6)
        version_row.addWidget(version_label)
        tag = self._format_tag(self._states.get(wid))
        if tag is not None:
            version_row.addWidget(tag)
        version_row.addStretch()
        text_col.addLayout(version_row)
        layout.addLayout(text_col, 1)

        status = self._states.get(wid)
        latest_text = self._latest_text(status) if status else t("mod.update_latest_checking")
        latest_label = QLabel(latest_text)
        latest_label.setWordWrap(True)
        latest_label.setFixedWidth(150)
        latest_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(latest_label)

        link_label = QLabel(f'<a href="#" style="color:{theme.hex("ACCENT")};">{t("mod.workshop_link_btn")}</a>')
        link_label.setFixedWidth(90)
        link_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        link_label.linkActivated.connect(lambda _href, w=wid: self.page._on_link(f"workshop-{w}"))
        layout.addWidget(link_label)

        action_box = QWidget()
        action_box.setFixedWidth(110)
        action_layout = QHBoxLayout(action_box)
        action_layout.setContentsMargins(0, 0, 0, 0)
        action = self._row_action(wid, status)
        if action is not None:
            label, handler, busy = action
            action_btn = QPushButton(label)
            action_btn.setEnabled(not busy)
            action_btn.setFont(theme.font("FONT_SIZE_SM"))
            if not busy:
                action_btn.clicked.connect(lambda _c=False, w=wid, h=handler: h(w))
            action_layout.addWidget(action_btn)
        layout.addWidget(action_box)
        return row

    @staticmethod
    def _format_tag(status) -> QLabel | None:
        """版本号后面的 V1/V2 小标签，依据是 Steam 自己的 LegacyItem 状态位；
        Steam 还没返回状态（检查中、查询失败）时不显示，不靠猜。"""
        steam = status.evidence.steam_state if status is not None and status.evidence is not None else None
        if steam is None:
            return None
        legacy = steam.legacy_item
        tag = QLabel("V1" if legacy else "V2")
        tag.setFont(theme.font("FONT_SIZE_SM", bold=True))  # 跟版本号文字同字号
        tag.setToolTip(t("mod.format_tag_v1_tip" if legacy else "mod.format_tag_v2_tip"))
        background, color = mod_format_tag_colors("V1" if legacy else "V2")
        # 样式必须用对象名限定：悬停提示框会继承控件样式表，否则提示框也画成圆角、四角露出黑点
        tag.setObjectName("modFormatTag")
        tag.setStyleSheet(f"QLabel#modFormatTag {{ background: {background.name()}; color: {color.name()}; "
                          "border-radius: 4px; padding: 0px 5px; }")
        return tag

    def _subscriber_text(self, account_id: str) -> str:
        """订阅者显示为"昵称 (ID)"，没有昵称时只显示 ID。"""
        name = self.page.ctx.env.steam_names.get(account_id, "")
        return f"{name} ({account_id})" if name else account_id

    def _subscribers_text(self, subscribers) -> str:
        return "、".join(self._subscriber_text(account) for account in subscribers)

    def _latest_text(self, status) -> str:
        """其他账号订阅的 Mod 按存档栏选中的账号区分：订阅者含选中账号时显示"本账号订阅"，否则列出订阅者。"""
        subscribers = status.evidence.acf_subscribers if status.evidence is not None else ()
        if status.state == WorkshopModState.SUBSCRIBED_BY_OTHER_ACCOUNT and subscribers:
            selected = self.page.ctx.env.current_account(Platform.STEAM)
            if selected is not None and selected.id in subscribers:
                return t("mod.update_latest_own_account")
            return t("mod.update_latest_other_account_named", name=self._subscribers_text(subscribers))
        return t(self._latest_key(status))

    @staticmethod
    def _latest_key(status) -> str:
        if status.state == WorkshopModState.UNSUBSCRIBED_PENDING_CLEANUP:
            return "mod.update_latest_unsubscribed_pending_cleanup"
        return _LATEST_LABELS.get(status.state, "mod.update_latest_unknown")

    def _row_action(self, wid: str, status):
        """每行只显示一个最合适的操作，优先级：更新 > 移除失效引用 > 清理残留。"""
        if status is None:
            return None
        if status.state == WorkshopModState.SHADOWED_BY_V1:
            # 更新 V2 解决不了：专服加载的是 mods 里的旧副本，要把它移走
            return t("mod.update_clean_shadow_btn"), self._clean_shadow, wid in self._cleanup_running
        if status.can_update:
            return t("mod.update_one_btn"), self._update_one, False
        if status.state == WorkshopModState.UNSUBSCRIBED_REFERENCED:
            return t("mod.update_remove_reference_btn"), self._remove_reference, False
        if status.can_cleanup_residual:
            return t("mod.update_cleanup_residual_btn"), self._cleanup_residual, wid in self._cleanup_running
        if self._can_force_cleanup(status):
            return t("mod.update_force_cleanup_btn"), self._force_cleanup_other, wid in self._cleanup_running
        return None

    @staticmethod
    def _can_force_cleanup(status) -> bool:
        """本机其他账号订阅、当前存档没有引用、内容目录还在的 Mod 才允许强制清理。
        游戏是否运行不在这里判断（运行中也能勾选），点击清理时再拦截。"""
        evidence = status.evidence
        return bool(status.state == WorkshopModState.SUBSCRIBED_BY_OTHER_ACCOUNT and evidence is not None
                    and not evidence.configured and evidence.workshop_content_path is not None)

    @staticmethod
    def _force_block_reason(status) -> str:
        """其他账号订阅的 Mod 不能勾选的原因，用作勾选框提示。"""
        evidence = status.evidence if status is not None else None
        if status is None or status.state != WorkshopModState.SUBSCRIBED_BY_OTHER_ACCOUNT or evidence is None:
            return ""
        if evidence.configured:
            return t("mod.update_force_blocked_referenced")
        if evidence.workshop_content_path is None:
            return t("mod.update_cannot_cleanup")
        return ""

    def _confirm_game_closed(self) -> bool:
        """游戏或专服运行中不能关闭 Steam 清理：提示后返回 False。"""
        from dstools.features.mod.legacy_v1 import running_dst_processes

        processes = running_dst_processes()
        if processes:
            dialogs.show_warning(self, t("mod.update_force_cleanup_title"),
                                 t("mod.update_force_cleanup_game_running", processes="、".join(processes)))
            return False
        return True

    def _on_check(self, wid: str, checked: bool) -> None:
        if checked:
            self._selected.add(wid)
        else:
            self._selected.discard(wid)
        self._count_label.setText(t("mod.update_selected_count", selected=len(self._selected), total=len(self._ids)))
        self._update_cleanup_all_btn()

    # ── 更新执行 ────────────────────────────────────────────────────────
    def _confirm_and_update(self, ids: list[str]) -> None:
        if not ids:
            dialogs.show_info(self, t("mod.update_title"), t("mod.update_none_selected"))
            return
        if not dialogs.ask_yes_no(self, t("mod.update_confirm_title"), t("mod.update_confirm_message", count=len(ids))):
            return
        expected_versions = {int(wid): self._states[wid].update_expected_version
                            for wid in ids if wid in self._states and self._states[wid].update_expected_version}
        force_redownload_ids = {int(wid) for wid in ids
                               if wid in self._states and self._states[wid].state == WorkshopModState.SUSPECTED_OUTDATED}
        self.accept()
        self._run_update([int(wid) for wid in ids], expected_versions, force_redownload_ids)

    def _update_selected(self) -> None:
        self._confirm_and_update([wid for wid in self._ids if wid in self._selected and self._can_select(wid)])

    def _quick_update_ids(self) -> list[str]:
        """一键更新的范围：处于"待更新"且可以直接更新的 Mod，已是最新的不再重复交给 Steam。"""
        return [wid for wid in self._ids if self._can_select(wid) and self._states[wid].needs_action]

    def _quick_update(self) -> None:
        self._confirm_and_update(self._quick_update_ids())

    def _update_one(self, wid: str) -> None:
        self._confirm_and_update([wid])

    def _run_update(self, ids: list[int], expected_versions: dict, force_redownload_ids: set) -> None:
        progress = dialogs.LogDialog(self.page.window(), t("mod.update_log_title"), closable=True)
        progress.show()
        progress.append(t("mod.update_log_start", count=len(ids)))

        def on_item_start(current, total, workshop_id) -> None:
            progress.append(t("mod.update_log_item_start", current=current, total=total,
                              name=self._name_for(str(workshop_id))))

        def on_line(current, total, result) -> None:
            name = self._name_for(str(result.workshop_id))
            extras = result.details.get("legacy_extras_removed") or ()
            if extras:
                progress.append(t("mod.update_log_item_extras_removed", current=current, total=total,
                                  name=name, count=len(extras)))
            if result.completed and result.up_to_date:
                progress.append(t("mod.update_log_item_current", current=current, total=total, name=name))
            elif result.completed:
                progress.append(t("mod.update_log_item_updated", current=current, total=total, name=name))
            else:
                progress.append(t("mod.update_log_item_failed", current=current, total=total, name=name,
                                  error=str(result.error or t("mod.update_latest_unknown"))))

        def on_finish(updated, up_to_date, failed, cancelled=False, error=None) -> None:
            skipped = max(0, len(ids) - (updated + up_to_date + failed))
            key = "mod.update_log_stopped" if cancelled else "mod.update_log_summary"
            progress.append(t(key, processed=updated + up_to_date + failed, total=len(ids),
                             updated=updated, up_to_date=up_to_date, failed=failed, skipped=skipped))
            progress.finish()
            self.page._finish_workshop_update(updated, up_to_date, failed, cancelled=cancelled, error=error)

        self.page._update_workshop_mods(ids, expected_versions=expected_versions,
                                        force_redownload_ids=force_redownload_ids,
                                        on_item_start=on_item_start, on_line=on_line, on_finish=on_finish)

    # ── 移除失效引用 ─────────────────────────────────────────────────────
    def _remove_reference(self, wid: str) -> None:
        if not dialogs.ask_yes_no(self, t("mod.update_remove_reference_title"),
                                   t("mod.update_remove_reference_confirm", mod_id=f"workshop-{wid}"), danger=True):
            return
        changed = 0
        cluster = self.page.get_cluster()
        for shard in cluster.shards if cluster else ():
            if not shard.mod_overrides_path:
                continue
            overrides = load_mod_overrides(shard.mod_overrides_path)
            removed = False
            for key in (f"workshop-{wid}", wid):
                removed = overrides.mods.pop(key, None) is not None or removed
            if removed:
                save_mod_overrides(overrides)
                changed += 1
        if not changed:
            dialogs.show_info(self, t("mod.update_title"), t("mod.update_reference_not_found"))
            return
        self.page._workshop_status_checked_at = 0.0
        self.accept()
        self.page._refresh_mods(full=False)
        dialogs.show_info(self.page.window(), t("mod.update_remove_reference_title"),
                          t("mod.update_reference_removed", count=changed))

    def _clean_shadow(self, wid: str) -> None:
        """清理挡住 V2 的专服 mods 旧副本（文件夹进回收站、链接只删链接），完成后渐隐提示。"""
        from dstools.features.mod.v1_shadow import ShadowedMod, remove_shadowed_mods

        status = self._states.get(wid)
        evidence = status.evidence if status is not None else None
        shadow_path = evidence.v1_shadow_path if evidence is not None else None
        if shadow_path is None or wid in self._cleanup_running:
            return
        try:
            remove_shadowed_mods([ShadowedMod(str(wid), shadow_path, shadow_path)])
        except OSError as exc:
            dialogs.show_error(self, t("mod.update_clean_shadow_btn"), t("local.v1_shadow_failed", error=str(exc)))
            return
        dialogs.show_toast(self, t("mod.update_clean_shadow_done"), ms=2400)
        self._reload(force=True)

    # ── 残留清理 ────────────────────────────────────────────────────────
    def _cleanup_residual(self, wid: str) -> None:
        if wid in self._cleanup_running:
            return
        status = self._states.get(wid)
        paths = _cleanup_paths(status)
        if not paths or status is None or not status.can_cleanup_residual:
            dialogs.show_warning(self, t("mod.update_title"), t("mod.update_cannot_cleanup"))
            return
        tree_text = format_residual_directory_tree(paths)

        def open_folder() -> None:
            target = next((p for p in paths if p.is_dir()), None)
            if target is not None:
                try:
                    os.startfile(str(target))
                except OSError as exc:
                    dialogs.show_error(self, t("mod.update_cleanup_residual_title"),
                                       t("mod.update_cleanup_open_failed", error=exc))

        dialog = _ResidualConfirmDialog(self, tree_text, open_folder)
        if not dialog.exec():
            return
        self._start_residual_cleanup([wid], bulk=False)

    def _cleanup_all_residuals(self) -> None:
        if self._cleanup_running:
            return
        force_ids = self._selected_force_ids()
        if force_ids:
            owners = sorted(set().union(*(self._states[wid].evidence.acf_subscribers for wid in force_ids)))
            owner_text = self._subscribers_text(owners) or t("mod.update_latest_other_account")
            if self._confirm_game_closed() and dialogs.ask_yes_no(self, t("mod.update_force_cleanup_title"),
                                  t("mod.update_force_cleanup_bulk_confirm", count=len(force_ids), owners=owner_text),
                                  min_width=560, danger=True):
                self._start_force_cleanup(force_ids)
            return
        candidates = [wid for wid, status in self._states.items()
                     if status.can_cleanup_residual and status.state != WorkshopModState.UNSUBSCRIBED_REFERENCED
                     and _cleanup_paths(status)]
        if not candidates:
            # 目录早已删掉的用户：残留没有了，但 Steam 清单里的记录仍会触发重新下载
            self._offer_acf_cleanup(show_empty=True)
            return
        paths = tuple(path for wid in candidates for path in _cleanup_paths(self._states.get(wid)))
        tree_text = format_residual_directory_tree(paths)
        dialog = _BulkResidualChoiceDialog(self, len(candidates), tree_text)
        if not dialog.exec() or dialog.mode is None:
            return
        if dialog.mode == "empty":
            candidates = [wid for wid in candidates
                         if any(_is_empty_directory(p) for p in _cleanup_paths(self._states.get(wid)))]
            if not candidates:
                dialogs.show_info(self, t("mod.update_cleanup_all_title"), t("mod.update_cleanup_empty_none"))
                return
        self._start_residual_cleanup(candidates, bulk=True, empty_only=dialog.mode == "empty")

    def _show_state_notice(self, message: str) -> None:
        self._state_notice.setText(message)
        self._state_notice.setVisible(bool(message))

    def _start_residual_cleanup(self, ids: list[str], *, bulk: bool, empty_only: bool = False) -> None:
        self._cleanup_running.update(ids)
        self._show_state_notice(t("mod.update_cleanup_all_checking", count=len(ids)) if bulk
                                else t("mod.update_cleanup_residual_checking"))
        self._render_rows()

        def work():
            from dstools.features.mod.legacy_v1 import (
                find_legacy_runtime_residual_dirs, running_dst_processes,
            )
            from dstools.features.mod.parser import (
                find_workshop_content_dirs, find_workshop_dir, find_workshop_residual_dirs,
            )
            from dstools.features.mod.workshop_acf import read_acf_subscribers
            from dstools.features.mod.workshop_status import inspect_workshop_items
            cleaned: list[str] = []
            errors: dict[str, Exception] = {}
            try:
                processes = running_dst_processes()
                context: ResidualCleanupContext = build_residual_cleanup_context(running_processes=processes)
                numeric_ids = [int(wid) for wid in ids]
                fresh_states = inspect_workshop_items(
                    numeric_ids, query_source=False, residual_paths=find_workshop_residual_dirs(),
                    workshop_content_paths=find_workshop_content_dirs(),
                    legacy_runtime_residual_paths=find_legacy_runtime_residual_dirs(),
                    running_dst_processes=processes,
                    acf_subscribers=read_acf_subscribers(find_workshop_dir()))
            except (OSError, ValueError, KeyError) as exc:
                fresh_states = {}
                errors.update({wid: exc for wid in ids})
                context = None
            for wid in ids:
                if wid in errors:
                    continue
                try:
                    fresh = fresh_states[int(wid)]
                    if not fresh.can_cleanup_residual or fresh.evidence is None:
                        raise ValueError(t("mod.update_cannot_cleanup"))
                    content_path = fresh.evidence.workshop_content_path or fresh.evidence.residual_path
                    deleted_any = False
                    if content_path is not None and (not empty_only or _is_empty_directory(content_path)):
                        delete_workshop_residual(int(wid), content_path, fresh.evidence.steam_state, context=context)
                        deleted_any = True
                    if not empty_only:
                        for runtime_path in fresh.evidence.legacy_runtime_residual_paths:
                            delete_legacy_runtime_residual(int(wid), runtime_path, fresh.evidence.steam_state,
                                                           context=context)
                            deleted_any = True
                    if not deleted_any:
                        raise ValueError(t("mod.update_cleanup_empty_changed"))
                    cleaned.append(wid)
                except (OSError, ValueError, KeyError) as exc:
                    errors[wid] = exc
            return cleaned, errors

        def done(result) -> None:
            cleaned, errors = result
            self._cleanup_running.difference_update(ids)
            self._apply_cleaned_items(cleaned)
            self._show_state_notice("")
            self._render_rows()
            if errors:
                # 同一原因合并成一组，避免几十个 Mod 逐条重复同一句话
                groups: dict[str, list[str]] = {}
                for wid, error in sorted(errors.items(), key=lambda item: int(item[0])):
                    groups.setdefault(str(error), []).append(wid)
                details = "\n\n".join(
                    t("mod.update_cleanup_error_group", reason=reason, count=len(wids),
                      ids=", ".join(wids))
                    for reason, wids in sorted(groups.items(), key=lambda item: -len(item[1])))
                dialogs.show_error(self, t("mod.update_cleanup_residual_title"),
                                   t("mod.update_cleanup_all_result", success=len(cleaned),
                                     failed=len(errors), details=details))
            elif bulk:
                dialogs.show_toast(self, t("mod.update_cleanup_all_done_toast", count=len(cleaned)))
            else:
                dialogs.show_toast(self, t("mod.update_cleanup_residual_done_toast"))
            if cleaned:
                self._offer_acf_cleanup(show_empty=False)

        def error(exc: Exception) -> None:
            self._cleanup_running.difference_update(ids)
            self._show_state_notice("")
            self._render_rows()
            dialogs.show_error(self, t("mod.update_cleanup_residual_title"), str(exc))

        run_async(work, done, error)

    def _force_cleanup_other(self, wid: str) -> None:
        """强制清理其他账号订阅的 Mod：关闭 Steam、删文件、清除下载记录后重启 Steam。"""
        if self._cleanup_running:
            return
        status = self._states.get(wid)
        if status is None or not self._can_force_cleanup(status):
            dialogs.show_warning(self, t("mod.update_title"), t("mod.update_cannot_cleanup"))
            return
        path = status.evidence.workshop_content_path
        owner = self._subscribers_text(status.evidence.acf_subscribers) or t("mod.update_latest_other_account")
        if not self._confirm_game_closed():
            return
        if not dialogs.ask_yes_no(self, t("mod.update_force_cleanup_title"),
                                  t("mod.update_force_cleanup_confirm", owner=owner, path=str(path)),
                                  min_width=560, danger=True):
            return
        self._start_force_cleanup([wid])

    def _start_force_cleanup(self, ids: list[str]) -> None:
        """后台强制清理一批其他账号订阅的 Mod：整批只关闭/重启一次 Steam。"""
        content_root = self._states[ids[0]].evidence.workshop_content_path.parent
        self._cleanup_running.update(ids)
        self._show_state_notice(t("mod.update_force_cleanup_running"))
        self._render_rows()

        def work():
            from dstools.features.local_service.steam_client_updater import (
                is_steam_running, launch_steam, shutdown_steam,
            )
            from dstools.features.mod.legacy_v1 import running_dst_processes
            from dstools.features.mod.workshop_cleanup import force_remove_other_account_items
            return force_remove_other_account_items(
                content_root, ids, is_steam_running=is_steam_running, shutdown_steam=shutdown_steam,
                launch_steam=launch_steam, running_dst_processes=running_dst_processes)

        def finish() -> None:
            self._cleanup_running.difference_update(ids)
            self._show_state_notice("")

        def done(result) -> None:
            finish()
            self._apply_cleaned_items(list(result.removed))
            self._refresh_account_filter()
            self._render_rows()
            if result.errors:
                details = "\n".join(f"{item}: {reason}" for item, reason in result.errors.items())
                dialogs.show_error(self, t("mod.update_force_cleanup_title"),
                                   t("mod.update_force_cleanup_failed", details=details))
            elif len(result.removed) > 1:
                dialogs.show_toast(self, t("mod.update_force_cleanup_bulk_done_toast", count=len(result.removed)),
                                   ms=2400)
            elif result.removed:
                dialogs.show_toast(self, t("mod.update_force_cleanup_done_toast"), ms=2400)

        def error(exc: Exception) -> None:
            finish()
            self._render_rows()
            dialogs.show_error(self, t("mod.update_force_cleanup_title"), str(exc))

        run_async(work, done, error)

    # ── Steam 下载记录清理 ──────────────────────────────────────────────
    def _offer_acf_cleanup(self, *, show_empty: bool) -> None:
        """删掉目录后 appworkshop_322330.acf 仍记录"已安装"，Steam 会重新下载；
        找出未订阅、目录已不存在且未被存档引用的记录，确认后清除。"""
        if self._cleanup_running:
            return
        referenced = {wid for wid, status in self._states.items()
                      if status.evidence is not None and status.evidence.configured}

        def work():
            from dstools.features.mod.parser import find_workshop_dir
            from dstools.features.mod.workshop_acf import (
                find_orphan_records, read_workshop_acf, workshop_acf_path,
            )
            root = find_workshop_dir()
            if root is None or not workshop_acf_path(root).is_file():
                return root, []
            return root, find_orphan_records(read_workshop_acf(workshop_acf_path(root)), root, referenced)

        def done(result) -> None:
            root, orphans = result
            if not orphans:
                if show_empty:
                    dialogs.show_info(self, t("mod.update_cleanup_residual_title"),
                                      t("mod.update_cleanup_all_empty"))
                return
            # 正文按 HTML 渲染：先转义纯文本，再把"完全退出 Steam"换成红色粗体
            marker = "\x00"
            body = html.escape(t("mod.acf_orphan_confirm", count=len(orphans), steam_exit=marker))
            emphasis = (f'<b style="color:{theme.hex("ERROR")}">'
                        f'{html.escape(t("mod.acf_orphan_steam_exit"))}</b>')
            body = body.replace(marker, emphasis).replace("\n", "<br>")
            if dialogs.ask_yes_no(self, t("mod.acf_orphan_title"), body, rich=True):
                self._run_acf_cleanup(root, orphans)

        def error(exc: Exception) -> None:
            dialogs.show_error(self, t("mod.acf_orphan_title"), str(exc))

        run_async(work, done, error)

    def _run_acf_cleanup(self, root, orphans: list[str]) -> None:
        self._cleanup_running.update(orphans)
        self._show_state_notice(t("mod.acf_orphan_running"))
        self._render_rows()

        def work():
            from dstools.features.local_service.steam_client_updater import (
                is_steam_running, launch_steam, shutdown_steam,
            )
            from dstools.features.mod.legacy_v1 import running_dst_processes
            from dstools.features.mod.workshop_acf import clear_orphan_records
            return clear_orphan_records(
                root, orphans, is_steam_running=is_steam_running, shutdown_steam=shutdown_steam,
                launch_steam=launch_steam, running_dst_processes=running_dst_processes)

        def finish() -> None:
            self._cleanup_running.difference_update(orphans)
            self._show_state_notice("")
            self._render_rows()

        def done(result) -> None:
            finish()
            if result.cleared:
                dialogs.show_toast(self, t("mod.acf_orphan_done_toast", count=len(result.cleared)), ms=2400)
            else:
                dialogs.show_info(self, t("mod.acf_orphan_title"), t("mod.acf_orphan_none"))

        def error(exc: Exception) -> None:
            finish()
            dialogs.show_error(self, t("mod.acf_orphan_title"), str(exc))

        run_async(work, done, error)

    def _apply_cleaned_items(self, cleaned_ids: list[str]) -> None:
        changed_main_list = False
        page = self.page
        for wid in cleaned_ids:
            self._states.pop(wid, None)
            numeric_id = int(wid)
            page._workshop_status_cache.pop(numeric_id, None)
            page._workshop_status_cache.pop(wid, None)
            self._selected.discard(wid)
            self._ids = [item for item in self._ids if item != wid]
            mod_keys = (wid, f"workshop-{wid}")
            for key in mod_keys:
                changed_main_list = page._mod_data.pop(key, None) is not None or changed_main_list
                page._mod_infos.pop(key, None)
                page._mod_paths.pop(key, None)
                page._icon_imgs.pop(key, None)
        if changed_main_list:
            page._update_scan_status_label()
            page._render_list()
        if cleaned_ids:
            page.ctx.mod_catalog.invalidate(Platform.STEAM)
            page._workshop_status_checked_at = 0.0
            page._update_workshop_update_hint()
