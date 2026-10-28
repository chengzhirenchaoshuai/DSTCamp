"""多存档开服端口模型的直接 E2E 测试（不依赖 pytest）。"""

from __future__ import annotations

import tempfile
from contextlib import contextmanager
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _harness import run  # noqa: E402

from dstools.models import Cluster, Shard
from dstools.shared.server_ports import (
    DEFAULT_MASTER_PORT,
    LAN_SERVER_PORT_FALLBACK,
    LAN_SERVER_PORT_MAX,
    LAN_SERVER_PORT_MIN,
    allocate_cluster_port_values,
    allocate_lan_server_ports,
    collect_cluster_port_claims,
    find_port_conflicts,
    next_free_port,
    rewrite_cluster_ports_atomic,
    rewrite_lan_server_ports_atomic,
    stable_path_key,
    UdpPortScan,
)


def _write_cluster(root: Path, name: str, *, master_port: int = 10888,
                   master_server_port: int | None = None,
                   master_auth_port: int | None = None,
                   caves: bool = True, lan_only: bool = False,
                   offline: bool = False,
                   master_game_port: int = 10999,
                   caves_game_port: int = 10998) -> Cluster:
    path = root / name
    path.mkdir()
    (path / "cluster.ini").write_text(
        f"[NETWORK]\nlan_only_cluster={str(lan_only).lower()}\n"
        f"offline_cluster={str(offline).lower()}\n\n"
        "[SHARD]\nshard_enabled=true\n"
        f"master_port={master_port}\nbind_ip=127.0.0.1\n",
        encoding="utf-8",
    )
    master = path / "Master"
    master.mkdir()
    steam = ""
    if master_server_port is not None or master_auth_port is not None:
        steam = "\n[STEAM]\n"
        if master_server_port is not None:
            steam += f"master_server_port={master_server_port}\n"
        if master_auth_port is not None:
            steam += f"authentication_port={master_auth_port}\n"
    (master / "server.ini").write_text(
        f"[NETWORK]\nserver_port={master_game_port}\n\n[SHARD]\nis_master=true\n" + steam,
        encoding="utf-8",
    )
    shards = [Shard("Master", master)]
    if caves:
        cave = path / "Caves"
        cave.mkdir()
        (cave / "server.ini").write_text(
            f"[NETWORK]\nserver_port={caves_game_port}\n\n[SHARD]\nis_master=false\n"
            "name=Caves\n\n[STEAM]\nmaster_server_port=27017\n"
            "authentication_port=8767\n",
            encoding="utf-8",
        )
        shards.append(Shard("Caves", cave))
    return Cluster(name, path, shards=shards)


def _local_page(clusters=(), *, manager=None, mapping=None, jumps=None):
    """构造不建控件的 Qt 本地服务器页，只挂启动预检等业务流程用到的属性。"""
    from dstools.qt.pages.local_service import LocalServicePage

    service = LocalServicePage.__new__(LocalServicePage)
    service.window = lambda: None
    service.ctx = SimpleNamespace(
        env=SimpleNamespace(clusters=list(clusters)),
        mapping_owner=lambda *_args: mapping,
        goto_tab=jumps.append if jumps is not None else (lambda _key: None),
        cluster_config_saved=SimpleNamespace(emit=lambda _cluster: None),
        confirm_unsaved=lambda _cluster=None: True,
    )
    service.manager = manager or SimpleNamespace(running=lambda: [])
    service._launching_keys = set()
    service._steam_update_running = False
    service._steam_remote_build_id = None
    service._steam_remote_build_app = None
    service._install_dir = None
    service._runtime = None
    service._runtime_resolution = None
    service._prepare_legacy_mods_for_start = lambda _cluster: True
    service._auto_restart = SimpleNamespace(cancel=lambda _cluster: None)
    return service


@contextmanager
def _no_steam_update():
    """启动预检会读本机 Steam 清单判断专服是否要更新，测试里固定为无需更新。"""
    from dstools.qt.pages import local_service as local_page

    with patch.object(local_page.steam_client_updater, "snapshot_app", return_value=None), \
            patch.object(local_page.steam_client_updater, "action_for_snapshot", return_value="none"):
        yield


def test_effective_defaults_and_internal_ports() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cluster = _write_cluster(Path(tmp), "Cluster_A")
        claims, issues = collect_cluster_port_claims(cluster)
        assert not issues
        values = {(c.shard_name, c.field): (c.port, c.source) for c in claims}
        assert values[("Master", "master_port")][0] == DEFAULT_MASTER_PORT
        assert values[("Master", "master_server_port")] == (27016, "default")
        assert values[("Master", "authentication_port")] == (8766, "default")
        assert values[("Caves", "master_server_port")] == (27017, "explicit")
        assert not find_port_conflicts(claims)


