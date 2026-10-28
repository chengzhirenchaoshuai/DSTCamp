"""内网穿透页：樱花映射（SakuraFrp 开放 API，本文件）、Lolia（qt/tunnel/lolia_panel.py）、自建节点（qt/tunnel/selfhost_panel.py，含大厅加速）。

跨页钩子（AppContext.mapping_owner / ensure_lobby_accel 等）在这里接管。
"""

import time
import webbrowser

from PySide6.QtCore import QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QStackedWidget,
    QVBoxLayout, QWidget,
)

from dstools.features.cluster_config.config_manager import (
    get_cluster_option, get_shard_option, load_cluster_config, load_shard_config, save_shard_config,
    set_shard_option,
)
from dstools.features.local_service.shard_helpers import RUNNING_LIKE
from dstools.features.sakura import api as sakura_frp
from dstools.features.sakura.frpc import FrpcManager, FrpcStatus
from dstools.features.sakura.frpc_recovery import SAKURA_FRPC_ARCHIVE, SakuraFrpcHealth, inspect_sakura_frpc
from dstools.i18n import t
from dstools.models import SaveSource
from dstools.qt import dialogs
from dstools.qt.pages.base import Page
from dstools.qt.tunnel.lolia_panel import LoliaPanel
from dstools.qt.tunnel.node_picker import NodeChoice, NodePickerDialog
from dstools.qt.tunnel.selfhost_panel import SelfHostPanel
from dstools.qt.theme import theme
from dstools.qt.threads import post_to_ui, run_async
from dstools.qt.tool_download import download_tool_with_dialog
from dstools.qt.widgets import AutoHideLabel, Card, PillTabBar, VScrollArea, clear_layout, section_card
from dstools.shared import app_settings, tool_store
from dstools.qt.tunnel.lan_mapping_guard import ensure_lan_free_for_mapping, ensure_no_other_mapping, remote_failure_item
from dstools.shared.server_ports import stable_path_key
from dstools.shared.token_manager import mask_token

_FALLBACK_MAX_TUNNELS = 2
_SUB_TAB_KEYS = ["sakura", "lolia", "selfhost"]  # 子页签顺序，同时是 nat_sub_tab 的取值
_FRPC_DATA_NAME = "frpc_config"


def _format_bytes_adaptive(num_bytes: float) -> str:
    kib = num_bytes / 1024
    if kib < 1024:
        return f"{kib:.2f} KiB"
    mib = kib / 1024
    if mib < 1024:
        return f"{mib:.2f} MiB"
    return f"{mib / 1024:.2f} GiB"


def _frpc_pointer_path(cluster_path, shard_name, app_env):
    """本地隧道 ID 指针文件路径（不是完整 frpc 配置，见 dedicated_server 同类说明）。"""
    from dstools.shared.resource_paths import data_dir
    root = data_dir(_FRPC_DATA_NAME, legacy_cache_name=_FRPC_DATA_NAME)
    current = root / f"{cluster_path.name}__{stable_path_key(cluster_path)}__{shard_name}.txt"
    if current.exists():
        return current
    legacy = root / f"{cluster_path.name}__{shard_name}.txt"
    if not legacy.exists():
        return current
    matches = [c for c in app_env.clusters if c.path.name == cluster_path.name
               and any(s.name == shard_name for s in c.shards)]
    if len(matches) == 1 and str(matches[0].path) == str(cluster_path):
        try:
            legacy.replace(current)
        except OSError:
            return legacy
    return current


class _FrpcRecoveryDialog(dialogs.Dialog):
    """樱花 frpc 缺失/被隔离时的人工恢复引导——只有这条链路会出现（自建节点用的
    是随包分发的 frpc.exe 副本，不经历这个"被杀毒软件删除"的问题）。"""

    def __init__(self, parent, initial: SakuraFrpcHealth):
        super().__init__(parent, t("sakura.frpc_recovery_title"), 640, confirm_text=t("dlg.close_btn"))
        self.restored = False
        self._health = initial
        self._message = self.text_label("")
        self.body.addWidget(self._message)
        self._path_label = self.text_label("", muted=True, size_key="FONT_SIZE_SM")
        self.body.addWidget(self._path_label)
        self._status_label = self.text_label("")
        self.body.addWidget(self._status_label)
        row = QHBoxLayout()
        open_security = QPushButton(t("sakura.frpc_recovery_open_security"))
        open_security.clicked.connect(self._open_security)
        open_folder = QPushButton(t("sakura.frpc_recovery_open_folder"))
        open_folder.clicked.connect(self._open_folder)
        copy_path = QPushButton(t("sakura.frpc_recovery_copy_path"))
        copy_path.clicked.connect(self._copy_path)
        recheck = QPushButton(t("sakura.frpc_recovery_recheck"))
        recheck.clicked.connect(self._recheck)
        for button in (open_security, open_folder, copy_path, recheck):
            row.addWidget(button)
        self.body.addLayout(row)
        self.body.addWidget(self.add_buttons())
        self._render()

    def _render(self) -> None:
        key = {"missing": "sakura.frpc_recovery_missing", "blocked": "sakura.frpc_recovery_blocked",
               "unreadable": "sakura.frpc_recovery_unreadable", "ready": "sakura.frpc_recovery_ready",
               "not_downloaded": "sakura.frpc_recovery_not_downloaded"}[self._health.status]
        self._message.setText(t(key, error=self._health.detail))
        self._path_label.setText(str(self._health.path))
        self._status_label.setText(t("sakura.frpc_recovery_ready" if self._health.ready else "sakura.frpc_recovery_waiting"))
        self._status_label.setStyleSheet(f"color: {theme.hex('ACCENT') if self._health.ready else theme.hex('ERROR')};")

    def _open_security(self) -> None:
        import os
        import subprocess
        import sys
        if sys.platform != "win32":
            return
        try:
            os.startfile("windowsdefender:")
        except OSError:
            try:
                subprocess.Popen(["explorer.exe", "windowsdefender:"])
            except OSError:
                pass

    def _open_folder(self) -> None:
        import os
        import sys
        from dstools.shared.resource_paths import exe_dir
        folder = exe_dir()
        while not folder.exists() and folder != folder.parent:
            folder = folder.parent
        if sys.platform != "win32":
            return
        try:
            os.startfile(str(folder))
        except OSError:
            pass

    def _copy_path(self) -> None:
        QGuiApplication.clipboard().setText(str(self._health.path))

    def _recheck(self) -> None:
        self._health = inspect_sakura_frpc()
        self._render()
        if self._health.ready:
            self.restored = True
            self.accept()


