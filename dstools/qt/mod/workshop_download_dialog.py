"""Mod 页"按 ID 下载"（仅 SteamCMD 通道）：输入工坊 ID 或链接，交给 steamcmd_mods.run_steamcmd_mod_update 下载。"""

from __future__ import annotations

from PySide6.QtWidgets import QPlainTextEdit, QPushButton

from dstools.features.mod.workshop_webapi import parse_workshop_ids
from dstools.i18n import t
from dstools.qt import dialogs
from dstools.qt.theme import theme

_PREVIEW_IDS = 8  # 识别结果最多列出的 ID 数，其余用"……"代替


class WorkshopDownloadDialog(dialogs.Dialog):
    """多行输入框，边输入边显示识别到的 ID；确认后 workshop_ids 为要下载的 ID。"""

    def __init__(self, parent):
        super().__init__(parent, t("mod.download_by_id_title"), width="md")
        self.workshop_ids: list[str] = []
        self.body.addWidget(self.text_label(t("mod.download_by_id_prompt"), size_key="FONT_SIZE_MD"))
        self._edit = QPlainTextEdit()
        self._edit.setFont(theme.font("FONT_SIZE_SM"))
        self._edit.setPlaceholderText("https://steamcommunity.com/sharedfiles/filedetails/?id=3444078585")
        self._edit.setFixedHeight(96)
        self._edit.textChanged.connect(self._on_text_changed)
        self.body.addWidget(self._edit)
        self._parsed = self.text_label("", muted=True)
        self.body.addWidget(self._parsed)
        self._invalid = self.error_label()
        self.body.addWidget(self._invalid)
        self.body.addWidget(self.text_label(t("mod.download_by_id_hint_steamcmd"), muted=True))
        cancel = dialogs.style_button(QPushButton(t("dlg.cancel_btn")), "secondary")
        cancel.clicked.connect(self.reject)
        self._confirm = QPushButton(t("mod.recommend_download"))
        self._confirm.clicked.connect(self.accept_if_valid)
        self.add_footer([cancel], [self._confirm])
        self._on_text_changed()
        self._edit.setFocus()

    def _on_text_changed(self) -> None:
        ids, invalid = parse_workshop_ids(self._edit.toPlainText())
        shown = "、".join(ids[:_PREVIEW_IDS]) + ("……" if len(ids) > _PREVIEW_IDS else "")
        self._parsed.setText(t("mod.download_by_id_parsed", count=len(ids), ids=shown) if ids else "")
        self._parsed.setVisible(bool(ids))
        self._invalid.setText(t("mod.download_by_id_invalid", items="、".join(invalid)) if invalid else "")
        self._invalid.setVisible(bool(invalid))
        self._confirm.setEnabled(bool(ids))
        self.workshop_ids = ids

    def accept_if_valid(self) -> None:
        if self.workshop_ids:
            self.accept()
