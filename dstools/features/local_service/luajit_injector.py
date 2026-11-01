"""管理 Steam 开服程序的 DontStarveLuaJIT2 注入（WeGame 不支持）。

按开服程序目录自动区分两种方式：
- 独立专服：真实 ``bin64`` 永不修改，``Winmm.dll`` 注入壳装入同级 ``luajit`` 隔离副本；
  游戏版本、Mod 声明版本、布局或注入壳变化时更新副本。
- 游戏专服（客户端目录自带的开服程序）：按作者 README 只把 ``Winmm.dll`` 放进游戏
  ``bin64``，卸载即删除；与客户端共用，是否安装只看该文件，与 DSTCamp 的开关无关。

真实 ``Injector.dll`` 及依赖都留在 Workshop Mod 目录，通过作者约定的路径标记
``data/unsafedata/ds_luajit_injector.path`` 连接。
"""

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from dstools.shared.app_settings import get_luajit_enabled, set_luajit_enabled
from dstools.features.mod.manager import enable_mod, load_mod_overrides, save_mod_overrides
from dstools.features.mod.parser import find_workshop_dir, parse_modinfo
from dstools.shared.steam_discovery import read_game_version_file
from dstools.i18n import t

# 触发文件——新版补丁唯一需要跟游戏 exe 放在同一目录的注入壳。
TRIGGER_FILE = "Winmm.dll"
# 真正的 hook 逻辑留在创意工坊 Mod 目录，由路径标记指向，不再复制进游戏目录。
_CORE_PAYLOAD_FILE = "Injector.dll"
_INJECTOR_PATH_MARKER = Path("data/unsafedata/ds_luajit_injector.path")
_LAYOUT_VERSION = 2
_LEGACY_PAYLOAD_FILES = (
    "Injector.dll",
    "Injector.pdb",
    "lua51.dll",
    "lua51.pdb",
    "lua51DS.dll",
    "lua51DS.pdb",
    "lua51DS_gengc.dll",
    "lua51DS_gengc.pdb",
    "lua51Original.dll",
    "lua51Original.pdb",
    "signatures_client.json",
    "signatures_server.json",
)
# 专用服务器启动文件。不同安装可能只有 64 位或只有 32 位，校验时只
# 检查真实 bin64/bin 中实际存在的那个，避免把测试/旧版安装误判成失败。
_SERVER_EXECUTABLE_NAMES = (
    "dontstarve_dedicated_server_nullrenderer_x64.exe",
    "dontstarve_dedicated_server_nullrenderer.exe",
)

# 隔离副本目录名——跟真实 bin64/ 同级（install_dir 下），整个复制一份
# bin64 内容进去，注入文件也装进这里，真实 bin64/ 永远不被触碰。
LUAJIT_DIR_NAME = "luajit"
# 副本目录内的标记文件：记录副本对应的游戏版本与配套 Mod 版本，任一变化即需重建（见 needs_regeneration）
_MARKER_FILE = "version.json"

# 配套 Mod 在创意工坊的物品 ID（作者确认，固定值，不是猜的）。
# modoverrides.lua 里的 key 用标准 Workshop 命名 "workshop-<id>"。
WORKSHOP_ID = "3444078585"
WORKSHOP_MOD_KEY = f"workshop-{WORKSHOP_ID}"
WORKSHOP_PAGE_URL = f"https://steamcommunity.com/sharedfiles/filedetails/?id={WORKSHOP_ID}"



def get_luajit_dir(install_dir: Path) -> Path:
    return install_dir / LUAJIT_DIR_NAME


def uses_game_bin64(install_dir: Path) -> bool:
    """游戏专服直接装进游戏 bin64（与客户端共用），独立专服用隔离副本。"""
    from dstools.features.local_service.dedicated_server import is_client_install_dir
    return is_client_install_dir(install_dir)


