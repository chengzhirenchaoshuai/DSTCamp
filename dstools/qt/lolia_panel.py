"""内网穿透页的"Lolia 映射"子页签。

- 登录（OAuth2，features/lolia/api.py）：选节点后开启映射时为每个世界创建/复用 UDP 隧道（remark 标记归属）
  并拉取「原版 frpc 配置」，关闭映射时删除这些隧道；
- 不登录：用户在控制台自建隧道，为每个世界粘贴「原版 frpc 配置」或「LoliaFRP-CLI 快捷启动」命令。

两种方式都为每个世界生成本地配置，交给自建节点的原版 frpc.exe 以 ``-c`` 启动，进程管理复用
frp_selfhost/client.py 的 FrpcManager。
"""

import threading
import webbrowser
from collections import deque

from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

from dstools.features.cluster_config.config_manager import (
    get_cluster_option, get_shard_option, load_cluster_config, load_shard_config, save_shard_config,
    set_shard_option,
)
from dstools.features.frp_selfhost.client import FrpcManager, FrpcStatus
from dstools.features.local_service.shard_helpers import RUNNING_LIKE
from dstools.features.lolia import api as lolia_api
from dstools.features.lolia import config as lolia_config
from dstools.i18n import t
from dstools.models import SaveSource
from dstools.qt import dialogs, frpc_tool
from dstools.qt.lan_mapping_guard import ensure_lan_free_for_mapping, ensure_no_other_mapping, remote_failure_item
from dstools.qt.lolia_oauth_dialog import LoliaOAuthAppDialog
from dstools.qt.node_picker import NodeChoice, NodePickerDialog
from dstools.qt.theme import theme
from dstools.qt.threads import post_to_ui, run_async
from dstools.qt.widgets import AutoHideLabel, section_card
from dstools.shared import app_settings
from dstools.shared.frpc_log import FrpcOutputLog
from dstools.shared.resource_paths import data_dir
from dstools.shared.server_ports import stable_path_key

_FRPC_DATA_NAME = "lolia_frpc_config"


def _frpc_exe_path():
    # 原版 frpc，跟自建节点共用同一份（Lolia 官方注明 config 字段兼容原版 frpc）
    return frpc_tool.frpc_exe_path()


class _PasteSourceDialog(dialogs.Dialog):
    """多行粘贴框：原版 frpc 配置或快捷启动命令，确认时校验，不合格不关闭。"""

    def __init__(self, parent, shard_name: str):
        super().__init__(parent, t("lolia.paste_title", shard=shard_name), "lg")
        self.result_source: dict | None = None
        self.body.addWidget(self.text_label(t("lolia.paste_prompt"), size_key="FONT_SIZE_MD"))
        self._edit = QPlainTextEdit()
        self._edit.setFont(QFont("Consolas", 11))
        self._edit.setMinimumHeight(260)
        self.body.addWidget(self._edit)
        self._error = self.error_label()
        self.body.addWidget(self._error)
        self.add_buttons()
        self._edit.setFocus()

    def accept_if_valid(self) -> None:
        text = self._edit.toPlainText().strip()
        if not text:
            return
        try:
            self.result_source = lolia_config.parse_source(text)
        except lolia_config.LoliaError as exc:
            self._error.setText(t("lolia.paste_invalid", detail=str(exc)))
            return
        self.accept()


