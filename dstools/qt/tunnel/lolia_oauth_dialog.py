"""Lolia OAuth 应用设置引导（内置应用失效时的兜底）。

内置 public 应用所有用户共用；被删除/停用（令牌接口返回 invalid_client）时，引导用户在 Lolia 控制台创建自己的
应用并填入 client_id。建议配置依据官方 OAuth 文档：public + PKCE，回调用回环地址 127.0.0.1（按 RFC 8252 忽略端口）。
"""

import webbrowser

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton

from dstools.features.lolia import api as lolia_api
from dstools.i18n import t
from dstools.qt import dialogs
from dstools.qt.theme import theme
from dstools.qt.threads import run_async
from dstools.shared import app_settings

_HOMEPAGE = "https://github.com/chengzhirenchaoshuai/DSTCamp"


class LoliaOAuthAppDialog(dialogs.Dialog):
    """`invalid=True` 表示因当前应用失效而自动打开，顶部给出红色说明。"""

    def __init__(self, parent, invalid: bool = False):
        super().__init__(parent, t("lolia.oauth_title"), "lg")
        self.changed = False
        self.body.addWidget(self.text_label(t("lolia.oauth_intro"), size_key="FONT_SIZE_MD"))
        if invalid:
            warn = self.error_label()
            warn.setText(t("lolia.oauth_invalid"))
            self.body.addWidget(warn)
        custom = app_settings.get_lolia_client_id()
        source = t("lolia.oauth_source_custom", id=custom) if custom else t("lolia.oauth_source_default")
        self.body.addWidget(self.text_label(t("lolia.oauth_current", source=source), muted=True))

        open_row = QHBoxLayout()
        open_btn = QPushButton(t("lolia.oauth_open_create"))
        open_btn.clicked.connect(lambda: webbrowser.open(lolia_api.CREATE_APP_URL))
        open_row.addWidget(open_btn)
        open_row.addStretch()
        self.body.addLayout(open_row)

        self.body.addWidget(self.heading_label(t("lolia.oauth_fields")))
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(4)
        fields = [
            ("lolia.oauth_field_name", "DSTCamp", None),
            ("lolia.oauth_field_homepage", _HOMEPAGE, None),
            ("lolia.oauth_field_type", "public", "lolia.oauth_note_type"),
            ("lolia.oauth_field_redirect", f"http://127.0.0.1{lolia_api.REDIRECT_PATH}", "lolia.oauth_note_redirect"),
            ("lolia.oauth_field_desc", t("lolia.oauth_desc_value"), None),
        ]
        row = 0
        for label_key, value, note_key in fields:
            grid.addWidget(QLabel(t(label_key)), row, 0)
            value_label = QLabel(value)
            value_label.setFont(QFont("Consolas", 10))
            value_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            value_label.setWordWrap(True)
            grid.addWidget(value_label, row, 1)
            copy_btn = dialogs.style_button(QPushButton(t("lolia.oauth_copy_btn")), "secondary")
            copy_btn.clicked.connect(lambda _c=False, v=value: self._copy(v))
            grid.addWidget(copy_btn, row, 2)
            row += 1
            if note_key:
                note = self.text_label(t(note_key), muted=True)
                grid.addWidget(note, row, 1, 1, 2)
                row += 1
        grid.setColumnStretch(1, 1)
        self.body.addLayout(grid)

        self.body.addWidget(self.heading_label(t("lolia.oauth_client_id_label")))
        self._edit = QLineEdit(custom or "")
        self._edit.setFont(QFont("Consolas", 11))
        self._edit.setPlaceholderText(t("lolia.oauth_client_id_placeholder"))
        self.body.addWidget(self._edit)
        self._status = self.error_label()
        self.body.addWidget(self._status)

        cancel = dialogs.style_button(QPushButton(t("dlg.cancel_btn")), "secondary")
        cancel.clicked.connect(self.reject)
        reset = dialogs.style_button(QPushButton(t("lolia.oauth_reset_btn")), "secondary")
        reset.clicked.connect(lambda: self._apply(None))
        reset.setEnabled(bool(custom))
        self._save_btn = QPushButton(t("lolia.oauth_save_btn"))
        self._save_btn.setDefault(True)
        self._save_btn.clicked.connect(self.accept_if_valid)
        self.add_footer([cancel], [reset, self._save_btn])

    def _copy(self, value: str) -> None:
        QGuiApplication.clipboard().setText(value)
        dialogs.show_toast(self, t("lolia.oauth_copied"))

    def accept_if_valid(self) -> None:
        cid = self._edit.text().strip()
        if not cid:
            return
        self._save_btn.setEnabled(False)
        self._status.setStyleSheet(f"color: {theme.hex('TEXT_MUTED')};")
        self._status.setText(t("lolia.oauth_checking"))

        def done(valid: bool) -> None:
            self._save_btn.setEnabled(True)
            if valid:
                self._apply(cid)
                return
            self._status.setStyleSheet(f"color: {theme.hex('ERROR')};")
            self._status.setText(t("lolia.oauth_check_invalid"))

        def error(exc: Exception) -> None:
            self._save_btn.setEnabled(True)
            self._status.setStyleSheet(f"color: {theme.hex('ERROR')};")
            self._status.setText(t("lolia.api_error", detail=str(exc)))

        run_async(lambda: lolia_api.check_client_id(cid), done, error)

    def _apply(self, cid: str | None) -> None:
        """保存（None 为恢复内置）。换了应用旧令牌就不能再刷新，清掉本地登录状态。"""
        new = None if cid == lolia_api.DEFAULT_CLIENT_ID else cid
        if new != app_settings.get_lolia_client_id():
            app_settings.set_lolia_client_id(new)
            lolia_api.clear_tokens()
            self.changed = True
        self.accept()
