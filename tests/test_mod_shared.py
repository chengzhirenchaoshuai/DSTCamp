"""共享 Mod 目录与列表模型的轻量回归测试。"""

import tempfile
import threading
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import run  # noqa: E402

from dstools.features.mod.catalog import ModCatalogStore
from dstools.features.mod.export_list import build_export_entries
from dstools.features.mod.icons import load_mod_icon_image
from dstools.features.mod.list_model import (
    build_mod_rows,
    merge_visible_mod_ids,
    referenced_missing_status_text,
    sort_mod_data,
)
from dstools.features.mod.parser import (
    ModInfo,
    find_workshop_content_dirs,
    find_workshop_residual_dirs,
    list_installed_mod_ids,
)
from dstools.qt.pages.mod import ModPage
from dstools.models import ModEntry, Platform
from dstools.features.mod.workshop_api import (
    SteamWorkshopSession,
    WorkshopItemDetails,
    WorkshopItemState,
    WorkshopUpdateCancelled,
    _run_workshop_worker,
)
from dstools.features.mod.workshop_cleanup import (
    ResidualCleanupContext,
    delete_legacy_runtime_residual,
    delete_workshop_residual,
    format_residual_directory_tree,
)
from dstools.features.mod.workshop_status import (
    WorkshopModEvidence,
    WorkshopModState,
    evaluate_workshop_status,
)


def test_catalog_does_not_store_page_state():
    store = ModCatalogStore()
    root = Path("C:/fake/wegame/mods")
    info = ModInfo(name="共享名称", version="1.2.3", version_status="confirmed")
    store.publish(
        Platform.WEGAME,
        {"mod-a": info},
        {"mod-a": root / "mod-a"},
        wegame_client_mods_dir=root,
    )

    snapshot = store.get(Platform.WEGAME, root)
    assert snapshot is not None
    assert snapshot.infos["mod-a"].name == "共享名称"
    assert not hasattr(snapshot, "enabled")
    assert not hasattr(snapshot, "configuration_options")

    homepage = ModEntry("mod-a", enabled=True, configuration_options={"x": 1})
    creation = ModEntry("mod-a", enabled=False, configuration_options={"x": 2})
    assert homepage.enabled is True and creation.enabled is False
    assert homepage.configuration_options != creation.configuration_options




def test_catalog_icons_and_platform_invalidation():
    store = ModCatalogStore()
    wegame_root = Path("C:/fake/wegame/mods")
    steam_info = ModInfo(name="Steam")
    wegame_info = ModInfo(name="WeGame")
    with patch(
        "dstools.features.mod.catalog.default_steam_mods_dir", return_value=Path("C:/steam/mods")
    ), patch(
        "dstools.features.mod.catalog.find_workshop_dir", return_value=Path("C:/steam/workshop")
    ):
        store.publish(Platform.STEAM, {"mod": steam_info}, {"mod": Path("C:/steam/mod")})
        store.publish(
            Platform.WEGAME,
            {"mod": wegame_info},
            {"mod": wegame_root / "mod"},
            wegame_client_mods_dir=wegame_root,
        )
        icon = Image.new("RGBA", (1, 1))
        store.update_icons(Platform.STEAM, {"mod": icon})
        assert store.get(Platform.STEAM).icons["mod"] is icon
        assert store.get(Platform.WEGAME, wegame_root).infos["mod"].name == "WeGame"
        store.invalidate(Platform.STEAM)
        assert store.get(Platform.STEAM) is None
        assert store.get(Platform.WEGAME, wegame_root) is not None


def test_catalog_releases_stale_icons_and_old_source_snapshots():
    store = ModCatalogStore()
    first_root = Path("C:/fake/wegame/first")
    second_root = Path("C:/fake/wegame/second")
    stale_icon = Image.new("RGBA", (192, 192), "red")
    kept_icon = Image.new("RGBA", (192, 192), "green")

    store.publish(
        Platform.WEGAME,
        {"stale": ModInfo(name="旧 Mod"), "kept": ModInfo(name="保留 Mod")},
        {},
        {"stale": stale_icon, "kept": kept_icon},
        first_root,
    )
    refreshed = store.publish(
        Platform.WEGAME,
        {"kept": ModInfo(name="保留 Mod")},
        {},
        {"orphan": stale_icon},
        first_root,
    )
    assert refreshed.icons == {"kept": kept_icon}

    store.publish(
        Platform.WEGAME,
        {"new": ModInfo(name="新目录 Mod")},
        {},
        {"new": Image.new("RGBA", (1, 1))},
        second_root,
    )
    assert store.get(Platform.WEGAME, first_root) is None
    assert store.get(Platform.WEGAME, second_root) is not None


