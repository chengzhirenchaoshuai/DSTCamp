"""专用服务器安装目录发现、conf_dir 计算与进程管理（不依赖界面）。

子进程 stdout/stdin 走管道，不弹控制台窗口，输出由界面层展示。
"""

import csv
import os
import shlex
import queue
import re
import subprocess
import sys
import threading
import time
from collections import deque
from enum import Enum
from pathlib import Path

from dstools.shared import app_settings
from dstools.features.cluster_config.config_manager import get_shard_option, load_shard_config
from dstools.features.mod.manager import load_mod_overrides
from dstools.features.local_service.server_diagnostics import analyze_mod_loading
from dstools.shared.steam_discovery import find_all_steam_libraries

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    import winreg

DEDICATED_SERVER_APP_ID = "343050"  # 已用真机 appmanifest 文件名核对
CLIENT_APP_ID = "322330"
_INSTALL_DIR_NAME = "Don't Starve Together Dedicated Server"
_EXE_NAMES = {64: "dontstarve_dedicated_server_nullrenderer_x64.exe", 32: "dontstarve_dedicated_server_nullrenderer.exe"}
_CLIENT_EXE_NAMES = {64: "dontstarve_steam_x64.exe", 32: "dontstarve_steam.exe"}
_BIN_DIRS = {64: "bin64", 32: "bin"}


# ── "文档"特殊文件夹 ──────────────────────────────────────────────
# -conf_dir 的隐式基准是 Windows "文档"特殊文件夹下的 Klei\，"文档"可能被重定向到其他盘，必须读真实值

def get_documents_dir() -> Path:
    """返回真实的"文档"特殊文件夹路径，取不到（非 Windows/注册表读取失败）时退回 ~/Documents。"""
    if IS_WINDOWS:
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            ) as key:
                value, _ = winreg.QueryValueEx(key, "Personal")
            path = Path(os.path.expandvars(value))
            if path.exists():
                return path
        except OSError:
            pass
    return Path.home() / "Documents"


# ── 关闭电源节流（EcoQoS） ─────────────────────────────────────────
# 无窗口的专服在工具切到后台后可能被 Windows 节流（降频、调度到能效核），导致模拟跟不上、
# 主机性能变黄，所以显式声明"不节流"（见微软 SetProcessInformation 文档）。

_PROCESS_SET_INFORMATION = 0x0200
_PROCESS_POWER_THROTTLING_INFO_CLASS = 4  # PROCESS_INFORMATION_CLASS.ProcessPowerThrottling
_PROCESS_POWER_THROTTLING_CURRENT_VERSION = 1
_PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1
_PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION = 0x4


