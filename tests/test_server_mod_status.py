"""服务器 Mod 完整性提示的数量与显示时机回归测试。"""

from pathlib import Path
import queue
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dstools.features.local_service.dedicated_server import (  # noqa: E402
    ServerProcess,
    ServerStatus,
    advance_world_ready_marker,
)
from _harness import run  # noqa: E402


def _run_process(lines: list[str], *, is_master: bool = True) -> ServerProcess:
    process = ServerProcess.__new__(ServerProcess)
    process.world_ready = False
    process.mods_enabled = set()
    process.mods_loaded = set()
    process.mods_failed = set()
    process._mod_context = None
    process.missing_mods = None
    process.is_master = is_master

    class _FakeProc:
        stdout = [line + "\n" for line in lines]

    process.proc = _FakeProc()
    process._out_queue = queue.Queue()
    process._read_loop()
    return process


def test_luajit_companion_is_checked_and_counted() -> None:
    lines = [
        "modoverrides.lua enabling workshop-1467214795",
        "modoverrides.lua enabling workshop-3435352667",
        "modoverrides.lua enabling workshop-3444078585",
        "Loading mod: workshop-1467214795 (岛屿冒险 - 海难) Version:1.0.57",
        "Loading mod: workshop-3435352667 (岛屿冒险 - 核心) Version:1.0.69",
        "Loading mod: workshop-3444078585 (DontStarveLuaJit2) Version:1.10.1",
        "About to start a shard with these settings:",
        "Reset() returning",
        "Sim paused",
    ]
    process = _run_process(lines)
    assert process.missing_mods == []
    assert process.mods_enabled == {
        "workshop-1467214795", "workshop-3435352667", "workshop-3444078585",
    }
    assert process.visible_mod_count == 3

    missing_companion = _run_process([
        line for line in lines if "Loading mod: workshop-3444078585" not in line
    ])
    assert missing_companion.missing_mods == ["workshop-3444078585"]
    assert missing_companion.visible_mod_count == 3


def test_missing_mods_are_reported_only_after_world_ready() -> None:
    lines = [
        "modoverrides.lua enabling workshop-1",
        "modoverrides.lua enabling workshop-2",
        "Loading mod: workshop-1 (One) Version:1.0",
    ]
    pending = _run_process(lines)
    assert pending.world_ready is False
    assert pending.missing_mods is None

    ready = _run_process([
        *lines,
        "About to start a shard with these settings:",
        "Reset() returning",
        "Sim paused",
    ])
    assert ready.world_ready is True
    assert ready.missing_mods == ["workshop-2"]


def test_presentation_waits_until_ready_line_is_consumed() -> None:
    start_seen = False
    ready_seen = False
    displayed_lines = [
        "ModIndex: Load sequence finished successfully.",
        "Reset() returning",  # 正式启动分界线之前的预加载假阳性
        "About to start a shard with these settings:",
    ]
    for line in displayed_lines:
        start_seen, ready_now = advance_world_ready_marker(
            line, True, start_seen,
        )
        ready_seen |= ready_now
    assert start_seen is True
    assert ready_seen is False

    start_seen, ready_now = advance_world_ready_marker(
        "Reset() returning", True, start_seen,
    )
    assert start_seen is True and ready_now is False
    start_seen, ready_now = advance_world_ready_marker(
        "Sim paused", True, start_seen,
    )
    assert ready_now is True

    secondary_start, secondary_ready = advance_world_ready_marker(
        "About to start a shard with these settings:", False, False,
    )
    assert secondary_start is True and secondary_ready is False
    secondary_start, secondary_ready = advance_world_ready_marker(
        "[Shard] secondary shard is now ready!", False, secondary_start,
    )
    assert secondary_ready is False, "之后还可能回退到主世界快照并重载"
    secondary_start, secondary_ready = advance_world_ready_marker(
        "[Shard] secondary shard LUA is now ready!", False, secondary_start,
    )
    assert secondary_ready is True


def test_master_ready_without_sim_paused() -> None:
    # 真机日志（三界同行 Mod 自行冻结世界）：Master 全程不打印 "Sim paused"
    for ready_line in (
        "[00:01:43]: [Shard] Shard server started on port: 10888",
        "[00:00:21]: Telling Client our new session identifier: 305E0304284463BA",  # 单世界、关闭空服暂停
        "[00:01:55]: Server registered via geo DNS in ap-east-1",
        "[00:02:06]: [Shard] Secondary Volcano(5) ready!",
    ):
        start_seen, ready_now = advance_world_ready_marker(
            "About to start a shard with these settings:", True, False,
        )
        start_seen, ready_now = advance_world_ready_marker(
            "[00:01:55]: [Shard] Secondary shard Caves(4) connected: [LAN] 127.0.0.1", True, start_seen,
        )
        assert ready_now is False, "从世界刚连上还不算就绪"
        start_seen, ready_now = advance_world_ready_marker(ready_line, True, start_seen)
        assert ready_now is True, ready_line


def test_mod_syntax_error_is_failed_but_world_can_be_ready() -> None:
    process = _run_process([
        "modoverrides.lua enabling CommonModSets",
        "Registering Mods:",
        "    Registering Mod CommonModSets",
        "Mod: CommonModSets (常用mod集合)\tLoading modmain.lua",
        "Mod: CommonModSets (常用mod集合)\t  Error loading mod!",
        "[string \"../mods/CommonModSets/modmain.lua\"]:7: unfinished string near '''",
        "Disabling CommonModSets (常用mod集合) because it had an error.",
        "About to start a shard with these settings:",
        "Reset() returning",
        "Sim paused",
    ])
    assert process.world_ready is True
    assert process.mods_failed == {"CommonModSets"}
    assert process.missing_mods == ["CommonModSets"]


