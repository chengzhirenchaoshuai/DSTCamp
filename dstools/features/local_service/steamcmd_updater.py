"""通过 SteamCMD 安装/更新独立专服（不依赖界面）。

真机实测（中文 Windows）：
- 清单写在 force_install_dir 下的 steamapps/appmanifest_343050.acf，专服文件直接放在 force_install_dir；
- 输出跟随系统语言（中文系统输出中文），不能按文本判断成败，只看退出码和清单；
- 刚装好的 SteamCMD 首次 app_update 报 "Missing configuration"（退出码 7），所以失败后自动重试一次；
- 刚释放的 SteamCMD 首次 app_update 会被立即挂起（应用信息还没取到）：空等约 2 分钟后照样打印 Success!，
  退出码有时是 0，实际什么也没做；content_log 里当场就有 "result Suspended"。这 2 分钟里它在下载并保存
  应用信息，提前结束的话重试照样被挂起（已实测），所以等它自己结束，再按失败重试；
- 先单独登录一次（+login ... +quit，约 1 分钟）就不会被挂起（实测两次）：登录会话会取回并保存
  appcache/appinfo.vdf，只跑 +quit 不会。所以首次更新/下载前先登录准备，界面也会提前在后台准备；
- 标准输出接管道时是块缓冲，进程结束才一次性吐出，所以优先放进伪控制台运行（输出实时）；
  不支持伪控制台时退回管道，改为跟踪它实时写入的 logs/content_log.txt；
- 账号登录时密码从标准输入读取（不回显）；没有缓存凭据又不给输入时 2 秒内失败，密码错误同样退出码 5；
- 同时跑两个 SteamCMD 时后启动的会报 "steam didn't shutdown cleanly"，且两者共写同一批 logs/*.txt，
  这里按日志判断的挂起、等密码会串到别的任务上，所以更新、下载 Mod、登录一律排队，同一时间只跑一个。
"""

from __future__ import annotations

import gzip
import os
import re
import shutil
import subprocess
import functools
import threading
import zlib
from pathlib import Path
from typing import Callable

from dstools.features.local_service.conpty import run_in_pseudo_console
from dstools.features.local_service.dedicated_server import is_steam_library_install  # noqa: F401  调用方经本模块使用
from dstools.i18n import t
from dstools.features.local_service.steam_client_updater import (
    DEDICATED_SERVER_APP_ID, SteamAppSnapshot, snapshot_from_manifest,
)
from dstools.shared import app_settings
from dstools.shared.resource_paths import cache_dir, path_is_ascii, tool_binary_dir

_LINE_SPLIT_RE = re.compile(rb"[\r\n]+")
# 实测：密码错误、缓存凭据失效都是退出码 5。登录失败不自动重试，避免连续失败触发 Steam 的登录限制
LOGIN_FAILED_EXIT_CODE = 5


class SteamcmdLoginError(RuntimeError):
    """账号登录失败或缓存的登录凭据已失效，需要重新登录。"""


_TASK_LOCK = threading.Lock()
# 本次运行内发现登录已失效的 (steamcmd 路径, 小写账号)：界面据此提示重新登录；
# 重新登录成功或用该账号跑成功一次即清除（登录缓存存在每份 SteamCMD 自己的目录里，所以按路径区分）
_expired_logins: set[tuple[str, str]] = set()


class SteamcmdBusyError(RuntimeError):
    """已有 SteamCMD 任务在运行，本操作不能同时进行。"""


def steamcmd_busy() -> bool:
    """是否有 SteamCMD 任务（更新、下载、登录、后台准备，或删除/复制工坊 Mod）正在进行。"""
    return _TASK_LOCK.locked()


def run_when_idle(func, *args, wait: bool = False, **kwargs):
    """SteamCMD 空闲时独占执行 func（如删除、复制工坊 Mod，不能和下载同时进行）；
    有任务在跑时立即抛 SteamcmdBusyError，wait=True 时排队等它结束。"""
    if not _TASK_LOCK.acquire(blocking=wait):
        raise SteamcmdBusyError("另一个 SteamCMD 任务正在运行")
    try:
        return func(*args, **kwargs)
    finally:
        _TASK_LOCK.release()