def test_multi_shard_default_steam_ports_no_conflict() -> None:
    """岛屿冒险等多世界存档：各从世界留空、共享默认 Steam 端口不应被误判为冲突。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = root / "5world"
        path.mkdir()
        (path / "cluster.ini").write_text(
            "[SHARD]\nshard_enabled=true\nmaster_port=10888\nbind_ip=127.0.0.1\n",
            encoding="utf-8",
        )
        shards = []
        for index, name in enumerate(("Master", "Caves", "Hamlet", "Shipwrecked", "Volcano")):
            shard_path = path / name
            shard_path.mkdir()
            is_master = name == "Master"
            (shard_path / "server.ini").write_text(
                f"[NETWORK]\nserver_port={10999 - index}\n\n"
                f"[SHARD]\nis_master={str(is_master).lower()}\n",
                encoding="utf-8",
            )
            shards.append(Shard(name, shard_path))
        cluster = Cluster("5world", path, shards=shards)
        claims, issues = collect_cluster_port_claims(cluster)
        assert not issues
        steam = [c for c in claims if c.field in ("master_server_port", "authentication_port")]
        assert steam and all(not c.binding for c in steam), "Steam 端口应被标为不绑定"
        assert not find_port_conflicts(claims)
        assert len({claim.port for claim in claims if claim.binding}) == 6


def test_cross_cluster_and_cross_field_conflicts() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        a = _write_cluster(root, "Cluster_A")
        b = _write_cluster(root, "Cluster_B", master_port=10999, caves=False,
                           master_server_port=27018, master_auth_port=8768)
        a_claims, _ = collect_cluster_port_claims(a)
        b_claims, _ = collect_cluster_port_claims(b)
        conflicts = find_port_conflicts(a_claims + b_claims)
        by_port = {c.port: c for c in conflicts}
        assert 10999 in by_port, "不同存档 server_port 相同必须报冲突"
        fields = {claim.field for claim in by_port[10999].claims}
        assert fields == {"server_port", "master_port"}, "跨字段冲突也必须识别"


def test_shard_override_and_invalid_values() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cluster = _write_cluster(Path(tmp), "Cluster_A", caves=False)
        server_ini = cluster.path / "Master" / "server.ini"
        server_ini.write_text(
            "[NETWORK]\nserver_port=70000\n\n[SHARD]\nis_master=true\nmaster_port=12001\n",
            encoding="utf-8",
        )
        claims, issues = collect_cluster_port_claims(cluster)
        assert any(issue.field == "server_port" for issue in issues)
        master = next(c for c in claims if c.field == "master_port")
        assert master.port == 12001, "server.ini 的分片覆盖值应优先于 cluster.ini"


def test_lan_only_effective_ports_and_ranges() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cluster = _write_cluster(
            root, "LAN_Invalid", lan_only=True,
            master_game_port=10002, caves_game_port=10001,
        )
        claims, issues = collect_cluster_port_claims(cluster)
        lan_issues = [issue for issue in issues if issue.code == "lan_server_port_range"]
        assert len(lan_issues) == 2
        fallback_claims = [
            claim for claim in claims if claim.field == "server_port"
        ]
        assert all(claim.port == LAN_SERVER_PORT_FALLBACK for claim in fallback_claims)
        assert all(claim.source == "lan_fallback" for claim in fallback_claims)
        assert any(
            conflict.port == LAN_SERVER_PORT_FALLBACK
            for conflict in find_port_conflicts(claims)
        ), "应按游戏实际回退后的 10999 识别冲突"

        valid = _write_cluster(
            root, "LAN_Valid", lan_only=True,
            master_game_port=LAN_SERVER_PORT_MAX,
            caves_game_port=LAN_SERVER_PORT_MIN,
        )
        _, valid_issues = collect_cluster_port_claims(valid)
        assert not valid_issues

        offline = _write_cluster(
            root, "Offline_Invalid", offline=True,
            master_game_port=10002, caves_game_port=10001,
        )
        offline_claims, offline_issues = collect_cluster_port_claims(offline)
        assert len([
            issue for issue in offline_issues
            if issue.code == "lan_server_port_range"
        ]) == 2
        assert all(
            claim.port == LAN_SERVER_PORT_FALLBACK
            for claim in offline_claims if claim.field == "server_port"
        )
        assert all(
            "离线模式" in issue.message for issue in offline_issues
            if issue.code == "lan_server_port_range"
        )

        ordinary = _write_cluster(
            root, "Ordinary", lan_only=False,
            master_game_port=10002, caves_game_port=10001,
        )
        ordinary_claims, ordinary_issues = collect_cluster_port_claims(ordinary)
        assert not ordinary_issues
        assert {claim.port for claim in ordinary_claims if claim.field == "server_port"} == {
            10001, 10002,
        }


def test_helpers() -> None:
    assert next_free_port(100, {100, 101, 103}) == 102
    assert stable_path_key(Path("C:/root-a/Cluster_1")) != stable_path_key(Path("D:/root-b/Cluster_1"))
    master_port, shards = allocate_cluster_port_values(
        ["Master", "Caves"], {10888, 10998, 10999, 27016, 27017, 8766, 8767},
    )
    values = [master_port] + [value for ports in shards.values() for value in ports.values()]
    assert len(values) == len(set(values))
    assert not set(values) & {10888, 10998, 10999, 27016, 27017, 8766, 8767}
    lan_ports = allocate_lan_server_ports(
        ["Master", "Caves", "Hamlet"], {LAN_SERVER_PORT_MIN},
    )
    assert lan_ports["Master"] == LAN_SERVER_PORT_FALLBACK
    assert len(set(lan_ports.values())) == 3
    assert all(
        LAN_SERVER_PORT_MIN <= port <= LAN_SERVER_PORT_MAX
        for port in lan_ports.values()
    )
    try:
        allocate_lan_server_ports(
            ["Master", "Caves"],
            range(LAN_SERVER_PORT_MIN, LAN_SERVER_PORT_MAX + 1),
        )
    except ValueError:
        pass
    else:
        raise AssertionError("LAN 端口耗尽时必须失败，不能分配到 11018 以上")
    from dstools.features.sakura.api import sanitize_tunnel_name, find_dstcamp_tunnel
    old_name = sanitize_tunnel_name("Cluster_1", "Master", "server", "steam")
    new_a = sanitize_tunnel_name(
        "Cluster_1", "Master", "server", "steam", cluster_identity="path-a",
    )
    new_b = sanitize_tunnel_name(
        "Cluster_1", "Master", "server", "steam", cluster_identity="path-b",
    )
    assert new_a != new_b != old_name
    assert find_dstcamp_tunnel(
        [{"name": old_name}], "Cluster_1", "Master", "server", "steam",
        cluster_identity="path-a", allow_legacy=False,
    ) is None


def test_atomic_port_rewrite() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cluster = _write_cluster(Path(tmp), "Cluster_A")
        master_port, values = rewrite_cluster_ports_atomic(
            cluster, {10888, 10998, 10999, 27016, 27017, 8766, 8767}, create_backup=False,
        )
        claims, issues = collect_cluster_port_claims(cluster)
        assert not issues
        actual = {claim.port for claim in claims}
        expected = {master_port} | {
            value for shard_values in values.values() for value in shard_values.values()
        }
        assert actual == expected
        assert not find_port_conflicts(claims)


def test_atomic_lan_port_rewrite_is_focused() -> None:
    from dstools.shared.ini_parser import parse_cluster_ini, parse_server_ini
    from dstools.shared import server_ports

    with tempfile.TemporaryDirectory() as tmp:
        cluster = _write_cluster(
            Path(tmp), "Cluster_A", lan_only=True,
            master_game_port=10002, caves_game_port=10001,
            master_server_port=27020, master_auth_port=8770,
        )
        before_cluster = parse_cluster_ini(cluster.path / "cluster.ini")
        before_shards = {
            shard.name: parse_server_ini(shard.path / "server.ini")
            for shard in cluster.shards
        }
        with patch.object(
            server_ports, "data_dir",
            side_effect=AssertionError("LAN 调整不应创建持久备份"),
        ):
            values = rewrite_lan_server_ports_atomic(
                cluster, {LAN_SERVER_PORT_MIN},
            )
        assert values["Master"] == LAN_SERVER_PORT_FALLBACK
        assert len(set(values.values())) == len(cluster.shards)
        after_cluster = parse_cluster_ini(cluster.path / "cluster.ini")
        assert after_cluster == before_cluster, "LAN 专项修复不应改写 cluster.ini"
        for shard in cluster.shards:
            after = parse_server_ini(shard.path / "server.ini")
            before = before_shards[shard.name]
            assert after.network["server_port"] == values[shard.name]
            assert after.steam == before.steam
            assert after.shard == before.shard


def test_atomic_lan_port_rewrite_rolls_back() -> None:
    from dstools.features.cluster_config.config_manager import load_cluster_config
    from dstools.shared import server_ports

    with tempfile.TemporaryDirectory() as tmp:
        cluster = _write_cluster(
            Path(tmp), "Cluster_A", lan_only=False,
            master_game_port=10002, caves_game_port=10001,
        )
        proposed = load_cluster_config(cluster.path)
        proposed.network["lan_only_cluster"] = True
        targets = [cluster.path / "cluster.ini"] + [
            shard.path / "server.ini" for shard in cluster.shards
        ]
        originals = {path: path.read_bytes() for path in targets}
        real_replace = server_ports.os.replace
        temp_replaces = 0

        def fail_second_temp_replace(source, target):
            nonlocal temp_replaces
            if str(source).endswith(".tmp"):
                temp_replaces += 1
                if temp_replaces == 2:
                    raise OSError("simulated replace failure")
            return real_replace(source, target)

        with patch.object(server_ports.os, "replace", side_effect=fail_second_temp_replace):
            try:
                rewrite_lan_server_ports_atomic(
                    cluster, set(),
                    cluster_config_override=proposed,
                )
            except OSError:
                pass
            else:
                raise AssertionError("部分替换失败时必须向调用方报告")
        assert all(path.read_bytes() == originals[path] for path in targets)


def test_local_service_batch_preflight() -> None:
    from dstools.qt.pages import local_service as local_page

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        running_cluster = _write_cluster(root, "Running")
        target = _write_cluster(root, "Target")

        running_proc = SimpleNamespace(
            cluster_path=running_cluster.path, cluster_name=running_cluster.name,
            shard_name="Master", proc=None,
        )
        manager = SimpleNamespace(running=lambda: [running_proc])
        service = _local_page([running_cluster, target], manager=manager, mapping="sakura")

        with _no_steam_update(), \
                patch.object(local_page, "scan_udp_ports", return_value=UdpPortScan(True, {})), \
                patch.object(local_page.dialogs, "ask_choice", return_value="cancel"):
            # 用户选择“否”（取消），预检必须拦截这次启动。
            assert not service._preflight_start(target, target.shards)

            running_claims, _ = collect_cluster_port_claims(running_cluster)
            rewrite_cluster_ports_atomic(
                target, {claim.port for claim in running_claims}, create_backup=False,
            )
            assert service._preflight_start(target, target.shards)

            # 重启是在停服前做预检：当前目标进程自己的端口应被排除，不能
            # 把它误报成外部占用；普通启动模式下同一证据仍必须报冲突。
            running_proc.proc = SimpleNamespace(pid=4321)
            master_claims, _ = collect_cluster_port_claims(running_cluster, ["Master"])
            own_ports = frozenset(claim.port for claim in master_claims if claim.binding)
            with patch.object(local_page, "scan_udp_ports", return_value=UdpPortScan(True, {4321: own_ports})):
                assert not service._preflight_start(running_cluster, running_cluster.shards)
                assert service._preflight_start(running_cluster, running_cluster.shards, restarting=True)


def test_local_service_lan_preflight_repair() -> None:
    from dstools.qt.pages import local_service as local_page
    from dstools.shared.ini_parser import parse_server_ini

    with tempfile.TemporaryDirectory() as tmp:
        cluster = _write_cluster(
            Path(tmp), "LAN_Invalid", lan_only=True,
            master_game_port=10002, caves_game_port=10001,
        )
        service = _local_page([cluster])

        with _no_steam_update(), \
                patch.object(local_page, "scan_udp_ports", return_value=UdpPortScan(True, {})), \
                patch.object(local_page.dialogs, "ask_choice", return_value="allocate"), \
                patch.object(local_page.dialogs, "show_info"):
            assert service._preflight_start(cluster, cluster.shards)

        ports = {
            parse_server_ini(shard.path / "server.ini").network["server_port"]
            for shard in cluster.shards
        }
        assert ports == {LAN_SERVER_PORT_MIN, LAN_SERVER_PORT_FALLBACK}


def test_local_service_lan_blocked_by_mapping_or_running() -> None:
    from dstools.qt.pages import local_service as local_page
    from dstools.shared.ini_parser import parse_cluster_ini

    def make_service(cluster, *, running):
        jumps = []
        proc = SimpleNamespace(cluster_path=cluster.path, shard_name="Master")
        manager = SimpleNamespace(running=lambda: [proc] if running else [])
        return _local_page([cluster], manager=manager, mapping="sakura", jumps=jumps), jumps

    def offline_flag(cluster) -> bool:
        return bool(parse_cluster_ini(cluster.path / "cluster.ini").network["offline_cluster"])

    with tempfile.TemporaryDirectory() as tmp, _no_steam_update():
        cluster = _write_cluster(
            Path(tmp), "Mapped_Offline", offline=True,
            master_game_port=39491, caves_game_port=12256,
        )
        service, jumps = make_service(cluster, running=False)
        with patch.object(local_page, "scan_udp_ports", return_value=UdpPortScan(True, {})):
            # 取消：不改配置、不跳转，也不能落到只有“确认”的错误框。
            with patch.object(local_page.dialogs, "ask_choice", return_value="cancel") as ask, \
                    patch.object(local_page.dialogs, "show_error") as error:
                assert not service._preflight_start(cluster, cluster.shards)
                assert not error.called
                message = ask.call_args.args[2]
                assert "离线模式" in message and "39491" in message and "12256" in message
                assert [value for _label, value in ask.call_args.args[3]] == [
                    "disable", "goto", "cancel",
                ]
            assert offline_flag(cluster) and not jumps

            with patch.object(local_page.dialogs, "ask_choice", return_value="goto"):
                assert not service._preflight_start(cluster, cluster.shards)
            assert jumps == ["sakura"] and offline_flag(cluster)

            # 关闭离线模式后重新预检，不再有 LAN 端口问题。
            with patch.object(local_page.dialogs, "ask_choice", return_value="disable"):
                assert service._preflight_start(cluster, cluster.shards)
            assert not offline_flag(cluster)

        running_cluster = _write_cluster(
            Path(tmp), "Running_Offline", offline=True,
            master_game_port=39491, caves_game_port=12256,
        )
        service, _ = make_service(running_cluster, running=True)
        with patch.object(local_page.dialogs, "ask_choice") as ask, \
                patch.object(local_page.dialogs, "show_error") as error:
            assert not service._preflight_start(running_cluster, running_cluster.shards)
            assert error.called and not ask.called
        assert offline_flag(running_cluster)


def test_config_editor_lan_port_repair_and_lock() -> None:
    from dstools.features.cluster_config import save_checks
    from dstools.features.cluster_config.config_manager import load_cluster_config
    from dstools.qt.pages import server_config as config_page
    from dstools.shared.ini_parser import parse_server_ini

    with tempfile.TemporaryDirectory() as tmp:
        cluster = _write_cluster(
            Path(tmp), "LAN_Invalid", lan_only=False,
            master_game_port=10002, caves_game_port=10001,
        )
        running, mapping, jumps = [], [], []
        editor = config_page.ServerConfigPage.__new__(config_page.ServerConfigPage)
        editor._window = lambda: None
        editor.on_cluster_changed = lambda _cluster: None
        editor.ctx = SimpleNamespace(
            env=SimpleNamespace(clusters=[cluster]),
            cluster_running=lambda _cluster: bool(running),
            mapping_owner=lambda *_args: mapping[0] if mapping else None,
            goto_tab=jumps.append,
        )
        proposed = load_cluster_config(cluster.path)
        proposed.network["lan_only_cluster"] = True
        _, issues = collect_cluster_port_claims(
            cluster, cluster_config_override=proposed,
        )
        lan_issues = [issue for issue in issues if issue.code == "lan_server_port_range"]

        with patch.object(config_page, "scan_udp_ports", return_value=UdpPortScan(True, {})), \
                patch.object(config_page.dialogs, "ask_choice", return_value="allocate"), \
                patch.object(config_page.dialogs, "show_info"):
            assert editor._repair_lan_ports(cluster, proposed, lan_issues)
        assert load_cluster_config(cluster.path).network["lan_only_cluster"] is True
        ports = {
            parse_server_ini(shard.path / "server.ini").network["server_port"]
            for shard in cluster.shards
        }
        assert ports == {LAN_SERVER_PORT_MIN, LAN_SERVER_PORT_FALLBACK}

        # 存档有映射时，配置页不得跨文件重写端口，只能让用户改为关闭 LAN 限制。
        before = {
            shard.path: (shard.path / "server.ini").read_bytes()
            for shard in cluster.shards
        }
        mapping.append("sakura")
        assert save_checks.cluster_ports_locked(
            cluster, editor.ctx.cluster_running, lambda c, s: bool(editor.ctx.mapping_owner(c, s)),
        )
        with patch.object(config_page.dialogs, "ask_choice", return_value="cancel"):
            assert not editor._resolve_lan_lock(cluster, proposed, lan_issues)
        with patch.object(config_page.dialogs, "ask_choice", return_value="goto"):
            assert not editor._resolve_lan_lock(cluster, proposed, lan_issues)
        assert jumps == ["sakura"] and proposed.network["lan_only_cluster"] is True
        with patch.object(config_page.dialogs, "ask_choice", return_value="disable") as ask:
            assert editor._resolve_lan_lock(cluster, proposed, lan_issues)
            assert "仅限局域网" in ask.call_args.args[2]
        assert proposed.network["lan_only_cluster"] is False
        assert proposed.network["offline_cluster"] is False
        assert all(
            (path / "server.ini").read_bytes() == content
            for path, content in before.items()
        )

        # 运行中只提示先停止，不提供修改类按钮。
        proposed.network["lan_only_cluster"] = True
        running.append(True)
        with patch.object(config_page.dialogs, "ask_choice") as ask, \
                patch.object(config_page.dialogs, "show_error") as error:
            assert not editor._resolve_lan_lock(cluster, proposed, lan_issues)
            assert error.called and not ask.called


def test_mapping_enable_guard_for_lan_saves() -> None:
    from dstools.qt.tunnel import lan_mapping_guard
    from dstools.shared.ini_parser import parse_cluster_ini

    with tempfile.TemporaryDirectory() as tmp:
        lan_cluster = _write_cluster(Path(tmp), "Offline_Save", offline=True)
        saved = []
        ctx = SimpleNamespace(cluster_config_saved=SimpleNamespace(emit=saved.append))

        with patch.object(lan_mapping_guard.dialogs, "ask_choice", return_value="cancel"):
            assert not lan_mapping_guard.ensure_lan_free_for_mapping(None, ctx, lan_cluster)
        assert parse_cluster_ini(lan_cluster.path / "cluster.ini").network["offline_cluster"]
        assert not saved

        with patch.object(lan_mapping_guard.dialogs, "ask_choice", return_value="disable"):
            assert lan_mapping_guard.ensure_lan_free_for_mapping(None, ctx, lan_cluster)
        assert not parse_cluster_ini(lan_cluster.path / "cluster.ini").network["offline_cluster"]
        assert saved == [lan_cluster], "关闭 LAN 限制后要通知其它页刷新"

        # 已经不带 LAN 限制的存档不弹任何窗口。
        with patch.object(lan_mapping_guard.dialogs, "ask_choice") as ask:
            assert lan_mapping_guard.ensure_lan_free_for_mapping(None, ctx, lan_cluster)
            assert not ask.called


def test_config_editor_effective_conflicts() -> None:
    from dstools.features.cluster_config import save_checks
    from dstools.features.cluster_config.config_manager import load_shard_config

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cluster = _write_cluster(root, "Cluster_A")
        other = _write_cluster(root, "Cluster_B")
        clusters = [cluster, other]
        master = next(shard for shard in cluster.shards if shard.name == "Master")
        config = load_shard_config(master.path)
        config.network["server_port"] = 10888
        assert save_checks.find_port_conflict(cluster, master, config)

        config.network["server_port"] = 10999
        warnings = save_checks.find_cross_cluster_port_conflicts(cluster, master, config, clusters)
        assert any("10999" in warning for warning in warnings)
        assert not any("10888" in warning for warning in warnings), (
            "保存 server.ini 时不得校验从 cluster.ini 继承的 master_port"
        )
        assert all(not warning.startswith("UDP ") for warning in warnings)
        assert any(
            warning.startswith("10999: ") and "Cluster_B/Master (server_port)" in warning
            for warning in warnings
        )

        cluster_warnings = save_checks.find_cross_cluster_cluster_port_conflicts(
            cluster, cluster.config, clusters,
        )
        assert any(
            warning.startswith("10888: ")
            and "Cluster_A (master_port)" in warning
            and "Cluster_B (master_port)" in warning
            for warning in cluster_warnings
        ), "保存 cluster.ini 时必须校验 master_port"


def test_config_editor_port_ranges() -> None:
    from dstools.features.cluster_config import save_checks
    from dstools.features.cluster_config.ini_field_info import get_range_limits

    port_fields = (
        ("SHARD", "master_port"),
        ("NETWORK", "server_port"),
        ("STEAM", "master_server_port"),
        ("STEAM", "authentication_port"),
    )
    assert all(get_range_limits(*field) == (1, 65535) for field in port_fields)

    def check(section, key, value, *, shard):
        return save_checks.validate_ranges({(section, key): value}, shard=shard) is None

    assert check("SHARD_NETWORK", "server_port", "1", shard=True)
    assert check("SHARD_NETWORK", "server_port", "65535", shard=True)
    assert not check("SHARD_NETWORK", "server_port", "0", shard=True)
    assert not check("SHARD_NETWORK", "server_port", "65536", shard=True)
    assert not check("SHARD_NETWORK", "server_port", "-1", shard=True)
    assert check("SHARD_STEAM", "master_server_port", "", shard=True), (
        "可选 Steam 端口仍应允许留空"
    )
    assert check("SHARD", "master_port", "10888", shard=False)
    assert not check("SHARD", "master_port", "abc", shard=False)


def test_world_creation_port_conflict_choices() -> None:
    from dstools.features.world.creation import default_cluster_config, default_shard_config
    from dstools.qt.world import creation_wizard

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        existing = _write_cluster(root, "Existing")
        tab = creation_wizard.CreationWizardDialog.__new__(creation_wizard.CreationWizardDialog)
        tab.ctx = SimpleNamespace(env=SimpleNamespace(clusters=[existing]))
        tab._live_fixed_shards = lambda: ("Master", "Caves")
        tab._extra_plans = {}

        def prepare(choice):
            cluster_ini = default_cluster_config("New")
            shard_configs = {
                "Master": default_shard_config(True),
                "Caves": default_shard_config(False),
            }
            with patch.object(creation_wizard.dialogs, "ask_choice", return_value=choice), \
                    patch.object(creation_wizard, "scan_udp_ports", return_value=UdpPortScan(True, {})):
                result = tab._prepare_unique_creation_ports(
                    "New", root / "New", cluster_ini, shard_configs,
                )
            return result, cluster_ini, shard_configs

        result, cluster_ini, shard_configs = prepare("create")
        assert result
        assert cluster_ini.shard["master_port"] == 10888
        assert shard_configs["Master"].network["server_port"] == 10999
        assert shard_configs["Caves"].network["server_port"] == 10998

        result, cluster_ini, shard_configs = prepare("cancel")
        assert not result
        assert cluster_ini.shard["master_port"] == 10888
        assert shard_configs["Master"].network["server_port"] == 10999

        result, cluster_ini, shard_configs = prepare("allocate")
        assert result
        allocated = {
            cluster_ini.shard["master_port"],
            *(value for config in shard_configs.values()
              for value in (
                  config.network["server_port"],
                  config.steam["master_server_port"],
                  config.steam["authentication_port"],
              )),
        }
        assert len(allocated) == 7
        assert not allocated & {10888, 10998, 10999, 27016, 27017, 8766, 8767}

        # 离线存档：10998..11018 被占满时提示无法分配，不能分到范围外（游戏会退回 10999 再次冲突）
        def prepare_offline(busy_ports):
            cluster_ini = default_cluster_config("New")
            cluster_ini.network["offline_cluster"] = True
            shard_configs = {}
            warnings = []
            with patch.object(creation_wizard.dialogs, "ask_choice", return_value="allocate"), \
                    patch.object(creation_wizard.dialogs, "show_warning",
                                 side_effect=lambda *args: warnings.append(args)), \
                    patch.object(creation_wizard, "scan_udp_ports",
                                 return_value=UdpPortScan(True, {1: frozenset(busy_ports)})):
                result = tab._prepare_unique_creation_ports("New", root / "New", cluster_ini, shard_configs)
            return result, shard_configs, warnings

        result, shard_configs, warnings = prepare_offline(range(11000, 11017))
        assert result and not warnings
        assert {config.network["server_port"] for config in shard_configs.values()} == {11017, 11018}
        result, _, warnings = prepare_offline(range(11000, 11019))
        assert not result and len(warnings) == 1


def test_single_world_master_port_is_not_bound() -> None:
    """实测：shard_enabled=false 的单世界不绑定 master_port，不能和别的存档算作冲突。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        multi = _write_cluster(root, "Multi")
        single = _write_cluster(root, "Single", caves=False, master_game_port=11005)
        ini = single.path / "cluster.ini"
        ini.write_text(ini.read_text(encoding="utf-8").replace("shard_enabled=true", "shard_enabled=false"),
                       encoding="utf-8")
        claims = [*collect_cluster_port_claims(multi)[0], *collect_cluster_port_claims(single)[0]]
        assert find_port_conflicts(claims) == []
        # 分配新端口时仍要避开，以后改成多世界也不会撞
        assert any(claim.field == "master_port" and claim.port == DEFAULT_MASTER_PORT
                   for claim in collect_cluster_port_claims(single)[0])


