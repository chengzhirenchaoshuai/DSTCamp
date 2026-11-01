"""把专服控制台里 server_log.txt 没有的行记到 cache/console_log/（按存档路径+世界哈希分文件），供日志包收录。

坑：LuaJIT 注入器的输出（[ds-shell]、[ds-bootstrap]、DynamicPluginLoader 等）走 stderr，
DSTCamp 把它并进管道才在控制台看到，游戏不写进 server_log.txt；启动即崩时它是唯一能看出
注入器死在哪一步的记录，崩溃后只留在内存里，关掉 DSTCamp 就没了（2026-11-01 实测）。
游戏日志行（"[00:00:00]: " 开头及其缩进续行）server_log.txt 里都有，不重复记。
每次启动把上一次的记录留作 .prev，自动重启后仍能看到崩溃那次。写失败静默忽略，不影响开服。
"""

from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path

from dstools.shared.resource_paths import cache_dir
from dstools.shared.server_ports import stable_path_key

_MAX_BYTES = 2 * 1024 * 1024  # 单次运行的上限，超过后不再记录
_GAME_LOG_RE = re.compile(r"^\[\d+:\d{2}:\d{2}\]: ")


def console_log_paths(cluster_path: Path, shard_name: str) -> tuple[Path, Path]:
    """（本次运行, 上一次运行）的记录文件路径。"""
    base = cache_dir("console_log") / stable_path_key(Path(cluster_path) / shard_name)
    return base.with_suffix(".log"), base.with_suffix(".prev.log")


def is_console_only(line: str) -> bool:
    """这行是否只在控制台出现（不是游戏日志行或其缩进续行）。"""
    return bool(line.strip()) and not _GAME_LOG_RE.match(line) and not line[:1].isspace()


class ConsoleOnlyLog:
    """一次专服运行的控制台独有输出。"""

    def __init__(self, cluster_path: Path, shard_name: str, launch_dir: Path):
        self.path, prev = console_log_paths(cluster_path, shard_name)
        self._size = 0
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists():
                self.path.replace(prev)
        except OSError:
            pass
        self._mark(f"启动 {shard_name}，启动目录：{launch_dir}")

    def _write(self, text: str) -> None:
        if self._size > _MAX_BYTES:
            return
        try:
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
        except OSError:
            return
        self._size += len(text.encode("utf-8"))

    def _mark(self, message: str) -> None:
        stamp = _dt.datetime.now().isoformat(sep=" ", timespec="seconds")
        self._write(f"{stamp} ==== {message}\n")

    def line(self, text: str) -> None:
        if is_console_only(text):
            self._write(text + "\n")

    def exited(self, code: int | None) -> None:
        detail = "未知" if code is None else f"{code}（0x{code & 0xFFFFFFFF:08X}）"
        self._mark(f"进程输出结束，退出码 {detail}")
