"""Steam Workshop 的 ctypes 封装，只用 DST 自带的 ``steam_api64.dll``（普通 SteamUGC）。

坑：下载必须持续泵送 SteamAPI_RunCallbacks，否则不会推进；只有状态稳定为
Installed 后才读安装路径，避免读到半成品。
"""

from __future__ import annotations

import atexit
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from dstools.features.mod.workshop_manifest import verify_mod_manifest


DST_APP_ID = 322330

# SteamAPI_Init 从当前工作目录读 steam_appid.txt（在 bin64 旁），只在初始化瞬间切换目录，进程级锁防止并发切换
_STEAM_INIT_CWD_LOCK = threading.Lock()

ITEM_SUBSCRIBED = 1 << 0
ITEM_LEGACY = 1 << 1
ITEM_INSTALLED = 1 << 2
ITEM_NEEDS_UPDATE = 1 << 3
ITEM_DOWNLOADING = 1 << 4
ITEM_DOWNLOAD_PENDING = 1 << 5


class WorkshopUpdateCancelled(RuntimeError):
    """用户主动停止了独立 Workshop 更新进程。"""


@dataclass(frozen=True)
class WorkshopItemState:
    """对 Steam ``EItemState`` 的稳定快照。"""

    flags: int

    @property
    def subscribed(self) -> bool:
        return bool(self.flags & ITEM_SUBSCRIBED)

    @property
    def installed(self) -> bool:
        return bool(self.flags & ITEM_INSTALLED)

    @property
    def legacy_item(self) -> bool:
        return bool(self.flags & ITEM_LEGACY)

    @property
    def needs_update(self) -> bool:
        return bool(self.flags & ITEM_NEEDS_UPDATE)

    @property
    def downloading(self) -> bool:
        return bool(self.flags & ITEM_DOWNLOADING)

    @property
    def download_pending(self) -> bool:
        return bool(self.flags & ITEM_DOWNLOAD_PENDING)


@dataclass(frozen=True)
class WorkshopPreview:
    """Workshop 查询返回的一个附加预览。"""

    url: str
    original_filename: str
    preview_type: int


@dataclass(frozen=True)
class WorkshopItemDetails:
    """源端 Workshop 项目的只读详情，不混入本地安装状态。"""

    workshop_id: int
    result: int
    title: str = ""
    creator_app_id: int = 0
    consumer_app_id: int = 0
    time_created: int = 0
    time_updated: int = 0
    file_size: int = 0
    content_handle: int = 0
    filename: str = ""
    tags: tuple[str, ...] = ()
    metadata: str = ""
    key_value_tags: tuple[tuple[str, str], ...] = ()
    previews: tuple[WorkshopPreview, ...] = ()


@dataclass(frozen=True)
class WorkshopInstallInfo:
    """Steam 客户端记录的本地安装信息；路径仍需额外做物理检查。"""

    path: Path
    size_on_disk: int
    timestamp: int


class _SteamUGCQueryCompleted(ctypes.Structure):
    """SteamUGCQueryCompleted_t（回调号 3401）。"""

    _fields_ = [
        ("handle", ctypes.c_uint64),
        ("result", ctypes.c_int32),
        ("num_results_returned", ctypes.c_uint32),
        ("total_matching_results", ctypes.c_uint32),
        ("cached_data", ctypes.c_bool),
        ("next_cursor", ctypes.c_char * 256),
    ]


class _RemoteStorageSubscribePublishedFileResult(ctypes.Structure):
    """RemoteStorageSubscribePublishedFileResult_t（回调号 1300 + 13）。"""

    _fields_ = [
        ("result", ctypes.c_int32),
        ("published_file_id", ctypes.c_uint64),
    ]


_STEAM_UGC_QUERY_COMPLETED_CALLBACK = 3401
_REMOTE_STORAGE_SUBSCRIBE_RESULT_CALLBACK = 1313
_STEAM_RESULT_OK = 1
_STEAM_RESULT_ACCESS_DENIED = 15
_UGC_DETAILS_BUFFER_SIZE = 32768


def _decode_c_string(data: bytes) -> str:
    return data.split(b"\0", 1)[0].decode("utf-8", errors="replace")


def _parse_ugc_details_buffer(raw: bytes) -> WorkshopItemDetails:
    """读取 SteamUGCDetails_t 的稳定前缀字段。

    v015 之后结构尾部可能扩展，原生调用用宽裕的原始缓冲区，这里只按 SDK 长期稳定的偏移取值，
    避免 ctypes 结构尺寸落后于用户 DLL 时越界写入。
    """
    import struct

    if len(raw) < 9764:
        raise ValueError("SteamUGCDetails_t 返回缓冲区过短")
    # 按 8 字节对齐：三个 bool 在 8184..8186，m_rgchTags[1025] 从 8187 开始，主文件句柄从 9216 开始。
    # 坑：漏算 bool/padding 时标签从字符串中间读，Klei 的 ``version:x.y.z`` 标签丢失
    tags_text = _decode_c_string(raw[8187:9212])
    content_handle = struct.unpack_from("<Q", raw, 9216)[0]
    file_size = max(0, struct.unpack_from("<i", raw, 9492)[0]) if content_handle else 0
    # 目录式项目通常不填旧版主文件句柄/文件名/大小，缓冲区也不保证清零，没有句柄/大小时必须忽略
    filename = _decode_c_string(raw[9232:9492]) if content_handle else ""
    return WorkshopItemDetails(
        workshop_id=struct.unpack_from("<Q", raw, 0)[0],
        result=struct.unpack_from("<i", raw, 8)[0],
        creator_app_id=struct.unpack_from("<I", raw, 16)[0],
        consumer_app_id=struct.unpack_from("<I", raw, 20)[0],
        title=_decode_c_string(raw[24:153]),
        time_created=struct.unpack_from("<I", raw, 8168)[0],
        time_updated=struct.unpack_from("<I", raw, 8172)[0],
        content_handle=content_handle,
        filename=filename,
        file_size=file_size,
        tags=tuple(tag.strip() for tag in tags_text.split(",") if tag.strip()),
    )


def workshop_source_error(details: WorkshopItemDetails | None) -> str:
    """把源端详情的逐项目 EResult 转成可操作的错误说明。"""
    if details is None or details.result == _STEAM_RESULT_OK:
        return ""
    if details.result == _STEAM_RESULT_ACCESS_DENIED:
        return (
            "Steam 源端不可用（EResult=15：拒绝访问；项目可能已下架、"
            "被设为私密、被平台移除，或当前账号无权访问）"
        )
    return f"Steam 源端无法取得此项目（EResult={details.result}）"


def workshop_version_from_details(details: WorkshopItemDetails | None) -> str:
    """提取 Klei 随 Workshop 项目发布的 ``version:<值>`` 标签。"""
    if details is None or details.result != _STEAM_RESULT_OK:
        return ""
    for tag in details.tags:
        key, separator, value = str(tag).partition(":")
        if separator and key.strip().casefold() == "version":
            return value.strip()
    return ""


@dataclass
class WorkshopDownloadResult:
    """一次更新请求的可展示结果。"""

    workshop_id: int
    accepted: bool = False
    completed: bool = False
    up_to_date: bool = False
    installed_path: Path | None = None
    state: WorkshopItemState | None = None
    downloaded_bytes: int | None = None
    total_bytes: int | None = None
    error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkshopInstallValidation:
    """Workshop 安装目录的落盘验收结果。"""

    valid: bool
    path: Path | None = None
    error: str = ""
    warning: str = ""


def validate_workshop_install(
    path: Path | None, *, legacy_item: bool = False
) -> WorkshopInstallValidation:
    """确认 Mod 已真实落盘，并验证游戏 Manifest 声明的文件。"""
    if path is None:
        return WorkshopInstallValidation(False, error="Steam 没有返回安装路径")
    path = Path(path)
    if legacy_item:
        from dstools.features.mod.legacy_v1 import validate_legacy_package

        validation = validate_legacy_package(path)
        return WorkshopInstallValidation(
            validation.valid, path, "" if validation.valid else validation.error
        )
    if not path.is_dir():
        return WorkshopInstallValidation(False, path, "Steam 返回的安装目录不存在")
    if not (path / "modinfo.lua").is_file():
        return WorkshopInstallValidation(False, path, "安装目录缺少 modinfo.lua")
    manifest = verify_mod_manifest(path)
    if manifest.available and manifest.valid is False:
        return WorkshopInstallValidation(
            True, path, warning=manifest.error or "mod.manifest 完整性无法确认"
        )
    return WorkshopInstallValidation(True, path)


@dataclass
class WorkshopBatchResult:
    """批量更新结果，保持输入顺序，便于 UI 展示逐项状态。"""

    results: list[WorkshopDownloadResult] = field(default_factory=list)

    @property
    def succeeded(self) -> int:
        return sum(item.completed for item in self.results)

    @property
    def updated(self) -> int:
        """实际发生下载/更新的项目数，不包含本来就是最新的项目。"""
        return sum(item.completed and not item.up_to_date for item in self.results)

    @property
    def up_to_date(self) -> int:
        """预检确认无需下载的项目数。"""
        return sum(item.up_to_date for item in self.results)

    @property
    def failed(self) -> int:
        return sum(not item.completed for item in self.results)


