"""单个 Mod 的配置编辑器。

每个选项是限定在 modinfo.lua 声明范围内的下拉框（不提供自由输入，手打值可能是 Mod 未预料的）。
"应用"立即写入 modoverrides.lua；"重置"恢复默认值但不写盘；"返回"丢弃未应用的改动。
耗时的沙箱解析（整份文件/动态选项/汉化叠加）在后台进行，数据就绪后一次性构建对话框。
"""

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from dstools.features.mod import chs_translation
from dstools.features.mod.cache import load_cached_result, save_result
from dstools.features.mod.config_memory import recall_for, same_value
from dstools.features.mod.parser import (
    find_mod_folder, resolve_config_value, resolve_full_modinfo, visible_config_options,
)
from dstools.features.mod.sandbox_apply import apply_full_sandbox_result
from dstools.i18n import t
from dstools.qt import dialogs
from dstools.qt.theme import theme
from dstools.qt.threads import run_async
from dstools.qt.widgets import Card


def _resolve_mod_config(page, workshop_id: str, mod_info) -> bool:
    """后台线程跑的耗时部分：整份文件沙箱解析 + 动态选项 + 汉化叠加。
    原地修改共享的 mod_info；返回是否真的改动过全量解析结果（决定要不要刷新主列表）。"""
    changed = False
    if not mod_info.full_sandbox_tried:
        mod_info.full_sandbox_tried = True
        platform, wegame_dir = page._resolve_mod_folder_args(page.get_cluster())
        mod_folder = page._mod_paths.get(workshop_id) or find_mod_folder(workshop_id, platform, wegame_dir)
        if mod_folder:
            modinfo_path = mod_folder / "modinfo.lua"
            result = load_cached_result(workshop_id, modinfo_path)
            if result is None:
                result = resolve_full_modinfo(mod_folder)
                save_result(workshop_id, modinfo_path, result)
            if result:
                apply_full_sandbox_result(mod_info, result)
                page._full_resolved_cache[workshop_id] = mod_info
                changed = True

    if mod_info.dynamic_preamble:
        import time as _time
        from dstools.features.mod.sandbox import resolve_dynamic_option
        deadline = _time.monotonic() + 3.0
        for opt in mod_info.config_options:
            if not opt.is_dynamic:
                continue
            if _time.monotonic() >= deadline:
                break
            choices = resolve_dynamic_option(mod_info.dynamic_preamble, opt.raw_options_expr)
            if choices:
                opt.choices = choices
                opt.is_dynamic = False

    if not mod_info.chs_translation_tried:
        mod_info.chs_translation_tried = True
        platform, wegame_dir = page._resolve_mod_folder_args(page.get_cluster())
        path = chs_translation.find_translation_file(workshop_id, platform, wegame_dir)
        if path:
            translation = chs_translation.resolve_translation(
                path, mod_info.name, mod_info.description, mod_info.version, mod_info.config_options)
            if translation and translation.get("configuration_options"):
                chs_translation.apply_translation(mod_info.config_options,
                                                  translation["configuration_options"])
    return changed


# 同一时间只开一个配置窗口：多个窗口各自"应用"会互相覆盖，窗口开着时切换存档还会把配置写进旧存档的数据。
# 后台解析期间存 True（窗口还没建好），窗口打开后存窗口本身，关闭后清空。
_active_dialog = None


def open_mod_config(page, workshop_id: str, mod, mod_info, read_only: bool, read_only_reason: str,
                    *, default_mode: bool = False) -> None:
    """入口：先后台跑耗时解析，再构建真正的对话框。default_mode 编辑的是默认配置（游戏主菜单"模组"的全局配置）。"""
    global _active_dialog
    if _active_dialog is not None:
        if isinstance(_active_dialog, QDialog):
            _active_dialog.raise_()
            _active_dialog.activateWindow()
        return

    def show_dialog() -> None:
        global _active_dialog
        dialog = ModConfigDialog(page, workshop_id, mod, mod_info, read_only, read_only_reason,
                                 default_mode=default_mode)
        # 模态于所在窗口（主窗口或创建向导）：开着时不能切换存档/世界或再点别的 Mod
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.finished.connect(_clear_active_dialog)
        _active_dialog = dialog
        dialog.show()

    needs_resolve = not mod_info.full_sandbox_tried or not mod_info.chs_translation_tried
    if not needs_resolve:
        show_dialog()
        return

    _active_dialog = True
    # 加载期间只显示忙碌光标，不弹提示小窗（会在选项多的 Mod 上闪一下）
    QApplication.setOverrideCursor(Qt.CursorShape.BusyCursor)

    def finish() -> None:
        global _active_dialog
        QApplication.restoreOverrideCursor()
        _active_dialog = None

    def done(changed: bool) -> None:
        finish()
        if changed:
            page._render_list()
        show_dialog()

    def error(_exc: Exception) -> None:
        finish()
        show_dialog()

    run_async(lambda: _resolve_mod_config(page, workshop_id, mod_info), done, error)