def _game_trigger_file(bin64_dir: Path) -> Path | None:
    """游戏 bin64 里现有的注入壳；作者脚本两种大小写都会处理。"""
    for name in (TRIGGER_FILE, "winmm.dll"):
        candidate = bin64_dir / name
        if candidate.is_file():
            return candidate
    return None


def _file_in_use(path: Path) -> bool:
    """已被进程加载的 DLL 不能以写方式打开（共享冲突），用来判断游戏或服务器是否正在用它。"""
    try:
        with path.open("r+b"):
            return False
    except PermissionError:
        return True
    except OSError:
        return False


def game_trigger_in_use(bin64_dir: Path) -> bool:
    """游戏专服模式下 Winmm.dll 是否正被游戏客户端或服务器占用（此时无法覆盖或删除）。"""
    trigger = _game_trigger_file(bin64_dir)
    return trigger is not None and _file_in_use(trigger)


def _remove_stale_luajit_copy(install_dir: Path, log) -> None:
    """清理 DSTCamp 早先在游戏目录生成的 luajit 隔离副本。只删带本工具
    version.json 标记的目录，不碰别人放的同名文件夹。"""
    luajit_dir = get_luajit_dir(install_dir)
    if (luajit_dir / _MARKER_FILE).is_file():
        shutil.rmtree(luajit_dir, ignore_errors=True)
        if not luajit_dir.exists():
            log(t("local.luajit_log_stale_copy_removed", dir=str(luajit_dir)))


def current_game_build_id(install_dir: Path) -> str | None:
    """读取专服安装根目录下 version.txt 的游戏版本号。"""
    return read_game_version_file(install_dir)


def current_injector_version() -> str | None:
    """配套 Mod 订阅内容 modinfo.lua 中作者声明的 version（如 "1.10.1"），找不到返回 None。"""
    mod_dir = _workshop_mod_dir()
    if mod_dir is None:
        return None
    info = parse_modinfo(mod_dir)
    if info is None or not info.version:
        return None
    return info.version


def _workshop_mod_dir() -> Path | None:
    workshop_dir = find_workshop_dir()
    if workshop_dir is None:
        return None
    candidate = workshop_dir / WORKSHOP_ID
    return candidate if candidate.is_dir() else None


def _injector_source_dir() -> Path | None:
    """返回新版注入壳所在的 ``bin64/windows`` 目录。"""
    mod_dir = _workshop_mod_dir()
    if mod_dir is None:
        return None
    candidate = mod_dir / "bin64" / "windows"
    return candidate if candidate.is_dir() else None


def _trigger_source_file(source_dir: Path | None = None) -> Path | None:
    source_dir = source_dir or _injector_source_dir()
    if source_dir is None:
        return None
    for name in ("Winmm.dll", "winmm.dll"):
        candidate = source_dir / name
        if candidate.is_file():
            return candidate
    return None


def _packaged_twin(path: Path) -> Path:
    return path.with_name(path.name + ".pkg")


def _module_present(path: Path) -> bool:
    """规范名或其 ``.pkg`` 双胞胎存在即可。

    坑：创意工坊自 3.3.x 起只发布 ``Injector.dll.pkg``，由注入壳在 LoadLibrary 前就地生成
    ``Injector.dll``（上游 core/PackagedModule.hpp），路径标记仍写规范名。"""
    return path.is_file() or _packaged_twin(path).is_file()


def _injector_payload_file() -> Path | None:
    """兼容作者文档与创意工坊实际包出现过的三种 Injector.dll 位置；返回规范名路径（可能尚未生成）。"""
    mod_dir = _workshop_mod_dir()
    if mod_dir is None:
        return None
    for relative in (
        Path(_CORE_PAYLOAD_FILE),
        Path("bin64") / _CORE_PAYLOAD_FILE,
        Path("bin64/windows") / _CORE_PAYLOAD_FILE,
    ):
        candidate = mod_dir / relative
        if _module_present(candidate):
            return candidate
    return None