def test_server_manager_rejects_duplicate_start() -> None:
    from dstools.features.local_service import dedicated_server

    calls = []
    old_start = dedicated_server.ServerProcess.start
    try:
        dedicated_server.ServerProcess.start = lambda process: calls.append(process)
        manager = dedicated_server.ServerManager()
        first = manager.start(
            "Cluster_A", Path("C:/saves/Cluster_A"), "Master",
            Path("C:/dst"), None,
        )
        second = manager.start(
            "Cluster_A", Path("C:/saves/Cluster_A"), "Master",
            Path("C:/dst"), None,
        )
        assert first is second
        assert len(calls) == 1
    finally:
        dedicated_server.ServerProcess.start = old_start


def test_restart_all_preserves_stopped_shards_and_rejects_transitions() -> None:
    from dstools.features.local_service.dedicated_server import ServerStatus

    with tempfile.TemporaryDirectory() as tmp:
        cluster = _write_cluster(Path(tmp), "Cluster_A")
        statuses = {
            "Master": ServerStatus.RUNNING,
            "Caves": ServerStatus.STOPPED,
        }
        service = _local_page([cluster])
        service.get_cluster = lambda: cluster
        service.manager = SimpleNamespace(
            get=lambda _path, name: SimpleNamespace(status=statuses[name])
        )
        calls = []
        service._restart_shards = lambda current, shards: calls.append(
            (current, [shard.name for shard in shards])
        )

        service._restart_all()
        assert calls == [(cluster, ["Master"])]

        statuses["Caves"] = ServerStatus.STARTING
        calls.clear()
        service._restart_all()
        assert not calls, "存在启动中或停止中的分片时不得批量重启"


