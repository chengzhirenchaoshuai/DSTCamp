"""自动更新元数据、下载和完整性校验测试。"""

from __future__ import annotations

import hashlib
import io
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from dstools import __version__
from dstools.shared import auto_update, http_download, update_check, update_signing
from dstools.shared.update_check import UpdatePatch, UpdateRelease
from scripts import build_exe
from _harness import run  # noqa: E402

TOOL_DIR = Path(__file__).resolve().parents[1] / "tools"

# 测试用密钥：本文件在独立子进程里跑，直接替换内置公钥，不依赖发布机上的私钥
_TEST_KEY = Ed25519PrivateKey.generate()
update_signing.PUBLIC_KEY_HEX = _TEST_KEY.public_key().public_bytes(
    serialization.Encoding.Raw, serialization.PublicFormat.Raw
).hex()


def _write_test_key(directory: Path) -> Path:
    path = directory / "key.pem"
    path.write_bytes(_TEST_KEY.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ))
    return path


class _Response(io.BytesIO):
    status = 200
    headers: dict = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def test_release_manifest_enables_auto_update() -> None:
    manifest = {
        "version": "1.4.0",
        "files": {
            "DSTCamp-1.4.0.exe": {"size": 4, "sha256": "a" * 64},
        },
        "patches": {
            __version__: {"name": "p.hdiff", "size": 2, "sha256": "b" * 64, "from_sha256": "c" * 64},
            "0.0.1": {"name": "old.hdiff", "size": 2, "sha256": "d" * 64, "from_sha256": "e" * 64},
        },
    }
    release = {
        "tag_name": "v1.4.0",
        "html_url": "https://example/release",
        "assets": [
            {"name": "DSTCamp-1.4.0.exe", "browser_download_url": "https://example/exe"},
            {"name": "DSTCamp-1.4.0.sha256.json", "browser_download_url": "https://example/manifest"},
            {"name": "p.hdiff", "browser_download_url": "https://example/patch"},
        ],
    }
    # 没签名的清单不能自动更新
    with patch.object(update_check, "_request_json", return_value=manifest):
        assert not update_check._parse_release(release, "gitee").can_auto_update
    with tempfile.TemporaryDirectory() as directory:
        manifest = update_signing.sign_manifest(manifest, _write_test_key(Path(directory)))
    with patch.object(update_check, "_request_json", return_value=manifest):
        parsed = update_check._parse_release(release, "gitee")
    assert parsed is not None and parsed.can_auto_update
    assert parsed.sha256 == "a" * 64 and parsed.size == 4
    # 只取以当前运行版本为起点的补丁
    assert parsed.patch == UpdatePatch("https://example/patch", 2, "b" * 64, "c" * 64)
    # 发行版列表里按版本号挑最新正式版：工具发行版、预发布不算
    listing = [{"tag_name": "v1.3.0"}, release, {"tag_name": "tools-1"}, {"tag_name": "v9.0.0", "prerelease": True}]
    assert update_check._newest_version_release(listing) is release


def test_download_requires_matching_hash_and_size() -> None:
    payload = b"verified-update"
    release = UpdateRelease(
        "1.4.0",
        "https://example/release",
        "gitee",
        "https://example/exe",
        hashlib.sha256(payload).hexdigest(),
        len(payload),
    )
    with tempfile.TemporaryDirectory() as directory, patch.object(
        auto_update, "data_dir", return_value=Path(directory)
    ), patch.object(
        http_download.urllib.request,
        "urlopen",
        side_effect=lambda *_args, **_kwargs: _Response(payload),
    ):
        path = auto_update.download_update(release)
        assert path.read_bytes() == payload
        # 已下载且校验一致时再次更新直接复用，不再联网
        with patch.object(http_download.urllib.request, "urlopen", side_effect=AssertionError("不应重新下载")):
            assert auto_update.download_update(release) == path

        bad = UpdateRelease(
            release.version,
            release.page_url,
            release.source,
            release.exe_url,
            "0" * 64,
            release.size,
        )
        try:
            auto_update.download_update(bad)
        except OSError as exc:
            assert "SHA-256" in str(exc)
        else:
            raise AssertionError("错误哈希不应通过校验")


