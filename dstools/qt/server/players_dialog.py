"""控制台"在线玩家"弹窗：列出存档各世界的在线玩家（带角色头像），可复活鬼魂、杀死玩家。

查询方式：给每个已就绪的世界发一条带随机标记的 Lua 命令，轮询该进程最近的日志行，读到结束标记即完成
（命令与解析见 features/local_service/player_commands.py）。命令按玩家所在世界发送：每个世界只认得自己的玩家。
头像与存档信息页同源（character_icons.resolve_character，含 Mod 角色），首次要转换贴图，放后台读取。
"""

import secrets
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QGraphicsOpacityEffect, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from dstools.features.local_service import player_commands as pc
from dstools.features.local_service.dedicated_server import ServerStatus
from dstools.features.save_browser.character_names import CHARACTER_NAMES, get_character_display_name
from dstools.i18n import get_lang, t
from dstools.qt import dialogs
from dstools.qt.theme import theme
from dstools.qt.threads import run_async
from dstools.qt.widgets import Card, clear_layout

_POLL_MS = 200
_TIMEOUT_MS = 5000
_REFRESH_AFTER_ACTION_MS = 1500  # 复活有动画、状态切换要一会儿，稍等再刷新
_AVATAR_SIZE = 56
_DEAD_AVATAR_OPACITY = 0.4  # 鬼魂、尸体的头像调暗，一眼看出谁死了

_STATE_TEXT_KEYS = {
    pc.PLAYER_ALIVE: "local.players_state_alive",
    pc.PLAYER_GHOST: "local.players_state_ghost",
    pc.PLAYER_CORPSE: "local.players_state_corpse",
    pc.PLAYER_DEAD: "local.players_state_dead",
}


def _ready(proc) -> bool:
    return proc is not None and proc.status == ServerStatus.RUNNING and proc.world_ready


def _label(text: str, size_key: str = "FONT_SIZE_SM", bold: bool = False, muted: bool = False) -> QLabel:
    label = QLabel(text)
    label.setFont(theme.font(size_key, bold=bold))
    label.setProperty("muted", muted)
    return label


