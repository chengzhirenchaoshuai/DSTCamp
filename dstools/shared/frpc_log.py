"""把 frpc 的输出逐行记到 cache/frpc_log/（按存档路径哈希分文件），供日志包收录。

樱花、Lolia、自建三种穿透各自经 stdout 管道读 frpc 输出，界面只留最后几行；断线后追查要看断开那一刻
frpc 报了什么，所以同时落盘一份。同一存档的多个 frpc（每世界一个）写进同一个文件，行首标出来源。
frpc 自己的输出行已带时间，这里只给启动/退出两条标记行加时间。写失败静默忽略，不影响转发。
"""

from __future__ import annotations

import datetime as _dt
import threading
from pathlib import Path

from dstools.shared.resource_paths import cache_dir
from dstools.shared.server_ports import stable_path_key

_MAX_BYTES = 2 * 1024 * 1024  # 超过后只留末尾一半大小
_TRIM_CHECK_LINES = 500  # 每写这么多行检查一次大小
_lock = threading.Lock()


def frpc_log_path(cluster_path: Path) -> Path:
    return cache_dir("frpc_log") / f"{stable_path_key(Path(cluster_path))}.log"


def _trim(path: Path) -> None:
    try:
        if path.stat().st_size <= _MAX_BYTES:
            return
        data = path.read_bytes()
    except OSError:
        return
    kept = data[-(_MAX_BYTES // 2):]
    kept = kept.split(b"\n", 1)[-1]  # 截断处的半行丢掉
    try:
        path.write_bytes(kept)
    except OSError:
        pass


class FrpcOutputLog:
    """一个 frpc 进程的输出记录；``secrets`` 里的字符串（如樱花 Token）写入前替换掉。"""

    def __init__(self, cluster_path: Path, source: str, secrets: tuple[str, ...] = ()):
        self.path = frpc_log_path(cluster_path)
        self.source = source
        self._secrets = tuple(s for s in secrets if s)
        self._count = 0

    def _append(self, text: str) -> None:
        for secret in self._secrets:
            text = text.replace(secret, "<Token>")
        with _lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                    stream.write(text)
            except OSError:
                return
            self._count += 1
            if self._count % _TRIM_CHECK_LINES == 0:
                _trim(self.path)

    def _mark(self, message: str) -> None:
        stamp = _dt.datetime.now().isoformat(sep=" ", timespec="seconds")
        self._append(f"{stamp} ==== [{self.source}] {message}\n")

    def started(self) -> None:
        with _lock:
            _trim(self.path)
        self._mark("frpc 已启动")

    def line(self, text: str) -> None:
        self._append(f"[{self.source}] {text}\n")

    def exited(self, code: int | None) -> None:
        self._mark(f"frpc 已退出（退出码 {code}）")