def _login_key(steamcmd: Path, account: str) -> tuple[str, str]:
    return str(Path(steamcmd)), account.casefold()


def _record_login(steamcmd: Path, account: str, returncode: int | None) -> None:
    """returncode 为退出码 5 记为失效，None 表示用该账号成功跑完一次。匿名不记录。"""
    if not account:
        return
    if returncode == LOGIN_FAILED_EXIT_CODE:
        _expired_logins.add(_login_key(steamcmd, account))
    elif returncode is None:
        _expired_logins.discard(_login_key(steamcmd, account))


def login_expired(steamcmd: Path | None, account: str) -> bool:
    """这份 SteamCMD 上该账号的登录本次运行中是否已发现失效。"""
    return bool(steamcmd and account) and _login_key(steamcmd, account) in _expired_logins


def _exclusive_task(func):
    """整个任务（含重试、分批）期间独占 SteamCMD；已有任务在跑时在日志里说明并排队，等待中可取消。
    被包装的函数须以关键字参数接收 on_line 与 cancel_event。"""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        cancel_event = kwargs.get("cancel_event")
        if not _TASK_LOCK.acquire(blocking=False):
            kwargs["on_line"](t("steamcmd.waiting_other_task"))
            while not _TASK_LOCK.acquire(timeout=0.5):
                if cancel_event is not None and cancel_event.is_set():
                    raise InterruptedError("等待其他 SteamCMD 任务时已取消")
        try:
            return func(*args, **kwargs)
        finally:
            _TASK_LOCK.release()

    return wrapper


def is_steamcmd_exe(path: Path | None) -> bool:
    return bool(path) and Path(path).name.casefold() == "steamcmd.exe" and Path(path).is_file()


def bundled_steamcmd_path() -> Path:
    """内置 SteamCMD 的运行位置：缓存目录下（缓存目录要求纯 ASCII，SteamCMD 在非 ASCII 路径下直接报错退出）。
    放缓存而不是 data：程序和它自更新下载的组件都可重建，清空缓存后下次使用重新释放、重新登录即可。"""
    return cache_dir("steamcmd") / "steamcmd.exe"


def ensure_bundled_steamcmd() -> Path | None:
    """首次使用时把随包的官方 steamcmd.exe 原样释放到缓存目录；已存在就不动（SteamCMD 会自更新这个文件）。
    路径含非 ASCII 字符或释放失败时返回 None。"""
    target = bundled_steamcmd_path()
    if not path_is_ascii(target):
        return None
    if target.is_file():
        return target
    source = tool_binary_dir() / "steamcmd"
    try:
        data = gzip.decompress((source / "steamcmd.exe.gz").read_bytes())
        target.parent.mkdir(parents=True, exist_ok=True)
        # 许可协议要求随程序附带，原样放在旁边
        shutil.copyfile(source / "LICENSE.txt", target.parent / "LICENSE.txt")
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
    except (OSError, EOFError, gzip.BadGzipFile):
        return None
    return target


def steamcmd_workshop_dir() -> Path:
    """SteamCMD 下载工坊内容的位置（steamcmd.exe 同级 steamapps/workshop，不受 force_install_dir 影响）。
    实测：目录结构、appworkshop_322330.acf 与 Steam 客户端相同，下载内容逐字节一致。只算路径，不释放程序。"""
    custom = custom_steamcmd_path()
    root = custom.parent if custom is not None else bundled_steamcmd_path().parent
    return root / "steamapps" / "workshop"


def custom_steamcmd_path() -> Path | None:
    """用户改选的 steamcmd.exe；没选过返回 None（使用内置）。"""
    return app_settings.get_steamcmd_path()


def find_steamcmd() -> Path | None:
    """实际使用的 steamcmd.exe：用户改选过就用它（不存在或路径含非 ASCII 字符时返回 None，不偷偷换内置），
    否则用内置的。"""
    custom = custom_steamcmd_path()
    if custom is not None:
        return custom if is_steamcmd_exe(custom) and path_is_ascii(custom) else None
    return ensure_bundled_steamcmd()


_VDF_TOKEN_RE = re.compile(r'"((?:\\.|[^"\\])*)"|([{}])')


