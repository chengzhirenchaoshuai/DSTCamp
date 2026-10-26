"""统一解析只读发布资源与可写运行时目录。

固定资源（每次启动都要用，随 EXE 打包，只读）：
    icons/{app,ui,world,avatars,recommended,ornaments}  tools/{fonts,ktools,vcredist,frp_selfhost,frpc-sakura,steamcmd,hdiffpatch}

%APPDATA%/DSTCamp/（运行时可写）：
    settings.json   全部用户设置
    cache/          可随时清空、按需重建（可在设置中改位置，须为纯 ASCII 路径）：
                    mod_full_resolve、modinfo_static、mod_versions、mod_chs_translation、mod_icons/<平台>、legacy_v1_read、
                    world_mod_icons、character_icons、connection_log、frpc_log（frpc 输出，日志包会收录）、
                    lobby_accel_mihomo、runtime/{ktools,ktech_jobs}、
                    steamcmd（内置 SteamCMD 及其自更新组件、日志和登录缓存，清空后下次使用重新释放并需重新登录）
    data/           需要保留的用户数据：background、player_registry、port_backups、auto_restart、updates、
                    runtime_tools（长驻工具的解压副本）、tools（按需下载或从工具包导入的工具，见 tool_store.py）、
                    frpc_config / frp_selfhost_config / lolia_frpc_config、
                    mod_config_memory（Mod 配置记忆 memory.json 与覆盖前的游戏配置备份 game_backup/）
                    mod_repair（V1 Mod 目录多余文件的清理记录 v1_extras.log，日志包会收录）
    security/       凭据与主机信任：frp_selfhost（SSH 密钥/known_hosts）、lobby_accel_wireguard、
                    lobby_accel_mihomo、lolia（OAuth 令牌）
"""

import hashlib
import lzma
import os
import secrets
import sys
import shutil
from pathlib import Path

from dstools.shared.app_settings import get_settings_dir


def bundled_resource_dir() -> Path:
    """返回只读素材根目录；onefile 运行时位于 ``sys._MEIPASS``。"""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)
    return Path(__file__).parent.parent.parent