_WORKSHOP_WORKER_FLAG = "--dstcamp-workshop-worker"

# 坑：onefile 下 Worker 复用主进程的 _MEI 目录，主程序退出时 Worker 仍在跑会锁住目录，
# bootloader 弹出 "Failed to remove temporary directory"，所以退出时统一结束存活的 Worker
_ACTIVE_WORKERS: set[subprocess.Popen] = set()
_ACTIVE_WORKERS_LOCK = threading.Lock()


def _terminate_active_workers() -> None:
    with _ACTIVE_WORKERS_LOCK:
        workers = list(_ACTIVE_WORKERS)
    for process in workers:
        try:
            process.kill()
            process.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass


atexit.register(_terminate_active_workers)

# Worker 临时目录前缀；TemporaryDirectory 会在后面追加 8 位随机字符
_WORKER_TMP_PREFIX = "dstcamp_workshop_"
_WORKER_TMP_NAME_RE = re.compile(rf"^{_WORKER_TMP_PREFIX}[a-z0-9_]{{8}}$")


def cleanup_stale_worker_dirs() -> None:
    """启动时尽力清理残留的 Worker 临时目录（退出时 Worker 仍占用 events.jsonl 导致删不掉的）。"""
    try:
        entries = list(Path(tempfile.gettempdir()).iterdir())
    except OSError:
        return
    for entry in entries:
        if _WORKER_TMP_NAME_RE.fullmatch(entry.name) and entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)


def _download_result_to_payload(result: WorkshopDownloadResult) -> dict[str, Any]:
    return {
        "workshop_id": result.workshop_id,
        "accepted": result.accepted,
        "completed": result.completed,
        "up_to_date": result.up_to_date,
        "installed_path": str(result.installed_path) if result.installed_path else None,
        "state": result.state.flags if result.state is not None else None,
        "downloaded_bytes": result.downloaded_bytes,
        "total_bytes": result.total_bytes,
        "error": result.error,
        "details": result.details,
    }


def _download_result_from_payload(payload: dict[str, Any]) -> WorkshopDownloadResult:
    state = payload.get("state")
    return WorkshopDownloadResult(
        int(payload["workshop_id"]),
        accepted=bool(payload.get("accepted")),
        completed=bool(payload.get("completed")),
        up_to_date=bool(payload.get("up_to_date")),
        installed_path=(
            Path(payload["installed_path"]) if payload.get("installed_path") else None
        ),
        state=WorkshopItemState(int(state)) if state is not None else None,
        downloaded_bytes=payload.get("downloaded_bytes"),
        total_bytes=payload.get("total_bytes"),
        error=payload.get("error"),
        details=dict(payload.get("details") or {}),
    )


def _snapshot_to_payload(
    states: dict[int, WorkshopItemState],
    installs: dict[int, WorkshopInstallInfo],
    details: dict[int, WorkshopItemDetails],
) -> dict[str, Any]:
    return {
        "states": {str(wid): state.flags for wid, state in states.items()},
        "installs": {
            str(wid): {
                "path": str(info.path),
                "size_on_disk": info.size_on_disk,
                "timestamp": info.timestamp,
            }
            for wid, info in installs.items()
        },
        "details": {str(wid): asdict(item) for wid, item in details.items()},
    }


def _snapshot_from_payload(
    payload: dict[str, Any],
) -> tuple[
    dict[int, WorkshopItemState],
    dict[int, WorkshopInstallInfo],
    dict[int, WorkshopItemDetails],
]:
    states = {
        int(wid): WorkshopItemState(int(flags))
        for wid, flags in (payload.get("states") or {}).items()
    }
    installs = {
        int(wid): WorkshopInstallInfo(
            Path(item["path"]), int(item["size_on_disk"]), int(item["timestamp"])
        )
        for wid, item in (payload.get("installs") or {}).items()
    }
    details = {}
    for wid, item in (payload.get("details") or {}).items():
        decoded = dict(item)
        decoded["tags"] = tuple(decoded.get("tags") or ())
        decoded["key_value_tags"] = tuple(
            tuple(pair) for pair in decoded.get("key_value_tags") or ()
        )
        decoded["previews"] = tuple(
            WorkshopPreview(**preview) for preview in decoded.get("previews") or ()
        )
        details[int(wid)] = WorkshopItemDetails(**decoded)
    return states, installs, details


def _workshop_worker_command(
    request_path: Path, event_path: Path, result_path: Path
) -> list[str]:
    args = [str(request_path), str(event_path), str(result_path)]
    if getattr(sys, "frozen", False):
        return [sys.executable, _WORKSHOP_WORKER_FLAG, *args]
    return [sys.executable, "-m", "dstools.features.mod.workshop_worker", *args]


