"""按需下载的工具：不打进 EXE，首次用到时下载，或从网盘工具包导入，存到 data/tools/。

来源依次为官网（VC++ 运行库来自微软）、Gitee、GitHub 上固定标签的工具 Release（TOOLS_RELEASE_TAG）；
网盘兜底是同一批文件打成的工具包（scripts/make_tools_release.py 生成），在"从文件导入"里导入。
不论来源，一律按这里写死的大小和 SHA-256 校验，不信任服务器或网盘上的任何清单。

frp 系列以 .xz 原样存放、用时才由 runtime_tool_path() 解压：裸 exe 落盘会被杀软按 HackTool 隔离。
换工具版本时：改 tools/ 下的文件和这里的登记（有测试核对两者一致），升 TOOLS_RELEASE_TAG 并发布新的工具 Release。
"""

from __future__ import annotations

import os
import re
import shutil
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from dstools.shared.http_download import ProgressCallback, download_verified, matches
from dstools.shared.resource_paths import data_dir

TOOLS_RELEASE_TAG = "tools-3"
TOOLS_PACKAGE_NAME = f"DSTCamp-{TOOLS_RELEASE_TAG}.zip"
# 网盘工具包所在的蓝奏云文件夹分享「依赖工具」（各版本工具包都放在里面，界面提示写明要下载 TOOLS_PACKAGE_NAME；
# 换工具版本时把新包传进这个文件夹即可，链接不变）
TOOLS_LANZOU_URL = "https://wwblt.lanzout.com/b01euv32cf"
TOOLS_LANZOU_PASSWORD = "6666"
_RELEASE_URLS = (
    "https://gitee.com/orange-blade/DSTCamp/releases/download/{tag}/{asset}",
    "https://github.com/chengzhirenchaoshuai/DSTCamp/releases/download/{tag}/{asset}",
)


@dataclass(frozen=True)
class StoreTool:
    relative: str  # tools/ 下的相对路径，与仓库和内嵌文件一致
    asset: str  # 工具 Release 附件与网盘工具包里的文件名
    size: int
    sha256: str
    official_url: str = ""  # 有官方下载地址时最先尝试
    unpacked_sha256: str = ""  # 旧版随包时解压出的副本哈希：升级后沿用，不必重新下载


TOOLS = (
    StoreTool("frp_selfhost/frpc.exe.xz", "frpc.exe.xz", 4965496,
              "0511bccba60b61098ca875458f89d7b033e650587efa7ab75f0a6b3072d9cd37"),
    StoreTool("frp_selfhost/frps_linux_amd64.xz", "frps_linux_amd64.xz", 5941080,
              "514507c6abcfed770ee802092c3052a3328ff4195361a73764d9cef215f163ce"),
    StoreTool("frp_selfhost/frps_linux_arm64.xz", "frps_linux_arm64.xz", 5017100,
              "5c43c9c75caf8f1dc36485f6bd378e55283af17814552109d921cf7c0841fa08"),
    StoreTool("frpc-sakura/sakura-frpc.exe.xz", "sakura-frpc.exe.xz", 4255628,
              "70c47f8afe6930aaafc886293dc3c9c45a48b0bb71eddbe93004c4694abf2b08"),
    StoreTool("vcredist/VC++ 2013 x86.exe", "vcredist_2013_x86.exe", 6510136,
              "53b605d1100ab0a88b867447bbf9274b5938125024ba01f5105a9e178a3dcdbd",
              official_url="https://aka.ms/highdpimfc2013x86enu"),
)
_BY_RELATIVE = {tool.relative: tool for tool in TOOLS}
_BY_ASSET = {tool.asset: tool for tool in TOOLS}
_lock = threading.Lock()


def store_tool(relative: str) -> StoreTool | None:
    return _BY_RELATIVE.get(relative)


def _stored_path(tool: StoreTool) -> Path:
    return data_dir("tools") / tool.asset


def find_stored(relative: str) -> Path | None:
    """已下载或导入、且校验通过的副本；没有返回 None。"""
    tool = _BY_RELATIVE.get(relative)
    if tool is None:
        return None
    path = _stored_path(tool)
    return path if matches(path, tool.size, tool.sha256) else None