def _parse_vdf(text: str) -> dict:
    """解析 Valve KeyValues 文本为嵌套字典（只读用途，格式异常时尽量读出已有部分）。"""
    stack: list[dict] = [{}]
    key: str | None = None
    for match in _VDF_TOKEN_RE.finditer(text):
        value, brace = match.groups()
        if brace == "{":
            child: dict = {}
            if key is not None:
                stack[-1][key] = child
            stack.append(child)
            key = None
        elif brace == "}":
            if len(stack) > 1:
                stack.pop()
            key = None
        elif key is None:
            key = value
        else:
            stack[-1][key] = value
            key = None
    return stack[0]


def _vdf_child(node, key: str) -> dict:
    """按键名取子对象，键名不区分大小写；没有或不是对象时返回空字典。"""
    if isinstance(node, dict):
        for name, child in node.items():
            if name.casefold() == key.casefold() and isinstance(child, dict):
                return child
    return {}


def cached_login_accounts(steamcmd: Path | None) -> list[str]:
    """这份 SteamCMD 保存着登录凭据的账号，按 config.vdf 中的顺序；读不到返回空列表。

    实测 config/config.vdf：InstallConfigStore/Software/Valve/Steam 下 Accounts 记登录过的用户名，
    ConnectCache 存登录凭据，键为用户名 CRC32 的小写十六进制（不补前导 0，实测 025d8a16 写成 25d8a161）加 "1"。
    这只说明凭据还在；改了密码或 Steam 让它过期时，要到实际登录才会发现（退出码 5）。"""
    if steamcmd is None:
        return []
    try:
        text = (Path(steamcmd).parent / "config" / "config.vdf").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    steam = _parse_vdf(text)
    for key in ("InstallConfigStore", "Software", "Valve", "Steam"):
        steam = _vdf_child(steam, key)
    cache = {name.casefold() for name in _vdf_child(steam, "ConnectCache")}
    # 补零的写法也认，以防不同版本的 SteamCMD 写法不一
    return [name for name in _vdf_child(steam, "Accounts")
            if {f"{zlib.crc32(name.encode('utf-8')):x}1", f"{zlib.crc32(name.encode('utf-8')):08x}1"} & cache]


def login_cached(steamcmd: Path | None, account: str) -> bool:
    """这份 SteamCMD 是否保存着该账号的登录凭据（用户名不区分大小写）。"""
    return bool(account) and account.casefold() in {name.casefold() for name in cached_login_accounts(steamcmd)}


def steamcmd_needs_prepare(steamcmd: Path) -> bool:
    """程序在、但还没登录取过应用信息（没有 appcache/appinfo.vdf）。"""
    steamcmd = Path(steamcmd)
    return steamcmd.is_file() and not (steamcmd.parent / "appcache" / "appinfo.vdf").is_file()


def snapshot_install(install_dir: Path, app_id: str = DEDICATED_SERVER_APP_ID) -> SteamAppSnapshot:
    """读 SteamCMD 安装目录自己的清单；没有清单时只凭专服文件判断是否已安装。"""
    install_dir = Path(install_dir)
    manifest = install_dir / "steamapps" / f"appmanifest_{app_id}.acf"
    existing = install_dir if install_dir.is_dir() else None
    if not manifest.is_file():
        return SteamAppSnapshot(app_id, None, existing, None, None, None, None, None)
    return snapshot_from_manifest(app_id, manifest, existing)


def build_command(steamcmd: Path, install_dir: Path, *, validate: bool = True, account: str = "") -> list[str]:
    """account 为空时匿名登录；有账号时只传用户名，靠 SteamCMD 缓存的凭据登录。"""
    # force_install_dir 必须写在 login 之前，否则 SteamCMD 会提示并忽略
    command = [str(steamcmd), "+force_install_dir", str(install_dir),
               "+login", account or "anonymous", "+app_update", DEDICATED_SERVER_APP_ID]
    if validate:
        command.append("validate")
    command.append("+quit")
    return command


def update_succeeded(returncode: int, snapshot: SteamAppSnapshot) -> bool:
    return (returncode == 0 and snapshot.manifest_path is not None and snapshot.install_ready
            and snapshot.download_complete and not snapshot.update_pending)


