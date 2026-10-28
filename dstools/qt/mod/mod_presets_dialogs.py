"""Mod 配置集相关弹窗：保存、套用、套用报告。"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from dstools.features.mod import presets
from dstools.features.mod.list_model import localize_mod_name
from dstools.i18n import t
from dstools.qt import dialogs
from dstools.qt.theme import theme
from dstools.qt.widgets import Card


class SavePresetDialog(dialogs.Dialog):
    """"保存为配置集"——名字输入 + 勾选要打包哪些 mod（默认勾选当前已启用的）。"""

    def __init__(self, page):
        super().__init__(page.window(), t("preset.save_dialog_title"), 640)
        self.page = page
        self._checks: dict[str, QCheckBox] = {}
        self.body.addWidget(self.text_label(t("preset.save_name_label"), size_key="FONT_SIZE_SM"))
        self._name_edit_row = QHBoxLayout()
        from PySide6.QtWidgets import QLineEdit
        self._name_edit = QLineEdit()
        self._name_edit_row.addWidget(self._name_edit)
        self.body.addLayout(self._name_edit_row)
        self.body.addWidget(self.text_label(t("preset.save_select_hint"), size_key="FONT_SIZE_SM"))

        # 启用/未启用 mod 列表外面套一圈主题色圆角框，框内底色走主题卡片色；
        # 滚动区和内容控件显式透明，否则视口会露出系统调色板的纯灰底。
        list_card = Card(radius=12, alpha=255, fill_key="CARD_BG", border_key="PRIMARY", border=True)
        card_layout = QVBoxLayout(list_card)
        card_layout.setContentsMargins(6, 6, 6, 6)
        area = QScrollArea()
        area.setObjectName("presetModArea")
        area.setWidgetResizable(True)
        area.setMinimumHeight(360)
        area.viewport().setAutoFillBackground(False)
        inner = QWidget()
        inner.setObjectName("presetModInner")
        inner.setAutoFillBackground(False)
        area.setStyleSheet("#presetModArea, #presetModInner { background: transparent; border: none; }")
        layout = QVBoxLayout(inner)

        ordered_ids = sorted(page._mod_data.keys(),
                            key=lambda wid: (page._mod_infos.get(wid).name if page._mod_infos.get(wid) else "") or wid)
        enabled_ids = [wid for wid in ordered_ids if page._mod_data[wid].enabled]
        disabled_ids = [wid for wid in ordered_ids if not page._mod_data[wid].enabled]

        def make_row(wid: str, checked: bool) -> QCheckBox:
            info = page._mod_infos.get(wid)
            name = localize_mod_name(wid, (info.name if info else "") or wid)
            box = QCheckBox(f"{name}  ({wid})")
            box.setChecked(checked)
            self._checks[wid] = box
            return box

        for wid in enabled_ids:
            layout.addWidget(make_row(wid, True))
        if disabled_ids:
            toggle_btn = QPushButton(f"▸ {t('preset.expand_disabled_btn', count=len(disabled_ids))}")
            toggle_btn.setProperty("flat", True)
            layout.addWidget(toggle_btn)
            disabled_container = QWidget()
            disabled_layout = QVBoxLayout(disabled_container)
            disabled_layout.setContentsMargins(0, 0, 0, 0)
            for wid in disabled_ids:
                disabled_layout.addWidget(make_row(wid, False))
            disabled_container.setVisible(False)

            def on_toggle() -> None:
                shown = not disabled_container.isVisible()
                disabled_container.setVisible(shown)
                arrow = "▾" if shown else "▸"
                toggle_btn.setText(f"{arrow} {t('preset.expand_disabled_btn', count=len(disabled_ids))}")

            toggle_btn.clicked.connect(on_toggle)
            layout.addWidget(disabled_container)
        layout.addStretch()
        area.setWidget(inner)
        card_layout.addWidget(area)
        self.body.addWidget(list_card, 1)
        self._error = self.error_label()
        self.body.addWidget(self._error)
        self.add_buttons()

    def accept_if_valid(self) -> None:
        name = self._name_edit.text().strip()
        if not name:
            self._error.setText(t("preset.save_name_empty"))
            return
        selected = {wid for wid, box in self._checks.items() if box.isChecked()}
        if not selected:
            self._error.setText(t("preset.save_none_selected"))
            return
        if presets.find_preset(name) and not dialogs.ask_yes_no(
                self, t("preset.save_dialog_title"), t("preset.save_overwrite_confirm", name=name), danger=True):
            return
        cluster = self.page.get_cluster()
        platform = cluster.platform.value if cluster else ""
        preset = presets.capture_preset(name, self.page._mod_data, self.page._mod_infos, selected, platform)
        presets.save_preset(preset, overwrite=True)
        dialogs.show_info(self, t("preset.save_dialog_title"), t("preset.save_done", name=name, count=len(selected)))
        self.accept()


class ApplyReportDialog(dialogs.Dialog):
    """应用配置集前的预览确认——列出正常写入数量，以及分类问题清单。"""

    def __init__(self, parent, plan: "presets.ApplyPlan"):
        super().__init__(parent, t("preset.report_title", name=plan.preset.name), 480,
                         confirm_text=t("preset.report_confirm_btn"))
        self.clear_first = False
        self.body.addWidget(self.text_label(t("preset.report_ok_count", count=len(plan.ok_ids))))

        def section(title_key: str, items: list[str], color: str) -> None:
            if not items:
                return
            title = QLabel(t(title_key))
            title.setWordWrap(True)
            title.setStyleSheet(f"color: {color};")
            self.body.addWidget(title)
            for text in items:
                label = QLabel(f"· {text}")
                label.setWordWrap(True)
                self.body.addWidget(label)

        missing = [i.display_name for i in plan.issues if i.kind == "missing"]
        stale = [f"{i.display_name}: {i.detail}" for i in plan.issues if i.kind == "stale_option"]
        invalid = [f"{i.display_name}: {i.detail}" for i in plan.issues if i.kind == "invalid_value"]
        section("preset.report_issue_missing_title", missing, theme.hex("ERROR"))
        section("preset.report_issue_stale_title", stale, "#8d6e00")
        section("preset.report_issue_invalid_title", invalid, "#8d6e00")
        if plan.needs_configs_extended:
            note = QLabel(t("preset.report_needs_configs_extended"))
            note.setWordWrap(True)
            note.setStyleSheet("color: #8d6e00;")
            self.body.addWidget(note)

        self._clear_check = QCheckBox(t("preset.clear_first_label"))
        self.body.addWidget(self._clear_check)
        self.add_buttons()

    def accept_if_valid(self) -> None:
        self.clear_first = self._clear_check.isChecked()
        self.accept()


class ApplyPresetDialog(dialogs.Dialog):
    """"应用配置集"——选一个已保存的配置集，先弹预览报告，确认了才真正写盘。"""

    def __init__(self, page):
        super().__init__(page.window(), t("preset.apply_dialog_title"), 460)
        self.page = page
        self._presets = presets.list_presets()
        self.body.addWidget(self.text_label(t("preset.apply_pick_hint")))
        self._list = QListWidget()
        self._list.setMinimumHeight(220)
        self.body.addWidget(self._list, 1)
        self._refill()
        # 底部按钮行："删除"在左、"应用"在右；不放"取消"，关闭窗口即可取消。
        row = QHBoxLayout()
        delete_btn = dialogs.style_button(QPushButton(t("preset.delete_btn")), "danger")
        delete_btn.clicked.connect(self._delete)
        apply_btn = QPushButton(t("preset.apply_btn"))
        apply_btn.clicked.connect(self.accept_if_valid)
        apply_btn.setDefault(True)
        row.addWidget(delete_btn)
        row.addStretch()
        row.addWidget(apply_btn)
        self.body.addSpacing(8)
        self.body.addLayout(row)

    def _refill(self) -> None:
        self._list.clear()
        if not self._presets:
            item = QListWidgetItem(t("preset.apply_none"))
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self._list.addItem(item)
            return
        for preset in self._presets:
            self._list.addItem(f"{preset.name}  ({len(preset.mods)})")

    def _selected_preset(self):
        row = self._list.currentRow()
        if row < 0 or row >= len(self._presets):
            return None
        return self._presets[row]

    def _delete(self) -> None:
        preset = self._selected_preset()
        if preset is None:
            return
        if not dialogs.ask_yes_no(self, t("preset.delete_btn"), t("preset.delete_confirm", name=preset.name), danger=True):
            return
        presets.delete_preset(preset.name)
        self._presets = presets.list_presets()
        self._refill()

    def accept_if_valid(self) -> None:
        preset = self._selected_preset()
        if preset is None:
            return
        cluster = self.page.get_cluster()
        if cluster is None:
            dialogs.show_warning(self, t("preset.apply_dialog_title"), t("local.select_cluster_first"))
            return
        plan = presets.plan_apply_preset(preset, self.page._mod_infos)
        report = ApplyReportDialog(self, plan)
        if not report.exec():
            return
        count = presets.apply_preset(cluster, plan, clear_first=report.clear_first)
        # 配置集直接写盘（不经过"保存修改"按钮）；世界设置页"来自 Mod"的显示依据
        # 的是这份磁盘内容，必须通知它下次打开时重新读取。
        self.page.ctx.cluster_config_saved.emit(cluster)
        dialogs.show_info(self, t("preset.apply_dialog_title"), t("preset.applied_done", count=count))
        self.page._refresh_mods(full=False)
        self.accept()
