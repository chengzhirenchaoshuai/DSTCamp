"""构建 DSTCamp 单文件 EXE（内嵌 tools/ 与 icons/），并执行产物冒烟测试。

用 ``--patch-from 旧版EXE ...`` 为这些正式发布过的旧版各生成一个增量补丁（HDiffPatch），
一并写进 sha256 清单，旧版客户端自动更新时只需下载补丁。

清单用项目外密钥目录（见 _project_key_dir()）里的 update_signing_key.pem 签名（客户端只认带有效签名的清单）；开发自测可用 ``--unsigned``。
release/offline/ 下另出供蓝奏云等网盘兜底的离线包：完整包（EXE + 清单）与补丁包（各补丁 + 清单），
用户下载后在"关于 → 手动更新 → 从文件导入"安装。它们不上传到 GitHub/Gitee。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

TOOL_FILES = (
    "fonts/FusionPixelFont_LICENSES/ark-pixel/OFL.txt",
    "fonts/FusionPixelFont_LICENSES/cubic-11/OFL.txt",
    "fonts/FusionPixelFont_LICENSES/galmuri/LICENSE.txt",
    "fonts/FusionPixelFont_OFL.txt",
    "fonts/KNMaiyuan-Regular.ttf",
    "fonts/KNMaiyuan_AUTHORS.txt",
    "fonts/KNMaiyuan_OFL.txt",
    "fonts/fusion-pixel-12px-proportional-zh_hans.ttf",
    "frp_selfhost/LICENSE",
    "hdiffpatch/LICENSE",
    "hdiffpatch/hpatchz.exe",  # 客户端合成增量补丁
    "ktools/CORE_RL_Magick++_.dll",
    "ktools/CORE_RL_bzlib_.dll",
    "ktools/CORE_RL_glib_.dll",
    "ktools/CORE_RL_lcms_.dll",
    "ktools/CORE_RL_lqr_.dll",
    "ktools/CORE_RL_magick_.dll",
    "ktools/CORE_RL_png_.dll",
    "ktools/CORE_RL_ttf_.dll",
    "ktools/CORE_RL_wand_.dll",
    "ktools/CORE_RL_zlib_.dll",
    "ktools/IM_MOD_RL_png_.dll",
    "ktools/IM_MOD_RL_rgb_.dll",
    "ktools/IM_MOD_RL_xc_.dll",
    "ktools/coder.xml",
    "ktools/colors.xml",
    "ktools/ktech.exe",
    "steamcmd/LICENSE.txt",
    "steamcmd/steamcmd.exe.gz",
)

# tools/ 里只在构建时用、不打进 EXE 的工具
BUILD_ONLY_TOOL_FILES = (
    "hdiffpatch/hdiffz.exe",  # 生成增量补丁
)

# 不打进 EXE、首次用到时下载的工具（与 dstools/shared/tool_store.py 的登记一致，有测试核对），
# 由 scripts/make_tools_release.py 发布到工具 Release 并打成网盘工具包
ON_DEMAND_TOOL_FILES = (
    "frp_selfhost/frpc.exe.xz",
    "frp_selfhost/frps_linux_amd64.xz",
    "frp_selfhost/frps_linux_arm64.xz",
    "frpc-sakura/sakura-frpc.exe.xz",
    "vcredist/VC++ 2013 x86.exe",
)

ICON_PATTERNS = {
    "app": ("*.png", "*.ico"),
    "ui": ("*.png",),
    "world": ("*.png",),
    "recommended": ("*.png",),
    "avatars": ("*.png",),
    "ornaments": ("*.png",),
}

REQUIRED_ICON_FILES = (
    "app/icon.ico",
    "app/icon.png",
    "ui/character_icon_default.png",
    "ui/client_mods_disabled.png",  # Mod 列表"禁用客户端模组"行
    "ui/combo_arrow.png",  # Qt 版下拉框箭头（QSS 引用）
    "ui/mod_icon_default.png",
)


def _stage_tools(project_root: Path, cache_root: Path) -> Path:
    """按白名单复制发布工具，避免未跟踪文件混入产物。"""
    source_root = project_root / "tools"
    target_root = cache_root / "bundled_tools"
    shutil.rmtree(target_root, ignore_errors=True)
    for relative in TOOL_FILES:
        source = source_root / relative
        if not source.is_file():
            raise FileNotFoundError(f"缺少发布工具：{source}")
        target = target_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return target_root


def _stage_icons(project_root: Path, cache_root: Path) -> Path:
    """只复制运行时图标，排除说明文件和其它开发资料。"""
    source_root = project_root / "icons"
    target_root = cache_root / "bundled_icons"
    shutil.rmtree(target_root, ignore_errors=True)
    for relative in REQUIRED_ICON_FILES:
        source = source_root / relative
        if not source.is_file():
            raise FileNotFoundError(f"缺少必要发布图标：{source}")
    for folder, globs in ICON_PATTERNS.items():
        copied = 0
        for pattern in globs:
            for source in (source_root / folder).glob(pattern):
                target = target_root / folder / source.name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                copied += 1
        if not copied:
            raise FileNotFoundError(f"发布图标目录为空：{source_root / folder}")
    return target_root


def _file_info(path: Path) -> dict:
    return {"size": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _make_patch(project_root: Path, old_exe: Path, new_exe: Path, version: str) -> tuple[str, dict]:
    """生成从旧版到新版的补丁，并实际合成一遍校验结果与新版逐字节一致。返回 (旧版本号, 清单条目)。"""
    match = re.fullmatch(r"DSTCamp-(\d+(?:\.\d+)+)\.exe", old_exe.name)
    if not match:
        raise SystemExit(f"--patch-from 只接受正式发布的 DSTCamp-版本号.exe：{old_exe}")
    old_version = match.group(1)
    tool_dir = project_root / "tools" / "hdiffpatch"
    patch = new_exe.with_name(f"DSTCamp-{old_version}-to-{version}.hdiff")
    subprocess.run(
        [str(tool_dir / "hdiffz.exe"), "-m-6", "-SD", "-c-lzma2-9-16m", "-f",
         str(old_exe), str(new_exe), str(patch)],
        check=True, capture_output=True,
    )
    rebuilt = patch.with_suffix(".check.exe")
    try:
        subprocess.run([str(tool_dir / "hpatchz.exe"), "-f", str(old_exe), str(patch), str(rebuilt)],
                       check=True, capture_output=True)
        if _file_info(rebuilt) != _file_info(new_exe):
            raise RuntimeError(f"补丁合成结果与新版不一致：{patch.name}")
    finally:
        rebuilt.unlink(missing_ok=True)
    entry = {"name": patch.name, **_file_info(patch), "from_sha256": _file_info(old_exe)["sha256"]}
    return old_version, entry


def _write_sha256_manifest(
    manifest_path: Path, version: str, artifacts: tuple[Path, ...], patches: dict[str, dict] | None = None,
    signing_key: Path | None = None,
) -> None:
    """为自动更新写入可复核的文件大小与 SHA-256 清单；patches 按旧版本号列出可用补丁，给了私钥就签名。"""
    manifest = {"version": version, "files": {}}
    for artifact in artifacts:
        manifest["files"][artifact.name] = _file_info(artifact)
    if patches:
        manifest["patches"] = patches
    if signing_key is not None:
        from dstools.shared.update_signing import sign_manifest

        manifest = sign_manifest(manifest, signing_key)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _write_offline_packages(dist_root: Path, manifest_path: Path, exe: Path, patch_files: list[Path]) -> list[Path]:
    """生成网盘兜底用的离线包：完整包，有补丁时再出一个补丁包；每个包都带签名清单。"""
    offline_dir = dist_root / "offline"
    offline_dir.mkdir(exist_ok=True)
    packages = [(offline_dir / f"{exe.stem}.zip", [exe])]
    if patch_files:
        packages.append((offline_dir / f"{exe.stem}-patches.zip", patch_files))
    for package, members in packages:
        # EXE 与补丁本身已压缩，不再压一遍
        with zipfile.ZipFile(package, "w", zipfile.ZIP_STORED) as archive:
            archive.write(manifest_path, manifest_path.name)
            for member in members:
                archive.write(member, member.name)
    return [package for package, _members in packages]


def _run_smoke_test(executable: Path) -> None:
    """实际启动冻结程序，验证入口、模块和资源可用。"""
    result = subprocess.run(
        [str(executable), "--smoke-test"],
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    if result.returncode:
        detail = (result.stdout + result.stderr).strip()
        raise RuntimeError(f"产物冒烟测试失败：{executable}\n{detail}")


# 界面只用 QtCore/QtGui/QtWidgets。下面这些 Python 模块不打包：
_EXCLUDED_MODULES = (
    "numpy",
    "tkinter",
    # Pillow 的 AVIF 编解码（4MB+），项目只处理 PNG/JPG/TEX 转出的图片
    "PIL._avif", "PIL.AvifImagePlugin",
    *(f"PySide6.{name}" for name in (
        "QtNetwork", "QtWebEngineCore", "QtWebEngineWidgets", "QtWebEngineQuick", "QtWebChannel",
        "QtWebSockets", "QtQml", "QtQuick", "QtQuickWidgets", "QtQuick3D", "Qt3DCore", "Qt3DRender",
        "QtMultimedia", "QtMultimediaWidgets", "QtPdf", "QtPdfWidgets", "QtCharts", "QtDataVisualization",
        "QtGraphs", "QtBluetooth", "QtPositioning", "QtLocation", "QtSensors", "QtSerialPort", "QtSql",
        "QtTest", "QtDesigner", "QtHelp", "QtOpenGL", "QtOpenGLWidgets", "QtSvg", "QtSvgWidgets",
        "QtTextToSpeech",
    )),
)

# PySide6 钩子会按插件顺带收集 Qt 动态库和插件；命令行排除模块管不到这些文件，
# 在 spec 里按打包后的路径过滤（路径统一用小写、正斜杠比较）。
_DROPPED_QT_FILES = (
    "pyside6/opengl32sw.dll",                     # 软件渲染 OpenGL（7MB+），界面不用 OpenGL
    "pyside6/qt6quick", "pyside6/qt6qml", "pyside6/qt6pdf", "pyside6/qt6network",
    "pyside6/qt6opengl", "pyside6/qt6svg", "pyside6/qt6virtualkeyboard",
    "pyside6/plugins/platforms/qdirect2d", "pyside6/plugins/platforms/qminimal",
    "pyside6/plugins/platforms/qoffscreen",       # 平台插件只需要 qwindows
    "pyside6/plugins/tls/", "pyside6/plugins/networkinformation/", "pyside6/plugins/generic/",
    "pyside6/plugins/platforminputcontexts/",     # 虚拟键盘，输入法走系统原生
    "pyside6/plugins/iconengines/",               # SVG 图标引擎，界面图标都是 PNG
    # 图片解码插件：背景图只允许 png/jpg/jpeg/bmp/gif（png/bmp 内置），保留 jpeg/gif/ico
    "pyside6/plugins/imageformats/qpdf", "pyside6/plugins/imageformats/qsvg",
    "pyside6/plugins/imageformats/qwebp", "pyside6/plugins/imageformats/qtiff",
    "pyside6/plugins/imageformats/qtga", "pyside6/plugins/imageformats/qwbmp",
    "pyside6/plugins/imageformats/qicns",
)
# Qt 自带的界面翻译只保留简体中文（英文是 Qt 内置原文，不需要翻译文件）
_KEPT_QT_TRANSLATIONS = ("qt_zh_cn.qm", "qtbase_zh_cn.qm")


def _drop_bundled_file(dest_name: str) -> bool:
    """spec 里 Analysis 结果的过滤规则：返回 True 表示这个文件不打进 EXE。"""
    name = dest_name.replace("\\", "/").lower()
    if name.startswith("pyside6/translations/"):
        return name.rsplit("/", 1)[-1] not in _KEPT_QT_TRANSLATIONS
    return any(name.startswith(prefix) for prefix in _DROPPED_QT_FILES)


def _spec_source(script: Path, project_root: Path, exe_name: str, icon: Path,
                 datas: list[tuple[Path, str]]) -> str:
    """生成 PyInstaller spec：单文件、无控制台，Analysis 之后按 _drop_bundled_file 裁剪。"""
    return f"""# 由 scripts/build_exe.py 自动生成，不要手改
