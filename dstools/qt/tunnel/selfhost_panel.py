"""内网穿透页的"自建节点"子页签：同一台 VPS 提供 FRP 映射，以及实验性大厅加速
（专服流量经 Mihomo TUN + WireGuard 从 VPS 出口发出）。纯逻辑在 features/frp_selfhost，这里只做编排和界面。
"""

import threading
import time
import webbrowser
from collections import deque

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QStackedWidget,
    QVBoxLayout, QWidget,
)

from dstools.features.cluster_config.config_manager import (
    get_cluster_option, load_cluster_config, load_shard_config, save_shard_config, set_shard_option,
)
from dstools.features.frp_selfhost import connectivity, deploy, host_firewall, probe, remote_deploy, wireguard_deploy
from dstools.features.frp_selfhost.client import FrpcManager, FrpcStatus, build_frpc_toml
from dstools.features.frp_selfhost.lobby_accel import LobbyAccelCoordinator, LobbyAccelError, LobbyAccelStatus
from dstools.features.frp_selfhost.lobby_diagnostics import ConnectionKind, DiagnosticRoute, LobbyDiagnosticSession
from dstools.features.frp_selfhost.mihomo import MihomoError, sha256_file
from dstools.features.frp_selfhost.wireguard import DEFAULT_WIREGUARD_PORT, ensure_client_keypair
from dstools.features.local_service.shard_helpers import RUNNING_LIKE
from dstools.i18n import t
from dstools.models import SaveSource
from dstools.qt import dialogs
from dstools.qt.tunnel import frpc_tool
from dstools.qt.theme import theme
from dstools.qt.threads import post_to_ui, run_async
from dstools.qt.widgets import AutoHideLabel, PillTabBar, ToggleSwitch, clear_layout, section_card
from dstools.shared import app_settings, frpc_diagnosis
from dstools.qt.tunnel.lan_mapping_guard import ensure_lan_free_for_mapping, ensure_no_other_mapping
from dstools.shared.frpc_log import FrpcOutputLog
from dstools.shared.resource_paths import data_dir
from dstools.shared.server_ports import stable_path_key

_FRPC_DATA_NAME = "frp_selfhost_config"
MIHOMO_RELEASES_URL = "https://github.com/MetaCubeX/mihomo/releases"
MIHOMO_LANZOU_URL = "https://wwblt.lanzout.com/iN5Vd4714e6h"
MIHOMO_LANZOU_CODE = "c0mu"
_PROBE_INTERVAL_MS = 10 * 60 * 1000

_UDP_CHECK_KEY_BY_STATUS = {
    "captured": "selfhost.conn_udp_captured", "not_captured": "selfhost.conn_udp_not_captured",
    "responded": "selfhost.conn_udp_responded", "refused": "selfhost.conn_udp_refused",
    "unknown": "selfhost.conn_udp_unknown", "error": "selfhost.conn_udp_error",
}


def _format_ports(ports: list[tuple[int, str]]) -> str:
    return "、".join(f"{proto.upper()} {port}" for port, proto in ports)


def _frpc_exe_path():
    return frpc_tool.frpc_exe_path()


def _muted(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setProperty("muted", True)
    label.setFont(theme.font("FONT_SIZE_SM"))
    return label


def _set_colored_text(label: QLabel, text: str, color: str) -> None:
    """轮询里反复调用：只在变化时写入，重设样式表会触发样式重算和重绘。"""
    style = f"color: {color};"
    if label.text() != text:
        label.setText(text)
    if label.styleSheet() != style:
        label.setStyleSheet(style)


def _make_pill_bar(labels: list[str]) -> PillTabBar:
    return PillTabBar(labels, height=32, pill_height=24, font_size_key="FONT_SIZE_SM")


class _SSHAuthSetupDialog(dialogs.Dialog):
    """初次鉴权只收集连接信息——这一步本身就是用密码去推公钥，不提供"已有密钥"选项。"""

    def __init__(self, parent, default_host: str, default_port: int = 22, default_username: str = "root"):
        super().__init__(parent, t("selfhost.ssh_auth_dialog_title"), 480, confirm_text=t("selfhost.ssh_auth_start_btn"))
        self.result_conn: dict | None = None
        self.body.addWidget(self.text_label(t("selfhost.ssh_auth_dialog_hint"), size_key="FONT_SIZE_SM"))
        grid = QGridLayout()
        self._host = QLineEdit(default_host)
        self._port = QLineEdit(str(default_port))
        self._user = QLineEdit(default_username)
        self._password = QLineEdit()
        self._password.setEchoMode(QLineEdit.EchoMode.Password)
        for row, (key, widget) in enumerate((
            ("selfhost.host_label", self._host), ("selfhost.ssh_port_label", self._port),
            ("selfhost.ssh_username_label", self._user), ("selfhost.ssh_password_label", self._password),
        )):
            grid.addWidget(self.text_label(t(key), wrap=False), row, 0)
            grid.addWidget(widget, row, 1)
        self.body.addLayout(grid)
        self._error = self.error_label()
        self.body.addWidget(self._error)
        self.add_buttons()

    def accept_if_valid(self) -> None:
        host = self._host.text().strip()
        try:
            port = int(self._port.text().strip())
        except ValueError:
            port = -1
        username = self._user.text().strip()
        password = self._password.text()
        if not host:
            self._error.setText(t("selfhost.host_missing"))
            return
        if not (1 <= port <= 65535):
            self._error.setText(t("selfhost.invalid_port"))
            return
        if not username:
            self._error.setText(t("selfhost.ssh_username_missing"))
            return
        if not password:
            self._error.setText(t("selfhost.ssh_password_missing"))
            return
        self.result_conn = {"host": host, "port": port, "username": username, "password": password}
        self.accept()


class _NodeSettingsDialog(QDialog):
    """节点连接信息——非模态，跟主页面一起活着，探测/部署进度靠 panel 反过来调 refresh()。"""

    def __init__(self, panel: "SelfHostPanel"):
        super().__init__(panel.window())
        self.panel = panel
        self.setWindowTitle(t("selfhost.node_settings_title"))
        self.setMinimumWidth(dialogs.DIALOG_WIDTHS["md"])
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*dialogs.DIALOG_MARGINS)
        layout.setSpacing(dialogs.DIALOG_SPACING)
        hint = QLabel(t("selfhost.node_settings_hint"))
        hint.setWordWrap(True)
        hint.setFont(theme.font("FONT_SIZE_SM"))
        hint.setProperty("muted", True)
        layout.addWidget(hint)

        # 概览只显示文字（"标题 / 内容"两列），操作按钮横排在下方；端口和 Token 只在「连接设置」里改
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(1, 1)
        self._host_label = QLabel()
        self._conn_label = QLabel()
        self._conn_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        # 服务器上 frps 的实际端口与本机记录不一致时的提醒（探测结果到了才有）
        self._conn_warning = AutoHideLabel()
        self._conn_warning.setWordWrap(True)
        self._conn_warning.setFont(theme.font("FONT_SIZE_SM"))
        self._conn_warning.setStyleSheet(f"color: {theme.hex('ERROR')};")
        conn_box = QVBoxLayout()
        conn_box.setSpacing(2)
        conn_box.addWidget(self._conn_label)
        conn_box.addWidget(self._conn_warning)
        deploy_note = QLabel(t("selfhost.deploy_note"))
        deploy_note.setWordWrap(True)
        deploy_note.setFont(theme.font("FONT_SIZE_SM"))
        deploy_note.setProperty("muted", True)
        rows = (("selfhost.row_server", self._host_label),
                ("selfhost.row_connection", conn_box),
                ("selfhost.row_deploy", deploy_note))
        for row, (title_key, content) in enumerate(rows):
            title = QLabel(t(title_key))
            title.setFont(theme.font("FONT_SIZE_BASE", bold=True))
            grid.addWidget(title, row, 0, Qt.AlignmentFlag.AlignTop)
            if isinstance(content, QVBoxLayout):
                grid.addLayout(content, row, 1)
            else:
                grid.addWidget(content, row, 1)
        layout.addLayout(grid)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        auth_btn = QPushButton(t("selfhost.ssh_auth_btn"))
        auth_btn.clicked.connect(panel._open_ssh_auth_dialog)
        actions.addWidget(auth_btn)
        self._conn_settings_btn = QPushButton(t("selfhost.conn_settings_btn"))
        self._conn_settings_btn.clicked.connect(self._on_conn_settings_clicked)
        actions.addWidget(self._conn_settings_btn)
        self._deploy_btn = QPushButton(t("selfhost.ssh_deploy_btn"))
        self._deploy_btn.clicked.connect(self._on_deploy_clicked)
        actions.addWidget(self._deploy_btn)
        actions.addStretch()
        close_btn = dialogs.style_button(QPushButton(t("dlg.close_btn")), "secondary")
        close_btn.clicked.connect(self.close)
        actions.addWidget(close_btn)
        layout.addSpacing(6)
        layout.addLayout(actions)
        self.set_connection_display(panel._pending_bind_port, panel._pending_token)
        self.refresh()

    def _on_deploy_clicked(self) -> None:
        self.panel._start_deploy()

    def _on_conn_settings_clicked(self) -> None:
        dialog = _ConnectionSettingsDialog(self, str(self.panel._pending_bind_port), self.panel._pending_token,
                                           app_settings.get_selfhost_ssh_connection() if self.panel._is_authenticated() else None)
        if dialog.exec() and dialog.result is not None:
            self.panel._save_connection(*dialog.result)
            self.refresh()

    def set_connection_display(self, bind_port, token: str) -> None:
        # 概览里 Token 只露首尾，完整值在「连接设置」里看
        masked = f"{token[:4]}…{token[-4:]}" if len(token) > 10 else token
        self._conn_label.setText(t("selfhost.conn_summary", port=bind_port, token=masked or "-"))

    def set_conn_settings_enabled(self, enabled: bool) -> None:
        self._conn_settings_btn.setEnabled(enabled)

    def refresh(self) -> None:
        self._host_label.setText(self.panel._authenticated_host or t("selfhost.host_pending_auth"))
        authed = self.panel._is_authenticated()
        self._deploy_btn.setEnabled(authed)
        self._deploy_btn.setText(
            t("selfhost.ssh_redeploy_btn") if self.panel._is_service_active() else t("selfhost.ssh_deploy_btn"))
        self._deploy_btn.setToolTip("" if authed else t("selfhost.deploy_needs_auth_hint"))
        self._conn_settings_btn.setEnabled(not self.panel._any_mapped)
        self._conn_settings_btn.setToolTip(t("selfhost.regen_token_disabled_hint") if self.panel._any_mapped else "")
        status = self.panel._last_status
        actual = status.frps_bind_port if status and status.reachable else None
        local = str(self.panel._pending_bind_port)
        self._conn_warning.setText(
            t("selfhost.conn_port_mismatch", port=actual) if actual and str(actual) != local else "")


