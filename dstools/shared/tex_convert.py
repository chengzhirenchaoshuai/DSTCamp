"""用内置 ``ktech.exe`` 把 Klei TEX 转成 PNG。

坑：ktech 的工具目录、输入和输出路径都不能可靠处理中文，且 8.3 短名可能被禁用；所以整套
ktools 部署到纯 ASCII 缓存目录，ktech 只接触固定英文文件名，源文件和输出由 Python 搬运。
"""

import ctypes
import hashlib
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from dstools.shared.resource_paths import (
    cache_root_dir,
    runtime_tool_path,
    tool_binary_dir,
    validate_cache_root,
)

_TOOLS_DIR = tool_binary_dir() / "ktools"
_KTECH_EXE = _TOOLS_DIR / "ktech.exe"
_KTOOLS_MARKER = ".bundle.sha256"
_KTOOLS_RUNTIME_LOCK = threading.Lock()
_ktools_runtime_dir: Path | None = None
_ktools_runtime_attempted = False
_logger = logging.getLogger(__name__)

# 微软官方安装包（已核实 Microsoft 数字签名）。发布版不打包，首次需要时从微软官网下载（tool_store）
VCREDIST_RELATIVE = "vcredist/VC++ 2013 x86.exe"

# ktech.exe 是控制台程序，不加该标志每次调用都会闪出黑色控制台窗口
_CREATIONFLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# ktech 依赖同目录的旧版 ImageMagick DLL，后者依赖 VC++ 2013 x86 运行库。
# 坑：不能靠启动 ktech 探测，缺 VCOMP120.dll 时系统会先弹加载器错误框，所以直接检查系统 DLL 目录。
_VC2013_X86_DLLS = ("MSVCR120.dll", "MSVCP120.dll", "VCOMP120.dll")

_runtime_probed = False
_runtime_file_problem: tuple[str, str] = ("", "")

# 运行库文件都在却加载失败（被换成 64 位、截断损坏等）时，系统会先弹「应用程序无法正常启动(0xc000007b)」。
# 启动 ktech 时临时开 SEM_FAILCRITICALERRORS，让它直接带错误码退出；不设成全局，以免开服程序缺 DLL 时
# 用户看不到系统提示缺哪个文件。错误模式是进程级的，加锁防止并发转换互相恢复成默认模式。
_SEM_FAILCRITICALERRORS = 0x0001
_ERROR_MODE_LOCK = threading.Lock()
# 加载器级失败：格式不对（0xc000007b）、找不到 DLL、DLL 损坏、DLL 初始化失败
_LOADER_FAILURE_CODES = frozenset({0xC000007B, 0xC0000135, 0xC000012F, 0xC0000142})
# 熔断：出现一次加载器级失败后本次运行不再调用 ktech，Mod 页改显示修复提示
_runtime_broken = False


def _has_vc2013_x86_runtime(runtime_dir: Path) -> bool:
    """三个 DLL 必须齐全；少任意一个，32 位 ktech.exe 都无法启动。"""
    return all((runtime_dir / dll_name).is_file() for dll_name in _VC2013_X86_DLLS)


def _is_intact_x86_dll(path: Path) -> bool:
    """只读 PE 头判断：必须是 32 位（i386），且文件长度覆盖全部节数据（排除截断）。

    真实反馈的 0xc000007b 就是文件在、但被换成 64 位或已损坏；32 位 ktech 加载它们会失败。
    """
    try:
        with path.open("rb") as stream:
            head = stream.read(4096)
            size = os.fstat(stream.fileno()).st_size
    except OSError:
        return False
    if len(head) < 0x40 or head[:2] != b"MZ":
        return False
    pe = int.from_bytes(head[0x3C:0x40], "little")
    if pe + 24 > len(head) or head[pe:pe + 4] != b"PE\0\0":
        return False
    machine = int.from_bytes(head[pe + 4:pe + 6], "little")
    section_count = int.from_bytes(head[pe + 6:pe + 8], "little")
    sections = pe + 24 + int.from_bytes(head[pe + 20:pe + 22], "little")
    if machine != 0x14C or sections + section_count * 40 > len(head):
        return False
    data_end = 0
    for index in range(section_count):
        entry = sections + index * 40
        raw_size = int.from_bytes(head[entry + 16:entry + 20], "little")
        raw_pointer = int.from_bytes(head[entry + 20:entry + 24], "little")
        data_end = max(data_end, raw_pointer + raw_size)
    return size >= data_end


