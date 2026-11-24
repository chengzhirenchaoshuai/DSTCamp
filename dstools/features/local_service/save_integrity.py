"""开服前检查存档索引与最新快照有没有在异常关机时写坏，并把写坏的快照移走（不删除）。

依据游戏脚本 shardindex.lua 与 2026-11-24 离线专服实测（用户黑屏后强制重启的存档）：
- 每次存盘先写快照 ``save/session/<会话ID>/<编号>``（+ .meta），紧接着重写 ``save/shardindex``。断电或强制
  重启时刚写的文件常留成"大小正常、内容全是 NUL"。
- shardindex 读不出（空、全 NUL、解不开）或没有 session_id：专服把它当成没有世界，**不报错直接生成新世界**，
  旧会话目录原样留在磁盘上不再被读取。session_id 指向的会话没有快照时同样如此。
- 游戏读 session_id 指向的会话里编号最大的快照；它全是 NUL 时报 Encoded Savedata is empty 退出。把它移走后
  游戏读上一份，玩家目录里更新的快照由游戏读档时按编号自行截掉（实测）。
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from dstools.features.mod.shardindex import ShardIndexError, decode_persistent_bytes
from dstools.shared.lua_parser import parse_lua_file

INDEX_LOST = "index_lost"              # 索引读不出，开服会静默生成新世界
SNAPSHOT_CORRUPT = "snapshot_corrupt"  # 最新快照是空的，读档报错退出

_SESSION_ID_RE = re.compile(r'session_id\s*=\s*"([0-9A-Fa-f]+)"')
_PROBE_BYTES = 4096  # 判断"全是 NUL"只看开头：写坏的文件从头就是 NUL，整份读 4 MB 没必要


@dataclass(frozen=True)
class SessionBrief:
    session_id: str
    latest: int           # 最新一份完好快照的编号
    day: int | None       # 该快照 .meta 里的天数，读不到为 None


@dataclass(frozen=True)
class SaveProblem:
    shard: str
    kind: str
    session_id: str = ""
    bad_slots: tuple[int, ...] = ()       # SNAPSHOT_CORRUPT：从新到旧连续写坏的快照编号
    fallback: SessionBrief | None = None  # SNAPSHOT_CORRUPT：移走后会读的那份；None 表示没有完好的
    orphans: tuple[SessionBrief, ...] = field(default_factory=tuple)  # INDEX_LOST：磁盘上还在的旧会话


def _blank(path: Path) -> bool:
    """文件是空的或开头全是 NUL（断电写坏的样子）。"""
    try:
        with path.open("rb") as stream:
            head = stream.read(_PROBE_BYTES)
    except OSError:
        return False  # 读不了（被占用等）不当成损坏，交给游戏自己报
    return not head.strip(b"\0")


def _slots(session_dir: Path) -> list[int]:
    """会话目录里世界快照的编号（数字文件名），从新到旧。"""
    try:
        return sorted((int(p.name) for p in session_dir.iterdir() if p.is_file() and p.name.isdigit()),
                      reverse=True)
    except OSError:
        return []


def _slot_path(session_dir: Path, slot: int) -> Path:
    return session_dir / f"{slot:010d}"


def _day(session_dir: Path, slot: int) -> int | None:
    meta = session_dir / f"{slot:010d}.meta"
    try:
        clock = parse_lua_file(meta).get("clock", {})
        return int(clock.get("cycles")) if isinstance(clock, dict) else None
    except Exception:
        return None


def _scan_session(session_dir: Path) -> tuple[tuple[int, ...], SessionBrief | None]:
    """(从新到旧连续写坏的编号, 最新一份完好快照)。"""
    bad = []
    for slot in _slots(session_dir):
        if _blank(_slot_path(session_dir, slot)):
            bad.append(slot)
            continue
        return tuple(bad), SessionBrief(session_dir.name, slot, _day(session_dir, slot))
    return tuple(bad), None


def read_session_id(shard_dir: Path) -> str | None:
    """shardindex 里的 session_id；文件不存在、读不出或没有这个字段时返回 None（游戏同样当成没有世界）。"""
    path = Path(shard_dir) / "save" / "shardindex"
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if not raw.strip(b"\0"):
        return None
    try:
        text, _encoded = decode_persistent_bytes(raw)
    except (ShardIndexError, UnicodeDecodeError):
        return None
    match = _SESSION_ID_RE.search(text)
    return match.group(1) if match else None


def check_shard(shard_dir: Path) -> SaveProblem | None:
    """检查一个世界目录；没有问题返回 None。"""
    shard_dir = Path(shard_dir)
    session_root = shard_dir / "save" / "session"
    session_id = read_session_id(shard_dir)
    if session_id and (session_root / session_id).is_dir():
        bad, fallback = _scan_session(session_root / session_id)
        if bad:
            return SaveProblem(shard_dir.name, SNAPSHOT_CORRUPT, session_id, bad, fallback)
        if fallback is not None:
            return None
    # 游戏会生成新世界：只有磁盘上还有别的完好会话时才是事故（全新存档、离线重置后都没有会话）
    try:
        others = [p for p in session_root.iterdir() if p.is_dir() and p.name != session_id]
    except OSError:
        others = []
    orphans = []
    for directory in others:
        _bad, brief = _scan_session(directory)
        if brief is not None:
            orphans.append(brief)
    if not orphans:
        return None
    orphans.sort(key=lambda b: (b.day or 0, b.latest), reverse=True)
    return SaveProblem(shard_dir.name, INDEX_LOST, session_id or "", orphans=tuple(orphans))


def session_briefs(shard_dir: Path) -> dict[str, SessionBrief]:
    """世界目录里每个有完好快照的会话：{会话 ID: 最新完好快照}。"""
    root = Path(shard_dir) / "save" / "session"
    result = {}
    try:
        directories = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return result
    for directory in directories:
        _bad, brief = _scan_session(directory)
        if brief is not None:
            result[directory.name] = brief
    return result


def find_save_problems(shard_dirs) -> list[SaveProblem]:
    return [problem for problem in (check_shard(Path(d)) for d in shard_dirs) if problem is not None]


def quarantine_dir(backup_root: Path) -> Path:
    """这次移走的文件放在 ``<备份目录>/corrupt_snapshots/<时间>/``，与备份 zip 同处、打包分享存档时不会带上。"""
    return Path(backup_root) / "corrupt_snapshots" / datetime.now().strftime("%Y%m%d_%H%M%S")


def move_bad_snapshots(shard_dir: Path, problem: SaveProblem, dest_root: Path) -> list[Path]:
    """把写坏的快照（世界级与各玩家目录里同编号的文件及 .meta）移到 ``dest_root/<世界>/<会话>/``，返回移走后的路径。

    玩家目录里同编号的快照即使完好也一起移走：游戏读上一份时本来就会把它们截掉，移走只是留个底。"""
    session_dir = Path(shard_dir) / "save" / "session" / problem.session_id
    target_root = Path(dest_root) / Path(shard_dir).name / problem.session_id
    names = {f"{slot:010d}" for slot in problem.bad_slots}
    names |= {f"{name}.meta" for name in names}
    moved = []
    for source in sorted(session_dir.rglob("*")):
        if not source.is_file() or source.name not in names:
            continue
        target = target_root / source.relative_to(session_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        moved.append(target)
    return moved
