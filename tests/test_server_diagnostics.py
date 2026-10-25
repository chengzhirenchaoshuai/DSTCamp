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


if __name__ == "__main__":
    run(globals())