def runtime_dlls() -> list[Path]:
    """配套 Mod 里依赖 VC++ x64 运行库的 DLL（注入壳与 Injector.dll），用于判断运行库版本是否够新。

    有 ``.pkg`` 时读它：它才是最新内容，已生成的规范名可能还是旧版。"""
    payload = _injector_payload_file()
    if payload is not None and _packaged_twin(payload).is_file():
        payload = _packaged_twin(payload)
    return [path for path in (_trigger_source_file(), payload) if path is not None]


def _file_sha256(path: Path | None) -> str:
    if path is None:
        return ""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


@dataclass
class LuajitMarker:
    """副本标记：生成副本时的游戏版本(DST_version)、配套 Mod 版本(luajit_version)、
    布局版本(layout_version) 与 Winmm.dll 哈希(trigger_sha256，识别 Mod 版本号未变但 DLL 已变)。

    DST_version 内部存字符串，写 JSON 时转为数字。"""
    DST_version: str
    luajit_version: str
    layout_version: int = 1
    trigger_sha256: str = ""


def read_marker(luajit_dir: Path) -> LuajitMarker | None:
    """读取副本标记；缺失或损坏返回 None（调用方视为"未成功安装"，而不是"版本没变"）。"""
    path = luajit_dir / _MARKER_FILE
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return LuajitMarker(DST_version=str(data["DST_version"]),
                             luajit_version=str(data["luajit_version"]),
                             layout_version=int(data.get("layout_version", 1)),
                             trigger_sha256=str(data.get("trigger_sha256", "")))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def write_marker(luajit_dir: Path, marker: LuajitMarker) -> None:
    """原子写入标记（临时文件再 rename）；DST_version 非纯数字时退回存字符串。"""
    path = luajit_dir / _MARKER_FILE
    part_path = path.with_suffix(".json.part")
    dst_version_value = int(marker.DST_version) if marker.DST_version.isdigit() else marker.DST_version
    part_path.write_text(
        json.dumps({
            "DST_version": dst_version_value,
            "luajit_version": marker.luajit_version,
            "layout_version": marker.layout_version,
            "trigger_sha256": marker.trigger_sha256,
        }),
        encoding="utf-8",
    )
    part_path.replace(path)


def write_injector_path_marker(install_dir: Path, injector_path: Path) -> None:
    """按作者新版协议原子写入真实 Injector.dll 的绝对路径。"""
    marker_path = install_dir / _INJECTOR_PATH_MARKER
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    part_path = marker_path.with_suffix(marker_path.suffix + ".part")
    part_path.write_text(str(injector_path.resolve()) + "\n", encoding="utf-8")
    part_path.replace(marker_path)


def read_injector_path_marker(install_dir: Path) -> Path | None:
    marker_path = install_dir / _INJECTOR_PATH_MARKER
    try:
        value = marker_path.read_text(encoding="utf-8").strip().strip('"')
    except OSError:
        return None
    if not value:
        return None
    candidate = Path(value)
    return candidate if _module_present(candidate) else None


def _runtime_ready(install_dir: Path) -> bool:
    luajit_dir = get_luajit_dir(install_dir)
    return (
        (luajit_dir / TRIGGER_FILE).is_file()
        and read_injector_path_marker(install_dir) is not None
    )


def is_workshop_subscribed() -> bool:
    """本机 Steam 是否已订阅配套 Mod：以 Workshop 目录下带 modinfo.lua 的 WORKSHOP_ID 子目录为准。

    未订阅时调用方引导用户去 WORKSHOP_PAGE_URL 手动订阅。"""
    workshop_dir = find_workshop_dir()
    if workshop_dir is None:
        return False
    candidate = workshop_dir / WORKSHOP_ID
    return candidate.exists() and (candidate / "modinfo.lua").exists()