def test_loaded_mod_icons_have_a_resident_size_limit():
    with tempfile.TemporaryDirectory() as tmp:
        large_path = Path(tmp) / "large.png"
        small_path = Path(tmp) / "small.png"
        Image.new("RGB", (512, 256), "red").save(large_path)
        Image.new("RGB", (64, 32), "blue").save(small_path)

        large = load_mod_icon_image(large_path)
        small = load_mod_icon_image(small_path)

        assert large.mode == "RGBA" and large.size == (192, 96)
        assert small.mode == "RGBA" and small.size == (64, 32)


def test_shared_rows_keep_filter_and_sort_consistent():
    infos = {
        "workshop-20": ModInfo(name="Mod 20", version_status="undeclared"),
        "workshop-3": ModInfo(name="Mod 3", version="3.0", version_status="confirmed"),
        "CommonModSets": ModInfo(
            name="自定义合集", version="1.0", version_status="confirmed"
        ),
    }
    entries = {
        mod_id: ModEntry(mod_id, enabled=(mod_id == "workshop-3")) for mod_id in infos
    }
    ordered = sort_mod_data(entries, infos)
    assert next(iter(ordered)) == "workshop-3"

    rows = build_mod_rows(
        ordered,
        infos,
        "",
        "custom",
        Platform.STEAM,
        show_local=False,
        separate_client_mods=True,
    )
    assert [row["workshop_id"] for row in rows] == ["CommonModSets"]
    assert rows[0]["version_text"]
    assert rows[0]["has_link"] is False


def test_luajit_mod_is_first_only_when_prioritized():
    luajit_id = "workshop-3444078585"
    infos = {
        "workshop-1": ModInfo(name="A Mod"),
        luajit_id: ModInfo(name="Z LuaJIT"),
        "workshop-2": ModInfo(name="Disabled Mod"),
    }
    entries = {
        "workshop-1": ModEntry("workshop-1", enabled=True),
        luajit_id: ModEntry(luajit_id, enabled=True),
        "workshop-2": ModEntry("workshop-2", enabled=False),
    }

    normal = list(sort_mod_data(entries, infos))
    prioritized = list(
        sort_mod_data(entries, infos, priority_mod_id=luajit_id)
    )

    assert normal == ["workshop-1", luajit_id, "workshop-2"]
    assert prioritized == [luajit_id, "workshop-1", "workshop-2"]


def test_visible_mod_ids_include_enabled_missing_references_only():
    configured = {
        "workshop-20": ModEntry("workshop-20", enabled=True),
        "workshop-30": ModEntry("workshop-30", enabled=False),
        "workshop-40": ModEntry("workshop-40", enabled=True),
    }
    assert merge_visible_mod_ids(
        ["workshop-10", "workshop-20", "workshop-10"], configured
    ) == ["workshop-10", "workshop-20", "workshop-40"]

    # 存档启用了但本机没有文件的 Mod：第三行写明原因，不能留空
    assert "本机未安装" in referenced_missing_status_text(None)
    unsubscribed = SimpleNamespace(state=WorkshopModState.UNSUBSCRIBED_REFERENCED)
    assert "未订阅" in referenced_missing_status_text(unsubscribed)




def test_workshop_worker_can_be_stopped_by_cancel_event():
    cancel_event = threading.Event()

    class FakeProcess:
        returncode = None
        terminated = False

        def poll(self):
            cancel_event.set()
            return None

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

    process = FakeProcess()
    with patch(
        "dstools.features.mod.workshop_api.subprocess.Popen", return_value=process
    ):
        try:
            _run_workshop_worker({"action": "update"}, cancel_event=cancel_event)
        except WorkshopUpdateCancelled:
            pass
        else:
            raise AssertionError("停止事件应中止 Workshop Worker")
    assert process.terminated is True


