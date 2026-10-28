"""核心纯逻辑回归测试：Lua/INI 解析、存档与角色读取、Mod 解析与 Workshop 状态、备份、内网穿透、LuaJIT 等。"""

import contextlib
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _harness import run  # noqa: E402


from dstools.shared.lua_parser import (
    parse_lua_table,
    serialize_lua_table,
)
from dstools.shared.ini_parser import (
    parse_cluster_ini,
    parse_server_ini,
    write_cluster_ini,
)
from dstools.models import ClusterConfig, ModEntry
from dstools.features.mod.manager import (
    ModOverrides,
    load_mod_overrides,
    save_mod_overrides,
    enable_mod,
    list_mods,
    sync_mods,
)
from dstools.shared.discovery import discover_environment, pick_current_account
from dstools.features.save_browser.reader import (
    list_save_sessions,
    get_save_summary,
    list_session_players,
)
from dstools.features.save_browser.save_bundle import create_save_bundle
from dstools.features.cluster_config.config_manager import (
    backfill_cluster_defaults,
    load_shard_config,
    save_shard_config,
    set_shard_option,
    get_shard_option,
)
from dstools.features.save_browser.character_icons import (
    find_mod_character_name,
    resolve_character,
)
from dstools.shared.app_settings import (
    get_minimize_on_close,
    set_minimize_on_close,
    get_cache_use_exe_dir,
    get_cache_dir_override,
    set_cache_dir_override,
    get_backup_auto_enabled,
    set_backup_auto_enabled,
    set_backup_retention,
    get_global_tokens,
    set_global_tokens,
)
from dstools.features.local_service.backup_manager import (
    backup_dir,
    changed_since_last_backup,
    create_backup,
    restore_backup,
    list_backups,
)
from dstools.models import SaveSession, SaveSource
from dstools.features.mod.parser import (
    find_game_mods_dir,
    find_mod_folder,
    is_dedicated_server_mods_dir,
    is_custom_steam_mod_id,
    is_workshop_content_id,
    list_installed_mod_ids,
    parse_modinfo,
    split_installed_mod_counts,
    visible_config_options,
)
from dstools.shared.token_manager import (
    read_token,
)
from dstools.features.sakura.api import find_dstcamp_tunnel, sanitize_tunnel_name
from dstools.shared.app_settings import get_luajit_enabled, set_luajit_enabled
from dstools.shared.cluster_names import validate_cluster_folder_name
from dstools.features.save_browser.cluster_copy import (
    suggest_new_cluster_name,
    copy_local_cluster_to_server,
)
from dstools.features.local_service.dedicated_server import find_bin64_dir
from dstools.features.local_service.luajit_injector import (
    WORKSHOP_ID,
    InjectorState,
    LuajitMarker,
    apply_uninstall,
    detect_state,
    get_luajit_dir,
    is_workshop_subscribed,
    needs_regeneration,
    plan_install,
    read_marker,
    write_injector_path_marker,
    regenerate,
    resolve_launch_bin64_dir,
    write_marker,
)
from dstools.shared.steam_discovery import parse_library_folders, read_game_version_file
from dstools.shared.tex_convert import _vc2013_x86_runtime_problem


@contextlib.contextmanager
def _isolated_settings_dir():
    """把设置目录临时指向临时目录，测试不碰真实 %APPDATA%/DSTCamp/。

    resource_paths 以 ``from ... import get_settings_dir`` 持有独立引用，两个模块都要替换。"""
    import dstools.shared.app_settings as app_settings
    import dstools.shared.resource_paths as resource_paths

    original_in_app_settings = app_settings.get_settings_dir
    original_in_resource_paths = resource_paths.get_settings_dir
    with tempfile.TemporaryDirectory() as tmpdir:
        def patched():
            return Path(tmpdir)

        app_settings.get_settings_dir = patched
        resource_paths.get_settings_dir = patched
        try:
            yield Path(tmpdir)
        finally:
            app_settings.get_settings_dir = original_in_app_settings
            resource_paths.get_settings_dir = original_in_resource_paths






def test_lua_parser_roundtrip():
    """Lua 表往返：解析 -> 序列化 -> 再解析；多行字符串的换行和制表符要正确转义。"""
    original = (
        "return {\n"
        '    ["workshop-123"]={\n'
        "        configuration_options={\n"
        "            audio=false,\n"
        '            language="ch",\n'
        "            volume=0.75,\n"
        "            count=42\n"
        "        },\n"
        "        enabled=true\n"
        "    },\n"
        '    ["workshop-456"]={\n'
        "        configuration_options={},\n"
        "        enabled=false\n"
        "    }\n"
        "}"
    )
    parsed = parse_lua_table(original)
    serialized = serialize_lua_table(parsed)
    re_parsed = parse_lua_table(serialized)
    assert parsed == re_parsed, (
        f"Round-trip failed!\nOriginal parsed: {parsed}\nRe-parsed: {re_parsed}"
    )
    multiline = {"desc": "第一行\n第二行\t完成", "enabled": True}
    assert parse_lua_table(serialize_lua_table(multiline)) == multiline


def test_ini_parser():
    """用临时夹具验证 cluster.ini/server.ini，不读取用户真实存档。"""

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        cluster_ini = root / "cluster.ini"
        cluster_ini.write_text(
            "[GAMEPLAY]\n"
            "game_mode = survival\n"
            "max_players = 6\n\n"
            "[NETWORK]\n"
            "cluster_name = Test Cluster\n\n"
            "[SHARD]\n"
            "shard_enabled = true\n",
            encoding="utf-8",
        )
        config = parse_cluster_ini(cluster_ini)
        assert config.gameplay["game_mode"] == "survival"
        assert config.gameplay["max_players"] == 6
        assert config.network["cluster_name"] == "Test Cluster"
        assert config.shard["shard_enabled"] is True

        roundtrip = root / "cluster-roundtrip.ini"
        write_cluster_ini(config, roundtrip)
        reparsed = parse_cluster_ini(roundtrip)
        assert config.gameplay == reparsed.gameplay
        assert config.network == reparsed.network
        assert config.shard == reparsed.shard

        server_ini = root / "server.ini"
        server_ini.write_text(
            "[NETWORK]\nserver_port = 10999\n\n"
            "[SHARD]\nis_master = true\nname = Master\n",
            encoding="utf-8",
        )
        server = parse_server_ini(server_ini)
        assert server.network["server_port"] == 10999
        assert server.shard["is_master"] is True


def test_discovery():
    """用临时目录验证服务器/本地同名存档发现，不依赖本机环境。"""

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir) / "DoNotStarveTogether"
        for cluster in (root / "Shared", root / "123456" / "Shared"):
            (cluster / "Master").mkdir(parents=True)
            (cluster / "cluster.ini").write_text("[NETWORK]\n", encoding="utf-8")
            (cluster / "Master" / "server.ini").write_text(
                "[SHARD]\nis_master = true\n", encoding="utf-8"
            )
        incomplete = root / "Incomplete"
        incomplete.mkdir()
        (incomplete / "cluster.ini").write_text("[NETWORK]\n", encoding="utf-8")
        (root / "123456" / "client.ini").write_text("[GRAPHICS]\n", encoding="utf-8")
        # 纯数字的 Mod 文件夹误放在根目录（里面还有 DSTCamp 曾误写的 mod_config_data），不是账号
        fake = root / "378160973"
        (fake / "client_save" / "mod_config_data").mkdir(parents=True)
        (fake / "modinfo.lua").write_text("name = 'x'\n", encoding="utf-8")

        # 不读本机注册表和 Steam 登录记录（Steam 当前登录的账号会被加入账号列表）
        import dstools.shared.discovery as discovery
        saved = discovery.read_active_steam_account, discovery.read_steam_login_users
        discovery.read_active_steam_account, discovery.read_steam_login_users = (lambda: ""), (lambda: [])
        try:
            env = discover_environment(root, root.parent / "missing-wegame")
        finally:
            discovery.read_active_steam_account, discovery.read_steam_login_users = saved
        assert env.user_id == "123456"
        assert [account.id for account in env.accounts] == ["123456"]
        assert len(env.clusters) == 2
        assert {cluster.source for cluster in env.clusters} == {
            SaveSource.SERVER,
            SaveSource.LOCAL,
        }
        assert all(cluster.name == "Shared" for cluster in env.clusters)
        assert all(
            [shard.name for shard in cluster.shards] == ["Master"]
            for cluster in env.clusters
        )

        # 手动指定存档根目录：服务器存档从指定目录扫描，账号、本地存档和 client.ini 仍在游戏客户端的默认位置
        from unittest.mock import patch
        from dstools.shared.app_settings import set_klei_root_override
        custom = Path(tmpdir) / "Servers" / "MySaves"
        (custom / "Remote" / "Master").mkdir(parents=True)
        (custom / "Remote" / "cluster.ini").write_text("[NETWORK]\n", encoding="utf-8")
        (custom / "Remote" / "Master" / "server.ini").write_text("[SHARD]\nis_master = true\n", encoding="utf-8")
        set_klei_root_override(custom)
        try:
            with patch.object(discovery, "auto_klei_root", return_value=root), \
                    patch.object(discovery, "read_active_steam_account", return_value=""), \
                    patch.object(discovery, "read_steam_login_users", return_value=[]):
                env = discover_environment(None, root.parent / "missing-wegame")
        finally:
            set_klei_root_override(None)
        assert env.klei_root == custom and env.client_klei_root == root
        assert [c.name for c in env.clusters if c.source == SaveSource.SERVER] == ["Remote"]
        assert [c.name for c in env.clusters if c.source == SaveSource.LOCAL] == ["Shared"]
        assert env.user_id == "123456"


def test_pick_current_account():
    """多账号时按 手动选择 > Steam 当前登录 > 最近登录 > 最近游玩 > 第一个 选当前账号，且只认给定的账号。"""
    ids = ["111", "222", "333"]
    assert pick_current_account([]) == ""
    assert pick_current_account(ids, selected="333", selected_active="222", active="222") == "333"
    assert pick_current_account(ids, selected="333") == "333"  # Steam 未运行时手动选择仍有效
    # Steam 换了账号：手动选择失效，跟随 Steam
    assert pick_current_account(ids, selected="333", selected_active="111", active="222") == "222"
    assert pick_current_account(ids, selected="999", active="222", recent=["111"]) == "222"
    assert pick_current_account(ids, active="999", recent=["888", "111"]) == "111"
    assert pick_current_account(ids, last_played={"111": 5.0, "333": 9.0}) == "333"
    assert pick_current_account(ids) == "111"


def test_save_reader():
    """用临时存档槽验证会话发现和摘要，不读取用户真实存档。"""

    with tempfile.TemporaryDirectory() as tmpdir:
        session_dir = Path(tmpdir) / "Master" / "save" / "session" / "ABC123"
        session_dir.mkdir(parents=True)
        slot = session_dir / "0000000001"
        slot.write_bytes(b"save")
        slot.with_suffix(".meta").write_text(
            "return {clock={cycles=12,phase=\"dusk\"},"
            "seasons={season=\"spring\",elapseddaysinseason=3,remainingdaysinseason=17}}",
            encoding="utf-8",
        )

        sessions = list_save_sessions(Path(tmpdir) / "Master")
        assert len(sessions) == 1
        session = sessions[0]
        assert session.session_id == "ABC123"
        assert len(session.slots) == 1
        assert session.metadata is not None and session.metadata.day == 12
        assert get_save_summary(session) == "第12天, 春季第3天, 黄昏, [1个存档槽]"


def test_mod_manager():
    """Mod 覆盖配置：启用、保存重读与同步。"""

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir) / "modoverrides.lua"
        overrides = ModOverrides(path=tmp_path)

        enable_mod(overrides, "workshop-test-1")
        assert "workshop-test-1" in overrides.mods
        assert overrides.mods["workshop-test-1"].enabled

        save_mod_overrides(overrides)
        reloaded = load_mod_overrides(tmp_path)
        assert len(list_mods(reloaded)) == 1

        a = ModOverrides(path=Path(tmpdir) / "a.lua")
        b = ModOverrides(path=Path(tmpdir) / "b.lua")
        enable_mod(a, "workshop-shared")
        enable_mod(a, "workshop-only-a")
        enable_mod(b, "workshop-shared")
        enable_mod(b, "workshop-only-b")

        sync_mods(a, b)
        assert "workshop-only-a" in b.mods
        assert "workshop-only-b" not in b.mods




def test_list_session_players():
    """测试单个会话目录下按玩家角色发现/解析存档。"""

    with tempfile.TemporaryDirectory() as tmpdir:
        session_dir = Path(tmpdir) / "session" / "ABCDEF0123456789"
        session_dir.mkdir(parents=True)

        # 正常玩家：真实存档的字节结构（3 字节二进制前缀 + return {...} + 0x01 结尾）
        good_dir = session_dir / "A7GOODPLAYER"
        good_dir.mkdir()
        table_text = (
            "return {x=100.5,z=-50.25,data={health={health=120},"
            "sanity={current=150,sane=true},hunger={hunger=100},"
            'age={age=42}},age=0,prefab="wilson"}'
        )
        (good_dir / "0000000007").write_bytes(
            b"\x03\x11\x22" + table_text.encode("utf-8") + b"\x01"
        )
        (good_dir / "0000000007.meta").write_bytes(b'return {character="wilson"}\x00')

        # 一个损坏玩家：主文件里完全没有 return，模拟解析失败——验证
        # "一个玩家坏了不连累其他玩家"。
        bad_dir = session_dir / "A7BADPLAYER0"
        bad_dir.mkdir()
        (bad_dir / "0000000003").write_bytes(b"\x00\x01\x02\x03garbage, not lua at all")
        (bad_dir / "0000000003.meta").write_bytes(b'return {character="wolfgang"}\x00')

        session = SaveSession(session_id="ABCDEF0123456789", path=session_dir)
        players = list_session_players(session)
        assert len(players) == 2, f"Expected 2 players, got {len(players)}"

        by_id = {p.player_id: p for p in players}
        good = by_id["A7GOODPLAYER"]
        assert not good.parse_error, (
            f"Good player should parse cleanly, got: {good.parse_error}"
        )
        assert good.character == "wilson"
        assert good.health == 120
        assert good.sanity == 150
        assert good.hunger == 100

        bad = by_id["A7BADPLAYER0"]
        assert bad.parse_error, "Corrupt player should have parse_error set"
        assert bad.player_id == "A7BADPLAYER0"

        # 最新槽位是 0 字节占位文件时回退读上一个槽位（跨世界传送/保存中断时真机复现）
        empty_latest_dir = session_dir / "A7EMPTYLATEST"
        empty_latest_dir.mkdir()
        (empty_latest_dir / "0000000010").write_bytes(
            b"\x03\x11\x22"
            + 'return {data={health={health=88}},prefab="willow"}'.encode("utf-8")
            + b"\x01"
        )
        (empty_latest_dir / "0000000010.meta").write_bytes(
            b'return {character="willow"}\x00'
        )
        (empty_latest_dir / "0000000011").write_bytes(b"")  # 0 字节占位文件
        (empty_latest_dir / "0000000011.meta").write_bytes(
            b'return {character="willow"}\x00'
        )

        players = list_session_players(session)
        empty_latest = next(p for p in players if p.player_id == "A7EMPTYLATEST")
        assert not empty_latest.parse_error, (
            f"Should fall back to slot 10, got: {empty_latest.parse_error}"
        )
        assert empty_latest.save_file.name == "0000000010"
        assert empty_latest.health == 88