class InjectorState(Enum):
    NOT_INSTALLED = "not_installed"          # 副本还不存在
    ACTIVE = "active"                        # 副本存在且已启用——真正生效
    DISABLED_LEFTOVER = "disabled_leftover"  # 副本存在但未启用——已关闭，文件残留无害


def detect_state(bin64_dir: Path) -> InjectorState:
    """计算隔离副本的状态（纯函数，不写真实 bin64）。

    副本中的 Winmm.dll 和能解析到现存 Injector.dll 的路径标记缺一即视为未安装，
    防止界面显示已启用、启动时却悄悄回退。订阅状态另见 is_workshop_subscribed()。"""
    install_dir = bin64_dir.parent
    if uses_game_bin64(install_dir):
        return InjectorState.ACTIVE if _game_trigger_file(bin64_dir) else InjectorState.NOT_INSTALLED
    if not _runtime_ready(install_dir):
        return InjectorState.NOT_INSTALLED
    return InjectorState.ACTIVE if get_luajit_enabled() else InjectorState.DISABLED_LEFTOVER


@dataclass
class InstallPlan:
    bin64_dir: Path | None = None
    current_state: InjectorState = InjectorState.NOT_INSTALLED
    # "bin64_not_found" / "server_running" / "game_running" / "workshop_not_subscribed" / None
    blocked_reason: str | None = None


def plan_install(bin64_dir: Path | None, server_running: bool) -> InstallPlan:
    """只读地计算安装计划；blocked_reason 是内部标识，界面自行翻译。

    先检查 bin64 与运行状态，再检查是否订阅配套 Mod。"""
    if bin64_dir is None or not bin64_dir.exists():
        return InstallPlan(bin64_dir=None, blocked_reason="bin64_not_found")
    if server_running:
        return InstallPlan(bin64_dir=bin64_dir, current_state=detect_state(bin64_dir),
                            blocked_reason="server_running")
    if uses_game_bin64(bin64_dir.parent) and game_trigger_in_use(bin64_dir):
        return InstallPlan(bin64_dir=bin64_dir, current_state=detect_state(bin64_dir),
                            blocked_reason="game_running")
    if not is_workshop_subscribed():
        return InstallPlan(bin64_dir=bin64_dir, current_state=detect_state(bin64_dir),
                            blocked_reason="workshop_not_subscribed")
    return InstallPlan(bin64_dir=bin64_dir, current_state=detect_state(bin64_dir), blocked_reason=None)


@dataclass
class InstallResult:
    ok: bool = False
    errors: list[str] = field(default_factory=list)


def _copy_injector_shell_into(source_dir: Path, dest_dir: Path, on_log=None) -> int:
    """按新版协议只把 Winmm.dll 注入壳复制到隔离副本。"""
    trigger = _trigger_source_file(source_dir)
    if trigger is None:
        raise FileNotFoundError(TRIGGER_FILE)
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(trigger, dest_dir / TRIGGER_FILE)
    n = 1
    if on_log:
        on_log(t("local.luajit_log_copied", n=n, dir=str(dest_dir)))
    return n