def test_download_resumes_and_falls_back_to_mirror() -> None:
    """主源断线时换备用源并按 Range 续传；服务器忽略 Range（如 Gitee 附件返回 200）时从头写。"""
    payload = bytes(range(256)) * 40
    release = UpdateRelease(
        "1.4.0", "https://example/release", "gitee", "https://primary/exe",
        hashlib.sha256(payload).hexdigest(), len(payload),
        fallback_urls=("https://mirror/exe",),
    )
    requested = []

    def urlopen(request, **_kwargs):
        requested.append((request.full_url, request.get_header("Range")))
        if request.full_url == "https://primary/exe":
            raise http_download.urllib.error.URLError("断线")
        start = int(request.get_header("Range")[len("bytes="):-1])
        response = _Response(payload[start:])
        response.status = 206
        response.headers = {"Content-Range": f"bytes {start}-{len(payload) - 1}/{len(payload)}"}
        return response

    with tempfile.TemporaryDirectory() as directory, patch.object(
        auto_update, "data_dir", return_value=Path(directory)
    ):
        part = Path(directory) / "1.4.0" / "DSTCamp-1.4.0.exe.part"
        part.parent.mkdir(parents=True)
        part.write_bytes(payload[:1000])
        with patch.object(http_download.urllib.request, "urlopen", side_effect=urlopen):
            path = auto_update.download_update(release)
        assert path.read_bytes() == payload and not part.exists()
        assert requested == [("https://primary/exe", "bytes=1000-"), ("https://mirror/exe", "bytes=1000-")]

        path.unlink()
        part.write_bytes(b"x" * 1000)
        with patch.object(
            http_download.urllib.request, "urlopen", side_effect=lambda *_a, **_k: _Response(payload)
        ):
            assert auto_update.download_update(release).read_bytes() == payload


def test_patch_update_builds_exe_locally() -> None:
    """本机 EXE 与补丁起点一致时只下补丁并用真实 hpatchz 合成；不一致时不下补丁，直接下整包。"""
    tool_dir = Path(__file__).resolve().parents[1] / "tools"
    old = bytes(range(256)) * 400
    new = old[:50000] + b"new-version-code" + old[50000:]
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        old_exe, new_exe, patch_file = root / "old.exe", root / "new.exe", root / "p.hdiff"
        old_exe.write_bytes(old)
        new_exe.write_bytes(new)
        subprocess.run([str(tool_dir / "hdiffpatch" / "hdiffz.exe"), "-f", str(old_exe), str(new_exe),
                        str(patch_file)], check=True, capture_output=True)
        patch_bytes = patch_file.read_bytes()
        update_patch = UpdatePatch("https://example/patch", len(patch_bytes),
                                   hashlib.sha256(patch_bytes).hexdigest(), hashlib.sha256(old).hexdigest())
        release = UpdateRelease("999.0.0", "https://example/release", "gitee", "https://example/exe",
                                hashlib.sha256(new).hexdigest(), len(new), patch=update_patch)
        requested = []

        def urlopen(request, **_kwargs):
            requested.append(request.full_url)
            return _Response(patch_bytes if request.full_url == update_patch.url else new)

        updates = root / "updates"
        with (
            patch.object(auto_update.sys, "frozen", True, create=True),
            patch.object(auto_update.sys, "executable", str(old_exe)),
            patch.object(auto_update, "data_dir", return_value=updates),
            patch.object(auto_update, "tool_binary_dir", return_value=tool_dir),
            patch.object(http_download.urllib.request, "urlopen", side_effect=urlopen),
        ):
            assert auto_update.download_update(release).read_bytes() == new
            assert requested == [update_patch.url]

            requested.clear()
            (updates / "999.0.0" / "DSTCamp-999.0.0.exe").unlink()
            old_exe.write_bytes(b"user-built-exe")
            assert auto_update.download_update(release).read_bytes() == new
            assert requested == [release.exe_url]