def test_restart_stop_barrier_waits_for_every_shard() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cluster = _write_cluster(Path(tmp), "Cluster_A")
        service = _local_page([cluster])
        callbacks = {}
        service._stop_and_then = lambda _cluster, shard, callback: callbacks.setdefault(
            shard.name, callback
        )
        completed = []

        service._stop_shards_and_then(cluster, cluster.shards, lambda: completed.append(True))
        callbacks["Caves"]()
        assert not completed, "不能在部分分片停完时提前重新启动"
        callbacks["Master"]()
        assert completed == [True]


def test_restart_prepares_legacy_after_stop() -> None:
    """重启必须先停服，再部署 V1，最后才重新创建专服进程。"""
    from dstools.features.local_service.dedicated_server import ServerStatus
    from dstools.qt.pages import local_service as local_page

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cluster = _write_cluster(root, "Cluster_A", caves=False)
        shard = cluster.shards[0]
        service = _local_page([cluster])
        service.ctx.env.klei_root = root
        service.ctx.ensure_lobby_accel = lambda _cluster, callback: callback(True, "")
        service.manager = SimpleNamespace(
            get=lambda _path, _name: SimpleNamespace(status=ServerStatus.RUNNING)
        )
        service._restarting_keys = set()
        service._install_dir = root / "server"
        service._prepare_token_for_start = lambda _cluster: True
        events = []
        service._preflight_start = lambda *_args, **kwargs: events.append(
            ("preflight", kwargs.get("restarting"))
        ) or True
        service._stop_shards_and_then = lambda _cluster, _shards, callback: (
            events.append(("stopped", True)),
            callback(),
        )
        service._prepare_legacy_mods_for_start = lambda _cluster: events.append(
            ("prepared", True)
        ) or True
        service._continue_start_shard = lambda *_args: events.append(("started", True))
        service._select_master_console_tab = lambda _cluster: None
        service._refresh_shard_rows = lambda _cluster: None
        service.get_cluster = lambda: cluster
        service._update_restart_all_btn_state = lambda _cluster: None

        with patch.object(local_page.luajit_injector, "needs_regeneration", return_value=False), \
                patch.object(local_page, "resolve_storage_args", return_value=[]):
            service._restart_shards(cluster, [shard])

        assert events == [
            ("preflight", True),
            ("stopped", True),
            ("prepared", True),
            ("started", True),
        ]
        assert not service._restarting_keys, "重启完成后要清掉进行中标记"