def test_workshop_candidates_do_not_require_installed_client_files():
    tab = ModPage.__new__(ModPage)
    tab._workshop_mod_ids = lambda: [11]
    tab._current_cluster_workshop_ids = lambda: {"33"}
    tab._workshop_scan_ids = (0.0, [])  # 没有可复用的扫描结果，实际扫描
    with patch(
        "dstools.features.mod.legacy_v1.find_legacy_packages",
        return_value={22: Path("C:/fake/22_legacy.bin")},
    ), patch(
        "dstools.features.mod.parser.find_workshop_residual_dirs",
        return_value={},
    ), patch(
        "dstools.features.mod.legacy_v1.find_legacy_runtime_residual_dirs",
        return_value={},
    ):
        assert tab._workshop_candidate_ids() == [11, 22, 33]


def test_residual_directory_is_not_treated_as_installed_or_updateable():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "content" / "322330"
        residue = root / "3671964429"
        residue.mkdir(parents=True)
        (residue / "preview.jpg").write_bytes(b"image")
        complete = root / "11"
        complete.mkdir()
        (complete / "modinfo.lua").write_text("name='ok'", encoding="utf-8")

        with patch(
            "dstools.features.mod.parser.find_workshop_dir", return_value=root
        ), patch(
            "dstools.features.mod.parser.find_game_mods_dir", return_value=None
        ):
            assert find_workshop_content_dirs() == {
                11: complete,
                3671964429: residue,
            }
            assert find_workshop_residual_dirs() == {3671964429: residue}
            assert list_installed_mod_ids(
                legacy_packages={2329855041: root / "2329855041" / "x_legacy.bin"}
            ) == ["workshop-11", "workshop-2329855041"]

        referenced = evaluate_workshop_status(
            WorkshopModEvidence(
                workshop_id=3671964429,
                steam_state=WorkshopItemState(0),
                configured=True,
                residual_path=residue,
            )
        )
        assert referenced.state == WorkshopModState.UNSUBSCRIBED_REFERENCED
        assert referenced.can_update is False
        assert referenced.needs_action is False
        assert referenced.can_cleanup_residual is True

        residue_only = evaluate_workshop_status(
            WorkshopModEvidence(
                workshop_id=3671964429,
                steam_state=WorkshopItemState(0),
                residual_path=residue,
            )
        )
        assert residue_only.state == WorkshopModState.RESIDUAL_FILES

        subscribed_missing = evaluate_workshop_status(
            WorkshopModEvidence(
                workshop_id=3671964429,
                steam_state=WorkshopItemState(1),
                configured=True,
                residual_path=residue,
            )
        )
        assert subscribed_missing.state == WorkshopModState.MISSING
        assert subscribed_missing.can_update is True
        assert subscribed_missing.can_cleanup_residual is False

        # Steam 仅标记已安装（4）、当前账号未订阅且没有文件：不能留在待更新里成为无操作的死项
        for evidence_kwargs, expected in (
            ({}, WorkshopModState.NOT_INSTALLED),
            ({"configured": True}, WorkshopModState.UNSUBSCRIBED_REFERENCED),
            ({"acf_subscribed": True, "acf_subscriber": "123"}, WorkshopModState.SUBSCRIBED_BY_OTHER_ACCOUNT),
        ):
            unsubscribed_missing = evaluate_workshop_status(
                WorkshopModEvidence(workshop_id=3671964429, steam_state=WorkshopItemState(4), **evidence_kwargs))
            assert unsubscribed_missing.state == expected
            assert unsubscribed_missing.needs_action is False

        runtime_residue = evaluate_workshop_status(
            WorkshopModEvidence(
                workshop_id=3671964429,
                steam_state=WorkshopItemState(0),
                discovered_path=complete,
                legacy_runtime_residual_paths=(complete,),
            )
        )
        assert runtime_residue.state == WorkshopModState.LEGACY_RUNTIME_RESIDUAL
        assert runtime_residue.can_update is False
        assert runtime_residue.can_cleanup_residual is True

        waiting_for_exit = evaluate_workshop_status(
            WorkshopModEvidence(
                workshop_id=3671964429,
                steam_state=WorkshopItemState(4),
                install_info=None,
                discovered_path=complete,
                workshop_content_path=complete,
                running_dst_processes=("dontstarve_steam_x64.exe",),
            )
        )
        assert (
            waiting_for_exit.state
            == WorkshopModState.UNSUBSCRIBED_PENDING_CLEANUP
        )
        assert waiting_for_exit.can_update is False
        assert waiting_for_exit.can_cleanup_residual is False

        installed_flag_residue = evaluate_workshop_status(
            WorkshopModEvidence(
                workshop_id=3671964429,
                steam_state=WorkshopItemState(4),
                discovered_path=complete,
                workshop_content_path=complete,
            )
        )
        assert installed_flag_residue.state == WorkshopModState.RESIDUAL_FILES
        assert installed_flag_residue.can_update is False
        assert installed_flag_residue.can_cleanup_residual is True

        no_flags_while_running = evaluate_workshop_status(
            WorkshopModEvidence(
                workshop_id=3671964429,
                steam_state=WorkshopItemState(0),
                discovered_path=complete,
                workshop_content_path=complete,
                running_dst_processes=("dontstarve_steam_x64.exe",),
            )
        )
        assert (
            no_flags_while_running.state
            == WorkshopModState.UNSUBSCRIBED_PENDING_CLEANUP
        )
        assert no_flags_while_running.can_cleanup_residual is False

        cleanup_after_exit = evaluate_workshop_status(
            WorkshopModEvidence(
                workshop_id=3671964429,
                steam_state=WorkshopItemState(0),
                discovered_path=complete,
                workshop_content_path=complete,
            )
        )
        assert cleanup_after_exit.state == WorkshopModState.RESIDUAL_FILES
        assert cleanup_after_exit.can_cleanup_residual is True

        unknown_steam = evaluate_workshop_status(
            WorkshopModEvidence(
                workshop_id=3671964429,
                residual_path=residue,
            )
        )
        assert unknown_steam.can_cleanup_residual is False


