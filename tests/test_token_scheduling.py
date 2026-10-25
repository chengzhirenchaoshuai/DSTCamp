"""新旧服务器令牌分类和多存档分配规则。"""

import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import run  # noqa: E402

from dstools.features.local_service.token_scheduler import (
    TokenUse,
    select_token_for_cluster,
)
from dstools.shared.token_manager import (
    ServerTokenKind,
    classify_token,
    token_fingerprint,
)


OLD_A = "pds-g^KU_" + "a" * 24 + "^" + "b" * 28
OLD_B = "pds-g^KU_" + "c" * 24 + "^" + "d" * 28
NEW_A = "pds-g^KU_" + "e" * 24 + "^" + "f" * 28 + "^" + "g" * 16
NEW_B = "pds-g^KU_" + "h" * 24 + "^" + "i" * 28 + "^" + "j" * 16
UNKNOWN = "custom-token-" + "x" * 30


def test_token_classification_and_selection() -> None:
    assert classify_token(OLD_A) == ServerTokenKind.OLD
    assert classify_token(NEW_A) == ServerTokenKind.NEW
    assert classify_token(UNKNOWN) == ServerTokenKind.UNKNOWN

    # 旧令牌可以跨存档复用。
    selected = select_token_for_cluster(
        current_token=OLD_A,
        pool=[OLD_A],
        target_cluster_key="B",
        active_uses=[TokenUse(OLD_A, "A", "Cluster_A")],
    )
    assert selected.token == OLD_A and not selected.changed

    # 新令牌不能给另一个存档复用，应自动选择池中的其它令牌。
    selected = select_token_for_cluster(
        current_token=NEW_A,
        pool=[NEW_A, NEW_B],
        target_cluster_key="B",
        active_uses=[TokenUse(NEW_A, "A", "Cluster_A")],
    )
    assert selected.token == NEW_B and selected.changed

    # 同一存档的 Master/Caves 共享一个租约，继续使用原新令牌。
    selected = select_token_for_cluster(
        current_token=NEW_A,
        pool=[NEW_A, NEW_B],
        target_cluster_key="A",
        active_uses=[TokenUse(NEW_A, "A", "Cluster_A")],
    )
    assert selected.token == NEW_A and not selected.changed

    # 崩溃后等待释放的新令牌必须跳过；没有其它令牌时明确返回不足。
    held = {token_fingerprint(NEW_A)}
    selected = select_token_for_cluster(
        current_token=NEW_A,
        pool=[NEW_A, OLD_B],
        target_cluster_key="B",
        held_fingerprints=held,
    )
    assert selected.token == OLD_B and selected.changed
    selected = select_token_for_cluster(
        current_token=NEW_A,
        pool=[NEW_A],
        target_cluster_key="B",
        held_fingerprints=held,
    )
    assert selected.token is None

    # 未知格式只尊重用户对当前存档的手工配置，不从池中自动分配。
    assert select_token_for_cluster(
        current_token=UNKNOWN, pool=[], target_cluster_key="A"
    ).token == UNKNOWN
    assert select_token_for_cluster(
        current_token="", pool=[UNKNOWN], target_cluster_key="A"
    ).token is None

    # 池外有效令牌属于存档私有配置，即使曾有冲突标记或被另一存档手工
    # 使用，也不能被全局池中的令牌静默替换。
    private = select_token_for_cluster(
        current_token=NEW_A,
        pool=[NEW_B],
        target_cluster_key="B",
        active_uses=[TokenUse(NEW_A, "A", "Cluster_A")],
        held_fingerprints=[token_fingerprint(NEW_A)],
    )
    assert private.token == NEW_A and not private.changed

    # DSTCamp 之外启动的进程也应通过真实 UDP 绑定映射到存档。
    from dstools.features.local_service import dedicated_server

    external_clusters = [
        SimpleNamespace(
            path=Path("Cluster_A"),
            shards=[SimpleNamespace(path=Path("Cluster_A/Master"))],
        ),
        SimpleNamespace(
            path=Path("Cluster_B"),
            shards=[SimpleNamespace(path=Path("Cluster_B/Master"))],
        ),
    ]
    with (
        patch.object(dedicated_server, "_find_dst_process_pids", return_value={7: 100.0}),
        patch.object(dedicated_server, "_udp_ports_by_pid", return_value={7: {10999}}),
        patch.object(dedicated_server, "load_shard_config", side_effect=lambda path: path),
        patch.object(
            dedicated_server, "get_shard_option",
            side_effect=lambda config, *_: 10999 if "Cluster_A" in str(config) else 11000,
        ),
    ):
        assert dedicated_server.detect_external_running_clusters(
            external_clusters
        ) == {"Cluster_A"}

    # 崩溃/冲突占用只按不可逆指纹持久化，并能随令牌池删除而清理。
    from dstools.shared import app_settings

    with tempfile.TemporaryDirectory() as settings_tmp, patch.dict(
        os.environ, {"APPDATA": settings_tmp}
    ):
        fingerprint = token_fingerprint(NEW_A)
        app_settings.set_token_hold(
            fingerprint,
            state="conflict",
            cluster_key="Cluster_A",
            cluster_name="Cluster_A",
            since=123.0,
        )
        holds = app_settings.get_token_holds()
        assert holds[fingerprint]["state"] == "conflict"
        settings_text = (
            Path(settings_tmp) / "DSTCamp" / "settings.json"
        ).read_text(encoding="utf-8")
        assert NEW_A not in settings_text
        app_settings.prune_token_holds([NEW_B])
        assert app_settings.get_token_holds() == {}

        # 超过等待上限的旧标记随清理删除，较新的保留。
        app_settings.set_token_hold(fingerprint, state="crashed", cluster_key="A", cluster_name="A", since=100.0)
        app_settings.prune_token_holds([NEW_A], expire_before=50.0)
        assert fingerprint in app_settings.get_token_holds()
        app_settings.prune_token_holds([NEW_A], expire_before=200.0)
        assert app_settings.get_token_holds() == {}

        crash_cluster = Path(settings_tmp) / "Cluster_Crash"
        crash_cluster.mkdir()
        from dstools.shared.token_manager import write_token
        write_token(crash_cluster / "cluster_token.txt", NEW_A)
        from dstools.qt.pages.local_service import LocalServicePage
        crash_service = LocalServicePage.__new__(LocalServicePage)
        crash_service._auto_restart = Mock()
        crash_service.manager = SimpleNamespace(running=lambda: [])
        crash_service._token_reservations = {str(crash_cluster): NEW_A}
        app_settings.set_global_tokens([NEW_A])
        # 验证 Master 诊断回调会持久化，并在后续明确注册成功后清除。
        proc = SimpleNamespace(
            is_master=True,
            cluster_path=crash_cluster,
            cluster_name="Cluster_Crash",
        )
        crash_service._on_server_failure(proc, SimpleNamespace(category="token_conflict"))
        assert app_settings.get_token_holds()[token_fingerprint(NEW_A)]["state"] == "conflict"
        crash_service._on_server_registered(proc)
        assert app_settings.get_token_holds() == {}

        # 注册前就失败的主世界在 Klei 端没有房间，不能锁住令牌；注册过再崩溃才记。
        unregistered = SimpleNamespace(is_master=True, cluster_path=crash_cluster,
                                       cluster_name="Cluster_Crash", registered=False)
        crash_service._on_server_failure(unregistered, SimpleNamespace(category="runtime"))
        assert app_settings.get_token_holds() == {}
        crash_service._on_server_failure(proc, SimpleNamespace(category="crash"))
        assert app_settings.get_token_holds()[token_fingerprint(NEW_A)]["state"] == "crashed"
        crash_service._on_server_registered(proc)
        assert app_settings.get_token_holds() == {}

        # E_ROWID_EXIST 只出现在洞穴时同样表示整个存档的令牌冲突
        cave_proc = SimpleNamespace(
            is_master=False,
            cluster_path=crash_cluster,
            cluster_name="Cluster_Crash",
        )
        crash_service._on_server_failure(
            cave_proc, SimpleNamespace(category="token_conflict")
        )
        assert app_settings.get_token_holds()[token_fingerprint(NEW_A)]["state"] == "conflict"
        crash_service._on_server_registered(proc)
        assert app_settings.get_token_holds() == {}

        # 池外私有令牌仍会显示冲突诊断，但不能形成界面不可见、启动又会
        # 读取的孤立锁定标记。
        write_token(crash_cluster / "cluster_token.txt", NEW_B)
        crash_service._on_server_failure(
            cave_proc, SimpleNamespace(category="token_conflict")
        )
        assert token_fingerprint(NEW_B) not in app_settings.get_token_holds()

    # 本地服务器页的启动入口应在 Popen 前写入替代令牌并建立预占。
    from dstools.qt.pages import local_service as local_module
    from dstools.qt.pages.local_service import LocalServicePage
    from dstools.shared.token_manager import read_token, write_token

    with tempfile.TemporaryDirectory() as tmp:
        cluster_path = Path(tmp) / "Cluster_B"
        cluster_path.mkdir()
        token_path = cluster_path / "cluster_token.txt"
        write_token(token_path, NEW_A)
        cluster = SimpleNamespace(
            path=cluster_path, name="Cluster_B", token_path=token_path,
        )
        service = LocalServicePage.__new__(LocalServicePage)
        service.window = lambda: None
        service._token_reservations = {}
        service.manager = SimpleNamespace(running=lambda: [])
        service.ctx = SimpleNamespace(env=SimpleNamespace(clusters=[cluster]))
        active = (TokenUse(NEW_A, "Cluster_A", "Cluster_A"),)
        service.token_usage_snapshot = Mock(return_value=active)
        with (
            patch.object(local_module, "load_cluster_config", return_value=SimpleNamespace(network={})),
            patch.object(local_module, "get_global_tokens", return_value=[NEW_A, NEW_B]),
            patch.object(local_module, "blocking_token_holds", return_value={}),
            patch.object(local_module, "prune_token_holds") as prune_holds,
            patch.object(local_module.dialogs, "show_toast") as toast,
        ):
            assert service._prepare_token_for_start(cluster)
        assert read_token(token_path) == NEW_B
        assert service._token_reservations[str(cluster_path)] == NEW_B
        assert prune_holds.call_args.args == ([NEW_A, NEW_B],)
        toast.assert_called_once()

        # 没有替代令牌时必须阻止启动，且不能改写存档当前令牌。
        write_token(token_path, NEW_A)
        service._token_reservations.clear()
        with (
            patch.object(local_module, "load_cluster_config", return_value=SimpleNamespace(network={})),
            patch.object(local_module, "get_global_tokens", return_value=[NEW_A]),
            patch.object(local_module, "blocking_token_holds", return_value={}),
            patch.object(local_module, "prune_token_holds"),
            patch.object(local_module.dialogs, "show_warning") as warning,
        ):
            assert not service._prepare_token_for_start(cluster)
        assert read_token(token_path) == NEW_A
        assert service._token_reservations == {}
        warning.assert_called_once()

        # 当前新令牌不属于池时先收进令牌池并提示（冲突后才有等待记录和一键换用），存档令牌不变。
        write_token(token_path, NEW_A)
        with (
            patch.object(local_module, "load_cluster_config", return_value=SimpleNamespace(network={})),
            patch.object(local_module, "get_global_tokens", return_value=[NEW_B]),
            patch.object(local_module, "set_global_tokens") as adopt,
            patch.object(local_module, "blocking_token_holds", return_value={}),
            patch.object(local_module, "prune_token_holds"),
            patch.object(local_module.dialogs, "show_toast") as toast,
        ):
            assert service._prepare_token_for_start(cluster)
        assert read_token(token_path) == NEW_A
        adopt.assert_called_once_with([NEW_B, NEW_A])
        toast.assert_called_once()

        # 原令牌仍在等待期且池里有替代：手动启动先询问，选"换用"才换，选"仍用原令牌"保持原值。
        held_a = {token_fingerprint(NEW_A): {"state": "crashed", "retry_at": time.time() + 300}}
        for choice, expected in (("switch", NEW_B), ("retry", NEW_A)):
            write_token(token_path, NEW_A)
            service.token_usage_snapshot = Mock(return_value=())
            with (
                patch.object(local_module, "load_cluster_config", return_value=SimpleNamespace(network={})),
                patch.object(local_module, "get_global_tokens", return_value=[NEW_A, NEW_B]),
                patch.object(local_module, "blocking_token_holds", return_value=held_a),
                patch.object(local_module, "prune_token_holds"),
                patch.object(local_module.dialogs, "ask_choice", return_value=choice) as ask,
                patch.object(local_module.dialogs, "show_toast"),
            ):
                assert service._prepare_token_for_start(cluster)
                ask.assert_called_once()
            assert read_token(token_path) == expected

            # 自动重启在阈值前不换令牌，也不改写存档令牌。
            write_token(token_path, NEW_A)
            with (
                patch.object(local_module, "load_cluster_config", return_value=SimpleNamespace(network={})),
                patch.object(local_module, "get_global_tokens", return_value=[NEW_A, NEW_B]),
                patch.object(local_module, "blocking_token_holds", return_value=held_a),
                patch.object(local_module, "prune_token_holds"),
            ):
                assert not service._choose_start_token(cluster, allow_switch=False)
                assert service._choose_start_token(cluster) == "changed"
            assert read_token(token_path) == NEW_B