def test_connect_code_display_masks_secrets() -> None:
    from dstools.shared.ini_parser import parse_cluster_ini, write_cluster_ini

    with tempfile.TemporaryDirectory() as tmp:
        cluster = _write_cluster(Path(tmp), "Cluster_A", caves=False)
        cluster_ini = cluster.path / "cluster.ini"
        config = parse_cluster_ini(cluster_ini)
        config.network["cluster_password"] = "secret123"
        write_cluster_ini(config, cluster_ini)
        service = _local_page([cluster])

        original, display = service._build_connect_strings(
            "203.0.113.42", 10999, cluster, mask_ipv4=True
        )
        assert '"203.0.113.42"' in original and '"secret123"' in original
        assert '"203.0.xx.xx"' in display and '"***"' in display
        assert "113.42" not in display and "secret123" not in display

        original, display = service._build_connect_strings(
            "example.com", 10999, cluster, mask_ipv4=True
        )
        assert '"example.com"' in original and '"example.com"' in display
        assert "secret123" not in display


def test_connect_code_waits_for_master_world_ready() -> None:
    """主世界进程刚创建时仍不可直连，消费到世界就绪标记后才算就绪。"""
    from dstools.features.local_service.dedicated_server import ServerStatus

    with tempfile.TemporaryDirectory() as tmp:
        cluster = _write_cluster(Path(tmp), "Cluster_A", caves=False)
        proc = SimpleNamespace(status=ServerStatus.STARTING, world_ready=False)
        service = _local_page([cluster])
        service.get_cluster = lambda: cluster
        service.manager = SimpleNamespace(get=lambda *_args: proc)

        assert service._master_ready() is False

        proc.status = ServerStatus.RUNNING
        assert service._master_ready() is False

        proc.world_ready = True
        assert service._master_ready() is True

        proc.status = ServerStatus.STOPPED
        assert service._master_ready() is False