class PlayersDialog(dialogs.Dialog):
    def __init__(self, parent, procs_fn):
        """procs_fn() 返回这个存档各世界当前的进程（按世界顺序），每次刷新时重新取。"""
        super().__init__(parent, t("local.players_title"), "md")
        self._procs_fn = procs_fn
        self._pending: dict[str, tuple[object, str]] = {}  # 世界名 -> (进程, 本次查询标记)
        self._results: dict[str, tuple[object, list[pc.OnlinePlayer]]] = {}
        self._elapsed_ms = 0
        # prefab -> (角色名, 头像路径)；跨刷新复用。正在后台解析的 prefab 记在 _resolving
        self._characters: dict[str, tuple[str, Path | None]] = {}
        self._resolving: set[str] = set()
        self._avatar_labels: dict[str, list[tuple[QLabel, QLabel]]] = {}  # prefab -> [(头像, 角色名)]
        self._closed = False

        self._status = self.text_label("", muted=True)
        self.body.addWidget(self._status)
        area = QScrollArea()
        area.setObjectName("playersArea")
        area.setWidgetResizable(True)
        area.setMinimumHeight(320)
        inner = QWidget()
        inner.setObjectName("playersInner")
        # 滚动区和内容显式透明，否则视口露出系统调色板的纯灰底
        area.viewport().setAutoFillBackground(False)
        area.setStyleSheet("#playersArea, #playersInner { background: transparent; border: none; }")
        self._rows = QVBoxLayout(inner)
        self._rows.setContentsMargins(0, 0, 0, 0)
        self._rows.setSpacing(8)
        self._rows.addStretch()
        area.setWidget(inner)
        self.body.addWidget(area, 1)

        self._refresh_btn = dialogs.style_button(QPushButton(t("local.players_refresh_btn")), "secondary")
        self._refresh_btn.clicked.connect(self.refresh)
        self._revive_all_btn = dialogs.style_button(QPushButton(t("local.players_revive_all_btn")), "secondary")
        self._revive_all_btn.clicked.connect(self._revive_all)
        close_btn = QPushButton(t("dlg.close_btn"))
        close_btn.clicked.connect(self.reject)
        self.add_footer([self._refresh_btn, self._revive_all_btn], [close_btn])

        self._timer = QTimer(self, interval=_POLL_MS)
        self._timer.timeout.connect(self._poll)
        self._refresh_later = QTimer(self, singleShot=True, interval=_REFRESH_AFTER_ACTION_MS)
        self._refresh_later.timeout.connect(self.refresh)
        self.refresh()

    # ── 查询 ────────────────────────────────────────────────────────────
    def refresh(self) -> None:
        self._pending.clear()
        self._results.clear()
        self._elapsed_ms = 0
        for proc in self._procs_fn():
            if not _ready(proc):
                continue
            nonce = secrets.token_hex(4)
            if proc.send_command(pc.list_players_command(nonce)):
                self._pending[proc.shard_name] = (proc, nonce)
        if not self._pending:
            self._show_players(t("local.players_no_world"))
            return
        self._refresh_btn.setEnabled(False)
        self._set_revive_all(False, t("local.players_revive_all_loading"))
        self._status.setText(t("local.players_loading"))
        self._timer.start()

    def _poll(self) -> None:
        self._elapsed_ms += _POLL_MS
        for shard_name, (proc, nonce) in list(self._pending.items()):
            try:
                lines = proc.recent_log_lines
            except RuntimeError:  # 读线程正在追加，下一轮再读
                continue
            players, done = pc.parse_player_lines(lines, nonce)
            if done:
                self._results[shard_name] = (proc, players)
                del self._pending[shard_name]
        if self._pending and self._elapsed_ms < _TIMEOUT_MS:
            return
        self._timer.stop()
        self._refresh_btn.setEnabled(True)
        missing = list(self._pending)
        self._pending.clear()
        self._show_players(t("local.players_timeout", shards=t("local.players_shard_sep").join(missing))
                           if missing else "")

    # ── 显示 ────────────────────────────────────────────────────────────
    def _show_players(self, note: str) -> None:
        clear_layout(self._rows)
        self._avatar_labels.clear()
        entries = [(shard_name, proc, player) for shard_name, (proc, players) in self._results.items()
                   for player in players]
        for shard_name, proc, player in entries:
            self._rows.addWidget(self._player_row(shard_name, proc, player))
        if self._results and not entries:
            empty = _label(t("local.players_empty"), size_key="FONT_SIZE_BASE", muted=True)
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setContentsMargins(0, 24, 0, 24)
            self._rows.addWidget(empty)
        self._rows.addStretch()
        self._update_revive_all(entries)
        self._status.setText(note or (t("local.players_count", count=len(entries)) if entries else ""))
        self._resolve_avatars({(player.prefab, proc) for _shard, proc, player in entries})

    def _update_revive_all(self, entries) -> None:
        if not self._results:
            self._set_revive_all(False, t("local.players_revive_all_no_world"))
        elif not entries:
            self._set_revive_all(False, t("local.players_revive_all_no_players"))
        elif not any(player.state in pc.REVIVABLE_STATES for _shard, _proc, player in entries):
            self._set_revive_all(False, t("local.players_revive_all_none_dead"))
        else:
            self._set_revive_all(True, t("local.players_revive_all_hover"))

    def _set_revive_all(self, enabled: bool, tooltip: str) -> None:
        self._revive_all_btn.setEnabled(enabled)
        self._revive_all_btn.setToolTip(tooltip)

    def _player_row(self, shard_name: str, proc, player: pc.OnlinePlayer) -> QWidget:
        row = Card(radius=14, alpha=128, fill_key="CARD_BG_ALT")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(10, 8, 12, 8)
        layout.setSpacing(12)
        alive = player.state == pc.PLAYER_ALIVE

        avatar = QLabel()
        avatar.setFixedSize(_AVATAR_SIZE, _AVATAR_SIZE)
        avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if not alive:
            effect = QGraphicsOpacityEffect(avatar)
            effect.setOpacity(_DEAD_AVATAR_OPACITY)
            avatar.setGraphicsEffect(effect)
        layout.addWidget(avatar)

        body = QVBoxLayout()
        body.setSpacing(3)
        name = _label(player.name or player.userid, size_key="FONT_SIZE_BASE", bold=True)
        name.setToolTip(player.userid)
        body.addWidget(name)
        detail = QHBoxLayout()
        detail.setSpacing(8)
        character = _label(self._character_name(player.prefab), muted=True)
        detail.addWidget(character)
        detail.addWidget(_label("·", muted=True))
        detail.addWidget(_label(shard_name, muted=True))
        detail.addWidget(_label("·", muted=True))
        state_key = _STATE_TEXT_KEYS.get(player.state)
        state = _label(t(state_key) if state_key else player.state, bold=True)
        state.setStyleSheet(f"color: {theme.hex('SUCCESS' if alive else 'ERROR')};")
        detail.addWidget(state)
        detail.addStretch()
        body.addLayout(detail)
        layout.addLayout(body, 1)
        self._avatar_labels.setdefault(player.prefab, []).append((avatar, character))
        self._apply_character(player.prefab)

        revivable = player.state in pc.REVIVABLE_STATES
        revive_btn = dialogs.style_button(QPushButton(t("local.players_revive_btn")), "secondary")
        revive_btn.setEnabled(revivable)
        revive_btn.setToolTip("" if revivable else t("local.players_revive_disabled"))
        revive_btn.clicked.connect(lambda: self._act(proc, pc.revive_command(player.userid)))
        kill_btn = dialogs.style_button(QPushButton(t("local.players_kill_btn")), "danger")
        kill_btn.setEnabled(alive)
        kill_btn.setToolTip("" if alive else t("local.players_kill_disabled"))
        kill_btn.clicked.connect(lambda: self._kill(proc, player))
        layout.addWidget(revive_btn)
        layout.addWidget(kill_btn)
        return row

    # ── 角色名与头像 ────────────────────────────────────────────────────
    def _character_name(self, prefab: str) -> str:
        # 官方角色按界面语言取名；Mod 角色用解析出的名字（解析前先显示 prefab）
        if prefab in CHARACTER_NAMES:
            return get_character_display_name(prefab, get_lang())
        cached = self._characters.get(prefab)
        return cached[0] if cached else prefab

    def _resolve_avatars(self, wanted) -> None:
        todo = {}
        for prefab, proc in wanted:
            if prefab not in self._characters and prefab not in self._resolving and prefab not in todo:
                todo[prefab] = Path(proc.cluster_path) / proc.shard_name / "modoverrides.lua"
        if not todo:
            return
        self._resolving.update(todo)

        def work():
            from dstools.features.save_browser.character_icons import resolve_character

            result = {}
            for prefab, overrides_path in todo.items():
                try:
                    result[prefab] = resolve_character(prefab, overrides_path)
                except Exception:  # 头像只是点缀，读不到就留空
                    result[prefab] = (prefab, None)
            return result

        def done(result) -> None:
            self._resolving.difference_update(todo)
            self._characters.update(result)
            if not self._closed:
                for prefab in result:
                    self._apply_character(prefab)

        def failed(_exc) -> None:
            self._resolving.difference_update(todo)

        run_async(work, done, failed)

    def _apply_character(self, prefab: str) -> None:
        cached = self._characters.get(prefab)
        if cached is None:
            return
        name, icon_path = cached
        pixmap = self._avatar_pixmap(icon_path)
        for avatar, character in self._avatar_labels.get(prefab, ()):
            character.setText(self._character_name(prefab) if prefab in CHARACTER_NAMES else name)
            if pixmap is not None:
                avatar.setPixmap(pixmap)

    def _avatar_pixmap(self, icon_path: Path | None) -> QPixmap | None:
        if icon_path is None:
            return None
        pixmap = QPixmap(str(icon_path))
        if pixmap.isNull():
            return None
        # 按物理像素缩放并设置像素比，避免 Qt 二次放大（与存档信息页一致）
        dpr = self.devicePixelRatioF()
        box = round(_AVATAR_SIZE * dpr)
        if pixmap.width() > box or pixmap.height() > box:
            pixmap = pixmap.scaled(box, box, Qt.AspectRatioMode.KeepAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation)
        pixmap.setDevicePixelRatio(dpr)
        return pixmap

    # ── 操作 ────────────────────────────────────────────────────────────
    def _kill(self, proc, player: pc.OnlinePlayer) -> None:
        if not dialogs.ask_yes_no(self, t("local.players_kill_btn"),
                                  t("local.players_kill_confirm", name=player.name), danger=True):
            return
        self._act(proc, pc.kill_command(player.userid))

    def _revive_all(self) -> None:
        procs = [proc for proc, players in self._results.values()
                 if any(p.state in pc.REVIVABLE_STATES for p in players)]
        self._send_then_refresh([(proc, pc.revive_all_command()) for proc in procs])

    def _act(self, proc, command: str) -> None:
        self._send_then_refresh([(proc, command)])

    def _send_then_refresh(self, commands) -> None:
        sent = [proc.send_command(command) for proc, command in commands if _ready(proc)]
        if not sent or not all(sent):
            self._status.setText(t("local.players_send_failed"))
            return
        self._status.setText(t("local.players_sent"))
        self._refresh_btn.setEnabled(False)
        self._set_revive_all(False, t("local.players_revive_all_loading"))
        self._refresh_later.start()

    def done(self, result: int) -> None:
        # 关闭后不再轮询、不再发查询命令，后台头像结果也不再写回控件
        self._closed = True
        self._timer.stop()
        self._refresh_later.stop()
        super().done(result)