def is_progress_log_line(line: str, app_id: str = DEDICATED_SERVER_APP_ID) -> bool:
    """content_log 里只转发本 App 的状态变化、下载速率和失败原因，下载源、连接细节不转发。"""
    return f"AppID {app_id}" in line or "download rate" in line or "Failed" in line


def is_suspended_log_line(line: str, app_id: str = DEDICATED_SERVER_APP_ID) -> bool:
    """更新任务被挂起（实测 content_log：AppID 343050 scheduler finished : removed from schedule (result Suspended, ...)）。
    content_log 固定英文，不随系统语言变化。"""
    return f"AppID {app_id} scheduler finished" in line and "result Suspended" in line


def is_workshop_log_line(line: str) -> bool:
    """workshop_log 里只转发每个项目的开始与结果（实测格式：Download item <ID> requested / result : OK）。"""
    return "Download item" in line


def _follow_log(log_path: Path, accept: Callable[[str], bool], on_line: Callable[[str], None],
                stop: threading.Event) -> None:
    """从启动时的末尾开始跟踪新写入的行；文件被轮转变短时从头读。"""
    try:
        offset = log_path.stat().st_size
    except OSError:
        offset = 0
    pending = ""
    while True:
        stopping = stop.wait(1.0)
        try:
            size = log_path.stat().st_size
            if size < offset:
                offset = 0
            if size > offset:
                with log_path.open("rb") as file:
                    file.seek(offset)
                    data = file.read(size - offset)
                offset = size
                lines = (pending + data.decode("utf-8", errors="replace")).split("\n")
                pending = lines.pop()
                for line in lines:
                    line = line.strip()
                    if line and accept(line):
                        on_line(line)
        except OSError:
            pass
        if stopping:
            return


# SteamCMD 等待输入的提示（实测英文：password: / Steam Guard code:）
_INPUT_PROMPT_RE = re.compile(r"(?:password|steam guard code|two-factor code)\s*:", re.IGNORECASE)


def _watch_input_prompts(log_path: Path, answers: int, abort: threading.Event, stop: threading.Event) -> None:
    """跟踪 console_log，提示次数超过事先给的输入行数就置 abort。

    坑（实测）：伪控制台里不换行的提示（"password: "）要到伪控制台关闭才送出，从输出里等不到；
    console_log 却会立刻写入，所以从这里判断 SteamCMD 是否卡在等输入。"""
    try:
        offset = log_path.stat().st_size
    except OSError:
        offset = 0
    seen = ""
    while not stop.wait(0.5):
        try:
            size = log_path.stat().st_size
            if size < offset:
                offset = 0
            if size > offset:
                with log_path.open("rb") as file:
                    file.seek(offset)
                    seen += file.read(size - offset).decode("utf-8", errors="replace")
                offset = size
        except OSError:
            continue
        if len(_INPUT_PROMPT_RE.findall(seen)) > answers:
            abort.set()
            return


class _EitherEvent:
    """用户取消或内部中止任一发生即视为取消，供伪控制台轮询。"""

    def __init__(self, *events: threading.Event | None):
        self._events = [event for event in events if event is not None]

    def is_set(self) -> bool:
        return any(event.is_set() for event in self._events)