def test_external_connect_status_rejects_lan_only() -> None:
    """仅局域网存档即使服务、IP 和 frpc 都正常，外部直连仍必须显示未就绪。"""
    from dstools.i18n import t
    from dstools.shared.ini_parser import parse_cluster_ini, write_cluster_ini

    with tempfile.TemporaryDirectory() as tmp:
        cluster = _write_cluster(
            Path(tmp), "Cluster_A", caves=False, lan_only=True,
        )
        service = _local_page([cluster])
        service.ctx.frpc_ready = lambda _cluster: True
        service.get_cluster = lambda: cluster
        service._master_ready = lambda: True
        service._public_code = "public-code"
        service._public_proxy_suspected = False
        service._nat_code = "nat-code"
        service._public_status_key = None
        service._nat_status_key = None
        service._lan_only_cache_key = None
        service._lan_only_cache_value = False
        public_status = []
        nat_status = []
        service._public_row = SimpleNamespace(
            set_status=lambda *args: public_status.append(args), set_value=lambda *_args: None,
        )
        service._nat_row = SimpleNamespace(
            set_status=lambda *args: nat_status.append(args), set_value=lambda *_args: None,
        )

        service._refresh_public_status(ip_available=True)
        service._refresh_nat_status()

        assert service._public_status_key == "lan_only"
        assert service._nat_status_key == "lan_only"
        assert t("local.connect_not_ready") in public_status[-1][0]
        assert t("local.external_lan_only_reason") == public_status[-1][2]
        assert t("local.connect_not_ready") in nat_status[-1][0]
        assert t("local.external_lan_only_reason") == nat_status[-1][2]

        # 穿透映射的异步结果首次回填时也要直接显示 LAN 限制，不能先短暂
        # 闪成“已就绪”，等下一次轮询才纠正。
        service._connect_row = SimpleNamespace(isVisible=lambda: True)
        service._nat_status_key = None
        service._apply_nat_result(
            ("c_connect('example.com', 11000)", "masked"),
            str(cluster.path),
        )
        assert service._nat_status_key == "lan_only"
        assert t("local.external_lan_only_reason") == nat_status[-1][2]

        # 同一路径保存关闭 LAN 后，mtime/大小签名变化应让状态立即恢复，
        # 不能要求用户再手动刷新一次本地服务页。
        path = cluster.path / "cluster.ini"
        config = parse_cluster_ini(path)
        config.network["lan_only_cluster"] = False
        write_cluster_ini(config, path)
        service._public_status_key = None
        service._nat_status_key = None
        service._refresh_public_status(ip_available=True)
        service._refresh_nat_status()
        assert service._public_status_key == "ready"
        assert service._nat_status_key == "ready"