def _vc2013_x86_runtime_problem(runtime_dir: Path) -> tuple[str, str]:
    """返回 (问题, 出问题的文件)：问题为 "missing"/"broken"，正常时为 ("", "")。"""
    if not _has_vc2013_x86_runtime(runtime_dir):
        return "missing", ""
    for dll_name in _VC2013_X86_DLLS:
        if not _is_intact_x86_dll(runtime_dir / dll_name):
            return "broken", str(runtime_dir / dll_name)
    return "", ""


def ktech_runtime_problem() -> tuple[str, str]:
    """检查 32 位 ktech 所需的 VC++ 2013 运行库（64 位系统在 SysWOW64，32 位在 System32）。

    返回 (问题, 出问题的文件)：问题为 "missing"（缺文件）、"broken"（文件损坏、版本不匹配，
    或本次运行已因加载失败熔断）或 ""（正常）。文件检查结果缓存；非 Windows 恒为正常。
    """
    global _runtime_probed, _runtime_file_problem
    if sys.platform != "win32":
        return "", ""
    if not _runtime_probed:
        _runtime_probed = True
        windows_dir = Path(os.environ.get("WINDIR", r"C:\\Windows"))
        x86_runtime_dir = windows_dir / "SysWOW64"
        if not x86_runtime_dir.is_dir():
            x86_runtime_dir = windows_dir / "System32"
        _runtime_file_problem = _vc2013_x86_runtime_problem(x86_runtime_dir)
    if _runtime_file_problem[0]:
        return _runtime_file_problem
    if _runtime_broken:
        return "broken", ""
    return "", ""




def _ktools_bundle_digest(source_dir: Path) -> str:
    """把文件相对路径和内容一起纳入哈希，目录缺文件也会生成不同版本。"""
    digest = hashlib.sha256()
    for path in sorted(
        (item for item in source_dir.rglob("*") if item.is_file()),
        key=lambda item: item.relative_to(source_dir).as_posix().casefold(),
    ):
        relative = path.relative_to(source_dir).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _runtime_bundle_ready(path: Path, digest: str) -> bool:
    try:
        return (
            (path / "ktech.exe").is_file()
            and (path / _KTOOLS_MARKER).read_text(encoding="ascii").strip() == digest
        )
    except OSError:
        return False


def _prepare_ktools_runtime() -> Path | None:
    """把整套 ktools（含 ImageMagick DLL）按内容哈希部署到纯 ASCII 缓存目录并返回该目录。"""
    global _ktools_runtime_attempted, _ktools_runtime_dir
    with _KTOOLS_RUNTIME_LOCK:
        if _ktools_runtime_attempted:
            return _ktools_runtime_dir
        _ktools_runtime_attempted = True
        if not _TOOLS_DIR.is_dir() or not _KTECH_EXE.is_file():
            return None

        cache_root = cache_root_dir()
        if validate_cache_root(cache_root) is not None:
            return None
        try:
            bundle_digest = _ktools_bundle_digest(_TOOLS_DIR)
        except OSError:
            return None
        runtime_parent = cache_root / "runtime" / "ktools"
        target = runtime_parent / bundle_digest
        if _runtime_bundle_ready(target, bundle_digest):
            _ktools_runtime_dir = target
            return target

        staging: Path | None = None
        try:
            runtime_parent.mkdir(parents=True, exist_ok=True)
            staging = Path(
                tempfile.mkdtemp(
                    prefix=f".{bundle_digest[:8]}-{os.getpid()}-",
                    dir=runtime_parent,
                )
            )
            shutil.copytree(_TOOLS_DIR, staging, dirs_exist_ok=True)
            (staging / _KTOOLS_MARKER).write_text(bundle_digest, encoding="ascii")
            if target.exists() and not _runtime_bundle_ready(target, bundle_digest):
                shutil.rmtree(target)
            try:
                os.replace(staging, target)
                staging = None
            except OSError:
                # 另一个进程可能刚刚完成了同一份原子部署。
                if not _runtime_bundle_ready(target, bundle_digest):
                    raise
            _ktools_runtime_dir = target
            return target
        except OSError as exc:
            _logger.warning("部署 ktools 运行副本失败：%s", exc)
            return None
        finally:
            if staging is not None and staging.exists():
                shutil.rmtree(staging, ignore_errors=True)