class _ConnectionSettingsDialog(dialogs.Dialog):
    """改本机记录的 frps 端口和 Token，不动服务器：共用别人部署的节点时照服务器填，鉴权过可一键从服务器读取。"""

    def __init__(self, parent, bind_port: str, token: str, ssh_conn: dict | None):
        super().__init__(parent, t("selfhost.conn_settings_title"), "md")
        self.result: tuple[int, str] | None = None
        self._ssh_conn = ssh_conn
        self.body.addWidget(self.text_label(t("selfhost.conn_settings_hint"), muted=True))
        self.body.addSpacing(4)

        # 字段标题在上、输入框在下，标题紧贴自己的输入框；端口很短，输入框收窄
        self._port_edit = QLineEdit(bind_port)
        self._port_edit.setMaxLength(5)
        self._port_edit.setFixedWidth(120)
        self._token_edit = QLineEdit(token)
        self._token_edit.setFont(QFont("Consolas", 10))
        for title, edit in ((t("selfhost.bind_port_label"), self._port_edit), (t("selfhost.token_label"), self._token_edit)):
            field = QVBoxLayout()
            field.setSpacing(4)
            field.addWidget(self._field_title(title))
            field.addWidget(edit)
            self.body.addLayout(field)

        tools = QHBoxLayout()
        tools.setSpacing(8)
        self._read_btn = QPushButton(t("selfhost.read_from_server_btn"))
        self._read_btn.setAutoDefault(False)
        self._read_btn.setEnabled(ssh_conn is not None)
        self._read_btn.setToolTip(t("selfhost.read_from_server_hint") if ssh_conn else t("selfhost.deploy_needs_auth_hint"))
        self._read_btn.clicked.connect(self._read_from_server)
        tools.addWidget(self._read_btn)
        # 只填进输入框，点保存才生效；新 Token 还要重新部署写到服务器，见按钮提示
        regen_btn = QPushButton(t("selfhost.regen_token_btn"))
        regen_btn.setToolTip(t("selfhost.regen_token_hint"))
        regen_btn.setAutoDefault(False)
        regen_btn.clicked.connect(lambda: self._token_edit.setText(deploy.generate_token()))
        tools.addWidget(regen_btn)
        tools.addStretch()
        self.body.addSpacing(4)
        self.body.addLayout(tools)

        self._status = AutoHideLabel()
        self._status.setWordWrap(True)
        self._status.setFont(theme.font("FONT_SIZE_SM"))
        self.body.addWidget(self._status)
        self.add_buttons()

    @staticmethod
    def _field_title(text: str) -> QLabel:
        label = QLabel(text.rstrip(":："))
        label.setFont(theme.font("FONT_SIZE_SM", bold=True))
        return label

    def _set_status(self, text: str, key: str) -> None:
        self._status.setStyleSheet(f"color: {theme.hex(key)};")
        self._status.setText(text)

    def _read_from_server(self) -> None:
        conn = self._ssh_conn
        self._read_btn.setEnabled(False)
        self._set_status(t("selfhost.read_from_server_running"), "TEXT_MUTED")

        def done(result) -> None:
            self._read_btn.setEnabled(True)
            port, token = result
            if port is None or token is None:
                self._set_status(t("selfhost.read_from_server_missing"), "ERROR")
                return
            self._port_edit.setText(str(port))
            self._token_edit.setText(token)
            self._set_status(t("selfhost.read_from_server_done", port=port), "SUCCESS")

        def error(exc: Exception) -> None:
            self._read_btn.setEnabled(True)
            self._set_status(t("selfhost.read_from_server_failed", detail=str(exc)), "ERROR")

        run_async(lambda: probe.read_frps_connection(conn["host"], conn["port"], conn["username"]), done, error)

    def accept_if_valid(self) -> None:
        port_text, token = self._port_edit.text().strip(), self._token_edit.text().strip()
        if not (port_text.isascii() and port_text.isdigit() and 1 <= int(port_text) <= 65535):
            error = t("selfhost.invalid_port")
        elif not deploy.is_valid_token(token):
            error = t("selfhost.invalid_token")
        else:
            self.result = (int(port_text), token)
            self.accept()
            return
        self._set_status(error, "ERROR")


class _LobbySettingsDialog(QDialog):
    """大厅加速的 Mihomo/WireGuard 配置——同样非模态，跟随 panel 的部署进度实时刷新。"""

    def __init__(self, panel: "SelfHostPanel"):
        super().__init__(panel.window())
        self.panel = panel
        self.setWindowTitle(t("selfhost.lobby_settings_title"))
        self.setMinimumWidth(dialogs.DIALOG_WIDTHS["md"])
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*dialogs.DIALOG_MARGINS)
        layout.setSpacing(dialogs.DIALOG_SPACING)
        hint = QLabel(t("selfhost.lobby_settings_hint"))
        hint.setWordWrap(True)
        hint.setFont(theme.font("FONT_SIZE_SM"))
        layout.addWidget(hint)

        layout.addWidget(QLabel(t("selfhost.lobby_mihomo_label")))
        self._mihomo_path_label = QLabel()
        self._mihomo_path_label.setWordWrap(True)
        layout.addWidget(self._mihomo_path_label)
        mihomo_row = QHBoxLayout()
        download_btn = QPushButton(t("selfhost.lobby_accel_download_mihomo"))
        download_btn.clicked.connect(panel._open_mihomo_download)
        mihomo_row.addWidget(download_btn)
        select_btn = QPushButton(t("selfhost.lobby_accel_select_mihomo"))
        select_btn.clicked.connect(self._on_select_mihomo)
        mihomo_row.addWidget(select_btn)
        mihomo_row.addStretch()
        layout.addLayout(mihomo_row)

        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(separator)

        layout.addWidget(QLabel(t("selfhost.lobby_wireguard_label")))
        self._wireguard_value_label = QLabel()
        layout.addWidget(self._wireguard_value_label)
        wg_row = QHBoxLayout()
        wg_row.addWidget(QLabel(t("selfhost.lobby_wireguard_port_label")))
        saved = app_settings.get_lobby_accel_wireguard() or {}
        self._wg_port_edit = QLineEdit(str(saved.get("port", DEFAULT_WIREGUARD_PORT)))
        self._wg_port_edit.setFixedWidth(80)
        wg_row.addWidget(self._wg_port_edit)
        self._wg_deploy_btn = QPushButton(t("selfhost.lobby_accel_deploy_wireguard"))
        self._wg_deploy_btn.clicked.connect(lambda: panel._deploy_wireguard(self._wg_port_edit.text()))
        wg_row.addWidget(self._wg_deploy_btn)
        wg_row.addStretch()
        layout.addLayout(wg_row)

        close_row = QHBoxLayout()
        close_row.addStretch()
        close_btn = dialogs.style_button(QPushButton(t("dlg.close_btn")), "secondary")
        close_btn.clicked.connect(self.close)
        close_row.addWidget(close_btn)
        layout.addLayout(close_row)
        self.refresh()

    def _on_select_mihomo(self) -> None:
        self.panel._select_mihomo()
        self.refresh()

    def refresh(self) -> None:
        mihomo_path = app_settings.get_lobby_accel_mihomo_path()
        self._mihomo_path_label.setText(str(mihomo_path) if mihomo_path else t("selfhost.lobby_mihomo_not_selected"))
        wireguard = app_settings.get_lobby_accel_wireguard()
        self._wireguard_value_label.setText(
            t("selfhost.lobby_wireguard_deployed") if wireguard else t("selfhost.lobby_wireguard_not_deployed"))
        self._wireguard_value_label.setStyleSheet(f"color: {theme.hex('SUCCESS')};" if wireguard else "")
        self._wg_deploy_btn.setEnabled(not self.panel._wireguard_deploying)
        self._wg_deploy_btn.setText(
            t("selfhost.lobby_accel_redeploy_wireguard") if wireguard else t("selfhost.lobby_accel_deploy_wireguard"))


