"""SakuraFrp 的 frpc.exe 进程生命周期管理（长驻子进程，stdout 管道轮询；无优雅关闭指令，停止即 terminate → kill）。"""

import queue
import subprocess
import sys
import threading
import time
from enum import Enum
from pathlib import Path

from dstools.shared.frpc_log import FrpcOutputLog


IS_WINDOWS = sys.platform == "win32"


class FrpcStatus(Enum):
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    CRASHED = "crashed"


class FrpcProcess:
    """一个 (存档, 世界) 的 frpc.exe 子进程，用 ``-f <Token>:<隧道ID>`` 启动，由 frpc 向樱花拉取配置。

    坑：只有樱花后台单独下载的独立版 frpc 能这样运行；启动器安装包里的 frpc.exe 只认 SakuraFrpService，
    直接运行会提示 "is not intended to be run directly"。
    """

    def __init__(self, cluster_path: Path, shard_name: str, frpc_exe: Path, token: str, tunnel_id: int):
        self.cluster_path = cluster_path
        self.shard_name = shard_name
        self.frpc_exe = frpc_exe
        self.token = token
        self.tunnel_id = tunnel_id
        self.status = FrpcStatus.STARTING
        self.proc: subprocess.Popen | None = None
        self.error: str | None = None
        self._out_queue: "queue.Queue[str]" = queue.Queue()
        # 输出同时落盘供日志包收录；Token 在命令行里，写入前替换掉
        self._log = FrpcOutputLog(cluster_path, f"樱花 {shard_name}", secrets=(token,))

    def start(self) -> None:
        # 被杀软隔离/删除时 Popen 抛 FileNotFoundError，提前检查并记录可读的失败原因
        if not self.frpc_exe.exists():
            self.status = FrpcStatus.CRASHED
            self.error = "frpc.exe 不存在（可能被杀毒软件隔离或已手动删除）"
            return
        try:
            creationflags = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0
            self.proc = subprocess.Popen(
                [str(self.frpc_exe), "-f", f"{self.token}:{self.tunnel_id}"],
                cwd=str(self.frpc_exe.parent),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                creationflags=creationflags,
            )
        except OSError as exc:
            self.status = FrpcStatus.CRASHED
            self.error = f"启动失败：{exc}"
            return
        self.status = FrpcStatus.RUNNING
        self._log.started()
        threading.Thread(target=self._read_loop, daemon=True).start()

    def _read_loop(self) -> None:
        try:
            for line in self.proc.stdout:
                line = line.rstrip("\n")
                self._out_queue.put(line)
                self._log.line(line)
        except (OSError, ValueError):
            pass
        try:
            code = self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            code = None
        self._log.exited(code)

    def read_available_lines(self) -> list[str]:
        lines = []
        while True:
            try:
                lines.append(self._out_queue.get_nowait())
            except queue.Empty:
                break
        return lines

    def poll_exit_code(self) -> int | None:
        return self.proc.poll() if self.proc else None

    def terminate(self) -> None:
        if self.proc:
            try:
                self.proc.terminate()
            except OSError:
                pass

    def kill(self) -> None:
        if self.proc:
            try:
                self.proc.kill()
            except OSError:
                pass

    def stop_blocking(self, term_timeout: float = 5.0) -> None:
        """terminate → kill，阻塞到进程退出，须在后台线程调用。"""
        self.status = FrpcStatus.STOPPING
        self.terminate()
        deadline = time.monotonic() + term_timeout
        while time.monotonic() < deadline:
            if self.poll_exit_code() is not None:
                self.status = FrpcStatus.STOPPED
                return
            time.sleep(0.2)
        self.kill()
        self.status = FrpcStatus.STOPPED


class FrpcManager:
    """管理本进程启动的 frpc，key 为 (存档路径, 世界名)；stop() 回调在后台线程触发，操作界面需转回界面线程。"""

    def __init__(self):
        self._procs: dict[tuple[str, str], FrpcProcess] = {}

    @staticmethod
    def _key(cluster_path: Path, shard_name: str) -> tuple[str, str]:
        return (str(cluster_path), shard_name)

    def start(self, cluster_path: Path, shard_name: str, frpc_exe: Path, token: str, tunnel_id: int) -> FrpcProcess:
        proc = FrpcProcess(cluster_path, shard_name, frpc_exe, token, tunnel_id)
        proc.start()
        self._procs[self._key(cluster_path, shard_name)] = proc
        return proc

    def get(self, cluster_path: Path, shard_name: str) -> FrpcProcess | None:
        return self._procs.get(self._key(cluster_path, shard_name))

    def processes(self) -> list[FrpcProcess]:
        return list(self._procs.values())

    def stop(self, cluster_path: Path, shard_name: str, on_done=None) -> None:
        key = self._key(cluster_path, shard_name)
        proc = self._procs.get(key)
        if not proc:
            return

        def _worker():
            proc.stop_blocking()
            self._procs.pop(key, None)
            if on_done:
                on_done(proc)

        threading.Thread(target=_worker, daemon=True).start()