def test_character_icons():
    """Mod 角色名扫描与 resolve_character 回退链（头像转换依赖真实 Steam/ktech，不在此测试）。"""

    with tempfile.TemporaryDirectory() as tmp:
        mod_folder = Path(tmp) / "fake_mod"
        prefab_dir = mod_folder / "scripts" / "prefabs"
        prefab_dir.mkdir(parents=True)
        (prefab_dir / "testchar.lua").write_text(
            'STRINGS.CHARACTER_NAMES.testchar = "测试角色"\n'
            'STRINGS.CHARACTER_TITLES.testchar = "某个称号"\n',
            encoding="utf-8",
        )

        assert find_mod_character_name(mod_folder, "testchar") == "测试角色"

        assert find_mod_character_name(mod_folder, "no_such_prefab") is None

    # 官方角色表命中：不需要 mod_overrides_path，直接走官方分支。
    name, _icon = resolve_character("wilson", None)
    assert name == "威尔逊.P.希格斯伯里"

    # 哪里都找不到（未知 prefab + 不存在的 modoverrides 路径）：原样回退，
    # 不抛异常、不给头像。
    name, icon = resolve_character(
        "totally_unknown_prefab", Path(tmp) / "does_not_exist.lua"
    )
    assert name == "totally_unknown_prefab" and icon is None

    # 模组脚本加密、读不出中文名，但带了头像：不能把头像跟着名字一起丢掉。
    # （真机复现：Cluster_New 里 thsj_peiling / mcw 两个角色。）
    from unittest.mock import patch

    from dstools.features.save_browser import character_icons

    class _Entry:
        workshop_id = "workshop-1"
        enabled = True

    fake_icon = Path("fake_avatar.png")
    overrides_file = Path(tempfile.gettempdir()) / "modoverrides.lua"
    with patch.object(Path, "exists", lambda self: True), \
            patch("dstools.features.mod.manager.load_mod_overrides", return_value={}), \
            patch("dstools.features.mod.manager.list_mods", return_value=[_Entry()]), \
            patch("dstools.features.mod.parser.find_mod_folder", return_value=Path("fake_mod")), \
            patch.object(character_icons, "find_mod_character_name", return_value=None), \
            patch.object(character_icons, "get_mod_avatar_path", return_value=fake_icon):
        name, icon = resolve_character("encrypted_char", overrides_file)
    assert name == "encrypted_char" and icon == fake_icon, (name, icon)

    # 模组后来被停用、角色数据还在的存档：停用的模组也要能给出头像。
    _Entry.enabled = False
    with patch.object(Path, "exists", lambda self: True), \
            patch("dstools.features.mod.manager.load_mod_overrides", return_value={}), \
            patch("dstools.features.mod.manager.list_mods", return_value=[_Entry()]), \
            patch("dstools.features.mod.parser.find_mod_folder", return_value=Path("fake_mod")), \
            patch.object(character_icons, "find_mod_character_name", return_value=None), \
            patch.object(character_icons, "get_mod_avatar_path", return_value=fake_icon):
        name, icon = resolve_character("disabled_mod_char", overrides_file)
    assert name == "disabled_mod_char" and icon == fake_icon, (name, icon)


def test_modinfo_reader():
    """用手写的合成 mod 数据测试 modinfo.lua 解析逻辑（parser.py）——这段
    逻辑此前完全没有功能测试覆盖，只在程序启动导入模块时被间接跑到。"""

    with tempfile.TemporaryDirectory() as tmp:
        mod_folder = Path(tmp) / "123456"
        mod_folder.mkdir()
        (mod_folder / "modinfo.lua").write_text(
            """
            name = "Test Mod"
            author = "Tester"
            version = "1.0.0"
            description = "A test mod for parsing."
            icon = "modicon.tex"
            icon_atlas = "modicon.xml"

            configuration_options = {
                {
                    name = "difficulty",
                    label = "Difficulty",
                    hover = "How hard",
                    options = {
                        {description = "Easy", data = "easy"},
                        {description = "Hard", data = "hard"},
                    },
                    default = "easy",
                },
            }
            """,
            encoding="utf-8",
        )

        info = parse_modinfo(mod_folder)
        assert info is not None
        assert (
            info.name == "Test Mod"
            and info.author == "Tester"
            and info.version == "1.0.0"
        )
        assert info.workshop_id == "workshop-123456"

        assert len(info.config_options) == 1
        opt = info.config_options[0]
        assert opt.name == "difficulty" and opt.label == "Difficulty"
        assert [c["data"] for c in opt.choices] == ["easy", "hard"]

        # 不存在 modinfo.lua 的文件夹：明确返回 None，不抛异常。
        assert parse_modinfo(Path(tmp) / "does_not_exist") is None

        # client_only_mod 同时 server_only_mod（DontStarveLuaJIT2 的写法）仍按服务器 Mod 处理
        local_folder = Path(tmp) / "654321"
        local_folder.mkdir()
        (local_folder / "modinfo.lua").write_text(
            'name = "Local Only Mod"\nclient_only_mod = true\n', encoding="utf-8"
        )
        assert parse_modinfo(local_folder).client_only is True

        server_folder = Path(tmp) / "654322"
        server_folder.mkdir()
        (server_folder / "modinfo.lua").write_text(
            'name = "LuaJIT-style Mod"\nclient_only_mod = true\nserver_only_mod = true\n',
            encoding="utf-8",
        )
        assert parse_modinfo(server_folder).client_only is False

        # 选项自带 client = true 的纯客户端设置应被隐藏（见 visible_config_options）
        client_folder = Path(tmp) / "654323"
        client_folder.mkdir()
        (client_folder / "modinfo.lua").write_text(
            """
            name = "Mixed Config Mod"
            configuration_options = {
                { name = "", label = "Server Settings", options = {{description = "", data = false}}, default = false },
                { name = "server_opt", label = "Server Opt", options = {{description = "On", data = true}}, default = true },
                { name = "", label = "Client Settings", options = {{description = "", data = false}}, default = false },
                { name = "client_opt", label = "Client Opt", options = {{description = "On", data = true}}, default = true, client = true },
            }
            """,
            encoding="utf-8",
        )
        info = parse_modinfo(client_folder)
        by_name = {o.name: o for o in info.config_options}
        assert by_name["server_opt"].client is False
        assert by_name["client_opt"].client is True

        visible = visible_config_options(info.config_options)
        visible_names = [o.name for o in visible]
        assert visible_names == ["", "server_opt"], (
            f"应该只剩服务端标题+选项，客户端标题和选项整组一起隐藏: {visible_names}"
        )

        # Configs Extended（3317960157）约定字段的解析，控件读写已用真实 Mod（3686724289）人工验证
        configs_extended_folder = Path(tmp) / "654324"
        configs_extended_folder.mkdir()
        (configs_extended_folder / "modinfo.lua").write_text(
            """
            name = "Configs Extended Style Mod"
            configuration_options = {
                { name = "ban_recipe_list", label = "Ban List", is_set_config = true,
                  options = {{description = "请启用配置扩展模组！", data = {}}}, default = {} },
                { name = "priority_list", label = "Priority List", is_array_config = true,
                  options = {{description = "请启用配置扩展模组！", data = {}}}, default = {} },
                { name = "welcome_msg", label = "Welcome Message", is_text_config = true,
                  options = {{description = "请启用配置扩展模组！", data = ""}}, default = "" },
                { name = "starting_items", label = "Starting Items", is_dictionary_config = true,
                  options = {{description = "请启用配置扩展模组！", data = {}}}, default = {} },
            }
            """,
            encoding="utf-8",
        )
        info = parse_modinfo(configs_extended_folder)
        by_name = {o.name: o for o in info.config_options}
        assert by_name["ban_recipe_list"].is_set_config is True
        assert by_name["ban_recipe_list"].is_header is False
        assert by_name["priority_list"].is_array_config is True
        assert by_name["welcome_msg"].is_text_config is True
        assert by_name["starting_items"].is_dictionary_config is True

        # is_dictionary_config 的值是字符串键值表，通用 Lua 读写路径需能原样往返
        overrides_path = Path(tmp) / "modoverrides.lua"
        mod_overrides = ModOverrides(path=overrides_path)
        mod_overrides.mods["workshop-654324"] = ModEntry(
            workshop_id="workshop-654324",
            enabled=True,
            configuration_options={
                "starting_items": {"草": "6个", "树枝": "6个", "燧石": "2个"}
            },
        )
        save_mod_overrides(mod_overrides)
        reloaded = load_mod_overrides(overrides_path)
        assert reloaded.mods["workshop-654324"].configuration_options[
            "starting_items"
        ] == {"草": "6个", "树枝": "6个", "燧石": "2个"}

        # 坑：数组配置被解析成 "1"/"2"... 键的 dict 而不是 list，编辑器必须识别，否则会把原有数据清空
        from dstools.qt.mod_config_dialog import ModConfigDialog

        parsed_array_shape = {
            "1": "torch",
            "2": "backpack",
            "3": "axe",
        }  # parse_lua_table() 对数组字面量的真实产出形状
        assert ModConfigDialog._raw_value_to_lines("array", parsed_array_shape) == [
            "torch",
            "backpack",
            "axe",
        ]
        assert ModConfigDialog._raw_value_to_lines("array", {}) == []
        assert ModConfigDialog._raw_value_to_lines("array", ["torch", "backpack"]) == [
            "torch",
            "backpack",
        ]


