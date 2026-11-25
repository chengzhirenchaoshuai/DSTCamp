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
from dstools.features.local_service.server_diagnostics import summarize_mod_error  # noqa: E402
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
    # 错误原文到 Disabling 行为止，卡片上的摘要去掉 [string "../mods/<目录>/..."] 包装
    from dstools.features.local_service.server_diagnostics import summarize_mod_error
    error = process.mod_errors["CommonModSets"]
    assert error == "[string \"../mods/CommonModSets/modmain.lua\"]:7: unfinished string near '''"
    assert summarize_mod_error(error) == "modmain.lua:7: unfinished string near '''"


def test_local_mod_folder_with_spaces() -> None:
    """11-25 用户截图：本地 Mod 目录名带空格，加载成功却被报成加载失败；出错时也要记到这个 Mod 上。"""
    ready = ["About to start a shard with these settings:", "Reset() returning", "Sim paused"]
    process = _run_process([
        "modoverrides.lua enabling pumpkin bomb survival",
        "modoverrides.lua enabling plain mod",
        "Loading mod: pumpkin bomb survival (南瓜炸弹生存) Version:1.0",
        "Loading mod: plain mod Version:2.0",
        "Mod: pumpkin bomb survival (南瓜炸弹生存)\tLoading modmain.lua\t",
        *ready,
    ])
    assert process.missing_mods == []

    broken = _run_process([
        "modoverrides.lua enabling workshop-1",
        "modoverrides.lua enabling pumpkin bomb survival",
        "Loading mod: workshop-1 (One) Version:1.0",
        "Loading mod: pumpkin bomb survival (南瓜炸弹生存) Version:1.0",
        "Mod: workshop-1 (One)\tLoading modmain.lua\t",
        "Mod: pumpkin bomb survival (南瓜炸弹生存)\tLoading modmain.lua\t",
        "Mod: pumpkin bomb survival (南瓜炸弹生存)\t  Error loading mod!",
        "[string \"../mods/pumpkin bomb survival/modmain.lua\"]:3: unexpected symbol",
        "Disabling pumpkin bomb survival (南瓜炸弹生存) because it had an error.",
        *ready,
    ])
    assert broken.missing_mods == ["pumpkin bomb survival"]
    assert summarize_mod_error(broken.mod_errors["pumpkin bomb survival"]) == "modmain.lua:3: unexpected symbol"


def test_mod_error_text_collected_from_real_log_block() -> None:
    """10-30 真机日志（节选）：运行出错的错误块不带时间戳、以只有空白的行结束，随后游戏又带时间戳重印一遍。"""
    from dstools.features.local_service.server_diagnostics import (
        ModErrorCollector, mod_error_is_missing_file, summarize_mod_error,
    )
    collector = ModErrorCollector()
    for line in [
        "[19:15:39]: Mod: workshop-2736985627 (夜雨心空10.29.1)\tLoading modmain.lua\t",
        "[19:15:40]: MOD ERROR: workshop-2736985627 (夜雨心空10.29.1): Mod: workshop-2736985627 (夜雨心空10.29.1)"
        "  Error loading mod!",
        "[string \"../mods/workshop-2736985627/images/saveslot...\"]:1: module 'yyxkasdkvgassamjka' not found:",
        "\tno field package.preload['yyxkasdkvgassamjka']",
        "LUA ERROR stack traceback:",
        "        ../mods/workshop-2736985627/modmain.lua(1306,1) in main chunk",
        "\t",
        "[19:15:40]: Mod: workshop-2912906543 (Boss锻炼了-1.8.5)\tLoading modmain.lua\t",
        "[19:15:40]: [string \"../mods/workshop-2736985627/images/saveslot...\"]:1: module 'other' not found:",
        # modinfo 出错：正文以空格开头
        "Error loading mod: workshop-5 (坏模组)!",
        " [string \"../mods/workshop-5/modinfo.lua\"]:3: '=' expected near 'x'",
        "",
    ]:
        collector.feed(line)
    collector.finish()
    error = collector.errors["workshop-2736985627"]
    assert error.splitlines()[0].endswith("module 'yyxkasdkvgassamjka' not found:")
    assert error.splitlines()[-1].strip() == "../mods/workshop-2736985627/modmain.lua(1306,1) in main chunk"
    assert summarize_mod_error(error) == "images/saveslot...:1: module 'yyxkasdkvgassamjka' not found:"
    assert mod_error_is_missing_file(error)
    assert summarize_mod_error(collector.errors["workshop-5"]) == "modinfo.lua:3: '=' expected near 'x'"
    assert not mod_error_is_missing_file(collector.errors["workshop-5"])
    assert set(collector.errors) == {"workshop-2736985627", "workshop-5"}


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


