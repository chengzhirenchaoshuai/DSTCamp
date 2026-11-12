"""服务器启动诊断规则的离线回归测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import run  # noqa: E402

from dstools.features.local_service.server_diagnostics import (
    analyze_mod_loading,
    contains_runtime_lua_error,
    contains_server_registration_success,
    contains_startup_failure,
    contains_token_conflict,
    diagnose_server_failure,
)


def test_server_failure_diagnostics() -> None:
    cases = [
        ("token_conflict", [
            "[Http] Curl failed[1] with HTTP_500. Response: E_ROWID_EXIST",
            "[Error] Master Server Broadcast Error: E_ROWID_EXIST",
        ]),
        ("runtime", ["The code execution cannot proceed because VCRUNTIME140.dll was not found."]),
        ("permission", ["PermissionError: [WinError 5] Access is denied."]),
        ("permission", ["Check for write access: FALSE", "Unable to write to config directory."]),
        ("world_generation", ["DoLuaFile Error: Must specify the task set for a level!"]),
        ("mod_conflict", ["Lua Error: stack traceback", "@../mods/workshop-456/main.lua:12"]),
        ("unknown", ["Server failed to start!", "Unexpected startup failure"]),
    ]
    for expected, lines in cases:
        report = diagnose_server_failure(
            shard_name="Master", exit_code=1, world_ready=False, log_lines=lines,
        )
        assert report is not None and report.category == expected, (expected, report)
    assert report.title == "服务器启动失败"

    # 世界运行中被强制结束（2026-10-05 真机强杀）：找不到具体原因时不能叫"启动失败"
    report = diagnose_server_failure(
        shard_name="Master", exit_code=1, world_ready=True, log_lines=["Sim paused"],
    )
    assert report is not None and report.category == "unknown" and report.title == "服务器异常退出"

    report = diagnose_server_failure(
        shard_name="Caves", exit_code=1, world_ready=False,
        log_lines=["Error loading worldgen_main.lua", "Must specify the task set for a level!"],
    )
    assert report is not None and report.category == "world_generation"

    report = diagnose_server_failure(
        shard_name="Master", exit_code=1, world_ready=False,
        log_lines=["Lua Error: stack traceback", "@../mods/workshop-123/main.lua:4"],
    )
    assert report is not None and report.category == "mod_conflict"
    assert report.related_mods == ("workshop-123",)

    report = diagnose_server_failure(
        shard_name="Master", exit_code=1, world_ready=True,
        log_lines=[
            "[00:18:32]: wrong number of arguments to 'insert'",
            "LUA ERROR stack traceback:",
            "../mods/workshop-3662240568/scripts/prefabs/bigbag.lua:1487 in (field) fn",
        ],
    )
    assert report is not None and report.category == "mod_conflict"
    assert "运行中" in report.summary
    assert report.related_mods == ("workshop-3662240568",)
    assert any("bigbag.lua:1487" in line for line in report.evidence)

    # 416756019/Caves 的真实形态：世界已经就绪，Lua 错误不会保证进程
    # 退出，仍应立即生成诊断而不是等待 poll() 返回退出码。
    cave_runtime_lines = [
        "[string \"scripts/components/inventory.lua\"]:770: "
        "attempt to index field 'isexternallyinsulated' (a nil value)",
        "LUA ERROR stack traceback:",
        "scripts/components/inventory.lua:770 in (method) IsInsulated",
        "../mods/workshop-1991746508/main/pills.lua:145",
        "../mods/workshop-375850593/modmain.lua:72",
        "../mods/workshop-661253977/modmain.lua:40",
    ]
    assert contains_runtime_lua_error(cave_runtime_lines)
    live_lua = diagnose_server_failure(
        shard_name="Caves",
        exit_code=None,
        world_ready=True,
        log_lines=cave_runtime_lines,
        enabled_mods=[
            "workshop-1991746508",
            "workshop-375850593",
            "workshop-661253977",
        ],
    )
    assert live_lua is not None and live_lua.category == "mod_conflict"
    assert "运行中" in live_lua.summary
    assert live_lua.related_mods == (
        "workshop-1991746508",
        "workshop-375850593",
        "workshop-661253977",
    )

    samples = (
        (
            [
                "MOD ERROR: workshop-3407249662 (Ordering): Mod error",
                '[string "../mods/workshop-1207269058/modmain.lua"]:1: module not found:',
                "no file ../mods/workshop-2893991859/scripts/widgets/foodcrafting.lua",
                "LUA ERROR stack traceback:",
                "../mods/workshop-1207269058/modmain.lua(1,1) in function require",
                "../mods/workshop-3407249662/modmain.lua(59,1) in main chunk",
                '[string "../mods/workshop-3366313760/modmain/abilities/error_tip.lua"]:89: attempt to index global',
                "LUA ERROR stack traceback:",
                "../mods/workshop-3366313760/modmain/abilities/error_tip.lua(89,1)",
            ],
            ("workshop-1207269058", "workshop-3366313760", "workshop-3407249662"),
        ),
        (
            [
                "error calling PrefabPostInit: world in mod workshop-362175979 (Wormhole Marks):",
                "no file ../mods/workshop-2189004162/scripts/components/wormhole_counter.lua",
                "LUA ERROR stack traceback:",
                "../mods/workshop-362175979/modmain.lua(72,1)",
                "Disabling workshop-362175979 (Wormhole Marks) because it had an error.",
            ],
            ("workshop-362175979",),
        ),
        (
            [
                '[string "../mods/workshop-3349143694/scripts/components/lzsave.lua"]:30: stack overflow',
                "LUA ERROR stack traceback:",
                "../mods/workshop-3349143694/scripts/components/lzsave.lua:30 in (upvalue) CleanInvalidRefs",
            ],
            ("workshop-3349143694",),
        ),
    )
    for lines, expected_mods in samples:
        report = diagnose_server_failure(
            shard_name="Master", exit_code=1, world_ready=False, log_lines=lines,
        )
        assert report is not None and report.category == "mod_conflict"
        assert report.related_mods == expected_mods

    # 加载器失败时会列出所有尝试过的 Mod 路径，未启用的 Insight 不能被当成疑似冲突 Mod
    report = diagnose_server_failure(
        shard_name="Master", exit_code=1, world_ready=False,
        enabled_mods=["workshop-1207269058"],
        log_lines=[
            '[string "scripts/modindex.lua"]:234: attempt to index local \'modname\'',
            "stack traceback:",
            "scripts/modindex.lua:234: in function 'IsWorkshopMod'",
            "no file '../mods/workshop-2189004162/scripts/widgets/foodcrafting.lua'",
        ],
    )
    assert report is not None and report.related_mods == ()
    assert report.evidence and any("stack traceback" in line for line in report.evidence)

    mod_status = analyze_mod_loading(
        enabled_mods=["workshop-123"], loaded_mods=["workshop-123"],
        visible_mod_count=1,
    )
    assert mod_status.failed_mods == ()
    assert mod_status.visible_mod_count == 1
    mod_status = analyze_mod_loading(
        enabled_mods=["CommonModSets"], loaded_mods=["CommonModSets"],
        failed_mods=["CommonModSets"], visible_mod_count=1,
    )
    assert mod_status.failed_mods == ("CommonModSets",)

    report = diagnose_server_failure(
        shard_name="Master", exit_code=1, world_ready=False,
        log_lines=[
            "[Error] Server failed to start!",
            "RakNet UDP startup failed: SOCKET_PORT_ALREADY_IN_USE (5)",
            'Details: SOCKET_PORT_ALREADY_IN_USE',
        ],
    )
    assert report is not None and report.category == "port"
    assert contains_startup_failure(["[Error] Server failed to start!"])
    assert contains_startup_failure(["Details: SOCKET_PORT_ALREADY_IN_USE"])
    conflict_lines = [
        '[Http] Curl failed[1] with HTTP_500, retrying (0 times). '
        'Response: {"Error":{"Code":"E_ROWID_EXIST"}}',
        "[Error] Master Server Broadcast Error: E_ROWID_EXIST"
    ]
    assert contains_token_conflict(conflict_lines)
    assert not contains_startup_failure(conflict_lines)
    live_conflict = diagnose_server_failure(
        shard_name="Caves",
        exit_code=None,
        world_ready=True,
        log_lines=conflict_lines,
    )
    assert live_conflict is not None
    assert live_conflict.category == "token_conflict"
    assert "Caves" in live_conflict.summary
    assert contains_server_registration_success([
        "[Steam] Server registered via geo DNS in ap-east-1"
    ])
    assert not contains_server_registration_success(["Sim paused"])
    assert not contains_startup_failure(["Starting Dedicated Server Game"])

    assert diagnose_server_failure(
        shard_name="Master", exit_code=0, world_ready=True, log_lines=[],
    ) is None
    assert diagnose_server_failure(
        shard_name="Master", exit_code=1, world_ready=False,
        log_lines=["Access is denied"], intentional_stop=True,
    ) is None


def test_luajit_boot_crash() -> None:
    """2026-10-31 用户日志：游戏更新后专服停在 CurlRequestManager 一行退出，五个世界都一样。"""
    stalled = [
        "[00:00:00]: Command Line Arguments: -console -cluster Cluster_2 -shard Master",
        "[00:00:00]: THREAD - started 'GAClient' (3004)",
        "[00:00:00]: CurlRequestManager::ClientThread::Main()",
    ]
    report = diagnose_server_failure(
        shard_name="Master", exit_code=1, world_ready=False, log_lines=stalled, luajit_injected=True,
    )
    assert report is not None and report.category == "luajit_boot_crash"
    # 没装注入壳，或已挂载数据包往后走了，都不能归到 LuaJIT
    assert diagnose_server_failure(
        shard_name="Master", exit_code=1, world_ready=False, log_lines=stalled,
    ).category == "unknown"
    assert diagnose_server_failure(
        shard_name="Master", exit_code=1, world_ready=False, luajit_injected=True,
        log_lines=stalled + ["[00:00:01]: Mounting file system databundles/klump.zip successful."],
    ).category == "unknown"


def test_invalid_token_while_process_alive() -> None:
    """2026-11-02 用户日志：令牌失效后专服进程不退出、frpc 正常，用户以为是穿透问题，换节点也进不去。"""
    lines = [
        '[00:00:15]: [200] Account Failed (6): "E_INVALID_TOKEN"',
        "[00:00:15]: !!!! Your Server Will Not Start !!!!",
        "[00:00:15]: No auth token could be found.",
    ]
    assert contains_startup_failure(lines)
    report = diagnose_server_failure(shard_name="Master", exit_code=None, world_ready=False, log_lines=lines)
    assert report is not None and report.category == "token_invalid"


def test_console_player_commands_run_in_lua() -> None:
    """控制台"在线玩家"/暂停的命令在 Lua 5.1 里实际执行：输出能解析，复活/杀死只作用于对应状态的玩家。"""
    from lupa.lua51 import LuaRuntime

    from dstools.features.local_service import player_commands as pc

    lua = LuaRuntime(unpack_returned_tuples=True)
    output: list[str] = []
    # 与游戏 print 一致：参数以 \t 连接，日志行带时间前缀、末尾带 \t
    lua.globals().print = lambda *args: output.append("[00:00:01]: " + "\t".join(map(str, args)) + "\t")
    lua.execute("""
        local function make(userid, name, tags, hp, invincible)
            local p = {userid=userid, name=name, prefab="wilson", _tags=tags, events={}}
            function p:HasTag(t) return self._tags[t] == true end
            function p:PushEvent(e) table.insert(self.events, e) end
            local h = {hp=hp, inv=invincible}
            function h:IsDead() return self.hp <= 0 end
            function h:Kill() if not self.inv then self.hp = 0 end end
            function h:ForceKill() self.hp = 0 end
            p.components = {health=h}
            return p
        end
        AllPlayers = {
            make("KU_alive", "A\\tB", {}, 100, true),
            make("KU_ghost", "Ghost", {playerghost=true}, 0, false),
            make("KU_corpse", "Corpse", {corpse=true}, 0, false),
        }
    """)
    lua.execute(pc.list_players_command("ab12cd34"))
    players, done = pc.parse_player_lines(output, "ab12cd34")
    assert done and [(p.userid, p.name, p.state) for p in players] == [
        ("KU_alive", "A B", "alive"), ("KU_ghost", "Ghost", "ghost"), ("KU_corpse", "Corpse", "corpse")]
    assert pc.parse_player_lines(output, "ffff0000") == ([], False)  # 别的查询的结果不混进来
    assert all(pc.is_query_output(line) for line in output)

    lua.execute(pc.revive_command("KU_alive"))  # 活人不受复活影响
    lua.execute(pc.kill_command("KU_alive"))  # 开了无敌也要能杀
    assert lua.eval("AllPlayers[1].components.health.hp") == 0
    assert len(lua.eval("AllPlayers[1].events")) == 0
    lua.execute(pc.revive_command("KU_ghost"))
    lua.execute(pc.revive_all_command())
    assert list(lua.eval("AllPlayers[2].events").values()) == ["respawnfromghost", "respawnfromghost"]
    assert list(lua.eval("AllPlayers[3].events").values()) == ["respawnfromcorpse"]
    lua.execute(pc.kill_command("KU_missing"))  # 找不到的玩家什么都不做
    lua.globals().TheNet = lua.eval("{SetServerPaused=function(self, v) PAUSED=v end}")
    lua.execute(pc.pause_command(True))
    assert lua.eval("PAUSED") is True
    for bad in ('KU_x" end os.exit() --', "KU x", ""):
        try:
            pc.kill_command(bad)
        except ValueError:
            continue
        raise AssertionError(f"应拒绝非法 ID：{bad!r}")

    # 管理员是否在线：专服自身（带 performance）即使 admin=true 也不算
    lua.execute('TheNet.GetClientTable = function(self) return CLIENTS end')
    lua.execute('CLIENTS = {{userid="KU_server", admin=true, performance=0}, {userid="KU_a", admin=false}}')
    output.clear()
    lua.execute(pc.admin_query_command("0a1b2c3d"))
    assert pc.parse_admin_line(output, "0a1b2c3d") == (0, 1)
    assert pc.parse_admin_line(output, "ffff0000") is None
    lua.execute('CLIENTS[3] = {userid="KU_b", admin=true}')
    output.clear()
    lua.execute(pc.admin_query_command("0a1b2c3d"))
    assert pc.parse_admin_line(output, "0a1b2c3d") == (1, 2)
    assert pc.is_query_output(output[0])
    # 专服回显的命令也不显示在控制台
    assert pc.is_query_output('[00:02:39]: RemoteCommandInput: "' + pc.admin_query_command("0a1b2c3d") + '"')
    # 真机日志里玩家进服、离开的行
    assert pc.is_player_presence_line("[00:01:42]: [ClientObject] Initialized (authenticated) on server: "
                                      "guid=974529692710868792 userid=KU_dwt6dfPl netid=76561198240522844 admin=1")
    assert pc.is_player_presence_line("[00:03:31]: [Shard] (KU_dwt6dfPl) disconnected from Master(1)")
    assert not pc.is_player_presence_line("[00:00:07]: [ClientObject] Initialized (self/server object, "
                                          "locally trusted) on server: guid=1 userid=KU_x netid= admin=1")

    # 真机日志里的暂停行（主世界与洞穴同一秒打印，行尾带 \t）
    assert [pc.parse_pause_line(line) for line in (
        "[00:04:06]: Server Paused\t", "[00:04:19]: Server Unpaused\t",
        "[00:06:24]: Server Autopaused\t", "[00:04:19]: Sim paused")] == [True, False, False, None]


if __name__ == "__main__":
    run(globals())
