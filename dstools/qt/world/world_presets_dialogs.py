"""世界设置页的"保存为预设"与"套用预设"弹窗：读写游戏自己的自定义世界预设（features/world/user_presets.py）。"""

from pathlib import Path

from PySide6.QtCore import QSize
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QLineEdit, QListWidget, QListWidgetItem, QPushButton

from dstools.features.world import user_presets
from dstools.i18n import t
from dstools.qt import dialogs
from dstools.qt.theme import theme

# 与主题 QListWidget::item 的 padding（4px 6px）一致；_ITEM_EXTRA 为样式自带的边框/焦点框余量（本机实测正常行高
# 比字高加内边距多 3px）
_ITEM_PAD_Y = 4
_ITEM_PAD_X = 6
_ITEM_EXTRA = 3


class SaveWorldPresetDialog(dialogs.Dialog):
    """填预设名称与说明；同名（不分大小写）时先确认覆盖。确认后 name/description 为填写结果。"""

    def __init__(self, parent, user_dir: Path):
        super().__init__(parent, t("world.preset_save_btn"), "md")
        self._user_dir = user_dir
        self.name = ""
        self.description = ""
        self.body.addWidget(self.text_label(t("world.preset_save_intro"), muted=True))
        self._name_edit = self._field(t("world.preset_name_label"))
        self._desc_edit = self._field(t("world.preset_desc_label"))
        self._error = self.error_label()
        self.body.addWidget(self._error)
        self.add_buttons()
        self._name_edit.setFocus()

    def _field(self, label: str) -> QLineEdit:
        self.body.addWidget(self.text_label(label))
        edit = QLineEdit()
        edit.setFont(theme.font("FONT_SIZE_BASE"))
        self.body.addWidget(edit)
        return edit

    def accept_if_valid(self) -> None:
        try:
            name = user_presets.validate_name(self._name_edit.text())
        except ValueError as exc:
            self._error.setText(str(exc))
            return
        preset_id = user_presets.preset_id_for_name(name)
        presets, _errors = user_presets.list_presets(self._user_dir)
        if any(p.preset_id.upper() == preset_id.upper() for p in presets) and not dialogs.ask_yes_no(
                self, t("world.preset_save_btn"), t("world.preset_exists", name=name)):
            return
        self.name, self.description = name, self._desc_edit.text().strip()
        self.accept()


class ApplyWorldPresetDialog(dialogs.Dialog):
    """列出与当前世界类型相同的自定义预设，可删除；确认后 selected 为选中的预设。"""

    def __init__(self, parent, user_dir: Path, location: str, location_label: str):
        super().__init__(parent, t("world.preset_apply_btn"), "md", confirm_text=t("world.preset_apply_confirm"))
        self._user_dir = user_dir
        self._location = location
        self.selected: user_presets.UserPreset | None = None
        self._presets: list[user_presets.UserPreset] = []
        self.body.addWidget(self.text_label(t("world.preset_apply_hint", location=location_label)))
        self._list = QListWidget()
        self._list.setMinimumHeight(220)
        self.body.addWidget(self._list, 1)
        self._empty = self.text_label(t("world.preset_none", location=location_label), muted=True)
        self.body.addWidget(self._empty)
        self._errors = self.error_label()
        self.body.addWidget(self._errors)
        # 底部只留一行：删除靠左、套用靠右，不要取消按钮（关窗口即取消）
        self._delete_btn = dialogs.style_button(QPushButton(t("world.preset_delete_btn")), "danger")
        self._delete_btn.setAutoDefault(False)
        self._delete_btn.clicked.connect(self._delete)
        self._confirm = QPushButton(t("world.preset_apply_confirm"))
        self._confirm.setDefault(True)
        self._confirm.clicked.connect(self.accept_if_valid)
        self.add_footer([self._delete_btn], [self._confirm])
        self._reload()

    def _reload(self) -> None:
        presets, errors = user_presets.list_presets(self._user_dir)
        self._presets = [p for p in presets if p.location == self._location]
        self._list.clear()
        # 显式给行高：用户真机上偶尔按不含样式表上下内边距（4px×2）的高度排版、却按含内边距的位置画字，
        # 字底被切掉、下一行压上来，本机复现不出；行高不交给样式表计算就不受时机影响
        metrics = QFontMetrics(self._list.font())
        row_height = metrics.height() + 2 * _ITEM_PAD_Y + _ITEM_EXTRA
        for preset in self._presets:
            text = f"{preset.name}  —  {preset.description}" if preset.description else preset.name
            item = QListWidgetItem(text)
            item.setSizeHint(QSize(metrics.horizontalAdvance(text) + 2 * _ITEM_PAD_X + _ITEM_EXTRA, row_height))
            self._list.addItem(item)
        if self._presets:
            self._list.setCurrentRow(0)
        has_items = bool(self._presets)
        self._list.setVisible(has_items)
        self._empty.setVisible(not has_items)
        self._delete_btn.setEnabled(has_items)
        self._confirm.setEnabled(has_items)
        self._errors.setText(t("world.preset_read_errors", errors="\n".join(errors)) if errors else "")
        self._errors.setVisible(bool(errors))

    def _current(self) -> user_presets.UserPreset | None:
        row = self._list.currentRow()
        return self._presets[row] if 0 <= row < len(self._presets) else None

    def _delete(self) -> None:
        preset = self._current()
        if preset is None or not dialogs.ask_yes_no(
                self, t("world.preset_delete_btn"), t("world.preset_delete_confirm", name=preset.name), danger=True):
            return
        try:
            user_presets.delete_preset(self._user_dir, preset.preset_id)
        except user_presets.UserPresetError as exc:
            dialogs.show_error(self, t("world.preset_delete_btn"), str(exc))
        self._reload()

    def accept_if_valid(self) -> None:
        self.selected = self._current()
        if self.selected is not None:
            self.accept()