def test_unsubscribed_v2_item_cannot_fall_back_to_legacy():
    session = object.__new__(SteamWorkshopSession)
    session._ensure_started = lambda: None
    session.item_state = lambda _wid: WorkshopItemState(0)
    session.item_install_details = lambda _wid: None

    result = session.download_item(
        3671964429,
        source_details=WorkshopItemDetails(
            3671964429,
            1,
            content_handle=123456,
            file_size=42,
        ),
    )

    assert result.completed is False
    assert result.state is not None and result.state.legacy_item is False
    assert "未订阅" in str(result.error)
    assert "legacy_path_recovered_from_source" not in result.details

    session.item_state = lambda _wid: WorkshopItemState(4)
    installed_only = session.download_item(3671964429)
    assert installed_only.completed is False
    assert "未订阅" in str(installed_only.error)


def test_true_legacy_item_can_recover_download_path_from_source_details():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        session = object.__new__(SteamWorkshopSession)
        session._ensure_started = lambda: None
        session.item_state = lambda _wid: WorkshopItemState(3)
        session.item_install_details = lambda _wid: None
        captured = {}

        def finish(result, install_info):
            captured["path"] = install_info.path
            return result

        session._download_legacy_item = finish
        with patch(
            "dstools.features.mod.parser.find_workshop_dir", return_value=root
        ):
            result = session.download_item(
                463952377,
                source_details=WorkshopItemDetails(
                    463952377,
                    1,
                    content_handle=987654321,
                    file_size=42,
                ),
            )

        assert result.state is not None and result.state.legacy_item is True
        assert captured["path"] == root / "463952377" / "987654321_legacy.bin"
        assert result.details["legacy_path_recovered_from_source"] is True


