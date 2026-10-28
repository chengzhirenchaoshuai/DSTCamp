"""订阅推荐 Mod 的引导弹窗。"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget

from dstools.features.local_service.steam_backend import SteamBackend, get_backend
from dstools.features.mod.parser import is_mod_subscribed
from dstools.i18n import t
from dstools.models import Platform
from dstools.qt import dialogs
from dstools.qt.mod.mod_panel import _DEFAULT_ICON_PATH
from dstools.qt.theme import theme
from dstools.qt.widgets import Card
from dstools.shared.resource_paths import bundled_resource_dir

# （工坊 ID, Mod 名称, 简介文案 key）
RECOMMENDED_MODS = [
    ("3444078585", "DontStarveLuaJit2", "mod.recommend_desc_luajit"),
    ("2941527805", "Chinese++ Pro", "mod.recommend_desc_chinese_pp"),
    ("2998347052", "Say about your ping(Server)", "mod.recommend_desc_ping"),
    ("3822260794", "万界之门(WorldGate)", "mod.recommend_desc_worldgate"),
]

_ICON = 56  # 图标边长（逻辑像素）


class RecommendModsDialog(dialogs.Dialog):
    """每个推荐 Mod 一张圆角卡片：图标、名称、简介，右侧为订阅按钮或"已订阅"标记。

    SteamCMD 通道没有订阅：按钮改为直接用 SteamCMD 下载，标记为"已下载"。"""

    def __init__(self, page):
        super().__init__(page.window(), t("mod.recommend_title"), width="lg")
        self.page = page
        inner = QWidget()
        inner.setObjectName("recommendInner")
        inner.setAutoFillBackground(False)
        inner_layout = QVBoxLayout(inner)
        inner_layout.setContentsMargins(0, 0, 0, 0)
        inner_layout.setSpacing(10)
        icon_dir = bundled_resource_dir() / "icons" / "recommended"
        for wid, name, desc_key in RECOMMENDED_MODS:
            inner_layout.addWidget(self._mod_card(wid, name, t(desc_key), icon_dir / f"{wid}.png"))
        inner_layout.addStretch()

        area = QScrollArea()
        area.setObjectName("recommendArea")
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setWidgetResizable(True)
        # 视口和内层都不自绘底色，否则会盖出一块灰色底
        area.viewport().setAutoFillBackground(False)
        area.setStyleSheet("#recommendArea, #recommendInner { background: transparent; border: none; }")
        area.setWidget(inner)
        self.body.addWidget(area, 1)

        close_btn = dialogs.style_button(QPushButton(t("dlg.close_btn")), "secondary")
        close_btn.clicked.connect(self.accept)
        self.add_footer(right=[close_btn])
        dialogs.fit_to_screen(self, dialogs.DIALOG_WIDTHS["lg"], 120 + 92 * len(RECOMMENDED_MODS))

    def _mod_card(self, wid: str, name: str, desc: str, icon_path) -> QWidget:
        card = Card(radius=12, alpha=170)
        row = QHBoxLayout(card)
        row.setContentsMargins(14, 12, 14, 12)
        row.setSpacing(14)

        icon = QLabel()
        icon.setFixedSize(_ICON, _ICON)
        pixmap = self._icon_pixmap(icon_path if icon_path.exists() else _DEFAULT_ICON_PATH)
        if pixmap is not None:
            icon.setPixmap(pixmap)
        row.addWidget(icon, 0, Qt.AlignmentFlag.AlignVCenter)

        text = QVBoxLayout()
        text.setSpacing(4)
        name_label = QLabel(name)
        name_label.setFont(theme.font("FONT_SIZE_MD", bold=True))
        name_label.setWordWrap(True)
        text.addWidget(name_label)
        desc_label = QLabel(desc)
        desc_label.setFont(theme.font("FONT_SIZE_SM"))
        desc_label.setWordWrap(True)
        desc_label.setProperty("muted", True)
        text.addWidget(desc_label)
        row.addLayout(text, 1)

        # Steam 通道只管 Steam 平台；WeGame 平台仍按原方式打开 WeGame 页面
        steamcmd = self.page.ctx.platform == Platform.STEAM and get_backend() is SteamBackend.STEAMCMD
        done_label = QLabel("✓ " + t("mod.recommend_downloaded" if steamcmd else "mod.recommend_subscribed"))
        done_label.setFont(theme.font("FONT_SIZE_SM", bold=True))
        done_label.setStyleSheet(f"color: {theme.hex('SUCCESS')};")
        row.addWidget(done_label, 0, Qt.AlignmentFlag.AlignVCenter)
        if is_mod_subscribed(wid):
            return card
        done_label.hide()
        sub_btn = QPushButton(t("mod.recommend_download" if steamcmd else "mod.recommend_subscribe"))
        if steamcmd:
            sub_btn.clicked.connect(lambda _c=False, w=wid, b=sub_btn, d=done_label: self._download(w, b, d))
        else:
            sub_btn.clicked.connect(lambda _c=False, w=wid: self.page._on_link(f"workshop-{w}"))
        row.addWidget(sub_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        return card

    def _download(self, wid: str, button: QPushButton, done_label: QLabel) -> None:
        from dstools.qt.mod.steamcmd_mods import run_steamcmd_mod_update

        def finished() -> None:
            if is_mod_subscribed(wid):
                button.hide()
                done_label.show()
            self.page.ctx.workshop_mods_changed.emit()

        run_steamcmd_mod_update(self, [wid], on_finished=finished)

    def _icon_pixmap(self, path) -> QPixmap | None:
        source = QPixmap(str(path))
        if source.isNull():
            return None
        # 按屏幕缩放比缩到物理像素再设 devicePixelRatio，避免被二次放大发虚
        dpr = self.devicePixelRatioF()
        side = round(_ICON * dpr)
        pixmap = source.scaled(side, side, Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
        pixmap.setDevicePixelRatio(dpr)
        return pixmap
