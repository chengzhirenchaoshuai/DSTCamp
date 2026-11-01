"""frpc 缺失/被安全软件拦截的诊断（樱花、自建与 Lolia）和配置前预检。"""

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dstools.features.sakura import frpc_recovery
from dstools.qt.pages import sakura as sakura_page
from _harness import run  # noqa: E402


def test_inspect_distinguishes_ready_and_missing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        client = root / "frpc-sakura" / "sakura-frpc.exe"
        with patch.object(frpc_recovery, "runtime_tool_path", return_value=client):
            missing = frpc_recovery.inspect_sakura_frpc()
            assert missing.status == "missing" and missing.path == client

            client.parent.mkdir(parents=True)
            client.write_bytes(b"frpc")
            ready = frpc_recovery.inspect_sakura_frpc()
            assert ready.ready and ready.path == client

        # 发布版不打包客户端：哪里都没有副本时是"未下载"，不是被隔离
        not_bundled = root / "bundle-tools"
        with (
            patch.object(frpc_recovery, "tool_binary_dir", return_value=not_bundled),
            patch.object(frpc_recovery, "runtime_tool_path",
                         return_value=not_bundled / "frpc-sakura" / "sakura-frpc.exe"),
        ):
            assert frpc_recovery.inspect_sakura_frpc().status == "not_downloaded"


def test_security_block_has_a_structured_status() -> None:
    client = Path("C:/DSTCamp/tools/frpc-sakura/sakura-frpc.exe")
    blocked = OSError("blocked by security software")
    blocked.winerror = 225
    with patch.object(
        frpc_recovery,
        "runtime_tool_path",
        side_effect=blocked,
    ), patch.object(
        frpc_recovery,
        "tool_binary_dir",
        return_value=Path("C:/DSTCamp/tools"),
    ):
        health = frpc_recovery.inspect_sakura_frpc()
    assert health.status == "blocked" and health.path == client


def test_enable_mapping_aborts_before_remote_changes_when_client_is_missing() -> None:
    cluster = SimpleNamespace(shards=[], path=Path("C:/Cluster"))
    page = SimpleNamespace(
        _current_cluster=cluster,
        _selected_node_id=1,
        _running_shard_names=lambda _cluster: [],
        ctx=SimpleNamespace(mapping_owner=lambda _cluster, _shard: None),
        _ensure_frpc_available=lambda **_kwargs: None,
        _enable_mapping=lambda: None,
        window=lambda: None,
    )
    with patch.object(
        sakura_page.app_settings, "get_sakura_token", return_value="token",
    ), patch.object(sakura_page.sakura_frp, "list_tunnels") as list_tunnels, patch.object(
        sakura_page.dialogs, "LogDialog",
    ) as progress:
        sakura_page._SakuraMappingPanel._enable_mapping(page)
    list_tunnels.assert_not_called()
    progress.assert_not_called()


def test_enable_mapping_rolls_back_when_a_later_shard_fails() -> None:
    """主世界隧道建好、洞穴失败（如端口被占用）：撤销主世界新建的隧道，两个世界的 server.ini 原样不动。"""
    from unittest.mock import MagicMock

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        shards = []
        for name, port in (("Master", 10999), ("Caves", 10998)):
            (root / "Cluster_1" / name).mkdir(parents=True)
            (root / "Cluster_1" / name / "server.ini").write_text(
                f"[NETWORK]\nserver_port = {port}\n\n[SHARD]\nis_master = {str(name == 'Master').lower()}\n",
                encoding="utf-8")
            shards.append(SimpleNamespace(name=name, path=root / "Cluster_1" / name))
        originals = {s.name: (s.path / "server.ini").read_bytes() for s in shards}
        cluster = SimpleNamespace(shards=shards, path=root / "Cluster_1",
                                  source=SimpleNamespace(value="server"), platform=SimpleNamespace(value="steam"))
        page = SimpleNamespace(
            _current_cluster=cluster, _selected_node_id=1,
            _running_shard_names=lambda _cluster: [],
            _ensure_frpc_available=lambda **_kwargs: object(),
            _enable_mapping=lambda: None,
            _find_cluster_tunnel=lambda *_args: None,
            _reload_async=lambda: None,
            ctx=SimpleNamespace(env=None, cluster_config_saved=SimpleNamespace(emit=lambda _c: None)),
            window=lambda: None,
        )
        port_dialog = MagicMock()
        port_dialog.return_value.exec.return_value = True
        port_dialog.return_value.result_ports = {"Master": 20001, "Caves": 20002}
        created = iter([{"id": 1, "remote": "20001"},
                        sakura_page.sakura_frp.SakuraApiError("远程端口 UDP:20002 已被占用")])

        def create_tunnel(*_args, **_kwargs):
            result = next(created)
            if isinstance(result, Exception):
                raise result
            return result

        errors = []

        def run_sync(work, done, error):
            try:
                done(work())
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                error(exc)

        with (
            patch.object(sakura_page.app_settings, "get_sakura_token", return_value="token"),
            patch.object(sakura_page, "ensure_no_other_mapping", return_value=True),
            patch.object(sakura_page, "ensure_lan_free_for_mapping", return_value=True),
            patch.object(sakura_page.dialogs, "RemotePortDialog", port_dialog),
            patch.object(sakura_page.dialogs, "LogDialog"),
            patch.object(sakura_page, "run_async", run_sync),
            patch.object(sakura_page, "post_to_ui", lambda fn: fn(None)),
            patch.object(sakura_page, "_frpc_pointer_path",
                         lambda _path, shard, _env: root / "pointers" / f"{shard}.txt"),
            patch.object(sakura_page.sakura_frp, "list_tunnels", return_value=[]),
            patch.object(sakura_page.sakura_frp, "create_tunnel", side_effect=create_tunnel),
            patch.object(sakura_page.sakura_frp, "edit_tunnel"),
            patch.object(sakura_page.sakura_frp, "delete_tunnel") as delete_tunnel,
        ):
            sakura_page._SakuraMappingPanel._enable_mapping(page)

        assert len(errors) == 1
        delete_tunnel.assert_called_once_with("token", [1])
        assert {s.name: (s.path / "server.ini").read_bytes() for s in shards} == originals
        assert not (root / "pointers").exists() or not any((root / "pointers").iterdir())


def test_selfhost_frpc_marks_security_blocks() -> None:
    """文件不见了、或系统以"含病毒"拒绝执行（WinError 225）算被拦截；其它启动错误不算。"""
    from dstools.features.frp_selfhost import client

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        missing = client.FrpcProcess(root, root / "frpc.exe", root / "frpc.toml")
        missing.start()
        assert missing.status == client.FrpcStatus.CRASHED and missing.blocked

        exe = root / "frpc.exe"
        exe.write_bytes(b"frpc")
        virus = OSError("含病毒")
        virus.winerror = 225
        for error, blocked in ((virus, True), (OSError("其它错误"), False)):
            proc = client.FrpcProcess(root, exe, root / "frpc.toml")
            with patch.object(client.subprocess, "Popen", side_effect=error):
                proc.start()
            assert proc.status == client.FrpcStatus.CRASHED and proc.blocked is blocked


if __name__ == "__main__":
    run(globals())