def _rebuild_luajit_copy(bin64_dir: Path, luajit_dir: Path, source_dir: Path, on_log=None) -> None:
    """在临时目录完整构建 LuaJIT 副本，校验通过后再替换正式目录。

    坑：先删正式目录再复制，中途被杀软、文件占用或磁盘空间打断就只剩半成品；
    临时目录方案保证失败时旧副本可用。"""
    def log(line: str) -> None:
        if on_log:
            on_log(line)

    install_dir = luajit_dir.parent
    temp_dir = Path(tempfile.mkdtemp(prefix=f".{LUAJIT_DIR_NAME}.tmp-", dir=str(install_dir)))
    # mkdtemp 已经创建了目录，copytree 需要一个不存在的目标路径。
    shutil.rmtree(temp_dir)
    backup_dir: Path | None = None
    try:
        log(t("local.luajit_log_copying_bin64"))
        shutil.copytree(bin64_dir, temp_dir)
        # 清掉按作者旧版说明手动复制进来的顶层载荷（只处理安装脚本列出的精确文件名），
        # 避免与路径标记指向的新 Injector 混用；真实 bin64 中的文件不删
        for name in _LEGACY_PAYLOAD_FILES:
            (temp_dir / name).unlink(missing_ok=True)
        log(t("local.luajit_log_copying_injector"))
        _copy_injector_shell_into(source_dir, temp_dir, on_log=log)

        # 在临时目录先校验注入锚点和实际的服务器启动文件，再触碰正式目录
        expected_server_files = [
            name for name in _SERVER_EXECUTABLE_NAMES
            if (bin64_dir / name).is_file()
        ]
        missing = [
            name for name in expected_server_files + [TRIGGER_FILE]
            if not (temp_dir / name).is_file()
        ]
        if missing:
            raise RuntimeError(t("local.luajit_error_copy_incomplete", files=", ".join(missing)))

        if luajit_dir.exists() or luajit_dir.is_symlink():
            backup_dir = Path(tempfile.mkdtemp(prefix=f".{LUAJIT_DIR_NAME}.backup-", dir=str(install_dir)))
            shutil.rmtree(backup_dir)
            luajit_dir.replace(backup_dir)
        temp_dir.replace(luajit_dir)
        log(t("local.luajit_log_bin64_copied", dir=str(luajit_dir)))
    except Exception:
        # 如果正式目录已经移到备份位置但新目录替换失败，优先恢复旧副本。
        if backup_dir is not None and backup_dir.exists() and not luajit_dir.exists():
            backup_dir.replace(luajit_dir)
        raise
    finally:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
        if backup_dir is not None and backup_dir.exists():
            # 新副本已经生效，旧副本仅作为回滚保护；清理失败不应再把
            # 本次成功报告成失败。
            shutil.rmtree(backup_dir, ignore_errors=True)


def apply_install(bin64_dir: Path, mod_overrides_paths: list[Path], on_log=None) -> InstallResult:
    """执行安装（调用方须已通过 plan_install 确认）：

    1. 从已订阅的配套 Mod 取注入文件（不联网，Steam 负责保持稳定版）；
    2. 复制真实 bin64 到同级 luajit/ 隔离副本；
    3. 只把 Winmm.dll 放进副本，写入 Injector.dll 路径及版本/哈希标记；
    4. 在各 modoverrides.lua 启用配套 Mod，并打开 LuaJIT 开关。
    日志通过 on_log 输出。"""
    def log(line: str) -> None:
        if on_log:
            on_log(line)

    result = InstallResult()
    install_dir = bin64_dir.parent
    game_bin64 = uses_game_bin64(install_dir)

    luajit_dir = get_luajit_dir(install_dir)
    try:
        source_dir = _injector_source_dir()
        injector_path = _injector_payload_file()
        trigger_path = _trigger_source_file(source_dir)
        if source_dir is None or injector_path is None or trigger_path is None:
            result.errors.append(t("local.luajit_error_no_injector_source"))
            log(result.errors[-1])
            return result

        if game_bin64:
            # 作者方式：只把注入壳放进游戏 bin64，客户端与游戏专服共用。
            _copy_injector_shell_into(source_dir, bin64_dir, on_log=log)
            write_injector_path_marker(install_dir, injector_path)
            _remove_stale_luajit_copy(install_dir, log)
        else:
            _rebuild_luajit_copy(bin64_dir, luajit_dir, source_dir, on_log=log)
            write_injector_path_marker(install_dir, injector_path)

            build_id = current_game_build_id(install_dir) or ""
            luajit_version = current_injector_version() or ""
            write_marker(luajit_dir, LuajitMarker(
                DST_version=build_id,
                luajit_version=luajit_version,
                layout_version=_LAYOUT_VERSION,
                trigger_sha256=_file_sha256(trigger_path),
            ))

        n_shards = 0
        for mo_path in mod_overrides_paths:
            overrides = load_mod_overrides(mo_path)
            enable_mod(overrides, WORKSHOP_MOD_KEY)
            save_mod_overrides(overrides)
            n_shards += 1
        log(t("local.luajit_log_mod_enabled", n=n_shards))

        # 全局开关只管独立专服的隔离副本；游戏专服是否生效只看 Winmm.dll。
        if not game_bin64:
            set_luajit_enabled(True)
        result.ok = True
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        result.errors.append(t("local.luajit_error_operation_failed", detail=detail))
        log(result.errors[-1])
    return result


