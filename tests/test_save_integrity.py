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