def download_tool(relative: str, progress: ProgressCallback | None = None,
                  on_imported: Callable[[Path], None] | None = None) -> Path:
    """下载登记过的工具到 data/tools/，已有校验通过的副本直接返回。

    网络下载失败时，再看「下载」文件夹里有没有当前版本的网盘工具包（用户从蓝奏云下完常常不知道要导入），
    有就导入并回调 on_imported(工具包路径)；仍拿不到时抛出原来的 OSError。"""
    tool = _BY_RELATIVE[relative]
    target = _stored_path(tool)
    target.parent.mkdir(parents=True, exist_ok=True)
    urls = tuple(url.format(tag=TOOLS_RELEASE_TAG, asset=tool.asset) for url in _RELEASE_URLS)
    if tool.official_url:
        urls = (tool.official_url, *urls)
    try:
        with _lock:
            if not matches(target, tool.size, tool.sha256):
                download_verified(urls, target, tool.size, tool.sha256, progress)
    except OSError as download_error:
        package = find_downloaded_package()
        if package is None:
            raise
        try:
            import_tools_package(package)
        except (OSError, zipfile.BadZipFile):
            raise download_error from None  # 工具包坏了就当没有，报原来的下载错误
        stored = find_stored(relative)
        if stored is None:
            raise
        if on_imported is not None:
            on_imported(package)
        return stored
    return target


def downloads_dir() -> Path:
    """当前用户的「下载」文件夹：按系统登记的位置（用户可能移到了别的盘），取不到时用 主目录/Downloads。"""
    try:
        import ctypes
        from ctypes import wintypes

        class _GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        # FOLDERID_Downloads {374DE290-123F-4565-9164-39C4925E467B}
        folder_id = _GUID(0x374DE290, 0x123F, 0x4565,
                          (ctypes.c_ubyte * 8)(0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B))
        buffer = ctypes.c_wchar_p()
        shell32 = ctypes.windll.shell32
        if shell32.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None, ctypes.byref(buffer)) == 0:
            try:
                return Path(buffer.value)
            finally:
                ctypes.windll.ole32.CoTaskMemFree(buffer)
    except (AttributeError, OSError):
        pass
    return Path.home() / "Downloads"


def _is_package_name(name: str) -> bool:
    """当前版本工具包的文件名：DSTCamp-tools-N.zip，或浏览器重复下载时加的「 (1)」「(1)」后缀。"""
    stem = TOOLS_PACKAGE_NAME[:-len(".zip")]
    return re.fullmatch(re.escape(stem) + r"(?: ?\(\d+\))?\.zip", name, re.IGNORECASE) is not None


def find_downloaded_package() -> Path | None:
    """「下载」文件夹顶层里当前版本的工具包，有多个时取最新的；只按文件名精确认，不递归、不碰别的文件。"""
    try:
        candidates = [entry for entry in downloads_dir().iterdir()
                      if _is_package_name(entry.name) and entry.is_file()]
        if not candidates:
            return None
        return max(candidates, key=lambda entry: entry.stat().st_mtime)
    except OSError:
        return None


def is_tools_package(package: Path) -> bool:
    """zip 里有任何一个登记过的工具文件名，就当作工具包。"""
    try:
        with zipfile.ZipFile(package) as archive:
            return any(Path(name).name in _BY_ASSET for name in archive.namelist())
    except (OSError, zipfile.BadZipFile):
        return False


def import_tools_package(package: Path) -> tuple[list[str], list[str]]:
    """从网盘工具包导入，逐个按登记的大小和 SHA-256 校验。返回 (已导入, 校验失败) 的文件名。

    包里允许多一层目录；不认识的文件忽略。
    """
    imported: list[str] = []
    rejected: list[str] = []
    with zipfile.ZipFile(package) as archive, _lock:
        for info in archive.infolist():
            tool = _BY_ASSET.get(Path(info.filename).name)
            if tool is None or info.is_dir():
                continue
            target = _stored_path(tool)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".part")
            with archive.open(info) as source, temporary.open("wb") as output:
                shutil.copyfileobj(source, output, 1024 * 1024)
            if matches(temporary, tool.size, tool.sha256):
                os.replace(temporary, target)
                imported.append(tool.asset)
            else:
                temporary.unlink(missing_ok=True)
                rejected.append(tool.asset)
    return imported, rejected
