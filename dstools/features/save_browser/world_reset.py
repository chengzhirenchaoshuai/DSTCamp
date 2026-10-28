"""不启动服务器重置世界：删掉各世界 save/session/ 下的会话，下次启动时游戏自动生成新世界。

依据游戏脚本：官方 c_regenerateworld() → WorldResetFromSim() → ShardIndex:Delete(cb, true) 只用
TheNet:DeleteSession() 删当前会话目录，保留 shardindex 里的世界选项、服务器信息和启用的 Mod；
开服时 LoadSlot() 发现 CheckWorldFile() 找不到会话文件就走 DoGenerateWorld()。
所以这里只删会话目录，shardindex（本地存档的世界设置与 Mod 都以它为准）和各配置文件一律不动。
会话目录里同时存着玩家存档，重置后玩家要重新选角色，这与官方重置一致。
所有世界一起删：只删主世界的话洞穴仍是旧世界，与官方重置连带重新生成其它世界的行为不符。
调用方须确认这个存档的所有世界都已停止。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from dstools.shared.discovery import list_shards


def _session_dir(shard_dir: Path) -> Path:
    return shard_dir / "save" / "session"


def shards_with_sessions(cluster_path: Path) -> list[str]:
    """有会话（已生成过世界）的世界目录名；为空说明没有可重置的世界。"""
    names = []
    for shard_dir in list_shards(Path(cluster_path)):
        session_dir = _session_dir(shard_dir)
        if session_dir.is_dir() and any(session_dir.iterdir()):
            names.append(shard_dir.name)
    return names


def reset_worlds(cluster_path: Path) -> list[str]:
    """删除每个世界 save/session/ 下的全部会话，返回被重置的世界目录名。

    遇到目录联接（junction）直接报错不删，免得删到联接目标里的内容。"""
    reset = []
    for shard_dir in list_shards(Path(cluster_path)):
        session_dir = _session_dir(shard_dir)
        if not session_dir.is_dir():
            continue
        entries = list(session_dir.iterdir())
        if not entries:
            continue
        for entry in entries:
            if os.path.isjunction(entry) or entry.is_symlink():
                raise OSError(f"会话目录里有链接，未删除：{entry}")
        for entry in entries:
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        reset.append(shard_dir.name)
    return reset
