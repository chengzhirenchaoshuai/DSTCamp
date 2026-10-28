"""下载、校验并安排替换冻结版 DSTCamp EXE。"""

from __future__ import annotations

import functools
import http.client
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import zipfile
from pathlib import Path

from dstools.shared.http_download import (
    ProgressCallback,
    download_verified as _download_verified,
    file_sha256 as _file_sha256,
    matches as _matches,
)
from dstools.shared.resource_paths import data_dir, tool_binary_dir
from dstools import __version__
from dstools.shared.update_check import UpdateRelease, is_newer_version
from dstools.shared.update_signing import verify_manifest

# 标准安装固定使用这个文件名：随版本改名会让任务栏/桌面快捷方式失效，也无法原地覆盖
STANDARD_EXE_NAME = "DSTCamp.exe"
# 旧版本按 Release 文件名逐版本改名；识别出这类命名是为了把它们一次性
# 迁移到固定的 STANDARD_EXE_NAME，之后不再改名。
_LEGACY_VERSIONED_EXE_NAME_RE = re.compile(
    r"^DSTCamp-\d+(?:\.\d+)+(?:[-+][0-9A-Za-z.-]+)?\.exe$", re.IGNORECASE
)
# 隐藏 staged 文件的命名规律，launch_update_helper() 与 cleanup_stale_update_artifacts() 共用
_HIDDEN_STAGED_GLOB = ".*.update-*.exe"
# 与 _helper_script() 里 PS1 模板内 $LogFile 的文件名保持一致。
_UPDATE_LOG_NAME = "apply_update.log"
# 与 _helper_script() 里的路径保持一致；清理下载缓存目录时需要跳过它。
_HELPER_SCRIPT_NAME = "apply_update.ps1"
_download_lock = threading.Lock()
_logger = logging.getLogger(__name__)


def _hidden_staged_name(stem: str, pid: int) -> str:
    return f".{stem}.update-{pid}.exe"


def _hpatchz() -> Path | None:
    """补丁合成工具；只有冻结版（正在运行的就是正式发布的 EXE）才能用补丁。"""
    if not getattr(sys, "frozen", False):
        return None
    hpatchz = tool_binary_dir() / "hdiffpatch" / "hpatchz.exe"
    return hpatchz if hpatchz.is_file() else None