def _run_workshop_worker(
    request: dict[str, Any],
    on_event: Callable[[dict[str, Any]], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """在短生命周期子进程中运行普通 SteamAPI，避免占用游戏 AppID。"""
    from dstools.shared.resource_paths import bundled_runtime_damaged

    # 解压目录被清理后 Worker 起不来，还会弹启动器错误框（见 bundled_runtime_damaged）
    if bundled_runtime_damaged():
        raise RuntimeError("DSTCamp 的临时运行文件已被清理软件或系统删除，请重启 DSTCamp 后重试")
    # 查询中途退出程序时 events.jsonl 仍被占用，删不掉就留给下次启动清理，不打印报错
    with tempfile.TemporaryDirectory(prefix=_WORKER_TMP_PREFIX, ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        request_path = root / "request.json"
        event_path = root / "events.jsonl"
        result_path = root / "result.json"
        request_path.write_text(
            json.dumps(request, ensure_ascii=False), encoding="utf-8"
        )
        event_path.touch()
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        # 源码版需要仓库根目录才能 -m 导入；冻结版 parents[3] 恰好是 _MEI
        # 根目录，进程工作目录会占住它，改用本次的临时目录。
        worker_cwd = root if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[3]
        process = subprocess.Popen(
            _workshop_worker_command(request_path, event_path, result_path),
            cwd=str(worker_cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        with _ACTIVE_WORKERS_LOCK:
            _ACTIVE_WORKERS.add(process)
        try:
            return _collect_workshop_worker(
                process, event_path, result_path, on_event, cancel_event
            )
        finally:
            with _ACTIVE_WORKERS_LOCK:
                _ACTIVE_WORKERS.discard(process)


def _collect_workshop_worker(
    process: subprocess.Popen,
    event_path: Path,
    result_path: Path,
    on_event: Callable[[dict[str, Any]], None] | None,
    cancel_event: threading.Event | None,
) -> dict[str, Any]:
    """转发 Worker 事件直到其退出，并读取结果文件。"""
    with event_path.open("r", encoding="utf-8") as events:
        while process.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                try:
                    process.terminate()
                except OSError:
                    # Worker 可能恰好在 poll() 后退出；停止意图仍然成立。
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                        process.wait(timeout=5)
                    except OSError:
                        pass
                raise WorkshopUpdateCancelled("Workshop 更新已停止")
            line = events.readline()
            if line:
                if on_event:
                    on_event(json.loads(line))
            else:
                time.sleep(0.05)
        for line in events:
            if on_event:
                on_event(json.loads(line))
    if not result_path.is_file():
        raise RuntimeError(f"Steam Worker 异常退出（exit={process.returncode}）")
    response = json.loads(result_path.read_text(encoding="utf-8"))
    if not response.get("ok"):
        raise RuntimeError(response.get("error") or "Steam Worker 执行失败")
    return dict(response.get("result") or {})


def _load_dll(path: Path):
    """加载 DLL 并保留依赖目录句柄直到会话关闭。"""
    if sys.platform != "win32":
        raise RuntimeError("Steam Workshop 下载当前只支持 Windows")
    if not path.is_file():
        raise FileNotFoundError(f"找不到 Steam API DLL：{path}")
    add_dll_directory = getattr(os, "add_dll_directory", None)
    directory_handle = (
        add_dll_directory(str(path.parent)) if add_dll_directory else None
    )
    try:
        return ctypes.WinDLL(str(path)), directory_handle
    except Exception:
        if directory_handle is not None:
            directory_handle.close()
        raise


def find_steam_api_dll(explicit: Path | None = None) -> Path | None:
    """查找可用的 ``steam_api64.dll``：显式路径 > 当前开服程序自带 > 各 Steam 库的游戏/专服。"""
    if explicit:
        candidate = explicit.expanduser()
        return candidate if candidate.is_file() else None

    candidates: list[Path] = []
    # 延迟导入，避免 Mod 页签构造时加载本地服务器模块。
    try:
        from dstools.features.local_service.server_runtime import current_runtime

        runtime = current_runtime()
        if runtime is not None and runtime.steam_api_dll is not None:
            candidates.append(runtime.steam_api_dll)
    except Exception:
        pass
    try:
        from dstools.shared.steam_discovery import find_all_steam_libraries

        for library in find_all_steam_libraries():
            candidates.extend(
                (
                    library
                    / "steamapps"
                    / "common"
                    / "Don't Starve Together"
                    / "bin64"
                    / "steam_api64.dll",
                    library
                    / "steamapps"
                    / "common"
                    / "Don't Starve Together Dedicated Server"
                    / "bin64"
                    / "steam_api64.dll",
                )
            )
    except Exception:
        pass
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_file():
            return candidate
    return None


class SteamWorkshopSession:
    """短生命周期的 Steam Workshop 会话：在后台线程里每批次开一次，用完立即 Shutdown，
    避免和游戏/专服长期抢占 Steam API 状态。"""

    def __init__(self, dll_path: Path, *, app_id: int = DST_APP_ID):
        self.dll_path = Path(dll_path)
        self.app_id = int(app_id)
        self.dll = None
        self.ugc = None
        self.utils = None
        self._dll_directory_handle = None
        self._started = False
        self._native_initialized = False

    def __enter__(self) -> "SteamWorkshopSession":
        try:
            self.start()
        except Exception:
            self.close()
            raise
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _require(self, *names: str) -> None:
        missing = [name for name in names if not hasattr(self.dll, name)]
        if missing:
            raise RuntimeError("Steam API 缺少导出：" + ", ".join(missing))

    def start(self) -> None:
        if self._started:
            return
        self.dll, self._dll_directory_handle = _load_dll(self.dll_path)
        self._require(
            "SteamAPI_Init",
            "SteamAPI_Shutdown",
            "SteamAPI_RunCallbacks",
            "SteamAPI_SteamUGC_v015",
            "SteamAPI_ISteamUGC_GetItemState",
            "SteamAPI_ISteamUGC_DownloadItem",
            "SteamAPI_ISteamUGC_GetItemDownloadInfo",
            "SteamAPI_ISteamUGC_GetItemInstallInfo",
        )
        self.dll.SteamAPI_Init.restype = ctypes.c_bool
        if not self._init_steam_api(self.dll.SteamAPI_Init):
            raise RuntimeError("SteamAPI_Init 失败：当前进程没有有效的 Steam/DST 应用上下文")
        self._native_initialized = True
        self.dll.SteamAPI_SteamUGC_v015.restype = ctypes.c_void_p
        self.ugc = self.dll.SteamAPI_SteamUGC_v015()
        if not self.ugc:
            raise RuntimeError("SteamAPI_SteamUGC_v015 返回空接口")
        self._configure_ugc_calls()
        self._started = True

    def _init_steam_api(self, initializer: Callable[[], Any]) -> bool:
        """切到 DLL 目录初始化 Steam API 后恢复工作目录。

        客户端 bin64 没有 steam_appid.txt，需临时设置 ``SteamAppId`` 环境变量（真机核对：
        不设时 SteamAPI_Init 失败），初始化后恢复，避免影响之后的子进程。"""
        previous = os.getcwd()
        need_app_id = not (self.dll_path.parent / "steam_appid.txt").is_file()
        with _STEAM_INIT_CWD_LOCK:
            previous_app_id = os.environ.get("SteamAppId")
            try:
                os.chdir(str(self.dll_path.parent))
                if need_app_id:
                    os.environ["SteamAppId"] = str(self.app_id)
                return bool(initializer())
            finally:
                os.chdir(previous)
                if need_app_id:
                    if previous_app_id is None:
                        os.environ.pop("SteamAppId", None)
                    else:
                        os.environ["SteamAppId"] = previous_app_id

    def _configure_ugc_calls(self) -> None:
        self.dll.SteamAPI_ISteamUGC_GetItemState.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint64,
        ]
        self.dll.SteamAPI_ISteamUGC_GetItemState.restype = ctypes.c_uint32
        self.dll.SteamAPI_ISteamUGC_DownloadItem.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint64,
            ctypes.c_bool,
        ]
        self.dll.SteamAPI_ISteamUGC_DownloadItem.restype = ctypes.c_bool
        self.dll.SteamAPI_ISteamUGC_GetItemDownloadInfo.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint64,
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint64),
        ]
        self.dll.SteamAPI_ISteamUGC_GetItemDownloadInfo.restype = ctypes.c_bool
        self.dll.SteamAPI_ISteamUGC_GetItemInstallInfo.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint64,
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        self.dll.SteamAPI_ISteamUGC_GetItemInstallInfo.restype = ctypes.c_bool

    def subscribed_item_ids(
        self, *, include_locally_disabled: bool = True
    ) -> list[int]:
        """枚举当前 Steam 账号为 DST 订阅的全部 Workshop 项目。

        不依赖 ACF 和内容目录，是发现"仍在订阅但文件与 ACF 都丢失"项目的唯一入口。
        额外的 bool 参数在新版 Steamworks 中表示包含本地禁用项，旧版 v015 会忽略。
        """
        self._ensure_started()
        self._require(
            "SteamAPI_ISteamUGC_GetNumSubscribedItems",
            "SteamAPI_ISteamUGC_GetSubscribedItems",
        )
        get_count = self.dll.SteamAPI_ISteamUGC_GetNumSubscribedItems
        get_count.argtypes = [ctypes.c_void_p, ctypes.c_bool]
        get_count.restype = ctypes.c_uint32
        get_items = self.dll.SteamAPI_ISteamUGC_GetSubscribedItems
        get_items.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.c_uint32,
            ctypes.c_bool,
        ]
        get_items.restype = ctypes.c_uint32
        count = int(get_count(self.ugc, bool(include_locally_disabled)))
        if count <= 0:
            return []
        buffer = (ctypes.c_uint64 * count)()
        written = min(
            count,
            int(get_items(self.ugc, buffer, count, bool(include_locally_disabled))),
        )
        return list(
            dict.fromkeys(
                int(buffer[index]) for index in range(written) if int(buffer[index]) > 0
            )
        )

    def subscribe_item(self, workshop_id: int, *, timeout: float = 30.0) -> int:
        """以当前 Steam 账号订阅 Workshop 项目，返回 Steam 的 EResult（1 为成功）。"""
        self._ensure_started()
        names = (
            "SteamAPI_ISteamUGC_SubscribeItem",
            "SteamAPI_SteamUtils_v010",
            "SteamAPI_ISteamUtils_IsAPICallCompleted",
            "SteamAPI_ISteamUtils_GetAPICallResult",
        )
        self._require(*names)
        d = self.dll
        if not self.utils:
            d.SteamAPI_SteamUtils_v010.restype = ctypes.c_void_p
            self.utils = d.SteamAPI_SteamUtils_v010()
            if not self.utils:
                raise RuntimeError("SteamAPI_SteamUtils_v010 返回空接口")
        d.SteamAPI_ISteamUGC_SubscribeItem.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        d.SteamAPI_ISteamUGC_SubscribeItem.restype = ctypes.c_uint64
        d.SteamAPI_ISteamUtils_IsAPICallCompleted.argtypes = [
            ctypes.c_void_p, ctypes.c_uint64, ctypes.POINTER(ctypes.c_bool),
        ]
        d.SteamAPI_ISteamUtils_IsAPICallCompleted.restype = ctypes.c_bool
        d.SteamAPI_ISteamUtils_GetAPICallResult.argtypes = [
            ctypes.c_void_p, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int,
            ctypes.c_int, ctypes.POINTER(ctypes.c_bool),
        ]
        d.SteamAPI_ISteamUtils_GetAPICallResult.restype = ctypes.c_bool
        api_call = int(d.SteamAPI_ISteamUGC_SubscribeItem(self.ugc, int(workshop_id)))
        if not api_call:
            raise RuntimeError("Steam 拒绝了订阅请求")
        run_callbacks = d.SteamAPI_RunCallbacks
        run_callbacks.restype = None
        io_failed = ctypes.c_bool()
        deadline = time.monotonic() + max(0.1, timeout)
        while time.monotonic() < deadline:
            run_callbacks()
            if d.SteamAPI_ISteamUtils_IsAPICallCompleted(self.utils, api_call, ctypes.byref(io_failed)):
                break
            time.sleep(0.05)
        else:
            raise TimeoutError("等待 Steam 订阅结果超时")
        completed = _RemoteStorageSubscribePublishedFileResult()
        result_failed = ctypes.c_bool()
        ok = d.SteamAPI_ISteamUtils_GetAPICallResult(
            self.utils, api_call, ctypes.byref(completed), ctypes.sizeof(completed),
            _REMOTE_STORAGE_SUBSCRIBE_RESULT_CALLBACK, ctypes.byref(result_failed),
        )
        if not ok or io_failed.value or result_failed.value:
            raise RuntimeError("Steam 订阅请求发生 IO 错误")
        return int(completed.result)

    def item_state(self, workshop_id: int) -> WorkshopItemState:
        self._ensure_started()
        return WorkshopItemState(
            int(self.dll.SteamAPI_ISteamUGC_GetItemState(self.ugc, int(workshop_id)))
        )

    def query_item_details(
        self, workshop_ids: list[int] | tuple[int, ...], *, timeout: float = 20.0
    ) -> list[WorkshopItemDetails]:
        """批量查询最多50个 Workshop 项目的源端详情，不触发下载。"""
        self._ensure_started()
        ids = [int(item) for item in workshop_ids if int(item) > 0]
        if not ids:
            return []
        if len(ids) > 50:
            raise ValueError("单次 Workshop 详情查询不能超过50项")
        query_handle, api_call = self._send_details_query(ids)
        return self._collect_details_query(query_handle, api_call, timeout)

    def query_item_details_batches(
        self, workshop_ids: list[int] | tuple[int, ...], *, timeout: float = 20.0
    ) -> list[WorkshopItemDetails]:
        """按每批50个查询任意数量的项目：所有批次先一起发出再逐个收取，几百个 Mod 只等约一次网络往返。

        某一批发送或收取失败只丢这一批，其余批次的结果照常返回。"""
        self._ensure_started()
        ids = [int(item) for item in workshop_ids if int(item) > 0]
        sent = []
        for start in range(0, len(ids), 50):
            try:
                sent.append(self._send_details_query(ids[start : start + 50]))
            except Exception:
                continue
        details: list[WorkshopItemDetails] = []
        for query_handle, api_call in sent:
            try:
                details.extend(self._collect_details_query(query_handle, api_call, timeout))
            except Exception:
                continue
        return details

    def _send_details_query(self, ids: list[int]) -> tuple[int, int]:
        """创建并发出一批详情查询，返回（查询句柄, API 调用）；调用方必须经 _collect_details_query 释放句柄。"""
        self._configure_query_calls()
        id_array = (ctypes.c_uint64 * len(ids))(*ids)
        query_handle = int(
            self.dll.SteamAPI_ISteamUGC_CreateQueryUGCDetailsRequest(
                self.ugc, id_array, len(ids)
            )
        )
        if query_handle == 0xFFFFFFFFFFFFFFFF:
            raise RuntimeError("CreateQueryUGCDetailsRequest 返回无效句柄")
        try:
            self.dll.SteamAPI_ISteamUGC_SetReturnKeyValueTags(
                self.ugc, query_handle, True
            )
            self.dll.SteamAPI_ISteamUGC_SetReturnMetadata(self.ugc, query_handle, True)
            self.dll.SteamAPI_ISteamUGC_SetReturnAdditionalPreviews(
                self.ugc, query_handle, True
            )
            api_call = int(
                self.dll.SteamAPI_ISteamUGC_SendQueryUGCRequest(self.ugc, query_handle)
            )
            if not api_call:
                raise RuntimeError("SendQueryUGCRequest 返回无效 API 调用")
        except BaseException:
            self.dll.SteamAPI_ISteamUGC_ReleaseQueryUGCRequest(self.ugc, query_handle)
            raise
        return query_handle, api_call

    def _collect_details_query(
        self, query_handle: int, api_call: int, timeout: float
    ) -> list[WorkshopItemDetails]:
        """等待一批详情查询完成并解析结果，无论成败都释放查询句柄。"""
        try:
            completed = self._wait_query_call(api_call, timeout)
            if completed.result != _STEAM_RESULT_OK:
                raise RuntimeError(
                    f"Workshop 源端查询失败：Steam EResult={completed.result}"
                )
            details = []
            for index in range(int(completed.num_results_returned)):
                raw = ctypes.create_string_buffer(_UGC_DETAILS_BUFFER_SIZE)
                if not self.dll.SteamAPI_ISteamUGC_GetQueryUGCResult(
                    self.ugc, query_handle, index, raw
                ):
                    continue
                base = _parse_ugc_details_buffer(raw.raw)
                metadata_buffer = ctypes.create_string_buffer(32768)
                metadata = ""
                if self.dll.SteamAPI_ISteamUGC_GetQueryUGCMetadata(
                    self.ugc, query_handle, index, metadata_buffer, len(metadata_buffer)
                ):
                    metadata = _decode_c_string(metadata_buffer.raw)
                key_values = []
                count = int(
                    self.dll.SteamAPI_ISteamUGC_GetQueryUGCNumKeyValueTags(
                        self.ugc, query_handle, index
                    )
                )
                for tag_index in range(count):
                    key = ctypes.create_string_buffer(1024)
                    value = ctypes.create_string_buffer(8192)
                    if self.dll.SteamAPI_ISteamUGC_GetQueryUGCKeyValueTag(
                        self.ugc,
                        query_handle,
                        index,
                        tag_index,
                        key,
                        len(key),
                        value,
                        len(value),
                    ):
                        key_values.append(
                            (_decode_c_string(key.raw), _decode_c_string(value.raw))
                        )
                previews = []
                preview_count = int(
                    self.dll.SteamAPI_ISteamUGC_GetQueryUGCNumAdditionalPreviews(
                        self.ugc, query_handle, index
                    )
                )
                for preview_index in range(preview_count):
                    url = ctypes.create_string_buffer(4096)
                    filename = ctypes.create_string_buffer(1024)
                    preview_type = ctypes.c_int32()
                    if self.dll.SteamAPI_ISteamUGC_GetQueryUGCAdditionalPreview(
                        self.ugc,
                        query_handle,
                        index,
                        preview_index,
                        url,
                        len(url),
                        filename,
                        len(filename),
                        ctypes.byref(preview_type),
                    ):
                        previews.append(
                            WorkshopPreview(
                                _decode_c_string(url.raw),
                                _decode_c_string(filename.raw),
                                int(preview_type.value),
                            )
                        )
                details.append(
                    WorkshopItemDetails(
                        **{
                            **base.__dict__,
                            "metadata": metadata,
                            "key_value_tags": tuple(key_values),
                            "previews": tuple(previews),
                        }
                    )
                )
            return details
        finally:
            self.dll.SteamAPI_ISteamUGC_ReleaseQueryUGCRequest(self.ugc, query_handle)

    def _configure_query_calls(self) -> None:
        """延迟绑定只读查询接口，普通下载路径不强制依赖这些导出。"""
        names = (
            "SteamAPI_SteamUtils_v010",
            "SteamAPI_ISteamUtils_IsAPICallCompleted",
            "SteamAPI_ISteamUtils_GetAPICallResult",
            "SteamAPI_ISteamUGC_CreateQueryUGCDetailsRequest",
            "SteamAPI_ISteamUGC_SetReturnKeyValueTags",
            "SteamAPI_ISteamUGC_SetReturnMetadata",
            "SteamAPI_ISteamUGC_SetReturnAdditionalPreviews",
            "SteamAPI_ISteamUGC_SendQueryUGCRequest",
            "SteamAPI_ISteamUGC_GetQueryUGCResult",
            "SteamAPI_ISteamUGC_GetQueryUGCNumKeyValueTags",
            "SteamAPI_ISteamUGC_GetQueryUGCKeyValueTag",
            "SteamAPI_ISteamUGC_GetQueryUGCMetadata",
            "SteamAPI_ISteamUGC_GetQueryUGCNumAdditionalPreviews",
            "SteamAPI_ISteamUGC_GetQueryUGCAdditionalPreview",
            "SteamAPI_ISteamUGC_ReleaseQueryUGCRequest",
        )
        self._require(*names)
        d = self.dll
        d.SteamAPI_SteamUtils_v010.restype = ctypes.c_void_p
        self.utils = d.SteamAPI_SteamUtils_v010()
        if not self.utils:
            raise RuntimeError("SteamAPI_SteamUtils_v010 返回空接口")
        d.SteamAPI_ISteamUGC_CreateQueryUGCDetailsRequest.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.c_uint32,
        ]
        d.SteamAPI_ISteamUGC_CreateQueryUGCDetailsRequest.restype = ctypes.c_uint64
        for name in (
            "SteamAPI_ISteamUGC_SetReturnKeyValueTags",
            "SteamAPI_ISteamUGC_SetReturnMetadata",
            "SteamAPI_ISteamUGC_SetReturnAdditionalPreviews",
        ):
            fn = getattr(d, name)
            fn.argtypes = [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_bool]
            fn.restype = ctypes.c_bool
        d.SteamAPI_ISteamUGC_SendQueryUGCRequest.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint64,
        ]
        d.SteamAPI_ISteamUGC_SendQueryUGCRequest.restype = ctypes.c_uint64
        d.SteamAPI_ISteamUtils_IsAPICallCompleted.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint64,
            ctypes.POINTER(ctypes.c_bool),
        ]
        d.SteamAPI_ISteamUtils_IsAPICallCompleted.restype = ctypes.c_bool
        d.SteamAPI_ISteamUtils_GetAPICallResult.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_bool),
        ]
        d.SteamAPI_ISteamUtils_GetAPICallResult.restype = ctypes.c_bool
        d.SteamAPI_ISteamUGC_GetQueryUGCResult.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint64,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        d.SteamAPI_ISteamUGC_GetQueryUGCResult.restype = ctypes.c_bool
        d.SteamAPI_ISteamUGC_GetQueryUGCNumKeyValueTags.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint64,
            ctypes.c_uint32,
        ]
        d.SteamAPI_ISteamUGC_GetQueryUGCNumKeyValueTags.restype = ctypes.c_uint32
        d.SteamAPI_ISteamUGC_GetQueryUGCKeyValueTag.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint64,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
        ]
        d.SteamAPI_ISteamUGC_GetQueryUGCKeyValueTag.restype = ctypes.c_bool
        d.SteamAPI_ISteamUGC_GetQueryUGCMetadata.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint64,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
        ]
        d.SteamAPI_ISteamUGC_GetQueryUGCMetadata.restype = ctypes.c_bool
        d.SteamAPI_ISteamUGC_GetQueryUGCNumAdditionalPreviews.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint64,
            ctypes.c_uint32,
        ]
        d.SteamAPI_ISteamUGC_GetQueryUGCNumAdditionalPreviews.restype = ctypes.c_uint32
        d.SteamAPI_ISteamUGC_GetQueryUGCAdditionalPreview.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint64,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_int32),
        ]
        d.SteamAPI_ISteamUGC_GetQueryUGCAdditionalPreview.restype = ctypes.c_bool
        d.SteamAPI_ISteamUGC_ReleaseQueryUGCRequest.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint64,
        ]
        d.SteamAPI_ISteamUGC_ReleaseQueryUGCRequest.restype = ctypes.c_bool

    def _wait_query_call(
        self, api_call: int, timeout: float
    ) -> _SteamUGCQueryCompleted:
        run_callbacks = self.dll.SteamAPI_RunCallbacks
        run_callbacks.restype = None
        io_failed = ctypes.c_bool()
        deadline = time.monotonic() + max(0.1, timeout)
        while time.monotonic() < deadline:
            run_callbacks()
            if self.dll.SteamAPI_ISteamUtils_IsAPICallCompleted(
                self.utils, api_call, ctypes.byref(io_failed)
            ):
                break
            time.sleep(0.05)
        else:
            raise TimeoutError("等待 Workshop 源端查询超时")
        completed = _SteamUGCQueryCompleted()
        result_failed = ctypes.c_bool()
        ok = self.dll.SteamAPI_ISteamUtils_GetAPICallResult(
            self.utils,
            api_call,
            ctypes.byref(completed),
            ctypes.sizeof(completed),
            _STEAM_UGC_QUERY_COMPLETED_CALLBACK,
            ctypes.byref(result_failed),
        )
        if not ok or io_failed.value or result_failed.value:
            raise RuntimeError("Workshop 源端查询发生 Steam IO 错误")
        return completed

    def download_item(
        self,
        workshop_id: int,
        *,
        high_priority: bool = True,
        expected_version: str = "",
        source_details: WorkshopItemDetails | None = None,
        force_redownload: bool = False,
    ) -> WorkshopDownloadResult:
        self._ensure_started()
        workshop_id = int(workshop_id)
        result = WorkshopDownloadResult(workshop_id)
        result.state = self.item_state(workshop_id)
        install_info = self.item_install_details(workshop_id)
        if not result.state.subscribed:
            result.error = "当前 Steam 账号未订阅此 Mod，已停止更新"
            return result
        if force_redownload and not result.state.legacy_item:
            if not SteamWorkshopSession._delete_install_for_redownload(
                result, install_info.path if install_info is not None else None
            ):
                return result
        if (
            result.state.legacy_item
            and install_info is None
            and source_details is not None
            and source_details.content_handle > 0
        ):
            from dstools.features.mod.parser import find_workshop_dir

            workshop_root = find_workshop_dir()
            if workshop_root is not None:
                legacy_path = (
                    Path(workshop_root)
                    / str(workshop_id)
                    / f"{source_details.content_handle}_legacy.bin"
                )
                install_info = WorkshopInstallInfo(
                    legacy_path,
                    max(0, int(source_details.file_size)),
                    max(0, int(source_details.time_updated)),
                )
                result.details["legacy_path_recovered_from_source"] = True
        if result.state.legacy_item and install_info is not None:
            # 目录里混入其他文件时游戏不会解压 V1 包，先清掉，只留 Steam 记录的压缩包
            from dstools.features.mod.legacy_v1 import (
                record_legacy_extras_removed, remove_legacy_package_extras,
            )

            try:
                removed = remove_legacy_package_extras(install_info.path)
            except OSError as exc:
                result.error = f"无法清理旧式 Mod 目录里的多余文件：{exc}"
                return result
            if removed:
                result.details["legacy_extras_removed"] = [str(path) for path in removed]
                record_legacy_extras_removed(workshop_id, removed, "更新")
        # 文件被手动删除后 Installed 位和安装信息可能仍是旧值，物理目录、modinfo 与
        # Manifest 都通过才跳过 DownloadItem，否则按修复处理
        if (
            not force_redownload
            and result.state.installed
            and not result.state.needs_update
            and not result.state.downloading
            and not result.state.download_pending
        ):
            validation = validate_workshop_install(
                install_info.path if install_info is not None else None,
                legacy_item=result.state.legacy_item,
            )
            if validation.valid:
                if result.state.legacy_item:
                    legacy_result = SteamWorkshopSession._finish_legacy_install(
                        result,
                        validation.path,
                        expected_version=expected_version,
                        force=False,
                    )
                    if legacy_result.completed:
                        return legacy_result
                    # 只有包损坏或版本不匹配才重新拉包；目录占用、无部署目标等错误原样返回，
                    # 不能误入只适用于 V2 目录的 modinfo.lua 强制修复
                    if not legacy_result.details.get("legacy_retry_download"):
                        return legacy_result
                    legacy_result.details["legacy_local_repair_error"] = (
                        legacy_result.error
                    )
                    legacy_result.error = ""
                    if expected_version:
                        legacy_result.details["expected_version"] = str(
                            expected_version
                        ).strip()
                    return self._download_legacy_item(legacy_result, install_info)
                if expected_version:
                    if not SteamWorkshopSession._prepare_forced_version_repair(
                        result, validation.path, expected_version
                    ):
                        return result
                else:
                    result.accepted = True
                    result.completed = True
                    result.up_to_date = True
                    result.installed_path = validation.path
                    result.details["validation"] = "passed"
                    if validation.warning:
                        result.details["validation_warning"] = validation.warning
                    return result
            result.details["repair"] = True
            result.details["precheck_error"] = validation.error
        if result.state.legacy_item:
            if expected_version:
                result.details["expected_version"] = str(expected_version).strip()
            return self._download_legacy_item(result, install_info)
        result.accepted = bool(
            self.dll.SteamAPI_ISteamUGC_DownloadItem(
                self.ugc, workshop_id, bool(high_priority)
            )
        )
        if not result.accepted:
            result.error = "ISteamUGC::DownloadItem 返回 false（项目无效、用户未登录或服务器上下文未就绪）"
            SteamWorkshopSession._finish_forced_version_repair(result, success=False)
        return result

    @staticmethod
    def _delete_install_for_redownload(
        result: WorkshopDownloadResult, install_path: Path | None
    ) -> bool:
        """删除精确匹配的 Workshop 安装目录，让 Steam 重新下载订阅内容。"""
        path = Path(install_path) if install_path is not None else None
        if (
            path is None
            or path.name != str(result.workshop_id)
            or path.parent.name != str(DST_APP_ID)
        ):
            result.error = "Steam 返回的 Mod 安装目录不符合 Workshop 路径，已停止重新下载"
            return False
        try:
            if getattr(os.path, "isjunction", lambda _path: False)(path):
                os.rmdir(path)
            elif path.is_symlink():
                path.unlink()
            elif path.exists():
                shutil.rmtree(path)
        except OSError as exc:
            result.error = f"无法删除疑似过期的 Mod 目录：{exc}"
            return False
        result.details.update(
            {
                "repair": True,
                "forced_redownload": True,
                "deleted_install_path": str(path),
            }
        )
        return True

    @staticmethod
    def _prepare_forced_version_repair(
        result: WorkshopDownloadResult, install_path: Path | None, expected_version: str
    ) -> bool:
        """可恢复地移走 modinfo.lua，让 Steam 对“清单最新但文件被改”重下。"""
        path = Path(install_path) if install_path is not None else None
        if path is None or path.name != str(result.workshop_id):
            result.error = "Steam 返回的 Mod 安装目录不符合 Workshop ID，已停止强制修复"
            return False
        modinfo = path / "modinfo.lua"
        if not modinfo.is_file():
            result.error = "强制修复前找不到 modinfo.lua"
            return False
        backup = path / f"modinfo.lua.dstcamp-update-{os.getpid()}-{time.time_ns()}.bak"
        try:
            modinfo.replace(backup)
        except OSError as exc:
            result.error = f"无法备份本地 modinfo.lua：{exc}"
            return False
        result.details.update(
            {
                "repair": True,
                "version_repair": True,
                "expected_version": str(expected_version).strip(),
                "forced_modinfo_path": str(modinfo),
                "forced_modinfo_backup": str(backup),
            }
        )
        return True

    @staticmethod
    def _finish_forced_version_repair(
        result: WorkshopDownloadResult, *, success: bool
    ) -> None:
        backup_text = result.details.get("forced_modinfo_backup")
        target_text = result.details.get("forced_modinfo_path")
        if not backup_text or not target_text:
            return
        backup = Path(backup_text)
        target = Path(target_text)
        try:
            if success:
                backup.unlink(missing_ok=True)
            elif backup.is_file():
                os.replace(backup, target)
        except OSError as exc:
            result.details["backup_cleanup_error"] = str(exc)

    @staticmethod
    def _downloaded_version_matches(
        result: WorkshopDownloadResult, install_path: Path | None
    ) -> bool:
        expected = str(result.details.get("expected_version") or "").strip()
        if not expected:
            return True
        if install_path is None:
            return False
        from dstools.features.mod.local_version import (
            VERSION_CONFIRMED,
            normalize_version_for_compare,
            resolve_local_mod_version,
        )

        local = resolve_local_mod_version(
            str(result.workshop_id),
            Path(install_path),
            f"workshop-{result.workshop_id}",
        )
        if local.status == VERSION_CONFIRMED and normalize_version_for_compare(
            local.version
        ) == normalize_version_for_compare(expected):
            result.details["installed_version"] = local.version
            return True
        result.details["postcheck_error"] = (
            f"下载后版本仍不匹配：本地 {local.version or '未知'}，远程 {expected}"
        )
        return False

    def _download_legacy_item(
        self, result: WorkshopDownloadResult, install_info: WorkshopInstallInfo | None
    ) -> WorkshopDownloadResult:
        """用旧版 RemoteStorage 接口修复 Legacy（V1）Workshop 文件。

        坑：V1 安装信息返回的是 ``*_legacy.bin`` 文件；文件被删后 Installed 位可能保留，现代
        DownloadItem 会接受请求却永不传输。文件名里包含旧接口需要的 UGCHandle，直接下载到原
        位置，完成后按实际文件大小验收。
        """
        if install_info is None:
            result.error = "Steam 没有返回 Legacy Mod 的安装记录"
            return result
        target = install_info.path
        suffix = "_legacy.bin"
        if not target.name.endswith(suffix):
            result.error = "Steam 返回的 Legacy Mod 文件名无法识别"
            return result
        handle_text = target.name[: -len(suffix)]
        if not handle_text.isdigit():
            result.error = "Steam 返回的 Legacy Mod 内容句柄无效"
            return result
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            result.error = f"无法创建 Legacy Mod 目录：{exc}"
            return result
        self._require(
            "SteamAPI_SteamRemoteStorage_v014",
            "SteamAPI_ISteamRemoteStorage_UGCDownloadToLocation",
            "SteamAPI_SteamUtils_v010",
            "SteamAPI_ISteamUtils_IsAPICallCompleted",
        )
        self.dll.SteamAPI_SteamRemoteStorage_v014.restype = ctypes.c_void_p
        remote_storage = self.dll.SteamAPI_SteamRemoteStorage_v014()
        if not remote_storage:
            result.error = "SteamAPI_SteamRemoteStorage_v014 返回空接口"
            return result
        self.dll.SteamAPI_ISteamRemoteStorage_UGCDownloadToLocation.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint64,
            ctypes.c_char_p,
            ctypes.c_uint32,
        ]
        self.dll.SteamAPI_ISteamRemoteStorage_UGCDownloadToLocation.restype = (
            ctypes.c_uint64
        )
        self.dll.SteamAPI_SteamUtils_v010.restype = ctypes.c_void_p
        self.utils = self.dll.SteamAPI_SteamUtils_v010()
        if not self.utils:
            result.error = "SteamAPI_SteamUtils_v010 返回空接口"
            return result
        self.dll.SteamAPI_ISteamUtils_IsAPICallCompleted.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint64,
            ctypes.POINTER(ctypes.c_bool),
        ]
        self.dll.SteamAPI_ISteamUtils_IsAPICallCompleted.restype = ctypes.c_bool
        temporary = target.with_name(
            f".{target.name}.dstcamp-download-{os.getpid()}-{time.time_ns()}.tmp"
        )
        try:
            temporary.unlink(missing_ok=True)
        except OSError as exc:
            result.error = f"无法清理 Legacy Mod 临时文件：{exc}"
            return result
        api_call = int(
            self.dll.SteamAPI_ISteamRemoteStorage_UGCDownloadToLocation(
                remote_storage, int(handle_text), str(temporary).encode("utf-8"), 0
            )
        )
        if not api_call:
            result.error = "Legacy Workshop 下载请求未能创建"
            return result
        result.accepted = True
        result.details["legacy_api_call"] = api_call
        result.details["legacy_target"] = str(temporary)
        result.details["legacy_final_target"] = str(target)
        return result

    @staticmethod
    def _finish_legacy_install(
        result: WorkshopDownloadResult,
        archive_path: Path | None,
        *,
        expected_version: str = "",
        force: bool,
    ) -> WorkshopDownloadResult:
        """校验 V1 包并立即部署到 DSTCamp 管理的运行目录。"""
        from dstools.features.mod.legacy_v1 import deploy_legacy_package
        from dstools.features.mod.legacy_v1 import discover_legacy_runtime_targets
        from dstools.features.mod.local_version import (
            VERSION_CONFIRMED,
            normalize_version_for_compare,
            resolve_local_mod_version,
        )
        from dstools.features.mod.legacy_v1 import (
            resolve_legacy_package_version,
            validate_legacy_package,
        )

        archive = Path(archive_path) if archive_path is not None else None
        if archive is None:
            result.error = "Legacy Mod 下载包路径不存在"
            result.details["legacy_retry_download"] = True
            return result
        expected = str(
            expected_version or result.details.get("expected_version") or ""
        ).strip()
        validation = validate_legacy_package(archive)
        if not validation.valid:
            result.error = validation.error
            result.details["legacy_retry_download"] = True
            return result
        roots = discover_legacy_runtime_targets()
        targets = [Path(root) / f"workshop-{result.workshop_id}" for root in roots]

        def target_version(path: Path):
            return (
                resolve_local_mod_version(
                    str(result.workshop_id), path, f"workshop-{result.workshop_id}"
                )
                if path.is_dir() and (path / "modinfo.lua").is_file()
                else None
            )

        target_versions = [(path, target_version(path)) for path in targets]
        all_targets_current = bool(target_versions)
        for _path, version in target_versions:
            if version is None:
                all_targets_current = False
                break
            if expected and (
                version.status != VERSION_CONFIRMED
                or normalize_version_for_compare(version.version)
                != normalize_version_for_compare(expected)
            ):
                all_targets_current = False
                break
        if expected:
            package_version = resolve_legacy_package_version(
                result.workshop_id, archive
            )
            if (
                package_version.status != VERSION_CONFIRMED
                or normalize_version_for_compare(package_version.version)
                != normalize_version_for_compare(expected)
            ):
                result.error = (
                    "Legacy Mod 下载包版本不匹配："
                    f"包内为 {package_version.version or '未知'}，远程为 {expected}"
                )
                result.details["legacy_retry_download"] = True
                return result
        if not force and all_targets_current:
            result.accepted = True
            result.completed = True
            result.up_to_date = True
            result.installed_path = targets[0]
            result.details.update(
                {
                    "validation": "passed",
                    "legacy_runtime": str(targets[0]),
                    "legacy_targets_checked": [str(path) for path in targets],
                }
            )
            return result
        deployment = deploy_legacy_package(result.workshop_id, archive, force=True)
        if not deployment.completed:
            result.error = deployment.error or "Legacy Mod 未能部署到运行目录"
            return result
        result.accepted = True
        result.completed = True
        result.up_to_date = False
        result.installed_path = deployment.deployed[0] if deployment.deployed else None
        result.details.update(
            {
                "repair": True,
                "legacy_archive": str(archive),
                "legacy_materialized": True,
                "legacy_targets": [str(path) for path in deployment.deployed],
                "validation": "passed",
            }
        )
        if expected:
            result.details["expected_version"] = expected
            installed_versions = [(path, target_version(path)) for path in targets]
            mismatch = next(
                (
                    (path, version)
                    for path, version in installed_versions
                    if (
                        version is None
                        or version.status != VERSION_CONFIRMED
                        or normalize_version_for_compare(version.version)
                        != normalize_version_for_compare(expected)
                    )
                ),
                None,
            )
            if mismatch is not None:
                mismatch_path, installed_version = mismatch
                result.completed = False
                result.error = (
                    "Legacy Mod 部署后版本仍不匹配："
                    f"{mismatch_path} 为 "
                    f"{installed_version.version if installed_version else '未知'}，"
                    f"远程 {expected}"
                )
        return result

    def wait_for_download(
        self,
        result: WorkshopDownloadResult,
        *,
        timeout: float = 180.0,
        poll_interval: float = 0.2,
        on_progress: Callable[[int | None, int | None], None] | None = None,
    ) -> WorkshopDownloadResult:
        """泵送回调并等待安装完成，再解析安装目录。"""
        self._ensure_started()
        if not result.accepted:
            return result
        # V1 可能直接用现有 *_legacy.bin 通过验收（completed=True），不能再进入下载等待制造伪错误
        if result.completed:
            return result
        if result.state is not None and result.state.legacy_item:
            return self._wait_for_legacy_download(
                result,
                timeout=timeout,
                poll_interval=poll_interval,
                on_progress=on_progress,
            )
        run_callbacks = self.dll.SteamAPI_RunCallbacks
        run_callbacks.restype = None
        deadline = time.monotonic() + max(0.0, timeout)
        started_at = time.monotonic()
        stable_installed_polls = 0
        saw_transfer = False
        while time.monotonic() < deadline:
            run_callbacks()
            result.state = self.item_state(result.workshop_id)
            downloaded = ctypes.c_uint64()
            total = ctypes.c_uint64()
            progress_available = False
            if self.dll.SteamAPI_ISteamUGC_GetItemDownloadInfo(
                self.ugc,
                result.workshop_id,
                ctypes.byref(downloaded),
                ctypes.byref(total),
            ):
                result.downloaded_bytes = int(downloaded.value)
                result.total_bytes = int(total.value)
                progress_available = True
                saw_transfer = saw_transfer or total.value > 0
                if on_progress:
                    on_progress(result.downloaded_bytes, result.total_bytes)
            transfer_finished = (
                not progress_available
                or total.value == 0
                or downloaded.value >= total.value
            )
            if (
                result.state.installed
                and not result.state.downloading
                and not result.state.download_pending
                and not result.state.needs_update
                and transfer_finished
            ):
                # DownloadItem 刚发起时可能仍返回旧的 Installed 状态，需连续稳定几次并留出启动窗口，
                # 避免把"请求已接受"误报为"新文件已落盘"
                stable_installed_polls += 1
            else:
                stable_installed_polls = 0
            if stable_installed_polls >= 3 and (
                saw_transfer or time.monotonic() - started_at >= 0.5
            ):
                path = self.item_install_info(result.workshop_id)
                validation = validate_workshop_install(
                    path, legacy_item=bool(result.state and result.state.legacy_item)
                )
                if (
                    validation.valid
                    and SteamWorkshopSession._downloaded_version_matches(result, path)
                ):
                    result.installed_path = validation.path
                    result.completed = True
                    result.details["validation"] = "passed"
                    if validation.warning:
                        result.details["validation_warning"] = validation.warning
                    SteamWorkshopSession._finish_forced_version_repair(
                        result, success=True
                    )
                    return result
                result.details["postcheck_error"] = validation.error
            time.sleep(max(0.02, poll_interval))
        validation_error = result.details.get("postcheck_error")
        if validation_error:
            result.error = f"Workshop 请求已接受，但文件验收失败：{validation_error}"
        else:
            result.error = "等待 Workshop 下载完成超时；请检查 Steam 登录状态和网络"
        SteamWorkshopSession._finish_forced_version_repair(result, success=False)
        return result

    def _wait_for_legacy_download(
        self,
        result: WorkshopDownloadResult,
        *,
        timeout: float,
        poll_interval: float,
        on_progress: Callable[[int | None, int | None], None] | None,
    ) -> WorkshopDownloadResult:
        """等待 ``UGCDownloadToLocation`` 完成并验收实际 Legacy 文件。"""
        target_text = result.details.get("legacy_target")
        api_call = int(result.details.get("legacy_api_call") or 0)
        if not target_text or not api_call:
            result.error = "Legacy Workshop 下载上下文不完整"
            return result
        target = Path(target_text)
        final_target = Path(result.details.get("legacy_final_target") or target)
        expected_size = int(result.details.get("legacy_expected_size") or 0)
        info = self.item_install_details(result.workshop_id)
        if not expected_size and info is not None:
            expected_size = max(0, int(info.size_on_disk))
        run_callbacks = self.dll.SteamAPI_RunCallbacks
        run_callbacks.restype = None
        io_failed = ctypes.c_bool()
        try:
            deadline = time.monotonic() + max(0.0, timeout)
            while time.monotonic() < deadline:
                run_callbacks()
                try:
                    current_size = target.stat().st_size
                except OSError:
                    current_size = 0
                result.downloaded_bytes = current_size
                result.total_bytes = expected_size or None
                if on_progress:
                    on_progress(result.downloaded_bytes, result.total_bytes)
                if self.dll.SteamAPI_ISteamUtils_IsAPICallCompleted(
                    self.utils, api_call, ctypes.byref(io_failed)
                ):
                    if io_failed.value:
                        result.error = "Legacy Workshop 下载发生 Steam IO 错误"
                        return result
                    from dstools.features.mod.legacy_v1 import validate_legacy_package

                    validation = validate_legacy_package(target)
                    if validation.valid and (
                        not expected_size or current_size == expected_size
                    ):
                        try:
                            os.replace(target, final_target)
                        except OSError as exc:
                            result.error = f"无法替换 Legacy Mod 下载包：{exc}"
                            return result
                        return SteamWorkshopSession._finish_legacy_install(
                            result,
                            final_target,
                            expected_version=str(
                                result.details.get("expected_version") or ""
                            ),
                            force=True,
                        )
                    if validation.valid:
                        result.error = f"Legacy Workshop 文件大小不完整：{current_size}/{expected_size} 字节"
                    else:
                        result.error = f"Legacy Workshop 文件验收失败：{validation.error}"
                    return result
                time.sleep(max(0.02, poll_interval))
            result.error = "等待 Legacy Workshop 下载完成超时；请检查 Steam 登录状态和网络"
            return result
        finally:
            # 失败时临时文件留在 V1 目录里会让游戏不再解压；成功时它已被改名，这里只清失败残留
            if target != final_target:
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    pass

    def item_install_info(self, workshop_id: int) -> Path | None:
        details = self.item_install_details(workshop_id)
        return details.path if details is not None else None

    def item_install_details(self, workshop_id: int) -> WorkshopInstallInfo | None:
        self._ensure_started()
        size = ctypes.c_uint64()
        timestamp = ctypes.c_uint32()
        buf = ctypes.create_string_buffer(4096)
        ok = bool(
            self.dll.SteamAPI_ISteamUGC_GetItemInstallInfo(
                self.ugc,
                int(workshop_id),
                ctypes.byref(size),
                buf,
                len(buf),
                ctypes.byref(timestamp),
            )
        )
        if not ok:
            return None
        raw = buf.value.decode("utf-8", errors="replace").strip()
        return (
            WorkshopInstallInfo(Path(raw), int(size.value), int(timestamp.value))
            if raw
            else None
        )

    def _ensure_started(self) -> None:
        if not self._started or self.dll is None or not self.ugc:
            raise RuntimeError("Steam Workshop 会话尚未初始化")

    def close(self) -> None:
        if self.dll is None:
            return
        try:
            if self._native_initialized:
                self.dll.SteamAPI_Shutdown()
        finally:
            self._started = False
            self._native_initialized = False
            self.ugc = None
            self.utils = None
            self.dll = None
            if self._dll_directory_handle is not None:
                self._dll_directory_handle.close()
                self._dll_directory_handle = None