def test_local_refresh_redetects_server_tool() -> None:
    """F5/刷新全部必须重新探测专用服务器工具，而不只刷新存档列表。"""
    service = _local_page()
    calls = []
    service._detect_install_dir = lambda: calls.append("detect_install")
    service._on_wegame_detect = lambda: calls.append("detect_wegame")

    service._on_env_refreshed()
    assert calls == ["detect_install", "detect_wegame"]


def test_nat_without_configuration_skips_loading_and_network_thread() -> None:
    """没有樱花映射和自建映射时应立即显示未映射，只发起公网 IP 查询。"""
    from dstools.i18n import t
    from dstools.qt.pages import local_service as local_page

    with tempfile.TemporaryDirectory() as tmp:
        cluster = _write_cluster(Path(tmp), "Cluster_A", caves=False)
        service = _local_page([cluster])
        service.get_cluster = lambda: cluster
        service._master_ready = lambda: False
        service._public_gen = service._nat_gen = 0
        service._lan_connect_code = lambda _cluster: None
        service._refresh_lan_status = lambda: None
        silent = SimpleNamespace(set_value=lambda *_args: None, set_status=lambda *_args: None)
        service._lan_row = silent
        service._public_row = silent
        nat_text = []
        nat_status = []
        service._nat_row = SimpleNamespace(
            set_value=lambda *args: nat_text.append(args),
            set_status=lambda *args: nat_status.append(args),
        )
        started = []

        with patch.object(local_page, "get_selfhost_frp_server", return_value=None), \
                patch.object(local_page, "run_async", side_effect=lambda work, done, error=None: started.append(work)):
            service._refresh_connect_labels()

        assert nat_text[-1] == (t("local.nat_not_mapped_short"),)
        assert t("local.connect_not_ready") in nat_status[-1][0]
        assert service._nat_status_key == "nomap"
        assert len(started) == 1, "只应发起公网 IP 查询，不能为穿透代码再起后台任务"