# 坑：不声明 argtypes/restype 时 ctypes 按 32 位 int 处理窗口句柄，64 位系统上会 OverflowError
if sys.platform == "win32":
    from ctypes import wintypes as _wintypes
    _user32 = ctypes.windll.user32
    _user32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(ctypes.c_bool, _wintypes.HWND, _wintypes.LPARAM),
                                     _wintypes.LPARAM]
    _user32.EnumWindows.restype = _wintypes.BOOL
    _user32.IsWindowVisible.argtypes = [_wintypes.HWND]
    _user32.IsWindowVisible.restype = _wintypes.BOOL
    _user32.GetWindowTextLengthW.argtypes = [_wintypes.HWND]
    _user32.GetWindowTextLengthW.restype = ctypes.c_int
    _user32.ShowWindow.argtypes = [_wintypes.HWND, ctypes.c_int]
    _user32.ShowWindow.restype = _wintypes.BOOL
    _user32.SetForegroundWindow.argtypes = [_wintypes.HWND]
    _user32.SetForegroundWindow.restype = _wintypes.BOOL
    _user32.GetForegroundWindow.restype = _wintypes.HWND
    _user32.GetWindowThreadProcessId.argtypes = [_wintypes.HWND, ctypes.POINTER(_wintypes.DWORD)]
    _user32.GetWindowThreadProcessId.restype = _wintypes.DWORD
    _user32.AttachThreadInput.argtypes = [_wintypes.DWORD, _wintypes.DWORD, _wintypes.BOOL]
    _user32.AttachThreadInput.restype = _wintypes.BOOL
    _kernel32 = ctypes.windll.kernel32
    _kernel32.GetCurrentThreadId.restype = _wintypes.DWORD


def _enum_visible_windows() -> set:
    """快照当前所有可见顶层窗口的句柄，供 launch_vcredist_installer() 后
    台线程"前后对比找新窗口"用。非 Windows 平台直接返回空集合。"""
    if sys.platform != "win32":
        return set()
    hwnds = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, _wintypes.HWND, _wintypes.LPARAM)
    def _callback(hwnd, _lparam):
        if _user32.IsWindowVisible(hwnd):
            hwnds.append(hwnd)
        return True

    _user32.EnumWindows(_callback, 0)
    return set(hwnds)


def _bring_new_window_to_front(before: set, timeout: float = 60.0) -> None:
    """后台轮询，把安装向导新弹出的窗口提到前台（默认会停在主窗口后面，用户以为没反应）。

    UAC 框在安全桌面上，不需要也无法处理；用户确认 UAC 后向导窗口才会出现，所以超时放宽到 60 秒。
    向导由自解压程序释放的子进程弹出，PID 会变，用启动前后可见窗口的差集来识别。
    """
    SW_RESTORE = 9
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.3)
        for hwnd in _enum_visible_windows() - before:
            if _user32.GetWindowTextLengthW(hwnd) == 0:
                continue  # 没有标题的多半是系统/托盘辅助窗口，不是安装向导本体
            try:
                _user32.ShowWindow(hwnd, SW_RESTORE)
                _force_foreground(hwnd)
            except Exception:
                pass
            return


def _force_foreground(hwnd) -> None:
    """把窗口提到前台。

    坑：Windows 的防抢焦点限制会让 SetForegroundWindow 经常只闪任务栏；用 AttachThreadInput
    临时挂到当前前台线程借用权限，改完立即脱钩（向导类工具的通用做法）。"""
    fg_hwnd = _user32.GetForegroundWindow()
    fg_thread = _user32.GetWindowThreadProcessId(fg_hwnd, None) if fg_hwnd else 0
    cur_thread = _kernel32.GetCurrentThreadId()
    attached = False
    if fg_thread and fg_thread != cur_thread:
        attached = bool(_user32.AttachThreadInput(cur_thread, fg_thread, True))
    try:
        _user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            _user32.AttachThreadInput(cur_thread, fg_thread, False)


