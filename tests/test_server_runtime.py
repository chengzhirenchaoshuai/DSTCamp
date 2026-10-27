"""开服程序选项（server_runtime）规则与"唯一来源"守护测试。"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from _harness import run  # noqa: E402

from dstools.features.local_service import dedicated_server, server_runtime  # noqa: E402
from dstools.features.local_service.server_runtime import (  # noqa: E402
    RuntimeKind, RuntimeMode, get_runtime_mode, resolve_runtime, set_runtime_mode,
)
from dstools.shared import app_settings  # noqa: E402


def _make_install(common: Path, name: str, *, client: bool) -> Path:
    bin64 = common / name / "bin64"
    bin64.mkdir(parents=True)
    (bin64 / "dontstarve_dedicated_server_nullrenderer_x64.exe").write_bytes(b"")
    if client:
        (bin64 / "dontstarve_steam_x64.exe").write_bytes(b"")
    return common / name


def test_mode_resolution_and_migration() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        library = Path(tmp)
        common = library / "steamapps" / "common"
        client = _make_install(common, "Don't Starve Together", client=True)
        libraries = [library]
        with patch.object(server_runtime, "find_all_steam_libraries", return_value=libraries), \
                patch.object(dedicated_server, "find_all_steam_libraries", return_value=libraries):
            # 没选过、有游戏本体：定为游戏专服并保存；指定独立专服时如实报告未找到，不偷偷换成客户端。
            runtime = resolve_runtime().runtime
            assert runtime.kind is RuntimeKind.CLIENT and runtime.install_dir == client
            assert runtime.app_id == "322330" and runtime.mods_dir == client / "mods"
            assert app_settings.get_server_runtime_mode() == RuntimeMode.CLIENT.value
            assert resolve_runtime(RuntimeMode.DEDICATED).runtime is None

            # 之后装了独立专服：已定的选项不变；指定独立专服时用独立专服。
            dedicated = _make_install(common, "Don't Starve Together Dedicated Server", client=False)
            assert resolve_runtime().runtime.install_dir == client
            assert resolve_runtime(RuntimeMode.DEDICATED).runtime.install_dir == dedicated
            assert resolve_runtime(RuntimeMode.DEDICATED).runtime.app_id == "343050"

            # 旧版本把客户端目录存在专服路径里：迁移成客户端路径 + 客户端模式。
            app_settings.set_dedicated_server_path(client)
            assert resolve_runtime().runtime.install_dir == client
            assert app_settings.get_dedicated_server_path() is None
            assert app_settings.get_client_runtime_path() == client
            assert app_settings.get_server_runtime_mode() == RuntimeMode.CLIENT.value

            # 已明确选过模式时，迁移只挪路径不改模式。
            set_runtime_mode(RuntimeMode.DEDICATED)
            app_settings.set_dedicated_server_path(client)
            assert resolve_runtime().runtime.install_dir == dedicated
            assert app_settings.get_server_runtime_mode() == RuntimeMode.DEDICATED.value

            # 两个通道的独立专服彻底分开：Steam 客户端通道只认 Steam 库里的，客户端目录也不能冒充独立专服
            from dstools.features.local_service.steam_backend import SteamBackend, set_backend
            custom = _make_install(library, "dstserver", client=False)  # SteamCMD 装到 Steam 库以外、任意名字
            assert server_runtime.accepts_install_dir(RuntimeKind.DEDICATED, dedicated)
            assert not server_runtime.accepts_install_dir(RuntimeKind.DEDICATED, custom)
            assert not server_runtime.accepts_install_dir(RuntimeKind.DEDICATED, client)

            # SteamCMD 通道固定用独立专服、不扫 Steam 库：还没装时如实报告没有
            set_runtime_mode(RuntimeMode.CLIENT)
            set_backend(SteamBackend.STEAMCMD)
            assert resolve_runtime().runtime is None
            assert server_runtime.accepts_install_dir(RuntimeKind.DEDICATED, custom)
            assert not server_runtime.accepts_install_dir(RuntimeKind.DEDICATED, dedicated)

            # 旧版本把 SteamCMD 装的专服存在共用路径里：迁移给 SteamCMD 通道，SteamCMD 通道下也会迁移
            app_settings.set_dedicated_server_path(custom)
            assert resolve_runtime().runtime.install_dir == custom
            assert app_settings.get_dedicated_server_path() is None
            assert app_settings.get_steamcmd_server_path() == custom
            assert app_settings.get_server_runtime_mode() == RuntimeMode.CLIENT.value  # 用户存的「游戏专服」不改

            # 切回 Steam 客户端通道照旧用客户端；指定独立专服时用 Steam 库里的，不碰 SteamCMD 那份
            set_backend(SteamBackend.CLIENT)
            assert resolve_runtime().runtime.install_dir == client
            set_runtime_mode(RuntimeMode.DEDICATED)
            assert resolve_runtime().runtime.install_dir == dedicated


def test_usage_scenario_migration() -> None:
    from dstools.features.local_service import steam_client_updater
    from dstools.features.local_service.usage_scenario import migrate_usage_scenario, scenario_chosen

    with tempfile.TemporaryDirectory() as tmp:
        library = Path(tmp)
        common = library / "steamapps" / "common"
        _make_install(common, "Don't Starve Together", client=True)
        dedicated = _make_install(common, "Don't Starve Together Dedicated Server", client=False)
        libraries = [library]
        with patch.object(server_runtime, "find_all_steam_libraries", return_value=libraries), \
                patch.object(dedicated_server, "find_all_steam_libraries", return_value=libraries), \
                patch.object(steam_client_updater, "find_steam_executable", return_value=library / "steam.exe"):
            # 新用户（设置为空）：什么都不改，留给启动弹窗选场景；开服程序用到时再按本机情况定
            app_settings.save_settings({})
            migrate_usage_scenario()
            assert not scenario_chosen() and app_settings.get_server_runtime_mode() is None

            # 老用户原来自动选中独立专服：固定为独立专服；有 Steam 直接定为自己电脑，不再弹窗；只迁移一次
            app_settings.save_settings({"theme_name": "gray"})
            migrate_usage_scenario()
            assert app_settings.get_server_runtime_mode() == RuntimeMode.DEDICATED.value
            assert resolve_runtime().runtime.install_dir == dedicated
            assert app_settings.get_steam_backend() == "client"
            set_runtime_mode(RuntimeMode.CLIENT)
            migrate_usage_scenario()
            assert app_settings.get_server_runtime_mode() == RuntimeMode.CLIENT.value

            # 旧版本存的"自动"已去掉：有游戏本体定为游戏专服，只有独立专服定为独立专服，定下后保存
            app_settings.save_settings({"server_runtime_mode": "auto"})
            assert get_runtime_mode() is RuntimeMode.CLIENT
            with patch.object(server_runtime, "find_client_install_dir", return_value=None):
                app_settings.save_settings({"server_runtime_mode": "auto"})
                assert get_runtime_mode() is RuntimeMode.DEDICATED
            assert app_settings.get_server_runtime_mode() == RuntimeMode.DEDICATED.value


# 业务代码只能经 server_runtime 取开服程序目录。白名单：探测函数自身（dedicated_server/server_runtime）、
# 清理 V1 残留需扫遍所有目录的 legacy_v1、只做路径比较的 parser.is_dedicated_server_mods_dir
_ALLOWED = {
    "find_dedicated_server_dir(": {"dedicated_server.py", "server_runtime.py", "legacy_v1.py"},
    "get_dedicated_server_path(": {"dedicated_server.py", "server_runtime.py", "parser.py", "app_settings.py"},
    "get_client_runtime_path(": {"server_runtime.py", "app_settings.py"},
    "find_steamcmd_server_dir(": {"dedicated_server.py", "server_runtime.py"},
    "get_steamcmd_server_path(": {"dedicated_server.py", "server_runtime.py", "parser.py", "app_settings.py"},
}


def test_runtime_single_source() -> None:
    offenders = []
    for path in (PROJECT_ROOT / "dstools").rglob("*.py"):
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        for call, allowed in _ALLOWED.items():
            if path.name in allowed:
                continue
            if re.search(r"(?<![\w.])" + re.escape(call) + r"|\." + re.escape(call), text):
                offenders.append(f"{rel}: {call}")
    assert not offenders, "绕过 server_runtime 直接探测开服目录：\n" + "\n".join(offenders)


def test_storage_args() -> None:
    # 默认位置不传参数；其他位置（含跨盘）按官方写法拆成上级目录绝对路径 + 不含斜杠的文件夹名
    with tempfile.TemporaryDirectory() as tmp:
        documents = Path(tmp) / "Documents"
        with patch.object(dedicated_server, "get_documents_dir", return_value=documents):
            assert dedicated_server.resolve_storage_args(documents / "Klei" / "DoNotStarveTogether") == []
            custom = Path(tmp) / "Servers" / "DST Saves"
            assert dedicated_server.resolve_storage_args(custom) == [
                "-persistent_storage_root", str(custom.parent), "-conf_dir", "DST Saves"]


if __name__ == "__main__":
    run(globals())
