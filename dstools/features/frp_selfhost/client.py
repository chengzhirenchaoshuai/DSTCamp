"""连接自建 frps 的本地 frpc.exe：配置生成与进程生命周期。

一个存档一个进程，用 ``-c <配置文件>`` 启动，配置包含存档内所有已映射世界。

孤儿进程认领：DSTCamp 未经"停止"退出时 frpc 会继续转发，重启后界面却显示未启动。
``FrpcManager.reconcile()`` 用 tasklist 找候选 PID，再用 PowerShell Get-CimInstance 读命令行按配置
文件路径精确认领（自己启动的进程，同一用户下能读到命令行）。
"""

import csv
import queue
import subprocess
import sys
import threading
import time
from enum import Enum
from pathlib import Path

from dstools.shared.frpc_log import FrpcOutputLog

IS_WINDOWS = sys.platform == "win32"
_SUBPROCESS_FLAGS = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0
_SECURITY_BLOCK_WINERROR = 225  # "操作无法成功完成，因为文件包含病毒或潜在的垃圾软件"


def _pid_exists(pid: int) -> bool:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10, creationflags=_SUBPROCESS_FLAGS,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return bool(out.strip())


def _kill_pid(pid: int) -> None:
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True,
                        timeout=10, creationflags=_SUBPROCESS_FLAGS)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _query_command_line(pid: int) -> str:
    """读一个指定 PID 的完整命令行，查不到（进程已退出/权限不够）返回
    空字符串，调用方按"匹配不上"处理，不当异常抛出。"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\").CommandLine"],
            capture_output=True, text=True, timeout=10, creationflags=_SUBPROCESS_FLAGS,
        )
        return out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _find_frpc_pid_by_config(config_path: Path) -> int | None:
    """扫描系统里所有 frpc.exe 进程，用命令行里 `-c <配置文件路径>` 精
    确匹配出属于这个存档的那一个孤儿进程。"""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq frpc.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10, creationflags=_SUBPROCESS_FLAGS,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    target = str(config_path).lower()
    for row in csv.reader(out.splitlines()):
        if len(row) < 2:
            continue
        try:
            pid = int(row[1])
        except ValueError:
            continue
        if target in _query_command_line(pid).lower():
            return pid
    return None


class FrpcStatus(Enum):
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    CRASHED = "crashed"


def build_frpc_toml(server_host: str, server_port: int, token: str, proxies: list[dict]) -> str:
    """生成 frpc.toml；proxies 为 [{"name", "type", "local_port", "remote_port"}]，每世界一条。
    server_host 由用户填写，只转义双引号/反斜杠防止破坏 TOML，填错由 frpc 日志报错。"""
    def _esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    lines = [
        f'serverAddr = "{_esc(server_host)}"',
        f'serverPort = {int(server_port)}',
        '',
        '[auth]',
        'method = "token"',
        f'token = "{_esc(token)}"',
    ]
    for p in proxies:
        lines += [
            '',
            '[[proxies]]',
            f'name = "{_esc(p["name"])}"',
            f'type = "{p["type"]}"',
            'localIP = "127.0.0.1"',
            f'localPort = {int(p["local_port"])}',
            f'remotePort = {int(p["remote_port"])}',
        ]
    return "\n".join(lines) + "\n"


class FrpcProcess:
    """一个存档的 frpc.exe 子进程（无优雅关闭指令，直接 terminate → kill）。

    ``adopted_pid`` 不为 None 表示认领来的孤儿进程：没有 Popen 句柄和 stdout，状态与终止都按 PID 操作。
    ``log`` 给出时把输出同时落盘，供日志包收录。"""

    def __init__(self, cluster_path: Path, frpc_exe: Path, config_path: Path, *, adopted_pid: int | None = None,
                 log: FrpcOutputLog | None = None):
        self.cluster_path = cluster_path
        self.frpc_exe = frpc_exe
        self.config_path = config_path
        self.status = FrpcStatus.RUNNING if adopted_pid is not None else FrpcStatus.STARTING
        self.proc: subprocess.Popen | None = None
        self.error: str | None = None
        self.blocked = False  # 文件被安全软件隔离或拦截，界面据此引导添加 Defender 排除项
        self._adopted_pid = adopted_pid
        self._log = log
        self._out_queue: "queue.Queue[str]" = queue.Queue()

    def start(self) -> None:
        # 被杀软隔离/删除时 Popen 抛 FileNotFoundError，提前检查并记录可读的失败原因
        if not self.frpc_exe.exists():
            self.status = FrpcStatus.CRASHED
            self.error = "frpc.exe 不存在（可能被杀毒软件隔离或已手动删除）"
            self.blocked = True
            return
        try:
            self.proc = subprocess.Popen(
                [str(self.frpc_exe), "-c", str(self.config_path)],
                cwd=str(self.frpc_exe.parent),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                creationflags=_SUBPROCESS_FLAGS,
            )
        except OSError as exc:
            self.status = FrpcStatus.CRASHED
            self.error = f"启动失败：{exc}"
            # 检查后到启动前被删（FileNotFoundError），或系统以"文件包含病毒"拒绝执行（WinError 225）
            self.blocked = isinstance(exc, FileNotFoundError) or getattr(exc, "winerror", None) == _SECURITY_BLOCK_WINERROR
            return
        self.status = FrpcStatus.RUNNING
        if self._log:
            self._log.started()
        threading.Thread(target=self._read_loop, daemon=True).start()

    def _read_loop(self) -> None:
        try:
            for line in self.proc.stdout:
                line = line.rstrip("\n")
                self._out_queue.put(line)
                if self._log:
                    self._log.line(line)
        except (OSError, ValueError):
            pass
        if self._log:
            try:
                code = self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                code = None
            self._log.exited(code)

    def read_available_lines(self) -> list[str]:
        if self._adopted_pid is not None:
            return []  # 认领来的孤儿进程没有 stdout 管道，没有日志可读
        lines = []
        while True:
            try:
                lines.append(self._out_queue.get_nowait())
            except queue.Empty:
                break
        return lines

    def poll_exit_code(self) -> int | None:
        if self._adopted_pid is not None:
            return None if _pid_exists(self._adopted_pid) else 0
        return self.proc.poll() if self.proc else None

    def terminate(self) -> None:
        if self._adopted_pid is not None:
            _kill_pid(self._adopted_pid)
            return
        if self.proc:
            try:
                self.proc.terminate()
            except OSError:
                pass

    def kill(self) -> None:
        if self._adopted_pid is not None:
            _kill_pid(self._adopted_pid)
            return
        if self.proc:
            try:
                self.proc.kill()
            except OSError:
                pass

    def stop_blocking(self, term_timeout: float = 5.0) -> None:
        """阻塞到进程退出，须在后台线程调用。"""
        self.status = FrpcStatus.STOPPING
        if self._adopted_pid is not None:
            # taskkill /F 本身就是同步的强制杀，不需要再轮询等待退出。
            self.terminate()
            self.status = FrpcStatus.STOPPED
            return
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
    """管理本进程启动的 frpc，key 为存档路径；stop() 回调在后台线程触发，操作界面需转回界面线程。"""

    def __init__(self):
        self._procs: dict[str, FrpcProcess] = {}

    def start(self, cluster_path: Path, frpc_exe: Path, config_path: Path,
              log: FrpcOutputLog | None = None) -> FrpcProcess:
        proc = FrpcProcess(cluster_path, frpc_exe, config_path, log=log)
        proc.start()
        self._procs[str(cluster_path)] = proc
        return proc

    def get(self, cluster_path: Path) -> FrpcProcess | None:
        return self._procs.get(str(cluster_path))

    def processes(self) -> list[FrpcProcess]:
        return list(self._procs.values())

    def reconcile(self, cluster_path: Path, frpc_exe: Path, config_path: Path) -> FrpcProcess | None:
        """返回存档的 frpc 进程：已跟踪的直接返回，否则按配置文件路径认领孤儿进程，都没有返回 None。"""
        key = str(cluster_path)
        if key in self._procs:
            return self._procs[key]
        pid = _find_frpc_pid_by_config(config_path)
        if pid is None:
            return None
        proc = FrpcProcess(cluster_path, frpc_exe, config_path, adopted_pid=pid)
        self._procs[key] = proc
        return proc

    def stop_blocking(self, cluster_path: Path) -> None:
        """阻塞到进程退出并移出跟踪，须在后台线程调用。"""
        key = str(cluster_path)
        proc = self._procs.get(key)
        if proc:
            proc.stop_blocking()
            self._procs.pop(key, None)

    def stop(self, cluster_path: Path, on_done=None) -> None:
        key = str(cluster_path)
        proc = self._procs.get(key)
        if not proc:
            return

        def _worker():
            proc.stop_blocking()
            self._procs.pop(key, None)
            if on_done:
                on_done(proc)

        threading.Thread(target=_worker, daemon=True).start()
