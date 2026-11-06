"""「客户端Mod转为服务器Mod」（3486375086）的客户端 Mod 勾选列表，样式与"更新 Mod"窗口一致：
搜索框与已选计数、全选表头，每个 Mod 一张交替底色的行卡片（勾选框、图标、名称、ID 与版本、工坊链接）。

只管勾选状态；version/config 由 ModConfigDialog 在"应用"时经 client_mod_bridge 后台生成。
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QLineEdit, QVBoxLayout, QWidget

from dstools.features.mod import client_mod_bridge
from dstools.features.mod.list_model import localize_mod_name, version_display
from dstools.i18n import t
from dstools.qt.imaging import pil_to_pixmap
from dstools.qt.theme import theme
from dstools.qt.widgets import Card

_ROW_H = 64
_ICON = 44


class ClientModPicker(QWidget):
    changed = Signal()

    def __init__(self, page, current: dict, read_only: bool = False, parent=None):
        super().__init__(parent)
        self._page = page
        self._read_only = read_only
        infos = getattr(page, "_mod_infos", {}) or {}
        self._infos = {key: info for key, info in infos.items() if client_mod_bridge.is_eligible(info)}
        names = {key: str(localize_mod_name(key, info.name or "") or key) for key, info in self._infos.items()}
        # 已在列表里、本机却没装（或已不是纯客户端 Mod）的排在最后，保留原记录，取消勾选才删除
        missing = [key for key in current if key not in self._infos]
        self._names = {**names, **{key: key for key in missing}}
        self._order = sorted(names, key=lambda k: names[k].casefold()) + missing
        self._checks: dict[str, QCheckBox] = {}
        self._rows: dict[str, Card] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 4, 0, 0)
        outer.setSpacing(0)

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 6)
        self._search = QLineEdit()
        self._search.setPlaceholderText(t("mod.bridge_search"))
        self._search.setClearButtonEnabled(True)
        self._search.setFixedWidth(220)
        self._search.textChanged.connect(self._apply_filter)
        toolbar.addWidget(self._search)
        toolbar.addStretch()
        self._count_label = QLabel("")
        self._count_label.setProperty("muted", True)
        toolbar.addWidget(self._count_label)
        outer.addLayout(toolbar)

        header_card = Card(radius=0, alpha=255, fill_key="PRIMARY_LIGHT")
        header = QHBoxLayout(header_card)
        header.setContentsMargins(12, 4, 12, 4)
        self._select_all = QCheckBox(t("mod.update_select_all"))
        self._select_all.setFont(theme.font("FONT_SIZE_SM", bold=True))
        self._select_all.setEnabled(not read_only and bool(self._order))
        self._select_all.clicked.connect(self._on_select_all)
        header.addWidget(self._select_all)
        header.addStretch()
        if self._can_link():
            link_head = QLabel(t("mod.update_workshop_column"))
            link_head.setFont(theme.font("FONT_SIZE_SM", bold=True))
            link_head.setProperty("muted", True)
            link_head.setFixedWidth(90)
            link_head.setAlignment(Qt.AlignmentFlag.AlignCenter)
            header.addWidget(link_head)
        header_card.setFixedHeight(34)
        outer.addWidget(header_card)

        self._rows_box = QVBoxLayout()
        self._rows_box.setContentsMargins(0, 3, 0, 3)
        self._rows_box.setSpacing(3)
        outer.addLayout(self._rows_box)
        for key in self._order:
            row = self._make_row(key, key in current)
            self._rows[key] = row
            self._rows_box.addWidget(row)
        self._empty = QLabel(t("mod.bridge_none") if not self._order else t("mod.no_filtered"))
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setContentsMargins(0, 18, 0, 18)
        self._empty.setProperty("muted", True)
        outer.addWidget(self._empty)
        self._apply_filter()

    # ── 对外 ────────────────────────────────────────────────────────────
    def selected(self) -> list[str]:
        return [key for key in self._order if self._checks[key].isChecked()]

    def set_selected(self, keys) -> None:
        keys = set(keys)
        for key, box in self._checks.items():
            box.blockSignals(True)
            box.setChecked(key in keys)
            box.blockSignals(False)
        self._update_counts()

    # ── 行 ──────────────────────────────────────────────────────────────
    def _can_link(self) -> bool:
        return callable(getattr(self._page, "_on_link", None))

    def _make_row(self, key: str, checked: bool) -> Card:
        # 坑：用 Card 自绘底色而不是给容器设无选择器的 styleSheet，后者会压掉子控件的全局样式
        row = Card(radius=0, alpha=255, fill_key="CARD_BG")
        row.setFixedHeight(_ROW_H)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(12, 4, 12, 4)
        box = QCheckBox()
        box.setChecked(checked)
        box.setEnabled(not self._read_only)
        box.toggled.connect(self._on_toggled)
        self._checks[key] = box
        layout.addWidget(box)

        installed = key in self._infos
        icons = getattr(self._page, "_icon_imgs", {}) or {}
        image = icons.get(key)
        icon_label = QLabel()
        if image is not None:
            icon_label.setPixmap(pil_to_pixmap(image).scaled(
                _ICON, _ICON, aspectMode=Qt.AspectRatioMode.KeepAspectRatio,
                mode=Qt.TransformationMode.SmoothTransformation))
        icon_label.setFixedSize(_ICON, _ICON)
        layout.addWidget(icon_label)

        text_col = QVBoxLayout()
        text_col.setSpacing(1)
        name_label = QLabel(self._names[key])
        name_label.setFont(theme.font("FONT_SIZE_BASE", bold=True))
        text_col.addWidget(name_label)
        detail = f"{key}    {version_display(self._infos[key])}" if installed else t("mod.bridge_not_installed",
                                                                                         mod_id=key)
        detail_label = QLabel(detail)
        detail_label.setProperty("muted", True)
        detail_label.setFont(theme.font("FONT_SIZE_SM"))
        text_col.addWidget(detail_label)
        layout.addLayout(text_col, 1)

        if self._can_link():
            link = QLabel(f'<a href="#" style="color:{theme.hex("ACCENT")};">{t("mod.workshop_link_btn")}</a>')
            link.setFixedWidth(90)
            link.setAlignment(Qt.AlignmentFlag.AlignCenter)
            if key.removeprefix("workshop-").isdecimal():
                link.linkActivated.connect(lambda _href, k=key: self._page._on_link(k))
            else:
                link.clear()
            layout.addWidget(link)
        return row

    # ── 交互 ────────────────────────────────────────────────────────────
    def _visible_keys(self) -> list[str]:
        needle = self._search.text().strip().casefold()
        return [key for key in self._order if not needle or needle in self._names[key].casefold()
                or needle in key.casefold()]

    def _apply_filter(self, _text: str = "") -> None:
        visible = set(self._visible_keys())
        index = 0
        for key in self._order:
            row = self._rows[key]
            shown = key in visible
            row.setVisible(shown)
            if shown:
                # 交替底色按可见行重新计算，与"更新 Mod"窗口一致
                row.set_fill_key("CARD_BG_ALT" if index % 2 == 0 else "CARD_BG")
                index += 1
        self._empty.setVisible(not visible)
        self._update_counts()

    def _on_select_all(self, checked: bool) -> None:
        for key in self._visible_keys():
            box = self._checks[key]
            box.blockSignals(True)
            box.setChecked(checked)
            box.blockSignals(False)
        self._update_counts()
        self.changed.emit()

    def _on_toggled(self, _checked: bool) -> None:
        self._update_counts()
        self.changed.emit()

    def _update_counts(self) -> None:
        self._count_label.setText(t("mod.update_selected_count", selected=len(self.selected()),
                                    total=len(self._order)))
        visible = self._visible_keys()
        self._select_all.blockSignals(True)
        self._select_all.setChecked(bool(visible) and all(self._checks[k].isChecked() for k in visible))
        self._select_all.blockSignals(False)
