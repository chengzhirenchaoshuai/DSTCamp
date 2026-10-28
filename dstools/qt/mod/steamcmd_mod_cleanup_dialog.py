"""SteamCMD 通道「清理未用 Mod」：列出 SteamCMD 工坊目录里没有任何服务器存档在用的 Mod 和下载残留，
勾选后删除。默认一个都不勾，删除前再确认一次；SteamCMD 正在运行时不删。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QListWidget, QListWidgetItem, QPushButton

from dstools.features.local_service import luajit_injector, steamcmd_updater
from dstools.features.local_service.server_runtime import current_runtime
from dstools.features.mod import steamcmd_mod_cleanup
from dstools.i18n import t
from dstools.qt import dialogs
from dstools.qt.theme import theme
from dstools.qt.threads import run_async


def _format_size(num_bytes: int) -> str:
    if num_bytes >= 1024 ** 3:
        return f"{num_bytes / 1024 ** 3:.2f} GB"
    return f"{num_bytes / 1024 ** 2:.1f} MB"


def _scan(clusters):
    """后台：统计未用 Mod 与下载残留；读不出名字的再向 Steam 公开接口查标题（查不到不影响清理）。"""
    root = steamcmd_updater.steamcmd_workshop_dir()
    used = steamcmd_mod_cleanup.used_workshop_ids(clusters)
    # LuaJIT 注入的 Injector.dll 就在配套 Mod 目录里，已安装时即使没有存档列着它也不能删
    runtime = current_runtime()
    if runtime is not None and luajit_injector.read_injector_path_marker(runtime.install_dir) is not None:
        used.add(luajit_injector.WORKSHOP_ID)
    unused = steamcmd_mod_cleanup.find_unused_mods(root, used)
    leftovers = steamcmd_mod_cleanup.find_leftover_downloads(root)
    titles = {}
    missing = [e.workshop_id for e in unused if not e.name]
    if missing:
        from dstools.features.mod.workshop_webapi import fetch_remote_items
        try:
            titles = {wid: item.title for wid, item in fetch_remote_items(missing).items()}
        except OSError:
            pass
    return root, unused, leftovers, titles


class SteamcmdModCleanupDialog(dialogs.Dialog):
    def __init__(self, parent, clusters):
        super().__init__(parent, t("steamcmd_cleanup.title"), "md", confirm_text=t("steamcmd_cleanup.delete_btn"))
        self._root = None
        self.body.addWidget(self.text_label(t("steamcmd_cleanup.desc"), muted=True))
        self._list = QListWidget()
        self._list.setFont(theme.font("FONT_SIZE_SM"))
        self._list.itemChanged.connect(self._refresh_total)
        self.body.addWidget(self._list, 1)
        row = QHBoxLayout()
        self._all_btn = QPushButton(t("steamcmd_cleanup.select_all"))
        self._none_btn = QPushButton(t("steamcmd_cleanup.select_none"))
        for button, state in ((self._all_btn, Qt.CheckState.Checked), (self._none_btn, Qt.CheckState.Unchecked)):
            button.setFont(theme.font("FONT_SIZE_SM"))
            button.clicked.connect(lambda _checked=False, s=state: self._set_all(s))
            row.addWidget(button)
        row.addStretch()
        self._total_label = self.text_label(t("steamcmd_cleanup.scanning"), muted=True, wrap=False)
        row.addWidget(self._total_label)
        # 只留「删除」，和全选按钮同一行；关闭用窗口右上角的关闭按钮
        self._confirm = QPushButton(self._confirm_text)
        self._confirm.clicked.connect(self.accept_if_valid)
        self._confirm.setDefault(True)
        row.addSpacing(8)
        row.addWidget(self._confirm)
        self.body.addLayout(row)
        self._set_ready(False)
        dialogs.fit_to_screen(self, dialogs.DIALOG_WIDTHS["md"], 520)
        run_async(lambda: _scan(clusters), self._on_scanned, self._on_scan_failed)

    def _set_ready(self, ready: bool) -> None:
        for widget in (self._list, self._all_btn, self._none_btn):
            widget.setEnabled(ready)
        self._confirm.setEnabled(ready and bool(self._checked_items()))

    def _on_scanned(self, result) -> None:
        self._root, unused, leftovers, titles = result
        self._list.blockSignals(True)
        for entry in unused:
            name = entry.name or titles.get(entry.workshop_id, "")
            label = f"workshop-{entry.workshop_id}" + (f"（{name}）" if name else "")
            self._add_item(f"{label}    {_format_size(entry.size)}", entry)
        for entry in leftovers:
            self._add_item(t("steamcmd_cleanup.leftover_item", id=entry.workshop_id, folder=entry.path.parent.parent.name,
                             size=_format_size(entry.size)), entry)
        self._list.blockSignals(False)
        if not unused and not leftovers:
            self._total_label.setText(t("steamcmd_cleanup.nothing"))
            return
        self._set_ready(True)
        self._refresh_total()

    def _on_scan_failed(self, exc: Exception) -> None:
        self._total_label.setText(t("steamcmd_cleanup.scan_failed", detail=str(exc)))

    def _add_item(self, text: str, entry) -> None:
        item = QListWidgetItem(text)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Unchecked)  # 默认不勾，由用户自己挑
        item.setData(Qt.ItemDataRole.UserRole, entry)
        item.setToolTip(str(entry.path))
        self._list.addItem(item)

    def _checked_items(self) -> list:
        return [self._list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self._list.count())
                if self._list.item(i).checkState() == Qt.CheckState.Checked]

    def _set_all(self, state) -> None:
        self._list.blockSignals(True)
        for i in range(self._list.count()):
            self._list.item(i).setCheckState(state)
        self._list.blockSignals(False)
        self._refresh_total()

    def _refresh_total(self, _item=None) -> None:
        checked = self._checked_items()
        self._total_label.setText(t("steamcmd_cleanup.total", count=len(checked),
                                    size=_format_size(sum(e.size for e in checked))))
        self._confirm.setEnabled(bool(checked))

    def accept_if_valid(self) -> None:
        checked = self._checked_items()
        if not checked or not dialogs.ask_yes_no(
                self, t("steamcmd_cleanup.title"),
                t("steamcmd_cleanup.confirm", count=len(checked), size=_format_size(sum(e.size for e in checked))),
                danger=True):
            return
        self._set_ready(False)
        self._total_label.setText(t("steamcmd_cleanup.deleting"))
        root = self._root
        run_async(lambda: steamcmd_updater.run_when_idle(
            steamcmd_mod_cleanup.delete_mod_dirs, [e.path for e in checked], root),
            self._on_deleted, self._on_delete_failed)

    def _on_deleted(self, failures) -> None:
        if failures:
            dialogs.show_warning(self, t("steamcmd_cleanup.title"), t(
                "steamcmd_cleanup.partial_failed",
                items="\n".join(f"{path.name}：{reason}" for path, reason in failures)))
        self.accept()

    def _on_delete_failed(self, exc: Exception) -> None:
        key = ("steamcmd_cleanup.busy" if isinstance(exc, steamcmd_updater.SteamcmdBusyError)
               else "steamcmd_cleanup.delete_failed")
        dialogs.show_warning(self, t("steamcmd_cleanup.title"), t(key, detail=str(exc)))
        self._set_ready(True)
        self._refresh_total()