def _apply_patch(hpatchz: Path, patch_file: Path, target: Path, size: int, sha256: str) -> bool:
    """用补丁把正在运行的 EXE 合成到 target，结果与清单一致才返回 True。"""
    built = target.with_name(target.name + ".patched")
    result = subprocess.run(
        [str(hpatchz), "-f", sys.executable, str(patch_file), str(built)],
        capture_output=True, text=True, timeout=300, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode == 0 and _matches(built, size, sha256):
        os.replace(built, target)
        return True
    _logger.warning("补丁合成失败（退出码 %s）：%s", result.returncode, (result.stdout + result.stderr).strip())
    _safe_unlink(built)
    return False


def _build_from_patch(release: UpdateRelease, target: Path, progress: ProgressCallback | None) -> bool:
    """用补丁在本地合成新版 EXE，成功返回 True；不适用或任何一步失败返回 False，由调用方改下整包。

    补丁基于正式发布的旧版 EXE，只有正在运行的 EXE 与 from_sha256 一致时才用。
    """
    patch = release.patch
    hpatchz = _hpatchz()
    if patch is None or hpatchz is None:
        return False
    try:
        if _file_sha256(Path(sys.executable)) != patch.from_sha256:
            return False
        patch_file = target.with_name(f"DSTCamp-{__version__}-to-{release.version}.hdiff")
        if not _matches(patch_file, patch.size, patch.sha256):
            _download_verified((patch.url, *patch.fallback_urls), patch_file, patch.size, patch.sha256, progress)
        try:
            return _apply_patch(hpatchz, patch_file, target, release.size, release.sha256)
        finally:
            _safe_unlink(patch_file)  # 成功后用不到；补丁校验过仍合成失败也别再复用
    except (OSError, http.client.HTTPException, subprocess.SubprocessError):
        _logger.warning("补丁更新不可用，改为下载完整安装包", exc_info=True)
    return False


@functools.lru_cache(maxsize=1)
def _running_exe_sha256() -> str:
    """正在运行的 EXE 运行期间不会变，算一次即可（约 50 MB，避免每次打开更新提示都重算）。"""
    return _file_sha256(Path(sys.executable))


def planned_download(release: UpdateRelease) -> tuple[str, int]:
    """更新提示里显示的下载量，判断与 download_update 实际走的分支一致：
    ("ready", 0) 已下载好；("patch", 补丁大小) 本机是补丁起点的原版 EXE；("full", 整包大小) 其余情况。"""
    target = data_dir("updates") / release.version / f"DSTCamp-{release.version}.exe"
    try:
        if _matches(target, release.size, release.sha256):
            return "ready", 0
        patch = release.patch
        if patch is not None and _hpatchz() is not None and _running_exe_sha256() == patch.from_sha256:
            return "patch", patch.size
    except OSError:
        pass
    return "full", release.size


def download_update(release: UpdateRelease, progress: ProgressCallback | None = None) -> Path:
    """准备新版 EXE 到持久更新目录，严格校验长度和 SHA-256；之前已准备好时直接复用。

    优先下载小补丁在本地合成（通常几 MB），不适用或失败时下载整包。主下载源失败时依次换备用源。
    后台预下载与"立即更新"可能同时调用，用锁串行：后到的等前一个结束后直接复用结果。
    """
    if not release.can_auto_update:
        raise ValueError("该发行版缺少自动更新文件或 SHA-256 清单")
    target_dir = data_dir("updates") / release.version
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"DSTCamp-{release.version}.exe"
    with _download_lock:
        if _matches(target, release.size, release.sha256):
            if progress is not None:
                progress(release.size, release.size)
            return target
        if not _build_from_patch(release, target, progress):
            _download_verified(
                (release.exe_url, *release.fallback_urls), target, release.size, release.sha256, progress
            )
        return target


class UpdatePackageError(Exception):
    """离线更新包不可用；消息直接展示给用户。"""


_PACKAGE_MANIFEST_RE = re.compile(r"DSTCamp-\d+(?:\.\d+)+\.sha256\.json")


def is_update_package(package: Path) -> bool:
    """zip 里带 DSTCamp 更新清单就是更新包（完整包或补丁包）。"""
    try:
        with zipfile.ZipFile(package) as archive:
            return any(_PACKAGE_MANIFEST_RE.fullmatch(name) for name in archive.namelist())
    except (OSError, zipfile.BadZipFile):
        return False


def _extract_verified(archive: zipfile.ZipFile, name: str, target: Path, size: int, sha256: str) -> None:
    temporary = target.with_name(target.name + ".part")
    with archive.open(name) as source, temporary.open("wb") as output:
        shutil.copyfileobj(source, output, 1024 * 1024)
    if not _matches(temporary, size, sha256):
        temporary.unlink(missing_ok=True)
        raise UpdatePackageError(f"{name} 校验失败，文件可能没下载完整，请重新下载")
    os.replace(temporary, target)


def import_update_package(package: Path) -> tuple[str, Path]:
    """校验网盘下载的离线包，把新版 EXE 准备到更新目录，返回 (版本号, EXE 路径)。

    清单必须带发布者签名（断网也能校验）。包里有完整 EXE 就直接用，否则找"从当前版本升级"的补丁合成。
    之后与自动更新走同一套冒烟启动和替换流程。
    """
    try:
        with zipfile.ZipFile(package) as archive:
            names = set(archive.namelist())
            manifest_names = [name for name in names if _PACKAGE_MANIFEST_RE.fullmatch(name)]
            if len(manifest_names) != 1:
                raise UpdatePackageError("这不是 DSTCamp 更新包")
            manifest = json.loads(archive.read(manifest_names[0]).decode("utf-8"))
            if not isinstance(manifest, dict) or not verify_manifest(manifest):
                raise UpdatePackageError("更新包签名校验失败，可能被篡改，请从官方渠道重新下载")
            version = str(manifest.get("version") or "")
            if not is_newer_version(__version__, version):
                raise UpdatePackageError(f"更新包版本 v{version} 不比当前版本 v{__version__} 新")
            exe_name = f"DSTCamp-{version}.exe"
            exe_info = manifest["files"][exe_name]
            size, sha256 = int(exe_info["size"]), str(exe_info["sha256"])
            target_dir = data_dir("updates") / version
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / exe_name
            with _download_lock:
                if _matches(target, size, sha256):
                    return version, target
                if exe_name in names:
                    _extract_verified(archive, exe_name, target, size, sha256)
                    return version, target
                patch_info = (manifest.get("patches") or {}).get(__version__) or {}
                patch_name = str(patch_info.get("name") or "")
                if patch_name not in names:
                    raise UpdatePackageError(f"这个补丁包里没有从当前版本 v{__version__} 升级的补丁，请下载完整包")
                hpatchz = _hpatchz()
                if hpatchz is None or _file_sha256(Path(sys.executable)) != str(patch_info.get("from_sha256")):
                    raise UpdatePackageError("当前程序不是官方发布的原版文件，不能用补丁升级，请下载完整包")
                patch_file = target_dir / patch_name
                _extract_verified(archive, patch_name, patch_file, int(patch_info["size"]), str(patch_info["sha256"]))
                try:
                    if not _apply_patch(hpatchz, patch_file, target, size, sha256):
                        raise UpdatePackageError("补丁合成失败，请下载完整包")
                finally:
                    _safe_unlink(patch_file)
                return version, target
    except zipfile.BadZipFile as exc:
        raise UpdatePackageError("文件不是有效的 zip，可能没下载完整") from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise UpdatePackageError("更新包内容不完整") from exc


def _helper_script() -> Path:
    path = data_dir("updates") / _HELPER_SCRIPT_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    content = r'''param(
    [int]$ParentPid,
    [string]$CurrentExe,
    [string]$NewExe,
    [string]$TargetExe,
    [string]$BackupExe,
    [string]$TargetBackupExe
)
$ErrorActionPreference = 'Stop'
$LogFile = Join-Path (Split-Path -Parent $NewExe) 'apply_update.log'
# 坑：旧进程恰在本脚本启动时退出、而 onefile 启动器还握着它的句柄时，Wait-Process 抛出
# "进程已退出，因此无法处理请求"（InvalidOperationException）。这类异常不受 -ErrorAction 控制，
# 在 Stop 下会直接终止整个脚本：不替换、不写日志、也不重启（1.7.2 测试实测）。所以包在 try 里，出错即视为已退出。
try { Wait-Process -Id $ParentPid -Timeout 60 -ErrorAction Stop } catch { }
function Move-WithRetry([string]$Path, [string]$Destination) {
    # 旧程序的 onefile 启动器退出前还在清理临时目录，可能短暂占用 EXE：最多重试约 10 秒
    for ($attempt = 1; ; $attempt++) {
        try { Move-Item -LiteralPath $Path -Destination $Destination; return }
        catch { if ($attempt -ge 40) { throw }; Start-Sleep -Milliseconds 250 }
    }
}
# PyInstaller 6.9+ 默认让同一 onefile EXE 的子进程复用父进程 _MEI。
# 更新后的程序必须独立解压；否则旧进程清理临时目录后会找不到 python DLL。
$env:PYINSTALLER_RESET_ENVIRONMENT = '1'
$MovedTarget = $false
$MovedCurrent = $false
$InstalledTarget = $false
try {
    # 旧的带版本号安装名迁移到固定名字时，同目录可能已有一份旧的固定名字 EXE：
    # 先备份成 .old（下次启动清理），失败时还原
    if (($TargetExe -ne $CurrentExe) -and (Test-Path -LiteralPath $TargetExe)) {
        if (Test-Path -LiteralPath $TargetBackupExe) { Remove-Item -LiteralPath $TargetBackupExe -Force }
        Move-WithRetry $TargetExe $TargetBackupExe
        $MovedTarget = $true
    }
    if (Test-Path -LiteralPath $BackupExe) { Remove-Item -LiteralPath $BackupExe -Force }
    Move-WithRetry $CurrentExe $BackupExe
    $MovedCurrent = $true
    Move-Item -LiteralPath $NewExe -Destination $TargetExe
    $InstalledTarget = $true
    Start-Process -FilePath $TargetExe -WorkingDirectory (Split-Path -Parent $TargetExe)
}
catch {
    # 先记下失败原因，回滚本身再出错也不丢；重启后的程序会读出并提示
    $_ | Out-String | Set-Content -LiteralPath $LogFile -Encoding UTF8
    try {
        if ($InstalledTarget -and (Test-Path -LiteralPath $TargetExe)) {
            Remove-Item -LiteralPath $TargetExe -Force
        }
        if ($MovedTarget -and (Test-Path -LiteralPath $TargetBackupExe)) {
            Move-Item -LiteralPath $TargetBackupExe -Destination $TargetExe
        }
        if ($MovedCurrent -and (Test-Path -LiteralPath $BackupExe)) {
            if (Test-Path -LiteralPath $CurrentExe) { Remove-Item -LiteralPath $CurrentExe -Force }
            Move-Item -LiteralPath $BackupExe -Destination $CurrentExe
        }
    }
    catch {
        $_ | Out-String | Add-Content -LiteralPath $LogFile -Encoding UTF8
    }
    # 不管在哪一步失败，都把原程序重新启动，避免更新后程序直接消失
    if (Test-Path -LiteralPath $CurrentExe) {
        Start-Process -FilePath $CurrentExe -WorkingDirectory (Split-Path -Parent $CurrentExe)
    }
}
'''
    # 按 utf-8-sig 写入（Windows PowerShell 5.1 读无 BOM 的中文注释会乱码），比较时也要去掉 BOM
    if not path.is_file() or path.read_text(encoding="utf-8-sig") != content:
        path.write_text(content, encoding="utf-8-sig")
    return path


def validate_staged_executable(staged_exe: Path) -> None:
    """替换前实际启动新 EXE 的发布冒烟入口。"""
    result = subprocess.run(
        [str(staged_exe), "--smoke-test"],
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    if result.returncode:
        detail = (result.stdout + result.stderr).strip()
        raise RuntimeError(f"新版本启动验证失败：{detail or result.returncode}")


def ensure_install_dir_writable() -> None:
    """在退出当前程序前确认 EXE 所在目录允许创建和替换文件。"""
    current = Path(sys.executable).resolve()
    probe = current.parent / f".dstcamp-update-probe-{os.getpid()}"
    try:
        probe.write_bytes(b"dstcamp")
    finally:
        probe.unlink(missing_ok=True)


def ensure_install_target_replaceable() -> None:
    """退出前确认安装目标能被覆盖：迁移到固定名字时同目录已有的那份不能正在运行。"""
    current = Path(sys.executable).resolve()
    target = resolve_install_target(current)
    if target == current or not target.exists():
        return
    try:
        # 正在运行的 EXE 无法以写方式打开；追加模式打开不改动文件内容
        with target.open("ab"):
            pass
    except OSError as exc:
        raise RuntimeError(f"同目录的 {target.name} 正在运行或被占用，请先关闭它再更新") from exc


def take_update_failure_log() -> str:
    """读出并删除上次更新助手留下的失败日志；没有则返回空字符串。须在清理残留前调用。"""
    if not getattr(sys, "frozen", False):
        return ""
    log = Path(sys.executable).resolve().parent / _UPDATE_LOG_NAME
    try:
        text = log.read_text(encoding="utf-8-sig", errors="replace").strip()
    except OSError:
        return ""
    _safe_unlink(log)
    return text


def resolve_install_target(current_exe: Path) -> Path:
    """标准安装固定用 STANDARD_EXE_NAME，用户自定义的 EXE 名称保持不变；旧的带版本号安装名在本次迁移。"""
    current = Path(current_exe).resolve()
    is_standard = current.name == STANDARD_EXE_NAME or bool(
        _LEGACY_VERSIONED_EXE_NAME_RE.fullmatch(current.name)
    )
    name = STANDARD_EXE_NAME if is_standard else current.name
    return current.with_name(name)


def launch_update_helper(staged_exe: Path) -> None:
    """启动独立 PowerShell，当前进程退出后替换并以安装目标名称重启。"""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        raise RuntimeError("自动替换仅支持 Windows 冻结版")
    current = Path(sys.executable).resolve()
    staged = staged_exe.resolve()
    if not staged.is_file() or staged == current:
        raise FileNotFoundError(staged)
    target = resolve_install_target(current)
    backup = current.with_name(current.name + ".old")
    local_staged = current.with_name(_hidden_staged_name(current.stem, os.getpid()))
    shutil.copy2(staged, local_staged)
    command = [
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
        str(_helper_script()), "-ParentPid", str(os.getpid()), "-CurrentExe",
        str(current), "-NewExe", str(local_staged), "-TargetExe", str(target),
        "-BackupExe", str(backup),
        "-TargetBackupExe", str(target.with_name(target.name + ".old")),
    ]
    helper_env = os.environ.copy()
    # PowerShell 更新助手会比当前 onefile 进程活得更久，并继续启动替换后的
    # EXE。让整条子进程链继承重置标记，避免新版复用即将被删除的旧 _MEI。
    helper_env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    try:
        subprocess.Popen(
            command,
            cwd=str(current.parent),
            env=helper_env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        local_staged.unlink(missing_ok=True)
        raise


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _safe_rmtree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def cleanup_stale_update_artifacts() -> None:
    """尽力清理历史更新遗留的 ``*.exe.old``、staged 文件、失败日志和过时的下载，单个失败不影响启动。

    能正常启动到这里就说明这些都已是残留，不依赖持久化的更新状态。
    比当前版本新的下载目录整个保留：下完的直接复用，半截的 ``.part`` 下次续传。
    """
    if not getattr(sys, "frozen", False):
        return
    try:
        install_dir = Path(sys.executable).resolve().parent
        for stale in install_dir.glob("*.exe.old"):
            _safe_unlink(stale)
        for stale in install_dir.glob(_HIDDEN_STAGED_GLOB):
            _safe_unlink(stale)
        _safe_unlink(install_dir / _UPDATE_LOG_NAME)
    except OSError:
        pass
    try:
        updates_dir = data_dir("updates")
        if updates_dir.is_dir():
            for entry in updates_dir.iterdir():
                if entry.name == _HELPER_SCRIPT_NAME:
                    continue
                if entry.is_dir() and is_newer_version(__version__, entry.name):
                    continue
                if entry.is_dir():
                    _safe_rmtree(entry)
                else:
                    _safe_unlink(entry)
    except OSError:
        pass


def clear_download_mark() -> None:
    """删除当前 EXE 的"来自其他计算机"下载标记（NTFS 备用数据流 ``Zone.Identifier``）。

    带此标记时资源管理器双击可能被系统静默拦截；能运行到这里说明用户已决定信任该文件，
    清掉后以后双击就不再被拦。没有标记或删除失败都静默忽略。
    """
    if not getattr(sys, "frozen", False) or sys.platform != "win32":
        return
    _safe_unlink(Path(f"{sys.executable}:Zone.Identifier"))


def cleanup_vestigial_external_tools() -> None:
    """清理存量 ZIP 版更新到内嵌版后不再被读取的外置 tools/ 目录。

    只在"当前 EXE 自带内嵌 tools 且同级还有外置 tools/"时删除（此时外置目录不会被任何代码读取）。
    """
    if not getattr(sys, "frozen", False):
        return
    bundled_tools = Path(getattr(sys, "_MEIPASS", "")) / "tools"
    if not bundled_tools.is_dir():
        return
    try:
        external_tools = Path(sys.executable).resolve().parent / "tools"
        if external_tools.is_dir():
            _safe_rmtree(external_tools)
    except OSError:
        pass