def test_mod_config_choice_keeps_unlisted_value():
    """下拉项里没有当前值（万界之门 worlds 表由 DSTCamp 写入）时，不动它直接"应用"要原样保留。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from dstools.features.mod.parser import ModConfigOption, ModInfo
    from dstools.qt.mod_config_dialog import ModConfigDialog

    app = QApplication.instance() or QApplication([])
    worlds = {"1": {"name": "森林"}, "3": {"name": "猪镇", "style": "porkland"}}
    options = [
        ModConfigOption(name="worlds", default="", choices=[{"description": "自动", "data": ""}]),
        ModConfigOption(name="cave_portal_shard", default="Caves",
                        choices=[{"description": "原洞穴", "data": "Caves"}, {"description": "不固定", "data": ""}]),
        ModConfigOption(name="hud_button", default=False,
                        choices=[{"description": "关闭", "data": False}, {"description": "开启", "data": True}]),
    ]
    dialog = ModConfigDialog.__new__(ModConfigDialog)
    dialog.mod = ModEntry(workshop_id="workshop-3822260794", enabled=True,
                          configuration_options={"worlds": worlds, "cave_portal_shard": "Forest2"})
    dialog.mod_info = ModInfo(config_options=options)
    dialog.read_only, dialog.vars, dialog.choice_maps, dialog.raw_widgets = False, {}, {}, {}
    rows = [dialog._render_choice_row(opt) for opt in options]  # 留住引用，否则下拉框随行控件被回收
    assert dialog._collect_values() == {"worlds": worlds, "cave_portal_shard": "Forest2", "hud_button": False}
    # 值在可选项里时不加"保持当前值"项；用户选了别的项照常覆盖
    assert dialog.choice_maps["hud_button"] == [False, True]
    dialog.vars["worlds"].setCurrentIndex(1)
    assert dialog._collect_values()["worlds"] == ""
    del rows, app


def test_workshop_content_directory_filter():
    """Steam UGC 内容目录只接受标准 PublishedFileId_t 文件夹名。"""

    assert is_workshop_content_id("3485293431")
    assert is_workshop_content_id("18446744073709551615")
    for invalid in (
        "",
        "0",
        "00123",
        "3485293431_bak",
        "workshop-3485293431",
        "１２３",
        "18446744073709551616",
    ):
        assert not is_workshop_content_id(invalid), invalid

    assert not is_custom_steam_mod_id("workshop-3485293431")
    assert not is_custom_steam_mod_id("workshop-18446744073709551615")
    for custom in ("CommonModSets", "my_local_mod", "workshop-demo", "workshop-00123"):
        assert is_custom_steam_mod_id(custom), custom

    from dstools.models import Platform

    sample_ids = ["workshop-1", "workshop-2", "CommonModSets"]
    assert split_installed_mod_counts(sample_ids, Platform.STEAM) == (2, 1)
    assert split_installed_mod_counts(sample_ids, Platform.WEGAME) == (3, 0)

    import dstools.features.mod.parser as mod_parser

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        workshop = root / "content" / "322330"
        local_mods = root / "mods"
        workshop.mkdir(parents=True)
        local_mods.mkdir()
        for folder_name in ("3485293431", "3485293431_bak", "00123", "１２３"):
            folder = workshop / folder_name
            folder.mkdir()
            (folder / "modinfo.lua").write_text(
                f'name = "{folder_name}"\n', encoding="utf-8"
            )
        local = local_mods / "my_local_mod_bak"
        local.mkdir()
        (local / "modinfo.lua").write_text('name = "Local"\n', encoding="utf-8")
        legacy = local_mods / "workshop-463952377"
        legacy.mkdir()
        (legacy / "modinfo.lua").write_text('name = "Legacy"\n', encoding="utf-8")

        original_workshop = mod_parser.find_workshop_dir
        original_game_mods = mod_parser.find_game_mods_dir
        try:
            mod_parser.find_workshop_dir = lambda: workshop
            mod_parser.find_game_mods_dir = lambda: local_mods
            ids = list_installed_mod_ids()
            assert ids == [
                "workshop-3485293431",
                "my_local_mod_bak",
                "workshop-463952377",
            ]
            assert find_mod_folder("workshop-3485293431") == workshop / "3485293431"
            assert find_mod_folder("463952377") == legacy
            assert find_mod_folder("workshop-3485293431_bak") is None
        finally:
            mod_parser.find_workshop_dir = original_workshop
            mod_parser.find_game_mods_dir = original_game_mods

    # 没安装客户端时，专服自己的 mods 绝不能回退成客户端源目录，否则
    # “创建软连接”会得到源=目标的古怪提示，甚至存在误操作风险。
    from dstools.shared import app_settings

    with tempfile.TemporaryDirectory() as tmp:
        library = Path(tmp)
        server_mods = (
            library
            / "steamapps"
            / "common"
            / "Don't Starve Together Dedicated Server"
            / "mods"
        )
        server_mods.mkdir(parents=True)
        original_libraries = mod_parser.find_all_steam_libraries
        original_override = app_settings.get_steam_mods_path
        original_server_path = app_settings.get_dedicated_server_path
        try:
            mod_parser.find_all_steam_libraries = lambda: [library]
            app_settings.get_steam_mods_path = lambda: server_mods
            app_settings.get_dedicated_server_path = lambda: server_mods.parent
            assert is_dedicated_server_mods_dir(server_mods)
            assert find_game_mods_dir() is None
        finally:
            mod_parser.find_all_steam_libraries = original_libraries
            app_settings.get_steam_mods_path = original_override
            app_settings.get_dedicated_server_path = original_server_path






def test_cluster_copy():
    """测试"复制为服务器存档"逻辑（cluster_copy.py）：名称校验、默认名建
    议、以及实际的文件夹复制。"""

    # 白名单：只允许英文字母、数字、下划线
    assert validate_cluster_folder_name("MyServer") is None
    assert validate_cluster_folder_name("Cluster_5") is None
    assert validate_cluster_folder_name("") == "empty"
    assert validate_cluster_folder_name("   ") == "empty"
    assert validate_cluster_folder_name("bad/name") == "invalid_chars"
    assert validate_cluster_folder_name("..") == "invalid_chars"
    assert validate_cluster_folder_name("我的存档") == "invalid_chars"
    assert validate_cluster_folder_name("my server") == "invalid_chars"
    assert validate_cluster_folder_name("my-server") == "invalid_chars"

    with tempfile.TemporaryDirectory() as tmp:
        klei_root = Path(tmp) / "klei_root"
        klei_root.mkdir()
        (klei_root / "Cluster_1").mkdir()  # 已占用

        assert suggest_new_cluster_name(klei_root, "Cluster_1") == "Cluster_2"
        assert suggest_new_cluster_name(klei_root, "MyLocalSave") == "MyLocalSave"

        # 造一个假的本地 cluster 文件夹（cluster.ini + 一个假世界子目录），
        # 复制到 klei_root 下一个新名字。
        local_cluster = Path(tmp) / "local_user" / "Cluster_1"
        (local_cluster / "Master").mkdir(parents=True)
        (local_cluster / "cluster.ini").write_text(
            "[GAMEPLAY]\nmax_players=6\n", encoding="utf-8"
        )
        (local_cluster / "Master" / "server.ini").write_text(
            "[NETWORK]\n", encoding="utf-8"
        )

        logs = []
        dest = copy_local_cluster_to_server(
            local_cluster, klei_root, "Cluster_2", on_log=logs.append
        )
        assert dest == klei_root / "Cluster_2"
        assert (dest / "cluster.ini").read_text(encoding="utf-8") == (
            local_cluster / "cluster.ini"
        ).read_text(encoding="utf-8")
        assert (dest / "Master" / "server.ini").exists()
        assert local_cluster.exists() and (local_cluster / "cluster.ini").exists(), (
            "源文件夹必须保持不变"
        )
        assert len(logs) > 0

        try:
            copy_local_cluster_to_server(local_cluster, klei_root, "Cluster_2")
            assert False, "Copying onto an already-existing destination must raise"
        except FileExistsError:
            pass

        # 源存档带空的 cluster_token.txt 时也要从全局令牌池补上（按 is_valid_token 判断，不看文件是否存在）
        original_pool = get_global_tokens()
        try:
            fake_token = "x" * 30
            set_global_tokens([fake_token])
            (local_cluster / "cluster_token.txt").write_text("", encoding="utf-8")
            dest3 = copy_local_cluster_to_server(local_cluster, klei_root, "Cluster_3")
            assert read_token(dest3 / "cluster_token.txt") == fake_token, (
                "源存档带的是空 cluster_token.txt，应该被判定为无效并从全局令牌池自动补上"
            )
        finally:
            set_global_tokens(original_pool)




def test_app_settings_toggles():
    """验证持久化开关，以及缓存、数据和安全目录的边界。"""

    with _isolated_settings_dir():
        for get_fn, set_fn, default in (
            (get_minimize_on_close, set_minimize_on_close, True),
            (get_backup_auto_enabled, set_backup_auto_enabled, True),
        ):
            assert get_fn() is default, f"{get_fn.__name__} 默认值应该是 {default}"
            set_fn(not default)
            assert get_fn() is not default
            set_fn(default)
            assert get_fn() is default

        from dstools.shared.app_settings import get_settings_dir
        from unittest.mock import patch

        import dstools.shared.resource_paths as resource_paths_module
        from dstools.shared.resource_paths import (
            cache_root_dir,
            cache_dir,
            data_dir,
            path_is_ascii,
            runtime_tool_path,
            security_dir,
            validate_cache_root,
        )

        root = get_settings_dir()
        custom_cache = root / "custom-cache"
        assert get_cache_dir_override() is None
        from dstools.shared.app_settings import load_settings, save_settings

        save_settings({**load_settings(), "cache_use_exe_dir": True})
        assert get_cache_use_exe_dir() is True
        set_cache_dir_override(custom_cache)
        assert get_cache_dir_override() == custom_cache
        assert get_cache_use_exe_dir() is False
        assert cache_root_dir() == custom_cache
        assert validate_cache_root(custom_cache) is None
        assert path_is_ascii(custom_cache) is True
        assert validate_cache_root(root / "中文缓存") == "non_ascii"
        assert validate_cache_root(Path("relative-cache")) == "not_absolute"
        set_cache_dir_override(None)
        assert get_cache_dir_override() is None
        assert cache_root_dir() == root / "cache"

        legacy_background = cache_dir("background")
        legacy_background.mkdir(parents=True)
        (legacy_background / "custom.png").write_bytes(b"image")
        migrated = data_dir("background", legacy_cache_name="background")
        assert migrated == root / "data" / "background"
        assert (migrated / "custom.png").read_bytes() == b"image"
        assert security_dir("frp_selfhost") == root / "security" / "frp_selfhost"

        bundled_tool = root / "bundle" / "tools" / "frpc" / "frpc.exe"
        bundled_tool.parent.mkdir(parents=True)
        bundled_tool.write_bytes(b"frpc")
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "_MEIPASS", str(root / "bundle"), create=True),
        ):
            stable_tool = runtime_tool_path("frpc/frpc.exe")
        assert stable_tool.parent.parent.parent == root / "data" / "runtime_tools"
        assert stable_tool.read_bytes() == b"frpc"

        # 单文件版的 .xz 工具：真实压缩再解压，内容一致且落到哈希缓存目录，第二次命中缓存
        import lzma

        xz_bundled = root / "bundle" / "tools" / "frpc-xz" / "frpc.exe.xz"
        xz_bundled.parent.mkdir(parents=True)
        xz_bundled.write_bytes(lzma.compress(b"frpc-compressed-payload"))
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "_MEIPASS", str(root / "bundle"), create=True),
        ):
            xz_stable_tool = runtime_tool_path("frpc-xz/frpc.exe")
            mtime_first = xz_stable_tool.stat().st_mtime_ns
            xz_stable_tool_again = runtime_tool_path("frpc-xz/frpc.exe")
        assert xz_stable_tool.parent.parent.parent == root / "data" / "runtime_tools"
        assert xz_stable_tool.read_bytes() == b"frpc-compressed-payload"
        assert xz_stable_tool_again == xz_stable_tool
        assert xz_stable_tool_again.stat().st_mtime_ns == mtime_first

        # 源码模式下仓库只有 .xz 没有裸文件时也要解压落地（曾因此误报"客户端文件缺失"）
        source_mode_tools = root / "source_mode_tools"
        xz_only = source_mode_tools / "frpc-src" / "frpc.exe.xz"
        xz_only.parent.mkdir(parents=True)
        xz_only.write_bytes(lzma.compress(b"source-mode-frpc-payload"))
        with (
            patch.object(sys, "frozen", False, create=True),
            patch.object(
                resource_paths_module, "tool_binary_dir", return_value=source_mode_tools
            ),
        ):
            source_mode_tool = runtime_tool_path("frpc-src/frpc.exe")
        assert source_mode_tool.read_bytes() == b"source-mode-frpc-payload"
        assert source_mode_tool.parent.parent.parent == root / "data" / "runtime_tools"


def test_cache_path_user_guidance():
    """缓存路径含非 ASCII 字符时启动即提醒；重启走等待旧实例退出的辅助进程。"""
    import subprocess
    from types import SimpleNamespace
    from unittest.mock import Mock, patch

    from dstools.qt import window as qt_window
    from dstools.shared import resource_paths

    invalid_path = Path("C:/Users/中文用户/AppData/Roaming/DSTCamp/cache")
    dummy = SimpleNamespace(show_cache_dir_dialog=Mock())
    with (
        patch.object(resource_paths, "cache_root_dir", return_value=invalid_path),
        patch.object(resource_paths, "path_is_ascii", return_value=False),
        patch.object(qt_window.dialogs, "ask_choice", return_value="fix") as ask_choice,
    ):
        qt_window.MainWindow.check_cache_dir_on_startup(dummy)
    assert str(invalid_path) in ask_choice.call_args.args[2]
    dummy.show_cache_dir_dialog.assert_called_once_with()

    dummy.show_cache_dir_dialog.reset_mock()
    with (
        patch.object(resource_paths, "path_is_ascii", return_value=True),
        patch.object(qt_window.dialogs, "ask_choice") as ask_choice,
    ):
        qt_window.MainWindow.check_cache_dir_on_startup(dummy)
    ask_choice.assert_not_called()

    import scripts.run_gui as run_gui

    with (
        patch.object(run_gui, "_wait_for_process_exit") as wait_for_exit,
        patch.object(run_gui.subprocess, "Popen") as start_process,
        patch.object(run_gui.sys, "frozen", True, create=True),
    ):
        run_gui._run_restart_helper(12345, ["--example"])
    wait_for_exit.assert_called_once_with(12345)
    assert start_process.call_args.args[0][-1] == "--example"
    assert start_process.call_args.kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"

    dummy = SimpleNamespace(quit_app=Mock())
    with (
        patch.object(sys, "frozen", True, create=True),
        patch.object(subprocess, "Popen") as start_helper,
    ):
        qt_window.MainWindow._spawn_restart_and_quit(dummy)
    command = start_helper.call_args.args[0]
    assert "--restart-helper" in command and str(os.getpid()) in command
    assert start_helper.call_args.kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    dummy.quit_app.assert_called_once_with()


def test_mod_sync_junction():
    """验证 Mod 目录联接的直接替换、解除复制和源目标保护。"""

    from unittest.mock import patch

    from dstools.features.mod.sync import (
        _ensure_junction,
        detach_mod_sync_junction,
        plan_mod_sync,
    )

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "client_mods" / "workshop-123"
        target = Path(tmp) / "server_mods" / "workshop-123"
        src.mkdir(parents=True)
        (src / "modinfo.lua").write_text("name = 'test'")

        _ensure_junction(target, src)
        assert os.path.isjunction(target), "target 应该变成指向 src 的目录联接"
        assert (target / "modinfo.lua").read_text() == "name = 'test'", (
            "透过联接应该能读到 src 里的真实内容"
        )

        _ensure_junction(target, src)
        assert os.path.isjunction(target), "重复调用应该保持联接，不报错"

        # 已有真实文件夹按新规则直接删除，不再保留长期备份。
        real_target = Path(tmp) / "server_mods" / "workshop-456"
        real_src = Path(tmp) / "client_mods" / "workshop-456"
        real_src.mkdir(parents=True)
        (real_src / "modinfo.lua").write_text("name = 'legacy copy source'")
        real_target.mkdir(parents=True)
        (real_target / "modinfo.lua").write_text("stale copied content")

        replaced = _ensure_junction(real_target, real_src, allow_replace=True)
        assert os.path.isjunction(real_target), "已存在的真实文件夹应该被替换成联接"
        assert (
            real_target / "modinfo.lua"
        ).read_text() == "name = 'legacy copy source'", (
            "替换后应该读到 src 的内容，不是残留的旧复制内容"
        )
        assert real_src.exists() and (real_src / "modinfo.lua").exists(), (
            "删除 target 这个联接本身，绝不能牵连删除它指向的 src 真实内容"
        )
        assert replaced is True
        assert not list(real_target.parent.glob("mods.dstcamp-backup-*"))

        # 用户确认的是永久删除；因此 mklink 后续失败时不会偷偷恢复或创建
        # 长期备份，但客户端源目录始终不得受影响。
        rollback_target = Path(tmp) / "server_mods" / "rollback"
        rollback_src = Path(tmp) / "client_mods" / "rollback"
        rollback_target.mkdir(parents=True)
        rollback_src.mkdir(parents=True)
        (rollback_target / "old.txt").write_text("keep")

        class FailedMklink:
            returncode = 1
            stderr = "forced failure"
            stdout = ""

        with patch(
            "dstools.features.mod.sync.subprocess.run", return_value=FailedMklink()
        ):
            try:
                _ensure_junction(rollback_target, rollback_src, allow_replace=True)
            except OSError:
                pass
            else:
                raise AssertionError("mklink 失败时应该抛出异常")
        assert not os.path.lexists(rollback_target)
        assert rollback_src.is_dir()

        # 源目录和目标目录相同的场景必须在真正改动前拒绝。
        same_root = Path(tmp) / "same_install"
        same_target = same_root / "mods"
        same_target.mkdir(parents=True)
        plan = plan_mod_sync(same_root, same_target)
        assert plan.invalid_reason and not plan.needs_confirm_delete

        # 解除整目录联接时要完整复制客户端 mods，而不是恢复历史备份或只
        # 迁移某一类 V1 目录。
        install = Path(tmp) / "dedicated"
        server_mods = install / "mods"
        client_mods = Path(tmp) / "whole_client_mods"
        server_mods.mkdir(parents=True)
        client_mods.mkdir()
        (server_mods / "stock.txt").write_text("server stock", encoding="utf-8")
        (client_mods / "client-root.txt").write_text("client copy", encoding="utf-8")
        client_v1 = client_mods / "workshop-987654321"
        client_v1.mkdir()
        (client_v1 / "modinfo.lua").write_text('name = "Client V1"', encoding="utf-8")
        assert _ensure_junction(server_mods, client_mods, allow_replace=True) is True
        assert os.path.isjunction(server_mods)
        with patch(
            "dstools.features.mod.legacy_v1.running_dst_processes", return_value=()
        ):
            detached = detach_mod_sync_junction(install, client_mods)
        assert detached.removed and detached.copied
        assert not os.path.isjunction(server_mods)
        assert not (server_mods / "stock.txt").exists()
        assert (server_mods / "client-root.txt").read_text(
            encoding="utf-8"
        ) == "client copy"
        assert (server_mods / "workshop-987654321" / "modinfo.lua").is_file()
        assert set(detached.copied_entries) == {"client-root.txt", "workshop-987654321"}

        # 复制阶段失败时不能先删联接；这是解除操作最重要的失败安全边界。
        failed_install = Path(tmp) / "failed_dedicated"
        failed_target = failed_install / "mods"
        _ensure_junction(failed_target, client_mods)
        with (
            patch(
                "dstools.features.mod.legacy_v1.running_dst_processes", return_value=()
            ),
            patch(
                "dstools.features.mod.sync.shutil.copytree",
                side_effect=OSError("forced copy failure"),
            ),
        ):
            failed = detach_mod_sync_junction(failed_install, client_mods)
        assert failed.errors and os.path.isjunction(failed_target)


def test_theme_set_theme():
    """每套主题必须提供与默认主题完全相同的颜色/字号键，切到任一主题都不能 KeyError。"""

    from dstools.shared import palettes

    base = set(palettes.THEMES["gray"])
    assert set(palettes.THEME_NAMES) <= set(palettes.THEMES)
    for name in palettes.THEME_NAMES:
        keys = set(palettes.THEMES[name])
        assert keys == base, f"{name}: 缺 {base - keys}，多 {keys - base}"


def test_world_reader_and_view_model():
    """世界 Lua 的读取状态、原子保存和 UI 无关视图模型必须可独立验证。"""

    from dstools.features.world.reader import (
        LeveldataStatus,
        WorldPreset,
        load_leveldata,
        save_leveldata,
    )
    from dstools.features.world.view_model import (
        WorldDisplayOverride,
        build_world_view_model,
    )

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        path = root / "leveldataoverride.lua"
        path.write_text(
            'return { id = "ENDLESS", name = "Endless", desc = "keep me", '
            'location = "forest", custom = { enabled = true }, '
            'overrides = { day = "default", autumn = "longseason", '
            'has_ocean = true, world_size_multi = 1.5 } }',
            encoding="utf-8",
        )
        result = load_leveldata(path)
        assert result.status == LeveldataStatus.OK and result.preset is not None
        preset = result.preset
        preset.overrides[0].value = "onlyday"
        save_leveldata(preset, path)
        reloaded = load_leveldata(path)
        assert reloaded.status == LeveldataStatus.OK and reloaded.preset is not None
        assert reloaded.preset.description == "keep me"
        assert reloaded.preset.raw["custom"]["enabled"] is True
        assert {item.key: item.value for item in reloaded.preset.overrides}[
            "day"
        ] == "onlyday"
        # 未改动的布尔/数字必须保持原类型，不能被写成字符串
        assert reloaded.preset.raw["overrides"]["has_ocean"] is True
        assert reloaded.preset.raw["overrides"]["world_size_multi"] == 1.5
        # 改过的值按 Mod 登记的全数字取值表写回 Lua 数字（世界设置页保存世界生成项时传入）
        changed = next(item for item in reloaded.preset.overrides if item.key == "world_size_multi")
        changed.value = "2"
        save_leveldata(reloaded.preset, path, {"world_size_multi": ["1.5", "2"]})
        assert load_leveldata(path).preset.raw["overrides"]["world_size_multi"] == 2

        # 本地存档：游戏读分片 save/shardindex 的 world.options.overrides，只替换这一段，其余原样保留
        from dstools.features.mod.shardindex import ShardIndexError
        from dstools.features.world.local_options import load_local_overrides, save_local_overrides
        shard_dir = root / "Master"
        (shard_dir / "save").mkdir(parents=True)
        index_file = shard_dir / "save" / "shardindex"
        index_file.write_bytes(
            b'KLEI     1 return {enabled_mods={["workshop-1"]={enabled=true}}, '
            b'world={options={id="SURVIVAL_TOGETHER", overrides={autumn="default", has_ocean=true}}}}'
        )
        local = load_local_overrides(shard_dir)
        local["autumn"] = "longseason"
        save_local_overrides(shard_dir, local)
        saved = index_file.read_bytes()
        assert saved.startswith(b"KLEI     1 ") and b'enabled_mods={["workshop-1"]={enabled=true}}' in saved
        assert load_local_overrides(shard_dir) == {"autumn": "longseason", "has_ocean": True}
        index_file.write_bytes(b"KLEI     1D" + saved[11:])
        try:
            save_local_overrides(shard_dir, local)
        except ShardIndexError:
            pass
        else:
            raise AssertionError("压缩格式的 shardindex 不能写回")
        assert not list(root.glob("*.tmp")), "原子写入完成后不应遗留临时文件"

        assert load_leveldata(root / "missing.lua").status == LeveldataStatus.MISSING
        invalid = root / "invalid.lua"
        invalid.write_text("return { overrides = {", encoding="utf-8")
        assert load_leveldata(invalid).status == LeveldataStatus.INVALID

    view = build_world_view_model(WorldPreset(location="forest"), {}, [])
    rows = [row for items in view.rules_by_category.values() for row in items]
    day = next(row for row in rows if row.key == "day")
    assert isinstance(day, WorldDisplayOverride) and day.persisted is False


def test_local_server_data_roundtrip():
    # 本地存档的房间设置在 Master shardindex 的 server 表：只替换这一段，表内未开放的键原样保留
    from dstools.features.cluster_config.local_server_data import (
        load_local_server_data, merge_local_values, save_local_server_data, validate_local_values,
    )
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "Master" / "save").mkdir(parents=True)
        index_file = root / "Master" / "save" / "shardindex"
        index_file.write_bytes(
            b'KLEI     1 return {enabled_mods={}, server={game_mode="survival", max_players=6, '
            b'name="old", password="", privacy_type=2, pvp=false}, world={options={}}}'
        )
        values = {("NETWORK", "cluster_name"): "新名字", ("GAMEPLAY", "max_players"): "4",
                  ("GAMEPLAY", "pvp"): True}
        assert validate_local_values(values) is None
        assert validate_local_values({("GAMEPLAY", "max_players"): "7"}) is not None
        save_local_server_data(root, merge_local_values(load_local_server_data(root), values))
        saved = index_file.read_bytes()
        assert saved.startswith(b"KLEI     1 ") and b"world={options={}}" in saved
        assert load_local_server_data(root) == {
            "game_mode": "survival", "max_players": 4, "name": "新名字", "password": "",
            "privacy_type": 2, "pvp": True,
        }


def test_world_catalog_layers_are_isolated():
    """原版目录与猪镇 Mod 覆盖层必须分离。"""

    from dstools.features.world.catalog_resolver import resolve_vanilla_settings
    from dstools.features.world.categories import FOREST_RULES_DICT, get_setting_info

    assert "specialevent" in FOREST_RULES_DICT
    for key in ("day", "basicresource_regrowth", "roads"):
        assert get_setting_info(key, "cave")[0] == "other"
    assert "specialevent" not in resolve_vanilla_settings("porkland", True)
    assert "day" in resolve_vanilla_settings("porkland", True)
    assert "butterfly" in resolve_vanilla_settings("porkland", True)
    assert get_setting_info("specialevent", "porkland")[0] == "other"
    assert get_setting_info("day", "porkland")[0] == "global"
    assert get_setting_info("butterfly", "porkland")[0] == "creatures"
    assert get_setting_info("season_start", "porkland")[0] == "other"
    assert get_setting_info("regrowth", "porkland")[0] == "other"


def test_world_creation_plan_and_atomic_writer():
    """创建层生成双世界目录，拒绝覆盖并可回读。"""
    from dstools.features.world.creation import (
        WorldCreationPlan,
        WorldShardPlan,
        create_world,
    )
    from dstools.features.world.reader import load_leveldata

    with tempfile.TemporaryDirectory() as td:
        plan = WorldCreationPlan(
            cluster_name="Cluster_Porkland_Test",
            mod_ids=frozenset({"3322803908"}),
            master=WorldShardPlan("porkland", "PORKLAND_DEFAULT", "猪镇", "危险丛林"),
            caves=WorldShardPlan("cave", "DST_CAVE", "洞穴", overrides={"task_set": "cave_default"}),
        )
        path = create_world(plan, Path(td))
        master = load_leveldata(path / "Master" / "leveldataoverride.lua").preset
        caves = load_leveldata(path / "Caves" / "leveldataoverride.lua").preset
        assert master and master.location == "porkland"
        assert caves and caves.location == "cave"
        assert (path / "cluster.ini").exists()
        mod_overrides = (path / "Master" / "modoverrides.lua").read_text(
            encoding="utf-8"
        )
        assert "workshop-3322803908" in mod_overrides and "enabled" in mod_overrides
        try:
            create_world(plan, Path(td))
        except FileExistsError:
            pass
        else:
            raise AssertionError("existing cluster must not be overwritten")


def test_mod_resolve_cache():
    """resolve_full_modinfo() 结果的磁盘缓存：按内容哈希与目录失效、格式版本校验与 ModConfigOption 的 JSON 往返。"""

    # 必须在导入 mod.cache 之前进入隔离目录：其 _CACHE_DIR 在导入时就已确定
    with _isolated_settings_dir():
        from dstools.features.mod.cache import _cache_path, load_cached_result, save_result
        from dstools.features.mod.parser import ModConfigOption

        workshop_id = "test-workshop-resolve-cache"
        with tempfile.TemporaryDirectory() as tmp:
            modinfo_path = Path(tmp) / "modinfo.lua"
            modinfo_path.write_text("name = 'x'", encoding="utf-8")

            assert load_cached_result(workshop_id, modinfo_path) is None

            result = {
                "name": "测试Mod",
                "config_options": [
                    ModConfigOption(name="opt1", label="选项1", default="a")
                ],
            }
            save_result(workshop_id, modinfo_path, result)
            cached = load_cached_result(workshop_id, modinfo_path)
            assert cached is not None and cached["name"] == "测试Mod"
            assert isinstance(cached["config_options"][0], ModConfigOption)
            assert cached["config_options"][0].name == "opt1"

            # 同步脚本（robocopy）保留源文件的旧时间：内容变了、修改时间却比缓存早，也必须失效
            modinfo_path.write_text("name = 'y'", encoding="utf-8")
            past = time.time() - 3600
            os.utime(modinfo_path, (past, past))
            assert load_cached_result(workshop_id, modinfo_path) is None

            # 同一 ID 换了目录（如 Steam/WeGame 各有一份）不能共用缓存
            save_result(workshop_id, modinfo_path, result)
            other = Path(tmp) / "other" / "modinfo.lua"
            other.parent.mkdir()
            other.write_text("name = 'y'", encoding="utf-8")
            assert load_cached_result(workshop_id, other) is None

            # 上一版按修改时间判断的缓存：不比 modinfo 旧时接管一次并补记哈希，免得升级后全部重跑沙箱
            legacy = {"name": "旧版Mod", "config_options": [], "_cache_format_version": 2}
            _cache_path(workshop_id).write_text(json.dumps(legacy), encoding="utf-8")
            newer = time.time() + 100
            os.utime(_cache_path(workshop_id), (newer, newer))
            assert load_cached_result(workshop_id, modinfo_path)["name"] == "旧版Mod"
            assert json.loads(_cache_path(workshop_id).read_text(encoding="utf-8"))["_cache_format_version"] == 3

            # 缺 _cache_format_version 的旧缓存必须失效
            _cache_path(workshop_id).write_text(
                json.dumps({"name": "旧格式测试Mod", "config_options": [{"name": "opt1", "label": "选项1", "default": "a"}]}),
                encoding="utf-8",
            )
            assert load_cached_result(workshop_id, modinfo_path) is None, (
                "没有 _cache_format_version 的旧格式缓存应该被当成失效"
            )


def test_mod_icon_cache_stamp():
    """图标缓存按源文件指纹命中：换了 .tex 但修改时间更旧（robocopy 保留源时间）也要失效。"""

    with _isolated_settings_dir():
        from dstools.features.mod.icons import _cache_dir_for, get_cached_mod_icon_path
        from dstools.features.mod.parser import ModInfo
        from dstools.models import Platform

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "modicon.xml").write_text("<Atlas/>", encoding="utf-8")
            tex = folder / "modicon.tex"
            tex.write_bytes(b"old")
            info = ModInfo(workshop_id="IconStampTest", icon="modicon.tex", icon_atlas="modicon.xml")
            cache_path = _cache_dir_for(Platform.STEAM) / "IconStampTest.png"
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(b"png")

            # 没有指纹的旧缓存按原规则接管并补记指纹
            assert get_cached_mod_icon_path(info, folder) == cache_path
            assert cache_path.with_suffix(".json").is_file()
            assert get_cached_mod_icon_path(info, folder) == cache_path

            tex.write_bytes(b"new!")
            past = time.time() - 3600
            os.utime(tex, (past, past))
            assert get_cached_mod_icon_path(info, folder) is None


def test_mod_version_resolution():
    """版本号必须来自完整成功执行后的最终值，失败时不采用中间值。"""

    from dstools.features.mod.local_version import (
        VERSION_CONFIRMED,
        VERSION_UNDECLARED,
        VERSION_UNRESOLVED,
        normalize_version_for_compare,
        normalize_version_result,
        resolve_local_mod_version,
    )
    from dstools.features.mod.sandbox import resolve_mod_versions
    from dstools.features.mod import version_cache

    result = resolve_mod_versions(
        'local prefix = "1."\nlocal title = "Dynamic Mod"\nname = title\n'
        'version = prefix .. "2.3"\n'
        'version = version .. "-final"\nversion_compatible = "1.2"',
        folder_name="workshop-123",
    )
    assert result == {
        "name": {"declared": True, "value": "Dynamic Mod"},
        "icon": {"declared": False},
        "icon_atlas": {"declared": False},
        "version": {"declared": True, "value": "1.2.3-final"},
        "version_compatible": {"declared": True, "value": "1.2"},
    }
    normalized = normalize_version_result(result)
    assert normalized.name == "Dynamic Mod"
    assert normalized.name_status == VERSION_CONFIRMED
    assert normalized.version == "1.2.3-final"
    assert normalized.status == VERSION_CONFIRMED
    assert normalized.version_compatible == "1.2"

    conditional = resolve_mod_versions(
        'version = folder_name == "workshop-123" and "workshop" or "local"',
        folder_name="workshop-123",
    )
    assert conditional["version"] == {"declared": True, "value": "workshop"}

    assert resolve_mod_versions('version = "temporary"\nmissing_engine_api()') is None
    assert normalize_version_result(None).status == VERSION_UNRESOLVED

    undeclared = resolve_mod_versions('name = "No Version"')
    undeclared_result = normalize_version_result(undeclared)
    assert undeclared_result.status == VERSION_UNDECLARED
    invalid = normalize_version_result(
        {
            "version": {"declared": True, "value": True},
            "version_compatible": {"declared": False},
        },
    )
    assert invalid.status == VERSION_UNRESOLVED

    fallback = normalize_version_result(
        {
            "version": {"declared": True, "value": " V1.2.3 "},
            "version_compatible": {"declared": False},
        },
    )
    assert fallback.compare_version == "v1.2.3"
    assert normalize_version_for_compare(" V1.2.3 ") == "v1.2.3"
    commented = normalize_version_result(
        {
            "version": {"declared": True, "value": "0.0.6  --版本"},
            "version_compatible": {"declared": False},
        },
    )
    assert commented.version == "0.0.6"

    with tempfile.TemporaryDirectory() as tmp:
        old_cache_dir = version_cache._CACHE_DIR
        version_cache._CACHE_DIR = Path(tmp) / "cache"
        try:
            first_dir = Path(tmp) / "source-a"
            second_dir = Path(tmp) / "source-b"
            first_dir.mkdir()
            second_dir.mkdir()
            first = first_dir / "modinfo.lua"
            second = second_dir / "modinfo.lua"
            first.write_text('version = "1"', encoding="utf-8")
            second.write_text('version = "1"', encoding="utf-8")
            cached_result = {
                "version": {"declared": True, "value": "1"},
                "version_compatible": {"declared": False},
            }
            version_cache.save_version_result(
                "workshop-123", first, "workshop-123", cached_result
            )
            assert (
                version_cache.load_version_result("workshop-123", first, "workshop-123")
                == cached_result
            )
            assert (
                version_cache.load_version_result(
                    "workshop-123", second, "workshop-123"
                )
                is None
            )
            first.write_text('version = "2"', encoding="utf-8")
            assert (
                version_cache.load_version_result("workshop-123", first, "workshop-123")
                is None
            )
            first.write_text(
                'version = "2"\nversion_compatible = "1"', encoding="utf-8"
            )
            local = resolve_local_mod_version("workshop-123", first_dir, "workshop-123")
            assert local.version == "2" and local.version_compatible == "1"
            assert local.status == VERSION_CONFIRMED
        finally:
            version_cache._CACHE_DIR = old_cache_dir


def test_chs_translation_uses_original_mod_info():
    """Chinese++ Pro 翻译文件按 gz_* 约定拿到原名/原简介/版本，拼出名称；变更原始信息后缓存失效。"""
    from dstools.features.mod import chs_translation
    from dstools.features.mod.parser import ModConfigOption

    with tempfile.TemporaryDirectory() as tmp:
        old_cache_dir = chs_translation._CACHE_DIR
        chs_translation._CACHE_DIR = Path(tmp) / "cache"
        try:
            path = Path(tmp) / "workshop-1.lua"
            path.write_text(
                'local n, d, v\n'
                'if gz_start then n, d, v = gz_modname, gz_description, gz_modversion\n'
                'else local m = KnownModIndex:InitializeModInfo("workshop-1") or {}\n'
                '  n, d, v = m.name or "", m.description or "", m.version or "" end\n'
                # 分组标题按原英文在原始配置项里找 section_N 键（同 3193922031 的翻译文件）
                'local sec\n'
                'for _, o in ipairs(gz_configuration_options or {}) do\n'
                '  if o.label == "Title" then sec = o.name end end\n'
                'return {name = n .. "(测试)", description = d:gsub("Hello", "你好"),\n'
                '  configuration_options = {{name = "ver", label = "Version: " .. v}, {name = sec, label = "标题"}}}',
                encoding="utf-8",
            )
            # 原名含引号和反斜杠，注入的 Lua 字面量必须正确转义
            result = chs_translation.resolve_translation(
                path, 'A "B" \\C', "Hello\nworld", "1.2",
                [ModConfigOption(name="section_1", label="Title", is_header=True)])
            assert result["name"] == 'A "B" \\C(测试)'
            assert result["description"] == "你好\nworld"
            assert result["configuration_options"][0]["label"] == "Version: 1.2"
            assert result["configuration_options"][1]["name"] == "section_1"
            # 缓存与原始信息绑定：版本变化后重新解析
            changed = chs_translation.resolve_translation(path, "X", "", "2.0")
            assert changed["name"] == "X(测试)"
            assert changed["configuration_options"][0]["label"] == "Version: 2.0"
        finally:
            chs_translation._CACHE_DIR = old_cache_dir


def test_damaged_bundle_skips_self_spawned_workers():
    """onefile 解压目录里的 base_library.zip 被清理后，沙箱不再起子进程（否则必弹启动器错误框）。"""
    import subprocess

    from dstools.features.mod import sandbox
    from dstools.shared import resource_paths

    spawned = []
    old_run = subprocess.run
    had_frozen = hasattr(sys, "frozen")
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "base_library.zip").write_bytes(b"")
        sys.frozen, sys._MEIPASS = True, tmp
        subprocess.run = lambda *a, **k: spawned.append(a) or old_run(*a, **k)
        try:
            assert not resource_paths.bundled_runtime_damaged()
            (Path(tmp) / "base_library.zip").unlink()
            assert resource_paths.bundled_runtime_damaged()
            assert sandbox.run_lua_snippet("return 1") is None
            assert spawned == []
        finally:
            subprocess.run = old_run
            del sys._MEIPASS
            if not had_frozen:
                del sys.frozen


def test_workshop_source_details_parser():
    """源端详情使用宽缓冲区读取，稳定字段偏移必须和 Steam SDK 一致。"""

    import struct
    from dstools.features.mod.workshop_api import (
        _parse_ugc_details_buffer,
        workshop_version_from_details,
    )

    raw = bytearray(32768)
    struct.pack_into("<Q", raw, 0, 3485293431)
    struct.pack_into("<i", raw, 8, 1)
    struct.pack_into("<I", raw, 16, 245850)
    struct.pack_into("<I", raw, 20, 322330)
    raw[24 : 24 + len("测试 Mod".encode())] = "测试 Mod".encode()
    struct.pack_into("<I", raw, 8168, 100)
    struct.pack_into("<I", raw, 8172, 200)
    tags = b"server_only,gameplay,version:1.4.3"
    raw[8187 : 8187 + len(tags)] = tags
    struct.pack_into("<Q", raw, 9216, 123456)
    raw[9232 : 9232 + len(b"content.zip")] = b"content.zip"
    struct.pack_into("<i", raw, 9492, 654321)
    details = _parse_ugc_details_buffer(bytes(raw))
    assert details.workshop_id == 3485293431
    assert details.title == "测试 Mod"
    assert details.creator_app_id == 245850 and details.consumer_app_id == 322330
    assert details.time_created == 100 and details.time_updated == 200
    assert details.tags == ("server_only", "gameplay", "version:1.4.3")
    assert workshop_version_from_details(details) == "1.4.3"
    assert details.content_handle == 123456
    assert details.filename == "content.zip" and details.file_size == 654321


def test_workshop_status_evidence_priority():
    """实际目录、版本和 Manifest 证据必须覆盖 Steam 的陈旧 Installed 位。"""

    from dstools.features.mod.local_version import LocalModVersion, VERSION_CONFIRMED
    from dstools.features.mod.workshop_api import (
        WorkshopInstallInfo,
        WorkshopItemDetails,
        WorkshopItemState,
    )
    from dstools.features.mod.workshop_status import (
        WorkshopModEvidence,
        WorkshopModState,
        evaluate_workshop_status,
    )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        missing = root / "3485293431"
        stale = WorkshopModEvidence(
            workshop_id=3485293431,
            steam_state=WorkshopItemState(5),
            install_info=WorkshopInstallInfo(missing, 100, 1),
        )
        status = evaluate_workshop_status(stale)
        assert status.state == WorkshopModState.MISSING
        assert "Steam 仍标记为已安装" in status.reasons[0]

        installed = root / "installed"
        installed.mkdir()
        (installed / "modinfo.lua").write_text('version = "V1.2.3"', encoding="utf-8")
        version = LocalModVersion(
            "V1.2.3", VERSION_CONFIRMED, "", "undeclared", "sandbox"
        )
        current = WorkshopModEvidence(
            workshop_id=1,
            steam_state=WorkshopItemState(5),
            install_info=WorkshopInstallInfo(installed, 100, 1),
            source_version=version,
            source_details=WorkshopItemDetails(1, 1, time_updated=1),
        )
        assert evaluate_workshop_status(current).state == WorkshopModState.CURRENT

        suspected = WorkshopModEvidence(
            **{**current.__dict__, "source_details": None}
        )
        suspected_status = evaluate_workshop_status(suspected)
        assert suspected_status.state == WorkshopModState.SUSPECTED_OUTDATED
        assert suspected_status.needs_action and suspected_status.can_update

        steam_update = WorkshopModEvidence(
            **{**current.__dict__, "steam_state": WorkshopItemState(13)}
        )
        assert (
            evaluate_workshop_status(steam_update).state
            == WorkshopModState.UPDATE_AVAILABLE
        )
        cached_update = WorkshopModEvidence(
            **{**current.__dict__, "remote_version": "1.2.2"}
        )
        cached_status = evaluate_workshop_status(cached_update)
        assert cached_status.state == WorkshopModState.UPDATE_AVAILABLE
        assert cached_status.remote_version == "1.2.2"
        exact_compare = WorkshopModEvidence(
            **{**current.__dict__, "remote_version": "1.2.3"}
        )
        assert (
            evaluate_workshop_status(exact_compare).state
            == WorkshopModState.UPDATE_AVAILABLE
        ), "不能擅自忽略本地 V 前缀"
        exact_current = WorkshopModEvidence(
            **{**current.__dict__, "remote_version": "V1.2.3"}
        )
        assert evaluate_workshop_status(exact_current).state == WorkshopModState.CURRENT
        case_only = WorkshopModEvidence(
            **{**current.__dict__, "remote_version": "v1.2.3"}
        )
        assert evaluate_workshop_status(case_only).state == WorkshopModState.CURRENT, (
            "Steam version 标签会转小写，纯大小写差异不能误报更新"
        )
        live_remote = WorkshopModEvidence(
            **{
                **current.__dict__,
                "source_details": WorkshopItemDetails(
                    1,
                    1,
                    time_updated=1,
                    tags=("all_clients_require_mod", "version:V1.2.3"),
                ),
                "remote_version": "V1.2.3",
            }
        )
        live_status = evaluate_workshop_status(live_remote)
        assert live_status.state == WorkshopModState.CURRENT
        assert live_status.remote_version == "V1.2.3"
        active_update = WorkshopModEvidence(
            **{
                **current.__dict__,
                "active_path": installed,
                "active_version": LocalModVersion(
                    "1.2.2", VERSION_CONFIRMED, "", "undeclared", "sandbox"
                ),
            }
        )
        assert (
            evaluate_workshop_status(active_update).state
            == WorkshopModState.UPDATE_AVAILABLE
        )

        corrupt = WorkshopModEvidence(
            **{
                **current.__dict__,
                "manifest_valid": False,
                "manifest_error": "缺少 scripts/main.lua",
            }
        )
        corrupt_status = evaluate_workshop_status(corrupt)
        assert corrupt_status.state == WorkshopModState.CURRENT
        assert "缺少 scripts/main.lua" in corrupt_status.reasons

        legacy_file = root / "521637598935453868_legacy.bin"
        legacy_file.write_bytes(b"legacy workshop payload")
        legacy = WorkshopModEvidence(
            workshop_id=463952377,
            steam_state=WorkshopItemState(7),
            install_info=WorkshopInstallInfo(
                legacy_file, legacy_file.stat().st_size, 1
            ),
            legacy_package_valid=True,
            legacy_package_version=LocalModVersion(
                "1.0", VERSION_CONFIRMED, "", "undeclared", "legacy_package"
            ),
        )
        legacy_status = evaluate_workshop_status(legacy)
        assert legacy_status.state == WorkshopModState.LEGACY_PACKAGE_READY
        assert legacy_status.can_update
        assert "首次加载" in legacy_status.reasons[0]
        legacy_runtime = root / "workshop-463952377"
        legacy_runtime.mkdir()
        (legacy_runtime / "modinfo.lua").write_text('version = "1.0"', encoding="utf-8")
        legacy_complete = WorkshopModEvidence(
            **{
                **legacy.__dict__,
                "discovered_path": legacy_runtime,
                "source_version": LocalModVersion(
                    "1.0", VERSION_CONFIRMED, "", "undeclared", "sandbox"
                ),
                "legacy_package_valid": True,
            }
        )
        assert (
            evaluate_workshop_status(legacy_complete).state == WorkshopModState.CURRENT
        )
        stale_remote_legacy = WorkshopModEvidence(
            **{
                **legacy_complete.__dict__,
                "remote_version": "9.9",
            }
        )
        stale_remote_status = evaluate_workshop_status(stale_remote_legacy)
        assert stale_remote_status.state == WorkshopModState.CURRENT
        assert stale_remote_status.remote_version == "1.0"
        runtime_mismatch = WorkshopModEvidence(
            **{
                **legacy_complete.__dict__,
                "source_version": LocalModVersion(
                    version="1.2", status=VERSION_CONFIRMED
                ),
            }
        )
        runtime_mismatch_status = evaluate_workshop_status(runtime_mismatch)
        assert runtime_mismatch_status.state == WorkshopModState.UPDATE_AVAILABLE
        assert runtime_mismatch_status.update_expected_version == "1.0"
        steam_update = WorkshopModEvidence(
            **{
                **legacy_complete.__dict__,
                "steam_state": WorkshopItemState(15),
            }
        )
        steam_update_status = evaluate_workshop_status(steam_update)
        assert steam_update_status.state == WorkshopModState.UPDATE_AVAILABLE
        assert steam_update_status.remote_version == "1.0"
        assert steam_update_status.update_expected_version == ""
        legacy_server_missing = WorkshopModEvidence(
            **{
                **legacy_complete.__dict__,
                "active_path": root / "server-mods" / "workshop-463952377",
                "active_version": LocalModVersion(),
            }
        )
        server_missing_status = evaluate_workshop_status(legacy_server_missing)
        assert server_missing_status.state == WorkshopModState.LEGACY_PACKAGE_READY
        legacy_modified = WorkshopModEvidence(
            **{
                **legacy_complete.__dict__,
                "source_version": LocalModVersion(
                    "1.1", VERSION_CONFIRMED, "", "undeclared", "sandbox"
                ),
                "legacy_package_version": LocalModVersion(
                    "1.0", VERSION_CONFIRMED, "", "undeclared", "legacy_package"
                ),
            }
        )
        modified_status = evaluate_workshop_status(legacy_modified)
        assert modified_status.state == WorkshopModState.UPDATE_AVAILABLE
        assert modified_status.remote_version == "1.0"
        assert "Legacy 下载包不同" in modified_status.reasons[0]

        local_only = WorkshopModEvidence(
            workshop_id=2,
            steam_state=WorkshopItemState(0),
            discovered_path=installed,
            source_version=version,
        )
        local_status = evaluate_workshop_status(local_only)
        assert local_status.state == WorkshopModState.LOCAL_FILES
        assert local_status.local_path == installed

        empty_folder = root / "empty-workshop-folder"
        empty_folder.mkdir()
        empty_leftover = WorkshopModEvidence(
            workshop_id=3,
            steam_state=WorkshopItemState(0),
            discovered_path=empty_folder,
        )
        assert (
            evaluate_workshop_status(empty_leftover).state
            == WorkshopModState.NOT_INSTALLED
        )

        unavailable = WorkshopModEvidence(
            workshop_id=2428854303,
            steam_state=WorkshopItemState(0),
            source_details=WorkshopItemDetails(2428854303, 15),
        )
        unavailable_status = evaluate_workshop_status(unavailable)
        assert unavailable_status.state == WorkshopModState.SOURCE_UNAVAILABLE
        assert "EResult=15" in unavailable_status.reasons[0]

        # 当前 Steam 账号未订阅、但清单显示本机其他账号订阅：不是残留，不能清理（删了会被重新下载）
        other_account = WorkshopModEvidence(
            workshop_id=4,
            steam_state=WorkshopItemState(0),
            discovered_path=installed,
            source_version=version,
            workshop_content_path=installed,
            acf_subscribed=True,
        )
        other_status = evaluate_workshop_status(other_account)
        assert other_status.state == WorkshopModState.SUBSCRIBED_BY_OTHER_ACCOUNT
        assert not other_status.can_cleanup_residual
        assert evaluate_workshop_status(
            WorkshopModEvidence(**{**other_account.__dict__, "acf_subscribed": False})
        ).can_cleanup_residual


def test_workshop_snapshot_uses_one_steam_session():
    """组合刷新必须只初始化一次 SteamAPI，标题失败不能丢本地证据。"""

    import dstools.features.mod.workshop_api as workshop_api

    opened = []

    class FakeSession:
        def __init__(self, dll_path):
            opened.append(dll_path)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def item_state(self, workshop_id):
            return workshop_api.WorkshopItemState(5)

        def subscribed_item_ids(self):
            return [11, 22, 33]

        def item_install_details(self, workshop_id):
            return workshop_api.WorkshopInstallInfo(
                Path(f"C:/workshop/{workshop_id}"), 123, 456
            )

        def query_item_details_batches(self, workshop_ids, timeout=20.0):
            return [
                workshop_api.WorkshopItemDetails(
                    workshop_id=item, result=1, title=f"Mod {item}"
                )
                for item in workshop_ids
            ]

    original = workshop_api.SteamWorkshopSession
    with tempfile.TemporaryDirectory() as tmp:
        dll = Path(tmp) / "steam_api64.dll"
        dll.write_bytes(b"fake")
        try:
            workshop_api.SteamWorkshopSession = FakeSession
            states, installs, details = (
                workshop_api._get_workshop_item_snapshot_in_process(
                    [11, 22], detail_ids=[22], dll_path=dll
                )
            )
        finally:
            workshop_api.SteamWorkshopSession = original
    assert len(opened) == 1
    assert set(states) == {11, 22} and set(installs) == {11, 22}
    assert set(details) == {22} and details[22].title == "Mod 22"

    opened.clear()
    with tempfile.TemporaryDirectory() as tmp:
        dll = Path(tmp) / "steam_api64.dll"
        dll.write_bytes(b"fake")
        try:
            workshop_api.SteamWorkshopSession = FakeSession
            states, installs, details = (
                workshop_api._get_workshop_item_snapshot_in_process(
                    [11, 22], dll_path=dll, include_subscribed=True
                )
            )
        finally:
            workshop_api.SteamWorkshopSession = original
    assert len(opened) == 1
    assert set(states) == {11, 22, 33} and set(installs) == {11, 22, 33}
    assert set(details) == {33} and details[33].title == "Mod 33"

    class FailingTitleSession(FakeSession):
        def query_item_details_batches(self, workshop_ids, timeout=20.0):
            raise TimeoutError("模拟标题查询超时")

    opened.clear()
    with tempfile.TemporaryDirectory() as tmp:
        dll = Path(tmp) / "steam_api64.dll"
        dll.write_bytes(b"fake")
        try:
            workshop_api.SteamWorkshopSession = FailingTitleSession
            states, installs, details = (
                workshop_api._get_workshop_item_snapshot_in_process(
                    [11], detail_ids=[11], dll_path=dll
                )
            )
        finally:
            workshop_api.SteamWorkshopSession = original
    assert len(opened) == 1
    assert set(states) == {11} and set(installs) == {11} and details == {}


def test_dst_mod_manifest_verification():
    """MNFS 路径哈希必须识别缺失文件，同时允许 Mod 运行时产生额外文件。"""

    import struct
    from dstools.features.mod.workshop_manifest import (
        ManifestFormatError,
        load_mod_manifest,
        parse_mod_manifest_bytes,
        sdbm_path_hash,
        verify_mod_manifest,
    )

    assert sdbm_path_hash("modinfo.lua") == 0xCD796EDA
    assert sdbm_path_hash("scripts/components/smart_minisign.lua") == 0x11E699C6
    assert sdbm_path_hash("修改者指南.txt") == 0x847C5105

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mod = root / "workshop-123"
        scripts = mod / "scripts"
        scripts.mkdir(parents=True)
        (mod / "modinfo.lua").write_text('version = "1"', encoding="utf-8")
        (scripts / "main.lua").write_text("return true", encoding="utf-8")
        hashes = (
            sdbm_path_hash("modinfo.lua"),
            sdbm_path_hash("scripts/main.lua"),
            sdbm_path_hash("mod.manifest"),
        )
        (mod / "mod.manifest").write_bytes(
            struct.pack("<4sII3I", b"MNFS", 1, 3, *hashes)
        )
        parsed = load_mod_manifest(mod / "mod.manifest")
        assert parsed.path_hashes == hashes
        assert verify_mod_manifest(mod).valid is True

        (mod / "runtime-cache.txt").write_text("extra", encoding="utf-8")
        assert verify_mod_manifest(mod).valid is True
        (scripts / "main.lua").write_text("return false", encoding="utf-8")
        assert verify_mod_manifest(mod).valid is True, (
            "MNFS 只保存路径哈希，不能假装检测内容修改"
        )
        (scripts / "main.lua").unlink()
        missing = verify_mod_manifest(mod)
        assert missing.valid is False and len(missing.missing_hashes) == 1

        try:
            parse_mod_manifest_bytes(b"BAD!")
            raise AssertionError("损坏的 Manifest 不应解析成功")
        except ManifestFormatError:
            pass


def test_workshop_download_precheck_uses_physical_files():
    """Steam Installed 缓存不能掩盖被删除或损坏的真实 Mod 目录。"""

    import struct
    from dstools.features.mod.workshop_api import (
        SteamWorkshopSession,
        WorkshopItemDetails,
        WorkshopItemState,
        validate_workshop_install,
        workshop_source_error,
    )

    assert "可能已下架" in workshop_source_error(WorkshopItemDetails(2428854303, 15))

    class FakeDll:
        def __init__(self):
            self.download_calls = []

        def SteamAPI_ISteamUGC_DownloadItem(self, ugc, workshop_id, high_priority):
            self.download_calls.append((workshop_id, high_priority))
            return True

    class FakeSession:
        ugc = object()

        def __init__(self, path):
            self.path = path
            self.dll = FakeDll()

        def _ensure_started(self):
            pass

        def item_state(self, workshop_id):
            return WorkshopItemState(5)

        def item_install_info(self, workshop_id):
            return self.path

        def item_install_details(self, workshop_id):
            from dstools.features.mod.workshop_api import WorkshopInstallInfo

            return WorkshopInstallInfo(self.path, 0, 0)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        valid = root / "valid"
        valid.mkdir()
        (valid / "modinfo.lua").write_text('version = "1"', encoding="utf-8")
        assert validate_workshop_install(valid).valid

        (valid / "mod.manifest").write_bytes(
            struct.pack("<4sIII", b"MNFS", 1, 1, 0x12345678)
        )
        manifest_warning = validate_workshop_install(valid)
        assert manifest_warning.valid and manifest_warning.warning

        legacy = root / "123_legacy.bin"
        import zipfile

        with zipfile.ZipFile(legacy, "w") as package:
            package.writestr("modinfo.lua", 'version = "1"')
        assert validate_workshop_install(legacy, legacy_item=True).valid
        assert not validate_workshop_install(legacy).valid

        current_session = FakeSession(valid)
        current = SteamWorkshopSession.download_item(current_session, 123)
        assert current.completed and current.up_to_date
        assert not current_session.dll.download_calls

        missing_session = FakeSession(root / "deleted")
        repair = SteamWorkshopSession.download_item(missing_session, 123)
        assert repair.accepted and not repair.completed
        assert repair.details["repair"] is True
        assert missing_session.dll.download_calls

        force_path = root / "123"
        force_path.mkdir()
        force_modinfo = force_path / "modinfo.lua"
        force_modinfo.write_text('version = "modified"', encoding="utf-8")
        force_session = FakeSession(force_path)
        forced = SteamWorkshopSession.download_item(
            force_session, 123, expected_version="official"
        )
        assert forced.accepted and forced.details["version_repair"] is True
        backup = Path(forced.details["forced_modinfo_backup"])
        assert backup.is_file() and not force_modinfo.exists()
        SteamWorkshopSession._finish_forced_version_repair(forced, success=False)
        assert force_modinfo.read_text(encoding="utf-8") == 'version = "modified"'
        assert not backup.exists()

        redownload_root = root / "322330"
        redownload_path = redownload_root / "123"
        redownload_path.mkdir(parents=True)
        (redownload_path / "modinfo.lua").write_text('version = "old"', encoding="utf-8")
        redownload_session = FakeSession(redownload_path)
        redownload = SteamWorkshopSession.download_item(
            redownload_session, 123, force_redownload=True
        )
        assert redownload.accepted and redownload.details["forced_redownload"] is True
        assert not redownload_path.exists()
        assert redownload_session.dll.download_calls


def test_backup_manager_restore_clears_stale_slots():
    """restore_backup() 必须先清空会被覆盖的项再解压，否则比备份更新的存档槽会残留并被游戏加载。"""

    with tempfile.TemporaryDirectory() as tmp:
        cluster = Path(tmp) / "Cluster_1"
        sess = cluster / "Master" / "save" / "session" / "ABCDEF0123456789"
        sess.mkdir(parents=True)
        (sess / "0000000001").write_text("old_slot_data")
        (cluster / "Master" / "server.ini").write_text("[NETWORK]\nserver_port=1\n")
        (cluster / "Master" / "modoverrides.lua").write_text("return {}")
        (cluster / "cluster.ini").write_text("[GAMEPLAY]\nmax_players=6\n")

        backup_zip = create_backup(cluster)

        # 模拟备份之后又产生了更新的存档槽位。
        (sess / "0000000002").write_text("newer_slot_after_backup")
        assert sorted(p.name for p in sess.iterdir()) == ["0000000001", "0000000002"]

        restore_backup(cluster, backup_zip)
        remaining = sorted(p.name for p in sess.iterdir())
        assert remaining == ["0000000001"], (
            f"应该只剩备份里的旧槽位，实际是 {remaining}"
        )

        assert (
            cluster / "Master" / "server.ini"
        ).read_text() == "[NETWORK]\nserver_port=1\n"


def test_reset_worlds_only_removes_sessions():
    """离线重置世界只删各世界 save/session/ 下的会话，shardindex 与配置文件保留（同官方 ShardIndex:Delete(cb, true)）。"""
    from dstools.features.save_browser.world_reset import reset_worlds, shards_with_sessions

    with tempfile.TemporaryDirectory() as tmp:
        cluster = Path(tmp) / "Cluster_1"
        for shard, session_id in (("Master", "48AA04095102666C"), ("Caves", "07803E27BB6F0C05")):
            save = cluster / shard / "save"
            (save / "session" / session_id / "KU_test_").mkdir(parents=True)
            (save / "session" / session_id / "0000000002").write_text("slot")
            (save / "shardindex").write_text("index")
            (cluster / shard / "server.ini").write_text("[SHARD]\n")
            (cluster / shard / "leveldataoverride.lua").write_text("return {}")
        (cluster / "Empty" / "save" / "session").mkdir(parents=True)  # 还没生成过世界的世界
        (cluster / "Empty" / "server.ini").write_text("[SHARD]\n")

        assert shards_with_sessions(cluster) == ["Caves", "Master"]
        assert reset_worlds(cluster) == ["Caves", "Master"]
        assert shards_with_sessions(cluster) == []
        for shard in ("Master", "Caves"):
            assert not any((cluster / shard / "save" / "session").iterdir())
            assert (cluster / shard / "save" / "shardindex").read_text() == "index"
            assert (cluster / shard / "leveldataoverride.lua").exists()


def test_save_bundle_contains_complete_cluster():
    """分享包应保留存档根目录，并包含备份功能会主动跳过的日志等文件。"""

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cluster = root / "Cluster_Share"
        session = cluster / "Master" / "save" / "session" / "ABC"
        empty_dir = cluster / "Caves" / "save" / "empty"
        session.mkdir(parents=True)
        empty_dir.mkdir(parents=True)
        (cluster / "cluster.ini").write_text("[NETWORK]\ncluster_name=Share\n")
        (cluster / "cluster_token.txt").write_text("secret-token")
        (cluster / "Master" / "server_log.txt").write_text("complete log")
        (session / "0000000001").write_bytes(b"save data")

        bundle = create_save_bundle(cluster, root / "exports")
        assert bundle.is_file()
        with zipfile.ZipFile(bundle) as archive:
            names = set(archive.namelist())
            assert "Cluster_Share/cluster.ini" in names
            assert "Cluster_Share/cluster_token.txt" in names
            assert "Cluster_Share/Master/server_log.txt" in names
            assert "Cluster_Share/Master/save/session/ABC/0000000001" in names
            assert "Cluster_Share/Caves/save/empty/" in names
            assert archive.read("Cluster_Share/Master/server_log.txt") == b"complete log"


def test_backup_manager_prune_retention_boundary():
    """备份超过保留份数时删除最旧的（手工构造时间戳不同的旧备份，再用一次真实 create_backup() 触发裁剪）。"""

    with _isolated_settings_dir():
        # 保留份数的合法范围是 5~99（见 app_settings.set_backup_retention
        # 的 clamp）。
        set_backup_retention(5)
        with tempfile.TemporaryDirectory() as tmp:
            cluster = Path(tmp) / "Cluster_2"
            cluster.mkdir(parents=True)
            (cluster / "cluster.ini").write_text("[GAMEPLAY]\nmax_players=4\n")

            dest = backup_dir(cluster)  # 跟存档同级的统一备份目录，不是存档目录自己内部
            dest.mkdir(parents=True)
            for i in range(1, 8):
                (dest / f"Cluster_2_2026010{i}_000000.zip").write_bytes(b"")
            kept = dest / "Cluster_2_20250101_000000_keep.zip"  # 比所有自动备份都旧的手动类备份
            kept.write_bytes(b"")

            newest = create_backup(cluster)  # 第 8 份，真实时间戳，必然是最新的
            backups = list_backups(cluster)
            assert len(backups) == 6, f"应该保留 5 份自动备份加 1 份手动类备份，实际 {len(backups)} 份"
            assert kept in backups, "带 _keep 标记的备份不参与自动清理"

            assert backups[0] == newest, "最新的一份必须排在最前面"
            assert backups == sorted(backups, key=lambda p: p.name, reverse=True)

            # 定时备份：存档没变就不需要再备份，之后任意一个文件被改写才需要
            assert not changed_since_last_backup(cluster)
            later = newest.stat().st_mtime + 5
            os.utime(cluster / "cluster.ini", (later, later))
            assert changed_since_last_backup(cluster)
            assert create_backup(cluster, keep=True).name.endswith("_keep.zip")


def test_backfill_cluster_defaults_only_fills_missing():
    """backfill_cluster_defaults() 只补缺失字段，不能覆盖已有值。"""

    config = ClusterConfig(
        gameplay={"vote_enabled": False}, network={}, misc={}, shard={}, steam={}
    )
    backfill_cluster_defaults(config)

    assert config.gameplay["vote_enabled"] is False, (
        "已经显式设置的值不应该被默认值覆盖"
    )

    assert config.network["tick_rate"] == 15, "缺失的字段应该被补上官方默认值"
    assert config.misc["max_snapshots"] == 6

    # 用户拿真实存档手工核对过一轮之后新补的默认值（见 reference/带注释
    # 版本的cluster.ini）——顺带确认 STEAM 这个新分区也会被正确回填。
    assert config.gameplay["pvp"] is False
    assert config.gameplay["pause_when_empty"] is True
    assert config.network["cluster_name"] == "[Host]'s World"
    assert config.network["cluster_description"] == ""
    assert config.network["cluster_password"] == ""
    assert config.network["cluster_language"] == "en"
    assert config.misc["console_enabled"] is True
    assert config.steam["steam_group_only"] is False
    assert config.steam["steam_group_admins"] is False

    # shard_enabled=true 时游戏生成的四项被删除后要补上官方默认值
    assert config.shard["bind_ip"] == "127.0.0.1"
    assert config.shard["master_ip"] == "127.0.0.1"
    assert config.shard["master_port"] == 10888
    assert config.shard["cluster_key"] == "defaultPass"

    # 没有确认默认值的字段补空字符串，保证配置页不缺行
    assert config.gameplay["game_mode"] == ""
    assert config.gameplay["max_players"] == ""
    assert config.network["cluster_cloud_id"] == ""

    # 空字符串等同于"没有"，也要被当成缺失补上默认值——用户明确要求"值
    # 为空也用默认值"，不是只处理 key 整个不存在的情况。
    config2 = ClusterConfig(
        gameplay={}, network={"cluster_name": ""}, misc={}, shard={}, steam={}
    )
    backfill_cluster_defaults(config2)
    assert config2.network["cluster_name"] == "[Host]'s World", (
        "空字符串也应该被当成缺失，补上默认值"
    )


def test_cluster_ini_steam_section_roundtrip():
    """cluster.ini 的 [STEAM] 分区必须解析并原样写回（曾在保存时被整段吞掉）。"""

    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "cluster.ini"
        path.write_text(
            "[GAMEPLAY]\nmax_players=8\n\n"
            "[STEAM]\nsteam_group_only=true\nsteam_group_id=123456\nsteam_group_admins=false\n",
            encoding="utf-8",
        )
        config = parse_cluster_ini(path)
        assert config.steam.get("steam_group_only") is True
        assert config.steam.get("steam_group_id") == 123456
        assert config.steam.get("steam_group_admins") is False

        write_cluster_ini(config, path)
        reloaded = parse_cluster_ini(path)
        assert reloaded.steam.get("steam_group_only") is True
        assert reloaded.steam.get("steam_group_id") == 123456
        assert reloaded.gameplay.get("max_players") == 8, (
            "保存 [STEAM] 的同时不能弄丢其它分区"
        )


def test_sakura_frp_tunnel_matching():
    """樱花隧道名：格式始终合法（3-20 位字母数字下划线）、同输入确定性一致，且 source/platform 参与哈希
    （否则复制出的同名存档或 Steam/WeGame 同名存档会互相冒充映射状态）。"""

    name = sanitize_tunnel_name("Cluster_1", "Master", "server", "steam")
    assert 3 <= len(name) <= 20, f"隧道名长度必须在 3-20 之间: {name}"
    assert all(c.isalnum() or c == "_" for c in name), (
        f"隧道名只能是字母数字和下划线: {name}"
    )

    assert sanitize_tunnel_name("Cluster_1", "Master", "server", "steam") == name, (
        "同样的输入应该每次都算出同一个名字"
    )
    assert sanitize_tunnel_name("Cluster_1", "Caves", "server", "steam") != name, (
        "不同世界应该算出不同的名字"
    )

    assert sanitize_tunnel_name("Cluster_1", "Master", "local", "steam") != name, (
        "同名存档不同来源（本地 vs 服务器）不应该撞名"
    )
    assert sanitize_tunnel_name("Cluster_1", "Master", "server", "wegame") != name, (
        "同名存档不同平台（Steam vs WeGame）不应该撞名"
    )

    caves_name = sanitize_tunnel_name("Cluster_1", "Caves", "server", "steam")
    tunnels = [
        {"id": 1, "name": name, "remote": "12345"},
        {"id": 2, "name": caves_name, "remote": "12346"},
        {"id": 3, "name": "someone_elses_tunnel", "remote": "8080"},
    ]
    found = find_dstcamp_tunnel(tunnels, "Cluster_1", "Master", "server", "steam")
    assert found is not None and found["id"] == 1, "应该按名字匹配到对应世界的隧道"

    assert (
        find_dstcamp_tunnel(tunnels, "Cluster_1", "Cave2", "server", "steam") is None
    ), "不存在的世界不应该匹配到任何隧道"
    assert (
        find_dstcamp_tunnel(tunnels, "Cluster_1", "Master", "local", "steam") is None
    ), "同名本地存档不应该匹配到服务器存档的隧道"


def test_sakura_server_port_rewrite():
    """ "开启樱花映射"最关键的一步：把樱花分配的远程端口回写进这个世界自
    己的 server.ini。这里只测这一步的读-改-写本身，不牵扯真实网络调用。"""

    with tempfile.TemporaryDirectory() as tmp:
        shard_dir = Path(tmp) / "Master"
        shard_dir.mkdir(parents=True)
        (shard_dir / "server.ini").write_text("[NETWORK]\nserver_port=10999\n")

        config = load_shard_config(shard_dir)
        assert get_shard_option(config, "NETWORK", "server_port") == 10999

        set_shard_option(config, "NETWORK", "server_port", 23456)
        save_shard_config(config, shard_dir)

        reloaded = load_shard_config(shard_dir)
        assert get_shard_option(reloaded, "NETWORK", "server_port") == 23456, (
            "回写的端口应该能重新读回来"
        )






@contextlib.contextmanager
def _fake_workshop_dir(
    root: Path,
    subscribed_ids: list[str],
    with_injector_files: bool = False,
    mod_version: str | None = None,
):
    """伪造 Workshop 目录（content/322330/<id>/modinfo.lua），替换 luajit_injector 持有的 find_workshop_dir 引用。

    root 由调用方提供，便于与伪造的专服安装目录放在同一个 Steam 库下。with_injector_files 时按新版布局
    放入 Winmm.dll（bin64/windows/）与 Injector.dll、deps/；mod_version 写入 modinfo.lua 的 version。"""
    import dstools.features.local_service.luajit_injector as lj

    workshop_dir = root / "steamapps" / "workshop" / "content" / "322330"
    for wid in subscribed_ids:
        d = workshop_dir / wid
        d.mkdir(
            parents=True, exist_ok=True
        )  # 允许同一个 root 反复调用，模拟 Steam 原地更新订阅内容
        lines = ["name = 'test'"]
        if wid == WORKSHOP_ID and mod_version is not None:
            lines.append(f'version = "{mod_version}"')
        (d / "modinfo.lua").write_text("\n".join(lines), encoding="utf-8")
        if with_injector_files and wid == WORKSHOP_ID:
            bin64_win = d / "bin64" / "windows"
            bin64_win.mkdir(parents=True, exist_ok=True)
            (bin64_win / "Winmm.dll").write_bytes(b"fake winmm")
            (d / "Injector.dll").write_bytes(b"fake injector")
            deps = d / "deps"
            deps.mkdir(parents=True, exist_ok=True)
            (deps / "lua_helper.dll").write_bytes(b"fake nested dependency")

    original = lj.find_workshop_dir
    lj.find_workshop_dir = lambda: workshop_dir
    try:
        yield workshop_dir
    finally:
        lj.find_workshop_dir = original


def _make_fake_install_dir(root: Path, build_id: str | None = None) -> Path:
    """伪造 <root>/steamapps/common/<产品名>/ 安装目录，可选写入 version.txt。"""
    install_dir = (
        root / "steamapps" / "common" / "Don't Starve Together Dedicated Server"
    )
    install_dir.mkdir(parents=True)
    if build_id is not None:
        (install_dir / "version.txt").write_text(f"{build_id}\n", encoding="utf-8")
    return install_dir


def test_luajit_runtime_requires_toolset_version_of_dlls():
    """运行库版本不能低于编译配套 DLL 的工具集：读 PE 链接器版本取最高值，读不到时用 14.40 兜底。"""
    import struct
    from unittest.mock import patch

    from dstools.features.local_service import luajit_runtime

    def fake_pe(path: Path, major: int, minor: int) -> Path:
        header = bytearray(512)
        struct.pack_into("<I", header, 0x3C, 0x80)
        header[0x80:0x84] = b"PE\0\0"
        struct.pack_into("<HBB", header, 0x80 + 24, 0x20B, major, minor)
        path.write_bytes(bytes(header))
        return path

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        dlls = [fake_pe(root / "a.dll", 14, 44), fake_pe(root / "b.dll", 14, 36)]
        assert luajit_runtime.required_version(dlls) == (14, 44)
        assert luajit_runtime.required_version([root / "missing.dll"]) == (14, 40)
        for installed, expected in (((14, 50), True), ((14, 44), True), ((14, 42), False), (None, False)):
            with patch.object(luajit_runtime, "installed_version", return_value=installed):
                assert luajit_runtime.is_satisfied(dlls) is expected, installed


def test_luajit_game_bin64_install():
    """游戏专服按作者方式：Winmm.dll 直接放进游戏 bin64（与客户端共用），不建 luajit 副本。"""
    from unittest.mock import patch

    import dstools.features.local_service.luajit_injector as lj
    from dstools.shared.app_settings import get_luajit_enabled

    with _isolated_settings_dir(), tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        install_dir = root / "steamapps" / "common" / "Don't Starve Together"
        bin64 = install_dir / "bin64"
        bin64.mkdir(parents=True)
        (bin64 / "dontstarve_dedicated_server_nullrenderer_x64.exe").write_bytes(b"server")
        (bin64 / "dontstarve_steam_x64.exe").write_bytes(b"client")
        stale = get_luajit_dir(install_dir)
        stale.mkdir()
        write_marker(stale, LuajitMarker(DST_version="1", luajit_version="1"))
        with _fake_workshop_dir(root, [WORKSHOP_ID], with_injector_files=True, mod_version="3.0.0") as workshop:
            assert lj.uses_game_bin64(install_dir)
            assert detect_state(bin64) is InjectorState.NOT_INSTALLED
            assert not needs_regeneration(install_dir), "未安装时不需要启动前修复"

            result = lj.apply_install(bin64, [])
            assert result.ok, result.errors
            assert (bin64 / "Winmm.dll").read_bytes() == b"fake winmm", "注入壳应直接放进游戏 bin64"
            marker = install_dir / "data" / "unsafedata" / "ds_luajit_injector.path"
            assert Path(marker.read_text(encoding="utf-8").strip()).read_bytes() == b"fake injector"
            assert not stale.exists(), "旧的 luajit 隔离副本应被清理"
            assert get_luajit_enabled() is False, "游戏专服不改独立专服用的全局开关"
            assert detect_state(bin64) is InjectorState.ACTIVE
            assert resolve_launch_bin64_dir(install_dir) is None, "直接从真实 bin64 启动"
            assert not needs_regeneration(install_dir)

            source = workshop / WORKSHOP_ID / "bin64" / "windows" / "Winmm.dll"
            source.write_bytes(b"new winmm")
            assert needs_regeneration(install_dir), "配套 Mod 更新了注入壳，应在启动前更新"
            assert regenerate(bin64).ok
            assert (bin64 / "Winmm.dll").read_bytes() == b"new winmm"

            source.write_bytes(b"newer winmm")
            with patch.object(lj, "_file_in_use", return_value=True):
                assert not needs_regeneration(install_dir), "被占用时沿用当前版本，不阻止开服"
                assert regenerate(bin64).ok
            assert (bin64 / "Winmm.dll").read_bytes() == b"new winmm"

            assert apply_uninstall(bin64) is True
            assert not (bin64 / "Winmm.dll").exists() and not marker.exists()
            assert detect_state(bin64) is InjectorState.NOT_INSTALLED

            # 新版创意工坊只发布 Injector.dll.pkg，由注入壳生成规范名；标记仍写规范名
            injector = workshop / WORKSHOP_ID / "Injector.dll"
            packaged = injector.with_name("Injector.dll.pkg")
            injector.replace(packaged)
            result = lj.apply_install(bin64, [])
            assert result.ok, result.errors
            assert Path(marker.read_text(encoding="utf-8").strip()) == injector.resolve()
            assert detect_state(bin64) is InjectorState.ACTIVE
            assert lj.runtime_dlls()[-1] == packaged, "运行库版本应按 .pkg 判断"


def test_luajit_full_uninstall():
    """独立专服彻底卸载：删除副本、中断遗留的临时目录和路径标记并停用配套 Mod；
    不带 version.json 的同名文件夹和真实 bin64 不碰。"""
    import dstools.features.local_service.luajit_injector as lj
    from dstools.features.mod.manager import ModOverrides, enable_mod, load_mod_overrides, save_mod_overrides

    with _isolated_settings_dir(), tempfile.TemporaryDirectory() as tmp:
        install_dir = _make_fake_install_dir(Path(tmp), build_id="100")
        bin64 = install_dir / "bin64"
        bin64.mkdir()
        (bin64 / "dontstarve_dedicated_server_nullrenderer_x64.exe").write_bytes(b"server")
        luajit_dir = get_luajit_dir(install_dir)
        luajit_dir.mkdir()
        (luajit_dir / "Winmm.dll").write_bytes(b"x")
        write_marker(luajit_dir, LuajitMarker(DST_version="100", luajit_version="1"))
        (install_dir / ".luajit.tmp-abc").mkdir()
        (install_dir / ".luajit.backup-def" / "sub").mkdir(parents=True)
        injector = Path(tmp) / "Injector.dll"
        injector.write_bytes(b"x")
        write_injector_path_marker(install_dir, injector)
        set_luajit_enabled(True)
        mo_path = Path(tmp) / "Master" / "modoverrides.lua"
        overrides = ModOverrides(path=mo_path)
        enable_mod(overrides, lj.WORKSHOP_MOD_KEY)
        enable_mod(overrides, "workshop-1")
        save_mod_overrides(overrides)
        assert detect_state(bin64) is InjectorState.ACTIVE

        result = lj.apply_full_uninstall(bin64, [mo_path])
        assert result.ok, result.errors
        assert get_luajit_enabled() is False
        assert not luajit_dir.exists()
        assert not list(install_dir.glob(".luajit.*")), "中断遗留的临时目录应被清理"
        assert not (install_dir / "data" / "unsafedata" / "ds_luajit_injector.path").exists()
        mods = load_mod_overrides(mo_path).mods
        assert mods[lj.WORKSHOP_MOD_KEY].enabled is False and mods["workshop-1"].enabled is True
        assert (bin64 / "dontstarve_dedicated_server_nullrenderer_x64.exe").is_file()
        assert not lj.has_leftovers(bin64)

        luajit_dir.mkdir()
        (luajit_dir / "user_file.txt").write_text("x", encoding="utf-8")
        assert not lj.has_leftovers(bin64), "没有 version.json 的同名文件夹不算本工具的残留"
        assert lj.apply_full_uninstall(bin64, []).ok
        assert (luajit_dir / "user_file.txt").is_file()


def test_luajit_injector():
    """luajit_injector 的离线逻辑：版本读取、副本状态检测、启动目录解析、标记往返、重建判断、订阅检测、
    安装计划与卸载幂等（真实注入效果属人工验证项）。"""

    with tempfile.TemporaryDirectory() as tmp:
        install_dir = _make_fake_install_dir(Path(tmp), build_id="111")
        assert read_game_version_file(install_dir) == "111"
        assert read_game_version_file(install_dir.parent) is None, (
            "没有 version.txt 应该返回 None"
        )

    with _isolated_settings_dir():
        with tempfile.TemporaryDirectory() as tmp:
            install_dir = _make_fake_install_dir(Path(tmp))
            bin64 = install_dir / "bin64"
            bin64.mkdir()
            luajit_dir = get_luajit_dir(install_dir)

            assert detect_state(bin64) is InjectorState.NOT_INSTALLED
            luajit_dir.mkdir(parents=True)
            (luajit_dir / "Winmm.dll").write_bytes(b"x")
            injector = install_dir / "fake-mod" / "Injector.dll"
            injector.parent.mkdir()
            injector.write_bytes(b"x")
            write_injector_path_marker(install_dir, injector)
            assert detect_state(bin64) is InjectorState.DISABLED_LEFTOVER, (
                "副本存在但还没启用，应该是已关闭残留"
            )
            set_luajit_enabled(True)
            assert detect_state(bin64) is InjectorState.ACTIVE
            set_luajit_enabled(False)
            assert detect_state(bin64) is InjectorState.DISABLED_LEFTOVER

        with tempfile.TemporaryDirectory() as tmp:
            install_dir = _make_fake_install_dir(Path(tmp))
            assert resolve_launch_bin64_dir(install_dir) is None, "未启用应该返回 None"
            set_luajit_enabled(True)
            assert resolve_launch_bin64_dir(install_dir) is None, (
                "已启用但副本还没装过（缺锚点文件）应该返回 None"
            )
            luajit_dir = get_luajit_dir(install_dir)
            luajit_dir.mkdir(parents=True)
            (luajit_dir / "Winmm.dll").write_bytes(b"x")
            injector = install_dir / "fake-mod" / "Injector.dll"
            injector.parent.mkdir()
            injector.write_bytes(b"x")
            write_injector_path_marker(install_dir, injector)
            assert resolve_launch_bin64_dir(install_dir) == luajit_dir, (
                "已启用且副本有效应该返回副本目录，给 ServerProcess 用来覆盖启动目录"
            )

        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            assert read_marker(d) is None, "没有标记文件应该返回 None，不抛异常"
            write_marker(d, LuajitMarker(DST_version="123", luajit_version="1.0.0"))
            m = read_marker(d)
            assert m.DST_version == "123" and m.luajit_version == "1.0.0"
            # 落盘的 version.json 里 DST_version 应该是不带引号的数字（用户
            # 指定的格式），luajit_version 是语义化版本号字符串。
            raw = json.loads((d / "version.json").read_text(encoding="utf-8"))
            assert raw == {
                "DST_version": 123,
                "luajit_version": "1.0.0",
                "layout_version": 1,
                "trigger_sha256": "",
            }, (
                f"version.json 落盘格式不对: {raw}"
            )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install_dir = _make_fake_install_dir(root, build_id="111")
            set_luajit_enabled(False)  # 上一个子测试可能留下 True，这里显式复位
            with _fake_workshop_dir(
                root, [WORKSHOP_ID], with_injector_files=True, mod_version="1.10.1"
            ) as workshop_dir:
                assert needs_regeneration(install_dir) is False, "未启用应该是 False"
                set_luajit_enabled(True)
                assert needs_regeneration(install_dir) is True, (
                    "已启用但缺少新版运行时和标记，应该触发修复"
                )

                luajit_dir = get_luajit_dir(install_dir)
                luajit_dir.mkdir(parents=True)
                (luajit_dir / "Winmm.dll").write_bytes(b"fake winmm")
                injector = workshop_dir / WORKSHOP_ID / "Injector.dll"
                write_injector_path_marker(install_dir, injector)
                trigger_hash = hashlib.sha256(b"fake winmm").hexdigest()
                write_marker(
                    luajit_dir, LuajitMarker(
                        DST_version="111",
                        luajit_version="1.10.1",
                        layout_version=2,
                        trigger_sha256=trigger_hash,
                    )
                )
                assert needs_regeneration(install_dir) is False, (
                    "游戏版本、配套 Mod 版本都一致，不需要重新生成"
                )

                legacy_injector = (
                    workshop_dir / WORKSHOP_ID / "bin64" / "windows" / "Injector.dll"
                )
                legacy_injector.write_bytes(b"legacy injector")
                write_injector_path_marker(install_dir, legacy_injector)
                assert needs_regeneration(install_dir) is True, (
                    "作者移动 Injector.dll 后应刷新路径标记"
                )
                write_injector_path_marker(install_dir, injector)

                write_marker(
                    luajit_dir, LuajitMarker(
                        DST_version="000", luajit_version="1.10.1",
                        layout_version=2, trigger_sha256=trigger_hash,
                    )
                )
                assert needs_regeneration(install_dir) is True, (
                    "游戏版本不一致（被更新过），需要重新生成"
                )

                write_marker(
                    luajit_dir, LuajitMarker(
                        DST_version="111", luajit_version="1.10.0",
                        layout_version=2, trigger_sha256=trigger_hash,
                    )
                )
                assert needs_regeneration(install_dir) is True, (
                    "配套 Mod 版本不一致（作者发布了新版本），也需要重新生成"
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install_dir = _make_fake_install_dir(root, build_id="222")
            bin64 = install_dir / "bin64"
            bin64.mkdir()
            (bin64 / "game.exe").write_bytes(
                b"fake game exe"
            )  # 模拟真实 bin64 里的游戏文件
            (bin64 / "Injector.dll").write_bytes(b"manually installed legacy injector")

            with _fake_workshop_dir(
                root, [WORKSHOP_ID], with_injector_files=True, mod_version="1.10.1"
            ):
                luajit_dir = get_luajit_dir(install_dir)
                luajit_dir.mkdir(parents=True)
                (luajit_dir / "Injector.dll").write_bytes(b"legacy injector")
                legacy_deps = luajit_dir / "deps"
                legacy_deps.mkdir()
                (legacy_deps / "old.dll").write_bytes(b"legacy dependency")
                write_marker(
                    luajit_dir, LuajitMarker(DST_version="111", luajit_version="1.10.0")
                )

                result = regenerate(bin64)
                assert result.ok is True, f"应该成功: {result.errors}"
                assert (luajit_dir / "game.exe").read_bytes() == b"fake game exe", (
                    "重新生成应该带上真实 bin64 里当前的游戏文件"
                )
                assert (luajit_dir / "Winmm.dll").read_bytes() == b"fake winmm", (
                    "注入文件应该直接取自订阅内容，不是重新联网下载"
                )
                assert not (luajit_dir / "Injector.dll").exists(), (
                    "即使真实 bin64 曾手动安装旧载荷，新副本也不应保留 Injector.dll"
                )
                assert not (luajit_dir / "deps").exists(), (
                    "迁移时应清掉旧版复制到副本中的 deps"
                )
                path_marker = (
                    install_dir / "data" / "unsafedata" / "ds_luajit_injector.path"
                )
                marker_target = Path(path_marker.read_text(encoding="utf-8").strip())
                assert marker_target.read_bytes() == b"fake injector", (
                    "路径标记应指向创意工坊 Mod 目录中的 Injector.dll"
                )
                new_marker = read_marker(luajit_dir)
                assert new_marker.DST_version == "222", (
                    "标记里的 DST_version 应该更新成当前真实值"
                )
                assert new_marker.luajit_version == "1.10.1", (
                    "luajit_version 也应该更新成当前配套 Mod 的版本"
                )
                assert new_marker.layout_version == 2
                assert new_marker.trigger_sha256 == hashlib.sha256(
                    b"fake winmm"
                ).hexdigest()

                # 只有配套 Mod 版本变化时不应重建 bin64：放一个哨兵文件，整份重建才会让它消失
                (luajit_dir / "existing_bin64_marker.txt").write_text(
                    "untouched", encoding="utf-8"
                )
                with _fake_workshop_dir(
                    root, [WORKSHOP_ID], with_injector_files=True, mod_version="1.10.2"
                ):
                    result2 = regenerate(bin64)
                    assert result2.ok is True, f"应该成功: {result2.errors}"
                    assert (luajit_dir / "existing_bin64_marker.txt").exists(), (
                        "只有 luajit_version 变了，DST_version 没变，不应该整个重新复制 bin64"
                    )
                    marker2 = read_marker(luajit_dir)
                    assert marker2.DST_version == "222", "DST_version 应该保持不变"
                    assert marker2.luajit_version == "1.10.2", (
                        "luajit_version 应该更新成新的配套 Mod 版本"
                    )

                # 作者有时只更新二进制而不改 modinfo.lua 版本号；用壳文件
                # 哈希检测这种更新，并继续走不重抄 bin64 的轻量路径。
                source_trigger = (
                    root / "steamapps" / "workshop" / "content" / "322330"
                    / WORKSHOP_ID / "bin64" / "windows" / "Winmm.dll"
                )
                source_trigger.write_bytes(b"updated winmm")
                assert needs_regeneration(install_dir) is True, (
                    "Winmm.dll 内容变化但 Mod 版本不变时也应检测到更新"
                )
                result3 = regenerate(bin64)
                assert result3.ok is True, f"应该成功: {result3.errors}"
                assert (luajit_dir / "existing_bin64_marker.txt").exists(), (
                    "仅 Winmm.dll 变化时不应重新复制 bin64"
                )
                assert (luajit_dir / "Winmm.dll").read_bytes() == b"updated winmm"
                assert needs_regeneration(install_dir) is False

            # 没有订阅内容时应优雅失败；必须用全新的 Workshop 根目录（之前写入的注入文件不会被清掉）
            with tempfile.TemporaryDirectory() as tmp_empty:
                with _fake_workshop_dir(Path(tmp_empty), []):
                    shutil.rmtree(luajit_dir)
                    result_no_source = regenerate(bin64)
                    assert result_no_source.ok is False, "找不到订阅内容应该失败"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with _fake_workshop_dir(root, []):
                assert is_workshop_subscribed() is False
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with _fake_workshop_dir(root, [WORKSHOP_ID]):
                assert is_workshop_subscribed() is True

        plan_missing = plan_install(None, server_running=False)
        assert plan_missing.blocked_reason == "bin64_not_found"

        with tempfile.TemporaryDirectory() as tmp2:
            real_bin64 = Path(tmp2) / "bin64"
            real_bin64.mkdir()
            plan_running = plan_install(real_bin64, server_running=True)
            assert plan_running.blocked_reason == "server_running"

            with _fake_workshop_dir(Path(tmp2), []):
                plan_not_subscribed = plan_install(real_bin64, server_running=False)
                assert plan_not_subscribed.blocked_reason == "workshop_not_subscribed"

            with _fake_workshop_dir(Path(tmp2), [WORKSHOP_ID]):
                plan_ok = plan_install(real_bin64, server_running=False)
                assert plan_ok.blocked_reason is None
                assert plan_ok.current_state is InjectorState.NOT_INSTALLED

            set_luajit_enabled(True)
            assert apply_uninstall(real_bin64) is True
            assert get_luajit_enabled() is False, "关闭应该只是把开关关掉"
            assert apply_uninstall(real_bin64) is False, (
                "已经关闭时重复调用应该幂等，不报错"
            )

    with tempfile.TemporaryDirectory() as tmp3:
        install_dir = Path(tmp3)
        assert find_bin64_dir(install_dir) is None, "空目录应该返回 None"
        (install_dir / "bin64").mkdir()
        (
            install_dir / "bin64" / "dontstarve_dedicated_server_nullrenderer_x64.exe"
        ).write_bytes(b"x")
        assert find_bin64_dir(install_dir) == install_dir / "bin64"


def test_steam_library_folder_casing():
    """Steam 库路径大小写以 libraryfolders.vdf 为准（注册表大小写可能不对，专服按字符串匹配会找不到 Mod）。"""

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        real_dir = root / "Steam"  # 磁盘上真实的目录名，大小写正确
        (real_dir / "steamapps").mkdir(parents=True)
        vdf_text = (
            '"libraryfolders"\n{\n'
            f'\t"0"\n\t{{\n\t\t"path"\t\t"{str(real_dir).replace(chr(92), chr(92) * 2)}"\n\t}}\n'
            "}\n"
        )
        (real_dir / "steamapps" / "libraryfolders.vdf").write_text(
            vdf_text, encoding="utf-8"
        )

        # 模拟注册表返回的大小写跟磁盘真实大小写不一致（Windows 文件系统
        # 不区分大小写，这个路径本身照样能正常访问/exists() 判断为真）。
        steam_root_wrong_case = Path(str(real_dir).lower())
        assert steam_root_wrong_case.exists(), "Windows 上大小写不影响路径是否存在"

        libraries = parse_library_folders(steam_root_wrong_case)
        assert str(libraries[0]) == str(real_dir), (
            f"应该优先用 libraryfolders.vdf 里 Steam 自己记录的正确大小写，结果是 {libraries[0]}"
        )


def test_font_style_switch():
    """字体样式独立于颜色主题：切换后字体族/字号生效、bold 保留、可持久化。"""

    from dstools.qt.theme import FONT_FAMILY_BY_STYLE, Theme
    from dstools.shared import app_settings

    with _isolated_settings_dir():
        theme = Theme()
        default_font = theme.font("FONT_SIZE_BASE")
        theme.set_font_style("cute")
        cute_font = theme.font("FONT_SIZE_BASE")
        assert cute_font.family() == FONT_FAMILY_BY_STYLE["cute"] == "KN Maiyuan"
        assert cute_font.pointSize() == default_font.pointSize(), "麦圆体与默认雅黑保持同一磅值"
        assert theme.font("FONT_SIZE_BASE", bold=True).bold()

        theme.set_theme("mint")
        assert theme.font_style == "cute", "切颜色主题不应改动字体样式"
        assert Theme().font_style == "cute", "新启动应读回已保存的字体样式"
        assert app_settings.get_font_style_choice() == "cute"


def test_frp_selfhost_port_conflict_detection():
    """自建 frps 端口冲突：解析探测输出中的 FRPSPORT；目标端口是 frps 自己当前绑定的端口时不算冲突，
    被第三方服务占用时才算冲突（曾因"服务在跑就跳过检查"放过真冲突）。"""

    from dstools.features.frp_selfhost.probe import _parse_probe_output

    output = (
        "UID:1000\nSUDO:ok\nSERVICE:active\nCPU:2\nMEM:1024,512\n"
        "PORTS:22,2323,7000,6010\nFRPSPORT:7000\n"
    )
    status = _parse_probe_output(output)
    assert status.used_ports == frozenset({22, 2323, 7000, 6010})
    assert status.frps_bind_port == 7000
    assert status.service_active is True

    assert status.port_conflicts(2323) is True
    assert status.port_conflicts(7000) is False, "frps 自己绑定的端口不算冲突"
    assert status.port_conflicts(9999) is False

    # 从未部署过时 FRPSPORT 为空，被其他服务占用的端口仍是冲突
    status2 = _parse_probe_output("UID:1000\nSUDO:ok\nSERVICE:inactive\nCPU:2\nMEM:1024,512\nPORTS:22\nFRPSPORT:\n")
    assert status2.frps_bind_port is None
    assert status2.port_conflicts(22) is True


def test_ktech_runtime_detector():
    """缺文件、被换成 64 位、截断损坏，都必须在启动 ktech.exe 前识别出来。"""
    x86_dll = (Path(__file__).resolve().parents[1] / "tools" / "ktools" / "CORE_RL_zlib_.dll").read_bytes()
    pe = int.from_bytes(x86_dll[0x3C:0x40], "little")
    x64_dll = bytearray(x86_dll)
    x64_dll[pe + 4:pe + 6] = (0x8664).to_bytes(2, "little")
    with tempfile.TemporaryDirectory() as tmp:
        runtime_dir = Path(tmp)
        for dll_name in ("MSVCR120.dll", "MSVCP120.dll", "VCOMP120.dll"):
            (runtime_dir / dll_name).write_bytes(x86_dll)
        assert _vc2013_x86_runtime_problem(runtime_dir) == ("", "")

        # 真实反馈的 0xc000007b：文件都在，但被换成 64 位或已损坏
        bad = runtime_dir / "MSVCP120.dll"
        bad.write_bytes(x64_dll)
        assert _vc2013_x86_runtime_problem(runtime_dir) == ("broken", str(bad))
        bad.write_bytes(x86_dll[:4096])
        assert _vc2013_x86_runtime_problem(runtime_dir) == ("broken", str(bad))
        bad.write_bytes(x86_dll)

        # 真实反馈缺的是 VCOMP120.dll；此前会先让 Windows 弹加载器错误框。
        (runtime_dir / "VCOMP120.dll").unlink()
        assert _vc2013_x86_runtime_problem(runtime_dir) == ("missing", "")


def test_ktech_broken_runtime_fails_without_dialog():
    """DLL 损坏（真实反馈 0xc000007b）时不弹系统错误框，直接熔断、不再调用 ktech。"""
    if sys.platform != "win32":
        return
    import ctypes
    import shutil

    from dstools.shared import tex_convert

    kernel32 = ctypes.windll.kernel32
    # 从终端启动时会继承「不弹框」的错误模式，先清成双击启动时的默认值，验证是代码自己做到的
    old_mode = kernel32.SetErrorMode(0)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            runtime_dir = Path(tmp) / "ktools"
            shutil.copytree(Path(__file__).resolve().parents[1] / "tools" / "ktools", runtime_dir)
            dll = runtime_dir / "CORE_RL_zlib_.dll"
            dll.write_bytes(dll.read_bytes()[:4096])
            result = tex_convert._run_ktech(runtime_dir / "ktech.exe", ["input.tex"], runtime_dir)
            assert (result.returncode & 0xFFFFFFFF) == 0xC000007B
            assert tex_convert._runtime_broken is True
            assert tex_convert.tex_to_png(runtime_dir / "coder.xml", Path(tmp) / "out.png") is False
            assert kernel32.GetErrorMode() == 0
    finally:
        kernel32.SetErrorMode(old_mode)
        tex_convert._runtime_broken = False


def test_ktech_not_launched_when_runtime_missing():
    """没装 VC++ 2013 的新用户：启动预热和 Mod 页都不能去启动 ktech，直接当作没有图标。"""
    from unittest.mock import patch

    from dstools.shared import tex_convert

    with tempfile.TemporaryDirectory() as tmp:
        tex_path = Path(tmp) / "icon.tex"
        tex_path.write_bytes(b"fake-tex")
        with (
            patch.object(tex_convert, "ktech_runtime_problem", return_value=("missing", "")),
            patch.object(tex_convert, "_run_ktech", side_effect=AssertionError("不应启动 ktech")),
        ):
            assert tex_convert.tex_to_png(tex_path, Path(tmp) / "out.png") is False


def test_ktech_ascii_runtime_conversion():
    """中文安装/输入路径下，ktech 仍须从纯 ASCII 缓存副本中运行。"""
    from types import SimpleNamespace
    from unittest.mock import patch

    from dstools.shared import tex_convert

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source_dir = root / "中文安装目录" / "tools" / "ktools"
        source_dir.mkdir(parents=True)
        (source_dir / "ktech.exe").write_bytes(b"fake-ktech")
        (source_dir / "CORE_RL_test.dll").write_bytes(b"fake-dll")
        cache_root = root / "ascii-cache"
        tex_path = root / "中文输入" / "icon.tex"
        tex_path.parent.mkdir()
        tex_path.write_bytes(b"fake-tex")
        out_path = root / "中文输出" / "icon.png"

        calls = []

        def fake_run(exe, args, cwd):
            calls.append((args, cwd))
            assert args == ["input.tex"]
            assert Path(exe).parent.is_relative_to(
                cache_root / "runtime" / "ktools"
            )
            assert Path(cwd).is_relative_to(cache_root / "runtime" / "ktech_jobs")
            assert (Path(cwd) / "input.tex").read_bytes() == b"fake-tex"
            (Path(cwd) / "input.png").write_bytes(b"fake-png")
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        tex_convert._ktools_runtime_attempted = False
        tex_convert._ktools_runtime_dir = None
        try:
            with (
                patch.object(tex_convert, "_TOOLS_DIR", source_dir),
                patch.object(tex_convert, "_KTECH_EXE", source_dir / "ktech.exe"),
                patch.object(tex_convert, "cache_root_dir", return_value=cache_root),
                patch.object(tex_convert, "ktech_runtime_problem", return_value=("", "")),
                patch.object(tex_convert, "_run_ktech", side_effect=fake_run),
            ):
                assert tex_convert.tex_to_png(tex_path, out_path) is True
        finally:
            tex_convert._ktools_runtime_attempted = False
            tex_convert._ktools_runtime_dir = None

        assert len(calls) == 1
        assert out_path.read_bytes() == b"fake-png"
        runtime_dirs = list((cache_root / "runtime" / "ktools").iterdir())
        assert len(runtime_dirs) == 1
        assert len(runtime_dirs[0].name) == 64
        assert (runtime_dirs[0] / "CORE_RL_test.dll").is_file()
        assert (runtime_dirs[0] / ".bundle.sha256").is_file()


def test_connect_fetch_timeout_watchdog():
    """直连代码查询的看门狗：未到阈值不动作，超时只触发一次，公网与穿透两条互不影响
    （urllib 的 timeout 不管 DNS 解析，后台线程可能长时间不返回）。"""

    from types import SimpleNamespace

    from dstools.i18n import t
    from dstools.qt.pages.local_service import LocalServicePage

    tab = LocalServicePage.__new__(LocalServicePage)
    calls = {"public_text": [], "public_status": [], "nat_text": [], "nat_status": []}
    tab._public_row = SimpleNamespace(
        set_value=lambda *a, **k: calls["public_text"].append(a),
        set_status=lambda *a, **k: calls["public_status"].append(a),
    )
    tab._nat_row = SimpleNamespace(
        set_value=lambda *a, **k: calls["nat_text"].append(a),
        set_status=lambda *a, **k: calls["nat_status"].append(a),
    )

    now = time.monotonic()
    tab._public_pending_since = now
    tab._public_timed_out = False
    tab._nat_pending_since = None
    tab._nat_timed_out = False
    tab._public_retry_attempt = tab._nat_retry_attempt = 0
    tab._check_connect_fetch_timeouts()
    assert calls["public_text"] == [] and calls["nat_text"] == []

    tab._public_pending_since = now - 1000  # 远超阈值
    tab._check_connect_fetch_timeouts()
    assert len(calls["public_text"]) == 1
    # 超时按失败处理，并安排第一次自动重试
    assert calls["public_text"][0][0] == t("local.connect_retry_in", seconds=5)
    assert tab._public_timed_out is True and tab._public_retry_due is not None
    assert calls["nat_text"] == []

    tab._check_connect_fetch_timeouts()
    assert len(calls["public_text"]) == 1, "已经标记过超时不该重复触发"

    tab._nat_pending_since = now - 1000
    tab._check_connect_fetch_timeouts()
    assert len(calls["nat_text"]) == 1
    assert calls["nat_text"][0][0] == t("local.connect_retry_in", seconds=5)
    assert len(calls["public_text"]) == 1, "穿透超时不该影响已经触发过的公网这一行"


if __name__ == "__main__":
    run(globals())