def test_offline_package_import() -> None:
    """构建脚本出的离线包能导入：完整包直接用，补丁包对当前 EXE 合成；签名不对、版本不新、起点不符都拒绝。"""
    old = bytes(range(256)) * 400
    new = old[:50000] + b"new-version-code" + old[50000:]
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        key = _write_test_key(root)
        old_exe = root / "DSTCamp-current.exe"
        old_exe.write_bytes(old)
        dist = root / "dist"
        dist.mkdir()
        new_exe = dist / "DSTCamp-999.0.0.exe"
        new_exe.write_bytes(new)
        patch_file = dist / f"DSTCamp-{__version__}-to-999.0.0.hdiff"
        subprocess.run([str(TOOL_DIR / "hdiffpatch" / "hdiffz.exe"), "-f", str(old_exe), str(new_exe),
                        str(patch_file)], check=True, capture_output=True)
        patches = {__version__: {"name": patch_file.name, **build_exe._file_info(patch_file),
                                 "from_sha256": build_exe._file_info(old_exe)["sha256"]}}
        manifest_path = dist / "DSTCamp-999.0.0.sha256.json"
        build_exe._write_sha256_manifest(manifest_path, "999.0.0", (new_exe,), patches, key)
        full_zip, patch_zip = build_exe._write_offline_packages(dist, manifest_path, new_exe, [patch_file])

        updates = root / "updates"
        with (
            patch.object(auto_update.sys, "frozen", True, create=True),
            patch.object(auto_update.sys, "executable", str(old_exe)),
            patch.object(auto_update, "data_dir", return_value=updates),
            patch.object(auto_update, "tool_binary_dir", return_value=TOOL_DIR),
        ):
            assert auto_update.is_update_package(patch_zip)
            version, path = auto_update.import_update_package(patch_zip)
            assert version == "999.0.0" and path.read_bytes() == new
            assert sorted(item.name for item in path.parent.iterdir()) == [path.name]
            path.unlink()
            assert auto_update.import_update_package(full_zip)[1].read_bytes() == new

            def expect_error(package: Path, text: str) -> None:
                path.unlink(missing_ok=True)
                try:
                    auto_update.import_update_package(package)
                except auto_update.UpdatePackageError as exc:
                    assert text in str(exc), exc
                else:
                    raise AssertionError(f"应拒绝：{package.name}")

            # 本机 EXE 不是补丁起点（如自己构建的版本）：补丁包不可用，完整包照常可用
            old_exe.write_bytes(b"user-built-exe")
            expect_error(patch_zip, "完整包")
            # 清单被改过：签名不符
            tampered = root / "tampered.zip"
            with zipfile.ZipFile(full_zip) as source, zipfile.ZipFile(tampered, "w") as target:
                for name in source.namelist():
                    data = source.read(name)
                    target.writestr(name, data.replace(b"999.0.0", b"999.0.1") if name.endswith(".json") else data)
            expect_error(tampered, "签名")
            # 版本不比当前新
            with patch.object(auto_update, "__version__", "1000.0.0"):
                expect_error(full_zip, "不比当前版本")


def test_launch_helper_stages_on_exe_volume() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        current = root / "DSTCamp-1.3.5.exe"
        staged = root / "cache" / "DSTCamp-1.3.6.exe"
        staged.parent.mkdir()
        current.write_bytes(b"old")
        staged.write_bytes(b"new")
        with patch.object(auto_update.sys, "frozen", True, create=True), patch.object(
            auto_update.sys, "executable", str(current)
        ), patch.object(auto_update, "data_dir", return_value=root / "data"), patch.object(
            auto_update.subprocess, "Popen"
        ) as popen:
            auto_update.launch_update_helper(staged)
        command = popen.call_args.args[0]
        local_staged = Path(command[command.index("-NewExe") + 1])
        target = Path(command[command.index("-TargetExe") + 1])
        assert local_staged.parent.resolve() == current.parent.resolve()
        assert local_staged.read_bytes() == b"new"
        assert target.resolve() == (root / auto_update.STANDARD_EXE_NAME).resolve()
        assert (
            popen.call_args.kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
        )

        helper_script = Path(command[command.index("-File") + 1])
        helper_content = helper_script.read_text(encoding="utf-8-sig")
        reset_at = helper_content.index("PYINSTALLER_RESET_ENVIRONMENT")
        start_at = helper_content.index("Start-Process -FilePath $TargetExe")
        assert reset_at < start_at


