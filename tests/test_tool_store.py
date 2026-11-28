"""按需下载工具：下载顺序与校验、网盘工具包导入、发布版从已下载副本运行。"""

from __future__ import annotations

import hashlib
import io
import lzma
import sys
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch

from dstools.shared import http_download, resource_paths, tool_store
from _harness import run  # noqa: E402


class _Response(io.BytesIO):
    status = 200
    headers: dict = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def _fake_tool(payload: bytes, *, official_url: str = "") -> tool_store.StoreTool:
    return tool_store.StoreTool("demo/demo.exe.xz", "demo.exe.xz", len(payload),
                                hashlib.sha256(payload).hexdigest(), official_url=official_url)


def test_download_tries_official_then_mirrors_and_verifies() -> None:
    payload = lzma.compress(b"demo-tool")
    tool = _fake_tool(payload, official_url="https://official/demo")
    requested = []

    def urlopen(request, **_kwargs):
        requested.append(request.full_url)
        if "official" in request.full_url:
            raise http_download.urllib.error.URLError("官网不通")
        return _Response(payload)

    with tempfile.TemporaryDirectory() as directory, \
            patch.dict(tool_store._BY_RELATIVE, {tool.relative: tool}, clear=True), \
            patch.object(tool_store, "data_dir", return_value=Path(directory)), \
            patch.object(http_download.urllib.request, "urlopen", side_effect=urlopen):
        path = tool_store.download_tool(tool.relative)
        assert path.read_bytes() == payload
        assert requested == ["https://official/demo",
                             f"https://gitee.com/orange-blade/DSTCamp/releases/download/"
                             f"{tool_store.TOOLS_RELEASE_TAG}/demo.exe.xz"]
        assert tool_store.find_stored(tool.relative) == path


def test_download_failure_falls_back_to_package_in_downloads() -> None:
    """网络下载失败时导入「下载」文件夹里当前版本的工具包（认浏览器加的 (1) 后缀，别的 zip 不碰）；没有就报原错误。"""
    payload = lzma.compress(b"demo-tool")
    tool = _fake_tool(payload)
    current = tool_store.TOOLS_PACKAGE_NAME[:-len(".zip")]
    with tempfile.TemporaryDirectory() as directory, \
            patch.dict(tool_store._BY_RELATIVE, {tool.relative: tool}, clear=True), \
            patch.dict(tool_store._BY_ASSET, {tool.asset: tool}, clear=True), \
            patch.object(tool_store, "data_dir", return_value=Path(directory) / "tools"), \
            patch.object(tool_store, "downloads_dir", return_value=Path(directory)), \
            patch.object(http_download.urllib.request, "urlopen",
                         side_effect=http_download.urllib.error.URLError("不通")):
        with zipfile.ZipFile(Path(directory) / "DSTCamp-tools-1.zip", "w") as archive:
            archive.writestr("demo.exe.xz", payload)
        try:
            tool_store.download_tool(tool.relative)
            raise AssertionError("旧版本的工具包不该被导入")
        except OSError:
            pass
        package = Path(directory) / f"{current} (1).zip"
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("demo.exe.xz", payload)
        imported = []
        path = tool_store.download_tool(tool.relative, on_imported=imported.append)
        assert path.read_bytes() == payload and imported == [package]


def test_tools_package_import_and_frozen_lookup() -> None:
    """工具包逐个校验后导入（可带一层目录，篡改的拒收）；发布版 EXE 里没有该工具时从导入的副本解压运行。"""
    payload = lzma.compress(b"demo-tool-binary")
    tool = _fake_tool(payload)
    with tempfile.TemporaryDirectory() as directory, \
            patch.dict(tool_store._BY_RELATIVE, {tool.relative: tool}, clear=True), \
            patch.dict(tool_store._BY_ASSET, {tool.asset: tool}, clear=True):
        root = Path(directory)
        good, bad = root / "good.zip", root / "bad.zip"
        with zipfile.ZipFile(good, "w") as archive:
            archive.writestr("DSTCamp-tools-1/demo.exe.xz", payload)
            archive.writestr("frp-LICENSE.txt", "license")
        with zipfile.ZipFile(bad, "w") as archive:
            archive.writestr("demo.exe.xz", payload[:-1] + b"x")
        with patch.object(tool_store, "data_dir", return_value=root / "data" / "tools"):
            assert tool_store.is_tools_package(good)
            assert tool_store.import_tools_package(bad) == ([], ["demo.exe.xz"])
            assert tool_store.find_stored(tool.relative) is None
            assert tool_store.import_tools_package(good) == (["demo.exe.xz"], [])

            bundle = root / "bundle"
            (bundle / "tools").mkdir(parents=True)
            with patch.object(sys, "frozen", True, create=True), \
                    patch.object(sys, "_MEIPASS", str(bundle), create=True), \
                    patch.object(resource_paths, "data_dir", side_effect=lambda name, **_k: root / "data" / name):
                runtime = resource_paths.runtime_tool_path("demo/demo.exe")
            assert runtime.read_bytes() == b"demo-tool-binary"
            assert runtime.parent.parent.parent == root / "data" / "runtime_tools"


def test_frozen_lookup_reuses_copy_unpacked_by_older_bundled_version() -> None:
    """工具从随包改为按需下载后，旧版已解压到 data/runtime_tools 的副本哈希对得上就沿用，被改过则当作未下载。"""
    binary = b"old-bundled-frpc"
    unpacked = hashlib.sha256(binary).hexdigest()
    tool = tool_store.StoreTool("demo/demo.exe.xz", "demo.exe.xz", 1, "0" * 64, unpacked_sha256=unpacked)
    with tempfile.TemporaryDirectory() as directory, \
            patch.dict(tool_store._BY_RELATIVE, {tool.relative: tool}, clear=True):
        root = Path(directory)
        previous = root / "data" / "runtime_tools" / unpacked[:16] / "demo" / "demo.exe"
        previous.parent.mkdir(parents=True)
        (root / "bundle" / "tools").mkdir(parents=True)
        with patch.object(sys, "frozen", True, create=True), \
                patch.object(sys, "_MEIPASS", str(root / "bundle"), create=True), \
                patch.object(tool_store, "data_dir", return_value=root / "data" / "tools"), \
                patch.object(resource_paths, "data_dir", side_effect=lambda name, **_k: root / "data" / name):
            previous.write_bytes(binary)
            assert resource_paths.runtime_tool_path("demo/demo.exe") == previous
            previous.write_bytes(b"tampered")
            assert not resource_paths.runtime_tool_path("demo/demo.exe").is_file()


if __name__ == "__main__":
    run(globals())