def test_console_log_reads_are_bounded_during_error_storm() -> None:
    process = ServerProcess.__new__(ServerProcess)
    process._out_queue = queue.Queue()
    for index in range(1_200):
        process._out_queue.put(f"LUA ERROR stack traceback #{index}")

    first = process.read_available_lines(max_lines=500)
    second = process.read_available_lines(max_lines=500)
    third = process.read_available_lines(max_lines=500)
    assert len(first) == 500
    assert len(second) == 500
    assert len(third) == 200
    assert process.read_available_lines(max_lines=500) == []


def test_console_shutdown_command_is_an_expected_exit() -> None:
    class _FakeStdin:
        closed = False

        def __init__(self) -> None:
            self.writes = []

        def write(self, text: str) -> None:
            self.writes.append(text)

        def flush(self) -> None:
            pass

    class _FakeProc:
        def __init__(self) -> None:
            self.stdin = _FakeStdin()
            self.exit_code = None

        def poll(self):
            return self.exit_code

    process = ServerProcess.__new__(ServerProcess)
    process.proc = _FakeProc()
    process.status = ServerStatus.RUNNING
    process.intentional_shutdown = False

    assert process.send_command(" c_shutdown(1); ") is True
    assert process.proc.stdin.writes == [" c_shutdown(1); \n"]
    assert process.intentional_shutdown is True
    assert process.status == ServerStatus.STOPPING
    assert process.sync_expected_exit() is None
    assert process.status == ServerStatus.STOPPING

    process.proc.exit_code = 0
    assert process.sync_expected_exit() == 0
    assert process.status == ServerStatus.STOPPED


def test_shutdown_text_inside_other_command_does_not_hide_crash() -> None:
    class _FakeStdin:
        closed = False

        def write(self, _text: str) -> None:
            pass

        def flush(self) -> None:
            pass

    process = ServerProcess.__new__(ServerProcess)
    process.proc = type("_FakeProc", (), {"stdin": _FakeStdin()})()
    process.status = ServerStatus.RUNNING
    process.intentional_shutdown = False

    assert process.send_command('c_announce("c_shutdown()")') is True
    assert process.intentional_shutdown is False
    assert process.status == ServerStatus.RUNNING


def test_console_command_history_supports_up_down_and_draft() -> None:
    from dstools.qt.server.local_console import ConsolePane

    class _FakeEdit:
        def __init__(self) -> None:
            self.value = ""

        def text(self) -> str:
            return self.value

        def setText(self, value: str) -> None:
            self.value = value

        def end(self, _mark: bool) -> None:
            pass

    pane = ConsolePane.__new__(ConsolePane)
    pane.cmd_edit = _FakeEdit()
    pane.proc = type(
        "_FakeServer",
        (),
        {"send_command": lambda _self, _command: True},
    )()
    pane._command_history = []
    pane._command_history_index = 0
    pane._command_draft = ""

    for command in ("c_save()", "c_listallplayers()"):
        pane.cmd_edit.setText(command)
        pane._send()

    pane.cmd_edit.setText("c_announce(\"草稿\")")
    pane._browse_history(-1)
    assert pane.cmd_edit.text() == "c_listallplayers()"
    pane._browse_history(-1)
    assert pane.cmd_edit.text() == "c_save()"
    pane._browse_history(1)
    assert pane.cmd_edit.text() == "c_listallplayers()"
    pane._browse_history(1)
    assert pane.cmd_edit.text() == 'c_announce("草稿")'


def test_stop_blocking_prefers_shutdown_command_over_force() -> None:
    process = ServerProcess.__new__(ServerProcess)
    process.status = ServerStatus.RUNNING
    calls = []

    def _request_shutdown() -> bool:
        calls.append("shutdown")
        return True

    process.request_shutdown = _request_shutdown
    process.poll_exit_code = lambda: 0
    process.terminate = lambda: calls.append("terminate")
    process.kill = lambda: calls.append("kill")

    process.stop_blocking(graceful_timeout=0.01, term_timeout=0.01)

    assert calls == ["shutdown"]
    assert process.status == ServerStatus.STOPPED


def test_missing_enabled_mods_follow_server_load_paths():
    """V2 目录、V1 包、专服 V1 运行目录和本地 Mod 都算已就绪，其余列为缺失。"""
    import tempfile
    from dstools.features.mod.missing_mods import find_missing_enabled_mods

    with tempfile.TemporaryDirectory() as tmp:
        content = Path(tmp) / "content" / "322330"
        mods = Path(tmp) / "server" / "mods"
        for folder in (content / "1", mods / "workshop-3", mods / "local_ok"):
            folder.mkdir(parents=True)
            (folder / "modinfo.lua").write_text("name='x'", encoding="utf-8")
        (content / "2").mkdir()
        (content / "2" / "2_legacy.bin").write_bytes(b"zip")
        (content / "4").mkdir()  # 只有空目录不算已就绪
        enabled = ["1", "2", "3", "4", "5", "local_ok", "local_missing"]
        missing = find_missing_enabled_mods(enabled, ugc_content_root=content, server_mods_root=mods)
        assert missing.workshop_ids == ("4", "5")
        assert missing.local_names == ("local_missing",)
        # 不传 -ugc_directory 时无从判断 Workshop Mod，不拦截
        unknown = find_missing_enabled_mods(["5"], ugc_content_root=None, server_mods_root=mods)
        assert not unknown


if __name__ == "__main__":
    run(globals())