def _clear_active_dialog(_result: int = 0) -> None:
    global _active_dialog
    _active_dialog = None


class ModConfigDialog(QDialog):
    def __init__(self, page, workshop_id: str, mod, mod_info, read_only: bool = False,
                 read_only_reason: str = "client_only", *, default_mode: bool = False):
        super().__init__(page.window())
        self.page = page
        self.workshop_id = workshop_id
        self.mod = mod
        self.mod_info = mod_info
        self.read_only = read_only
        # 默认配置模式：跟游戏主菜单"模组"一致，显示 client=true 的客户端选项，应用后只写默认配置
        self.default_mode = default_mode
        self.vars: dict[str, QComboBox] = {}
        # 按下拉框项的顺序记录 data（与 combo 的 item 索引一一对应），不用显示文本当键，
        # 见 _render_choice_row() 的说明
        self.choice_maps: dict[str, list[Any]] = {}
        # "Configs Extended"(工坊 3317960157) 风格的集合/数组/文本/字典配置项，见
        # _render_raw_value_editor() —— 不走 self.vars/choice_maps 那套下拉框机制。
        self.raw_widgets: dict[str, tuple[str, dict]] = {}
        # 搜索过滤用的行索引：每项 (widget, 已 casefold 的搜索文本, 是否分组标题)。
        self._search_items: list[tuple[QWidget, str, bool]] = []

        self.setWindowTitle(t("mod.config_dialog_title", name=mod_info.name or workshop_id))
        self.setMinimumSize(700, 480)
        dialogs.fit_to_screen(self, 900, 680)

        root = QVBoxLayout(self)
        root.setContentsMargins(*dialogs.DIALOG_MARGINS)
        root.setSpacing(dialogs.DIALOG_SPACING)
        remaining_dynamic = sum(1 for o in mod_info.config_options if o.is_dynamic)
        if read_only:
            banner_key = "mod.read_only_local" if read_only_reason == "client_only" else "mod.read_only_local_save"
            root.addWidget(self._banner(t(banner_key), "#607d8b"))
        elif default_mode:
            root.addWidget(self._banner(t("mod.default_config_banner"), "#607d8b"))
        if mod_info.unsupported_schema:
            root.addWidget(self._banner(t("mod.unsupported_schema"), theme.hex("ERROR")))
        elif remaining_dynamic:
            root.addWidget(self._banner(t("mod.dynamic_banner", count=remaining_dynamic), "#8d6e00"))

        # 搜索框：按配置项标题（label/name）与注释（hover 及每个选项值的
        # description/hover）过滤下方列表，实时过滤、不重建控件。
        search = QLineEdit()
        search.setPlaceholderText(t("mod.search_placeholder"))
        search.setClearButtonEnabled(True)
        search.textChanged.connect(self._apply_search)
        root.addWidget(search)

        area = QScrollArea()
        area.setObjectName("modConfigArea")
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.viewport().setAutoFillBackground(False)
        body = QWidget()
        body.setObjectName("modConfigBody")
        body.setAutoFillBackground(False)
        # 显式透明，否则滚动区露出系统调色板的灰底
        area.setStyleSheet("#modConfigArea, #modConfigArea > QWidget, #modConfigBody "
                           "{ background: transparent; border: none; }")
        self._body_layout = QVBoxLayout(body)
        self._body_layout.setContentsMargins(2, 2, 8, 2)
        self._body_layout.setSpacing(8)
        area.setWidget(body)
        root.addWidget(area, 1)

        real_options = 0
        shown = mod_info.config_options if default_mode else visible_config_options(mod_info.config_options)
        for opt in shown:
            if opt.is_header:
                label_text = opt.label.strip()
                # 分组标题放进一个容器，搜索时整体隐藏/显示——标题、分隔线、
                # 留白本来是散开的多个控件，无法一并控制可见性。
                header_box = QWidget()
                header_box.setAutoFillBackground(False)
                hbox = QVBoxLayout(header_box)
                hbox.setContentsMargins(0, 0, 0, 0)
                hbox.setSpacing(8)
                if label_text:
                    hbox.addSpacing(6)
                    header = QLabel(label_text)
                    header.setFont(theme.font("FONT_SIZE_LG", bold=True))
                    header.setStyleSheet(f"color: {theme.hex('PRIMARY_DARK')};")
                    hbox.addWidget(header)
                    line = QFrame()
                    line.setFixedHeight(2)
                    line.setStyleSheet(f"background: {theme.hex('PRIMARY_LIGHT')}; border: none;")
                    hbox.addWidget(line)
                else:
                    spacer = QWidget()
                    spacer.setFixedHeight(10)
                    hbox.addWidget(spacer)
                self._body_layout.addWidget(header_box)
                self._search_items.append((header_box, "", True))
                continue
            real_options += 1
            if opt.is_set_config or opt.is_array_config or opt.is_text_config or opt.is_dictionary_config:
                current_value = mod.configuration_options.get(opt.name, opt.default)
                widget = self._render_raw_value_editor(opt, current_value)
            else:
                widget = self._render_choice_row(opt)
            self._body_layout.addWidget(widget)
            self._search_items.append((widget, self._opt_search_text(opt), False))

        if not real_options and not mod_info.unsupported_schema:
            self._body_layout.addWidget(QLabel(t("mod.no_config_options")))
        self._body_layout.addStretch()

        # 按弹窗规范：返回（取消类）、重置靠左用浅色描边，应用（主操作）靠右用主题色。
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        back_btn = dialogs.style_button(QPushButton(t("mod.back")), "secondary")
        back_btn.clicked.connect(self.close)
        btn_row.addWidget(back_btn)
        if not read_only:
            reset_btn = dialogs.style_button(QPushButton(t("mod.reset")), "secondary")
            reset_btn.clicked.connect(self._reset)
            btn_row.addWidget(reset_btn)
            if not default_mode and recall_for(workshop_id, mod_info):
                import_btn = dialogs.style_button(QPushButton(t("mod.import_memory_btn")), "secondary")
                import_btn.clicked.connect(self._import_memory)
                btn_row.addWidget(import_btn)
        btn_row.addStretch()
        if not read_only:
            apply_btn = QPushButton(t("mod.apply"))
            apply_btn.clicked.connect(self._apply)
            btn_row.addWidget(apply_btn)
        root.addLayout(btn_row)

    @staticmethod
    def _banner(text: str, color: str) -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet(f"color: {color}; font-weight: bold;")
        label.setFont(theme.font("FONT_SIZE_XS", bold=True))
        return label

    @staticmethod
    def _option_card() -> tuple[QWidget, QVBoxLayout]:
        """单个配置项的容器：主题卡片底色 + 浅描边圆角（替代系统灰色 StyledPanel）。"""
        card = Card(radius=10, alpha=200, fill_key="CARD_BG", border=True)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(4)
        return card, layout

    def _render_choice_row(self, opt) -> QWidget:
        row, layout = self._option_card()
        top = QHBoxLayout()
        label_full = opt.label or opt.name
        name_label = QLabel(label_full)
        name_label.setFont(theme.font("FONT_SIZE_MD", bold=True))
        top.addWidget(name_label, 1)

        current_value = self.mod.configuration_options.get(opt.name, opt.default)
        choices, current_display, _valid = resolve_config_value(self.mod_info, opt.name, current_value)

        # 显示文本去掉首尾空格（部分 Mod 补空格做等宽对齐，比例字体下显得奇怪）；写回的仍是 data 值
        items = [(str(c["description"]).strip(), c["data"]) for c in choices]
        hovers = [str(c.get("hover", "") or "") for c in choices]
        # 当前值不在可选项里（如万界之门的 worlds 表由 DSTCamp 写入）时，最前面加一项原样保留，
        # 否则下拉框落到第一项，一点"应用"就把原值冲掉
        if items and not any(same_value(data, current_value) for _desc, data in items):
            items.insert(0, (t("mod.keep_custom_value"), current_value))
            hovers.insert(0, t("mod.keep_custom_value_hover"))

        if not items:
            reason = t("mod.dynamic_option") if opt.is_dynamic else t("mod.no_choices")
            hint = QLabel(f"{current_display}  ({reason})")
            hint.setStyleSheet(f"color: {theme.hex('TEXT_MUTED')}; font-style: italic;")
            top.addWidget(hint)
            layout.addLayout(top)
            if opt.hover:
                layout.addWidget(self._desc_label(opt.hover))
            return row

        # 按下拉项顺序记录 data/hover 并用索引取值：显示文本可能重复（如 "启用" 对应 true 和 -1），
        # 用文本当键会丢掉其中一项
        self.choice_maps[opt.name] = [data for _desc, data in items]
        combo = QComboBox()
        combo.setMinimumWidth(260)
        combo.setEnabled(not self.read_only)
        for i, (desc, _data) in enumerate(items):
            combo.addItem(desc)
            # 每个下拉项自己的说明用 item 的 ToolTipRole 提供——展开下拉框
            # 后鼠标悬停到某一项上时显示该项注释（strip 掉对齐空格，空则不显示）。
            hover_text = hovers[i].strip()
            if hover_text:
                combo.setItemData(i, hover_text, Qt.ItemDataRole.ToolTipRole)
        # 按已保存的 data 值定位初始项，不能用 findText（显示文本可能重复，只会命中第一个）
        initial = 0
        for idx, (_desc, data) in enumerate(items):
            if same_value(data, current_value):
                initial = idx
                break
        combo.setCurrentIndex(initial)

        # 未展开时鼠标悬停到下拉框本体，显示当前选中项的注释。
        def update_tooltip(_index=None) -> None:
            idx = combo.currentIndex()
            combo.setToolTip(hovers[idx].strip() if 0 <= idx < len(hovers) else "")

        combo.currentIndexChanged.connect(update_tooltip)
        update_tooltip()
        self.vars[opt.name] = combo
        top.addWidget(combo)
        layout.addLayout(top)
        if opt.hover:
            layout.addWidget(self._desc_label(opt.hover))
        return row

    @staticmethod
    def _desc_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet(f"color: {theme.hex('TEXT_MUTED')};")
        label.setFont(theme.font("FONT_SIZE_XS"))
        return label

    @staticmethod
    def _opt_search_text(opt) -> str:
        """一个配置项用于搜索匹配的全部文本：标题（label/name）、选项级注释
        （hover），以及每个选项值的描述和注释。返回已 casefold 的字符串。"""
        parts = [str(opt.label or ""), str(opt.name or ""), str(opt.hover or "")]
        for c in opt.choices:
            parts.append(str(c.get("description", "") or ""))
            parts.append(str(c.get("hover", "") or ""))
        return " ".join(parts).casefold()

    def _apply_search(self, text: str) -> None:
        """按搜索框内容过滤配置列表：匹配标题或注释的选项显示、其余隐藏；
        分组标题只要下面还有匹配项就显示。空搜索恢复全部。"""
        needle = text.strip().casefold()
        items = self._search_items
        n = len(items)
        # 第一遍：计算每个选项是否匹配并设置其可见性。
        matched = [False] * n
        for idx, (widget, search_text, is_header) in enumerate(items):
            if is_header:
                continue
            visible = (not needle) or (needle in search_text)
            matched[idx] = visible
            widget.setVisible(visible)
        # 第二遍：分组标题的可见性 = 后面（到下一个标题为止）有匹配项。
        for idx, (widget, _search_text, is_header) in enumerate(items):
            if not is_header:
                continue
            any_match = False
            for j in range(idx + 1, n):
                if items[j][2]:
                    break
                if matched[j]:
                    any_match = True
                    break
            widget.setVisible(not needle or any_match)

    # ── Configs Extended 风格的集合/数组/字典/文本编辑器 ────────────────
    def _render_raw_value_editor(self, opt, current_value) -> QWidget:
        row, layout = self._option_card()
        header = QLabel(opt.label or opt.name)
        header.setFont(theme.font("FONT_SIZE_MD", bold=True))
        layout.addWidget(header)
        if opt.hover:
            layout.addWidget(self._desc_label(opt.hover))

        if opt.is_text_config:
            edit = QLineEdit("" if current_value is None else str(current_value))
            edit.setEnabled(not self.read_only)
            layout.addWidget(edit)
            self.raw_widgets[opt.name] = ("text", {"edit": edit})
            return row

        if opt.is_dictionary_config:
            pairs = self._raw_value_to_pairs(current_value)
            layout.addWidget(self._render_dict_list_editor(opt.name, pairs))
            return row

        kind = "set" if opt.is_set_config else "array"
        values = self._raw_value_to_lines(kind, current_value)
        layout.addWidget(self._render_item_list_editor(opt.name, kind, values))
        return row

    def _render_item_list_editor(self, name: str, kind: str, values: list) -> QWidget:
        container = QWidget()
        outer = QVBoxLayout(container)
        outer.setContentsMargins(0, 4, 0, 0)
        items_layout = QVBoxLayout()
        outer.addLayout(items_layout)
        entry_edits: list[QLineEdit] = []

        def add_row(initial: str = "") -> None:
            edit = QLineEdit(initial)
            edit.setEnabled(not self.read_only)
            entry_edits.append(edit)
            row = QHBoxLayout()
            row.addWidget(edit, 1)
            remove_btn = QPushButton("×")
            remove_btn.setFixedWidth(28)
            remove_btn.setEnabled(not self.read_only)
            item_widget = QWidget()
            item_widget.setLayout(row)

            def remove() -> None:
                entry_edits.remove(edit)
                item_widget.setParent(None)
                item_widget.deleteLater()

            remove_btn.clicked.connect(remove)
            row.addWidget(remove_btn)
            items_layout.addWidget(item_widget)

        if not self.read_only:
            add_btn = QPushButton(t("mod.add_value_btn"))
            add_btn.clicked.connect(lambda: add_row(""))
            outer.addWidget(add_btn, alignment=Qt.AlignmentFlag.AlignLeft)
        for value in values:
            add_row(value)

        self.raw_widgets[name] = (kind, {"edits": entry_edits, "items_layout": items_layout, "add_row": add_row})
        return container

    def _render_dict_list_editor(self, name: str, pairs: list) -> QWidget:
        container = QWidget()
        outer = QVBoxLayout(container)
        outer.setContentsMargins(0, 4, 0, 0)
        items_layout = QVBoxLayout()
        outer.addLayout(items_layout)
        entry_edits: list[tuple[QLineEdit, QLineEdit]] = []

        def add_row(initial_key: str = "", initial_val: str = "") -> None:
            key_edit, val_edit = QLineEdit(initial_key), QLineEdit(initial_val)
            key_edit.setFixedWidth(140)
            for edit in (key_edit, val_edit):
                edit.setEnabled(not self.read_only)
            entry_edits.append((key_edit, val_edit))
            row = QHBoxLayout()
            row.addWidget(key_edit)
            row.addWidget(QLabel("="))
            row.addWidget(val_edit, 1)
            remove_btn = QPushButton("×")
            remove_btn.setFixedWidth(28)
            remove_btn.setEnabled(not self.read_only)
            item_widget = QWidget()
            item_widget.setLayout(row)

            def remove() -> None:
                entry_edits.remove((key_edit, val_edit))
                item_widget.setParent(None)
                item_widget.deleteLater()

            remove_btn.clicked.connect(remove)
            row.addWidget(remove_btn)
            items_layout.addWidget(item_widget)

        if not self.read_only:
            add_btn = QPushButton(t("mod.add_value_btn"))
            add_btn.clicked.connect(lambda: add_row("", ""))
            outer.addWidget(add_btn, alignment=Qt.AlignmentFlag.AlignLeft)
        for key, value in pairs:
            add_row(key, value)

        self.raw_widgets[name] = ("dict", {"edits": entry_edits, "items_layout": items_layout, "add_row": add_row})
        return container

    @staticmethod
    def _raw_value_to_pairs(value) -> list:
        if not isinstance(value, dict):
            return []
        return sorted((str(k), str(v)) for k, v in value.items())

    @staticmethod
    def _raw_value_to_lines(kind: str, value) -> list:
        if kind == "set":
            return sorted(str(k) for k in value.keys()) if isinstance(value, dict) else []
        if isinstance(value, (list, tuple)):
            return [str(v) for v in value]
        if isinstance(value, dict):
            if not value:
                return []
            try:
                idx_keys = sorted(int(k) for k in value.keys())
            except (TypeError, ValueError):
                return []
            if idx_keys == list(range(1, len(idx_keys) + 1)):
                return [str(value[str(k)]) for k in idx_keys]
        return []

    def _read_raw_widget_value(self, kind: str, data: dict) -> Any:
        if kind == "text":
            return data["edit"].text()
        if kind == "dict":
            result = {}
            for key_edit, val_edit in data["edits"]:
                key = key_edit.text().strip()
                if key:
                    result[key] = val_edit.text().strip()
            return result
        lines = [edit.text().strip() for edit in data["edits"] if edit.text().strip()]
        if kind == "set":
            return {line: True for line in lines}
        return lines

    def _reset_raw_widget(self, opt, kind: str, data: dict) -> None:
        self._fill_raw_widget(kind, data, opt.default)

    def _fill_raw_widget(self, kind: str, data: dict, value: Any) -> None:
        if kind == "text":
            data["edit"].setText("" if value is None else str(value))
            return
        for i in reversed(range(data["items_layout"].count())):
            item = data["items_layout"].takeAt(i)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        data["edits"].clear()
        if kind == "dict":
            for key, item in self._raw_value_to_pairs(value):
                data["add_row"](key, item)
            return
        for item in self._raw_value_to_lines(kind, value):
            data["add_row"](item)

    # ── 应用 / 重置 ─────────────────────────────────────────────────────
    def _reset(self) -> None:
        for opt in self.mod_info.config_options:
            if opt.is_header:
                continue
            if opt.name in self.raw_widgets:
                kind, data = self.raw_widgets[opt.name]
                self._reset_raw_widget(opt, kind, data)
                continue
            if opt.name not in self.vars:
                continue
            datas = self.choice_maps[opt.name]
            for idx, data in enumerate(datas):
                if data == opt.default:
                    self.vars[opt.name].setCurrentIndex(idx)
                    break

    def _collect_values(self) -> dict:
        """读取界面上各配置项的当前值。"""
        values = {}
        for opt in self.mod_info.config_options:
            if opt.is_header:
                continue
            if opt.name in self.raw_widgets:
                kind, data = self.raw_widgets[opt.name]
                values[opt.name] = self._read_raw_widget_value(kind, data)
                continue
            if opt.name not in self.vars:
                continue
            datas = self.choice_maps[opt.name]
            idx = self.vars[opt.name].currentIndex()
            if 0 <= idx < len(datas):
                values[opt.name] = datas[idx]
        return values

    def _import_memory(self) -> None:
        """把配置记忆（DSTCamp 或游戏里最近一次的配置）填进界面，先列出差异确认；仍需"应用"才保存。"""
        memory = recall_for(self.workshop_id, self.mod_info)
        current = self._collect_values()
        changes = [opt for opt in self.mod_info.config_options
                   if not opt.is_header and opt.name in memory and opt.name in current
                   and not same_value(current[opt.name], memory[opt.name])]
        if not changes:
            dialogs.show_info(self, t("mod.import_memory_btn"), t("mod.import_memory_none"))
            return
        lines = [f"{opt.label or opt.name}: {self._describe(opt, current[opt.name])} → "
                 f"{self._describe(opt, memory[opt.name])}" for opt in changes[:15]]
        if len(changes) > 15:
            lines.append(t("mod.import_memory_more", count=len(changes) - 15))
        if not dialogs.ask_yes_no(self, t("mod.import_memory_btn"),
                                  t("mod.import_memory_confirm", changes="\n".join(lines))):
            return
        for opt in changes:
            value = memory[opt.name]
            if opt.name in self.raw_widgets:
                kind, data = self.raw_widgets[opt.name]
                self._fill_raw_widget(kind, data, value)
                continue
            for idx, data in enumerate(self.choice_maps[opt.name]):
                if same_value(data, value):
                    self.vars[opt.name].setCurrentIndex(idx)
                    break

    @staticmethod
    def _describe(opt, value: Any) -> str:
        for choice in opt.choices:
            if same_value(choice.get("data"), value):
                return str(choice.get("description") or value)
        return str(value)

    def _apply(self) -> None:
        self.mod.configuration_options.update(self._collect_values())
        if self.default_mode:
            # 默认配置不进存档，只保存到 DSTCamp 并与游戏全局配置同步
            self.page._save_default_config(self.workshop_id, self.mod.configuration_options)
            self.close()
            return
        # 只改内存里的配置，由页面决定何时写入（Mod 页等"保存修改"，创建向导随创建写入）。
        # 存档配置不影响默认配置（与游戏一致）。
        self.page.config_applied()
        self.close()