def apply_uninstall(bin64_dir: Path, on_log=None) -> bool:
    """关闭 LuaJIT 开关（幂等），下次启动用真实 bin64；保留副本和配套 Mod 启用状态。"""
    def log(line: str) -> None:
        if on_log:
            on_log(line)

    install_dir = bin64_dir.parent
    if uses_game_bin64(install_dir):
        # 作者的卸载方式：删除游戏 bin64 的注入壳和路径标记；客户端的 LuaJIT 一并卸载。
        removed = False
        try:
            for name in (TRIGGER_FILE, "winmm.dll"):
                target = bin64_dir / name
                if target.is_file():
                    target.unlink()
                    removed = True
            (install_dir / _INJECTOR_PATH_MARKER).unlink(missing_ok=True)
        except OSError as exc:
            log(t("local.luajit_error_operation_failed", detail=f"{type(exc).__name__}: {exc}"))
            return False
        log(t("local.luajit_log_game_uninstalled") if removed else t("local.luajit_log_already_not_active"))
        return removed

    if not get_luajit_enabled():
        log(t("local.luajit_log_already_not_active"))
        return False
    set_luajit_enabled(False)
    log(t("local.luajit_log_uninstalled"))
    return True


def _remove_tree(path: Path) -> None:
    """删除目录；联接只删联接本身，不进入目标（符号链接 rmtree 会直接报错，同样不进入）。"""
    if os.path.isjunction(path):
        os.rmdir(path)
    else:
        shutil.rmtree(path)


def _leftover_dirs(install_dir: Path) -> list[Path]:
    """本工具生成的副本与中断遗留的临时目录。只认带 version.json 标记的 luajit，不碰别人放的同名文件夹。"""
    luajit_dir = get_luajit_dir(install_dir)
    dirs = [luajit_dir] if (luajit_dir / _MARKER_FILE).is_file() else []
    dirs += sorted(install_dir.glob(f".{LUAJIT_DIR_NAME}.tmp-*"))
    dirs += sorted(install_dir.glob(f".{LUAJIT_DIR_NAME}.backup-*"))
    return dirs


def has_leftovers(bin64_dir: Path) -> bool:
    """未生效时是否还有可彻底卸载的残留（副本、临时目录或路径标记），决定卸载按钮是否可点。"""
    install_dir = bin64_dir.parent
    return bool(_leftover_dirs(install_dir)) or (install_dir / _INJECTOR_PATH_MARKER).is_file()