def test_residual_cleanup_deletes_and_rejects_steam_managed_items():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "content" / "322330"
        residue = root / "3671964429"
        residue.mkdir(parents=True)
        (residue / "preview.jpg").write_bytes(b"image")
        with patch(
            "dstools.features.mod.workshop_cleanup.find_workshop_dir",
            return_value=root,
        ), patch(
            "dstools.features.mod.legacy_v1.running_dst_processes",
            return_value=(),
        ):
            tree = format_residual_directory_tree((residue,))
            assert str(residue) in tree and "preview.jpg" in tree
            target = delete_workshop_residual(
                3671964429, residue, WorkshopItemState(0)
            )
            assert not residue.exists()
            assert target == residue.resolve()

            installed_flag_residue = root / "3671964433"
            installed_flag_residue.mkdir()
            (installed_flag_residue / "modinfo.lua").write_text(
                "name='other account residue'", encoding="utf-8"
            )
            installed_target = delete_workshop_residual(
                3671964433, installed_flag_residue, WorkshopItemState(4)
            )
            assert installed_target == installed_flag_residue.resolve()
            assert not installed_flag_residue.exists()

            managed = root / "3671964429"
            managed.mkdir()
            (managed / "preview.jpg").write_bytes(b"image")
            try:
                delete_workshop_residual(
                    3671964429, managed, WorkshopItemState(1)
                )
            except ValueError as exc:
                assert "Steam 仍在" in str(exc)
            else:
                raise AssertionError("已订阅目录不能删除")

            complete_residue = root / "3671964430"
            complete_residue.mkdir()
            (complete_residue / "modinfo.lua").write_text(
                "name='complete residue'", encoding="utf-8"
            )
            complete_target = delete_workshop_residual(
                3671964430, complete_residue, WorkshopItemState(0)
            )
            assert complete_target == complete_residue.resolve()
            assert not complete_residue.exists()

            shared_context_residue = root / "3671964432"
            shared_context_residue.mkdir()
            context = ResidualCleanupContext(
                workshop_root=root,
                legacy_runtime_roots=(),
                running_processes=(),
            )
            with patch(
                "dstools.features.mod.legacy_v1.running_dst_processes",
                side_effect=AssertionError("共享上下文不应重复检查进程"),
            ):
                delete_workshop_residual(
                    3671964432,
                    shared_context_residue,
                    WorkshopItemState(0),
                    context=context,
                )
            assert not shared_context_residue.exists()

            # 已取消订阅的 V1：同一批次先删 *_legacy.bin 包目录，再删 mods 运行目录
            v1_package = root / "3671964434"
            v1_package.mkdir()
            (v1_package / "3671964434_legacy.bin").write_bytes(b"zip")
            runtime_root = Path(tmp) / "mods"
            v1_runtime = runtime_root / "workshop-3671964434"
            v1_runtime.mkdir(parents=True)
            (v1_runtime / "modinfo.lua").write_text("name='v1'", encoding="utf-8")
            v1_context = ResidualCleanupContext(
                workshop_root=root,
                legacy_runtime_roots=(runtime_root,),
                running_processes=(),
            )
            try:
                delete_legacy_runtime_residual(
                    3671964434, v1_runtime, WorkshopItemState(4), context=v1_context
                )
            except ValueError as exc:
                assert "Legacy 下载包仍然存在" in str(exc)
            else:
                raise AssertionError("包仍在时不能删除运行目录")
            delete_workshop_residual(
                3671964434, v1_package, WorkshopItemState(4), context=v1_context
            )
            delete_legacy_runtime_residual(
                3671964434, v1_runtime, WorkshopItemState(4), context=v1_context
            )
            assert not v1_package.exists() and not v1_runtime.exists()

        running_residue = root / "3671964431"
        running_residue.mkdir()
        (running_residue / "preview.jpg").write_bytes(b"image")
        with patch(
            "dstools.features.mod.workshop_cleanup.find_workshop_dir",
            return_value=root,
        ), patch(
            "dstools.features.mod.legacy_v1.running_dst_processes",
            return_value=("dontstarve_steam_x64.exe",),
        ):
            try:
                delete_workshop_residual(
                    3671964431, running_residue, WorkshopItemState(0)
                )
            except ValueError as exc:
                assert "正在运行" in str(exc)
            else:
                raise AssertionError("游戏运行时不能清理 322330 目录")


