"""按需下载的工具：不打进 EXE，首次用到时下载，或从网盘工具包导入，存到 data/tools/。

来源依次为官网（VC++ 运行库来自微软）、Gitee、GitHub 上固定标签的工具 Release（TOOLS_RELEASE_TAG）；
网盘兜底是同一批文件打成的工具包（scripts/make_tools_release.py 生成），在"从文件导入"里导入。
不论来源，一律按这里写死的大小和 SHA-256 校验，不信任服务器或网盘上的任何清单。

frp 系列以 .xz 原样存放、用时才由 runtime_tool_path() 解压：裸 exe 落盘会被杀软按 HackTool 隔离。
换工具版本时：改 tools/ 下的文件和这里的登记（有测试核对两者一致），升 TOOLS_RELEASE_TAG 并发布新的工具 Release。
"""

from __future__ import annotations

import os
import shutil
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path

from dstools.shared.http_download import ProgressCallback, download_verified, matches
from dstools.shared.resource_paths import data_dir

TOOLS_RELEASE_TAG = "tools-2"
TOOLS_PACKAGE_NAME = f"DSTCamp-{TOOLS_RELEASE_TAG}.zip"
# 网盘工具包的蓝奏云单文件分享（与 TOOLS_RELEASE_TAG 一一对应，换工具版本时一起换）
TOOLS_LANZOU_URL = "https://wwblt.lanzout.com/iJeFk4cozrra"
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
    StoreTool("frp_selfhost/frpc.exe.xz", "frpc.exe.xz", 4770108,
              "04829ce3d730afd87e24b522d4aee7b70903e32cc470f21146ada16d68987291",
              unpacked_sha256="1320325b3fd46d83ef7b2161d5e19f2b5dd9341b3391084a58d75ad82ef374d3"),
    StoreTool("frp_selfhost/frps_linux_amd64.xz", "frps_linux_amd64.xz", 5802848,
              "f7c8b4508c90a7a94ae42b03dd9cc7faadc0c05d365722311182404733d5e4f1"),
    StoreTool("frp_selfhost/frps_linux_arm64.xz", "frps_linux_arm64.xz", 4896496,
              "01b026e7a894f361aaa9dd8d5d6d4e77ea5941062619a8e56f0a2e477f866d64"),
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


def download_tool(relative: str, progress: ProgressCallback | None = None) -> Path:
    """下载登记过的工具到 data/tools/，已有校验通过的副本直接返回。失败抛 OSError。"""
    tool = _BY_RELATIVE[relative]
    target = _stored_path(tool)
    target.parent.mkdir(parents=True, exist_ok=True)
    urls = tuple(url.format(tag=TOOLS_RELEASE_TAG, asset=tool.asset) for url in _RELEASE_URLS)
    if tool.official_url:
        urls = (tool.official_url, *urls)
    with _lock:
        if not matches(target, tool.size, tool.sha256):
            download_verified(urls, target, tool.size, tool.sha256, progress)
    return target


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