def _run_once(command: list[str], on_line: Callable[[str], None],
              cancel_event: threading.Event | None, stdin_text: str | None = None,
              log_name: str = "content_log.txt",
              log_filter: Callable[[str], bool] = is_progress_log_line,
              follow_log_with_pty: bool = False) -> int:
    """优先在伪控制台里运行，输出（含每 2 秒一行的下载进度）实时到达；系统不支持时退回管道，
    管道下输出要到结束才有，改为跟踪 SteamCMD 实时写的日志。follow_log_with_pty 为 True 时伪控制台下也跟踪日志。"""
    log_stop = threading.Event()
    log_thread: threading.Thread | None = None

    def start_log() -> None:
        nonlocal log_thread
        log_thread = threading.Thread(
            target=_follow_log,
            args=(Path(command[0]).parent / "logs" / log_name, log_filter, on_line, log_stop), daemon=True)
        log_thread.start()

    try:
        if follow_log_with_pty:
            start_log()
        # 坑：伪控制台的输入一直开着，SteamCMD 要密码/验证码时会无限等待（管道下读到输入结束会立刻失败）。
        # 提示数超过事先给的输入行数就中止，按登录失败处理
        answers = len([line for line in (stdin_text or "").splitlines() if line])
        abort = threading.Event()
        prompt_stop = threading.Event()
        prompt_thread = threading.Thread(
            target=_watch_input_prompts,
            args=(Path(command[0]).parent / "logs" / "console_log.txt", answers, abort, prompt_stop), daemon=True)
        prompt_thread.start()
        try:
            code = run_in_pseudo_console(command, on_line, stdin_text=stdin_text,
                                         cancel_event=_EitherEvent(cancel_event, abort))
            return LOGIN_FAILED_EXIT_CODE if abort.is_set() else code
        except OSError:
            pass  # 伪控制台不可用：退回管道
        finally:
            prompt_stop.set()
            prompt_thread.join(2)
        if log_thread is None:
            start_log()
        return _run_piped(command, on_line, cancel_event, stdin_text)
    finally:
        log_stop.set()
        if log_thread is not None:
            log_thread.join(3)


