"""程序自身的崩溃记录：未捕获的 Python 异常与进程级崩溃（访问违例等）写进 data/crash_log/，日志包会收录。

窗口版 EXE 没有控制台，sys.stderr 为 None，原来出错只能看到窗口消失，事后无从查起。
- errors.log：sys.excepthook / threading.excepthook 收到的异常（含 Qt 槽函数里抛出的，PySide6 经 sys.excepthook 输出）。
- fault.log：faulthandler 在进程崩溃时写下的各线程调用栈。
两份各超过 _MAX_BYTES 时先改名为 .old 再重新写，只保留一份旧的。
"""

from __future__ import annotations

import datetime as _dt
import faulthandler
import sys
import threading
import traceback
from pathlib import Path

from dstools.shared.resource_paths import data_dir

_MAX_BYTES = 512 * 1024
_fault_file = None  # faulthandler 要求文件对象在进程存活期间一直打开
_installed = False


def crash_log_dir() -> Path:
    return data_dir("crash_log")


def _rotate(path: Path) -> None:
    try:
        if path.stat().st_size > _MAX_BYTES:
            path.replace(path.with_suffix(path.suffix + ".old"))
    except OSError:
        pass


def _write_exception(title: str, exc_type, exc, tb) -> None:
    from dstools import __version__

    path = crash_log_dir() / "errors.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate(path)
        stamp = _dt.datetime.now().isoformat(timespec="seconds")
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(f"===== {stamp} DSTCamp {__version__} {title}\n{text}\n")
    except Exception:  # noqa: BLE001 记录失败不能再抛，否则会盖掉原异常
        pass


def install() -> None:
    """启动时调用一次；重复调用无副作用。"""
    global _fault_file, _installed
    if _installed:
        return
    _installed = True
    previous_hook = sys.excepthook
    previous_thread_hook = threading.excepthook

    def excepthook(exc_type, exc, tb):
        _write_exception("未捕获异常", exc_type, exc, tb)
        previous_hook(exc_type, exc, tb)

    def thread_excepthook(args):
        if args.exc_type is not SystemExit:
            name = args.thread.name if args.thread is not None else "?"
            _write_exception(f"线程 {name} 未捕获异常", args.exc_type, args.exc_value, args.exc_traceback)
        previous_thread_hook(args)

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook
    path = crash_log_dir() / "fault.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate(path)
        _fault_file = path.open("a", encoding="utf-8")
        from dstools import __version__

        # 每次启动写一行抬头：崩溃时的调用栈接在所属那次运行的抬头后面
        _fault_file.write(f"===== {_dt.datetime.now().isoformat(timespec='seconds')} DSTCamp {__version__} 启动\n")
        _fault_file.flush()
        faulthandler.enable(file=_fault_file, all_threads=True)
    except OSError:
        _fault_file = None


def log_files() -> list[Path]:
    """日志包收录用：存在的崩溃记录（新的在前）。"""
    names = ("errors.log", "fault.log", "errors.log.old", "fault.log.old")
    return [p for p in (crash_log_dir() / n for n in names) if p.is_file() and p.stat().st_size > 0]