def apply_full_uninstall(bin64_dir: Path, mod_overrides_paths: list[Path], on_log=None) -> InstallResult:
    """彻底卸载：删除本工具生成的全部 LuaJIT 文件，并在给定分片中停用配套 Mod。

    - 独立专服：关闭开关，删除带 version.json 标记的 luajit 副本、中断遗留的
      .luajit.tmp-*/.luajit.backup-* 临时目录和路径标记；
    - 游戏专服：删除游戏 bin64 的注入壳和路径标记（客户端一并卸载），并清理旧副本。
    真实 bin64、创意工坊订阅和作者旧版手动复制的文件都不碰。某项失败时继续处理其余项并汇总报错。"""
    def log(line: str) -> None:
        if on_log:
            on_log(line)

    result = InstallResult()

    def fail(exc: OSError) -> None:
        result.errors.append(t("local.luajit_error_uninstall_failed", detail=f"{type(exc).__name__}: {exc}"))
        log(result.errors[-1])

    install_dir = bin64_dir.parent
    if uses_game_bin64(install_dir):
        for name in (TRIGGER_FILE, "winmm.dll"):
            target = bin64_dir / name
            try:
                if target.is_file():
                    target.unlink()
                    log(t("local.luajit_log_removed", path=str(target)))
            except OSError as exc:
                fail(exc)
    elif get_luajit_enabled():
        set_luajit_enabled(False)
        log(t("local.luajit_log_switch_off"))

    for path in _leftover_dirs(install_dir):
        try:
            _remove_tree(path)
            log(t("local.luajit_log_removed", path=str(path)))
        except OSError as exc:
            fail(exc)

    marker_path = install_dir / _INJECTOR_PATH_MARKER
    try:
        if marker_path.is_file():
            marker_path.unlink()
            log(t("local.luajit_log_removed", path=str(marker_path)))
    except OSError as exc:
        fail(exc)

    n_shards = 0
    for mo_path in mod_overrides_paths:
        try:
            overrides = load_mod_overrides(mo_path)
            entry = overrides.mods.get(WORKSHOP_MOD_KEY)
            if entry is not None and entry.enabled:
                entry.enabled = False
                save_mod_overrides(overrides)
                n_shards += 1
        except OSError as exc:
            fail(exc)
    if n_shards:
        log(t("local.luajit_log_mod_disabled", n=n_shards))

    result.ok = not result.errors
    log(t("local.luajit_log_full_uninstalled") if result.ok else t("local.luajit_log_full_uninstall_partial"))
    return result


def resolve_launch_bin64_dir(install_dir: Path) -> Path | None:
    """返回启动用的副本目录；未启用或副本不完整返回 None（回退真实 bin64）。

    纯只读，是否需要重建由调用方提前用 needs_regeneration() 处理。"""
    if uses_game_bin64(install_dir):
        return None  # 游戏专服的注入壳就在真实 bin64 里，直接从真实目录启动
    if not get_luajit_enabled():
        return None
    luajit_dir = get_luajit_dir(install_dir)
    if not _runtime_ready(install_dir):
        return None
    return luajit_dir


def needs_regeneration(install_dir: Path) -> bool:
    """已启用的隔离运行时是否需要在启动前修复或更新（纯本地读取）。

    除游戏与 Mod 版本外还校验布局、路径标记和 Winmm.dll 哈希：旧布局自动重建，
    作者只换 DLL 不改版本号时也能发现。
    """
    if uses_game_bin64(install_dir):
        return _game_needs_refresh(install_dir)
    if not get_luajit_enabled():
        return False
    luajit_dir = get_luajit_dir(install_dir)
    marker = read_marker(luajit_dir)
    if marker is None or not _runtime_ready(install_dir):
        return True
    current_build = current_game_build_id(install_dir)
    current_injector = current_injector_version()
    build_changed = current_build is not None and current_build != marker.DST_version
    injector_changed = current_injector is not None and current_injector != marker.luajit_version
    layout_changed = marker.layout_version != _LAYOUT_VERSION
    trigger_path = _trigger_source_file()
    trigger_changed = (
        trigger_path is not None
        and _file_sha256(trigger_path) != marker.trigger_sha256
    )
    marked_payload = read_injector_path_marker(install_dir)
    current_payload = _injector_payload_file()
    payload_moved = (
        marked_payload is not None
        and current_payload is not None
        and marked_payload.resolve() != current_payload.resolve()
    )
    return (
        build_changed
        or injector_changed
        or layout_changed
        or trigger_changed
        or payload_moved
    )


