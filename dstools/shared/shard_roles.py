"""主世界判定：以各分片 server.ini [SHARD] is_master 为准，不看文件夹名。

其他工具建的存档、或用户改过结构的存档，主世界文件夹不一定叫 Master（如猪镇做主世界的
Hamlet）。只有没有任何分片写明 is_master = true 时，才退回 Master 文件夹；只有一个分片时它就是主世界。
"""

from pathlib import Path

from dstools.shared.ini_parser import parse_server_ini

MASTER_FOLDER = "Master"


def _declares_master(shard_dir: Path) -> bool:
    ini = shard_dir / "server.ini"
    if not ini.is_file():
        return False
    try:
        return parse_server_ini(ini).shard.get("is_master") is True
    except (OSError, ValueError):
        return False


def find_master_dir(shard_dirs) -> Path | None:
    """在一组分片目录里找主世界目录；找不到返回 None。"""
    dirs = [Path(d) for d in shard_dirs]
    declared = next((d for d in dirs if _declares_master(d)), None)
    if declared is not None:
        return declared
    named = next((d for d in dirs if d.name == MASTER_FOLDER), None)
    if named is not None:
        return named
    return dirs[0] if len(dirs) == 1 else None


def master_shard(cluster):
    """存档的主世界分片（Shard 对象）；判定不出返回 None。"""
    master_dir = find_master_dir(shard.path for shard in cluster.shards)
    return next((shard for shard in cluster.shards if shard.path == master_dir), None)


def master_shard_name(cluster) -> str:
    """主世界分片的文件夹名；判定不出返回空字符串。"""
    shard = master_shard(cluster)
    return shard.name if shard is not None else ""