class SelfHostPanel(QWidget):
    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        self.frpc = FrpcManager()
        self.lobby_accel = LobbyAccelCoordinator()
        self._lobby_accel_busy = False
        self._wireguard_deploying = False
        self._diagnostic_busy = False
        self._current_cluster = None
        self._any_mapped = False
        self._last_status: probe.ServerStatus | None = None
        self._probing = False
        self._probe_cycle_started = False
        self._server_ip_visible = False
        self._authenticated_host = ""
        self._node_dialog: _NodeSettingsDialog | None = None
        self._lobby_dialog: _LobbySettingsDialog | None = None
        # 孤儿 frpc 认领要扫进程表（tasklist + PowerShell，实测约 0.9 秒），放到后台做；
        # 每个存档每次运行只扫一次，之后靠自己启动/认领的跟踪记录
        self._scanned: set[str] = set()
        self._scanning = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 10, 0, 0)
        root.setSpacing(12)

        # ── 服务器状态分区：标题和"立即检测/节点管理"按钮同一行，下面三行状态信息
        status_card, status_layout = section_card()
        status_layout.setSpacing(6)
        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        status_heading = QLabel(t("selfhost.server_status_title"))
        status_heading.setProperty("heading", True)
        status_heading.setFont(theme.font("FONT_SIZE_BASE", bold=True))
        status_row.addWidget(status_heading)
        status_row.addStretch()
        self._probe_btn = QPushButton(t("selfhost.probe_now_btn"))
        self._probe_btn.clicked.connect(self._run_probe_manual)
        status_row.addWidget(self._probe_btn)
        self._manage_btn = QPushButton(t("selfhost.node_manage_btn"))
        self._manage_btn.clicked.connect(self._open_node_settings)
        status_row.addWidget(self._manage_btn)
        status_layout.addLayout(status_row)

        line1 = QHBoxLayout()
        line1.setSpacing(12)
        self._status_label = QLabel(t("selfhost.status_unknown"))
        line1.addWidget(self._status_label)
        self._permission_label = _muted(t("selfhost.permission_display", permission="--"))
        line1.addWidget(self._permission_label)
        line1.addStretch()
        status_layout.addLayout(line1)

        line2 = QHBoxLayout()
        line2.addWidget(_muted(t("selfhost.server_ip_label")))
        self._ip_value_label = _muted(t("selfhost.host_pending_auth"))
        line2.addWidget(self._ip_value_label)
        self._ip_eye_btn = QPushButton("👁")
        self._ip_eye_btn.setFixedWidth(32)
        self._ip_eye_btn.setProperty("flat", True)
        self._ip_eye_btn.clicked.connect(self._toggle_ip_visibility)
        line2.addWidget(self._ip_eye_btn)
        line2.addStretch()
        status_layout.addLayout(line2)

        line3 = QHBoxLayout()
        line3.setSpacing(16)
        self._resource_label = _muted(t("selfhost.resource_unknown"))
        line3.addWidget(self._resource_label)
        self._checked_label = _muted(t("selfhost.never_checked"))
        line3.addWidget(self._checked_label)
        line3.addStretch()
        status_layout.addLayout(line3)
        root.addWidget(status_card)

        self._feature_tabs = _make_pill_bar([t("selfhost.feature_tab_frp"), t("selfhost.feature_tab_lobby")])
        root.addWidget(self._feature_tabs)
        self._feature_stack = QStackedWidget()
        root.addWidget(self._feature_stack, 1)
        self._feature_tabs.current_changed.connect(self._feature_stack.setCurrentIndex)

        self._frp_page = QWidget()
        self._build_frp_page(self._frp_page)
        self._lobby_page = QWidget()
        self._build_lobby_page(self._lobby_page)
        self._feature_stack.addWidget(self._frp_page)
        self._feature_stack.addWidget(self._lobby_page)

        self._load_server_display()
        self._refresh_action_buttons()
        self._refresh_server_status_card()
        self._maybe_start_probe_cycle()

    # ── FRP 映射子页 ────────────────────────────────────────────────────
    def _build_frp_page(self, page: QWidget) -> None:
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 8, 0, 0)
        card, layout = section_card(t("sakura.section_shards"))
        self._status_error_label = AutoHideLabel()
        self._status_error_label.setStyleSheet(f"color: {theme.hex('ERROR')};")
        self._status_error_label.setWordWrap(True)
        layout.addWidget(self._status_error_label)
        self._shards_grid = QGridLayout()
        self._shards_grid.setHorizontalSpacing(18)
        self._shards_grid.setVerticalSpacing(8)
        # 拉伸因子全部给数据列后面的空列，数据列紧凑排列——跟 qt/pages/sakura.py
        # 的樱花映射面板同一个坑同一个修法，见那边的详细说明。
        self._shards_grid.setColumnStretch(4, 1)
        layout.addLayout(self._shards_grid)
        action_row = QHBoxLayout()
        action_row.setSpacing(12)
        self._action_btn = QPushButton(t("selfhost.enable_btn"))
        self._action_btn.clicked.connect(self._on_action_btn)
        action_row.addWidget(self._action_btn)
        self._conn_check_btn = QPushButton(t("selfhost.conn_check_btn"))
        self._conn_check_btn.clicked.connect(self._check_connectivity)
        action_row.addWidget(self._conn_check_btn)
        # frpc 状态和启停按钮放在操作行的下一行，跟樱花映射面板一致。
        self._frpc_row = QWidget()
        frpc_outer = QVBoxLayout(self._frpc_row)
        frpc_outer.setContentsMargins(0, 0, 0, 0)
        frpc_layout = QHBoxLayout()
        frpc_layout.setSpacing(8)
        frpc_outer.addLayout(frpc_layout)
        # frpc 输出里认出的故障与处理建议，直接显示（只放悬停提示时用户看不到）
        self._frpc_hint_label = AutoHideLabel()
        self._frpc_hint_label.setStyleSheet(f"color: {theme.hex('ERROR')};")
        self._frpc_hint_label.setWordWrap(True)
        frpc_outer.addWidget(self._frpc_hint_label)
        self._frpc_status_label = QLabel(self._frpc_status_text(False))
        frpc_layout.addWidget(self._frpc_status_label)
        self._frpc_toggle_btn = QPushButton(t("sakura.frpc_start_btn"))
        self._frpc_toggle_btn.clicked.connect(self._on_frpc_toggle)
        frpc_layout.addWidget(self._frpc_toggle_btn)
        frpc_layout.addStretch()
        action_row.addStretch()
        layout.addSpacing(4)
        layout.addLayout(action_row)
        layout.addWidget(self._frpc_row)
        self._frpc_row.setVisible(False)
        self._frpc_shown_running = False
        self._frpc_tails: dict[int, deque] = {}  # id(frpc 进程) → 最近输出
        self._frpc_shown_blocked = False  # 状态行正显示"被安全软件拦截"，按钮此时是"处理拦截"
        self._frpc_shown_download_failed = False  # 状态行正显示"客户端下载失败"，按钮此时是"下载客户端"
        frpc_tool.add_listener(self._refresh_frpc_row)
        # frpc 会被本地服务器页的停服流程停掉、也可能自己退出，状态行要定时跟上实际状态
        self._frpc_timer = QTimer(self, interval=1000)
        self._frpc_timer.timeout.connect(self._tick_frpc_row)
        self._frpc_timer.start()
        page_layout.addWidget(card)
        page_layout.addStretch()

    # ── 大厅加速子页 ────────────────────────────────────────────────────
    def _build_lobby_page(self, page: QWidget) -> None:
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 8, 0, 0)
        card, layout = section_card()
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(QLabel(t("selfhost.lobby_enable_label")))
        self._lobby_switch = ToggleSwitch(app_settings.get_lobby_accel_enabled())
        self._lobby_switch.toggled.connect(self._on_lobby_accel_toggle)
        row.addWidget(self._lobby_switch)
        self._lobby_status_label = QLabel(t("selfhost.lobby_accel_status_stopped"))
        row.addWidget(self._lobby_status_label)
        row.addStretch()
        self._diag_btn = QPushButton(t("selfhost.lobby_diag_btn"))
        self._diag_btn.clicked.connect(self._start_lobby_diagnostic)
        row.addWidget(self._diag_btn)
        self._lobby_settings_btn = QPushButton(t("selfhost.lobby_settings_btn"))
        self._lobby_settings_btn.clicked.connect(self._open_lobby_settings)
        row.addWidget(self._lobby_settings_btn)
        layout.addLayout(row)

        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet(f"background: {theme.hex('CARD_BORDER')}; border: none;")
        layout.addWidget(divider)

        info_grid = QGridLayout()
        info_grid.setHorizontalSpacing(10)
        info_grid.setVerticalSpacing(6)
        info_grid.addWidget(QLabel(t("selfhost.lobby_mihomo_label")), 0, 0)
        self._mihomo_summary_label = _muted(t("selfhost.lobby_mihomo_not_selected"))
        info_grid.addWidget(self._mihomo_summary_label, 0, 1)
        info_grid.addWidget(QLabel(t("selfhost.lobby_wireguard_label")), 1, 0)
        self._wireguard_summary_label = _muted(t("selfhost.lobby_wireguard_not_deployed"))
        info_grid.addWidget(self._wireguard_summary_label, 1, 1)
        info_grid.setColumnStretch(1, 1)
        layout.addLayout(info_grid)
        route = _muted(t("selfhost.lobby_route_summary"))
        route.setWordWrap(True)
        layout.addWidget(route)
        page_layout.addWidget(card)
        page_layout.addStretch()

    # ── 服务器连接信息 ───────────────────────────────────────────────────
    def _load_server_display(self) -> None:
        server = app_settings.get_selfhost_frp_server()
        self._pending_bind_port = server.get("bind_port", deploy.DEFAULT_BIND_PORT) if server else deploy.DEFAULT_BIND_PORT
        self._pending_token = server.get("token", "") if server else deploy.generate_token()
        self._refresh_authenticated_host_display()

    def _refresh_authenticated_host_display(self) -> None:
        conn = app_settings.get_selfhost_ssh_connection()
        self._authenticated_host = str(conn.get("host", "")).strip() if conn else ""
        self._server_ip_visible = False
        self._refresh_ip_display()

    @staticmethod
    def _mask_host(host: str) -> str:
        parts = host.split(".")
        if len(parts) == 4:
            try:
                if all(0 <= int(part) <= 255 for part in parts):
                    return f"{parts[0]}.{parts[1]}.xx.xx"
            except ValueError:
                pass
        if ":" in host:
            groups = [part for part in host.split(":") if part]
            return ":".join(groups[:2] + ["xxxx", "xxxx"])
        return "••••••" if host else ""

    def _refresh_ip_display(self) -> None:
        host = self._authenticated_host
        if not host:
            self._ip_value_label.setText(t("selfhost.host_pending_auth"))
            self._ip_eye_btn.setEnabled(False)
        else:
            self._ip_value_label.setText(host if self._server_ip_visible else self._mask_host(host))
            self._ip_eye_btn.setEnabled(True)
            self._ip_eye_btn.setText("🙈" if self._server_ip_visible else "👁")

    def _toggle_ip_visibility(self) -> None:
        if not self._authenticated_host:
            return
        self._server_ip_visible = not self._server_ip_visible
        self._refresh_ip_display()

    def _refresh_server_status_card(self) -> None:
        if not self._is_authenticated():
            status_text, color = t("selfhost.node_settings_unauthenticated"), theme.hex("TEXT_MUTED")
        elif self._probing:
            status_text, color = t("selfhost.node_status_checking"), theme.hex("TEXT_MUTED")
        else:
            status_text, color = self._service_status_text(), self._service_status_color()
        self._status_label.setText(status_text)
        self._status_label.setStyleSheet(f"color: {color};")
        self._status_label.setToolTip(self._last_status.error if self._last_status and self._last_status.error else "")
        self._permission_label.setText(t("selfhost.permission_display", permission=self._permission_text()))
        self._resource_label.setText(self._resource_text())
        self._checked_label.setText(self._checked_at_text())
        self._probe_btn.setEnabled(not self._probing and self._is_authenticated())
        if self._node_dialog is not None:
            self._node_dialog.refresh()

    def _validated_port(self, raw: str) -> int | None:
        try:
            port = int(raw)
        except (TypeError, ValueError):
            return None
        return port if 1 <= port <= 65535 else None

    # ── SSH 鉴权/部署 ───────────────────────────────────────────────────
    def _is_authenticated(self) -> bool:
        return remote_deploy.has_local_key() and app_settings.get_selfhost_ssh_connection() is not None

    def _is_service_active(self) -> bool:
        return bool(self._last_status and self._last_status.reachable and self._last_status.service_active)

    def _refresh_action_buttons(self) -> None:
        if self._node_dialog is not None:
            self._node_dialog.refresh()
        self._refresh_server_status_card()

    def _confirm_host_key(self, host: str, fingerprint: str) -> bool:
        """在后台线程里被 remote_deploy 调用，阻塞直到界面线程上的确认框有了结果。"""
        result = [False]
        event = threading.Event()

        def ask(_arg):
            result[0] = dialogs.ask_yes_no(self.window(), t("selfhost.host_key_confirm_title"),
                                            t("selfhost.host_key_confirm_msg", host=host, fingerprint=fingerprint))
            event.set()

        post_to_ui(ask)
        event.wait()
        return result[0]

    def _open_ssh_auth_dialog(self) -> None:
        saved = app_settings.get_selfhost_ssh_connection()
        if saved:
            default_host, default_port, default_user = saved["host"], saved["port"], saved["username"]
        else:
            default_host, default_port, default_user = "", 22, "root"
        dialog = _SSHAuthSetupDialog(self.window(), default_host, default_port, default_user)
        if not dialog.exec() or dialog.result_conn is None:
            return
        conn = dialog.result_conn
        progress = dialogs.LogDialog(self.window(), t("selfhost.ssh_auth_progress_title"))
        progress.show()

        def on_log(line):
            post_to_ui(lambda _a: progress.append(line))

        def work():
            pubkey = remote_deploy.ensure_local_keypair()
            remote_deploy.authorize_key_on_server(conn["host"], conn["port"], conn["username"], conn["password"],
                                                   pubkey, on_log, self._confirm_host_key)
            remote_deploy.verify_key_login(conn["host"], conn["port"], conn["username"], on_log)
            app_settings.set_selfhost_ssh_connection(conn["host"], conn["port"], conn["username"])

        def done(_result) -> None:
            progress.append(t("selfhost.ssh_auth_done"))
            progress.finish()
            self._refresh_authenticated_host_display()
            self._refresh_action_buttons()
            self._maybe_start_probe_cycle()

        def error(exc: Exception) -> None:
            progress.append(t("selfhost.ssh_auth_failed", detail=str(exc)))
            progress.finish()

        run_async(work, done, error)

    def _save_connection(self, bind_port: int, token: str) -> None:
        """「连接设置」里填的端口和 Token（已校验）立即存入设置，不必重新部署（共用别人部署的节点时照服务器填）；
        还没部署过时只记下，部署时一并保存。只改本机，不动服务器。"""
        if self._any_mapped:
            return
        self._pending_bind_port, self._pending_token = bind_port, token
        server = app_settings.get_selfhost_frp_server()
        if server:
            app_settings.set_selfhost_frp_server(server["host"], bind_port, token)
        if self._node_dialog is not None:
            self._node_dialog.set_connection_display(bind_port, token)

    def _start_deploy(self) -> None:
        if not self._is_authenticated():
            dialogs.show_warning(self.window(), t("selfhost.ssh_deploy_btn"), t("selfhost.deploy_needs_auth_hint"))
            return
        conn = app_settings.get_selfhost_ssh_connection()
        host = str(conn.get("host", "")).strip() if conn else ""
        if not host:
            dialogs.show_warning(self.window(), t("selfhost.ssh_deploy_btn"), t("selfhost.host_missing"))
            return
        port = self._validated_port(str(self._pending_bind_port))
        if port is None:
            dialogs.show_warning(self.window(), t("selfhost.ssh_deploy_btn"), t("selfhost.invalid_port"))
            return
        token = (self._pending_token or "").strip() or deploy.generate_token()
        self._pending_token = token
        app_settings.set_selfhost_frp_server(host, port, token)

        redeploying = self._is_service_active()
        confirm_msg = t("selfhost.redeploy_confirm_msg") if redeploying else t("selfhost.deploy_confirm_msg", host=conn["host"])
        if not dialogs.ask_yes_no(self.window(), t("selfhost.ssh_deploy_btn"), confirm_msg):
            return

        cancel_event = threading.Event()
        progress = dialogs.LogDialog(self.window(), t("selfhost.ssh_progress_title"), on_cancel=cancel_event.set)
        progress.show()

        def on_log(line):
            post_to_ui(lambda _a: progress.append(line))

        def work():
            on_log(t("selfhost.checking_port_conflict"))
            status = probe.probe_server_status(conn["host"], conn["port"], conn["username"])
            if status.port_conflicts(port):
                raise remote_deploy.RemoteDeployError(t("selfhost.bind_port_conflict_msg", port=port))
            remote_deploy.deploy_via_ssh(conn["host"], conn["port"], conn["username"], port, token, on_log,
                                          self._confirm_host_key, key_path=str(remote_deploy.SSH_KEY_PATH),
                                          cancel_event=cancel_event)

        def done(_result) -> None:
            progress.append(t("selfhost.ssh_deploy_done"))
            progress.finish()
            self._run_probe(reschedule=False)

        def error(exc: Exception) -> None:
            if isinstance(exc, remote_deploy.RemoteDeployCancelled):
                progress.append(t("selfhost.ssh_deploy_cancelled"))
            else:
                progress.append(t("selfhost.ssh_deploy_failed", detail=str(exc)))
            progress.finish()

        run_async(work, done, error)

    # ── 服务器状态探测 ───────────────────────────────────────────────────
    def _maybe_start_probe_cycle(self) -> None:
        if self._probe_cycle_started or not self._is_authenticated():
            return
        self._probe_cycle_started = True
        self._run_probe(reschedule=True)

    def _run_probe(self, reschedule: bool) -> None:
        if self._probing:
            return
        conn = app_settings.get_selfhost_ssh_connection()
        if not conn:
            return
        self._probing = True
        self._refresh_server_status_card()
        run_async(lambda: probe.probe_server_status(conn["host"], conn["port"], conn["username"]),
                  lambda status: self._on_probe_done(status, reschedule))

    def _run_probe_manual(self) -> None:
        self._run_probe(reschedule=False)

    def _on_probe_done(self, status, reschedule: bool) -> None:
        self._probing = False
        self._last_status = status
        self._refresh_server_status_card()
        self._refresh_action_buttons()
        if reschedule:
            QTimer.singleShot(_PROBE_INTERVAL_MS, lambda: self._periodic_probe_tick())

    def _periodic_probe_tick(self) -> None:
        if not self._is_authenticated():
            return
        self._run_probe(reschedule=True)

    def _service_status_text(self) -> str:
        status = self._last_status
        if status is None:
            return t("selfhost.status_unknown")
        if not status.reachable:
            return t("selfhost.status_unreachable")
        return t("selfhost.status_running") if status.service_active else t("selfhost.status_stopped")

    def _service_status_color(self) -> str:
        status = self._last_status
        if status is None or not status.reachable:
            return theme.hex("TEXT_MUTED")
        return theme.hex("SUCCESS") if status.service_active else theme.hex("ERROR")

    def _permission_text(self) -> str:
        status = self._last_status
        if status is None or not status.reachable or not status.permission:
            # 外层文案已是"SSH 权限：{permission}"，未知时只填"--"
            # （permission_unknown 自带"权限:"前缀，会显示成"SSH 权限：权限: --"）
            return "--"
        return {"root": t("selfhost.permission_root"), "sudo_nopasswd": t("selfhost.permission_sudo"),
                "no_permission": t("selfhost.permission_denied")}[status.permission]

    def _resource_text(self) -> str:
        status = self._last_status
        if status is None or not status.reachable:
            return t("selfhost.resource_unknown")
        cpu = str(status.cpu_count) if status.cpu_count is not None else "--"
        mem = f"{status.mem_used_mb}/{status.mem_total_mb} MB" if status.mem_total_mb is not None else "--"
        return t("selfhost.resource_display", cpu=cpu, mem=mem)

    def _checked_at_text(self) -> str:
        status = self._last_status
        if status is None:
            return t("selfhost.never_checked")
        if not status.reachable:
            return t("selfhost.last_checked", time=t("selfhost.check_failed"))
        return t("selfhost.last_checked", time=time.strftime("%H:%M:%S", time.localtime(status.checked_at)))

    # ── 节点设置弹窗 ─────────────────────────────────────────────────────
    def _open_node_settings(self) -> None:
        if self._node_dialog is not None:
            self._node_dialog.show()
            self._node_dialog.raise_()
            return
        self._node_dialog = _NodeSettingsDialog(self)
        self._node_dialog.finished.connect(self._on_node_dialog_closed)
        self._node_dialog.show()

    def _on_node_dialog_closed(self, _result) -> None:
        self._node_dialog = None

    # ── FRP 映射：世界行渲染 ─────────────────────────────────────────────
    def on_cluster_changed(self, cluster) -> None:
        self._current_cluster = cluster
        self._refresh_server_status_card()  # 状态文字是带参数的动态文案，切换语言后也要重算
        self._render_shard_rows()
        self._maybe_start_probe_cycle()
        if self._is_authenticated():
            self._run_probe(reschedule=False)
        self._refresh_lobby_row()

    @staticmethod
    def _is_master_shard(shard) -> bool:
        return load_shard_config(shard.path).shard.get("is_master", True)

    def _render_shard_rows(self) -> None:
        clear_layout(self._shards_grid)
        self._status_error_label.setText("")
        cluster = self._current_cluster
        if not cluster or cluster.source != SaveSource.SERVER or not cluster.shards:
            key = ("local.select_cluster_first" if not cluster else
                   "sakura.local_save_hint" if cluster.source != SaveSource.SERVER else "sakura.no_shards")
            self._shards_grid.addWidget(QLabel(t(key)), 0, 0)
            self._action_btn.setVisible(False)
            self._conn_check_btn.setVisible(False)
            self._frpc_row.setVisible(False)
            return

        server = app_settings.get_selfhost_frp_server()
        any_mapped = False
        for row, shard in enumerate(cluster.shards):
            remote_port = app_settings.get_selfhost_frp_mapping(cluster.path, shard.name)
            self._shards_grid.addWidget(QLabel(shard.name), row, 0)
            if remote_port is not None:
                any_mapped = True
                mapped_label = QLabel(t("sakura.shard_mapped"))
                mapped_label.setStyleSheet(f"color: {theme.hex('ACCENT')};")
                self._shards_grid.addWidget(mapped_label, row, 1)
                self._shards_grid.addWidget(QLabel(t("sakura.port_display", remote=remote_port)), row, 2)
                is_master = self._is_master_shard(shard)
                copy_btn = QPushButton(t("sakura.copy_connect_btn"))
                copy_btn.setEnabled(is_master)
                copy_btn.clicked.connect(lambda _c=False, s=shard, p=remote_port: self._copy_connect_string(s, p))
                if not is_master:
                    copy_btn.setToolTip(t("sakura.copy_connect_master_only_hint"))
                self._shards_grid.addWidget(copy_btn, row, 3)
            else:
                unmapped = QLabel(t("sakura.shard_unmapped"))
                unmapped.setProperty("muted", True)
                self._shards_grid.addWidget(unmapped, row, 1)

        self._action_btn.setVisible(True)
        self._action_btn.setText(t("selfhost.disable_btn") if any_mapped else t("selfhost.enable_btn"))
        self._any_mapped = any_mapped
        self._action_btn.setEnabled(any_mapped or bool(server))
        self._action_btn.setToolTip("" if (any_mapped or server) else t("selfhost.server_not_configured"))
        self._conn_check_btn.setVisible(True)
        self._conn_check_btn.setEnabled(any_mapped)
        self._conn_check_btn.setToolTip("" if any_mapped else t("selfhost.conn_check_needs_mapping_hint"))
        if self._node_dialog is not None:
            self._node_dialog.set_conn_settings_enabled(not any_mapped)
        if any_mapped:
            self._frpc_row.setVisible(True)
            self._refresh_frpc_row()
            self._reconcile_async(cluster)
        else:
            self._frpc_row.setVisible(False)

    def _copy_connect_string(self, shard, remote_port) -> None:
        server = app_settings.get_selfhost_frp_server()
        host = server.get("host", "") if server else ""
        cluster = self._current_cluster
        password = get_cluster_option(load_cluster_config(cluster.path), "NETWORK", "cluster_password") if cluster else None
        text = f'c_connect("{host}", {remote_port}, "{password}")' if password else f'c_connect("{host}", {remote_port})'
        QGuiApplication.clipboard().setText(text)
        dialogs.show_toast(self.window(), t("sakura.connect_copied"))

    # ── 一键检测连通性 ───────────────────────────────────────────────────
    def _check_connectivity(self) -> None:
        cluster = self._current_cluster
        if not cluster or not self._any_mapped:
            return
        server = app_settings.get_selfhost_frp_server()
        if not server:
            return
        mappings = [(s.name, app_settings.get_selfhost_frp_mapping(cluster.path, s.name)) for s in cluster.shards]
        mappings = [(name, port) for name, port in mappings if port is not None]
        if not mappings:
            return
        host, bind_port = server["host"], server["bind_port"]
        ssh_conn = app_settings.get_selfhost_ssh_connection()
        running = set(self._running_shard_names(cluster))
        progress = dialogs.LogDialog(self.window(), t("selfhost.conn_check_title"))
        progress.show()

        def work():
            ok, detail = connectivity.check_tcp_port(host, bind_port)
            line = t("selfhost.conn_tcp_ok", port=bind_port) if ok else t("selfhost.conn_tcp_fail", port=bind_port, detail=detail or "")
            post_to_ui(lambda _a: progress.append(line))

            tcpdump_probe = connectivity.TcpdumpProbe(ssh_conn["host"], ssh_conn["port"], ssh_conn["username"]) if ssh_conn else None
            if tcpdump_probe and tcpdump_probe.available:
                post_to_ui(lambda _a: progress.append(t("selfhost.conn_udp_method_tcpdump")))
            else:
                reason = (tcpdump_probe.unavailable_reason if tcpdump_probe else None) or "not_authenticated"
                detail = tcpdump_probe.unavailable_detail if tcpdump_probe else None
                post_to_ui(lambda _a, k=f"selfhost.conn_udp_method_fallback_{reason}", d=detail: progress.append(t(k, detail=d or "")))

            for shard_name, remote_port in mappings:
                if tcpdump_probe and tcpdump_probe.available:
                    status, detail = tcpdump_probe.capture_udp(host, remote_port)
                else:
                    status, detail = connectivity.check_udp_port(host, remote_port)
                post_to_ui(lambda _a, ln=t(_UDP_CHECK_KEY_BY_STATUS[status], shard=shard_name, port=remote_port,
                                            detail=detail or ""): progress.append(ln))
            if tcpdump_probe:
                tcpdump_probe.close()
            result = self._check_host_firewall(cluster, progress)
            # 端到端：前面只能分段看云安全组/系统防火墙，frps → frpc → 饥荒这段（Token、端口冲突、frpc 没跑）只有它能发现
            for shard_name, remote_port in mappings:
                if connectivity.raknet_ping(host, remote_port):
                    key = "selfhost.conn_e2e_ok"
                elif shard_name in running:
                    key = "selfhost.conn_e2e_fail"
                else:
                    key = "selfhost.conn_e2e_not_running"
                post_to_ui(lambda _a, ln=t(key, shard=shard_name, port=remote_port): progress.append(ln))
            return result

        def done(result) -> None:
            progress.finish()
            self._offer_open_firewall(result)

        run_async(work, done, lambda exc: (progress.append(str(exc)), progress.finish()))

    # ── 服务器系统防火墙 ─────────────────────────────────────────────────
    def _firewall_ports(self, cluster) -> list[tuple[int, str]]:
        server = app_settings.get_selfhost_frp_server()
        ports = [(server["bind_port"], "tcp")] if server else []
        for shard in cluster.shards:
            if (port := app_settings.get_selfhost_frp_mapping(cluster.path, shard.name)) is not None:
                ports.append((port, "udp"))
        return ports

    def _check_host_firewall(self, cluster, progress=None) -> host_firewall.FirewallCheck | None:
        """后台线程调用：抓包只能证明包到了网卡，还要看 ufw/firewalld 是否放行。没鉴权或查不了返回 None。"""
        conn = app_settings.get_selfhost_ssh_connection()
        ports = self._firewall_ports(cluster)
        if not conn or not ports:
            return None
        try:
            with host_firewall.ssh_session(conn["host"], conn["port"], conn["username"]) as (client, sudo):
                result = host_firewall.check(client, sudo, ports)
        except host_firewall.FirewallError as exc:
            if progress is not None:
                post_to_ui(lambda _a, d=str(exc): progress.append(t("selfhost.firewall_check_failed", detail=d)))
            return None
        if progress is not None and result.tool:
            line = (t("selfhost.firewall_blocked", tool=result.tool, ports=_format_ports(result.blocked))
                    if result.blocked else t("selfhost.firewall_ok", tool=result.tool))
            post_to_ui(lambda _a, ln=line: progress.append(ln))
        return result

    def _check_host_firewall_async(self, cluster) -> None:
        run_async(lambda: self._check_host_firewall(cluster), self._offer_open_firewall, lambda _exc: None)

    def _offer_open_firewall(self, result: host_firewall.FirewallCheck | None) -> None:
        if not result or not result.blocked:
            return
        title = t("selfhost.firewall_open_title")
        if not dialogs.ask_yes_no(self.window(), title, t(
                "selfhost.firewall_open_confirm", tool=result.tool, ports=_format_ports(result.blocked))):
            return
        conn = app_settings.get_selfhost_ssh_connection()
        if not conn:
            return

        def work():
            with host_firewall.ssh_session(conn["host"], conn["port"], conn["username"]) as (client, sudo):
                host_firewall.open_ports(client, sudo, result)

        run_async(work,
                  lambda _r: dialogs.show_info(self.window(), title, t(
                      "selfhost.firewall_open_done", ports=_format_ports(result.blocked))),
                  lambda exc: dialogs.show_error(self.window(), title, t(
                      "selfhost.firewall_open_failed", detail=str(exc))))

    # ── frpc 本地进程 ───────────────────────────────────────────────────
    def _frpc_config_path(self, cluster_path):
        root = data_dir(_FRPC_DATA_NAME, legacy_cache_name=_FRPC_DATA_NAME)
        current = root / f"{cluster_path.name}__{stable_path_key(cluster_path)}.toml"
        if current.exists():
            return current
        legacy = root / f"{cluster_path.name}.toml"
        if not legacy.exists():
            return current
        matches = [c for c in self.ctx.env.clusters if c.path.name == cluster_path.name]
        mapped_matches = [c for c in matches if any(
            app_settings.get_selfhost_frp_mapping(c.path, s.name) is not None for s in c.shards)]
        owner = mapped_matches[0] if len(mapped_matches) == 1 else (matches[0] if len(matches) == 1 else None)
        if owner is not None and str(owner.path) == str(cluster_path):
            try:
                legacy.replace(current)
            except OSError:
                return legacy
        return current

    def has_active_mapping(self, cluster, shard) -> bool:
        return app_settings.get_selfhost_frp_mapping(cluster.path, shard.name) is not None

    def maybe_start_frpc(self, cluster, shard) -> None:
        if not self.has_active_mapping(cluster, shard):
            return
        config_path = self._frpc_config_path(cluster.path)
        if not config_path.exists():
            self._rebuild_frpc_config(cluster)
        exe = _frpc_exe_path()
        if not config_path.exists():
            return
        if not frpc_tool.ensure(exe, lambda: self.maybe_start_frpc(cluster, shard)):
            return  # 发布版不打包 frpc：后台下载完成后再拉起
        existing = self._reconcile_once(cluster)
        if existing is not None and existing.status not in (FrpcStatus.CRASHED, FrpcStatus.STOPPED):
            return  # 已在运行/启停中；崩溃或已停止的才重新拉起
        self.frpc.start(cluster.path, exe, config_path, log=FrpcOutputLog(cluster.path, "自建"))

    def stop_frpc_for_shard(self, cluster, shard, on_done=None) -> None:
        if on_done:
            on_done()

    def _tick_frpc_row(self) -> None:
        """把自己启动后已退出的 frpc 标记为失败，并在显示状态与实际不一致时刷新状态行。
        只看已跟踪的进程，不做 reconcile 的进程表扫描（每秒跑一次太重）。"""
        exited = False
        procs = self.frpc.processes()
        # 持续收走输出、只留最后几十行，供 frpc_diagnosis 找原因；已不跟踪的进程一并清掉
        self._frpc_tails = {id(p): self._frpc_tails.get(id(p)) or deque(maxlen=60) for p in procs}
        for proc in procs:
            if lines := proc.read_available_lines():
                self._frpc_tails[id(proc)].extend(lines)
                exited = True  # 有新输出就重新诊断
            if proc.proc is not None and proc.status == FrpcStatus.RUNNING \
                    and (code := proc.poll_exit_code()) is not None:
                proc.status = FrpcStatus.CRASHED
                proc.error = t("sakura.frpc_exited", code=code)
                exited = True
        cluster = self._current_cluster
        if not cluster or not self._frpc_row.isVisible():
            return
        tracked = self.frpc.get(cluster.path)
        running = tracked is not None and tracked.status == FrpcStatus.RUNNING
        if exited or running != self._frpc_shown_running:
            self._refresh_frpc_row()

    def _frpc_hint(self, cluster) -> str:
        proc = self.frpc.get(cluster.path)
        if proc is None or proc.status not in (FrpcStatus.RUNNING, FrpcStatus.CRASHED):
            return ""
        problem = frpc_diagnosis.diagnose(self._frpc_tails.get(id(proc)) or ())
        if problem is None:
            return ""
        # 代理名是"存档-哈希-世界名"，提示里只说世界名
        return t(f"frpc_hint.{problem.kind}", shard=problem.proxy.rsplit("-", 1)[-1])

    def _reconcile_once(self, cluster):
        if str(cluster.path) in self._scanned:
            return self.frpc.get(cluster.path)
        proc = self.frpc.reconcile(cluster.path, _frpc_exe_path(), self._frpc_config_path(cluster.path))
        self._scanned.add(str(cluster.path))
        return proc

    def frpc_running(self, cluster) -> bool:
        self._reconcile_once(cluster)
        return self._tracked_running(cluster)

    def _tracked_running(self, cluster) -> bool:
        """只看已跟踪的进程，不扫描进程表。"""
        proc = self.frpc.get(cluster.path)
        return proc is not None and proc.status == FrpcStatus.RUNNING

    def _reconcile_async(self, cluster) -> None:
        """后台认领上次遗留的 frpc 进程，完成后刷新状态行；扫描期间状态显示检测中。"""
        if str(cluster.path) in self._scanned or self._scanning:
            return
        self._scanning = True
        self._refresh_frpc_row()
        config_path = self._frpc_config_path(cluster.path)

        def work():
            self.frpc.reconcile(cluster.path, _frpc_exe_path(), config_path)

        def finish(_result=None) -> None:
            self._scanning = False
            self._scanned.add(str(cluster.path))
            if self._current_cluster is cluster:
                self._refresh_frpc_row()

        run_async(work, finish, finish)

    def _frpc_failed_error(self, cluster) -> str | None:
        proc = self.frpc.get(cluster.path)
        if proc is not None and proc.status == FrpcStatus.CRASHED and proc.error:
            return proc.error
        return None

    def _frpc_blocked(self, cluster) -> bool:
        proc = self.frpc.get(cluster.path)
        return proc is not None and proc.status == FrpcStatus.CRASHED and proc.blocked

    @staticmethod
    def _frpc_status_text(running: bool) -> str:
        return t("selfhost.frpc_status_running" if running else "selfhost.frpc_status_stopped")

    def _refresh_frpc_row(self) -> None:
        cluster = self._current_cluster
        running = bool(cluster) and self._tracked_running(cluster)
        error = self._frpc_failed_error(cluster) if cluster and not self._scanning else None
        self._frpc_shown_blocked = bool(error) and self._frpc_blocked(cluster)
        downloading = frpc_tool.downloading()
        download_error = frpc_tool.failed_error() if not running else None
        self._frpc_shown_download_failed = bool(download_error) and not downloading
        self._frpc_toggle_btn.setEnabled(not self._scanning and not downloading)
        if downloading:
            text, color = t("selfhost.frpc_status_downloading"), theme.hex("TEXT_MUTED")
        elif self._frpc_shown_download_failed:
            text, color = t("selfhost.frpc_status_download_failed"), theme.hex("ERROR")
            error = download_error
        elif self._scanning:
            text, color = t("selfhost.frpc_status_checking"), theme.hex("TEXT_MUTED")
        elif self._frpc_shown_blocked:
            text, color = t("selfhost.frpc_status_blocked"), theme.hex("ERROR")
        elif error:
            text, color = t("selfhost.frpc_status_failed"), theme.hex("ERROR")
        elif running:
            text, color = t("selfhost.frpc_status_running"), theme.hex("SUCCESS")
        else:
            text, color = t("selfhost.frpc_status_stopped"), theme.hex("TEXT_MUTED")
        self._frpc_shown_running = running
        self._frpc_status_label.setText(text)
        self._frpc_status_label.setStyleSheet(f"color: {color};")
        self._frpc_status_label.setToolTip(error or "")
        self._frpc_hint_label.setText(self._frpc_hint(cluster) if cluster and not self._scanning else "")
        self._frpc_toggle_btn.setText(t("sakura.frpc_download_btn") if self._frpc_shown_download_failed
                                      else t("selfhost.frpc_blocked_btn") if self._frpc_shown_blocked
                                      else t("sakura.frpc_stop_btn") if running else t("sakura.frpc_start_btn"))

    def _on_frpc_toggle(self) -> None:
        cluster = self._current_cluster
        if not cluster or self._scanning:
            return
        if self._frpc_shown_download_failed:
            frpc_tool.download_with_dialog(self.window(), lambda: [
                self.maybe_start_frpc(cluster, shard) for shard in cluster.shards])
            return
        if self._frpc_shown_blocked:
            self.window().show_defender_dialog()  # 关窗后接着按"启动"处理，排除项生效时就能起来
        running = self._tracked_running(cluster)
        if running != self._frpc_shown_running:
            # 显示的状态已过期（如 frpc 已随停服结束），先刷新，不能反过来执行相反的操作
            self._refresh_frpc_row()
            return
        if running:
            self.frpc.stop(cluster.path, on_done=lambda _p: post_to_ui(lambda _a: self._refresh_frpc_row()))
        else:
            for shard in cluster.shards:
                self.maybe_start_frpc(cluster, shard)
            self._refresh_frpc_row()

    def _running_shard_names(self, cluster) -> list[str]:
        return [s.name for s in cluster.shards
                if (proc := self.ctx.manager.get(cluster.path, s.name)) and proc.status in RUNNING_LIKE]

    # ── 开启/关闭映射 ────────────────────────────────────────────────────
    def _on_action_btn(self) -> None:
        if self._any_mapped:
            self._disable_mapping()
        else:
            self._enable_mapping()

    def _next_free_port(self, base: int, extra_used: frozenset = frozenset()) -> int:
        used = set(app_settings.get_all_selfhost_frp_ports())
        if self._last_status and self._last_status.reachable:
            used |= self._last_status.used_ports
        used |= extra_used
        port = base
        while port in used:
            port += 1
        return port

    def _rebuild_frpc_config(self, cluster) -> None:
        server = app_settings.get_selfhost_frp_server()
        proxies = []
        for shard in cluster.shards:
            remote_port = app_settings.get_selfhost_frp_mapping(cluster.path, shard.name)
            if remote_port is None:
                continue
            shard_config = load_shard_config(shard.path)
            local_port = shard_config.network.get("server_port", remote_port)
            proxies.append({"name": f"{cluster.path.name}-{stable_path_key(cluster.path, length=8)}-{shard.name}",
                             "type": "udp", "local_port": local_port, "remote_port": remote_port})
        config_path = self._frpc_config_path(cluster.path)
        if not proxies:
            config_path.unlink(missing_ok=True)
            return
        toml_text = build_frpc_toml(server["host"], server["bind_port"], server["token"], proxies)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(toml_text, encoding="utf-8")

    def _restart_frpc(self, cluster, on_done=None) -> None:
        if self.frpc.get(cluster.path):
            def after_stop(_proc):
                self._maybe_start_after_restart(cluster, on_done)
            self.frpc.stop(cluster.path, on_done=lambda p: post_to_ui(lambda _a: after_stop(p)))
        else:
            self._maybe_start_after_restart(cluster, on_done)

    def _maybe_start_after_restart(self, cluster, on_done) -> None:
        config_path = self._frpc_config_path(cluster.path)
        exe = _frpc_exe_path()
        if config_path.exists() and frpc_tool.ensure(exe, lambda: self._maybe_start_after_restart(cluster, None)):
            self.frpc.start(cluster.path, exe, config_path, log=FrpcOutputLog(cluster.path, "自建"))
        if on_done:
            on_done()

    def _enable_mapping(self) -> None:
        cluster = self._current_cluster
        if not cluster:
            return
        server = app_settings.get_selfhost_frp_server()
        if not server:
            dialogs.show_warning(self.window(), t("selfhost.enable_btn"), t("selfhost.server_not_configured"))
            return
        running = self._running_shard_names(cluster)
        if running:
            dialogs.show_warning(self.window(), t("sakura.require_stopped_title"),
                                  t("sakura.require_stopped_msg", shards="、".join(running)))
            return
        if not ensure_no_other_mapping(self.window(), self.ctx, cluster, "selfhost", t("selfhost.enable_btn")):
            return
        if not ensure_lan_free_for_mapping(self.window(), self.ctx, cluster):
            return
        shards = list(cluster.shards)
        port_dialog = dialogs.RemotePortDialog(self.window(), [shard.name for shard in shards], t("selfhost.enable_btn"))
        if not port_dialog.exec():
            return
        requested = {name: port for name, port in port_dialog.result_ports.items() if port is not None}

        progress = dialogs.LogDialog(self.window(), t("selfhost.setup_progress_title"))
        progress.show()
        base_port = server["bind_port"] + 1

        def work():
            fresh_used_ports: frozenset = frozenset()
            conn = app_settings.get_selfhost_ssh_connection()
            if conn:
                post_to_ui(lambda _a: progress.append(t("selfhost.checking_port_conflict")))
                fresh_status = probe.probe_server_status(conn["host"], conn["port"], conn["username"])
                if fresh_status.reachable:
                    fresh_used_ports = fresh_status.used_ports
                    post_to_ui(lambda _a, s=fresh_status: setattr(self, "_last_status", s))
            # 手填端口先全部校验再落盘：frps 拒绝时只在 frpc 日志里报错，界面上看不到
            # （共用节点时别人的映射端口也在服务器的占用列表里）
            taken = set(app_settings.get_all_selfhost_frp_ports()) | fresh_used_ports | {server["bind_port"]}
            for shard in shards:
                port = requested.get(shard.name)
                if port is not None and port in taken \
                        and port != app_settings.get_selfhost_frp_mapping(cluster.path, shard.name):
                    raise ValueError(t("selfhost.remote_port_taken", shard=shard.name, port=port))
            for shard in shards:
                remote_port = requested.get(shard.name) or app_settings.get_selfhost_frp_mapping(cluster.path, shard.name)
                if remote_port is None:
                    remote_port = self._next_free_port(
                        base_port, extra_used=fresh_used_ports | frozenset(requested.values()))
                    app_settings.set_selfhost_frp_mapping(cluster.path, shard.name, remote_port)
                shard_config = load_shard_config(shard.path)
                set_shard_option(shard_config, "NETWORK", "server_port", remote_port)
                save_shard_config(shard_config, shard.path)
                post_to_ui(lambda _a, s=shard, p=remote_port: progress.append(t("selfhost.setup_step_mapping", shard=s.name, port=p)))
            self._rebuild_frpc_config(cluster)

        def done(_result) -> None:
            self._restart_frpc(cluster, on_done=lambda: self._on_enable_done(progress))

        def error(exc: Exception) -> None:
            progress.append(str(exc))
            progress.finish()
            self._render_shard_rows()

        run_async(work, done, error)

    def _on_enable_done(self, progress) -> None:
        progress.finish()
        cluster = self._current_cluster
        running = self._running_shard_names(cluster) if cluster else []
        if running:
            dialogs.show_info(self.window(), t("selfhost.setup_done_title"),
                               t("sakura.setup_done_msg_restart", shards="、".join(running)))
        self._render_shard_rows()
        self.ctx.cluster_config_saved.emit(cluster)
        if cluster:
            self._check_host_firewall_async(cluster)

    def _disable_mapping(self) -> None:
        cluster = self._current_cluster
        if not cluster:
            return
        if not dialogs.ask_yes_no(self.window(), t("selfhost.disable_confirm_title"), t("selfhost.disable_confirm_msg")):
            return
        for shard in cluster.shards:
            app_settings.set_selfhost_frp_mapping(cluster.path, shard.name, None)
        self._frpc_config_path(cluster.path).unlink(missing_ok=True)
        self.frpc.stop(cluster.path)
        self._render_shard_rows()
        self.ctx.cluster_config_saved.emit(cluster)

    def release_cluster(self, cluster):
        """存档被删除（或新存档撞上旧存档残留的记录）时关闭自建节点映射：端口记账立即清掉，返回在后台
        停 frpc、删配置的函数。自建 frps 没有分配接口，远端没有要删的东西。"""
        if not any(self.has_active_mapping(cluster, s) for s in cluster.shards):
            return None
        for shard in cluster.shards:
            app_settings.set_selfhost_frp_mapping(cluster.path, shard.name, None)
        config_path = self._frpc_config_path(cluster.path)

        def work() -> list[str]:
            # 停服后 frpc 仍在运行，可能是上次启动 DSTCamp 时留下的，要按配置路径认领后再停
            if self.frpc.reconcile(cluster.path, _frpc_exe_path(), config_path) is not None:
                self.frpc.stop_blocking(cluster.path)
            config_path.unlink(missing_ok=True)
            return []

        return work

    # ── 大厅加速 ────────────────────────────────────────────────────────
    def ensure_lobby_accel(self, cluster, on_done) -> None:
        if not app_settings.get_lobby_accel_enabled():
            on_done(True, "")
            return
        if self._lobby_accel_busy:
            on_done(False, t("selfhost.lobby_accel_busy"))
            return
        self._lobby_accel_busy = True
        self._refresh_lobby_row()

        def work():
            try:
                self.lobby_accel.start(cluster)
                return True, ""
            except LobbyAccelError as exc:
                return False, str(exc)

        def done(result) -> None:
            ok, detail = result
            self._lobby_accel_busy = False
            self._refresh_lobby_row()
            on_done(ok, detail)

        run_async(work, done, lambda exc: done((False, str(exc))))


    def stop_lobby_accel_async(self) -> None:
        if self._lobby_accel_busy:
            return
        self._lobby_accel_busy = True
        self._refresh_lobby_row()

        def work():
            self.lobby_accel.stop()

        def done(_result) -> None:
            self._lobby_accel_busy = False
            self._refresh_lobby_row()

        run_async(work, done, lambda _exc: done(None))

    def poll_lobby_accel(self) -> None:
        self._refresh_lobby_row()

    def _on_lobby_accel_toggle(self, checked: bool) -> None:
        if self.ctx.manager.running():
            self._lobby_switch.setChecked(app_settings.get_lobby_accel_enabled())
            dialogs.show_warning(self.window(), t("selfhost.lobby_accel_label"), t("selfhost.lobby_accel_disable_running"))
            return
        app_settings.set_lobby_accel_enabled(checked)
        if not checked:
            self.stop_lobby_accel_async()
        self._refresh_lobby_row()

    def _refresh_lobby_row(self) -> None:
        diagnostic_ready = not self._diagnostic_busy and self._current_cluster is not None and self.lobby_accel.is_healthy()
        self._diag_btn.setEnabled(diagnostic_ready)
        if self._lobby_accel_busy:
            text, color = t("selfhost.lobby_accel_status_starting"), theme.hex("TEXT_MUTED")
        elif self.lobby_accel.is_healthy():
            text, color = t("selfhost.lobby_accel_status_running"), theme.hex("SUCCESS")
        elif self.lobby_accel.status == LobbyAccelStatus.CRASHED:
            text, color = t("selfhost.lobby_accel_status_failed"), theme.hex("ERROR")
        elif not app_settings.get_lobby_accel_enabled():
            text, color = t("selfhost.lobby_accel_status_stopped"), theme.hex("TEXT_MUTED")
        elif not app_settings.get_lobby_accel_mihomo_path():
            text, color = t("selfhost.lobby_accel_status_no_mihomo"), theme.hex("TEXT_MUTED")
        elif not app_settings.get_lobby_accel_wireguard():
            text, color = t("selfhost.lobby_accel_status_no_wireguard"), theme.hex("TEXT_MUTED")
        else:
            text, color = t("selfhost.lobby_accel_status_ready"), theme.hex("TEXT_MUTED")
        _set_colored_text(self._lobby_status_label, text, color)

        mihomo_path = app_settings.get_lobby_accel_mihomo_path()
        if mihomo_path:
            _set_colored_text(self._mihomo_summary_label, t("selfhost.lobby_mihomo_selected", name=mihomo_path.name),
                              theme.hex("SUCCESS"))
        else:
            _set_colored_text(self._mihomo_summary_label, t("selfhost.lobby_mihomo_not_selected"),
                              theme.hex("TEXT_MUTED"))
        wireguard = app_settings.get_lobby_accel_wireguard()
        if wireguard:
            _set_colored_text(self._wireguard_summary_label,
                              t("selfhost.lobby_wireguard_summary", port=wireguard["port"]), theme.hex("SUCCESS"))
        else:
            _set_colored_text(self._wireguard_summary_label, t("selfhost.lobby_wireguard_not_deployed"),
                              theme.hex("TEXT_MUTED"))
        if self._lobby_dialog is not None:
            self._lobby_dialog.refresh()

    def _open_lobby_settings(self) -> None:
        if self._lobby_dialog is not None:
            self._lobby_dialog.show()
            self._lobby_dialog.raise_()
            return
        self._lobby_dialog = _LobbySettingsDialog(self)
        self._lobby_dialog.finished.connect(self._on_lobby_dialog_closed)
        self._lobby_dialog.show()

    def _on_lobby_dialog_closed(self, _result) -> None:
        self._lobby_dialog = None

    def _open_mihomo_download(self) -> None:
        choice = dialogs.ask_choice(
            self.window(), t("selfhost.lobby_accel_download_mihomo"), t("selfhost.lobby_accel_download_source_hint"),
            [(t("selfhost.lobby_accel_download_official"), "official"), (t("selfhost.lobby_accel_download_lanzou"), "lanzou")])
        if not choice:
            return
        if choice == "lanzou":
            QGuiApplication.clipboard().setText(MIHOMO_LANZOU_CODE)
            dialogs.show_toast(self.window(), t("selfhost.lobby_accel_download_code_copied"))
            url = MIHOMO_LANZOU_URL
        else:
            url = MIHOMO_RELEASES_URL
        try:
            opened = webbrowser.open(url)
        except OSError:
            opened = False
        if not opened:
            dialogs.show_warning(self.window(), t("selfhost.lobby_accel_download_mihomo"),
                                  t("selfhost.lobby_accel_download_mihomo_failed", url=url))

    def _select_mihomo(self) -> None:
        picked, _filter = QFileDialog.getOpenFileName(
            self.window(), t("selfhost.lobby_accel_select_mihomo"), "", "mihomo.exe (mihomo.exe);;Executable (*.exe)")
        if not picked:
            return
        try:
            digest = sha256_file(picked)
        except (OSError, MihomoError) as exc:
            dialogs.show_error(self.window(), t("selfhost.lobby_accel_label"), str(exc))
            return
        app_settings.set_lobby_accel_mihomo_path(picked, digest)
        self._refresh_lobby_row()

    def _deploy_wireguard(self, port_text: str) -> None:
        if self._wireguard_deploying:
            return
        if self.ctx.manager.running():
            dialogs.show_warning(self.window(), t("selfhost.lobby_accel_label"), t("selfhost.lobby_accel_disable_running"))
            return
        server = app_settings.get_selfhost_frp_server()
        ssh = app_settings.get_selfhost_ssh_connection()
        if not server or not ssh or not self._is_authenticated():
            dialogs.show_warning(self.window(), t("selfhost.lobby_accel_label"), t("selfhost.lobby_accel_needs_auth"))
            return
        port = self._validated_port(port_text.strip())
        if port is None:
            dialogs.show_warning(self.window(), t("selfhost.lobby_accel_label"), t("selfhost.invalid_port"))
            return
        if not dialogs.ask_yes_no(self.window(), t("selfhost.lobby_accel_deploy_wireguard"),
                                   t("selfhost.lobby_accel_deploy_confirm", port=port)):
            return
        try:
            _private_key, client_public_key = ensure_client_keypair()
        except (OSError, ValueError) as exc:
            dialogs.show_error(self.window(), t("selfhost.lobby_accel_label"), str(exc))
            return

        progress = dialogs.LogDialog(self.window(), t("selfhost.lobby_accel_deploy_wireguard"))
        progress.show()
        self._wireguard_deploying = True
        self._refresh_lobby_row()

        def on_log(line):
            post_to_ui(lambda _a: progress.append(line))

        def work():
            public_key = wireguard_deploy.deploy_wireguard_via_ssh(
                ssh["host"], int(ssh["port"]), ssh["username"], port, client_public_key, on_log)
            app_settings.set_lobby_accel_wireguard(port, public_key)

        def done(_result) -> None:
            self._wireguard_deploying = False
            progress.append(t("selfhost.lobby_accel_deploy_done"))
            progress.finish()
            self._refresh_lobby_row()

        def error(exc: Exception) -> None:
            self._wireguard_deploying = False
            progress.append(t("selfhost.lobby_accel_deploy_failed", detail=str(exc)))
            progress.finish()
            self._refresh_lobby_row()

        run_async(work, done, error)

    # ── 大厅加速诊断 ─────────────────────────────────────────────────────
    def _start_lobby_diagnostic(self) -> None:
        if self._diagnostic_busy:
            return
        cluster = self._current_cluster
        if cluster is None or not self._running_shard_names(cluster):
            dialogs.show_warning(self.window(), t("selfhost.lobby_diag_title"), t("selfhost.lobby_diag_needs_server"))
            return
        if not self.lobby_accel.is_healthy():
            dialogs.show_warning(self.window(), t("selfhost.lobby_diag_title"), t("selfhost.lobby_diag_needs_accel"))
            return
        ssh = app_settings.get_selfhost_ssh_connection()
        mapped_ports = {int(port) for shard in cluster.shards
                        if (port := app_settings.get_selfhost_frp_mapping(cluster.path, shard.name)) is not None}
        if not ssh or not mapped_ports:
            dialogs.show_warning(self.window(), t("selfhost.lobby_diag_title"), t("selfhost.lobby_diag_needs_mapping"))
            return
        if not self.lobby_accel.mihomo.controller_port:
            dialogs.show_warning(self.window(), t("selfhost.lobby_diag_title"), t("selfhost.lobby_diag_needs_restart"))
            return
        if not dialogs.ask_yes_no(self.window(), t("selfhost.lobby_diag_title"), t("selfhost.lobby_diag_confirm")):
            return
        session = LobbyDiagnosticSession(cluster, self.lobby_accel.mihomo, ssh, mapped_ports)

        def cancel():
            session.cancel()
            post_to_ui(lambda _a: progress.append(t("selfhost.lobby_diag_canceling")))

        progress = dialogs.LogDialog(self.window(), t("selfhost.lobby_diag_title"), on_cancel=cancel,
                                      cancel_text=t("selfhost.lobby_diag_stop_btn"))
        progress.show()
        progress.append(t("selfhost.lobby_diag_preparing"))
        self._diagnostic_busy = True
        self._refresh_lobby_row()

        key_by_event = {"remote_ready": "selfhost.lobby_diag_remote_ready", "capture_started": "selfhost.lobby_diag_capture_started",
                         "player_authenticated": "selfhost.lobby_diag_player_authenticated",
                         "mihomo_matched": "selfhost.lobby_diag_mihomo_matched"}

        def on_progress(event, detail):
            if event == "remote_error":
                line = t("selfhost.lobby_diag_remote_error", detail=detail)
            else:
                key = key_by_event.get(event)
                if key is None:
                    return
                line = t(key)
            post_to_ui(lambda _a: progress.append(line))

        def work():
            return session.run(on_progress)

        def done(report) -> None:
            self._finish_lobby_diagnostic(progress, report)

        def error(exc: Exception) -> None:
            self._diagnostic_busy = False
            progress.append(t("selfhost.lobby_diag_failed", detail=str(exc)))
            progress.finish()
            self._refresh_lobby_row()

        run_async(work, done, error)

    def _finish_lobby_diagnostic(self, progress, report) -> None:
        self._diagnostic_busy = False
        evidence = report.evidence
        remote = evidence.remote
        route = report.route
        effective = route in {DiagnosticRoute.FRP, DiagnosticRoute.WIREGUARD}
        status_key = "selfhost.lobby_diag_status_effective" if effective else "selfhost.lobby_diag_status_not_confirmed"
        if route == DiagnosticRoute.INCONCLUSIVE:
            status_key = "selfhost.lobby_diag_status_inconclusive"
        route_key = {DiagnosticRoute.FRP: "selfhost.lobby_diag_route_frp", DiagnosticRoute.WIREGUARD: "selfhost.lobby_diag_route_wireguard",
                     DiagnosticRoute.SIGNAL_ONLY: "selfhost.lobby_diag_route_signal_only", DiagnosticRoute.BYPASS: "selfhost.lobby_diag_route_bypass",
                     DiagnosticRoute.DIRECT: "selfhost.lobby_diag_route_direct",
                     DiagnosticRoute.INCONCLUSIVE: "selfhost.lobby_diag_route_inconclusive"}[route]
        judgment_key = {DiagnosticRoute.FRP: "selfhost.lobby_diag_judgment_frp", DiagnosticRoute.WIREGUARD: "selfhost.lobby_diag_judgment_wireguard",
                        DiagnosticRoute.SIGNAL_ONLY: "selfhost.lobby_diag_judgment_signal_only", DiagnosticRoute.BYPASS: "selfhost.lobby_diag_judgment_bypass",
                        DiagnosticRoute.DIRECT: "selfhost.lobby_diag_judgment_direct",
                        DiagnosticRoute.INCONCLUSIVE: "selfhost.lobby_diag_judgment_inconclusive"}[route]
        confidence_key = {"high": "selfhost.lobby_diag_confidence_high", "medium": "selfhost.lobby_diag_confidence_medium",
                          "low": "selfhost.lobby_diag_confidence_low"}.get(report.confidence, "selfhost.lobby_diag_confidence_low")
        connection_key = ("selfhost.lobby_diag_connection_loopback" if evidence.loopback_connection else
                          ("selfhost.lobby_diag_connection_p2p" if evidence.p2p_connection else "selfhost.lobby_diag_connection_unknown"))
        external_ports = ", ".join(str(p) for p in sorted(evidence.external_ports)) or t("selfhost.lobby_diag_not_observed")

        def count(value) -> str:
            return f"{max(0, int(value)):,}"

        def section(key: str) -> None:
            progress.append(t(key), "section")

        def detail(key: str, **values) -> None:
            progress.append(t(key, **values), "detail")

        progress.clear()
        section("selfhost.lobby_diag_result_title")
        progress.append(t(status_key), "result_success" if effective else "result_warning")
        progress.append(t("selfhost.lobby_diag_confidence", value=t(confidence_key)), "note")
        section("selfhost.lobby_diag_route_title")
        progress.append(t(route_key), "detail")
        section("selfhost.lobby_diag_connection_title")
        detail("selfhost.lobby_diag_player_auth", value=t("dlg.yes_btn") if evidence.authenticated else t("dlg.no_btn"))
        if evidence.players:
            # 逐个玩家列出日志里识别到的连接方式（同一玩家进出多次只列最后一次）
            latest = {item.player: item.kind for item in evidence.players}
            for player, kind in latest.items():
                detail("selfhost.lobby_diag_player_line", player=player, kind=t(f"selfhost.lobby_diag_kind_{kind.value}"))
        else:
            detail("selfhost.lobby_diag_connection_type", value=t(connection_key))
        detail("selfhost.lobby_diag_loopback", value=t("dlg.yes_btn") if evidence.loopback_connection else t("dlg.no_btn"))
        detail("selfhost.lobby_diag_external_ports", value=external_ports)
        section("selfhost.lobby_diag_wg_title")
        detail("selfhost.lobby_diag_wg_rx", bytes=count(remote.wg_rx_delta))
        detail("selfhost.lobby_diag_wg_tx", bytes=count(remote.wg_tx_delta))
        detail("selfhost.lobby_diag_wg_stun", bytes=count(remote.wg_stun_bytes))
        detail("selfhost.lobby_diag_wg_non_stun", bytes=count(remote.wg_non_stun_bytes))
        detail("selfhost.lobby_diag_mihomo_stun", bytes=count(evidence.mihomo_wg_stun_bytes))
        detail("selfhost.lobby_diag_mihomo_non_stun", bytes=count(evidence.mihomo_wg_non_stun_bytes))
        section("selfhost.lobby_diag_frp_title")
        detail("selfhost.lobby_diag_frp_packets", packets=count(remote.frp_packets))
        detail("selfhost.lobby_diag_frp_bytes", bytes=count(remote.frp_bytes))
        section("selfhost.lobby_diag_judgment_title")
        progress.append(t(judgment_key), "note")
        identified_wg_traffic = any((remote.wg_stun_bytes, remote.wg_non_stun_bytes,
                                      evidence.mihomo_wg_stun_bytes, evidence.mihomo_wg_non_stun_bytes))
        if (remote.wg_rx_delta or remote.wg_tx_delta) and not identified_wg_traffic:
            progress.append(t("selfhost.lobby_diag_counter_only_note"), "note")
        if any(item.kind == ConnectionKind.STEAM_P2P for item in evidence.players):
            progress.append(t("selfhost.lobby_diag_p2p_note"), "note")
        warnings = []
        if remote.error:
            warnings.append(t("selfhost.lobby_diag_remote_error", detail=remote.error))
        if evidence.mihomo_api_error and not evidence.mihomo_api_available:
            warnings.append(t("selfhost.lobby_diag_mihomo_error", detail=evidence.mihomo_api_error))
        if warnings:
            section("selfhost.lobby_diag_warning_title")
            for warning in warnings:
                progress.append(warning, "result_error")
        progress.scroll_to_start()
        progress.finish()
        self._refresh_lobby_row()
