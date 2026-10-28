"""Qt 版入口：``python -m dstools.qt.app``。"""

import os
import sys

# 按保存的字体样式选字体引擎：像素字体在 DirectWrite 下关抗锯齿会亚像素粘连，只有 FreeType 能像素完美；
# 但 FreeType 会让雅黑、麦圆体失去 ClearType 而发细，所以只在像素字体下启用。引擎须在 QApplication
# 构造前选定，运行中切换要重启（见 theme.freetype_engine_active()）。setdefault 不覆盖显式指定的 QT_QPA_PLATFORM。
from dstools.shared.app_settings import get_font_style_choice  # noqa: E402  纯 Python，不依赖 Qt

if get_font_style_choice() == "pixel":
    os.environ.setdefault("QT_QPA_PLATFORM", "windows:fontengine=freetype")

from PySide6.QtCore import QLoggingCategory, QTimer
from PySide6.QtWidgets import QApplication

from dstools.qt.context import AppContext
from dstools.qt.theme import theme
from dstools.qt.window import MainWindow


def create_window(app: QApplication) -> MainWindow:
    theme.load_fonts()
    theme.apply_to_app()
    return MainWindow(AppContext())


def _application() -> QApplication:
    """取得（必要时创建）QApplication；启动前的确认弹窗和主窗口共用同一个。"""
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
        app.setQuitOnLastWindowClosed(False)  # 托盘常驻；也避免确认弹窗关闭时误触发退出
    return app


def notify_old_instance_busy(server_count: int) -> None:
    """更早的 Tk 版旧实例还在跑专服、无法替用户安全关闭时，提示先手动退出它。"""
    from dstools.i18n import t
    from dstools.qt import dialogs

    _application()
    theme.load_fonts()
    theme.apply_to_app()
    dialogs.show_info(None, t("app.old_instance_title"), t("app.old_instance_msg", count=server_count))


def main() -> int:
    # 清理上次自动更新留下的临时文件、旧版 EXE 备份等（尽力而为，失败不影响启动）
    from dstools.shared.auto_update import (
        cleanup_stale_update_artifacts,
        cleanup_vestigial_external_tools,
        clear_download_mark,
        take_update_failure_log,
    )

    from dstools.features.mod.workshop_api import cleanup_stale_worker_dirs

    # 上次自动更新失败时助手已回滚并重启了本程序，先取出失败原因再清理残留
    update_failure = take_update_failure_log()
    clear_download_mark()
    cleanup_stale_update_artifacts()
    cleanup_vestigial_external_tools()
    cleanup_stale_worker_dirs()
    # 开服场景的老用户迁移要在建窗口（会写设置）之前做，靠设置文件是否为空区分新老用户
    from dstools.features.local_service.usage_scenario import migrate_usage_scenario
    migrate_usage_scenario()
    # Qt 枚举系统字体时，Fixedsys/Terminal 等老式位图字体 DirectWrite 不支持，会刷一串
    # "CreateFontFaceFromHDC() failed" 警告（从终端启动时可见）。不影响任何显示，屏蔽这一类。
    QLoggingCategory.setFilterRules("qt.qpa.fonts.warning=false")
    app = _application()
    window = create_window(app)
    window.show()
    if update_failure:
        from dstools.i18n import t
        from dstools.qt import dialogs

        QTimer.singleShot(0, lambda: dialogs.show_error(
            window, t("update.title"), t("update.apply_failed", error=update_failure)))
    window.start_update_check()
    # 延迟到主窗口渲染稳定后再弹开服场景选择、缓存目录警告，避免弹窗抢在窗口还没显示完整前出现
    QTimer.singleShot(500, window.run_startup_checks)
    # Mod 配置记忆与游戏客户端同步：启动时静默跑一次（结果写 sync.log），打开 Mod 页时再跑并提示
    from dstools.qt.mod.mod_config_sync import start_sync, sync_if_account_changed
    QTimer.singleShot(2000, lambda: start_sync(window.ctx))
    # 后台预热 Mod 缓存（图标、版本），新用户首次打开 Mod 页不再等几十秒；排在缓存目录检查和配置同步之后。
    # 轻量模式不预热：云服务器多半只开服，预热要起沙箱子进程，正好跟开服抢 CPU 和磁盘；打开 Mod 页时照常加载
    from dstools.qt.mod import mod_preload
    from dstools.shared.app_settings import get_lite_mode_enabled
    if not get_lite_mode_enabled():
        QTimer.singleShot(3000, lambda: mod_preload.start(window.ctx))
    # 存档栏切换游戏账号后对新账号补同步一次
    window.ctx.env_changed.connect(lambda: sync_if_account_changed(window.ctx))
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
