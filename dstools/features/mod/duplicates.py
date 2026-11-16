"""检测同时启用的 Mod 里可能重复的组合（只读，不依赖界面）。

两类信号：
1. 同名：modinfo 名称规范化后相同，多半是同一个 Mod 装了两份（工坊版与本地副本、作者重新上传等）。
2. 同路径脚本：两个 Mod 都在 ``scripts/`` 下带了相对路径相同、内容不同的 ``.lua``。游戏把每个启用 Mod
   的 ``scripts`` 目录依次加到 ``package.path`` 前面（scripts/mods.lua），``require`` 按模块名缓存，
   同名模块只会有一份生效，另一份等于被覆盖；内容相同的只是一起带的公共库，不提示。
"""

from __future__ import annotations

import hashlib
import os
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class SameNameGroup:
    name: str  # 第一个 Mod 的原始名称，供显示
    mod_ids: tuple[str, ...]


@dataclass(frozen=True)
class FileClash:
    mod_ids: tuple[str, ...]
    paths: tuple[str, ...]  # 相对 scripts/ 的路径，小写、正斜杠


@dataclass
class DuplicateReport:
    same_name: list[SameNameGroup] = field(default_factory=list)
    file_clashes: list[FileClash] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.same_name or self.file_clashes)

    @property
    def group_count(self) -> int:
        return len(self.same_name) + len(self.file_clashes)


def _normalize_name(name: str) -> str:
    """忽略大小写与所有空白。"""
    return "".join(name.split()).casefold()


def _script_files(mod_folder: Path) -> dict[str, Path]:
    """Mod 的 scripts 目录下全部 .lua，键为小写的相对路径（Windows 文件名不分大小写）。"""
    root = mod_folder / "scripts"
    result: dict[str, Path] = {}
    if not root.is_dir():
        return result
    for dirpath, _dirnames, filenames in os.walk(root):
        for filename in filenames:
            if filename.lower().endswith(".lua"):
                full = Path(dirpath) / filename
                result[full.relative_to(root).as_posix().lower()] = full
    return result


def _file_digest(path: Path) -> str | None:
    try:
        return hashlib.sha1(path.read_bytes()).hexdigest()
    except OSError:
        return None


def find_duplicates(mods: dict[str, tuple[str, Path | None]]) -> DuplicateReport:
    """mods 为 {Mod 键: (modinfo 名称, Mod 目录)}，只传同时启用的 Mod；目录缺失的只参与同名比较。"""
    report = DuplicateReport()

    by_name: dict[str, list[str]] = defaultdict(list)
    for mod_id, (name, _folder) in mods.items():
        key = _normalize_name(name or "")
        if key:
            by_name[key].append(mod_id)
    same_name_of: dict[str, int] = {}
    for ids in by_name.values():
        if len(ids) >= 2:
            same_name_of.update((mod_id, len(report.same_name)) for mod_id in ids)
            report.same_name.append(SameNameGroup(name=mods[ids[0]][0], mod_ids=tuple(ids)))

    owners: dict[str, dict[str, Path]] = defaultdict(dict)  # 相对路径 -> {Mod 键: 文件}
    for mod_id, (_name, folder) in mods.items():
        if folder is None or not folder.is_dir():
            continue
        for rel, full in _script_files(folder).items():
            owners[rel][mod_id] = full

    clash_paths: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for rel, files in owners.items():
        if len(files) < 2:
            continue
        digests = {mod_id: _file_digest(path) for mod_id, path in files.items()}
        if len(set(digests.values())) < 2:
            continue  # 内容完全相同（或都读不了）
        ids = tuple(sorted(files))
        # 同一个 Mod 的两份副本版本不同时文件也会不同，已在同名里提示，不再重复列出
        if len({same_name_of.get(mod_id, mod_id) for mod_id in ids}) < 2:
            continue
        clash_paths[ids].append(rel)
    for ids, paths in sorted(clash_paths.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        report.file_clashes.append(FileClash(mod_ids=ids, paths=tuple(sorted(paths))))
    return report