def launch_vcredist_installer() -> bool:
    """拉起内置的 VC++ 2013 x86 运行库安装程序（自带向导，不等待结果，装完需重启 DSTCamp 生效）。

    不能带 _CREATIONFLAGS（那是给控制台程序隐藏黑框用的）。拉起后另起 daemon 线程把向导提到
    前台。本机没有安装包（还没下载）返回 False，调用方先用 tool_store 下载。
    """
    try:
        # 坑：从 _MEI 临时目录直接启动会锁住目录，DSTCamp 先退出时 bootloader 报错删除失败，故先复制到数据目录
        installer = runtime_tool_path(VCREDIST_RELATIVE)
        if not installer.is_file():
            return False
        before = _enum_visible_windows()
        subprocess.Popen([str(installer)], cwd=str(installer.parent))
        if sys.platform == "win32":
            threading.Thread(target=_bring_new_window_to_front, args=(before,), daemon=True).start()
    except Exception:
        return False
    return True


def _run_ktech(exe: Path, args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """启动 ktech 并等待结束；加载器级失败时置熔断标记。超时或启动失败抛 OSError/SubprocessError。"""
    global _runtime_broken
    popen_kwargs = dict(
        cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=_CREATIONFLAGS,
    )
    if sys.platform == "win32":
        # 子进程在创建时继承错误模式，创建完立即恢复
        with _ERROR_MODE_LOCK:
            old_mode = _kernel32.GetErrorMode()
            _kernel32.SetErrorMode(old_mode | _SEM_FAILCRITICALERRORS)
            try:
                proc = subprocess.Popen([str(exe), *args], **popen_kwargs)
            finally:
                _kernel32.SetErrorMode(old_mode)
    else:
        proc = subprocess.Popen([str(exe), *args], **popen_kwargs)
    try:
        stdout, stderr = proc.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise
    if (proc.returncode & 0xFFFFFFFF) in _LOADER_FAILURE_CODES:
        _runtime_broken = True
        _logger.warning("ktech 无法启动（0x%08X），运行库损坏或版本不对，本次运行不再转换图标",
                        proc.returncode & 0xFFFFFFFF)
    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)


def tex_to_png(tex_path: Path, out_path: Path) -> bool:
    """把单个 .tex 转成 PNG，成功返回 True；工具缺失或转换失败返回 False（视为没有图标），不抛异常。"""
    tex_path = Path(tex_path)
    # 运行库缺失或损坏时不启动 ktech：省掉一次注定失败的进程启动，也不依赖错误模式去压系统弹框
    if ktech_runtime_problem()[0] or not _KTECH_EXE.exists() or not tex_path.exists():
        return False
    runtime_dir = _prepare_ktools_runtime()
    if runtime_dir is None:
        return False
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    jobs_root = cache_root_dir() / "runtime" / "ktech_jobs"
    try:
        jobs_root.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    # 只传 input.tex：该版 ktech 会把第二个位置参数也当输入，输出按原生规则在 cwd 生成 input.png
    with tempfile.TemporaryDirectory(prefix="job_", dir=jobs_root) as tmp_dir:
        job_dir = Path(tmp_dir)
        staged_input = job_dir / "input.tex"
        staged_out = job_dir / "input.png"
        try:
            shutil.copy2(tex_path, staged_input)
            result = _run_ktech(runtime_dir / "ktech.exe", [staged_input.name], job_dir)
        except (OSError, subprocess.SubprocessError) as exc:
            _logger.warning("ktech 转换启动失败：%s", exc)
            return False
        if result.returncode != 0 or not staged_out.exists():
            detail = (result.stderr or result.stdout or b"").decode(
                errors="replace"
            ).strip()
            _logger.warning("ktech 转换失败（exit=%s）：%s", result.returncode, detail)
            return False
        try:
            shutil.move(str(staged_out), str(out_path))
        except OSError:
            return False
    return out_path.exists()