def test_export_entries_keep_enabled_server_mods_only():
    """导出图片只收已启用的服务端 Mod；版本只取沙箱已确认的值，非工坊 Mod 不显示 ID。"""
    data = {
        "workshop-1": ModEntry("workshop-1", True),
        "workshop-2": ModEntry("workshop-2", False),
        "workshop-3": ModEntry("workshop-3", True),
        "my_local_mod": ModEntry("my_local_mod", True),
    }
    infos = {
        "workshop-1": ModInfo(name="A", author=" Bob ", version="1.2", version_status="confirmed"),
        "workshop-3": ModInfo(name="Client", client_only=True),
        "my_local_mod": ModInfo(name="Local", version="9", version_status="pending"),
    }
    entries = build_export_entries(data, infos, {"workshop-1": "V1", "my_local_mod": None})
    assert [e.workshop_id for e in entries] == ["workshop-1", "my_local_mod"]
    assert (entries[0].author, entries[0].version, entries[0].id_text, entries[0].mod_format) == ("Bob", "1.2", "1", "V1")
    assert (entries[1].version, entries[1].id_text, entries[1].mod_format) == ("", "", "")

    # 运行中专服按实际加载的目录判断：工坊内容目录是 V2，mods 里的 workshop-ID 副本是 V1
    from dstools.features.mod.export_list import loaded_mod_format

    assert loaded_mod_format("workshop-1", Path("ugc/content/322330/1")) == "V2"
    assert loaded_mod_format("workshop-1", Path("Server/mods/workshop-1")) == "V1"
    assert loaded_mod_format("WorldGate", Path("mods/WorldGate")) is None


def test_shadowed_v1_copy_is_detected_only_when_v2_exists():
    """专服 mods 里的旧副本只有在 Workshop 已有 V2 内容时才算挡住新版。"""
    from dstools.features.mod.v1_shadow import find_shadowed_mods

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mods, content = root / "mods", root / "content"
        for wid in ("111", "222"):
            (mods / f"workshop-{wid}").mkdir(parents=True)
        (content / "111").mkdir(parents=True)
        (content / "111" / "modinfo.lua").write_text('name = "New"', encoding="utf-8")
        found = find_shadowed_mods(["workshop-111", "222", "333"], mods, content, with_versions=False)
    assert [(m.workshop_id, m.name) for m in found] == [("111", "New")]


def test_shardindex_save_replaces_only_enabled_mods():
    """本地存档 shardindex 写回只替换 enabled_mods，字符串里的括号和其它字段原样保留。"""
    from dstools.features.mod.shardindex import load_shardindex_mods, save_shardindex_mods
    from dstools.models import ModEntry

    rest = ('  server={ name="a}{b", description=[[x}]] },\n'
            '  enabled_mods={ ["workshop-1"]={ enabled=true, configuration_options={ k="}" } } },\n'
            '  session_id="S", version=5\n}')
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "shardindex"
        path.write_bytes(b"KLEI     1 return {\n" + rest.encode())
        mods = load_shardindex_mods(path)
        assert mods["workshop-1"].configuration_options == {"k": "}"}
        mods["workshop-1"].enabled = False
        mods["workshop-2"] = ModEntry("workshop-2", True, {"n": 3})
        save_shardindex_mods(path, mods)
        text = path.read_text(encoding="utf-8")
        back = load_shardindex_mods(path)
    assert text.startswith('KLEI     1 return {\n  server={ name="a}{b", description=[[x}]] },\n  enabled_mods=')
    assert text.endswith(',\n  session_id="S", version=5\n}')
    assert not back["workshop-1"].enabled and back["workshop-2"].configuration_options == {"n": 3}


def test_client_mods_disabled_switch_is_not_a_mod():
    """禁用客户端 Mod 的键不当成 Mod；游戏只看真假（enabled=false 也算开启），关闭时整键删除。"""
    from dstools.features.mod.manager import load_mod_overrides, save_mod_overrides, sync_mods
    from dstools.models import ModOverrides
    from dstools.shared.lua_parser import parse_lua_file

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "modoverrides.lua"
        path.write_text('return { ["workshop-1"]={ enabled=true, configuration_options={ k=1 } },\n'
                        '  client_mods_disabled={ enabled=false } }', encoding="utf-8")
        overrides = load_mod_overrides(path)
        assert list(overrides.mods) == ["workshop-1"] and overrides.client_mods_disabled
        save_mod_overrides(overrides)
        assert parse_lua_file(path)["client_mods_disabled"] == {"enabled": True}

        other = ModOverrides(path=Path(tmp) / "Caves.lua")
        sync_mods(overrides, other)
        assert other.client_mods_disabled

        overrides.client_mods_disabled = False
        save_mod_overrides(overrides)
        assert parse_lua_file(path) == {"workshop-1": {"enabled": True, "configuration_options": {"k": 1}}}


