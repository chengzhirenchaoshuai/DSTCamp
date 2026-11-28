"""按需工具（shared/tool_store.py）的下载界面：日志窗口里显示进度，成功后自动关窗并继续原来的操作。"""

from __future__ import annotations

import webbrowser
from pathlib import Path
from typing import Callable

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QFileDialog

from dstools.i18n import t
from dstools.qt import dialogs
from dstools.qt.threads import post_to_ui, run_async
from dstools.shared import tool_store


def open_tools_package_link(parent) -> None:
    """打开网盘工具包的蓝奏云分享页；先复制密码再跳转，免得用户在蓝奏页和程序之间来回找。"""
    QGuiApplication.clipboard().setText(tool_store.TOOLS_LANZOU_PASSWORD)
    dialogs.show_toast(parent, t("tools.lanzou_password_copied"))
    webbrowser.open(tool_store.TOOLS_LANZOU_URL)


def download_tool_with_dialog(parent, relative: str, title: str, on_done: Callable[[Path], None]) -> None:
    """后台下载 relative 登记的工具（失败时 tool_store 会先试「下载」文件夹里的工具包）。

    仍失败时窗口留着，并询问：去网盘下载工具包，或直接在这里导入已下载的工具包；导入成功就接着调用 on_done，
    用户不用再去「关于 → 手动更新」找入口。"""
    tool = tool_store.store_tool(relative)
    dialog = dialogs.LogDialog(parent, title)
    dialog.append(t("tools.download_start", name=tool.asset, size=f"{tool.size / 1e6:.1f}"), "section")
    dialog.show()
    shown = [0]

    def progress(done: int, total: int) -> None:
        percent = done * 100 // max(1, total)
        if percent >= shown[0] + 20:
            shown[0] = percent - percent % 20
            post_to_ui(lambda value: dialog.append(t("tools.download_progress", percent=value), "detail"), shown[0])

    def imported_from(package: Path) -> None:
        post_to_ui(lambda _a: dialog.append(t("tools.imported_from", path=package), "detail"))

    def finished(path: Path) -> None:
        dialog.finish()
        dialog.accept()
        on_done(path)

    def offer_package(text: str, default: str) -> None:
        """循环询问，直到导入成功或用户关闭：去网盘下载后还会再问一次，好让用户下完直接导入。"""
        choice = dialogs.ask_choice(dialog, title, text, [
            (t("dlg.close_btn"), "cancel"),
            (t("tools.open_lanzou_btn"), "lanzou"),
            (t("tools.import_btn"), "import"),
        ], default=default)
        if choice == "lanzou":
            open_tools_package_link(dialog)
            offer_package(t("tools.after_lanzou_hint", package=tool_store.TOOLS_PACKAGE_NAME), "import")
        elif choice == "import":
            import_package()

    def import_package() -> None:
        picked, _filter = QFileDialog.getOpenFileName(
            dialog, t("tools.import_pick"), str(tool_store.downloads_dir()), "ZIP (*.zip)")
        if not picked:
            offer_package(t("tools.after_lanzou_hint", package=tool_store.TOOLS_PACKAGE_NAME), "import")
            return
        package = Path(picked)

        def work() -> Path | None:
            tool_store.import_tools_package(package)
            return tool_store.find_stored(relative)

        def done(path: Path | None) -> None:
            if path is not None:
                dialog.append(t("tools.imported_from", path=package), "result_success")
                finished(path)
            elif dialog.isVisible():  # 导入期间用户已关窗就不再追问
                offer_package(t("tools.import_missing", name=tool.asset,
                                package=tool_store.TOOLS_PACKAGE_NAME), "import")

        def error(exc: Exception) -> None:
            if dialog.isVisible():
                offer_package(t("tools.import_failed", error=exc), "import")

        run_async(work, done, error)

    def failed(exc: Exception) -> None:
        dialog.append(t("tools.download_failed", error=exc), "result_error")
        dialog.finish()
        offer_package(t("tools.download_failed_choice", package=tool_store.TOOLS_PACKAGE_NAME,
                        password=tool_store.TOOLS_LANZOU_PASSWORD), "lanzou")

    run_async(lambda: tool_store.download_tool(relative, progress, imported_from), finished, failed)
