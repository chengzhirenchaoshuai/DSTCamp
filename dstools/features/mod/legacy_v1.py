"""Steam LegacyItem（V1 Mod）的安全校验、解压和运行目录部署。"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import secrets
import shutil
import stat
import threading
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from dstools.shared.resource_paths import cache_dir, data_dir


_MAX_ENTRY_COUNT = 50_000
_MAX_EXPANDED_SIZE = 4 * 1024 * 1024 * 1024
_CLIENT_PROCESS_NAMES = {
    "dontstarve_steam.exe",
    "dontstarve_steam_x64.exe",
}
_SERVER_PROCESS_NAMES = {
    "dontstarve_dedicated_server_nullrenderer.exe",
    "dontstarve_dedicated_server_nullrenderer_x64.exe",
}
_DST_PROCESS_NAMES = _CLIENT_PROCESS_NAMES | _SERVER_PROCESS_NAMES
_PACKAGE_VERSION_CACHE: dict[str, tuple[int, int, int, object]] = {}
_PACKAGE_VERSION_CACHE_LOCK = threading.RLock()
_READ_CACHE_MARKER = "source.json"


@dataclass(frozen=True)
class LegacyPackageValidation:
    valid: bool
    archive_path: Path
    entry_count: int = 0
    error: str = ""


@dataclass
class LegacyDeploymentResult:
    workshop_id: int
    archive_path: Path
    targets: list[Path] = field(default_factory=list)
    deployed: list[Path] = field(default_factory=list)
    already_current: list[Path] = field(default_factory=list)
    error: str = ""

    @property
    def completed(self) -> bool:
        return not self.error and bool(self.deployed or self.already_current)


@dataclass
class LegacyPreparationResult:
    """启动专服前把已启用 V1 包准备成专服可以读取的目录。"""

    checked: list[int] = field(default_factory=list)
    deployed: list[Path] = field(default_factory=list)
    already_current: list[Path] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    extras_removed: list[int] = field(default_factory=list)  # 删掉了多余文件的项目

    @property
    def completed(self) -> bool:
        return not self.errors


def validate_legacy_package(path: Path) -> LegacyPackageValidation:
    """验收 ``*_legacy.bin``；该文件实际上是游戏使用的 ZIP 包。"""
    archive = Path(path)
    if not archive.is_file():
        return LegacyPackageValidation(False, archive, error="Legacy Mod 下载包不存在")
    try:
        with zipfile.ZipFile(archive) as package:
            entries = package.infolist()
            if not entries:
                return LegacyPackageValidation(
                    False, archive, error="Legacy Mod 下载包为空"
                )
            if len(entries) > _MAX_ENTRY_COUNT:
                return LegacyPackageValidation(
                    False, archive, error="Legacy Mod 文件数量异常"
                )
            total = 0
            has_modinfo = False
            for entry in entries:
                normalized = entry.filename.replace("\\", "/")
                pure = PurePosixPath(normalized)
                if (
                    not normalized
                    or normalized.startswith("/")
                    or pure.is_absolute()
                    or ".." in pure.parts
                    or (pure.parts and ":" in pure.parts[0])
                ):
                    return LegacyPackageValidation(
                        False,
                        archive,
                        error=f"Legacy Mod 包含不安全路径：{entry.filename}",
                    )
                mode = (entry.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode):
                    return LegacyPackageValidation(
                        False,
                        archive,
                        error=f"Legacy Mod 包含不允许的符号链接：{entry.filename}",
                    )
                total += max(0, int(entry.file_size))
                if total > _MAX_EXPANDED_SIZE:
                    return LegacyPackageValidation(
                        False, archive, error="Legacy Mod 解压体积异常"
                    )
                if normalized.casefold() == "modinfo.lua":
                    has_modinfo = True
            if not has_modinfo:
                return LegacyPackageValidation(
                    False, archive, error="Legacy Mod 包缺少根目录 modinfo.lua"
                )
            bad = package.testzip()
            if bad:
                return LegacyPackageValidation(
                    False, archive, error=f"Legacy Mod CRC 校验失败：{bad}"
                )
            return LegacyPackageValidation(True, archive, len(entries), total)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        return LegacyPackageValidation(
            False, archive, error=f"Legacy Mod 下载包损坏：{exc}"
        )


def resolve_legacy_package_version(workshop_id: int, archive_path: Path):
    """执行包内原始 ``modinfo.lua``，作为无远程 version 标签时的基准。"""
    from dstools.features.mod.local_version import (
        LocalModVersion,
        normalize_version_result,
    )
    from dstools.features.mod.sandbox import resolve_mod_versions
    from dstools.features.mod.version_cache import load_package_version_result, save_package_version_result

    archive = Path(archive_path)
    try:
        stat_result = archive.stat()
        with zipfile.ZipFile(archive) as package:
            info = next(
                (
                    entry
                    for entry in package.infolist()
                    if entry.filename.replace("\\", "/").casefold() == "modinfo.lua"
                ),
                None,
            )
            if info is None:
                return LocalModVersion()
            key = str(archive.resolve(strict=False)).casefold()
            fingerprint = (stat_result.st_mtime_ns, stat_result.st_size, int(info.CRC))
            with _PACKAGE_VERSION_CACHE_LOCK:
                cached = _PACKAGE_VERSION_CACHE.get(key)
                if cached is not None and cached[:3] == fingerprint:
                    return cached[3]
            source_bytes = package.read(info)
        folder_name = f"workshop-{int(workshop_id)}"
        # 内存缓存只在本次运行有效；磁盘缓存免得每次启动都给所有 V1 包各跑一遍 Lua 沙箱
        result = load_package_version_result(str(workshop_id), archive, source_bytes, folder_name)
        if result is None:
            result = resolve_mod_versions(
                source_bytes.decode("utf-8", errors="replace"), folder_name=folder_name)
            if result is not None:
                save_package_version_result(str(workshop_id), archive, source_bytes, folder_name, result)
        resolved = normalize_version_result(result)
        with _PACKAGE_VERSION_CACHE_LOCK:
            _PACKAGE_VERSION_CACHE[key] = (*fingerprint, resolved)
        return resolved
    except (OSError, zipfile.BadZipFile, RuntimeError):
        return LocalModVersion()


def discover_legacy_runtime_targets() -> list[Path]:
    """返回 DSTCamp 应维护的 V1 运行目录（去重）：当前开服程序实际读取的 ``mods``。

    独立专服模式是专服 ``mods``，游戏客户端模式是客户端 ``mods``（客户端玩游戏时由官方客户端自己解压，
    不接管）；专服 ``mods`` 是指向客户端的 junction 时也只有一个目标。
    """
    from dstools.features.local_service.server_runtime import current_runtime

    runtime = current_runtime()
    candidates: list[Path] = [runtime.mods_dir] if runtime is not None else []

    result: list[Path] = []
    resolved: set[str] = set()
    for candidate in candidates:
        try:
            key = os.path.normcase(str(candidate.resolve(strict=False)))
        except OSError:
            key = os.path.normcase(str(candidate.absolute()))
        if key not in resolved:
            resolved.add(key)
            result.append(candidate)
    return result


def discover_legacy_runtime_roots() -> list[Path]:
    """返回游戏可能展开 V1 Mod 的客户端和专服 ``mods`` 目录。"""
    from dstools.features.local_service.dedicated_server import (
        find_dedicated_server_dir,
    )
    from dstools.features.mod.parser import find_game_mods_dir

    candidates: list[Path] = []
    client = find_game_mods_dir()
    if client is not None:
        candidates.append(Path(client))
    server = find_dedicated_server_dir()
    if server is not None:
        candidates.append(Path(server) / "mods")

    roots: list[Path] = []
    for candidate in candidates:
        if not any(_same_location(candidate, existing) for existing in roots):
            roots.append(candidate)
    return roots


def _is_v2_with_stray_package(archive: Path, installed_sizes: dict[int, int]) -> bool:
    """压缩包是否只是 V2 内容里夹带的旧文件（有的作者改成 V2 上传时把旧 *_legacy.bin 一起传了）。

    V1 项目 Steam 只安装这一个包，清单里的安装大小正好等于包的大小；V2 的安装大小是整个目录，
    不会相等。不能看目录里有没有 modinfo.lua：V1 目录被混入解压文件时也有。清单没有记录时才退回看它。
    """
    item_dir = archive.parent
    size = installed_sizes.get(int(item_dir.name))
    if size is not None:
        try:
            return size != archive.stat().st_size
        except OSError:
            return False
    return (item_dir / "modinfo.lua").is_file()


def _installed_sizes(root: Path) -> dict[int, int]:
    from dstools.features.mod.workshop_acf import read_acf_installed_sizes

    return read_acf_installed_sizes(root)


def find_legacy_packages() -> dict[int, Path]:
    """扫描 Steam 共享缓存中的 V1 包，每个项目只取最新的有效文件；V2 夹带的旧包不算。"""
    from dstools.features.mod.parser import find_workshop_dir, is_workshop_content_id

    root = find_workshop_dir()
    if root is None or not root.is_dir():
        return {}
    installed_sizes = _installed_sizes(root)
    packages: dict[int, Path] = {}
    for item_dir in root.iterdir():
        if not item_dir.is_dir() or not is_workshop_content_id(item_dir.name):
            continue
        candidates = []
        try:
            candidates = [
                path for path in item_dir.glob("*_legacy.bin") if path.is_file()
            ]
        except OSError:
            continue
        if not candidates:
            continue
        try:
            candidates.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)
        except OSError:
            continue
        valid = next(
            (path for path in candidates if validate_legacy_package(path).valid), None
        )
        if valid is not None and not _is_v2_with_stray_package(valid, installed_sizes):
            packages[int(item_dir.name)] = valid
    return packages


def find_legacy_package_ids() -> set[int]:
    """返回存在任意 ``*_legacy.bin`` 的项目，不要求包已经通过校验；V2 夹带的旧包不算。"""
    from dstools.features.mod.parser import find_workshop_dir, is_workshop_content_id

    root = find_workshop_dir()
    if root is None or not root.is_dir():
        return set()
    installed_sizes = _installed_sizes(root)
    result: set[int] = set()
    try:
        item_dirs = list(root.iterdir())
    except OSError:
        return result
    for item_dir in item_dirs:
        if not item_dir.is_dir() or not is_workshop_content_id(item_dir.name):
            continue
        try:
            if any(path.is_file() and not _is_v2_with_stray_package(path, installed_sizes)
                   for path in item_dir.glob("*_legacy.bin")):
                result.add(int(item_dir.name))
        except OSError:
            continue
    return result


def _is_workshop_item_dir(path: Path) -> bool:
    from dstools.features.mod.parser import is_workshop_content_id

    return is_workshop_content_id(path.name) and path.parent.name == "322330"


def legacy_package_extras(archive_path: Path) -> tuple[Path, ...]:
    """V1 项目目录里除 Steam 记录的 ``*_legacy.bin`` 以外的条目。

    坑（用户真机）：V1 项目目录里只要混入其他文件（如手动解压出的内容），游戏就不再把包解压到
    ``mods/workshop-<id>``，原地解压的文件也不会被当成 V2 加载，Mod 直接加载失败。
    """
    archive = Path(archive_path)
    item_dir = archive.parent
    if not archive.name.endswith("_legacy.bin") or not _is_workshop_item_dir(item_dir):
        return ()
    try:
        return tuple(sorted(path for path in item_dir.iterdir() if path.name != archive.name))
    except OSError:
        return ()


def remove_legacy_package_extras(archive_path: Path) -> tuple[Path, ...]:
    """删除 V1 项目目录里压缩包以外的全部条目，返回删除的路径；失败抛 OSError。"""
    removed = []
    for path in legacy_package_extras(archive_path):
        if os.path.isjunction(path):
            os.rmdir(path)
        elif path.is_symlink() or not path.is_dir():
            path.unlink(missing_ok=True)
        else:
            shutil.rmtree(path)
        removed.append(path)
    return tuple(removed)


_EXTRAS_LOG_MAX_BYTES = 256 * 1024


def legacy_extras_log_path() -> Path:
    """V1 目录多余文件的清理记录，日志包会一并收录。"""
    return data_dir("mod_repair") / "v1_extras.log"


def record_legacy_extras_removed(workshop_id: int, removed, source: str) -> None:
    """追加一行清理记录（时间、项目、来源、删掉的条目），用于追查多余文件从哪来；写失败不影响修复。"""
    if not removed:
        return
    names = "、".join(path.name for path in removed)
    stamp = _dt.datetime.now().isoformat(sep=" ", timespec="seconds")
    line = f"{stamp}  workshop-{int(workshop_id)}  {source}  {names}\n"
    path = legacy_extras_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and path.stat().st_size > _EXTRAS_LOG_MAX_BYTES:
            # 超过上限只保留后半部分，避免无限增长
            kept = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            path.write_text("".join(kept[len(kept) // 2:]), encoding="utf-8")
        with path.open("a", encoding="utf-8") as stream:
            stream.write(line)
    except OSError:
        pass


def materialize_legacy_package_for_read(
    workshop_id: int, archive_path: Path
) -> Path:
    """把有效 V1 包展开到内容寻址缓存，仅供元数据、图标和配置解析。"""
    workshop_id = int(workshop_id)
    archive = Path(archive_path)
    validation = validate_legacy_package(archive)
    if not validation.valid:
        raise OSError(validation.error)
    try:
        archive_stat = archive.stat()
        source_path = str(archive.resolve(strict=True))
    except OSError as exc:
        raise OSError(f"无法读取 Legacy Mod 下载包：{exc}") from exc

    digest = hashlib.sha256()
    with archive.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    content_hash = digest.hexdigest()
    source_hash = hashlib.sha256(
        os.path.normcase(source_path).encode("utf-8", errors="surrogatepass")
    ).hexdigest()[:12]
    item_root = cache_dir("legacy_v1_read") / str(workshop_id)
    bundle = item_root / f"{source_hash}-{content_hash}"
    mod_root = bundle / f"workshop-{workshop_id}"
    marker = bundle / _READ_CACHE_MARKER
    expected = {
        "workshop_id": workshop_id,
        "source_path": source_path,
        "source_size": archive_stat.st_size,
        "source_mtime_ns": archive_stat.st_mtime_ns,
        "sha256": content_hash,
    }
    try:
        cached = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        cached = None
    if cached == expected and (mod_root / "modinfo.lua").is_file():
        return mod_root

    item_root.mkdir(parents=True, exist_ok=True)
    stage = item_root / f".stage-{secrets.token_hex(6)}"
    stage_mod = stage / f"workshop-{workshop_id}"
    try:
        stage_mod.mkdir(parents=True)
        with zipfile.ZipFile(archive) as package:
            _safe_extract(package, stage_mod)
        if not (stage_mod / "modinfo.lua").is_file():
            raise OSError("Legacy Mod 解析缓存缺少 modinfo.lua")
        (stage / _READ_CACHE_MARKER).write_text(
            json.dumps(expected, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        stale = None
        if os.path.lexists(bundle):
            stale = item_root / f".stale-{secrets.token_hex(6)}"
            bundle.rename(stale)
        try:
            stage.rename(bundle)
        except FileExistsError:
            try:
                concurrent = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                concurrent = None
            if concurrent == expected and (mod_root / "modinfo.lua").is_file():
                return mod_root
            raise
        except Exception:
            if stale is not None and not os.path.lexists(bundle):
                stale.rename(bundle)
            raise
        if stale is not None:
            if os.path.isjunction(stale):
                os.rmdir(stale)
            elif stale.is_dir() and not stale.is_symlink():
                shutil.rmtree(stale, ignore_errors=True)
            else:
                stale.unlink(missing_ok=True)
        if not (mod_root / "modinfo.lua").is_file():
            raise OSError("Legacy Mod 解析缓存创建失败")
        return mod_root
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)


def is_legacy_read_cache_path(path: Path) -> bool:
    """判断路径是否位于 V1 只读解析缓存，避免把缓存当成运行目录。"""
    candidate = Path(path)
    root = cache_dir("legacy_v1_read")
    try:
        candidate.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False


def find_legacy_runtime_residual_dirs() -> dict[int, tuple[Path, ...]]:
    """查找已无 V1 包、但仍留在标准 ``mods`` 目录的运行文件夹。"""
    from dstools.features.mod.parser import is_workshop_content_id

    packaged_ids = find_legacy_package_ids()
    residuals: dict[int, list[Path]] = {}
    for root in discover_legacy_runtime_roots():
        if not root.is_dir():
            continue
        try:
            children = list(root.iterdir())
        except OSError:
            continue
        for child in children:
            name = child.name
            if not name.startswith("workshop-"):
                continue
            text_id = name.removeprefix("workshop-")
            if (
                not is_workshop_content_id(text_id)
                or int(text_id) in packaged_ids
                or child.is_symlink()
                or os.path.isjunction(child)
                or not child.is_dir()
                or not (child / "modinfo.lua").is_file()
            ):
                continue
            paths = residuals.setdefault(int(text_id), [])
            if not any(_same_location(child, existing) for existing in paths):
                paths.append(child)
    return {workshop_id: tuple(paths) for workshop_id, paths in residuals.items()}


def legacy_runtime_matches_package(archive_path: Path, runtime_dir: Path) -> bool:
    """逐文件按大小和 CRC 核对运行目录是否与 Legacy 包一致（作者可能更新文件却不改 version），忽略 Mod 自己生成的额外文件。"""
    archive = Path(archive_path)
    runtime = Path(runtime_dir)
    if not runtime.is_dir() or not (runtime / "modinfo.lua").is_file():
        return False
    try:
        with zipfile.ZipFile(archive) as package:
            for entry in package.infolist():
                normalized = entry.filename.replace("\\", "/")
                if entry.is_dir() or normalized.endswith("/"):
                    continue
                target = runtime.joinpath(*PurePosixPath(normalized).parts)
                if not target.is_file() or target.stat().st_size != entry.file_size:
                    return False
                crc = 0
                with target.open("rb") as source:
                    while chunk := source.read(1024 * 1024):
                        crc = zlib.crc32(chunk, crc)
                if (crc & 0xFFFFFFFF) != entry.CRC:
                    return False
        return True
    except (OSError, zipfile.BadZipFile, RuntimeError):
        return False


def prepare_enabled_legacy_mods(
    workshop_ids, server_mods_root: Path
) -> LegacyPreparationResult:
    """启动前用本地已校验的 Legacy 包原子部署当前存档启用的 V1 Mod。

    真机确认：独立专服只有在 dedicated_server_mods_setup.lua 写了 ServerModSetup(id) 时才会下载并
    解压 V1，只靠 modoverrides.lua 会静默跳过，所以由 DSTCamp 自行部署。
    V2 项目不会出现在 find_legacy_packages 里（含夹带旧包的），不会被解压去挡住 V2（见 v1_shadow.py）。
    V1 目录混入的其他文件会先删掉，否则游戏客户端不再解压它。
    """
    result = LegacyPreparationResult()
    packages = find_legacy_packages()
    root = Path(server_mods_root)
    normalized_ids = []
    for value in workshop_ids:
        text = str(value).removeprefix("workshop-")
        if text.isdigit() and int(text) > 0:
            normalized_ids.append(int(text))
    for workshop_id in dict.fromkeys(normalized_ids):
        archive = packages.get(workshop_id)
        if archive is None:
            continue
        result.checked.append(workshop_id)
        try:
            removed = remove_legacy_package_extras(archive)
        except OSError as exc:
            result.errors.append(f"workshop-{workshop_id} 目录里的多余文件无法删除：{exc}")
            continue
        if removed:
            result.extras_removed.append(workshop_id)
            record_legacy_extras_removed(workshop_id, removed, "开服前")
        target = root / f"workshop-{workshop_id}"
        if legacy_runtime_matches_package(archive, target):
            result.already_current.append(target)
            continue
        deployment = deploy_legacy_package(
            workshop_id,
            archive,
            target_roots=[root],
            force=True,
        )
        if deployment.completed:
            result.deployed.extend(deployment.deployed)
            result.already_current.extend(deployment.already_current)
        else:
            result.errors.append(deployment.error or f"workshop-{workshop_id} 部署失败")
    return result


def running_dst_processes() -> tuple[str, ...]:
    """只读检测可能占用 V1 运行目录的游戏/专服进程（Toolhelp 快照，不起 tasklist 子进程）。"""
    from dstools.shared.process_snapshot import snapshot_processes

    found = []
    for _pid, _parent, image in snapshot_processes() or ():
        if image in _DST_PROCESS_NAMES and image not in found:
            found.append(image)
    return tuple(found)


def is_dst_client_running() -> bool:
    """游戏客户端（不含专服）是否在运行。"""
    return any(name in _CLIENT_PROCESS_NAMES for name in running_dst_processes())


def _safe_extract(package: zipfile.ZipFile, stage: Path) -> None:
    for entry in package.infolist():
        normalized = entry.filename.replace("\\", "/")
        pure = PurePosixPath(normalized)
        mode = (entry.external_attr >> 16) & 0xFFFF
        if (
            not normalized
            or normalized.startswith("/")
            or pure.is_absolute()
            or ".." in pure.parts
            or (pure.parts and ":" in pure.parts[0])
            or stat.S_ISLNK(mode)
        ):
            raise OSError(f"Legacy Mod 包含不安全条目：{entry.filename}")
        parts = pure.parts
        target = stage.joinpath(*parts)
        if entry.is_dir() or normalized.endswith("/"):
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with package.open(entry) as source, target.open("wb") as output:
            shutil.copyfileobj(source, output, length=1024 * 1024)


def _next_sibling(target: Path, kind: str) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(6)
    return target.with_name(f".dstcamp-v1-{kind}-{target.name}-{token}")


def _deploy_one(archive: Path, target: Path) -> bool:
    """部署到单个目录；返回是否实际替换，失败时恢复原目录。"""
    if os.path.isjunction(target):
        raise OSError(f"目标 Mod 目录是未知联接，已停止替换：{target}")
    stage = _next_sibling(target, "stage")
    backup = _next_sibling(target, "backup")
    moved_old = False
    try:
        stage.mkdir(parents=False)
        with zipfile.ZipFile(archive) as package:
            _safe_extract(package, stage)
        if not (stage / "modinfo.lua").is_file():
            raise OSError("解压结果缺少 modinfo.lua")
        if os.path.lexists(target):
            if not target.is_dir():
                raise OSError(f"目标不是普通 Mod 目录：{target}")
            target.rename(backup)
            moved_old = True
        stage.rename(target)
        if moved_old:
            shutil.rmtree(backup)
        return True
    except Exception:
        if os.path.lexists(target) and moved_old and os.path.lexists(backup):
            shutil.rmtree(target, ignore_errors=True)
        if moved_old and os.path.lexists(backup) and not os.path.lexists(target):
            backup.rename(target)
        raise
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)


def deploy_legacy_package(
    workshop_id: int,
    archive_path: Path,
    *,
    target_roots: list[Path] | tuple[Path, ...] | None = None,
    force: bool = True,
) -> LegacyDeploymentResult:
    """把一个 V1 包原子部署到 DSTCamp 管理的专服 ``mods`` 目录。"""
    workshop_id = int(workshop_id)
    archive = Path(archive_path)
    result = LegacyDeploymentResult(workshop_id, archive)
    validation = validate_legacy_package(archive)
    if not validation.valid:
        result.error = validation.error
        return result
    roots = (
        list(target_roots)
        if target_roots is not None
        else discover_legacy_runtime_targets()
    )
    result.targets = [Path(root) / f"workshop-{workshop_id}" for root in roots]
    if not result.targets:
        result.error = "没有找到游戏或专用服务器的 mods 运行目录"
        return result
    try:
        for target in result.targets:
            # 同一个真实目录可能经 junction 以不同表面路径出现，再做一次防重。
            if any(_same_location(target, previous) for previous in result.deployed):
                continue
            if not force and (target / "modinfo.lua").is_file():
                result.already_current.append(target)
                continue
            _deploy_one(archive, target)
            result.deployed.append(target)
    except Exception as exc:
        result.error = f"Legacy Mod 部署失败：{exc}"
    return result




def _same_location(left: Path, right: Path) -> bool:
    try:
        return os.path.samefile(left, right)
    except OSError:
        return os.path.normcase(str(left.resolve(strict=False))) == os.path.normcase(
            str(right.resolve(strict=False))
        )
