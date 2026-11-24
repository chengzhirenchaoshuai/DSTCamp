"""切回旧会话：把各世界 shardindex 的 session_id 改回磁盘上仍在的旧会话。

用途：断电把 shardindex 写坏后游戏不报错地生成了新世界（新会话），旧进度还留在原会话目录里（2026-11-24 实例）。
游戏开服只读 session_id 指向的会话，改这一个字段即可读回旧进度（离线专服实测读回第 57 天）；当前会话不删除，
切错了还能切回来。旧会话最新快照若是写坏的空文件，交给开服前检查回退（save_integrity）。

各世界的会话 ID 互不相同，只能靠快照对应：同一次存盘各世界的快照编号相同、天数相同（主世界与洞穴共用时钟）。
所以以用户选的那个世界为准，其余世界按"同编号 → 同天数（编号最近）→ 只有一个旧会话"依次挑；都对不上的保持不变，
确认框里逐个写明，由用户判断。调用方须确认这个存档的所有世界都已停止。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dstools.features.local_service.save_integrity import SessionBrief, read_session_id, session_briefs
from dstools.features.mod.shardindex import ShardIndexError, with_session_id, write_bytes_atomic
from dstools.shared.discovery import list_shards


@dataclass(frozen=True)
class ShardSwitch:
    shard: str
    path: Path
    current: str | None               # 现在 shardindex 指向的会话 ID
    current_brief: SessionBrief | None
    target: SessionBrief | None       # None：没有对应的旧会话，保持不变


def inactive_sessions(shard_dir: Path) -> list[SessionBrief]:
    """可以切过去的旧会话（有完好快照、不是当前生效的），按天数、编号从大到小。"""
    current = read_session_id(shard_dir)
    briefs = [b for sid, b in session_briefs(shard_dir).items() if sid != current]
    return sorted(briefs, key=lambda b: (b.day or 0, b.latest), reverse=True)


def _match(chosen: SessionBrief, candidates: list[SessionBrief]) -> SessionBrief | None:
    same_slot = [c for c in candidates if c.latest == chosen.latest]
    if same_slot:
        return same_slot[0]
    if chosen.day is not None:
        same_day = [c for c in candidates if c.day == chosen.day]
        if same_day:
            return min(same_day, key=lambda c: abs(c.latest - chosen.latest))
    return candidates[0] if len(candidates) == 1 else None


def plan_switch(cluster_path: Path, shard_name: str, session_id: str) -> list[ShardSwitch]:
    """以 shard_name 世界切到 session_id 为准，给出每个世界要切到哪个会话。"""
    shard_dirs = list_shards(Path(cluster_path))
    chosen_dir = next((d for d in shard_dirs if d.name == shard_name), None)
    if chosen_dir is None:
        raise ShardIndexError(f"找不到世界：{shard_name}")
    chosen = next((b for b in inactive_sessions(chosen_dir) if b.session_id == session_id), None)
    if chosen is None:
        raise ShardIndexError(f"{shard_name} 没有可切换的会话 {session_id}")
    plan = []
    for shard_dir in shard_dirs:
        current = read_session_id(shard_dir)
        briefs = session_briefs(shard_dir)
        current_brief = briefs.get(current or "")
        if shard_dir == chosen_dir:
            target = chosen
        elif current_brief is not None and (current_brief.latest == chosen.latest
                                            or (chosen.day is not None and current_brief.day == chosen.day)):
            target = None  # 这个世界没被重新生成（只写坏了别的世界的索引），当前会话本来就对得上
        else:
            target = _match(chosen, inactive_sessions(shard_dir))
        plan.append(ShardSwitch(shard_dir.name, shard_dir, current, current_brief, target))
    return plan


def apply_switch(plan: list[ShardSwitch]) -> list[str]:
    """先把所有要改的 shardindex 都算好并校验，再逐个原子写入，免得改到一半才发现某个世界改不了。返回改过的世界名。"""
    updates = []
    for item in plan:
        if item.target is None:
            continue
        path = item.path / "save" / "shardindex"
        updates.append((item.shard, path, with_session_id(path.read_bytes(), item.target.session_id, str(path))))
    for _shard, path, data in updates:
        write_bytes_atomic(path, data)
    return [shard for shard, _path, _data in updates]