def _update_workshop_items_in_process(
    workshop_ids: list[int] | tuple[int, ...],
    *,
    dll_path: Path | None = None,
    timeout: float = 180.0,
    expected_versions: dict[int, str] | None = None,
    force_redownload_ids: set[int] | None = None,
    on_progress: Callable[[int, int, int | None, int | None], None] | None = None,
    on_item_start: Callable[[int, int, int], None] | None = None,
    on_item_complete: Callable[[int, int, WorkshopDownloadResult], None] | None = None,
) -> WorkshopBatchResult:
    """用普通 SteamUGC 会话批量检查/更新 Workshop Mod，只更新 Steam 共享 Workshop 缓存。"""
    unique_ids: list[int] = []
    seen: set[int] = set()
    for raw_id in workshop_ids:
        workshop_id = int(raw_id)
        if workshop_id > 0 and workshop_id not in seen:
            seen.add(workshop_id)
            unique_ids.append(workshop_id)
    batch = WorkshopBatchResult()
    if not unique_ids:
        return batch
    resolved_dll = find_steam_api_dll(dll_path)
    if resolved_dll is None:
        error = "找不到 DST 或专用服务器的 bin64\\steam_api64.dll"
        batch.results = [
            WorkshopDownloadResult(item, error=error)
            for item in unique_ids
        ]
        return batch
    total = len(unique_ids)
    expected_versions = {
        int(key): str(value).strip()
        for key, value in (expected_versions or {}).items()
        if int(key) > 0 and str(value).strip()
    }
    force_redownload_ids = {
        int(item) for item in (force_redownload_ids or ()) if int(item) > 0
    }
    try:
        with SteamWorkshopSession(resolved_dll) as session:
            source_details: dict[int, WorkshopItemDetails] = {}
            # 已下架/私密项目的 DownloadItem 可能返回 true 但永不下载：先批量预检源端，
            # 只有明确的逐项错误才短路，整体查询失败仍走原下载路径
            try:
                for start in range(0, total, 50):
                    for item in session.query_item_details(
                        unique_ids[start : start + 50], timeout=min(timeout, 20.0)
                    ):
                        source_details[item.workshop_id] = item
            except Exception:
                source_details = {}
            for index, workshop_id in enumerate(unique_ids, 1):
                if on_item_start:
                    on_item_start(index, total, workshop_id)
                try:
                    source_error = workshop_source_error(
                        source_details.get(workshop_id)
                    )
                    if source_error:
                        result = WorkshopDownloadResult(workshop_id,
                            error=source_error,
                            details={
                                "source_result": source_details[workshop_id].result
                            },
                        )
                    else:
                        result = session.download_item(
                            workshop_id,
                            expected_version=expected_versions.get(workshop_id, ""),
                            source_details=source_details.get(workshop_id),
                            force_redownload=workshop_id in force_redownload_ids,
                        )
                        detail = source_details.get(workshop_id)
                        if (
                            result.state is not None
                            and result.state.legacy_item
                            and detail is not None
                            and detail.file_size > 0
                        ):
                            result.details["legacy_expected_size"] = int(
                                detail.file_size
                            )
                        try:
                            result = session.wait_for_download(
                                result,
                                timeout=timeout,
                                on_progress=(
                                    None
                                    if on_progress is None
                                    else lambda done, size, i=index: on_progress(
                                        i, total, done, size
                                    )
                                ),
                            )
                        except Exception:
                            SteamWorkshopSession._finish_forced_version_repair(
                                result, success=False
                            )
                            raise
                except Exception as exc:
                    result = WorkshopDownloadResult(workshop_id,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                if result.completed and result.details.get("legacy_extras_removed"):
                    # 清理了多余文件就算一次修复，不能报"已是最新"
                    result.up_to_date = False
                batch.results.append(result)
                if on_item_complete:
                    on_item_complete(index, total, result)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        while len(batch.results) < total:
            workshop_id = unique_ids[len(batch.results)]
            result = WorkshopDownloadResult(workshop_id, error=error
            )
            batch.results.append(result)
            if on_item_complete:
                on_item_complete(len(batch.results), total, result)
    return batch


def update_workshop_items(
    workshop_ids: list[int] | tuple[int, ...],
    *,
    dll_path: Path | None = None,
    timeout: float = 180.0,
    expected_versions: dict[int, str] | None = None,
    force_redownload_ids: set[int] | None = None,
    on_progress: Callable[[int, int, int | None, int | None], None] | None = None,
    on_item_start: Callable[[int, int, int], None] | None = None,
    on_item_complete: Callable[[int, int, WorkshopDownloadResult], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> WorkshopBatchResult:
    """通过独立进程更新 Mod，完成后立即释放 Steam 的 322330 AppID。"""
    ids = list(dict.fromkeys(int(item) for item in workshop_ids if int(item) > 0))
    if not ids:
        return WorkshopBatchResult()
    expected_versions = {
        int(key): str(value).strip()
        for key, value in (expected_versions or {}).items()
        if int(key) in ids and str(value).strip()
    }
    force_redownload_ids = {
        int(item) for item in (force_redownload_ids or ()) if int(item) in ids
    }

    def handle_event(event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "progress" and on_progress:
            on_progress(
                int(event["current"]),
                int(event["total"]),
                event.get("downloaded"),
                event.get("size"),
            )
        elif event_type == "item_start" and on_item_start:
            on_item_start(
                int(event["current"]), int(event["total"]), int(event["workshop_id"])
            )
        elif event_type == "item_complete" and on_item_complete:
            on_item_complete(
                int(event["current"]),
                int(event["total"]),
                _download_result_from_payload(event["result"]),
            )

    try:
        payload = _run_workshop_worker(
            {
                "action": "update",
                "ids": ids,
                "dll_path": str(dll_path) if dll_path else None,
                "timeout": timeout,
                "expected_versions": expected_versions,
                "force_redownload_ids": sorted(force_redownload_ids),
            },
            handle_event,
            cancel_event=cancel_event,
        )
        return WorkshopBatchResult(
            [
                _download_result_from_payload(item)
                for item in payload.get("results") or ()
            ]
        )
    except WorkshopUpdateCancelled:
        raise
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        results = []
        for index, workshop_id in enumerate(ids, 1):
            result = WorkshopDownloadResult(workshop_id, error=error
            )
            results.append(result)
            if on_item_complete:
                on_item_complete(index, len(ids), result)
        return WorkshopBatchResult(results)




def _get_workshop_item_snapshot_in_process(
    workshop_ids: list[int] | tuple[int, ...],
    *,
    detail_ids: list[int] | tuple[int, ...] = (),
    dll_path: Path | None = None,
    detail_timeout: float = 20.0,
    include_subscribed: bool = False,
) -> tuple[
    dict[int, WorkshopItemState],
    dict[int, WorkshopInstallInfo],
    dict[int, WorkshopItemDetails],
]:
    """在同一次 Steam 会话中读取状态、安装记录和可选的源端详情（更新选择器刷新用）。

    源端详情只是展示增强，查询失败时保留本地状态与安装记录。
    """
    requested_ids = list(
        dict.fromkeys(int(item) for item in workshop_ids if int(item) > 0)
    )
    if not requested_ids and not include_subscribed:
        return {}, {}, {}
    resolved_dll = find_steam_api_dll(dll_path)
    if resolved_dll is None:
        raise FileNotFoundError("找不到 DST 或专用服务器的 bin64\\steam_api64.dll")
    states: dict[int, WorkshopItemState] = {}
    installs: dict[int, WorkshopInstallInfo] = {}
    details: dict[int, WorkshopItemDetails] = {}
    with SteamWorkshopSession(resolved_dll) as session:
        subscribed_ids = session.subscribed_item_ids() if include_subscribed else []
        ids = list(dict.fromkeys((*requested_ids, *subscribed_ids)))
        id_set = set(ids)
        # 只为新枚举出的 ID 查询详情（没有本地名称），已扫描项目仍只查 detail_ids，避免每次查几百个
        details_to_query = list(
            dict.fromkeys(
                (
                    *(int(raw) for raw in detail_ids if int(raw) > 0),
                    *(
                        item
                        for item in subscribed_ids
                        if item not in set(requested_ids)
                    ),
                )
            )
        )
        details_to_query = [item for item in details_to_query if item in id_set]
        states = {workshop_id: session.item_state(workshop_id) for workshop_id in ids}
        for workshop_id in ids:
            info = session.item_install_details(workshop_id)
            if info is not None:
                installs[workshop_id] = info
        # 150+ Mod 会分成多批查询；某一批失败时保留其余批次已经取得的
        # 远程版本和标题，其余项目再由 Klei 本地缓存降级，不能整批清空。
        # 整个查询失败（如接口初始化出错）也不能连带丢掉上面已取得的本地状态。
        try:
            for item in session.query_item_details_batches(details_to_query, timeout=detail_timeout):
                details[item.workshop_id] = item
        except Exception:
            pass
    return states, installs, details


def get_workshop_item_snapshot(
    workshop_ids: list[int] | tuple[int, ...],
    *,
    detail_ids: list[int] | tuple[int, ...] = (),
    dll_path: Path | None = None,
    detail_timeout: float = 20.0,
    include_subscribed: bool = False,
) -> tuple[
    dict[int, WorkshopItemState],
    dict[int, WorkshopInstallInfo],
    dict[int, WorkshopItemDetails],
]:
    """在独立进程中读取 Workshop 证据，GUI 进程不加载 Steam API。"""
    ids = list(dict.fromkeys(int(item) for item in workshop_ids if int(item) > 0))
    if not ids:
        return {}, {}, {}
    payload = _run_workshop_worker(
        {
            "action": "snapshot",
            "ids": ids,
            "detail_ids": list(detail_ids),
            "dll_path": str(dll_path) if dll_path else None,
            "detail_timeout": detail_timeout,
            "include_subscribed": include_subscribed,
        }
    )
    return _snapshot_from_payload(payload)


def _subscribe_workshop_items_in_process(
    workshop_ids, *, dll_path: Path | None = None
) -> dict[int, str]:
    """逐个订阅；返回 {ID: 错误信息}，成功项错误信息为空串。"""
    resolved_dll = find_steam_api_dll(dll_path)
    if resolved_dll is None:
        raise FileNotFoundError("找不到 DST 或专用服务器的 bin64\\steam_api64.dll")
    results: dict[int, str] = {}
    with SteamWorkshopSession(resolved_dll) as session:
        for workshop_id in workshop_ids:
            try:
                code = session.subscribe_item(int(workshop_id))
                results[int(workshop_id)] = "" if code == _STEAM_RESULT_OK else f"Steam 返回错误码 {code}"
            except (RuntimeError, TimeoutError, OSError) as exc:
                results[int(workshop_id)] = str(exc)
    return results


def subscribe_workshop_items(
    workshop_ids: list[int] | tuple[int, ...], *, dll_path: Path | None = None
) -> dict[int, str]:
    """在独立进程中以当前 Steam 账号订阅项目；返回 {ID: 错误信息}，成功为空串。"""
    ids = list(dict.fromkeys(int(item) for item in workshop_ids if int(item) > 0))
    if not ids:
        return {}
    payload = _run_workshop_worker(
        {"action": "subscribe", "ids": ids, "dll_path": str(dll_path) if dll_path else None}
    )
    return {int(key): str(value) for key, value in (payload.get("results") or {}).items()}