def test_auto_restart_rules_and_hold_retry_window() -> None:
    """自动重启只针对跑起来后崩溃的世界并限流；令牌等待标记过了重试时间就放行。"""
    from dstools.features.local_service import auto_restart
    from dstools.shared import app_settings

    assert auto_restart.is_restartable("unknown", world_ready=True, auto_attempt=False)
    assert not auto_restart.is_restartable("unknown", world_ready=False, auto_attempt=False), "手动启动就失败不重启"
    assert auto_restart.is_restartable("mod_conflict", world_ready=False, auto_attempt=True)
    assert not auto_restart.is_restartable("port", world_ready=True, auto_attempt=True)
    assert not auto_restart.is_restartable("token_conflict", world_ready=True, auto_attempt=True)

    budget = auto_restart.CrashBudget()
    assert all(budget.allow(100.0 + i) for i in range(auto_restart.MAX_CRASH_RESTARTS))
    assert not budget.allow(110.0), "时间窗内超过次数上限必须停止自动重启"
    assert budget.allow(100.0 + auto_restart.CRASH_WINDOW + 1), "旧崩溃滑出时间窗后恢复"

    assert auto_restart.TOKEN_HOLD_DURATION >= 25 * 60, "实测强杀后约 25 分钟才释放"
    assert app_settings.TOKEN_SWITCH_MINUTES_RANGE[1] * 60 < auto_restart.TOKEN_WAIT_LIMIT

    with tempfile.TemporaryDirectory() as settings_tmp, patch.dict(os.environ, {"APPDATA": settings_tmp}):
        fingerprint = token_fingerprint(NEW_A)
        app_settings.set_token_hold(fingerprint, state="crashed", cluster_key="A", cluster_name="A",
                                    since=1000.0, retry_at=1300.0, failures=0)
        assert fingerprint in app_settings.blocking_token_holds(1299.0)
        assert fingerprint not in app_settings.blocking_token_holds(1300.0), "过了重试时间要允许再试"
        assert app_settings.get_token_holds()[fingerprint]["failures"] == 0

        assert not app_settings.get_auto_restart_enabled("C:/saves/A")
        app_settings.set_auto_restart_enabled("C:/saves/A", True)
        assert app_settings.get_auto_restart_enabled("C:/saves/A")
        app_settings.set_auto_restart_enabled("C:/saves/A", False)
        assert not app_settings.get_auto_restart_enabled("C:/saves/A")

        assert app_settings.get_token_switch_on_timeout(), "超时换令牌默认开启"
        app_settings.set_token_switch_on_timeout(False)
        assert not app_settings.get_token_switch_on_timeout()

        assert app_settings.get_token_switch_after_minutes() == app_settings.TOKEN_SWITCH_MINUTES_DEFAULT
        low, high = app_settings.TOKEN_SWITCH_MINUTES_RANGE
        assert app_settings.set_token_switch_after_minutes(60) == 60 == app_settings.get_token_switch_after_minutes()
        assert app_settings.set_token_switch_after_minutes(-5) == low, "低于下限按下限保存"
        assert app_settings.set_token_switch_after_minutes(999) == high == app_settings.get_token_switch_after_minutes()