def _disable_power_throttling(pid: int) -> bool:
    """关闭指定进程的执行速度与计时器精度节流，返回是否成功；失败不影响开服。

    旧系统不支持计时器标志位时整体调用失败，退回只关执行速度节流。
    """
    import ctypes
    from ctypes import wintypes

    class _ThrottlingState(ctypes.Structure):
        _fields_ = [("Version", wintypes.ULONG),
                    ("ControlMask", wintypes.ULONG),
                    ("StateMask", wintypes.ULONG)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_info = getattr(kernel32, "SetProcessInformation", None)
    if set_info is None:
        return False
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    set_info.restype = wintypes.BOOL
    set_info.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]

    handle = kernel32.OpenProcess(_PROCESS_SET_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        for mask in (_PROCESS_POWER_THROTTLING_EXECUTION_SPEED | _PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION,
                     _PROCESS_POWER_THROTTLING_EXECUTION_SPEED):
            state = _ThrottlingState(_PROCESS_POWER_THROTTLING_CURRENT_VERSION, mask, 0)
            if set_info(handle, _PROCESS_POWER_THROTTLING_INFO_CLASS, ctypes.byref(state), ctypes.sizeof(state)):
                return True
        return False
    finally:
        kernel32.CloseHandle(handle)


# ── Steam 专用服务器安装目录发现 ──────────────────────────────────
# 注册表与 libraryfolders.vdf 解析统一在 steam_discovery.py。

def is_valid_install_dir(path: Path) -> bool:
    """目录下是否有专服 exe 且确实是独立专服安装包。

    坑：客户端 bin64 里也自带同名专服 exe（供"开办本地游戏"），只看 exe 会误判，所以有客户端 exe 的不算。
    不看目录名：SteamCMD 的 force_install_dir 可以是任意名字。"""
    return _has_server_exe(path) and not _has_client_exe(path)


def _has_server_exe(path: Path) -> bool:
    return any((path / _BIN_DIRS[b] / _EXE_NAMES[b]).exists() for b in (64, 32))


def _has_client_exe(path: Path) -> bool:
    return any((path / _BIN_DIRS[b] / _CLIENT_EXE_NAMES[b]).exists() for b in (64, 32))


def is_client_install_dir(path: Path) -> bool:
    """游戏客户端安装目录：bin64/bin 里同时有客户端 exe 和自带的专服 exe。

    真机核对（756039）：自带专服 exe 与独立专服 SHA256 相同，可正常开服。"""
    if "dedicated server" in path.name.lower():
        return False
    return any(
        (path / _BIN_DIRS[b] / _EXE_NAMES[b]).exists()
        and (path / _BIN_DIRS[b] / _CLIENT_EXE_NAMES[b]).exists()
        for b in (64, 32)
    )


def pick_bitness(install_dir: Path) -> int:
    """优先选 64 位，install_dir 必须已经通过 is_valid_install_dir() 校验。"""
    for b in (64, 32):
        if (install_dir / _BIN_DIRS[b] / _EXE_NAMES[b]).exists():
            return b
    raise FileNotFoundError(f"未在 {install_dir} 找到专用服务器可执行文件")


def find_bin64_dir(install_dir: Path) -> Path | None:
    """专服安装根目录（bin64/bin 的上一级），找不到返回 None 而不是抛异常。"""
    try:
        b = pick_bitness(install_dir)
    except FileNotFoundError:
        return None
    return install_dir / _BIN_DIRS[b]


def is_steam_library_install(install_dir: Path) -> bool:
    """装在 Steam 库 steamapps/common 下的由 Steam 客户端管理，其余按 SteamCMD 安装处理。

    坑：SteamCMD 不带 force_install_dir 时默认装到它自己的 steamapps/common 下，目录结构与 Steam 库相同，
    以 steamapps 同级有 steamcmd.exe 区分，这种归 SteamCMD。

    另一坑：Steam 客户端根目录也会自带 steamcmd.exe（与 steam.exe 同级）。若只凭 steamcmd.exe 判断，
    会把真实 Steam 库专服误判成 SteamCMD 安装，进而对库路径跑 +force_install_dir，留下嵌套的
    install_dir/steamapps/appmanifest_*.acf，干扰版本核对。有 steam.exe 时一律按 Steam 库处理。"""
    parent = Path(install_dir).parent
    if parent.name.casefold() != "common" or parent.parent.name.casefold() != "steamapps":
        return False
    root = parent.parent.parent  # steamapps 的上一级
    if (root / "steam.exe").is_file():
        return True
    if (root / "steamcmd.exe").is_file():
        return False
    return True


def find_dedicated_server_dir() -> Path | None:
    """Steam 客户端通道的独立专服：用户手动确认的路径 > 全部 Steam 库；只认 Steam 库里的，找不到返回 None。

    两个通道的专服彻底分开：SteamCMD 装的（不在 Steam 库里）只归 find_steamcmd_server_dir，
    免得 Steam 客户端和 SteamCMD 管理同一份文件、清单互相打架。"""
    remembered = app_settings.get_dedicated_server_path()
    if remembered and is_steam_library_install(remembered) and is_valid_install_dir(remembered):
        return remembered

    for lib in find_all_steam_libraries():
        install_dir = lib / "steamapps" / "common" / _INSTALL_DIR_NAME
        if is_valid_install_dir(install_dir):
            return install_dir
    return None


def find_steamcmd_server_dir() -> Path | None:
    """SteamCMD 通道的独立专服：只认在 SteamCMD 通道安装或选定的目录（不在 Steam 库里），不扫描 Steam 库。"""
    remembered = app_settings.get_steamcmd_server_path()
    if remembered and not is_steam_library_install(remembered) and is_valid_install_dir(remembered):
        return remembered
    return None


# ── -conf_dir 计算 ────────────────────────────────────────────────

class ConfDirCrossDriveError(Exception):
    """klei_root 和 <文档目录>/Klei 不在同一个盘符，-conf_dir 只能表达同盘符的
    相对路径（游戏引擎本身的限制，与 Windows 相对路径无法跨盘符一致）。"""


def resolve_conf_dir_arg(klei_root: Path) -> str | None:
    """默认情况（klei_root 就是 <文档目录>/Klei/DoNotStarveTogether）返回 None，
    不需要传 -conf_dir；否则返回相对于 <文档目录>/Klei 的相对路径。"""
    base = get_documents_dir() / "Klei"
    default_root = base / "DoNotStarveTogether"
    try:
        if klei_root.resolve() == default_root.resolve():
            return None
    except OSError:
        pass
    try:
        return os.path.relpath(klei_root, base)
    except ValueError as e:
        raise ConfDirCrossDriveError(str(klei_root)) from e


def build_launch_args(cluster_name: str, shard_name: str, conf_dir_arg: str | None,
                       ugc_directory: str | None = None,
                       extra_args: str = "") -> list[str]:
    """ugc_directory 用 Steam 的 steamapps/workshop（见 parser.find_shared_ugc_directory），
    找不到为 None，不传该参数，服务器退回默认行为。"""
    args = ["-console", "-cluster", cluster_name, "-shard", shard_name]
    if ugc_directory:
        args = args + ["-ugc_directory", ugc_directory]
    if conf_dir_arg:
        args = ["-conf_dir", conf_dir_arg] + args
    if extra_args.strip():
        try:
            parsed = shlex.split(extra_args, posix=False)
        except ValueError as exc:
            raise ValueError(f"额外启动参数格式错误：{exc}") from exc
        args.extend(
            token[1:-1] if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'" else token
            for token in parsed
        )
    return args


# ── 进程管理 ──────────────────────────────────────────────────────

class ServerStatus(Enum):
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    CRASHED = "crashed"


# 进程 RUNNING 不等于世界可进入，Master 与 Secondary 的就绪标记不同（已用真实 server_log.txt 核对）：
# - Master：正式 Sim 建立后打印 "Sim paused"/"Sim unpaused"（兼容旧版 "DST_Master_Ready"）。
#   坑：新建世界的临时 worldgen 也会打印 "About to start..." 和 "Reset() returning"，
#   随后销毁重载，不能把 Reset 当完成标记；
#   坑：引擎不做空服暂停时（pause_when_empty=false，或"三界同行"等 Mod 自行 SetTimeScale(0) 冻结）
#   永远不打印 "Sim paused"（已用两份只差该项的离线存档实跑确认），因此大厅注册成功
#   （"Server registered via geo DNS"）和从世界接入完成（"[Shard] Secondary <名>(<id>) ready!"）也算就绪；
#   多世界的 Master 在世界加载完后打印 "[Shard] Shard server started on port"，正常日志里
#   "Sim paused" 紧随其后 0～1 秒，同样算就绪（单世界和从世界不打印这行）；
#   单世界靠 "Telling Client our new session identifier"：世界加载完后的最后一步，新建和已有存档实跑
#   都在 "Sim paused" 前约 1 秒；从世界也打印它但要再等主世界接入，所以只给 Master 用；
#   这几行在真机日志中都只出现在正式 Sim 阶段（之后不再销毁重载）；比 "Sim paused" 早 0～1 秒，
#   Mod 多或 WeGame 创建 Rail 服务器时早 2～4 秒，此时 Lua 世界已建好，控制台命令可用；
# - Secondary：连上 Master 后打印 "secondary shard LUA is now ready!"（旧版为 "Slave LUA ..."）。
#   坑：更早的 "secondary shard is now ready!" 不能算：从世界快照比主世界新时，随后会
#   "Synchronizing backward to master snapshot" 并整个销毁重载，LUA ready 才在重载之后（202 份真机日志核对）。
# 启动分界线有两种措辞，随 cluster.ini 的 shard_enabled 变化：
# true 为 "About to start a shard with these settings"，false 为
# "About to start a server with the following settings"，两种都要认，否则单世界存档永远不就绪。
_REAL_START_MARKERS = (
    "about to start a shard with these settings",
    "about to start a server with the following settings",
)
_MASTER_READY_MARKERS = (
    "sim paused", "sim unpaused", "dst_master_ready",
    "server registered via geo dns", "shard server started on port",
    "telling client our new session identifier",
)
_MASTER_SECONDARY_READY_RE = re.compile(r"\[shard\] secondary .+\(\S+\) ready!")
_SECONDARY_READY_MARKERS = ("lua is now ready!",)


def advance_world_ready_marker(
    line: str, is_master: bool, real_start_seen: bool,
) -> tuple[bool, bool]:
    """消费一行日志，返回（已进入正式启动阶段, 本行是否确认就绪）；进程判定与界面进度共用。"""
    lowered = line.lower()
    if not real_start_seen:
        real_start_seen = any(marker in lowered for marker in _REAL_START_MARKERS)
        return real_start_seen, False
    if is_master:
        return real_start_seen, (
            any(marker in lowered for marker in _MASTER_READY_MARKERS)
            or _MASTER_SECONDARY_READY_RE.search(lowered) is not None
        )
    return real_start_seen, any(marker in lowered for marker in _SECONDARY_READY_MARKERS)

# Mod 加载完整性：配置启用时打印 "modoverrides.lua enabling <id>"（不代表加载成功），
# 真正加载才打印 "Loading mod: <id> (<name>) Version:..."，两者之差即配置启用但未加载的 Mod。
# 不依赖"加载失败"文案（真机日志里没见过，无法核实）。
_MOD_ENABLING_RE = re.compile(r"modoverrides\.lua enabling (\S+)", re.IGNORECASE)
_MOD_LOADING_RE = re.compile(r"loading mod:\s*(\S+)\s*\(", re.IGNORECASE)
_MOD_REGISTER_RE = re.compile(r"Registering Mod\s+(\S+)", re.IGNORECASE)
_MOD_CONTEXT_RE = re.compile(r"Mod:\s+(\S+)\s+\(", re.IGNORECASE)
_MOD_DISABLED_RE = re.compile(r"Disabling\s+(\S+)(?:\s+\([^)]*\))?\s+because it had an error", re.IGNORECASE)
# c_shutdown 的参数在实际用法中会出现空参数、布尔值或 0/1。只识别完整
# 命令，避免聊天/公告文字里恰好包含这段文本时把进程误标为正在停止。
_SHUTDOWN_COMMAND_RE = re.compile(
    r"^\s*c_shutdown\s*\(\s*(?:(?:true|false|[01])\s*)?\)\s*;?\s*$"
)


class ServerProcess:
    """一个 (cluster, shard) 对应的专用服务器子进程：启动、读取控制台输出、
    发送控制台命令、优雅/强制关闭。"""

    def __init__(self, cluster_name: str, shard_name: str, cluster_path: Path,
                 install_dir: Path, conf_dir_arg: str | None, is_master: bool = True,
                 ugc_directory: str | None = None, bin64_override: Path | None = None,
                 extra_args: str = ""):
        self.cluster_name = cluster_name
        self.shard_name = shard_name
        self.cluster_path = cluster_path
        self.install_dir = install_dir
        self.conf_dir_arg = conf_dir_arg
        self.is_master = is_master
        self.ugc_directory = ugc_directory
        self.extra_args = extra_args
        # LuaJIT 模式下实际运行的 exe 目录（隔离副本），install_dir 仍表示所属安装根目录
        self.bin64_override = bin64_override
        self.status = ServerStatus.STARTING
        # 明确请求过关服后，退出及其后到达的尾部日志都不触发崩溃诊断
        # （进程退出后状态变 STOPPED，读取线程仍可能晚一轮送来最后几行）
        self.intentional_shutdown = False
        self.world_ready = False
        # 主世界是否已在 Klei 完成房间注册；没注册过就崩溃时 Klei 端没有
        # 需要释放的房间，不必给令牌打等待标记。
        self.registered = False
        self.proc: subprocess.Popen | None = None
        self._out_queue: "queue.Queue[str]" = queue.Queue()
        # 只保留最近一段日志供异常退出诊断使用，避免长时间运行的世界
        # 无限增长内存；完整日志仍然照常显示在控制台文本框里。
        self._recent_log_lines = deque(maxlen=500)
        # world_ready 变 True 时计算一次；None 表示世界尚未就绪
        self.mods_enabled: set[str] = set()
        self.mods_loaded: set[str] = set()
        self.mods_failed: set[str] = set()
        self._mod_context: str | None = None
        self.missing_mods: list[str] | None = None

    @property
    def visible_mod_ids(self) -> set[str]:
        """已启用的 Mod（LuaJIT 加速补丁也是启动的 Mod，一并计入）。"""
        return set(self.mods_enabled)

    @property
    def visible_mod_count(self) -> int:
        return len(self.visible_mod_ids)

    def start(self) -> None:
        # 完全不存在的 Mod 服务器可能连 enabling 行都不打印，所以启动前先读配置里的启用集合
        overrides_path = self.cluster_path / self.shard_name / "modoverrides.lua"
        try:
            overrides = load_mod_overrides(overrides_path)
            self.mods_enabled.update(
                key for key, entry in overrides.mods.items() if entry.enabled
            )
        except (OSError, ValueError, TypeError):
            # 日志读取和服务器启动不能因为诊断预读失败而被阻断；后续仍
            # 可使用服务器实际打印的 modoverrides enabling 行继续判断。
            pass
        bitness = pick_bitness(self.install_dir)  # 位数判断始终用真实安装目录
        bin64_dir = self.bin64_override if self.bin64_override is not None \
            else self.install_dir / _BIN_DIRS[bitness]
        exe = bin64_dir / _EXE_NAMES[bitness]
        args = build_launch_args(
            self.cluster_name, self.shard_name, self.conf_dir_arg,
            self.ugc_directory, self.extra_args
        )
        # 专服没有窗口，拿不到前台时间片加成；提到"高于正常"，避免和同机
        # 前台运行的游戏客户端等程序抢 CPU 时吃亏。
        creationflags = (subprocess.CREATE_NO_WINDOW | subprocess.ABOVE_NORMAL_PRIORITY_CLASS) if IS_WINDOWS else 0
        self.proc = subprocess.Popen(
            [str(exe)] + args,
            cwd=str(exe.parent),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            creationflags=creationflags,
        )
        if IS_WINDOWS:
            _disable_power_throttling(self.proc.pid)
        self.status = ServerStatus.RUNNING
        threading.Thread(target=self._read_loop, daemon=True).start()

    def _read_loop(self) -> None:
        real_start_seen = False
        if not hasattr(self, "mods_failed"):
            self.mods_failed = set()
        if not hasattr(self, "_mod_context"):
            self._mod_context = None
        try:
            for line in self.proc.stdout:
                line = line.rstrip("\n")
                recent_lines = getattr(self, "_recent_log_lines", None)
                if recent_lines is None:
                    recent_lines = self._recent_log_lines = deque(maxlen=500)
                recent_lines.append(line)
                if not self.world_ready:
                    m = _MOD_ENABLING_RE.search(line)
                    if m:
                        self.mods_enabled.add(m.group(1))
                    else:
                        m = _MOD_LOADING_RE.search(line)
                        if m:
                            self.mods_loaded.add(m.group(1))
                        else:
                            m = _MOD_REGISTER_RE.search(line)
                            if m:
                                self.mods_loaded.add(m.group(1))

                    context = _MOD_CONTEXT_RE.search(line)
                    if context:
                        self._mod_context = context.group(1)
                    if "error loading mod!" in line.lower() and self._mod_context:
                        self.mods_failed.add(self._mod_context)
                    disabled = _MOD_DISABLED_RE.search(line)
                    if disabled:
                        self.mods_failed.add(disabled.group(1))
                    real_start_seen, ready_now = advance_world_ready_marker(
                        line, self.is_master, real_start_seen,
                    )
                    if ready_now:
                        self.world_ready = True
                        self.missing_mods = list(analyze_mod_loading(
                            enabled_mods=self.mods_enabled,
                            loaded_mods=self.mods_loaded,
                            failed_mods=self.mods_failed,
                            visible_mod_count=self.visible_mod_count,
                        ).failed_mods)
                self._out_queue.put(line)
        except (OSError, ValueError):
            pass

    @property
    def recent_log_lines(self) -> tuple[str, ...]:
        """返回进程退出前的有限日志快照，供诊断模块只读使用。"""
        return tuple(getattr(self, "_recent_log_lines", ()))

    def read_available_lines(self, max_lines: int | None = None) -> list[str]:
        """非阻塞读取日志；可限制单批数量，避免错误风暴长期占用界面线程。"""
        lines = []
        while max_lines is None or len(lines) < max_lines:
            try:
                lines.append(self._out_queue.get_nowait())
            except queue.Empty:
                break
        return lines

    def send_command(self, text: str) -> bool:
        if not self.proc or self.proc.stdin is None or self.proc.stdin.closed:
            return False
        try:
            self.proc.stdin.write(text + "\n")
            self.proc.stdin.flush()
            # 控制台手动 c_shutdown() 与"停止"按钮同义，之后的退出不算崩溃；
            # 写入成功后再改状态，管道已断时不能掩盖真实异常
            if _SHUTDOWN_COMMAND_RE.fullmatch(text):
                self.intentional_shutdown = True
                self.status = ServerStatus.STOPPING
            return True
        except (OSError, ValueError):
            return False

    def request_shutdown(self) -> bool:
        return self.send_command("c_shutdown()")

    def poll_exit_code(self) -> int | None:
        return self.proc.poll() if self.proc else None

    def sync_expected_exit(self) -> int | None:
        """同步预期关服的最终状态并返回退出码（控制台手动 c_shutdown 时由轮询收口）。"""
        exit_code = self.poll_exit_code()
        if exit_code is not None and self.status == ServerStatus.STOPPING:
            self.status = ServerStatus.STOPPED
        return exit_code

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

    def stop_blocking(self, graceful_timeout: float = 30.0, term_timeout: float = 5.0) -> None:
        """依次尝试 c_shutdown → terminate → kill，阻塞到进程退出，必须在后台线程调用。"""
        self.intentional_shutdown = True
        self.status = ServerStatus.STOPPING
        if self.request_shutdown():
            # c_shutdown() 会先存档，大型存档可能超过 5 秒，每个分片等 30 秒（stop_all 并行等待）
            deadline = time.monotonic() + graceful_timeout
            while time.monotonic() < deadline:
                if self.poll_exit_code() is not None:
                    self.status = ServerStatus.STOPPED
                    return
                time.sleep(0.2)
        self.terminate()
        deadline = time.monotonic() + term_timeout
        while time.monotonic() < deadline:
            if self.poll_exit_code() is not None:
                self.status = ServerStatus.STOPPED
                return
            time.sleep(0.2)
        self.kill()
        # 坑：Windows 上 kill() 返回时进程未必已退出，直接置 STOPPED 会让存档句柄仍被占用；
        # 用 wait() 等到真正退出（最多 term_timeout）
        try:
            self.proc.wait(timeout=term_timeout)
        except subprocess.TimeoutExpired:
            pass
        self.status = ServerStatus.STOPPED


class ServerManager:
    """管理本进程启动的专服子进程，key 为 (存档路径, 分片名)（不同目录的存档可能重名）。

    stop()/stop_all() 的回调在后台线程触发，操作界面需自行转回界面线程。
    """

    def __init__(self):
        self._procs: dict[tuple[str, str], ServerProcess] = {}

    @staticmethod
    def _key(cluster_path: Path, shard_name: str) -> tuple[str, str]:
        return (str(cluster_path), shard_name)

    def start(self, cluster_name: str, cluster_path: Path, shard_name: str,
              install_dir: Path, conf_dir_arg: str | None, is_master: bool = True,
              ugc_directory: str | None = None, bin64_override: Path | None = None,
              extra_args: str = "") -> ServerProcess:
        existing = self.get(cluster_path, shard_name)
        if existing and existing.status in (
                ServerStatus.STARTING, ServerStatus.RUNNING, ServerStatus.STOPPING):
            # UI 已经会禁用重复启动，但“全部启动”/未来其它调用方不能只靠
            # 按钮状态兜底，否则旧进程引用会被覆盖并变成停不掉的孤儿。
            return existing
        proc = ServerProcess(cluster_name, shard_name, cluster_path, install_dir, conf_dir_arg,
                             is_master, ugc_directory, bin64_override, extra_args)
        proc.start()
        self._procs[self._key(cluster_path, shard_name)] = proc
        return proc

    def get(self, cluster_path: Path, shard_name: str) -> ServerProcess | None:
        return self._procs.get(self._key(cluster_path, shard_name))

    def running(self) -> list[ServerProcess]:
        return [p for p in self._procs.values()
                if p.status in (ServerStatus.STARTING, ServerStatus.RUNNING, ServerStatus.STOPPING)]


    def stop(self, cluster_path: Path, shard_name: str, on_done=None) -> None:
        proc = self.get(cluster_path, shard_name)
        if not proc:
            return

        def _worker():
            proc.stop_blocking()
            if on_done:
                on_done(proc)

        threading.Thread(target=_worker, daemon=True).start()

    def stop_all(self, on_each_done=None, on_all_done=None) -> None:
        procs = self.running()
        if not procs:
            if on_all_done:
                on_all_done()
            return
        remaining = len(procs)
        lock = threading.Lock()

        def _worker(p):
            nonlocal remaining
            p.stop_blocking()
            if on_each_done:
                on_each_done(p)
            with lock:
                remaining -= 1
                done = remaining == 0
            if done and on_all_done:
                on_all_done()

        for p in procs:
            threading.Thread(target=_worker, args=(p,), daemon=True).start()


# ── WeGame 等外部启动的世界进程探测 ──────────────────────────────────
# WeGame 专服由 WeGame 客户端拉起（Rail 会话令牌只有它能签发），只能反向扫描系统进程：
# tasklist 找专服进程，netstat 查其绑定的 UDP 端口，与各世界 server.ini 的端口比对。
# 端口唯一，比读命令行可靠（非管理员读不到其他会话进程的命令行）。


def _find_dst_process_pids() -> dict[int, float]:
    """返回 {pid: 内存占用(MB)}，两种历史可执行文件名（32/64 位）都扫。
    单个 tasklist 找不到时输出为空，不当异常处理。"""
    pids: dict[int, float] = {}
    for exe_name in set(_EXE_NAMES.values()):
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {exe_name}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=10,
                # tasklist 按系统代码页输出（中文系统为 GBK），UTF-8 模式下解码会失败，故按代码页容错解码
                encoding="mbcs" if IS_WINDOWS else None, errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0,
            ).stdout
        except (OSError, subprocess.TimeoutExpired):
            continue
        for row in csv.reader((out or "").splitlines()):
            if len(row) < 5:
                continue
            try:
                pid = int(row[1])
            except ValueError:
                continue
            mem_str = row[4].replace(",", "").replace("K", "").strip()
            try:
                pids[pid] = int(mem_str) / 1024
            except ValueError:
                pids[pid] = 0.0
    return pids