import sys
sys.path.insert(0, {str(project_root / "scripts")!r})
from build_exe import _drop_bundled_file, _EXCLUDED_MODULES
from PyInstaller.utils.hooks import collect_data_files

a = Analysis(
    [{str(script)!r}],
    pathex=[{str(project_root)!r}],
    datas={[(str(src), dest) for src, dest in datas]!r} + collect_data_files("certifi"),
    hiddenimports=["lupa.lua51"],
    excludes=list(_EXCLUDED_MODULES),
    noarchive=False,
)
a.binaries = [entry for entry in a.binaries if not _drop_bundled_file(entry[0])]
a.datas = [entry for entry in a.datas if not _drop_bundled_file(entry[0])]
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name={exe_name!r},
    icon={str(icon)!r},
    console=False,
    upx=False,
)
"""


def _check_world_settings_audit(project_root: Path) -> None:
    """正式发布前核对世界设置登记表是否跟上了游戏与已适配 Mod 的更新；有需要处理的地方就中止构建。
    核对脚本在 reference/（不进 Git），本机没有时只提示不拦截。"""
    script = project_root / "reference" / "dev_scripts" / "audit_world_settings.py"
    if not script.is_file():
        print(f"提示：没有找到世界设置核对脚本 {script}，跳过发布前核对")
        return
    print("发布前核对世界设置适配……")
    if subprocess.run([sys.executable, str(script)], cwd=project_root).returncode != 0:
        raise SystemExit("世界设置核对发现需要处理的地方（见上方报告）：更新登记表后用 --snapshot 存快照再构建；"
                         "确认这次先不处理可加 --skip-world-audit")


def _project_key_dir() -> Path:
    """发布机上存放密钥的目录（项目外，不进 Git）；可用环境变量 DSTCAMP_KEY_DIR 改位置。"""
    return Path(os.environ.get("DSTCAMP_KEY_DIR") or r"F:\AI\mywork\ProjectKey\DSTCamp")


def build(patch_from: list[Path], *, signed: bool = True, world_audit: bool = True) -> None:
    release_dir = Path(__file__).resolve().parents[1] / "release"
    signing_key = _project_key_dir() / "update_signing_key.pem" if signed else None
    if signing_key is not None and not signing_key.is_file():
        raise SystemExit(f"找不到更新签名私钥：{signing_key}（开发自测可加 --unsigned，正式发布必须签名）")
    if signed and world_audit:
        _check_world_settings_audit(release_dir.parent)
    for old_exe in patch_from:
        if not old_exe.is_file():
            raise SystemExit(f"找不到旧版 EXE：{old_exe}")
        if old_exe.resolve().is_relative_to(release_dir):
            raise SystemExit(f"release/ 构建前会被清空，请先把旧版 EXE 移到别处：{old_exe}")
    try:
        import PyInstaller.__main__
    except ImportError as exc:
        raise SystemExit('请先运行 pip install -e ".[build]"') from exc
    try:
        import PySide6  # noqa: F401  发布入口是 Qt 版界面，缺 PySide6 打出来的 EXE 无法启动
    except ImportError as exc:
        raise SystemExit('缺少 PySide6，请先运行 pip install -e ".[build]"') from exc

    project_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(project_root))
    from dstools import __version__

    exe_name = f"DSTCamp-{__version__}"
    dist_root = project_root / "release"
    cache_root = project_root / "build"
    shutil.rmtree(dist_root, ignore_errors=True)
    dist_root.mkdir(parents=True)
    cache_root.mkdir(parents=True, exist_ok=True)
    staged_tools = _stage_tools(project_root, cache_root)
    staged_icons = _stage_icons(project_root, cache_root)

    spec_path = cache_root / f"{exe_name}.spec"
    spec_path.write_text(
        _spec_source(
            script=project_root / "scripts" / "run_gui.py",
            project_root=project_root,
            exe_name=exe_name,
            icon=staged_icons / "app" / "icon.ico",
            datas=[
                (staged_icons / "world", "icons/world"),
                (staged_icons / "ui", "icons/ui"),
                (staged_icons / "app", "icons/app"),
                (staged_icons / "recommended", "icons/recommended"),
                (staged_icons / "avatars", "icons/avatars"),
                (staged_icons / "ornaments", "icons/ornaments"),
                (staged_tools, "tools"),
            ],
        ),
        encoding="utf-8",
    )
    PyInstaller.__main__.run([
        str(spec_path),
        "--noconfirm",
        "--clean",
        f"--distpath={dist_root}",
        f"--workpath={cache_root / ('build_' + exe_name)}",
    ])

    onefile_exe = dist_root / f"{exe_name}.exe"
    _run_smoke_test(onefile_exe)

    patches = dict(_make_patch(project_root, old_exe.resolve(), onefile_exe, __version__)
                   for old_exe in patch_from)
    manifest_path = dist_root / f"{exe_name}.sha256.json"
    _write_sha256_manifest(manifest_path, __version__, (onefile_exe,), patches, signing_key)
    for old_version, entry in patches.items():
        print(f"增量补丁：{old_version} -> {__version__}  {entry['name']}（{entry['size'] / 1e6:.1f} MB）")
    if signing_key is None:
        print("警告：清单未签名，客户端不会自动安装这个版本，只能用于自测")
    offline = _write_offline_packages(
        dist_root, manifest_path, onefile_exe, [dist_root / entry["name"] for entry in patches.values()]
    )
    for package in offline:
        print(f"离线包（传蓝奏云，不传 GitHub/Gitee）：{package}（{package.stat().st_size / 1e6:.1f} MB）")

    print(f"构建及冒烟测试完成：{onefile_exe}")
    print(f"更新校验清单：{manifest_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="构建 DSTCamp 单文件 EXE")
    parser.add_argument("--patch-from", nargs="*", type=Path, default=[],
                        help="正式发布过的旧版 EXE（文件名须为 DSTCamp-版本号.exe），为每个生成增量补丁")
    parser.add_argument("--unsigned", action="store_true", help="不签名清单（仅开发自测，也不做发布前核对）")
    parser.add_argument("--skip-world-audit", action="store_true",
                        help="跳过发布前的世界设置适配核对（确认这次先不处理时使用）")
    arguments = parser.parse_args()
    build(arguments.patch_from, signed=not arguments.unsigned, world_audit=not arguments.skip_world_audit)