def test_mod_config_memory_two_way_sync():
    """默认配置与游戏全局配置（普通 Mod 为 _CLIENT 文件）三方比较：单边变化同步到另一边，
    游戏运行时只拉不推，双边变化取较新一方；存档中转用的无后缀文件不参与。"""
    import os

    from dstools.features.mod import config_memory as cm
    from dstools.features.mod import game_mod_config as g

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        user, mem, bak = root / "123", root / "memory.json", root / "bak"
        game_file = g.mod_config_dir(user) / g.file_name("workshop-9", True)
        g.write_values(game_file, {"a": 1, "flag": True})
        g.write_values(g.mod_config_dir(user) / g.file_name("workshop-9", False), {"a": 99})
        g.write_values(g.mod_config_dir(user) / g.file_name("workshop-8", False), {"a": 99})

        def sync(can_write=True):
            return cm.sync_with_game(user, "123", set(), can_write=can_write, memory_path=mem, backup_dir=bak)

        assert sync().pulled == [game_file.name]
        assert cm.recall("workshop-9", path=mem) == {"a": 1, "flag": True}

        cm.remember("workshop-9", {"a": 2}, path=mem)  # DSTCamp 改了
        assert sync(can_write=False).pending == [game_file.name]
        assert g.read_values(game_file)["a"] == 1
        assert sync().pushed == [game_file.name] and g.read_values(game_file)["a"] == 2
        assert (bak / game_file.name).exists()

        g.write_values(game_file, {"flag": False})  # 游戏改了
        assert sync().pulled and cm.recall("workshop-9", path=mem)["flag"] is False

        cm.remember("workshop-9", {"a": 3}, now=1.0, path=mem)  # 双边都改：游戏文件更新，游戏胜出
        g.write_values(game_file, {"a": 4})
        os.utime(game_file, (100.0, 100.0))
        report = sync()
        assert report.conflicts == [(game_file.name, "game")] and cm.recall("workshop-9", path=mem)["a"] == 4


def test_modinfo_cache_invalidates_on_edit_and_skips_lossy_results():
    import dstools.features.mod.modinfo_cache as modinfo_cache
    from dstools.features.mod.parser import ModConfigOption

    with tempfile.TemporaryDirectory() as temp:
        folder = Path(temp) / "workshop-123"
        folder.mkdir()
        modinfo = folder / "modinfo.lua"
        with patch.object(modinfo_cache, "_CACHE_DIR", Path(temp) / "cache"):
            modinfo.write_text('name = "A"\nversion = "1.0"\n', encoding="utf-8")
            assert modinfo_cache.parse_modinfo_cached(folder).version == "1.0"
            # 第二次读缓存；改了内容必须重新解析
            assert modinfo_cache.parse_modinfo_cached(folder).version == "1.0"
            modinfo.write_text('name = "A"\nversion = "2.0"\n', encoding="utf-8")
            assert modinfo_cache.parse_modinfo_cached(folder).version == "2.0"

            # JSON 存不住元组：不能缓存，否则下次读回来变成列表
            lossy = ModInfo(name="B", config_options=[ModConfigOption(name="o", default=(1, 2))])
            with patch.object(modinfo_cache, "parse_modinfo", return_value=lossy):
                modinfo.write_text('name = "B"\n', encoding="utf-8")
                assert modinfo_cache.parse_modinfo_cached(folder) is lossy
            assert all('"name": "B"' not in p.read_text(encoding="utf-8")
                       for p in (Path(temp) / "cache").glob("*.json"))