def _udp_ports_by_pid() -> dict[int, set[int]]:
    """返回 {pid: {绑定的本地 UDP 端口}}；netstat -ano 无需管理员权限。"""
    result: dict[int, set[int]] = {}
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "UDP"], capture_output=True, text=True, timeout=10,
            encoding="mbcs" if IS_WINDOWS else None, errors="replace",  # 同 _find_dst_process_pids
            creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return result
    for line in (out or "").splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[0] != "UDP":
            continue
        try:
            pid = int(parts[-1])
            port = int(parts[1].rsplit(":", 1)[-1])
        except ValueError:
            continue
        result.setdefault(pid, set()).add(port)
    return result


def detect_external_shard_processes(cluster) -> dict[str, dict]:
    """按"进程存在且端口绑定成功"探测存档各世界状态（不依赖本进程是否启动过）。

    返回 {世界名: {"configured_port", "running", "pid", "mem_mb"}}；端口对不上的专服进程不算该世界。"""
    pid_mem = _find_dst_process_pids()
    pid_ports = _udp_ports_by_pid()
    result: dict[str, dict] = {}
    for shard in cluster.shards:
        config = load_shard_config(shard.path)
        raw_port = get_shard_option(config, "NETWORK", "server_port")
        info = {"configured_port": raw_port, "running": False, "pid": None, "mem_mb": None}
        try:
            port = int(raw_port)
        except (TypeError, ValueError):
            port = None
        if port is not None:
            for pid, ports in pid_ports.items():
                if port in ports and pid in pid_mem:
                    info.update(running=True, pid=pid, mem_mb=round(pid_mem[pid], 1))
                    break
        result[shard.name] = info
    return result


def detect_external_running_clusters(clusters) -> set[str]:
    """一次系统扫描找出所有有世界真实绑定端口的存档路径（供令牌独占预检）。"""
    pid_mem = _find_dst_process_pids()
    if not pid_mem:
        return set()
    pid_ports = _udp_ports_by_pid()
    dst_ports = {
        port for pid, ports in pid_ports.items() if pid in pid_mem for port in ports
    }
    result = set()
    for cluster in clusters:
        for shard in getattr(cluster, "shards", ()):
            config = load_shard_config(shard.path)
            raw_port = get_shard_option(config, "NETWORK", "server_port")
            try:
                port = int(raw_port)
            except (TypeError, ValueError):
                continue
            if port in dst_ports:
                result.add(str(cluster.path))
                break
    return result