def test_saved_sakura_token_without_local_mapping_skips_lookup() -> None:
    """仅保存过 Token 不代表当前存档有映射，不能因此进入网络等待。"""
    from dstools.qt.pages import local_service as local_page

    with tempfile.TemporaryDirectory() as tmp:
        cluster = _write_cluster(Path(tmp), "Cluster_A", caves=False)
        service = _local_page([cluster])

        with patch.object(local_page, "get_sakura_token", return_value="old-token"), \
                patch.object(local_page, "get_selfhost_frp_server", return_value=None):
            assert service._nat_lookup_needed(cluster) is False


def test_nat_without_matching_sakura_tunnel_skips_nodes_request() -> None:
    """樱花隧道列表没有当前存档时，不应继续等待节点列表。"""
    from dstools.qt.pages import local_service as local_page

    with tempfile.TemporaryDirectory() as tmp:
        cluster = _write_cluster(Path(tmp), "Cluster_A", caves=False)
        service = _local_page([cluster])

        with patch.object(local_page, "get_sakura_token", return_value="token"), \
                patch.object(local_page.sakura_frp, "list_tunnels", return_value=[]), \
                patch.object(local_page.sakura_frp, "list_nodes") as list_nodes, \
                patch.object(local_page, "get_selfhost_frp_server", return_value=None):
            assert service._nat_connect_info(cluster, "sakura") == (None, None)

        list_nodes.assert_not_called()


def test_public_ipv4_prefers_cip_cc_plain_text() -> None:
    """公网 IP 查询绕过系统代理，并能解析 cip.cc 的纯文本格式。"""
    from dstools.qt.pages import local_service as local_page

    calls = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        @staticmethod
        def read(size):
            assert size == 256
            return "IP\t: 203.0.113.42\n地址\t: 中国 广东".encode("utf-8")

    class FakeOpener:
        @staticmethod
        def open(request, *, timeout):
            calls.append((request.full_url, request.get_header("User-agent"), timeout))
            assert request.full_url == "https://cip.cc/"
            return FakeResponse()

    def fake_build_opener(*handlers):
        # 必须显式禁用系统代理，否则开着代理软件时查到的是代理出口 IP。
        proxy_handlers = [h for h in handlers if isinstance(h, local_page.urllib.request.ProxyHandler)]
        assert len(proxy_handlers) == 1 and proxy_handlers[0].proxies == {}
        return FakeOpener()

    with patch.object(local_page.urllib.request, "build_opener", fake_build_opener):
        assert local_page._fetch_public_ipv4() == "203.0.113.42"

    assert len(calls) == 1


if __name__ == "__main__":
    run(globals())