def test_mod_page_uses_preload_snapshot_then_detects_changes():
    """Mod 页按启动预热的快照显示时不再扫盘；Mod 在预热之后被改过，事后重扫的指纹必须对不上。"""
    from dstools.features.mod.preload import warm_mod_caches

    with tempfile.TemporaryDirectory() as temp:
        mods_dir = Path(temp) / "mods"
        folder = mods_dir / "my_mod"
        folder.mkdir(parents=True)
        (folder / "modinfo.lua").write_text('name = "A"\nversion = "1.0"\n', encoding="utf-8")
        tab = ModPage.__new__(ModPage)
        tab.ctx = SimpleNamespace(mod_catalog=ModCatalogStore())
        args = (Path(temp) / "modoverrides.lua", False, Platform.STEAM, None, mods_dir, None, {}, None, None)
        with (
            patch("dstools.features.mod.parser.find_workshop_dir", return_value=None),
            patch("dstools.features.mod.preload.find_legacy_packages", return_value={}),
            patch("dstools.qt.pages.mod.find_legacy_packages", return_value={}),
        ):
            snapshot = warm_mod_caches(Platform.STEAM, None, mods_dir).snapshot
            assert snapshot.infos["my_mod"].version == "1.0"
            with patch("dstools.qt.pages.mod.list_installed_mod_ids", side_effect=AssertionError("不应扫盘")):
                shown = tab._load_mods_worker(*args, snapshot=snapshot)
            assert list(shown["mod_data"]) == ["my_mod"] and shown["mod_infos"]["my_mod"].name == "A"
            assert tab._load_mods_worker(*args)["stamps"] == shown["stamps"]

            (folder / "modinfo.lua").write_text('name = "A"\nversion = "2.0.0"\n', encoding="utf-8")
            assert tab._load_mods_worker(*args)["stamps"] != shown["stamps"]


def test_not_registered_when_steam_manifest_misses_installed_files():
    """文件齐全、Steam 也认为订阅着，但清单没登记：开服时专服静默跳过，必须单独报出来。"""
    import tempfile
    from dstools.features.mod.workshop_api import (
        ITEM_DOWNLOADING,
        ITEM_LEGACY,
        ITEM_SUBSCRIBED,
        WorkshopItemState,
    )

    with tempfile.TemporaryDirectory() as tmp:
        content = Path(tmp) / "3671964429"
        content.mkdir()
        (content / "modinfo.lua").write_text("name='x'", encoding="utf-8")

        def state_of(flags=ITEM_SUBSCRIBED, **kwargs):
            return evaluate_workshop_status(WorkshopModEvidence(
                workshop_id=3671964429, steam_state=WorkshopItemState(flags),
                discovered_path=content, **kwargs)).state

        unregistered = evaluate_workshop_status(WorkshopModEvidence(
            workshop_id=3671964429, steam_state=WorkshopItemState(ITEM_SUBSCRIBED),
            discovered_path=content, acf_installed=False))
        assert unregistered.state == WorkshopModState.NOT_REGISTERED
        assert unregistered.needs_action is True
        # 清单读不到（None）或已登记时都不报这个状态
        assert state_of(acf_installed=None) != WorkshopModState.NOT_REGISTERED
        assert state_of(acf_installed=True) != WorkshopModState.NOT_REGISTERED
        # 下载途中清单还没写条目，不能当成异常
        assert state_of(ITEM_SUBSCRIBED | ITEM_DOWNLOADING, acf_installed=False) \
            == WorkshopModState.DOWNLOADING
        # V1 靠专服 mods 下的副本加载（MODS_ROOT 兜底），不受清单登记影响
        assert state_of(ITEM_SUBSCRIBED | ITEM_LEGACY, acf_installed=False) \
            != WorkshopModState.NOT_REGISTERED


def test_parse_workshop_ids_from_links():
    from dstools.features.mod.workshop_webapi import parse_workshop_ids
    text = ("https://steamcommunity.com/workshop/filedetails/?l=schinese&id=375850593#comments，"
            "steam://url/CommunityFilePage/378160973/\nworkshop-3444078585 3444078585、abc 0123")
    assert parse_workshop_ids(text) == (["375850593", "378160973", "3444078585"], ["abc", "0123"])


if __name__ == "__main__":
    run(globals())