class _SakuraMappingPanel(QWidget):
    """"樱花映射"子页签内容。"""

    _SHARD_ROW_PADY = 6

    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        self.frpc = FrpcManager()
        self._current_cluster = None
        self._tunnels_by_key: dict[tuple[str, str], dict] = {}
        self._nodes: dict[str, dict] = {}
        self._node_loads: dict[int, float] = {}
        self._node_choices: list[tuple[int, dict, bool]] = []
        self._selected_node_id: int | None = None
        self._my_group_level = 0
        self._max_tunnels = _FALLBACK_MAX_TUNNELS
        self._tunnel_count = 0
        self._tunnels_exhausted = False
        self._reload_gen = 0
        self._tunnel_occupants: list[str] = []
        self._any_mapped = False
        self._frpc_health: SakuraFrpcHealth | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 10, 0, 0)
        root.setSpacing(12)

        # ── 分区一：账户与节点（Token/节点两行用网格让标签列对齐，下面是账户数据）
        account_card, account_layout = section_card(t("sakura.section_account"))
        form = QGridLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)
        form.addWidget(QLabel(t("sakura.token_label")), 0, 0)
        row1 = QHBoxLayout()
        row1.setSpacing(8)
        self._token_label = QLabel()
        self._token_label.setFont(theme.font("FONT_SIZE_SM"))
        self._token_label.setStyleSheet(f"font-family: Consolas; color: {theme.hex('TEXT')};")
        row1.addWidget(self._token_label)
        change_btn = QPushButton(t("token.change"))
        change_btn.clicked.connect(self._change_token)
        row1.addWidget(change_btn)
        dashboard_btn = QPushButton(t("sakura.open_dashboard_btn"))
        dashboard_btn.clicked.connect(lambda: webbrowser.open("https://www.natfrp.com/user/"))
        row1.addWidget(dashboard_btn)
        row1.addStretch()
        form.addLayout(row1, 0, 1)

        form.addWidget(QLabel(t("sakura.node_label")), 1, 0)
        row2 = QHBoxLayout()
        row2.setSpacing(8)
        self._node_btn = QPushButton(t("sakura.node_none_selected"))
        self._node_btn.clicked.connect(self._open_node_picker)
        row2.addWidget(self._node_btn)
        refresh_btn = QPushButton(t("sakura.node_refresh_btn"))
        refresh_btn.clicked.connect(self._reload_async)
        row2.addWidget(refresh_btn)
        row2.addStretch()
        form.addLayout(row2, 1, 1)
        form.setColumnStretch(1, 1)
        account_layout.addLayout(form)

        # 账户数据跟上面的操作行之间用一条细分隔线隔开，数据列之间拉开间距。
        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet(f"background: {theme.hex('CARD_BORDER')}; border: none;")
        account_layout.addSpacing(2)
        account_layout.addWidget(divider)
        account_grid = QGridLayout()
        account_grid.setHorizontalSpacing(36)
        account_grid.setVerticalSpacing(2)
        self._account_value_labels = []
        # 列顺序与 Lolia 映射一致：可用流量、限速、隧道（已用/上限）
        for col, key in enumerate(("sakura.account_group", "sakura.account_traffic", "sakura.account_speed",
                                    "sakura.account_tunnels_usage")):
            header = QLabel(t(key))
            header.setProperty("muted", True)
            header.setFont(theme.font("FONT_SIZE_SM"))
            account_grid.addWidget(header, 0, col)
            value = QLabel("--")
            value.setFont(theme.font("FONT_SIZE_MD", bold=True))
            account_grid.addWidget(value, 1, col)
            self._account_value_labels.append(value)
        account_grid.setColumnStretch(4, 1)
        account_layout.addLayout(account_grid)

        self._recent_traffic_label = QLabel("")
        self._recent_traffic_label.setProperty("muted", True)
        self._recent_traffic_label.setFont(theme.font("FONT_SIZE_SM"))
        account_layout.addWidget(self._recent_traffic_label)
        root.addWidget(account_card)

        # ── 分区二：世界映射（分片列表 + 开启/关闭 + frpc 状态）
        shards_card, shards_layout = section_card(t("sakura.section_shards"))
        self._status_label = AutoHideLabel()
        self._status_label.setStyleSheet(f"color: {theme.hex('ERROR')};")
        self._status_label.setWordWrap(True)
        shards_layout.addWidget(self._status_label)

        self._shards_grid = QGridLayout()
        self._shards_grid.setHorizontalSpacing(18)
        self._shards_grid.setVerticalSpacing(8)
        # 数据列不给拉伸因子，末尾占一个空列独得全部拉伸，避免多余宽度被平均分给各列（同 FormGrid）
        self._shards_grid.setColumnStretch(4, 1)
        shards_layout.addLayout(self._shards_grid)

        action_row = QHBoxLayout()
        action_row.setSpacing(12)
        self._action_btn = QPushButton(t("sakura.enable_btn"))
        self._action_btn.clicked.connect(self._on_action_btn)
        action_row.addWidget(self._action_btn)

        # frpc 状态和启停按钮放在"开启/关闭映射"的下一行。
        self._frpc_row = QWidget()
        frpc_layout = QHBoxLayout(self._frpc_row)
        frpc_layout.setContentsMargins(0, 0, 0, 0)
        frpc_layout.setSpacing(8)
        self._frpc_status_label = QLabel(t("sakura.frpc_status_stopped"))
        frpc_layout.addWidget(self._frpc_status_label)
        self._frpc_toggle_btn = QPushButton(t("sakura.frpc_start_btn"))
        self._frpc_toggle_btn.clicked.connect(self._on_frpc_toggle)
        frpc_layout.addWidget(self._frpc_toggle_btn)
        frpc_layout.addStretch()
        action_row.addStretch()
        shards_layout.addSpacing(4)
        shards_layout.addLayout(action_row)
        shards_layout.addWidget(self._frpc_row)
        self._frpc_row.setVisible(False)
        self._frpc_shown_running = False
        # frpc 会被本地服务器页的停服流程停掉、也可能自己退出，状态行要定时跟上实际状态
        self._frpc_timer = QTimer(self, interval=1000)
        self._frpc_timer.timeout.connect(self._tick_frpc_row)
        self._frpc_timer.start()
        root.addWidget(shards_card)
        root.addStretch()

        self._render_account_info("--", "--", "--", "--")
        self._load_token_display()

    # ── Token ───────────────────────────────────────────────────────────
    def _load_token_display(self) -> None:
        token = app_settings.get_sakura_token() or ""
        self._token_label.setText(mask_token(token) if token else t("token.empty"))

    def _change_token(self) -> None:
        dialog = dialogs.TextInputDialog(
            self.window(), t("token.change"), t("sakura.token_prompt"),
            validator=lambda v: None if sakura_frp.is_valid_token(v) else t("sakura.token_invalid_hint"))
        if not dialog.exec() or not dialog.result_text:
            return
        app_settings.set_sakura_token(dialog.result_text)
        self._load_token_display()
        self._reload_async()

    def _open_node_picker(self) -> None:
        if not self._node_choices:
            return
        dialog = NodePickerDialog(self.window(), t("sakura.node_picker_title"), self._build_picker_choices(),
                                  self._selected_node_id)
        if not dialog.exec() or dialog.result_id is None:
            return
        self._selected_node_id = dialog.result_id
        app_settings.set_sakura_last_node_id(dialog.result_id)
        self._update_node_display()

    def _build_picker_choices(self) -> list[NodeChoice]:
        """按中国大陆 / 海外及港澳台分组（_node_choices 已按 node_sort_key 排好），卡片上补充实时负载、
        "可穿透公开游戏"和高负载标记，悬停显示节点说明。"""
        mainland_count = sum(1 for _nid, node, _ok in self._node_choices if sakura_frp.node_is_mainland(node))
        overseas_count = len(self._node_choices) - mainland_count
        choices = []
        for node_id, node, eligible in self._node_choices:
            load = self._node_loads.get(node_id)
            detail = self._node_tag(node)
            if load is not None:
                detail = t("sakura.node_tag_load", tag=detail, load=f"{load:.0f}%")
            badges = []
            if sakura_frp.node_allows_public_game(node):
                badges.append((t("sakura.node_public_game"), "SUCCESS"))
            if load is not None and round(load) >= 80:  # 与卡片上显示的整数一致
                badges.append((t("sakura.node_high_load"), "ERROR"))
            group = (t("sakura.node_group_mainland", count=mainland_count) if sakura_frp.node_is_mainland(node)
                     else t("sakura.node_group_overseas", count=overseas_count))
            # 官方说明用 " | " 分隔多条提示，悬停时分行显示
            tooltip = "\n".join(p.strip() for p in str(node.get("description") or "").split("|") if p.strip())
            choices.append(NodeChoice(node_id, str(node.get("name", node_id)), detail, eligible,
                                      group=group, badges=badges, tooltip=tooltip))
        return choices

    @staticmethod
    def _node_tag(node: dict) -> str:
        return t("sakura.node_tag_free") if node.get("vip", 0) == 0 else t("sakura.node_tag_vip", level=node.get("vip"))

    def _update_node_display(self) -> None:
        node = self._nodes.get(str(self._selected_node_id), {}) if self._selected_node_id else {}
        self._node_btn.setText(node.get("name") or t("sakura.node_none_selected"))

    # ── 生命周期 ────────────────────────────────────────────────────────
    def on_cluster_changed(self, cluster) -> None:
        self._current_cluster = cluster
        self._render_shard_placeholders()
        self._reload_async()

    # ── 后台加载 ────────────────────────────────────────────────────────
    def _reload_async(self) -> None:
        self._reload_gen += 1
        gen = self._reload_gen
        token = app_settings.get_sakura_token()
        cluster = self._current_cluster
        cluster_key = str(cluster.path) if cluster else None
        self._status_label.setText("")
        if not token:
            self._render_account_info("--", "--", "--", "--")
            self._recent_traffic_label.setText("")
            self._render_shard_placeholders(t("sakura.token_not_configured"))
            return

        def work():
            user_info = sakura_frp.get_user_info(token)
            nodes = sakura_frp.list_nodes(token)
            try:
                node_loads = sakura_frp.get_node_loads(token)
            except sakura_frp.SakuraApiError:
                node_loads = {}  # 负载只是参考信息，取不到不影响选节点
            tunnels = sakura_frp.list_tunnels(token)
            by_key = {}
            if cluster and cluster.source == SaveSource.SERVER:
                for shard in cluster.shards:
                    tunnel = self._find_cluster_tunnel(tunnels, cluster, shard)
                    if tunnel:
                        by_key[(str(cluster.path), shard.name)] = tunnel
            recent_bytes = 0
            now = time.time()
            cutoff = now - 7 * 86400
            for tunnel in by_key.values():
                try:
                    history = sakura_frp.get_traffic(token, tunnel["id"])
                except sakura_frp.SakuraApiError:
                    continue
                for ts_str, val in history.items():
                    try:
                        ts = float(ts_str)
                    except (TypeError, ValueError):
                        continue
                    if ts > now * 2:
                        ts /= 1000
                    if ts >= cutoff:
                        recent_bytes += val
            occupants = []
            for c in self.ctx.env.clusters:
                if c.source != SaveSource.SERVER:
                    continue
                for shard in c.shards:
                    occupant_tunnel = self._find_cluster_tunnel(tunnels, c, shard)
                    if occupant_tunnel:
                        occupants.append(f"{c.name}（{shard.name}）")
            return user_info, nodes, node_loads, by_key, recent_bytes, len(tunnels), occupants

        def done(result) -> None:
            self._apply_loaded(*result, gen, cluster_key, token)

        def error(exc: Exception) -> None:
            self._apply_error(exc, gen, cluster_key, token)

        run_async(work, done, error)

    def _reload_is_current(self, gen, cluster_key, token) -> bool:
        cluster = self._current_cluster
        return (gen == self._reload_gen and cluster_key == (str(cluster.path) if cluster else None)
                and token == (app_settings.get_sakura_token() or ""))

    def _apply_error(self, exc, gen, cluster_key, token) -> None:
        if not self._reload_is_current(gen, cluster_key, token):
            return
        self._render_account_info("--", "--", "--", "--")
        self._recent_traffic_label.setText("")
        self._status_label.setText(t("sakura.api_error", detail=str(exc)))

    def _apply_loaded(self, user_info, nodes, node_loads, by_key, recent_bytes, tunnel_count, occupants,
                      gen, cluster_key, token) -> None:
        if not self._reload_is_current(gen, cluster_key, token):
            return
        self._nodes = nodes
        self._node_loads = node_loads
        self._tunnels_by_key = by_key
        self._tunnel_count = tunnel_count
        self._tunnel_occupants = list(occupants)
        self._recent_traffic_label.setText(
            t("sakura.recent_traffic", size=_format_bytes_adaptive(recent_bytes)) if by_key else "")
        self._max_tunnels = user_info.get("tunnels") or _FALLBACK_MAX_TUNNELS
        group = user_info.get("group") or {}
        self._my_group_level = group.get("level", 0)
        _today_used, remaining = (user_info.get("traffic") or [0, 0])[:2]
        self._render_account_info(group.get("name", "--"), f"{remaining / (1024 ** 3):.2f} GiB",
                                   user_info.get("speed") or "--", f"{self._tunnel_count}/{self._max_tunnels}")

        self._node_choices = []
        for node_id_str, node in nodes.items():
            if not sakura_frp.node_supports_udp(node) or not sakura_frp.node_accepts_new_tunnel(node):
                continue
            eligible = node.get("vip", 0) <= self._my_group_level
            self._node_choices.append((int(node_id_str), node, eligible))
        self._node_choices.sort(key=lambda item: sakura_frp.node_sort_key(item[1], item[2]))

        last_node_id = app_settings.get_sakura_last_node_id()
        eligible_ids = [nid for nid, _n, ok in self._node_choices if ok]
        if last_node_id in eligible_ids:
            self._selected_node_id = last_node_id
        elif self._selected_node_id not in eligible_ids:
            self._selected_node_id = eligible_ids[0] if eligible_ids else None
        self._update_node_display()
        self._render_shard_rows()

    def _render_account_info(self, *values) -> None:
        for label, value in zip(self._account_value_labels, values):
            label.setText(str(value))

    def _render_shard_placeholders(self, status_text: str | None = None) -> None:
        clear_layout(self._shards_grid)
        cluster = self._current_cluster
        text = status_text if status_text is not None else t("sakura.loading")
        if not cluster:
            self._shards_grid.addWidget(QLabel(t("local.select_cluster_first")), 0, 0)
            self._action_btn.setVisible(False)
            return
        self._action_btn.setVisible(True)
        if not cluster.shards:
            self._shards_grid.addWidget(QLabel(text), 0, 0)
            return
        for row, shard in enumerate(cluster.shards):
            self._shards_grid.addWidget(QLabel(shard.name), row, 0)
            muted = QLabel(text)
            muted.setProperty("muted", True)
            self._shards_grid.addWidget(muted, row, 1)

    @staticmethod
    def _is_master_shard(shard) -> bool:
        return load_shard_config(shard.path).shard.get("is_master", True)

    def _legacy_tunnel_name_is_unambiguous(self, cluster) -> bool:
        matches = [c for c in self.ctx.env.clusters if c.source == cluster.source and c.platform == cluster.platform
                   and c.path.name == cluster.path.name]
        return len(matches) == 1

    def _find_cluster_tunnel(self, tunnels, cluster, shard):
        tunnel = sakura_frp.find_dstcamp_tunnel(
            tunnels, cluster.path.name, shard.name, cluster.source.value, cluster.platform.value,
            cluster_identity=stable_path_key(cluster.path),
            allow_legacy=self._legacy_tunnel_name_is_unambiguous(cluster))
        if tunnel:
            return tunnel
        pointer = _frpc_pointer_path(cluster.path, shard.name, self.ctx.env)
        if not pointer.exists():
            return None
        try:
            tunnel_id = int(pointer.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None
        return next((item for item in tunnels if str(item.get("id", "")).isdigit() and int(item["id"]) == tunnel_id), None)

    def _render_shard_rows(self) -> None:
        clear_layout(self._shards_grid)
        cluster = self._current_cluster
        if not cluster:
            self._shards_grid.addWidget(QLabel(t("local.select_cluster_first")), 0, 0)
            self._action_btn.setVisible(False)
            return
        if cluster.source != SaveSource.SERVER:
            self._shards_grid.addWidget(QLabel(t("sakura.local_save_hint")), 0, 0)
            self._action_btn.setVisible(False)
            return
        if not cluster.shards:
            self._shards_grid.addWidget(QLabel(t("sakura.no_shards")), 0, 0)
            self._action_btn.setVisible(False)
            return
        if len(cluster.shards) > self._max_tunnels:
            label = QLabel(t("sakura.too_many_shards", count=len(cluster.shards), max=self._max_tunnels))
            label.setStyleSheet(f"color: {theme.hex('ERROR')};")
            self._shards_grid.addWidget(label, 0, 0)
            self._action_btn.setVisible(False)
            return

        any_mapped = False
        for row, shard in enumerate(cluster.shards):
            key = (str(cluster.path), shard.name)
            tunnel = self._tunnels_by_key.get(key)
            self._shards_grid.addWidget(QLabel(shard.name), row, 0)
            if tunnel:
                any_mapped = True
                remote = tunnel.get("remote", "?")
                mapped_label = QLabel(t("sakura.shard_mapped"))
                mapped_label.setStyleSheet(f"color: {theme.hex('ACCENT')};")
                self._shards_grid.addWidget(mapped_label, row, 1)
                port_label = QLabel(t("sakura.port_display", remote=remote))
                local_port = tunnel.get("local_port", "?")
                if str(local_port) != str(remote):
                    port_label.setToolTip(t("sakura.port_mismatch_hint", local=local_port, remote=remote))
                self._shards_grid.addWidget(port_label, row, 2)
                is_master = self._is_master_shard(shard)
                copy_btn = QPushButton(t("sakura.copy_connect_btn"))
                copy_btn.setEnabled(is_master)
                copy_btn.clicked.connect(lambda _c=False, tun=tunnel: self._copy_connect_string(tun))
                if not is_master:
                    copy_btn.setToolTip(t("sakura.copy_connect_master_only_hint"))
                self._shards_grid.addWidget(copy_btn, row, 3)
            else:
                unmapped = QLabel(t("sakura.shard_unmapped"))
                unmapped.setProperty("muted", True)
                self._shards_grid.addWidget(unmapped, row, 1)

        self._action_btn.setVisible(True)
        self._action_btn.setText(t("sakura.disable_btn") if any_mapped else t("sakura.enable_btn"))
        self._any_mapped = any_mapped
        if any_mapped:
            self._frpc_health = inspect_sakura_frpc()
            self._frpc_row.setVisible(True)
            self._refresh_frpc_row()
        else:
            self._frpc_health = None
            self._frpc_row.setVisible(False)
        self._tunnels_exhausted = not any_mapped and self._tunnel_count >= self._max_tunnels
        self._action_btn.setEnabled(not self._tunnels_exhausted)
        if self._tunnels_exhausted:
            hint = t("sakura.tunnels_exhausted_hint", max=self._max_tunnels)
            if self._tunnel_occupants:
                hint += "\n" + t("sakura.tunnels_exhausted_occupants", names="、".join(self._tunnel_occupants))
            self._action_btn.setToolTip(hint)
        else:
            self._action_btn.setToolTip("")

    def _copy_connect_string(self, tunnel: dict) -> None:
        node = self._nodes.get(str(tunnel.get("node")), {})
        host = node.get("host", "")
        remote = tunnel.get("remote", "")
        cluster = self._current_cluster
        password = get_cluster_option(load_cluster_config(cluster.path), "NETWORK", "cluster_password") if cluster else None
        text = f'c_connect("{host}", {remote}, "{password}")' if password else f'c_connect("{host}", {remote})'
        QGuiApplication.clipboard().setText(text)
        dialogs.show_toast(self.window(), t("sakura.connect_copied"))

    # ── frpc 本地进程 ───────────────────────────────────────────────────
    def _mapped_shards(self, cluster):
        return [s for s in cluster.shards if self.has_active_mapping(cluster, s)]

    def has_pointer(self, cluster, shard) -> bool:
        return _frpc_pointer_path(cluster.path, shard.name, self.ctx.env).exists()

    def has_active_mapping(self, cluster, shard) -> bool:
        return self.has_pointer(cluster, shard)

    def frpc_all_running(self, cluster) -> bool:
        shards = self._mapped_shards(cluster)
        if not shards:
            return False
        return all((proc := self.frpc.get(cluster.path, s.name)) is not None and proc.status == FrpcStatus.RUNNING
                   for s in shards)

    def _frpc_failed_error(self, cluster) -> str | None:
        health = self._frpc_health
        if health is not None and not health.ready:
            key = {"missing": "sakura.frpc_recovery_missing", "blocked": "sakura.frpc_recovery_blocked",
                   "unreadable": "sakura.frpc_recovery_unreadable",
                   "not_downloaded": "sakura.frpc_recovery_not_downloaded"}[health.status]
            return t(key, error=health.detail)
        for s in self._mapped_shards(cluster):
            proc = self.frpc.get(cluster.path, s.name)
            if proc is not None and proc.status == FrpcStatus.CRASHED and proc.error:
                return proc.error
        return None

    def _ensure_frpc_available(self, retry=None) -> SakuraFrpcHealth | None:
        """客户端可用时返回健康状态；否则引导恢复并返回 None。

        发布版不打包客户端：首次使用时问一句再下载，下完自动重新执行 retry（调用方原本的操作）。"""
        health = inspect_sakura_frpc()
        self._frpc_health = health
        if health.ready:
            return health
        if health.status == "not_downloaded":
            tool = tool_store.store_tool(SAKURA_FRPC_ARCHIVE)
            if dialogs.ask_yes_no(self.window(), t("sakura.frpc_download_title"),
                                  t("sakura.frpc_download_confirm", size=f"{tool.size / 1e6:.1f}")):

                def downloaded(_path) -> None:
                    self._frpc_health = inspect_sakura_frpc()
                    if self._any_mapped:
                        self._refresh_frpc_row()
                    if retry is not None and self._frpc_health.ready:
                        retry()

                download_tool_with_dialog(self.window(), SAKURA_FRPC_ARCHIVE, t("sakura.frpc_download_title"),
                                          downloaded)
            return None
        dialog = _FrpcRecoveryDialog(self.window(), health)
        dialog.exec()
        self._frpc_health = inspect_sakura_frpc()
        if self._any_mapped:
            self._refresh_frpc_row()
        if dialog.restored and self._frpc_health.ready:
            return self._frpc_health
        return None

    def _refresh_frpc_row(self) -> None:
        cluster = self._current_cluster
        running = bool(cluster) and self.frpc_all_running(cluster)
        error = self._frpc_failed_error(cluster) if cluster else None
        not_downloaded = self._frpc_health is not None and self._frpc_health.status == "not_downloaded"
        if error:
            if not_downloaded:
                text = t("sakura.frpc_status_not_downloaded")
            elif self._frpc_health and not self._frpc_health.ready:
                text = t("sakura.frpc_status_file_missing")
            else:
                text = t("sakura.frpc_status_failed")
            color = theme.hex("ERROR")
        elif running:
            text, color = t("sakura.frpc_status_running"), theme.hex("SUCCESS")
        else:
            text, color = t("sakura.frpc_status_stopped"), theme.hex("TEXT_MUTED")
        self._frpc_shown_running = running
        self._frpc_status_label.setText(text)
        self._frpc_status_label.setStyleSheet(f"color: {color};")
        self._frpc_status_label.setToolTip(self._frpc_failed_error(cluster) or "" if cluster else "")
        if not_downloaded:
            button_text = t("sakura.frpc_download_btn")
        elif self._frpc_health is not None and not self._frpc_health.ready:
            button_text = t("sakura.frpc_recovery_btn")
        else:
            button_text = t("sakura.frpc_stop_btn" if running else "sakura.frpc_start_btn")
        self._frpc_toggle_btn.setText(button_text)

    def _on_frpc_toggle(self) -> None:
        cluster = self._current_cluster
        if not cluster:
            return
        shards = self._mapped_shards(cluster)
        if not shards:
            return
        running = self.frpc_all_running(cluster)
        if running != self._frpc_shown_running:
            # 显示的状态已过期（如 frpc 已随停服结束），先刷新，不能反过来执行相反的操作
            self._refresh_frpc_row()
            return
        if running:
            remaining = [len(shards)]

            def one_done():
                remaining[0] -= 1
                if remaining[0] <= 0:
                    self._refresh_frpc_row()

            for s in shards:
                self.stop_frpc_for_shard(cluster, s, on_done=lambda: post_to_ui(lambda _a: one_done()))
        else:
            health = self._ensure_frpc_available(retry=self._on_frpc_toggle)
            if health is None:
                return
            for s in shards:
                self.maybe_start_frpc(cluster, s, frpc_exe=health.path)
            self._refresh_frpc_row()

    def _tick_frpc_row(self) -> None:
        """把已退出的 frpc 标记为失败，并在显示状态与实际不一致时刷新状态行。"""
        exited = False
        for proc in self.frpc.processes():
            if proc.status == FrpcStatus.RUNNING and (code := proc.poll_exit_code()) is not None:
                proc.status = FrpcStatus.CRASHED
                proc.error = t("sakura.frpc_exited", code=code)
                exited = True
        cluster = self._current_cluster
        if cluster and self._frpc_row.isVisible() and (
                exited or self.frpc_all_running(cluster) != self._frpc_shown_running):
            self._refresh_frpc_row()

    def maybe_start_frpc(self, cluster, shard, frpc_exe=None) -> None:
        pointer_path = _frpc_pointer_path(cluster.path, shard.name, self.ctx.env)
        if not pointer_path.exists():
            return
        health = SakuraFrpcHealth("ready", frpc_exe) if frpc_exe is not None else inspect_sakura_frpc()
        self._frpc_health = health
        existing = self.frpc.get(cluster.path, shard.name)
        restartable = existing is None or existing.status in {FrpcStatus.CRASHED, FrpcStatus.STOPPED}
        if health.ready and restartable:
            token = app_settings.get_sakura_token()
            tunnel_id = pointer_path.read_text(encoding="utf-8").strip()
            if token and tunnel_id:
                self.frpc.start(cluster.path, shard.name, health.path, token, int(tunnel_id))

    def stop_frpc_for_shard(self, cluster, shard, on_done=None) -> None:
        if self.frpc.get(cluster.path, shard.name):
            self.frpc.stop(cluster.path, shard.name, on_done=lambda _p: on_done() if on_done else None)
        elif on_done:
            on_done()

    def _running_shard_names(self, cluster) -> list[str]:
        return [s.name for s in cluster.shards
                if (proc := self.ctx.manager.get(cluster.path, s.name)) and proc.status in RUNNING_LIKE]

    # ── 开启/关闭映射 ────────────────────────────────────────────────────
    def _on_action_btn(self) -> None:
        if self._any_mapped:
            self._disable_mapping()
        else:
            self._enable_mapping()

    def _enable_mapping(self) -> None:
        cluster = self._current_cluster
        if not cluster:
            return
        token = app_settings.get_sakura_token()
        if not token:
            dialogs.show_warning(self.window(), t("sakura.enable_btn"), t("sakura.token_missing"))
            return
        node_id = self._selected_node_id
        if node_id is None:
            dialogs.show_warning(self.window(), t("sakura.enable_btn"), t("sakura.select_node_first"))
            return
        running = self._running_shard_names(cluster)
        if running:
            dialogs.show_warning(self.window(), t("sakura.require_stopped_title"),
                                  t("sakura.require_stopped_msg", shards="、".join(running)))
            return
        if not ensure_no_other_mapping(self.window(), self.ctx, cluster, "sakura", t("sakura.enable_btn")):
            return
        if self._ensure_frpc_available(retry=self._enable_mapping) is None:
            return
        if not ensure_lan_free_for_mapping(self.window(), self.ctx, cluster):
            return
        shards = list(cluster.shards)
        # 只对新建的隧道生效：已有隧道的远程端口建好后改不了，照旧复用
        port_dialog = dialogs.RemotePortDialog(self.window(), [shard.name for shard in shards],
                                              t("sakura.enable_btn"))
        if not port_dialog.exec():
            return
        remote_ports = port_dialog.result_ports

        progress = dialogs.LogDialog(self.window(), t("sakura.setup_progress_title"))
        progress.show()

        def log(text: str) -> None:
            post_to_ui(lambda _a: progress.append(text))

        def rollback(created_ids: list[int], originals: dict) -> None:
            # 任何一个世界失败：先删这次新建的隧道（复用的旧隧道不动），再把改过的 server.ini 和指针原样写回
            ok = True
            if created_ids:
                try:
                    sakura_frp.delete_tunnel(token, created_ids)
                except sakura_frp.SakuraApiError as exc:
                    ok = False
                    log(t("dlg.mapping_rollback_failed", tunnels="、".join(map(str, created_ids)), detail=exc))
            for shard in shards:
                if shard.name not in originals:
                    continue
                ini_bytes, pointer_text = originals[shard.name]
                pointer_path = _frpc_pointer_path(cluster.path, shard.name, self.ctx.env)
                try:
                    (shard.path / "server.ini").write_bytes(ini_bytes)
                    if pointer_text is None:
                        pointer_path.unlink(missing_ok=True)
                    else:
                        pointer_path.write_text(pointer_text, encoding="utf-8")
                except OSError as exc:
                    ok = False
                    log(t("sakura.api_error", detail=f"{shard.name}: {exc}"))
            if ok:
                log(t("dlg.mapping_rollback_done"))

        def work():
            created_ids: list[int] = []
            originals: dict[str, tuple] = {}
            try:
                # 第一步只动樱花后台：所有世界的隧道都建好/复用成功后，才改本地端口和指针
                ready = []
                for shard in shards:
                    log(t("sakura.setup_step_creating", shard=shard.name))
                    local_port = get_shard_option(load_shard_config(shard.path), "NETWORK", "server_port")
                    tunnels = sakura_frp.list_tunnels(token)
                    name = sakura_frp.sanitize_tunnel_name(cluster.path.name, shard.name, cluster.source.value,
                                                            cluster.platform.value, cluster_identity=stable_path_key(cluster.path))
                    existing = self._find_cluster_tunnel(tunnels, cluster, shard)
                    if existing:
                        tunnel_id, remote = existing["id"], existing.get("remote")
                    else:
                        created = sakura_frp.create_tunnel(token, name=name, type="udp", node=node_id,
                                                            local_ip="127.0.0.1", local_port=local_port,
                                                            note=f"DSTCamp: {cluster.path.name}/{shard.name}",
                                                            remote=remote_ports.get(shard.name))
                        tunnel_id, remote = created["id"], created.get("remote")
                        created_ids.append(tunnel_id)
                    remote_port = int(remote) if remote and str(remote).isdigit() else None
                    if remote_port is None or not (1 <= remote_port <= 65535):
                        raise sakura_frp.SakuraApiError(f"invalid remote port for {name}: {remote}")
                    sakura_frp.edit_tunnel(token, tunnel_id, name=name, local_port=remote_port)
                    ready.append((shard, tunnel_id, remote_port))
                for shard, tunnel_id, remote_port in ready:
                    shard_config = load_shard_config(shard.path)
                    pointer_path = _frpc_pointer_path(cluster.path, shard.name, self.ctx.env)
                    originals[shard.name] = ((shard.path / "server.ini").read_bytes(),
                                             pointer_path.read_text(encoding="utf-8") if pointer_path.exists() else None)
                    pointer_path.parent.mkdir(parents=True, exist_ok=True)
                    pointer_path.write_text(str(tunnel_id), encoding="utf-8")
                    set_shard_option(shard_config, "NETWORK", "server_port", remote_port)
                    save_shard_config(shard_config, shard.path)
                    log(t("sakura.setup_step_ready", shard=shard.name, port=remote_port))
            except Exception:
                rollback(created_ids, originals)
                raise

        def done(_result) -> None:
            self._on_enable_done(progress)

        def error(exc: Exception) -> None:
            progress.append(t("sakura.api_error", detail=str(exc)))
            progress.finish()
            self._reload_async()
            self.ctx.cluster_config_saved.emit(self._current_cluster)

        run_async(work, done, error)

    def _on_enable_done(self, progress) -> None:
        progress.finish()
        cluster = self._current_cluster
        if cluster:
            for shard in cluster.shards:
                self.maybe_start_frpc(cluster, shard)
        running = self._running_shard_names(cluster) if cluster else []
        if running:
            dialogs.show_info(self.window(), t("sakura.setup_done_title"),
                               t("sakura.setup_done_msg_restart", shards="、".join(running)))
        self._reload_async()
        self.ctx.cluster_config_saved.emit(cluster)

    def _disable_mapping(self) -> None:
        cluster = self._current_cluster
        if not cluster:
            return
        mapped = [(name, tunnel) for (path, name), tunnel in self._tunnels_by_key.items() if path == str(cluster.path)]
        # 删除后远程端口找不回来，确认框里列出当前端口，方便重新开启时填回去
        ports = "、".join(f"{name} {tunnel.get('remote', '?')}" for name, tunnel in mapped)
        if not dialogs.ask_yes_no(self.window(), t("sakura.disable_confirm_title"), t("sakura.disable_confirm_msg", ports=ports)):
            return
        token = app_settings.get_sakura_token()
        ids = [tunnel["id"] for _name, tunnel in mapped]

        def work():
            try:
                if ids:
                    sakura_frp.delete_tunnel(token, ids)
            except sakura_frp.SakuraApiError:
                pass
            for shard in cluster.shards:
                _frpc_pointer_path(cluster.path, shard.name, self.ctx.env).unlink(missing_ok=True)

        def done(_result) -> None:
            self._reload_async()
            self.ctx.cluster_config_saved.emit(cluster)

        run_async(work, done, lambda _exc: done(None))

    def release_cluster(self, cluster, remote: bool):
        """存档被删除（或新存档撞上旧存档残留的记录）时关闭樱花映射：本地指针与 frpc 立即清掉；
        remote 为 True 时返回在后台删除账号上隧道的函数，结果是未能删除的说明列表。"""
        pointers = {s.name: _frpc_pointer_path(cluster.path, s.name, self.ctx.env) for s in cluster.shards}
        mapped = [s for s in cluster.shards if pointers[s.name].exists()]
        if not mapped:
            return None
        pointer_ids = {}
        for shard in mapped:
            try:
                pointer_ids[shard.name] = int(pointers[shard.name].read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                pass
            pointers[shard.name].unlink(missing_ok=True)
            if self.frpc.get(cluster.path, shard.name):
                self.frpc.stop(cluster.path, shard.name)
        if not remote:
            return None
        token = app_settings.get_sakura_token()
        identity = stable_path_key(cluster.path)
        # 调用方随后会重新扫描环境（删除的存档不在其中），名字是否唯一要在这里先算好
        allow_legacy = self._legacy_tunnel_name_is_unambiguous(cluster)
        names = [s.name for s in mapped]

        def work() -> list[str]:
            if not token:
                return [remote_failure_item("sakura", names, t("save.delete_mapping_not_logged_in"))]
            try:
                tunnels = sakura_frp.list_tunnels(token)
                ids = set()
                for shard in mapped:
                    tunnel = sakura_frp.find_dstcamp_tunnel(
                        tunnels, cluster.path.name, shard.name, cluster.source.value, cluster.platform.value,
                        cluster_identity=identity, allow_legacy=allow_legacy)
                    if tunnel is None:
                        tunnel = next((x for x in tunnels if str(x.get("id", "")) == str(pointer_ids.get(shard.name))),
                                      None)
                    if tunnel is not None:
                        ids.add(int(tunnel["id"]))
                if ids:
                    sakura_frp.delete_tunnel(token, sorted(ids))
            except (sakura_frp.SakuraApiError, OSError) as exc:
                return [remote_failure_item("sakura", names, exc)]
            return []

        return work


class SakuraPage(Page):
    def __init__(self, ctx):
        super().__init__(ctx)
        self._mapping = _SakuraMappingPanel(ctx)
        self._lolia = LoliaPanel(ctx)
        self._selfhost = SelfHostPanel(ctx)

        root = QVBoxLayout(self)
        # 与其他主页签统一的外边距
        root.setContentsMargins(24, 12, 24, 12)
        card = Card(radius=15, alpha=0, border=True)  # 内部全透明，跟其它主页签统一
        root.addWidget(card)
        layout = QVBoxLayout(card)
        # 内容与边框的间距与其他页签一致
        layout.setContentsMargins(15, 13, 15, 13)
        self._tabs = PillTabBar(self._tab_labels(),
                                 height=36, pill_height=28, font_size_key="FONT_SIZE_SM")
        layout.addWidget(self._tabs)
        self._stack = QStackedWidget()
        # 各子页可纵向滚动：窗口矮或世界多时出现滚动条，不把按钮和世界映射文字压扁挤没
        for panel in (self._mapping, self._lolia, self._selfhost):
            self._stack.addWidget(VScrollArea(panel))
        layout.addWidget(self._stack, 1)
        self._tabs.current_changed.connect(self._on_sub_tab_changed)
        initial = app_settings.get_nat_sub_tab()
        if initial in _SUB_TAB_KEYS and initial != _SUB_TAB_KEYS[0]:
            index = _SUB_TAB_KEYS.index(initial)
            self._tabs.set_current_index(index)
            self._stack.setCurrentIndex(index)

        ctx.mapping_owner = self._mapping_owner
        ctx.ensure_lobby_accel = self._selfhost.ensure_lobby_accel
        ctx.stop_lobby_accel_async = self._selfhost.stop_lobby_accel_async
        ctx.poll_lobby_accel = self._selfhost.poll_lobby_accel
        ctx.stop_frpc_for_shard = self._stop_frpc_for_shard
        ctx.maybe_start_frpc = self._maybe_start_frpc
        ctx.frpc_ready = self._frpc_ready
        ctx.release_cluster_mappings = self._release_cluster_mappings

    @staticmethod
    def _tab_labels() -> list[str]:
        return [t("selfhost.tab_sakura"), t("selfhost.tab_lolia"), t("selfhost.tab_selfhost")]

    def _on_sub_tab_changed(self, index: int) -> None:
        self._stack.setCurrentIndex(index)
        app_settings.set_nat_sub_tab(_SUB_TAB_KEYS[index])

    # ── 跨页钩子 ────────────────────────────────────────────────────────
    def _mapping_owner(self, cluster, shard) -> str | None:
        if self._mapping.has_pointer(cluster, shard):
            return "sakura"
        if self._lolia.has_active_mapping(cluster, shard):
            return "lolia"
        if self._selfhost.has_active_mapping(cluster, shard):
            return "selfhost"
        return None

    def _stop_frpc_for_shard(self, cluster, shard, on_done=None) -> None:
        if self._mapping.frpc.get(cluster.path, shard.name):
            self._mapping.stop_frpc_for_shard(cluster, shard, on_done=on_done)
        elif self._lolia.has_active_mapping(cluster, shard):
            self._lolia.stop_frpc_for_shard(cluster, shard, on_done=on_done)
        else:
            self._selfhost.stop_frpc_for_shard(cluster, shard, on_done=on_done)

    def _maybe_start_frpc(self, cluster, shard) -> None:
        self._mapping.maybe_start_frpc(cluster, shard)
        self._lolia.maybe_start_frpc(cluster, shard)
        self._selfhost.maybe_start_frpc(cluster, shard)

    def _frpc_ready(self, cluster) -> bool:
        return (self._mapping.frpc_all_running(cluster) or self._lolia.frpc_running(cluster)
                or self._selfhost.frpc_running(cluster))

    def _release_cluster_mappings(self, cluster, remote: bool):
        """关闭这个存档上全部三种映射：本地记录立即清掉（须在重新扫描环境前调用），
        返回在后台停 frpc、删远程隧道的函数，结果是未能从账号上删除的隧道说明列表。"""
        jobs = [job for job in (self._mapping.release_cluster(cluster, remote),
                                self._lolia.release_cluster(cluster, remote),
                                self._selfhost.release_cluster(cluster)) if job]

        def work() -> list[str]:
            return [item for job in jobs for item in job()]

        return work

    # ── Page 协议 ───────────────────────────────────────────────────────
    def on_cluster_changed(self, cluster) -> None:
        self._mapping.on_cluster_changed(cluster)
        self._lolia.on_cluster_changed(cluster)
        self._selfhost.on_cluster_changed(cluster)

    def retranslate(self) -> None:
        self._tabs.set_labels(self._tab_labels())
        self.on_cluster_changed(self.ctx.selected_cluster())