def exe_dir() -> Path:
    """返回可执行文件目录；源码运行时返回仓库根目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent.parent.parent


def tool_binary_dir() -> Path:
    """返回第三方工具目录，兼容内嵌版和 ZIP 外置版。"""
    if getattr(sys, "frozen", False):
        # 单文件版优先取临时展开目录；ZIP 版回退到 exe 同级目录。
        bundled_tools = Path(sys._MEIPASS) / "tools"
        if bundled_tools.is_dir():
            return bundled_tools
        return exe_dir() / "tools"
    return Path(__file__).parent.parent.parent / "tools"


def runtime_tool_path(relative: str | Path) -> Path:
    """返回长驻子进程可用的工具路径。

    tools/ 中的可执行文件以 ``.xz`` 打包（比 gzip 小约 25%）：onefile 每次启动都会把 tools/ 解压到新的
    _MEI 临时目录，裸 exe（尤其 frp 被杀软归为 HackTool）每次启动都会被隔离。所以优先读 ``.xz`` 现场解压，
    按内容哈希落地到 data/runtime_tools（_MEI 目录随主程序退出回收，长驻进程不能用）；没有 ``.xz`` 才读裸文件。
    源码/ZIP 版的裸文件存在时直接使用（仓库只提交了 .xz，源码模式同样要解压）。
    不打进 EXE 的工具（登记在 tool_store.TOOLS）再找 data/tools 里已下载或导入的副本。
    """
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"工具路径必须相对 tools 目录：{relative}")
    source = tool_binary_dir() / relative
    xz_source = source.with_name(source.name + ".xz")
    bundled_root = Path(getattr(sys, "_MEIPASS", "")) / "tools"
    is_onefile_bundle = (
        getattr(sys, "frozen", False) and source.parent == bundled_root / relative.parent
    )
    if source.is_file() and not is_onefile_bundle:
        return source

    if xz_source.is_file():
        data = lzma.decompress(xz_source.read_bytes())
    elif source.is_file():
        data = source.read_bytes()
    else:
        # 不打进 EXE 的工具：用已下载或导入的副本，都没有时返回不存在的路径，由调用方提示下载
        from dstools.shared.tool_store import find_stored, store_tool

        stored = find_stored(relative.as_posix() + ".xz") or find_stored(relative.as_posix())
        if stored is None:
            # 旧版随包时解压过的副本（落地规则同下）还在就沿用，升级后不必为它重新下载
            tool = store_tool(relative.as_posix() + ".xz")
            if tool is not None and tool.unpacked_sha256:
                previous = data_dir("runtime_tools") / tool.unpacked_sha256[:16] / relative
                if previous.is_file() and hashlib.sha256(previous.read_bytes()).hexdigest() == tool.unpacked_sha256:
                    return previous
            return source
        data = lzma.decompress(stored.read_bytes()) if stored.suffix == ".xz" else stored.read_bytes()

    source_digest = hashlib.sha256(data).hexdigest()
    target = data_dir("runtime_tools") / source_digest[:16] / relative
    if (
        target.is_file()
        and hashlib.sha256(target.read_bytes()).hexdigest() == source_digest
    ):
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_bytes(data)
    try:
        os.replace(temporary, target)
    except OSError:
        temporary.unlink(missing_ok=True)
        if not target.is_file():
            raise
    return target


def default_cache_root_dir() -> Path:
    """返回不含用户覆盖值的默认缓存目录。"""
    return get_settings_dir() / "cache"


def cache_root_dir() -> Path:
    """返回缓存根目录，并兼容旧版“缓存跟随 EXE”设置。"""
    from dstools.shared.app_settings import (
        get_cache_dir_override,
        get_cache_use_exe_dir,
    )

    override = get_cache_dir_override()
    if override is not None:
        return override
    return exe_dir() / "cache" if get_cache_use_exe_dir() else default_cache_root_dir()


def path_is_ascii(path: str | Path) -> bool:
    """路径传给旧版 ktech 前必须能完整编码成 ASCII。"""
    try:
        str(path).encode("ascii")
    except UnicodeEncodeError:
        return False
    return True


def validate_cache_root(path: str | Path) -> str | None:
    """验证缓存目录能否存放并运行 ktools，通过返回 None，否则返回错误码（用 Python 宽字符 API 测试，不交给 ktech）。"""
    candidate = Path(path)
    if not candidate.is_absolute():
        return "not_absolute"
    if not path_is_ascii(candidate):
        return "non_ascii"
    probe = candidate / f".dstcamp-write-test-{secrets.token_hex(6)}"
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        probe.write_bytes(b"dstcamp")
        probe.unlink()
    except OSError:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass
        return "not_writable"
    return None


def cache_dir(name: str) -> Path:
    """返回具名缓存子目录，但不主动创建。"""
    return cache_root_dir() / name


def _persistent_dir(kind: str, name: str, legacy_cache_name: str | None) -> Path:
    target = get_settings_dir() / kind / name
    if not legacy_cache_name or target.exists():
        return target
    legacy = cache_root_dir() / legacy_cache_name
    if not legacy.exists():
        return target
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(legacy), str(target))
    except OSError:
        # 迁移失败时保留旧目录，调用方仍可创建新位置。
        pass
    return target


def data_dir(name: str, *, legacy_cache_name: str | None = None) -> Path:
    """返回不可随缓存清理的应用数据目录，并迁移旧缓存位置。"""
    return _persistent_dir("data", name, legacy_cache_name)


def security_dir(name: str, *, legacy_cache_name: str | None = None) -> Path:
    """返回凭据与主机信任目录，并迁移旧缓存位置。"""
    return _persistent_dir("security", name, legacy_cache_name)