def _game_needs_refresh(install_dir: Path) -> bool:
    """游戏专服：仅在已安装时检查。路径标记缺失/过期需修复，注入壳不一致需更新；
    文件被游戏或服务器占用时沿用当前版本，不阻止开服。"""
    bin64_dir = install_dir / "bin64"
    trigger = _game_trigger_file(bin64_dir)
    current_payload = _injector_payload_file()
    if trigger is None or current_payload is None:
        return False
    marked = read_injector_path_marker(install_dir)
    if marked is None or marked.resolve() != current_payload.resolve():
        return True
    source = _trigger_source_file()
    if source is not None and _file_sha256(source) != _file_sha256(trigger):
        return not _file_in_use(trigger)
    return False


def _refresh_game_install(bin64_dir: Path, log) -> InstallResult:
    """游戏专服的启动前修复：写路径标记，必要时更新注入壳（被占用时跳过并说明）。"""
    result = InstallResult()
    install_dir = bin64_dir.parent
    source_dir = _injector_source_dir()
    injector_path = _injector_payload_file()
    trigger_path = _trigger_source_file(source_dir)
    if source_dir is None or injector_path is None or trigger_path is None:
        result.errors.append(t("local.luajit_error_no_injector_source"))
        log(result.errors[-1])
        return result
    try:
        current = _game_trigger_file(bin64_dir)
        if current is None or _file_sha256(current) != _file_sha256(trigger_path):
            if current is not None and _file_in_use(current):
                log(t("local.luajit_log_shell_locked"))
            else:
                _copy_injector_shell_into(source_dir, bin64_dir, on_log=log)
        write_injector_path_marker(install_dir, injector_path)
        _remove_stale_luajit_copy(install_dir, log)
        result.ok = True
    except Exception as exc:
        result.errors.append(t("local.luajit_error_operation_failed", detail=f"{type(exc).__name__}: {exc}"))
        log(result.errors[-1])
    return result


def regenerate(bin64_dir: Path, on_log=None) -> InstallResult:
    """副本过期时按变化选择性更新：

    - 游戏版本变化：删除并重建整个副本（bin64 可能全变）；
    - 只有配套 Mod 版本或 Winmm.dll 变化：只覆盖注入壳并刷新路径标记；
    - 标记缺失或布局需要迁移：完整重建，顺带清掉旧版复制进来的 Injector/deps/plugins。
    找不到完整订阅内容时返回失败，不用残缺文件启动。"""
    def log(line: str) -> None:
        if on_log:
            on_log(line)

    if uses_game_bin64(bin64_dir.parent):
        return _refresh_game_install(bin64_dir, log)

    result = InstallResult()
    install_dir = bin64_dir.parent
    luajit_dir = get_luajit_dir(install_dir)

    build_id = current_game_build_id(install_dir) or ""
    luajit_version = current_injector_version() or ""
    old_marker = read_marker(luajit_dir)
    build_changed = old_marker is None or build_id != old_marker.DST_version
    layout_changed = old_marker is None or old_marker.layout_version != _LAYOUT_VERSION

    try:
        source_dir = _injector_source_dir()
        injector_path = _injector_payload_file()
        trigger_path = _trigger_source_file(source_dir)
        if source_dir is None or injector_path is None or trigger_path is None:
            result.errors.append(t("local.luajit_error_no_injector_source"))
            log(result.errors[-1])
            return result

        if build_changed or layout_changed or not luajit_dir.is_dir():
            _rebuild_luajit_copy(bin64_dir, luajit_dir, source_dir, on_log=log)
        else:
            _copy_injector_shell_into(source_dir, luajit_dir, on_log=log)

        write_injector_path_marker(install_dir, injector_path)
        write_marker(luajit_dir, LuajitMarker(
            DST_version=build_id,
            luajit_version=luajit_version,
            layout_version=_LAYOUT_VERSION,
            trigger_sha256=_file_sha256(trigger_path),
        ))
        result.ok = True
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        result.errors.append(t("local.luajit_error_operation_failed", detail=detail))
        log(result.errors[-1])
    return result
