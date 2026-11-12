"""存档备份：把存档当前状态打包成 zip。

内容：各世界 save/ 与 modoverrides.lua/leveldataoverride.lua/server.ini，以及存档级 cluster.ini/
cluster_token.txt/adminlist.txt/blocklist.txt；跳过游戏自己滚动维护的 backup/ 与日志。
备份放在与存档同级的 ``<Klei根>/dstcamp_backups/<存档名>/``，打包分享存档时不会带上。
保留份数见 get_backup_retention()，只计自动备份（关服、运行中定时），超出删最旧的；"立即备份"、恢复前的
保险备份和世界管理操作前的备份文件名带 _keep 标记，不参与自动清理，免得关键的回退点被定时备份挤掉。
自动备份受 get_backup_auto_enabled() 控制，带标记的备份不受影响。
"""

import os
import shutil
import time
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from dstools.shared.app_settings import get_backup_retention
from dstools.shared.discovery import list_shards
from dstools.shared.shard_roles import find_master_dir

_BACKUP_DIR_NAME = "dstcamp_backups"
_SHARD_ITEMS = ("save", "modoverrides.lua", "leveldataoverride.lua", "server.ini")
_CLUSTER_ITEMS = ("cluster.ini", "cluster_token.txt", "adminlist.txt", "blocklist.txt")
_KEEP_SUFFIX = "_keep"  # 不参与自动清理的备份：文件名（不含扩展名）以它结尾


def backup_dir(cluster_path: Path) -> Path:
    """这个存档的备份目录：`<存档目录的上一级>/dstcamp_backups/<存档目录名>/`。"""
    return cluster_path.parent / _BACKUP_DIR_NAME / cluster_path.name


def list_backups(cluster_path: Path) -> list[Path]:
    """按时间从新到旧排列的备份 zip 列表。"""
    d = backup_dir(cluster_path)
    if not d.exists():
        return []
    return sorted(d.glob("*.zip"), key=lambda p: p.name, reverse=True)


def is_kept_backup(path: Path) -> bool:
    """手动类备份（立即备份、恢复前、世界管理），不参与自动清理。"""
    return path.stem.endswith(_KEEP_SUFFIX)


def _backup_files(cluster_path: Path):
    """备份要收的文件：逐个给出 (源文件, zip 内路径)。"""
    for name in _CLUSTER_ITEMS:
        src = cluster_path / name
        if src.is_file():
            yield src, name
    for shard_dir in list_shards(cluster_path):
        for name in _SHARD_ITEMS:
            src = shard_dir / name
            if src.is_dir():
                for f in src.rglob("*"):
                    if f.is_file():
                        yield f, str(Path(shard_dir.name) / name / f.relative_to(src))
            elif src.is_file():
                yield src, str(Path(shard_dir.name) / name)


def changed_since_last_backup(cluster_path: Path) -> bool:
    """最近一份备份之后存档有没有变化（备份要收的文件里有更新的），没有备份时算有变化。

    定时备份据此跳过：没人在线时世界暂停不存盘，或者卡在启动阶段，都不该一份份重复备份把旧备份挤掉。"""
    backups = list_backups(cluster_path)
    if not backups:
        return True
    try:
        since = max(path.stat().st_mtime for path in backups)
        return any(src.stat().st_mtime > since for src, _arcname in _backup_files(cluster_path))
    except OSError:  # 文件正被游戏改写等，宁可多备份一份
        return True


def create_backup(cluster_path: Path, keep: bool = False) -> Path:
    """打包 cluster_path 当前状态，返回新建的 zip 路径。keep=True 的备份不参与自动清理。"""
    dest_dir = backup_dir(cluster_path)
    dest_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    stamp = datetime.fromtimestamp(started).strftime("%Y%m%d_%H%M%S")
    suffix = _KEEP_SUFFIX if keep else ""
    zip_path = dest_dir / f"{cluster_path.name}_{stamp}{suffix}.zip"
    # 同一秒内被连续调用两次（比如"全部停止"时两个世界几乎同时停下，各
    # 自触发一次自动备份）不该让后一份静默覆盖前一份。
    n = 2
    while zip_path.exists():
        zip_path = dest_dir / f"{cluster_path.name}_{stamp}_{n}{suffix}.zip"
        n += 1

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for src, arcname in _backup_files(cluster_path):
            zf.write(src, arcname=arcname)
    # 修改时间记成开始打包的时刻：打包期间游戏又存的盘，下次 changed_since_last_backup 能看出来
    os.utime(zip_path, (started, started))

    _prune_old_backups(dest_dir)
    return zip_path


def shards_missing_from_backup(cluster_path: Path, backup_zip: Path) -> list[str]:
    """当前存档里有、备份里没有的世界目录（备份之后新增或改名的世界），恢复时会整个删掉。"""
    with zipfile.ZipFile(backup_zip) as zf:
        backed_up = {Path(name).parts[0].casefold() for name in zf.namelist()
                     if len(Path(name).parts) == 2 and Path(name).name == "server.ini"}
    return [shard.name for shard in list_shards(cluster_path) if shard.name.casefold() not in backed_up]


def restore_backup(cluster_path: Path, backup_zip: Path) -> None:
    """用备份 zip 覆盖存档当前状态（调用方须确认相关世界已停止）。

    坑：必须先删掉备份涉及的每一项再解压，否则比备份更新的存档槽会残留，游戏仍会加载最新槽位。
    备份里没有的世界目录整个删掉：留着会变成多出来的世界，改过文件夹名时还会和备份里的旧目录编号相同。
    """
    for name in shards_missing_from_backup(cluster_path, backup_zip):
        shutil.rmtree(cluster_path / name)
    for name in _CLUSTER_ITEMS:
        target = cluster_path / name
        if target.exists():
            target.unlink()
    for shard_dir in list_shards(cluster_path):
        for name in _SHARD_ITEMS:
            target = shard_dir / name
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()

    with zipfile.ZipFile(backup_zip) as zf:
        zf.extractall(cluster_path)


def _prune_old_backups(dest_dir: Path) -> None:
    """自动备份超过保留份数时删最旧的；带 _keep 标记的手动类备份不计数也不删。"""
    backups = sorted((p for p in dest_dir.glob("*.zip") if not is_kept_backup(p)), key=lambda p: p.name, reverse=True)
    for old in backups[get_backup_retention():]:
        old.unlink()


def get_backup_summary(zip_path: Path) -> dict:
    """解压到临时目录读取备份的基本信息（名称/模式/人数/进度）供恢复列表使用；读不出的字段不出现，不猜。"""
    from dstools.features.cluster_config.config_manager import load_cluster_config
    from dstools.features.save_browser.reader import get_save_summary, list_save_sessions

    info: dict = {}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(tmp_dir)

            config = load_cluster_config(tmp_dir)
            if config.network.get("cluster_name"):
                info["cluster_name"] = config.network["cluster_name"]
            if config.gameplay.get("game_mode"):
                info["game_mode"] = config.gameplay["game_mode"]
            if config.gameplay.get("max_players"):
                info["max_players"] = config.gameplay["max_players"]

            shards = list_shards(tmp_dir)
            shard_dir = find_master_dir(shards) or (shards[0] if shards else None)
            if shard_dir:
                sessions = list_save_sessions(shard_dir)
                if sessions:
                    info["summary"] = get_save_summary(sessions[-1])
    except (OSError, zipfile.BadZipFile):
        pass
    return info
