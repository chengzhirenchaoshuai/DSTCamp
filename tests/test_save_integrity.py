"""开服前存档完整性检查：索引写坏会静默生成新世界、最新快照写坏会读档报错（规则 2026-11-24 离线专服实测）。"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import run  # noqa: E402

from dstools.features.local_service import save_integrity as si  # noqa: E402

OLD, NEW = "520B2FE82132B31F", "4804636DD67BEF10"


def _snapshot(session: Path, slot: int, day: int | None = None, blank: bool = False) -> None:
    session.mkdir(parents=True, exist_ok=True)
    name = f"{slot:010d}"
    (session / name).write_bytes(b"\0" * 64 if blank else b"KLEI     1 local savedata = {}")
    meta = b"\0" * 26 if blank else f"return {{clock={{cycles={day or 0},phase=\"day\"}}}}".encode()
    (session / f"{name}.meta").write_bytes(meta)


def _index(shard: Path, content: bytes) -> None:
    (shard / "save").mkdir(parents=True, exist_ok=True)
    (shard / "save" / "shardindex").write_bytes(content)


def _index_for(session_id: str) -> bytes:
    return f'KLEI     1 return {{\n  session_id="{session_id}",\n  version=5,\n}}'.encode()


def test_index_lost_and_fresh_states() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        shard = Path(tmp) / "Master"
        sessions = shard / "save" / "session"
        # 全新存档、离线重置后都没有会话：不是事故
        assert si.check_shard(shard) is None
        _index(shard, _index_for(OLD))
        assert si.check_shard(shard) is None
        # 断电把索引写成 NUL / 空文件，旧会话还在：开服会静默生成新世界
        _snapshot(sessions / OLD, 67, day=56)
        _snapshot(sessions / OLD, 68, day=57)
        for broken in (b"\0" * 300, b""):
            _index(shard, broken)
            problem = si.check_shard(shard)
            assert problem.kind == si.INDEX_LOST and problem.orphans[0] == si.SessionBrief(OLD, 68, 57), problem
        # 索引正常指向这个会话：没有问题
        _index(shard, _index_for(OLD))
        assert si.check_shard(shard) is None
        # 索引指向的会话没有快照、另一个会话有：同样会生成新世界
        (sessions / NEW).mkdir()
        _index(shard, _index_for(NEW))
        assert si.check_shard(shard).kind == si.INDEX_LOST


def test_active_session_follows_shardindex() -> None:
    """存档页展示的会话按 shardindex 认，不按目录名顺序（新会话 ID 排在旧会话后面时曾显示成旧世界）。"""
    from dstools.features.save_browser.reader import find_active_session, list_save_sessions

    with tempfile.TemporaryDirectory() as tmp:
        shard = Path(tmp) / "Master"
        sessions_dir = shard / "save" / "session"
        _snapshot(sessions_dir / "0AAAAAAAAAAAAAAA", 68, day=57)
        _snapshot(sessions_dir / "FBBBBBBBBBBBBBBB", 2, day=0)
        sessions = list_save_sessions(shard)
        _index(shard, _index_for("FBBBBBBBBBBBBBBB"))
        assert find_active_session(shard, sessions).session_id == "FBBBBBBBBBBBBBBB"
        _index(shard, b"\0" * 100)
        assert find_active_session(shard, sessions) is None


def test_switch_back_to_old_sessions() -> None:
    """断电后游戏各世界另开了新会话：以主世界选的旧会话为准，洞穴按同编号对上自己的旧会话，只改 session_id。"""
    from dstools.features.save_browser import session_switch

    with tempfile.TemporaryDirectory() as tmp:
        cluster = Path(tmp) / "1113"
        layout = {"Master": (OLD, NEW), "Caves": ("60514BBD3F96B75D", "01A1256FC42C7331")}
        for shard, (old, new) in layout.items():
            (cluster / shard).mkdir(parents=True)
            (cluster / shard / "server.ini").write_text("[SHARD]\n", encoding="utf-8")
            sessions = cluster / shard / "save" / "session"
            _snapshot(sessions / old, 68, day=57)
            _snapshot(sessions / old, 69, blank=True)
            _snapshot(sessions / new, 4, day=0)
            _index(cluster / shard, b"KLEI     1 return {\n  enabled_mods={},\n  session_id=\"" + new.encode()
                   + b"\",\n  version=5,\n  world={ options={ id=\"x\" } }\n}")
        plan = session_switch.plan_switch(cluster, "Master", OLD)
        targets = {item.shard: item.target.session_id for item in plan}
        assert targets == {"Master": OLD, "Caves": "60514BBD3F96B75D"}, plan
        before = (cluster / "Caves" / "save" / "shardindex").read_bytes()
        assert sorted(session_switch.apply_switch(plan)) == ["Caves", "Master"]
        for shard, (old, new) in layout.items():
            assert si.read_session_id(cluster / shard) == old
            assert (cluster / shard / "save" / "session" / new / "0000000004").is_file()  # 新会话保留
        after = (cluster / "Caves" / "save" / "shardindex").read_bytes()
        assert after == before.replace(b"01A1256FC42C7331", b"60514BBD3F96B75D"), after
        # 切过去之后最新一份是空文件，交给开服前检查回退
        assert si.check_shard(cluster / "Master").kind == si.SNAPSHOT_CORRUPT
        # 切回来同样可行
        plan = session_switch.plan_switch(cluster, "Caves", "01A1256FC42C7331")
        assert {item.shard: item.target.session_id for item in plan} == {"Master": NEW, "Caves": "01A1256FC42C7331"}


def test_index_repaired_from_backup() -> None:
    """索引写坏时从备份取回：跳过同样写坏的最新备份，优先指向旧会话的那份；都不指向时改指到旧会话。"""
    import zipfile

    with tempfile.TemporaryDirectory() as tmp:
        shard = Path(tmp) / "1113" / "Master"
        _snapshot(shard / "save" / "session" / OLD, 68, day=57)
        _index(shard, b"\0" * 300)
        problem = si.check_shard(shard)
        assert problem.kind == si.INDEX_LOST

        def backup(name: str, index: bytes) -> Path:
            path = Path(tmp) / name
            with zipfile.ZipFile(path, "w") as zf:
                zf.writestr("master/save/shardindex", index)  # 世界名大小写不同也认
            return path

        broken = backup("c.zip", b"\0" * 300)       # 断电时正在备份
        other = backup("b.zip", _index_for("0123456789ABCDEF"))
        good = backup("a.zip", _index_for(OLD))
        repair = si.find_index_repair(problem, [broken, other, good])
        assert repair.backup == good and not repair.rewritten and repair.data == _index_for(OLD), repair
        # 没有指向旧会话的备份：取最新一份能读的，改指到旧会话
        repair = si.find_index_repair(problem, [broken, other])
        assert repair.backup == other and repair.rewritten, repair
        assert si.session_id_from_bytes(repair.data) == OLD
        assert si.find_index_repair(problem, [broken]) is None

        si.apply_index_repair(shard, repair, Path(tmp) / "q")
        assert si.check_shard(shard) is None
        assert (Path(tmp) / "q" / "Master" / "shardindex").read_bytes() == b"\0" * 300


def test_blank_latest_snapshot_moved_aside() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        shard = Path(tmp) / "Caves"
        session = shard / "save" / "session" / OLD
        _index(shard, _index_for(OLD))
        _snapshot(session, 68, day=57)
        _snapshot(session, 69, blank=True)
        _snapshot(session / "A7LFUP8UF05O", 68, day=57)
        _snapshot(session / "A7LFUP8UF05O", 69, day=57)  # 玩家快照完好也一起移走，游戏读档时本来就会截掉
        problem = si.check_shard(shard)
        assert problem.kind == si.SNAPSHOT_CORRUPT and problem.bad_slots == (69,), problem
        assert problem.fallback == si.SessionBrief(OLD, 68, 57), problem

        dest = Path(tmp) / "quarantine"
        moved = si.move_bad_snapshots(shard, problem, dest)
        assert len(moved) == 4, moved
        assert (dest / "Caves" / OLD / "A7LFUP8UF05O" / "0000000069").is_file()
        assert (session / "0000000068").is_file() and not (session / "0000000069").exists()
        assert si.check_shard(shard) is None

        # 全部快照都写坏：没有可回退的
        (session / "0000000068").write_bytes(b"")
        problem = si.check_shard(shard)
        assert problem.kind == si.SNAPSHOT_CORRUPT and problem.fallback is None, problem


if __name__ == "__main__":
    run(globals())