class LoliaPanel(QWidget):
    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        self.frpc = FrpcManager()
        self._current_cluster = None
        self._any_mapped = False
        self._nodes: dict[int, dict] = {}
        self._node_choices: list[NodeChoice] = []
        self._selected_node_id: int | None = None
        self._reload_gen = 0
        self._frpc_logs: dict[str, deque] = {}
        self._reason_pending: dict[int, tuple] = {}
        # 孤儿 frpc 认领要扫进程表（tasklist + 每个 frpc 一次 PowerShell，实测两个世界约 1.3 秒），
        # 放到后台做；每个世界每次运行只扫一次，之后靠自己启动/认领的跟踪记录
        self._scanned: set[str] = set()
        self._scanning = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 10, 0, 0)
        root.setSpacing(12)

        # ── 分区一：账号与节点（登录后才显示节点行和账号数据）
        account_card, account_layout = section_card(t("lolia.section_account"))
        form = QGridLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)
        form.addWidget(QLabel(t("lolia.account_label")), 0, 0)
        row1 = QHBoxLayout()
        row1.setSpacing(8)
        self._account_label = QLabel(t("lolia.not_logged_in"))
        row1.addWidget(self._account_label)
        self._login_btn = QPushButton(t("lolia.login_btn"))
        self._login_btn.clicked.connect(self._on_login_btn)
        row1.addWidget(self._login_btn)
        dashboard_btn = QPushButton(t("lolia.open_dashboard_btn"))
        dashboard_btn.clicked.connect(lambda: webbrowser.open(lolia_config.DASHBOARD_URL))
        row1.addWidget(dashboard_btn)
        # 内置 OAuth 应用失效时的兜底入口，只在未登录时显示
        self._oauth_btn = dialogs.style_button(QPushButton(t("lolia.oauth_app_btn")), "secondary")
        self._oauth_btn.clicked.connect(lambda: self._open_oauth_app_dialog(invalid=False))
        row1.addWidget(self._oauth_btn)
        row1.addStretch()
        form.addLayout(row1, 0, 1)
        self._node_caption = QLabel(t("lolia.node_label"))
        form.addWidget(self._node_caption, 1, 0)
        self._node_row = QWidget()
        row2 = QHBoxLayout(self._node_row)
        row2.setContentsMargins(0, 0, 0, 0)
        row2.setSpacing(8)
        self._node_btn = QPushButton(t("lolia.node_none"))
        self._node_btn.clicked.connect(self._open_node_picker)
        row2.addWidget(self._node_btn)
        self._refresh_btn = QPushButton(t("sakura.node_refresh_btn"))
        self._refresh_btn.clicked.connect(self._reload_async)
        row2.addWidget(self._refresh_btn)
        row2.addStretch()
        form.addWidget(self._node_row, 1, 1)
        form.setColumnStretch(1, 1)
        account_layout.addLayout(form)

        self._stats_widget = QWidget()
        stats_layout = QVBoxLayout(self._stats_widget)
        stats_layout.setContentsMargins(0, 0, 0, 0)
        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet(f"background: {theme.hex('CARD_BORDER')}; border: none;")
        stats_layout.addSpacing(2)
        stats_layout.addWidget(divider)
        stats_grid = QGridLayout()
        stats_grid.setHorizontalSpacing(36)
        stats_grid.setVerticalSpacing(2)
        self._stat_labels = []
        for col, key in enumerate(("lolia.account_traffic", "lolia.account_speed", "lolia.account_tunnels")):
            header = QLabel(t(key))
            header.setProperty("muted", True)
            header.setFont(theme.font("FONT_SIZE_SM"))
            stats_grid.addWidget(header, 0, col)
            value = QLabel("--")
            value.setFont(theme.font("FONT_SIZE_MD", bold=True))
            stats_grid.addWidget(value, 1, col)
            self._stat_labels.append(value)
        stats_grid.setColumnStretch(3, 1)
        stats_layout.addLayout(stats_grid)
        account_layout.addWidget(self._stats_widget)
        root.addWidget(account_card)

        # ── 分区二：世界映射（开启/关闭 + frpc 状态；未登录时每个世界粘贴来源）
        shards_card, shards_layout = section_card(t("sakura.section_shards"))
        self._status_label = AutoHideLabel()
        self._status_label.setStyleSheet(f"color: {theme.hex('ERROR')};")
        self._status_label.setWordWrap(True)
        shards_layout.addWidget(self._status_label)
        self._shards_grid = QGridLayout()
        self._shards_grid.setHorizontalSpacing(18)
        self._shards_grid.setVerticalSpacing(8)
        # 拉伸因子全给数据列后面的空列，数据列紧凑排列（同 qt/pages/sakura.py 的说明）
        self._shards_grid.setColumnStretch(4, 1)
        shards_layout.addLayout(self._shards_grid)

        action_row = QHBoxLayout()
        action_row.setSpacing(12)
        self._action_btn = QPushButton(t("lolia.enable_btn"))
        self._action_btn.clicked.connect(self._on_action_btn)
        action_row.addWidget(self._action_btn)
        action_row.addStretch()
        self._frpc_row = QWidget()
        frpc_layout = QHBoxLayout(self._frpc_row)
        frpc_layout.setContentsMargins(0, 0, 0, 0)
        frpc_layout.setSpacing(8)
        self._frpc_status_label = QLabel(t("selfhost.frpc_status_stopped"))
        frpc_layout.addWidget(self._frpc_status_label)
        self._frpc_toggle_btn = QPushButton(t("sakura.frpc_start_btn"))
        self._frpc_toggle_btn.clicked.connect(self._on_frpc_toggle)
        frpc_layout.addWidget(self._frpc_toggle_btn)
        frpc_layout.addStretch()
        shards_layout.addSpacing(4)
        shards_layout.addLayout(action_row)
        shards_layout.addWidget(self._frpc_row)
        self._frpc_row.setVisible(False)
        self._frpc_shown_running = False
        self._frpc_shown_blocked = False  # 状态行正显示"被安全软件拦截"，按钮此时是"处理拦截"
        self._frpc_shown_download_failed = False  # 状态行正显示"客户端下载失败"，按钮此时是"下载客户端"
        frpc_tool.add_listener(self._refresh_frpc_row)
        # frpc 可能随停服结束或自己退出，状态行定时跟上实际状态，只在变化时刷新
        self._frpc_timer = QTimer(self, interval=1000)
        self._frpc_timer.timeout.connect(self._tick_frpc_row)
        self._frpc_timer.start()
        root.addWidget(shards_card)
        root.addStretch()

        # 有已保存的登录令牌时先显示"获取中"，等后台拉到账号信息再切换，
        # 免得启动后先闪"未登录"、用户误以为要重新登录
        if lolia_api.is_logged_in():
            self._show_loading()
        else:
            self._show_logged_out()

    # ── 账号 ────────────────────────────────────────────────────────────
    def _set_account_busy(self, busy: bool) -> None:
        self._login_btn.setEnabled(not busy)
        self._refresh_btn.setEnabled(not busy)

    def _show_loading(self) -> None:
        self._account_label.setText(t("lolia.loading"))
        self._login_btn.setText(t("lolia.logout_btn"))
        self._oauth_btn.setVisible(False)
        self._set_account_busy(True)

    def _show_logged_out(self) -> None:
        self._account_label.setText(t("lolia.not_logged_in"))
        self._login_btn.setText(t("lolia.login_btn"))
        self._oauth_btn.setVisible(True)
        self._set_account_busy(False)
        self._node_caption.setVisible(False)
        self._node_row.setVisible(False)
        self._stats_widget.setVisible(False)
        self._nodes, self._node_choices, self._selected_node_id = {}, [], None

    def _on_login_btn(self) -> None:
        if lolia_api.is_logged_in():
            if not dialogs.ask_yes_no(self.window(), t("lolia.logout_btn"), t("lolia.logout_confirm")):
                return
            run_async(lolia_api.logout, lambda _r: self._after_logout(), lambda _e: self._after_logout())
            return
        cancel_event = threading.Event()
        progress = dialogs.LogDialog(self.window(), t("lolia.login_title"), on_cancel=cancel_event.set)
        progress.show()

        def open_browser(url: str) -> None:
            # 登录流程先校验 client_id，确认有效后才会走到这里打开浏览器
            post_to_ui(lambda _a: progress.append(t("lolia.login_waiting")))
            webbrowser.open(url)

        def work():
            lolia_api.login(open_browser, cancel_event)
            return lolia_api.get_user_info()

        def done(user) -> None:
            progress.append(t("lolia.login_ok", user=user.get("username", "")), "result_success")
            progress.finish()
            self._reload_async()

        def error(exc: Exception) -> None:
            if isinstance(exc, lolia_api.LoliaClientError):
                # 内置/自定义 OAuth 应用失效：直接转到创建引导
                progress.finish()
                progress.accept()
                self._open_oauth_app_dialog(invalid=True)
                return
            progress.append(t("lolia.login_failed", detail=str(exc)), "result_error")
            progress.finish()

        run_async(work, done, error)

    def _open_oauth_app_dialog(self, invalid: bool) -> None:
        dialog = LoliaOAuthAppDialog(self.window(), invalid=invalid)
        dialog.exec()
        if dialog.changed:
            self._after_logout()  # 换了应用，旧令牌已清除，需要重新登录

    def _after_logout(self) -> None:
        self._show_logged_out()
        self._render_shard_rows()

    def _reload_async(self) -> None:
        """登录状态下后台拉取账号、节点、隧道列表；过期结果按代号丢弃。"""
        if not lolia_api.is_logged_in():
            self._show_logged_out()
            return
        self._reload_gen += 1
        gen = self._reload_gen
        self._show_loading()

        def work():
            return lolia_api.get_user_info(), lolia_api.list_nodes(), lolia_api.list_tunnels()

        def done(result) -> None:
            if gen == self._reload_gen:
                self._apply_loaded(*result)

        def error(exc: Exception) -> None:
            if gen != self._reload_gen:
                return
            if isinstance(exc, lolia_api.LoliaAuthError):
                self._after_logout()
                self._status_label.setText(t("lolia.relogin_needed"))
            else:
                # 网络等临时错误：仍是登录状态，可点"刷新"重试或退出登录
                self._account_label.setText(t("lolia.load_failed"))
                self._set_account_busy(False)
                self._status_label.setText(t("lolia.api_error", detail=str(exc)))

        run_async(work, done, error)

    def _apply_loaded(self, user: dict, nodes: list[dict], tunnels: list[dict]) -> None:
        self._account_label.setText(user.get("username") or "--")
        self._login_btn.setText(t("lolia.logout_btn"))
        self._set_account_busy(False)
        self._node_caption.setVisible(True)
        self._node_row.setVisible(True)
        self._stats_widget.setVisible(True)
        self._stat_labels[0].setText(f"{lolia_api.available_traffic(user) / (1024 ** 3):.2f} GiB")
        speed = lolia_api.bandwidth_mbps(user)
        self._stat_labels[1].setText(f"{speed} Mbps" if speed else t("lolia.speed_unlimited"))
        self._stat_labels[2].setText(f"{len(tunnels)}/{user.get('max_tunnel_count', '--')}")

        has_kyc = bool(user.get("has_kyc"))
        self._nodes = {int(n["id"]): n for n in nodes if lolia_api.node_supports_udp(n)}
        self._node_choices = self._build_node_choices(has_kyc)
        eligible_ids = [c.node_id for c in self._node_choices if c.eligible]
        last = app_settings.get_lolia_last_node_id()
        if last in eligible_ids:
            self._selected_node_id = last
        elif self._selected_node_id not in eligible_ids:
            self._selected_node_id = eligible_ids[0] if eligible_ids else None
        self._update_node_display()
        self._render_shard_rows()

    def _build_node_choices(self, has_kyc: bool) -> list[NodeChoice]:
        """按地区分组、组内按名称固定排序（features/lolia/api.py 的 node_sort_key），
        卡片上补充流量倍率、高负载标记，悬停显示节点备注。"""
        rows = []
        for node_id, node in self._nodes.items():
            eligible = has_kyc or not node.get("need_kyc")
            rows.append((lolia_api.node_sort_key(node, eligible), node_id, node, eligible))
        rows.sort(key=lambda r: r[0])
        counts = {}
        for _key, _nid, node, _ok in rows:
            region = lolia_api.node_region(node)
            counts[region] = counts.get(region, 0) + 1
        choices = []
        for _key, node_id, node, eligible in rows:
            load = node.get("load")
            detail = t("lolia.node_tag", bandwidth=node.get("bandwidth", "--"),
                       load=f"{load:.0f}%" if isinstance(load, (int, float)) else "--")
            if not eligible:
                detail += " · " + t("lolia.node_tag_kyc")  # 已实名时大陆节点全都需要实名，不再逐个标注
            badges = []
            ratio = node.get("traffic_ratio")
            if isinstance(ratio, (int, float)) and ratio != 1:
                # 倍率 >1 更费流量（流量要靠签到获取），<1 更省
                badges.append((t("lolia.node_ratio", ratio=f"{ratio:g}"), "ERROR" if ratio > 1 else "SUCCESS"))
            if isinstance(load, (int, float)) and load >= 80:
                badges.append((t("lolia.node_high_load"), "ERROR"))
            region = lolia_api.node_region(node)
            group = t("lolia.node_group", name=t(f"lolia.region_{region}"), count=counts[region])
            choices.append(NodeChoice(node_id, str(node.get("name", node_id)), detail, eligible,
                                      group=group, badges=badges, tooltip=str(node.get("remark") or "")))
        return choices

    def _open_node_picker(self) -> None:
        if not self._node_choices:
            return
        dialog = NodePickerDialog(self.window(), t("lolia.node_picker_title"), self._node_choices,
                                  self._selected_node_id)
        if not dialog.exec() or dialog.result_id is None:
            return
        self._selected_node_id = dialog.result_id
        app_settings.set_lolia_last_node_id(dialog.result_id)
        self._update_node_display()

    def _update_node_display(self) -> None:
        node = self._nodes.get(self._selected_node_id) if self._selected_node_id is not None else None
        self._node_btn.setText(node.get("name", str(self._selected_node_id)) if node else t("lolia.node_none"))

    # ── 世界列表 ────────────────────────────────────────────────────────
    def on_cluster_changed(self, cluster) -> None:
        self._current_cluster = cluster
        self._render_shard_rows()
        self._reload_async()

    def _clear_shards_grid(self) -> None:
        while self._shards_grid.count():
            item = self._shards_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # 立即隐藏并摘掉父级，不等 deleteLater()（同 qt/pages/sakura.py 的说明）
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()

    @staticmethod
    def _is_master_shard(shard) -> bool:
        return load_shard_config(shard.path).shard.get("is_master", True)

    def _render_shard_rows(self) -> None:
        self._clear_shards_grid()
        self._status_label.setText("")
        cluster = self._current_cluster
        if not cluster or cluster.source != SaveSource.SERVER or not cluster.shards:
            key = ("local.select_cluster_first" if not cluster else
                   "sakura.local_save_hint" if cluster.source != SaveSource.SERVER else "sakura.no_shards")
            self._shards_grid.addWidget(QLabel(t(key)), 0, 0)
            self._action_btn.setVisible(False)
            self._frpc_row.setVisible(False)
            return

        logged_in = lolia_api.is_logged_in()
        any_mapped = False
        for row, shard in enumerate(cluster.shards):
            self._shards_grid.addWidget(QLabel(shard.name), row, 0)
            mapping = app_settings.get_lolia_mapping(cluster.path, shard.name)
            if mapping is not None:
                any_mapped = True
                mapped_label = QLabel(t("sakura.shard_mapped"))
                mapped_label.setStyleSheet(f"color: {theme.hex('ACCENT')};")
                self._shards_grid.addWidget(mapped_label, row, 1)
                self._shards_grid.addWidget(QLabel(t("sakura.port_display", remote=mapping["remote_port"])), row, 2)
                is_master = self._is_master_shard(shard)
                copy_btn = QPushButton(t("sakura.copy_connect_btn"))
                copy_btn.setEnabled(is_master)
                copy_btn.clicked.connect(lambda _c=False, m=mapping: self._copy_connect_string(m))
                if not is_master:
                    copy_btn.setToolTip(t("sakura.copy_connect_master_only_hint"))
                self._shards_grid.addWidget(copy_btn, row, 3)
            else:
                unmapped = QLabel(t("sakura.shard_unmapped"))
                unmapped.setProperty("muted", True)
                self._shards_grid.addWidget(unmapped, row, 1)
                if logged_in:
                    continue  # 登录后开启映射自动建隧道，不需要粘贴
                source = app_settings.get_lolia_source(cluster.path, shard.name)
                source_label = QLabel(self._source_text(source))
                source_label.setProperty("muted", source is None)
                self._shards_grid.addWidget(source_label, row, 2)
                paste_btn = QPushButton(t("lolia.repaste_btn") if source else t("lolia.paste_btn"))
                paste_btn.clicked.connect(lambda _c=False, s=shard: self._paste_source(s))
                self._shards_grid.addWidget(paste_btn, row, 3)

        self._any_mapped = any_mapped
        self._action_btn.setVisible(True)
        self._action_btn.setText(t("sakura.disable_btn") if any_mapped else t("lolia.enable_btn"))
        self._frpc_row.setVisible(any_mapped)
        if any_mapped:
            self._refresh_frpc_row()
            self._reconcile_async(cluster)

    @staticmethod
    def _source_text(source: dict | None) -> str:
        if source is None:
            return t("lolia.source_none")
        if source["kind"] == "cli":
            return t("lolia.source_cli", id=source["id"])
        return t("lolia.source_config")

    def _paste_source(self, shard) -> None:
        cluster = self._current_cluster
        if not cluster:
            return
        dialog = _PasteSourceDialog(self.window(), shard.name)
        if not dialog.exec() or dialog.result_source is None:
            return
        app_settings.set_lolia_source(cluster.path, shard.name, dialog.result_source)
        self._render_shard_rows()

    def _copy_connect_string(self, mapping: dict) -> None:
        cluster = self._current_cluster
        password = get_cluster_option(load_cluster_config(cluster.path), "NETWORK", "cluster_password") if cluster else None
        host, port = mapping["host"], mapping["remote_port"]
        text = f'c_connect("{host}", {port}, "{password}")' if password else f'c_connect("{host}", {port})'
        QGuiApplication.clipboard().setText(text)
        dialogs.show_toast(self.window(), t("sakura.connect_copied"))

    # ── frpc 本地进程（每个世界一份配置、一个进程：不同世界的隧道可能在不同节点）──
    @staticmethod
    def _proc_key(cluster, shard):
        # FrpcManager 按路径字符串区分进程，这里用"存档路径/世界名"当键
        return cluster.path / shard.name

    def _frpc_config_path(self, cluster, shard):
        root = data_dir(_FRPC_DATA_NAME)
        return root / f"{cluster.path.name}__{stable_path_key(cluster.path)}__{shard.name}.toml"

    def has_active_mapping(self, cluster, shard) -> bool:
        return app_settings.get_lolia_mapping(cluster.path, shard.name) is not None

    def _mapped_shards(self, cluster) -> list:
        return [s for s in cluster.shards if self.has_active_mapping(cluster, s)]

    def maybe_start_frpc(self, cluster, shard) -> None:
        if not self.has_active_mapping(cluster, shard):
            return
        config_path = self._frpc_config_path(cluster, shard)
        if not config_path.exists():
            return  # 缺失时由状态行提示用户重新开启映射
        exe = _frpc_exe_path()
        if not frpc_tool.ensure(exe, lambda: self.maybe_start_frpc(cluster, shard)):
            return  # 发布版不打包 frpc：后台下载完成后再拉起
        key = self._proc_key(cluster, shard)
        existing = self._reconcile_once(key, config_path)
        if existing is not None and existing.status not in (FrpcStatus.CRASHED, FrpcStatus.STOPPED):
            return  # 已在运行/启停中；崩溃或已停止的才重新拉起
        self.frpc.start(key, exe, config_path, log=FrpcOutputLog(cluster.path, f"Lolia {shard.name}"))

    def stop_frpc_for_shard(self, cluster, shard, on_done=None) -> None:
        # 单个世界停服时不停 frpc（同自建节点），下次开服直接可用
        if on_done:
            on_done()

    def _reconcile_once(self, key, config_path):
        if str(key) in self._scanned:
            return self.frpc.get(key)
        proc = self.frpc.reconcile(key, _frpc_exe_path(), config_path)
        self._scanned.add(str(key))
        return proc

    def _shard_running(self, cluster, shard, *, scan: bool) -> bool:
        key = self._proc_key(cluster, shard)
        if scan:
            self._reconcile_once(key, self._frpc_config_path(cluster, shard))
        proc = self.frpc.get(key)
        return proc is not None and proc.status == FrpcStatus.RUNNING

    def _reconcile_async(self, cluster) -> None:
        """后台认领上次遗留的 frpc 进程，完成后刷新状态行；扫描期间状态显示检测中。"""
        pending = [(self._proc_key(cluster, s), self._frpc_config_path(cluster, s))
                   for s in self._mapped_shards(cluster)
                   if str(self._proc_key(cluster, s)) not in self._scanned]
        if not pending or self._scanning:
            return
        self._scanning = True
        self._refresh_frpc_row()

        def work():
            for key, config_path in pending:
                self.frpc.reconcile(key, _frpc_exe_path(), config_path)

        def finish(_result=None) -> None:
            self._scanning = False
            self._scanned.update(str(key) for key, _cfg in pending)
            if self._current_cluster is cluster:
                self._refresh_frpc_row()

        run_async(work, finish, finish)

    def frpc_running(self, cluster) -> bool:
        """所有已映射世界的 frpc 都在跑才算就绪；没映射时不扫描进程表。"""
        mapped = self._mapped_shards(cluster)
        return bool(mapped) and all(self._shard_running(cluster, s, scan=True) for s in mapped)

    def _tick_frpc_row(self) -> None:
        exited = False
        for proc in self.frpc.processes():
            # 持续收走输出、只留最后几行：退出时据此给出 frpc 自己报的原因
            # （真机：Lolia 拒绝登录时只看退出码 1 看不出是"可用流量已耗尽"）
            tail = self._frpc_logs.setdefault(str(proc.cluster_path), deque(maxlen=30))
            tail.extend(proc.read_available_lines())
            if proc.proc is not None and proc.status == FrpcStatus.RUNNING \
                    and (code := proc.poll_exit_code()) is not None:
                proc.status = FrpcStatus.CRASHED
                proc.error = t("sakura.frpc_exited", code=code)
                self._reason_pending[id(proc)] = (proc, proc.error)
                exited = True
        # 进程退出时读输出的线程可能还没交出最后几行，之后几次刷新里继续找原因
        for key, (proc, base_error) in list(self._reason_pending.items()):
            tail = self._frpc_logs.get(str(proc.cluster_path)) or ()
            reason = lolia_config.frpc_failure_reason(list(tail))
            if reason and "流量" in reason:
                reason += t("lolia.traffic_hint")
            if reason or proc.status != FrpcStatus.CRASHED:
                del self._reason_pending[key]
            if reason and proc.status == FrpcStatus.CRASHED:
                proc.error = f"{base_error}：{reason}"
                exited = True
        cluster = self._current_cluster
        if not cluster or not self._frpc_row.isVisible():
            return
        mapped = self._mapped_shards(cluster)
        # 每秒一次，只看已跟踪的进程，不扫描进程表
        running = bool(mapped) and all(self._shard_running(cluster, s, scan=False) for s in mapped)
        if exited or running != self._frpc_shown_running:
            self._refresh_frpc_row()

    def _frpc_failed_error(self, cluster) -> str | None:
        for shard in self._mapped_shards(cluster):
            if not self._frpc_config_path(cluster, shard).exists():
                return f"{shard.name}: {t('lolia.config_missing')}"
            proc = self.frpc.get(self._proc_key(cluster, shard))
            if proc is not None and proc.status == FrpcStatus.CRASHED and proc.error:
                return f"{shard.name}: {proc.error}"
        return None

    def _frpc_blocked(self, cluster) -> bool:
        return any(
            (proc := self.frpc.get(self._proc_key(cluster, shard))) is not None
            and proc.status == FrpcStatus.CRASHED and proc.blocked
            for shard in self._mapped_shards(cluster)
        )

    def _refresh_frpc_row(self) -> None:
        cluster = self._current_cluster
        mapped = self._mapped_shards(cluster) if cluster else []
        running = bool(mapped) and all(self._shard_running(cluster, s, scan=False) for s in mapped)
        error = self._frpc_failed_error(cluster) if cluster and not running and not self._scanning else None
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
            text, color = t("lolia.frpc_status_checking"), theme.hex("TEXT_MUTED")
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
        if error:
            self._status_label.setText(error)  # 失败原因直接显示出来，不只藏在悬停提示里
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
        mapped = self._mapped_shards(cluster)
        running = bool(mapped) and all(self._shard_running(cluster, s, scan=False) for s in mapped)
        if running != self._frpc_shown_running:
            # 显示的状态已过期，先刷新，不能反过来执行相反的操作
            self._refresh_frpc_row()
            return
        if running:
            for shard in cluster.shards:
                key = self._proc_key(cluster, shard)
                if self.frpc.get(key) is not None:
                    self.frpc.stop(key, on_done=lambda _p: post_to_ui(lambda _a: self._refresh_frpc_row()))
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

    def _enable_mapping(self) -> None:
        cluster = self._current_cluster
        if not cluster:
            return
        oauth = lolia_api.is_logged_in()
        node_id = self._selected_node_id
        sources = {s.name: app_settings.get_lolia_source(cluster.path, s.name) for s in cluster.shards}
        if oauth and node_id is None:
            dialogs.show_warning(self.window(), t("lolia.enable_btn"), t("lolia.select_node_first"))
            return
        missing = [name for name, source in sources.items() if source is None]
        if not oauth and missing:
            dialogs.show_warning(self.window(), t("lolia.enable_btn"),
                                  t("lolia.source_missing", shards="、".join(missing)))
            return
        running = self._running_shard_names(cluster)
        if running:
            dialogs.show_warning(self.window(), t("sakura.require_stopped_title"),
                                  t("sakura.require_stopped_msg", shards="、".join(running)))
            return
        if not ensure_no_other_mapping(self.window(), self.ctx, cluster, "lolia", t("lolia.enable_btn")):
            return
        if not ensure_lan_free_for_mapping(self.window(), self.ctx, cluster):
            return
        shards = list(cluster.shards)
        remote_ports: dict[str, int | None] = {}
        if oauth:
            # 只对新建的隧道生效；粘贴配置方式用的是用户自己的隧道，端口本来就固定
            port_dialog = dialogs.RemotePortDialog(self.window(), [shard.name for shard in shards],
                                                   t("lolia.enable_btn"))
            if not port_dialog.exec():
                return
            remote_ports = port_dialog.result_ports

        progress = dialogs.LogDialog(self.window(), t("lolia.setup_progress_title"))
        progress.show()

        def log(text: str) -> None:
            post_to_ui(lambda _a: progress.append(text))

        def work():
            # 先把所有世界的隧道和配置都准备好再落盘；任何一个失败都撤销这次新建的隧道并还原已写的文件
            created: list[str] = []
            originals: dict[str, tuple] = {}
            try:
                if oauth:
                    prepared = self._prepare_with_oauth(cluster, shards, node_id, log, remote_ports, created)
                else:
                    log(t("lolia.setup_fetching"))
                    prepared = {s.name: (*lolia_config.prepare_shard(sources[s.name]), None) for s in shards}
                for shard in shards:
                    toml_text, remote_port, host, tunnel_name = prepared[shard.name]
                    config_path = self._frpc_config_path(cluster, shard)
                    originals[shard.name] = (
                        (shard.path / "server.ini").read_bytes(),
                        config_path.read_text(encoding="utf-8") if config_path.exists() else None,
                        app_settings.get_lolia_mapping(cluster.path, shard.name),
                    )
                    config_path.parent.mkdir(parents=True, exist_ok=True)
                    config_path.write_text(toml_text, encoding="utf-8")
                    shard_config = load_shard_config(shard.path)
                    set_shard_option(shard_config, "NETWORK", "server_port", remote_port)
                    save_shard_config(shard_config, shard.path)
                    app_settings.set_lolia_mapping(cluster.path, shard.name, remote_port, host, tunnel_name)
                    log(t("sakura.setup_step_ready", shard=shard.name, port=remote_port))
            except Exception:
                self._rollback_enable(cluster, shards, created, originals, log)
                raise

        def done(_result) -> None:
            self._restart_frpc(cluster, on_done=lambda: self._on_enable_done(progress))

        def error(exc: Exception) -> None:
            detail = t("lolia.relogin_needed") if isinstance(exc, lolia_api.LoliaAuthError) else str(exc)
            progress.append(t("lolia.api_error", detail=detail), "result_error")
            progress.finish()
            self._render_shard_rows()
            self._reload_async()
            self.ctx.cluster_config_saved.emit(cluster)

        run_async(work, done, error)

    @staticmethod
    def _prepare_with_oauth(cluster, shards, node_id: int, log, remote_ports: dict | None = None,
                            created: list | None = None) -> dict:
        """（工作线程）为每个世界创建/复用隧道并拉取配置，返回 {世界名: (本地 TOML, 远程端口, 节点地址, 隧道名)}；
        这次新建的隧道名追加到 created，中途失败时由调用方撤销。"""
        user = lolia_api.get_user_info()
        if not user.get("has_qq"):
            raise lolia_config.LoliaError(t("lolia.qq_required"))
        if lolia_api.available_traffic(user) <= 0:
            # 没流量时 frpc 一连就被拒（"可用流量已耗尽"），不白建隧道
            raise lolia_config.LoliaError(t("lolia.no_traffic"))
        tunnels = lolia_api.list_tunnels()
        identity = stable_path_key(cluster.path)
        remarks = {s.name: lolia_api.make_remark(cluster.path.name, s.name, cluster.source.value,
                                                 cluster.platform.value, identity) for s in shards}
        existing = {name: lolia_api.find_tunnel(tunnels, remark) for name, remark in remarks.items()}
        need = sum(1 for tunnel in existing.values() if tunnel is None)
        max_count = int(user.get("max_tunnel_count") or 0)
        if need and len(tunnels) + need > max_count:
            raise lolia_config.LoliaError(t("lolia.tunnels_exhausted", need=need, used=len(tunnels), max=max_count))
        prepared = {}
        for shard in shards:
            log(t("lolia.setup_step_creating", shard=shard.name))
            tunnel = existing[shard.name]
            if tunnel is None:
                local_port = get_shard_option(load_shard_config(shard.path), "NETWORK", "server_port") or 10999
                tunnel = lolia_api.create_udp_tunnel(node_id, int(local_port), remarks[shard.name],
                                                     (remote_ports or {}).get(shard.name))
                if created is not None and tunnel.get("name"):
                    created.append(tunnel["name"])
            remote_port = int(tunnel["remote_port"])
            if tunnel.get("local_port") != remote_port:
                # 让控制台里的本地端口跟实际一致（本地配置也会改写，这一步只为显示正确）
                lolia_api.edit_tunnel(tunnel["name"], local_port=remote_port)
            config = lolia_config.parse_toml(lolia_api.get_frpc_config_text(tunnel["name"]))
            toml_text, port, host = lolia_config.local_config_from(config)
            prepared[shard.name] = (toml_text, port, host, tunnel["name"])
        return prepared

    def _rollback_enable(self, cluster, shards, created: list[str], originals: dict, log) -> None:
        """（工作线程）开启失败：先删这次新建的隧道（复用的旧隧道不动），再把 server.ini、frpc 配置和映射记录还原。"""
        failed = []
        detail = ""
        for name in created:
            try:
                lolia_api.delete_tunnel(name)
            except (lolia_config.LoliaError, OSError) as exc:
                failed.append(name)
                detail = str(exc)
        if failed:
            log(t("dlg.mapping_rollback_failed", tunnels="、".join(failed), detail=detail))
        restored = True
        for shard in shards:
            if shard.name not in originals:
                continue
            ini_bytes, config_text, mapping = originals[shard.name]
            config_path = self._frpc_config_path(cluster, shard)
            try:
                (shard.path / "server.ini").write_bytes(ini_bytes)
                if config_text is None:
                    config_path.unlink(missing_ok=True)
                else:
                    config_path.write_text(config_text, encoding="utf-8")
            except OSError as exc:
                restored = False
                log(t("lolia.api_error", detail=f"{shard.name}: {exc}"))
            if mapping is None:
                app_settings.set_lolia_mapping(cluster.path, shard.name, None)
            else:
                app_settings.set_lolia_mapping(cluster.path, shard.name, mapping["remote_port"],
                                               mapping["host"], mapping["tunnel"])
        if not failed and restored:
            log(t("dlg.mapping_rollback_done"))

    def _restart_frpc(self, cluster, on_done) -> None:
        """配置已更新：先停掉旧进程（在后台线程停），全部停完后按新配置启动。"""
        tracked = [s for s in cluster.shards if self.frpc.get(self._proc_key(cluster, s)) is not None]
        pending = {"count": len(tracked)}

        def start_all():
            for shard in cluster.shards:
                self.maybe_start_frpc(cluster, shard)
            on_done()

        def one_stopped(_proc):
            def on_ui(_a):
                pending["count"] -= 1
                if pending["count"] == 0:
                    start_all()
            post_to_ui(on_ui)

        if not tracked:
            start_all()
            return
        for shard in tracked:
            self.frpc.stop(self._proc_key(cluster, shard), on_done=one_stopped)

    def _on_enable_done(self, progress) -> None:
        progress.finish()
        cluster = self._current_cluster
        running = self._running_shard_names(cluster) if cluster else []
        if running:
            dialogs.show_info(self.window(), t("sakura.setup_done_title"),
                               t("sakura.setup_done_msg_restart", shards="、".join(running)))
        self._render_shard_rows()
        self._reload_async()
        self.ctx.cluster_config_saved.emit(cluster)

    def _disable_mapping(self) -> None:
        cluster = self._current_cluster
        if not cluster:
            return
        mappings = [app_settings.get_lolia_mapping(cluster.path, s.name) for s in cluster.shards]
        tunnel_names = [m["tunnel"] for m in mappings if m and m["tunnel"]]
        delete_remote = bool(tunnel_names) and lolia_api.is_logged_in()
        # 删除后远程端口找不回来，确认框里列出当前端口，方便重新开启时填回去
        ports = "、".join(f"{shard.name} {m['remote_port']}" for shard, m in zip(cluster.shards, mappings) if m)
        message = (t("lolia.disable_confirm_msg_oauth", ports=ports) if delete_remote
                   else t("lolia.disable_confirm_msg"))
        if not dialogs.ask_yes_no(self.window(), t("sakura.disable_confirm_title"), message):
            return
        for shard in cluster.shards:
            app_settings.set_lolia_mapping(cluster.path, shard.name, None)
            config_path = self._frpc_config_path(cluster, shard)
            # 进程退出后再删配置：配置里有认证 Token，且孤儿认领靠配置路径。
            # 映射开启期间状态行刷新时已认领过进程，这里不再同步扫描进程表。
            key = self._proc_key(cluster, shard)
            if self.frpc.get(key) is not None:
                self.frpc.stop(key, on_done=lambda _p, c=config_path: c.unlink(missing_ok=True))
            else:
                config_path.unlink(missing_ok=True)
        self._render_shard_rows()
        self.ctx.cluster_config_saved.emit(cluster)
        if not delete_remote:
            return

        def work():
            # 删除失败（如已在控制台手动删掉）不影响本地关闭，结果以刷新后的列表为准
            for name in tunnel_names:
                try:
                    lolia_api.delete_tunnel(name)
                except lolia_config.LoliaError:
                    pass

        run_async(work, lambda _r: self._reload_async(), lambda _e: self._reload_async())

    def release_cluster(self, cluster, remote: bool):
        """存档被删除（或新存档撞上旧存档残留的记录）时关闭 Lolia 映射：本地记录立即清掉，返回在后台
        停 frpc、删配置的函数；remote 为 True 时顺带删除 DSTCamp 自动创建的隧道，结果是未能删除的说明列表。"""
        for shard in cluster.shards:
            # 粘贴过的隧道来源含节点凭据，存档没了一并删除
            if app_settings.get_lolia_source(cluster.path, shard.name) is not None:
                app_settings.set_lolia_source(cluster.path, shard.name, None)
        mappings = {s.name: app_settings.get_lolia_mapping(cluster.path, s.name) for s in cluster.shards}
        mapped = [s for s in cluster.shards if mappings[s.name] is not None]
        if not mapped:
            return None
        for shard in mapped:
            app_settings.set_lolia_mapping(cluster.path, shard.name, None)
        procs = [(self._proc_key(cluster, s), self._frpc_config_path(cluster, s)) for s in mapped]
        # 只有 OAuth 自动创建的隧道归 DSTCamp 管；粘贴配置方式的隧道是用户自己建的，与关闭映射一样不删
        tunnels = {s.name: mappings[s.name]["tunnel"] for s in mapped if mappings[s.name]["tunnel"]} if remote else {}

        def work() -> list[str]:
            # 停服后 frpc 仍在运行，可能是上次启动 DSTCamp 时留下的，要按配置路径认领后再停
            for key, config_path in procs:
                if self.frpc.reconcile(key, _frpc_exe_path(), config_path) is not None:
                    self.frpc.stop_blocking(key)
                config_path.unlink(missing_ok=True)
            if not tunnels:
                return []
            if not lolia_api.is_logged_in():
                return [remote_failure_item("lolia", tunnels, t("save.delete_mapping_not_logged_in"))]
            try:
                existing = {item.get("name") for item in lolia_api.list_tunnels()}
            except (lolia_config.LoliaError, OSError) as exc:
                return [remote_failure_item("lolia", tunnels, exc)]
            failed, error = [], None
            for shard_name, name in tunnels.items():
                if name not in existing:
                    continue  # 已在控制台手动删掉
                try:
                    lolia_api.delete_tunnel(name)
                except (lolia_config.LoliaError, OSError) as exc:
                    failed.append(shard_name)
                    error = exc
            return [remote_failure_item("lolia", failed, error)] if failed else []

        return work
