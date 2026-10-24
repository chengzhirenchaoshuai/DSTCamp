"""内网穿透节点选择弹窗（樱花、Lolia 共用）：可滚动卡片网格，支持分组标题、第三行彩色标记、悬停备注，
不满足条件（VIP 等级、实名等）的节点淡化且不可点。
"""

import html
from dataclasses import dataclass, field

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QGridLayout, QLabel, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

from dstools.i18n import t
from dstools.qt import dialogs
from dstools.qt.theme import theme

_GRID_COLS = 3
_CARD_MIN_HEIGHT = 64


@dataclass
class NodeChoice:
    node_id: int
    name: str
    detail: str
    eligible: bool = True
    group: str = ""          # 分组标题（含数量），为空不分组
    badges: list[tuple[str, str]] = field(default_factory=list)  # [(文字, 调色板颜色键)]
    tooltip: str = ""        # 悬停提示（节点备注）


class _NodeCard(QPushButton):
    """卡片式按钮：内部两到三行标签（标签不接收鼠标事件，点击落到按钮上）。"""

    def __init__(self, choice: NodeChoice, selected: bool):
        super().__init__()
        self.setObjectName("nodeCard")
        self.setProperty("selected", selected)
        self.setEnabled(choice.eligible)
        self.setCursor(Qt.CursorShape.PointingHandCursor if choice.eligible else Qt.CursorShape.ArrowCursor)
        # 按钮默认纵向固定高度；放开后同一行的卡片统一拉到该行最高的那张，不参差
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(2)
        name = QLabel(choice.name)
        name.setObjectName("nodeCardName")
        name.setFont(theme.font("FONT_SIZE_BASE", bold=True))
        detail = QLabel(choice.detail)
        detail.setObjectName("nodeCardDetail")
        detail.setFont(theme.font("FONT_SIZE_SM"))
        labels = [name, detail]
        if choice.badges:
            badges = QLabel(" · ".join(
                f'<span style="color:{theme.hex(key)};">{html.escape(text)}</span>' for text, key in choice.badges))
            badges.setTextFormat(Qt.TextFormat.RichText)
            badges.setFont(theme.font("FONT_SIZE_SM", bold=True))
            labels.append(badges)
        for label in labels:
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            label.setEnabled(choice.eligible)
            layout.addWidget(label)
        if choice.tooltip:
            self.setToolTip(choice.tooltip)

    # 不能用 setMinimumHeight() 固定下限（布局不再撑高，第三行会被截断），按内部布局实际需要给尺寸提示
    def sizeHint(self) -> QSize:
        hint = self.layout().sizeHint()
        return QSize(hint.width(), max(_CARD_MIN_HEIGHT, hint.height()))

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()


class NodePickerDialog(dialogs.Dialog):
    def __init__(self, parent, title: str, choices: list[NodeChoice], current_id: int | None):
        super().__init__(parent, title, "lg")
        self.result_id: int | None = None
        self.setStyleSheet(self._style())
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.viewport().setAutoFillBackground(False)
        grid_widget = QWidget()
        grid = QGridLayout(grid_widget)
        grid.setContentsMargins(2, 2, 8, 2)  # 右侧给滚动条留位置
        grid.setSpacing(10)
        row, col, group = 0, 0, None
        for choice in choices:
            if choice.group and choice.group != group:
                group = choice.group
                if col:
                    row, col = row + 1, 0
                header = QLabel(choice.group)
                header.setProperty("heading", True)
                header.setFont(theme.font("FONT_SIZE_MD", bold=True))
                if row:
                    header.setContentsMargins(0, 8, 0, 0)  # 与上一组拉开距离
                grid.addWidget(header, row, 0, 1, _GRID_COLS)
                row += 1
            card = _NodeCard(choice, choice.node_id == current_id)
            card.clicked.connect(lambda _c=False, nid=choice.node_id: self._select(nid))
            grid.addWidget(card, row, col)
            col += 1
            if col == _GRID_COLS:
                row, col = row + 1, 0
        for col in range(_GRID_COLS):
            grid.setColumnStretch(col, 1)
        grid.setRowStretch(grid.rowCount(), 1)
        area.setWidget(grid_widget)
        # QScrollArea.setWidget() 会把内容控件设为自动填充背景，用的是系统默认灰
        # （#efefef），盖住了弹窗的主题底色；关掉填充让主题背景透出来
        grid_widget.setAutoFillBackground(False)
        hint = self.text_label(t("dlg.node_picker_hint"), size_key="FONT_SIZE_BASE")
        hint.setStyleSheet(f"color: {theme.hex('PRIMARY_DARK')};")
        self.body.addWidget(hint)
        self.body.addWidget(area, 1)
        cancel = dialogs.style_button(QPushButton(t("dlg.cancel_btn")), "secondary")
        cancel.clicked.connect(self.reject)
        self.add_footer([cancel], [])
        dialogs.fit_to_screen(self, 760, 560)

    @staticmethod
    def _style() -> str:
        card, alt, border = theme.hex("CARD_BG"), theme.hex("CARD_BG_ALT"), theme.hex("CARD_BORDER")
        accent, light = theme.hex("ACCENT"), theme.hex("PRIMARY_LIGHT")
        text, muted = theme.hex("TEXT"), theme.hex("TEXT_MUTED")
        return f"""
            QPushButton#nodeCard {{ background: {card}; border: 1px solid {border}; border-radius: 10px;
                padding: 0px; text-align: left; }}
            QPushButton#nodeCard:hover {{ background: {light}; border-color: {accent}; }}
            QPushButton#nodeCard[selected="true"] {{ background: {light}; border: 2px solid {accent}; }}
            QPushButton#nodeCard:disabled {{ background: {alt}; border: 1px dashed {border}; }}
            QLabel#nodeCardName {{ color: {text}; }}
            QLabel#nodeCardDetail {{ color: {muted}; }}
            QLabel#nodeCardName:disabled, QLabel#nodeCardDetail:disabled {{ color: {muted}; }}
        """

    def _select(self, node_id: int) -> None:
        self.result_id = node_id
        self.accept()
