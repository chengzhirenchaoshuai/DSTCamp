"""切换语言时把全应用已创建控件上的静态文字一次性替换为新语言（含隐藏页面和菜单）。

按"旧语言原文 → 同一 key 的新语言译文"遍历替换，避免各页手写 retranslate 遗漏；带参数的动态文字由各页刷新逻辑重新生成。
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QAbstractButton, QApplication, QComboBox, QGroupBox, QLabel, QLineEdit, QMenu, QWidget,
)

from dstools.i18n import build_text_translation, translate_static_text


def retranslate_all_widgets(from_lang: str, to_lang: str) -> int:
    """返回替换的文字条数。"""
    if from_lang == to_lang:
        return 0
    mapping = build_text_translation(from_lang, to_lang)
    changed = 0

    def swap(getter, setter) -> None:
        nonlocal changed
        new = translate_static_text(getter(), mapping)
        if new is not None:
            setter(new)
            changed += 1

    for widget in QApplication.allWidgets():
        if isinstance(widget, QLabel):
            text = widget.text()
            if not text.lstrip().startswith("<"):  # 富文本（链接等）由各自页面重新生成
                swap(widget.text, widget.setText)
        elif isinstance(widget, QAbstractButton):
            swap(widget.text, widget.setText)
        elif isinstance(widget, QLineEdit):
            swap(widget.placeholderText, widget.setPlaceholderText)
        elif isinstance(widget, QGroupBox):
            swap(widget.title, widget.setTitle)
        elif isinstance(widget, QComboBox):
            for index in range(widget.count()):
                swap(lambda i=index: widget.itemText(i), lambda value, i=index: widget.setItemText(i, value))
        elif isinstance(widget, QMenu):
            swap(widget.title, widget.setTitle)
            for action in widget.actions():
                swap(action.text, action.setText)
                swap(action.toolTip, action.setToolTip)
        _retranslate_custom(widget, mapping)
        swap(widget.toolTip, widget.setToolTip)
        if widget.isWindow():
            swap(widget.windowTitle, widget.setWindowTitle)
    return changed


def _retranslate_custom(widget: QWidget, mapping: dict[str, str]) -> None:
    """项目里自绘的控件：胶囊页签条（_labels + set_labels）、自绘菜单项/提示条（_text + setText/set_text）；
    文字由程序拼出来的控件（如世界下拉的“（主世界）”后缀）提供 refresh_labels() 按当前语言重拼。"""
    refresh = getattr(widget, "refresh_labels", None)
    if callable(refresh):
        refresh()
    labels = getattr(widget, "_labels", None)
    if isinstance(labels, list) and hasattr(widget, "set_labels"):
        widget.set_labels([translate_static_text(label, mapping) or label for label in labels])
    text = getattr(widget, "_text", None)
    if isinstance(text, str):
        new = translate_static_text(text, mapping)
        setter = getattr(widget, "setText", None) or getattr(widget, "set_text", None)
        if new is not None and setter is not None:
            setter(new)