def test_auto_restart_controller_flow() -> None:
    """用假页面驱动真实调度器：直接触发定时器回调，覆盖冲突保持运行、超时换令牌、等令牌、成功、限流与取消。"""
    from PySide6.QtCore import QCoreApplication

    from dstools.features.local_service.auto_restart import (
        MAX_CRASH_RESTARTS, TOKEN_BUSY_RECHECK, TOKEN_WAIT_LIMIT,
    )
    from dstools.features.local_service.dedicated_server import ServerStatus
    from dstools.qt import auto_restart as controller_module

    app = QCoreApplication.instance() or QCoreApplication([])  # QTimer 需要应用对象，保持引用
    assert app is not None

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cluster = SimpleNamespace(name="Cluster_A", path=root / "Cluster_A", token_path=None,
                                  shards=[SimpleNamespace(name="Master"), SimpleNamespace(name="Caves")])
        cluster.path.mkdir()
        procs = {}
        events = []
        tokens = {"available": True}
        choose_calls = []
        switch_calls = []
        opts = {"switch": True, "alternative": NEW_B, "minutes": 30, "hold": None}

        def make_proc(name, *, ready=True, status=ServerStatus.RUNNING):
            procs[name] = SimpleNamespace(cluster_path=cluster.path, shard_name=name, is_master=name == "Master",
                                          world_ready=ready, status=status)
            return procs[name]

        def stop_then(_cluster, shards, on_done):
            for shard in shards:
                procs.pop(shard.name, None)
            events.append(("stop", tuple(s.name for s in shards)))
            on_done()

        page = SimpleNamespace(
            ctx=SimpleNamespace(env=SimpleNamespace(clusters=[cluster], klei_root=root),
                                ensure_lobby_accel=lambda _c, callback: callback(True, "")),
            manager=SimpleNamespace(get=lambda _path, name: procs.get(name)),
            _cluster_for_running_process=lambda _proc, _current: cluster,
            _master_shard=lambda _c: cluster.shards[0],
            _stop_shards_and_then=stop_then,
            _choose_start_token=lambda _c, allow_switch=True, retry_current=False: (
                choose_calls.append((allow_switch, retry_current)), tokens["available"])[1],
            _alternative_start_token=lambda _c: opts["alternative"],
            _switch_to_alternative_token=lambda _c: (switch_calls.append(1), bool(opts["alternative"]))[1],
            _current_token_hold=lambda _c: opts["hold"],
            _install_dir=root, _detect_install_dir=lambda: None, _launching_keys=set(),
            _steamcmd_update_running=lambda: False,
            _release_token_reservation_if_stopped=lambda _path: None,
            _continue_start_shard=lambda _c, shard, _arg: (events.append(("start", shard.name)),
                                                           make_proc(shard.name, ready=False)),
            _select_master_console_tab=lambda _c: None,
            window=lambda: None,
        )
        crash = SimpleNamespace(category="unknown", title="服务器启动失败")
        conflict = SimpleNamespace(category="token_conflict", title="令牌注册冲突")
        key = str(cluster.path)
        with patch.object(controller_module, "data_dir", return_value=root / "logs"), \
                patch.object(controller_module, "get_auto_restart_enabled", return_value=True), \
                patch.object(controller_module, "get_token_switch_on_timeout", side_effect=lambda: opts["switch"]), \
                patch.object(controller_module, "get_token_switch_after_minutes",
                             side_effect=lambda: opts["minutes"]), \
                patch.object(controller_module, "load_cluster_config",
                             return_value=SimpleNamespace(network={})), \
                patch.object(controller_module.luajit_injector, "needs_regeneration", return_value=False), \
                patch.object(controller_module, "resolve_conf_dir_arg", return_value=None):
            controller = controller_module.AutoRestartController.__new__(controller_module.AutoRestartController)
            controller_module.QObject.__init__(controller)
            controller._page, controller._states = page, {}
            controller._log_path = root / "logs" / "auto_restart.log"

            # 1. 手动启动就失败（世界没就绪过）：不重启
            controller.on_failure(make_proc("Master", ready=False, status=ServerStatus.CRASHED), crash)
            assert key not in controller._states or controller._states[key].phase == "idle"

            # 1b. 用户只开了地上（洞穴没开）：地上崩溃只重启地上，不能把没开的洞穴也拉起来
            controller.on_failure(make_proc("Master", status=ServerStatus.CRASHED), crash)
            controller._run(key)
            assert [e for e in events if e[0] == "start"] == [("start", "Master")]
            procs["Master"].world_ready, procs["Master"].status = True, ServerStatus.RUNNING
            controller.on_registered(procs["Master"])
            assert controller._states[key].phase == "idle", "只等崩溃前在运行的世界就绪"
            events.clear()
            choose_calls.clear()

            # 2. 地上、洞穴都在跑，地上崩溃：只重启地上，洞穴保持运行等饥荒自动重连并回退快照
            caves = make_proc("Caves")
            controller.on_failure(make_proc("Master", status=ServerStatus.CRASHED), crash)
            state = controller._states[key]
            assert state.phase == "scheduled" and not state.full
            controller._run(key)
            assert not any(e[0] == "stop" and "Caves" in e[1] for e in events), "不能停洞穴"
            assert [e for e in events if e[0] == "start"] == [("start", "Master")]
            assert procs["Caves"] is caves
            assert state.phase == "starting" and state.attempts == 1
            assert choose_calls == [(False, True)], "崩溃后用原令牌拉起，不换池中其它令牌"

            # 3. 拉起后注册冲突：不停服，等到换令牌时刻再检查
            events.clear()
            controller.on_failure(procs["Master"], conflict)
            assert state.phase == "waiting_release" and state.conflicts == 1
            assert state.due == state.crashed_at + opts["minutes"] * 60, "换令牌时刻按用户设定的分钟数"
            assert set(procs) == {"Master", "Caves"} and not events, "冲突时专服会自己重试，不能停服"
            assert controller.banner_text(cluster)

            # 4. 到了换令牌时刻仍在冲突：停服换池中其它令牌重启
            controller._run(key)
            assert switch_calls == [1] and state.switched and state.phase == "starting" and state.attempts == 2
            assert events[0][0] == "stop" and [e for e in events if e[0] == "start"] == [
                ("start", "Master"), ("start", "Caves")]

            # 5. 换了令牌还冲突：不再换第二次，等到总上限后停止管理，但不关服
            events.clear()
            controller.on_failure(procs["Master"], conflict)
            assert state.phase == "waiting_release" and state.due == state.crashed_at + TOKEN_WAIT_LIMIT
            controller._run(key)
            assert state.phase == "gave_up" and set(procs) == {"Master", "Caves"} and not events

            # 6. 关闭超时换令牌 + 令牌正被别的存档占用：先隔一会儿再查，拉起后冲突就一直等到注册成功
            opts["switch"] = False
            controller.on_failure(make_proc("Master", status=ServerStatus.CRASHED), crash)
            tokens["available"] = False
            controller._run(key)
            assert state.phase == "waiting_token" and abs(state.due - (time.time() + TOKEN_BUSY_RECHECK)) < 5
            tokens["available"] = True
            controller._run(key)
            assert state.phase == "starting"
            controller.on_failure(procs["Master"], conflict)
            assert state.phase == "waiting_release" and state.due == state.crashed_at + TOKEN_WAIT_LIMIT
            for proc in procs.values():
                proc.world_ready, proc.status = True, ServerStatus.RUNNING
            controller.on_registered(procs["Master"])
            assert state.phase == "idle", "冲突期间专服自行注册成功即结束本轮"
            assert switch_calls == [1]

            # 7. 默认等待 0 分钟：原令牌仍在等待期且池中有空闲令牌，拉起前直接换用；没有空闲令牌才用原令牌
            state.budget.reset()
            opts.update(switch=True, minutes=0, hold={"state": "crashed", "retry_at": time.time() + 600})
            calls_before = len(choose_calls)
            controller.on_failure(make_proc("Master", status=ServerStatus.CRASHED), crash)
            controller._run(key)
            assert switch_calls == [1, 1] and len(choose_calls) == calls_before and state.switched
            assert state.phase == "starting", "换了空闲令牌直接拉起，不先用原令牌试冲突"
            opts["alternative"] = None
            controller.on_failure(make_proc("Master", status=ServerStatus.CRASHED), crash)
            controller._run(key)
            assert choose_calls[-1] == (False, True) and state.phase == "starting", "没有空闲令牌时用原令牌拉起"

            # 7b. 手动操作取消排队中的重启
            controller.on_failure(make_proc("Master", status=ServerStatus.CRASHED), crash)
            assert state.phase == "scheduled"
            controller.cancel(cluster)
            assert state.phase == "idle"

            # 8. 30 分钟内崩溃超过上限：放弃并给出原因
            state.budget.reset()
            for _ in range(MAX_CRASH_RESTARTS):
                controller.on_failure(make_proc("Master", status=ServerStatus.CRASHED), crash)
                controller.cancel(cluster)
            controller.on_failure(make_proc("Master", status=ServerStatus.CRASHED), crash)
            assert state.phase == "gave_up" and str(MAX_CRASH_RESTARTS) in state.reason
            assert "放弃自动重启" in (root / "logs" / "auto_restart.log").read_text(encoding="utf-8")


if __name__ == "__main__":
    run(globals())