def test_unregistered_mods_separated_from_missing_ones():
    """文件在 content 里、Steam 清单却没登记：专服静默跳过，单列出来而不是算作缺失。"""
    import tempfile
    from dstools.features.mod.missing_mods import find_missing_enabled_mods

    with tempfile.TemporaryDirectory() as tmp:
        content = Path(tmp) / "content" / "322330"
        mods = Path(tmp) / "server" / "mods"
        for folder in (content / "1", content / "7", mods / "workshop-8", content / "8"):
            folder.mkdir(parents=True)
            (folder / "modinfo.lua").write_text("name='x'", encoding="utf-8")
        enabled = ["1", "7", "8", "9"]
        missing = find_missing_enabled_mods(
            enabled, ugc_content_root=content, server_mods_root=mods,
            # 7 的文件在但没登记；8 也没登记，但专服 mods 里有副本，走 MODS_ROOT 兜底不受影响
            acf_installed_ids={1})
        assert missing.unregistered_ids == ("7",)
        assert missing.workshop_ids == ("9",)
        # 读不到清单（None）时不判未登记，否则会把所有 Mod 误报一遍
        unknown = find_missing_enabled_mods(
            enabled, ugc_content_root=content, server_mods_root=mods, acf_installed_ids=None)
        assert unknown.unregistered_ids == ()
        assert unknown.workshop_ids == ("9",)


def test_failed_list_picks_unregistered_mods_by_process_ugc_directory():
    """控制台加载失败列表按进程实际的 -ugc_directory 和清单找出未登记的 Mod，ID 保持 workshop- 形式。"""
    import tempfile
    from types import SimpleNamespace
    from dstools.features.local_service.shard_helpers import find_process_unregistered_mods

    with tempfile.TemporaryDirectory() as tmp:
        workshop = Path(tmp) / "workshop"
        content = workshop / "content" / "322330"
        for wid in ("1", "7"):
            (content / wid).mkdir(parents=True)
            (content / wid / "modinfo.lua").write_text("name='x'", encoding="utf-8")
        (workshop / "appworkshop_322330.acf").write_text(
            '"AppWorkshop"\n{\n\t"appid"\t\t"322330"\n\t"WorkshopItemsInstalled"\n\t{\n'
            '\t\t"1"\n\t\t{\n\t\t\t"size"\t\t"1"\n\t\t}\n\t}\n'
            '\t"WorkshopItemDetails"\n\t{\n\t\t"1"\n\t\t{\n\t\t\t"manifest"\t\t"5"\n\t\t}\n\t}\n}\n',
            encoding="utf-8")
        failed = ("workshop-1", "workshop-7", "workshop-9", "localmod")
        proc = SimpleNamespace(ugc_directory=str(workshop))
        # 1 已登记、9 没下载、localmod 不是工坊 Mod，都不算未登记
        assert find_process_unregistered_mods(proc, failed, None) == ("workshop-7",)
        # 没传 -ugc_directory 时专服走自己的下载目录，无从判断
        assert find_process_unregistered_mods(SimpleNamespace(ugc_directory=None), failed, None) == ()


if __name__ == "__main__":
    run(globals())