def test_helper_migrates_over_existing_standard_exe() -> None:
    # 同目录已有旧的 DSTCamp.exe，又从带版本号的 EXE 更新：旧的固定名字备份成 .old 后被覆盖。
    # 真实跑 PowerShell 助手；Start-Process 需要合法 EXE，用系统小程序冒充各版本。
    system32 = Path(r"C:\Windows\System32")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        target = root / auto_update.STANDARD_EXE_NAME
        current = root / "DSTCamp-1.6.3.exe"
        staged = root / ".DSTCamp-1.6.3.update-1.exe"
        shutil.copy(system32 / "where.exe", target)
        shutil.copy(system32 / "hostname.exe", current)
        shutil.copy(system32 / "whoami.exe", staged)
        expected = {path: path.read_bytes() for path in (target, current, staged)}
        finished = subprocess.Popen(["cmd", "/c", "exit"], creationflags=subprocess.CREATE_NO_WINDOW)
        finished.wait()
        with patch.object(auto_update, "data_dir", return_value=root / "data"):
            script = auto_update._helper_script()
        subprocess.run([
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
            "-ParentPid", str(finished.pid), "-CurrentExe", str(current), "-NewExe", str(staged),
            "-TargetExe", str(target), "-BackupExe", str(root / "DSTCamp-1.6.3.exe.old"),
            "-TargetBackupExe", str(root / "DSTCamp.exe.old"),
        ], check=True, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
        assert not (root / "apply_update.log").exists()
        assert target.read_bytes() == expected[staged]
        assert (root / "DSTCamp.exe.old").read_bytes() == expected[target]
        assert (root / "DSTCamp-1.6.3.exe.old").read_bytes() == expected[current]
        time.sleep(1)  # 等拉起的进程退出再删临时目录


def test_helper_retries_while_old_exe_is_still_in_use() -> None:
    """旧 EXE 退出前后还被占用（onefile 启动器清理临时目录）时，助手重试移动，而不是直接失败。"""
    import threading

    system32 = Path(r"C:\Windows\System32")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        current = root / auto_update.STANDARD_EXE_NAME
        staged = root / ".DSTCamp.update-1.exe"
        shutil.copy(system32 / "hostname.exe", current)
        shutil.copy(system32 / "whoami.exe", staged)
        expected = staged.read_bytes()
        holder = current.open("rb")  # Python 打开的句柄不允许重命名，模拟占用
        threading.Timer(1.5, holder.close).start()
        finished = subprocess.Popen(["cmd", "/c", "exit"], creationflags=subprocess.CREATE_NO_WINDOW)
        finished.wait()
        with patch.object(auto_update, "data_dir", return_value=root / "data"):
            script = auto_update._helper_script()
        subprocess.run([
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
            "-ParentPid", str(finished.pid), "-CurrentExe", str(current), "-NewExe", str(staged),
            "-TargetExe", str(current), "-BackupExe", str(root / "DSTCamp.exe.old"),
            "-TargetBackupExe", str(root / "DSTCamp.exe.old"),
        ], check=True, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
        assert not (root / "apply_update.log").exists()
        assert current.read_bytes() == expected and not staged.exists()
        time.sleep(1)  # 等拉起的进程退出再删临时目录


def test_standard_exe_name_is_fixed_but_custom_name_is_preserved() -> None:
    root = Path("C:/DSTCamp")
    # 旧版本号命名一次性迁移到固定名字。
    assert auto_update.resolve_install_target(root / "DSTCamp-1.3.5.exe") == root / auto_update.STANDARD_EXE_NAME
    # 已经是固定名字的安装，后续更新继续保持固定名字。
    assert auto_update.resolve_install_target(root / auto_update.STANDARD_EXE_NAME) == root / auto_update.STANDARD_EXE_NAME
    # 用户自定义命名不受影响。
    assert auto_update.resolve_install_target(root / "我的开服工具.exe") == root / "我的开服工具.exe"


def test_cleanup_removes_known_stale_artifacts() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        install_dir = root / "install"
        install_dir.mkdir()
        current = install_dir / "DSTCamp.exe"
        current.write_bytes(b"current")
        # 上一次成功更新留下的旧版本备份。
        (install_dir / "DSTCamp-1.3.7.exe.old").write_bytes(b"old")
        # 上一次失败更新留下的隐藏 staged 文件——用跟"当前文件名"不同的
        # stem 生成，模拟用户在两次更新之间改过名字的场景。
        (install_dir / ".我的旧名字.update-4321.exe").write_bytes(b"stale")
        (install_dir / "apply_update.log").write_text("失败原因", encoding="utf-8")
        keep_file = install_dir / "readme.txt"
        keep_file.write_text("keep", encoding="utf-8")

        updates_dir = root / "data" / "updates"
        (updates_dir / "1.3.6").mkdir(parents=True)
        (updates_dir / "1.3.6" / "DSTCamp-1.3.6.exe").write_bytes(b"stale-download")
        updates_dir.joinpath("apply_update.ps1").write_text("script", encoding="utf-8")
        # 比当前版本新的下载留给下次复用，半截文件留着续传
        newer = updates_dir / "999.0.0"
        newer.mkdir()
        (newer / "DSTCamp-999.0.0.exe").write_bytes(b"ready")
        (newer / "DSTCamp-999.0.0.exe.part").write_bytes(b"partial")

        with patch.object(auto_update.sys, "frozen", True, create=True), patch.object(
            auto_update.sys, "executable", str(current)
        ), patch.object(auto_update, "data_dir", return_value=updates_dir):
            auto_update.cleanup_stale_update_artifacts()

        assert not (install_dir / "DSTCamp-1.3.7.exe.old").exists()
        assert not (install_dir / ".我的旧名字.update-4321.exe").exists()
        assert not (install_dir / "apply_update.log").exists()
        assert keep_file.exists()
        assert current.exists()
        assert not (updates_dir / "1.3.6").exists()
        assert updates_dir.joinpath("apply_update.ps1").exists()
        assert (newer / "DSTCamp-999.0.0.exe").exists()
        assert (newer / "DSTCamp-999.0.0.exe.part").exists()


def test_cleanup_is_noop_when_not_frozen() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        install_dir = root / "install"
        install_dir.mkdir()
        stale = install_dir / "DSTCamp.exe.old"
        stale.write_bytes(b"old")
        with patch.object(auto_update.sys, "frozen", False, create=True), patch.object(
            auto_update.sys, "executable", str(install_dir / "DSTCamp.exe")
        ):
            auto_update.cleanup_stale_update_artifacts()
        assert stale.exists()


def test_cleanup_removes_vestigial_external_tools_once_embedded() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        install_dir = root / "install"
        install_dir.mkdir()
        (install_dir / "tools").mkdir()
        (install_dir / "tools" / "leftover.txt").write_text("dead", encoding="utf-8")
        current = install_dir / "DSTCamp.exe"
        current.write_bytes(b"current")

        meipass = root / "meipass"
        (meipass / "tools").mkdir(parents=True)

        with patch.object(auto_update.sys, "frozen", True, create=True), patch.object(
            auto_update.sys, "executable", str(current)
        ), patch.object(auto_update.sys, "_MEIPASS", str(meipass), create=True):
            auto_update.cleanup_vestigial_external_tools()

        assert not (install_dir / "tools").exists()


def test_cleanup_keeps_external_tools_when_not_yet_embedded() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        install_dir = root / "install"
        install_dir.mkdir()
        (install_dir / "tools").mkdir()
        needed = install_dir / "tools" / "still-needed.txt"
        needed.write_text("in use", encoding="utf-8")
        current = install_dir / "DSTCamp-1.3.5.exe"
        current.write_bytes(b"current")

        meipass = root / "meipass"
        meipass.mkdir()  # 没有内嵌 tools 子目录——这份 EXE 仍需要外置 tools

        with patch.object(auto_update.sys, "frozen", True, create=True), patch.object(
            auto_update.sys, "executable", str(current)
        ), patch.object(auto_update.sys, "_MEIPASS", str(meipass), create=True):
            auto_update.cleanup_vestigial_external_tools()

        assert needed.exists()


if __name__ == "__main__":
    run(globals())