def _run_piped(command: list[str], on_line: Callable[[str], None],
               cancel_event: threading.Event | None, stdin_text: str | None = None) -> int:
    proc = subprocess.Popen(
        command, stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if stdin_text is not None:
        # 密码等交互输入预先写入：提示同样被缓冲，没法等看到提示再输入
        try:
            proc.stdin.write(stdin_text.encode("utf-8"))
            proc.stdin.close()
        except OSError:
            pass
    finished = threading.Event()

    def watch_cancel() -> None:
        while not finished.is_set():
            if cancel_event.wait(0.5):
                proc.kill()
                return

    if cancel_event is not None:
        threading.Thread(target=watch_cancel, daemon=True).start()
    try:
        output = b""
        while chunk := proc.stdout.read1(4096):
            output += chunk
        returncode = proc.wait()
    finally:
        finished.set()
        proc.stdout.close()
    # 进度行用 \r 覆盖刷新，按 \r/\n 一起切行；连续重复的行只报一次
    last = None
    for raw in _LINE_SPLIT_RE.split(output):
        text = raw.decode("utf-8", errors="replace").strip()
        if text and text != last:
            last = text
            on_line(text)
    return returncode


def _prepare(steamcmd: Path, account: str, on_line: Callable[[str], None],
             cancel_event: threading.Event | None) -> None:
    """登录一次即退出，让 SteamCMD 取回应用信息；用已设置的账号登录，免得匿名登录影响账号的登录缓存。
    失败（如账号登录已失效）不处理，交给随后的正式任务按原有逻辑报告。"""
    returncode = _run_once([str(steamcmd), "+login", account or "anonymous", "+quit"], on_line, cancel_event)
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("SteamCMD 初始化已取消")
    if returncode == LOGIN_FAILED_EXIT_CODE:
        _record_login(steamcmd, account, returncode)  # 启动时的后台准备就能发现登录失效


@_exclusive_task
def prepare_steamcmd(
    steamcmd: Path,
    *,
    on_line: Callable[[str], None],
    account: str = "",
    cancel_event: threading.Event | None = None,
) -> bool:
    """首次使用前在后台准备 SteamCMD，返回是否已准备好；已准备过直接返回。"""
    if steamcmd_needs_prepare(steamcmd):
        _prepare(steamcmd, account, on_line, cancel_event)
    return not steamcmd_needs_prepare(steamcmd)



def _manifest_stuck_at_update_state_6(manifest: Path) -> bool:
    """appmanifest 的 StateFlags 或 UpdateResult 为 6 时视为上次更新失败卡住（社区常见 0x6 循环）。"""
    if not manifest.is_file():
        return False
    from dstools.features.local_service.steam_client_updater import _parse_scalar_manifest
    values = _parse_scalar_manifest(manifest)
    for key in ("stateflags", "updateresult"):
        raw = values.get(key)
        if raw is None:
            continue
        try:
            if int(raw) == 6:
                return True
        except (TypeError, ValueError):
            continue
    return False


def clear_steamapps_if_manifest_stuck(
    install_dir: Path, app_id: str = DEDICATED_SERVER_APP_ID,
) -> bool:
    """仅当 force_install_dir 下 appmanifest 卡住（StateFlags/UpdateResult 为 6）时删除 steamapps。

    正常清单（如 StateFlags=4）保留，避免破坏 SteamCMD 独立安装的版本缓存。
    返回是否删除了目录。
    """
    install_dir = Path(install_dir)
    steamapps = install_dir / "steamapps"
    manifest = steamapps / f"appmanifest_{app_id}.acf"
    if not _manifest_stuck_at_update_state_6(manifest):
        return False
    shutil.rmtree(steamapps, ignore_errors=True)
    return not steamapps.exists()


@_exclusive_task
def run_update(
    steamcmd: Path,
    install_dir: Path,
    *,
    on_line: Callable[[str], None],
    on_retry: Callable[[int], None] | None = None,
    on_suspended: Callable[[], None] | None = None,
    cancel_event: threading.Event | None = None,
    validate: bool = True,
    attempts: int = 2,
    account: str = "",
) -> SteamAppSnapshot:
    """运行 SteamCMD 直到清单显示安装完整；全部失败抛 RuntimeError，账号登录失败抛 SteamcmdLoginError，
    取消抛 InterruptedError。任务被挂起时先回调 on_suspended（之后约 2 分钟没有输出），本次按失败处理。"""
    # 开始前：上次失败留下 StateFlags/UpdateResult=6 时清掉 steamapps，否则本轮可能直接 0x6
    clear_steamapps_if_manifest_stuck(install_dir)
    if steamcmd_needs_prepare(steamcmd):
        on_line(t("steamcmd.preparing"))
        _prepare(steamcmd, account, on_line, cancel_event)
    command = build_command(steamcmd, install_dir, validate=validate, account=account)
    content_log = Path(steamcmd).parent / "logs" / "content_log.txt"
    returncode = None
    suspended = threading.Event()
    for attempt in range(1, max(1, attempts) + 1):
        # 被挂起时 on_suspended 已说明会自动重试，不再报"未完成（退出码 0）"这种自相矛盾的提示
        if attempt > 1 and on_retry is not None and not suspended.is_set():
            on_retry(returncode)
        # 任务被挂起时 SteamCMD 会空等约 2 分钟再假报成功：记下来，结束后按失败重试
        suspended = threading.Event()
        watch_stop = threading.Event()

        def mark_suspended(_line: str) -> None:
            if not suspended.is_set():
                suspended.set()
                if on_suspended is not None:
                    on_suspended()

        watcher = threading.Thread(
            target=_follow_log, args=(content_log, is_suspended_log_line, mark_suspended, watch_stop), daemon=True)
        watcher.start()
        try:
            returncode = _run_once(command, on_line, cancel_event)
        finally:
            watch_stop.set()
            watcher.join(3)
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("SteamCMD 更新已取消")
        snapshot = snapshot_install(install_dir)
        if not suspended.is_set() and update_succeeded(returncode, snapshot):
            _record_login(steamcmd, account, None)
            return snapshot
        if account and returncode == LOGIN_FAILED_EXIT_CODE:
            _record_login(steamcmd, account, returncode)
            raise SteamcmdLoginError(f"SteamCMD 退出码 {returncode}")
    manifest = Path(install_dir) / "steamapps" / f"appmanifest_{DEDICATED_SERVER_APP_ID}.acf"
    if _manifest_stuck_at_update_state_6(manifest):
        on_line(t("steamcmd.manifest_stuck_after_update",
                  path=str(Path(install_dir) / "steamapps")))
    if suspended.is_set():
        raise RuntimeError("更新任务被 Steam 挂起")
    raise RuntimeError(f"SteamCMD 退出码 {returncode}")


WORKSHOP_APP_ID = "322330"
_WORKSHOP_BATCH = 50  # 每次 SteamCMD 下载的项目数，控制命令行长度，也让中途失败只影响一批


def has_workshop_files(content_dir: Path) -> bool:
    """V2 解压后有 modinfo.lua，V1 是 *_legacy.bin（启动前由 DSTCamp 部署）。"""
    try:
        return (content_dir / "modinfo.lua").is_file() or any(
            item.is_file() for item in content_dir.glob("*_legacy.bin"))
    except OSError:
        return False


@_exclusive_task
def download_workshop_items(
    steamcmd: Path,
    workshop_ids,
    *,
    on_line: Callable[[str], None],
    account: str = "",
    cancel_event: threading.Event | None = None,
) -> dict[str, int]:
    """用 SteamCMD 下载/更新工坊 Mod 到它自己的工坊目录，返回 {工坊 ID: 清单里的更新时间}，没装上的为 0。

    成败按 appworkshop_322330.acf 与实际文件判断，不看输出文字；是否已是最新由调用方对照 Web API。
    账号登录失效时抛 SteamcmdLoginError，取消抛 InterruptedError。
    """
    from dstools.features.mod.workshop_acf import read_acf_time_updated

    ids = [str(wid) for wid in dict.fromkeys(workshop_ids) if str(wid).isdigit()]
    content_root = Path(steamcmd).parent / "steamapps" / "workshop" / "content" / WORKSHOP_APP_ID
    if ids and steamcmd_needs_prepare(steamcmd):
        on_line(t("steamcmd.preparing"))
        _prepare(steamcmd, account, on_line, cancel_event)
    for start in range(0, len(ids), _WORKSHOP_BATCH):
        command = [str(steamcmd), "+login", account or "anonymous"]
        for wid in ids[start:start + _WORKSHOP_BATCH]:
            command += ["+workshop_download_item", WORKSHOP_APP_ID, wid]
        command.append("+quit")
        returncode = _run_once(command, on_line, cancel_event, log_name="workshop_log.txt",
                               log_filter=is_workshop_log_line, follow_log_with_pty=True)
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("SteamCMD 下载已取消")
        if account and returncode == LOGIN_FAILED_EXIT_CODE:
            _record_login(steamcmd, account, returncode)
            raise SteamcmdLoginError(f"SteamCMD 退出码 {returncode}")
    if ids:
        _record_login(steamcmd, account, None)
    installed = read_acf_time_updated(content_root)
    return {wid: installed.get(int(wid), 0) if has_workshop_files(content_root / wid) else 0 for wid in ids}


_GUARD_MARKERS = ("Steam Guard code", "Two-factor code", "Account Logon Denied")


def needs_guard_code(lines: list[str]) -> bool:
    """登录输出里出现 Steam 令牌验证码提示（登录相关输出是英文，真机实测邮箱令牌；手机令牌为 Two-factor code）。"""
    return any(marker in line for line in lines for marker in _GUARD_MARKERS)


@_exclusive_task
def login(
    steamcmd: Path,
    username: str,
    password: str,
    guard_code: str = "",
    *,
    on_line: Callable[[str], None],
    cancel_event: threading.Event | None = None,
) -> int:
    """用账号密码登录一次，成功后 SteamCMD 会缓存凭据，之后只传用户名即可；返回退出码（0 为成功）。

    密码和令牌验证码经标准输入传入，不出现在命令行里，也不转发到输出。
    """
    secrets = [value for value in (password, guard_code) if value]

    def safe_line(line: str) -> None:
        if not any(secret in line for secret in secrets):
            on_line(line)

    stdin_text = password + "\n" + (guard_code + "\n" if guard_code else "")
    console_log = Path(steamcmd).parent / "logs" / "console_log.txt"
    try:
        log_start = console_log.stat().st_size
    except OSError:
        log_start = 0
    returncode = _run_once([str(steamcmd), "+login", username, "+quit"], safe_line, cancel_event,
                           stdin_text=stdin_text)
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("SteamCMD 登录已取消")
    if returncode == LOGIN_FAILED_EXIT_CODE:
        # 坑：伪控制台里不换行的验证码提示要等关闭才送出，等不到提示就被中止时输出里没有它；
        # console_log 是实时写的，从这里补上验证码相关的行，供 needs_guard_code 判断
        try:
            with console_log.open("rb") as file:
                file.seek(log_start)
                tail = file.read().decode("utf-8", errors="replace")
        except OSError:
            tail = ""
        for line in tail.splitlines():
            if any(marker in line for marker in _GUARD_MARKERS):
                safe_line(line.strip())
    if returncode == 0:
        _record_login(steamcmd, username, None)
    return returncode
