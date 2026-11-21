"""检测同时启用的 Mod 里可能重复的组合（只读，不依赖界面）。

三类信号：
0. 同类名单：similar_mods.py 里人工审核登记的功能相同的 Mod（文件结构不同、靠上面两条查不出）。
1. 同名：modinfo 名称规范化后相同，多半是同一个 Mod 装了两份（工坊版与本地副本、作者重新上传等）。
2. 同路径脚本：两个 Mod 都在 ``scripts/`` 下带了相对路径相同、内容不同的 ``.lua``。游戏把每个启用 Mod
   的 ``scripts`` 目录依次加到 ``package.path`` 前面（scripts/mods.lua），``require`` 按模块名缓存，
   同名模块只会有一份生效，另一份等于被覆盖；内容相同的只是一起带的公共库，不提示。
   在 modinfo 的 ``mod_dependencies`` 里直接或间接依赖对方的（扩展包依赖本体，如岛屿冒险海难依赖核心），
   覆盖是作者有意为之，也不提示。
"""

from __future__ import annotations

import hashlib
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from dstools.features.mod import similar_mods
from dstools.features.mod.parser import _strip_lua_comments


# 同类名单的数据在 similar_mods.py（由 scripts/mod_conflict_tool.py 维护）：(类别键, 工坊 ID)
SIMILAR_MOD_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = tuple(
    (group["key"], tuple(group["mods"])) for group in similar_mods.GROUPS)


def similar_group_name(kind: str, lang: str) -> str:
    """类别显示名（zh/en），名称随名单数据一起维护，不在 strings.py 里。"""
    for group in similar_mods.GROUPS:
        if group["key"] == kind:
            return group["en"] if lang == "en" else group["zh"]
    return kind


@dataclass(frozen=True)
class SimilarGroup:
    kind: str  # similar_mods.GROUPS 的类别键
    mod_ids: tuple[str, ...]


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
    similar: list[SimilarGroup] = field(default_factory=list)
    same_name: list[SameNameGroup] = field(default_factory=list)
    file_clashes: list[FileClash] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.similar or self.same_name or self.file_clashes)

    @property
    def group_count(self) -> int:
        return len(self.similar) + len(self.same_name) + len(self.file_clashes)


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


_DEPENDENCIES_START = re.compile(r"(?<![\w.])mod_dependencies\s*=\s*\{")
# 依赖项的两种写法：{ workshop = "workshop-123" } 与按目录名/Mod 名的 { ["名称"] = true }
_DEPENDENCY_ITEM = re.compile(r"""workshop\s*=\s*["']([^"']+)["']|\[\s*["']([^"']+)["']\s*\]\s*=\s*true\b""")


def _declared_dependencies(mod_folder: Path) -> set[str]:
    """modinfo.lua 里各处 mod_dependencies 表声明的依赖（条件分支里的几份取并集），小写。

    只做文本匹配，不执行 Lua：用于消除误报，漏认只会多提示一条，不会影响加载。"""
    try:
        text = (mod_folder / "modinfo.lua").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    text = _strip_lua_comments(text)
    result: set[str] = set()
    for match in _DEPENDENCIES_START.finditer(text):
        depth, end = 1, match.end()
        while end < len(text) and depth:
            depth += {"{": 1, "}": -1}.get(text[end], 0)
            end += 1
        for item in _DEPENDENCY_ITEM.finditer(text, match.end(), end):
            result.add((item.group(1) or item.group(2)).strip().casefold())
    return result


def _dependency_components(mods: dict[str, tuple[str, Path | None]]) -> dict[str, str]:
    """按 mod_dependencies 把已启用 Mod 连成组（不分方向、可传递），返回 {Mod 键: 组代表}。"""
    parent = {mod_id: mod_id for mod_id in mods}

    def find(mod_id: str) -> str:
        while parent[mod_id] != mod_id:
            parent[mod_id] = parent[parent[mod_id]]
            mod_id = parent[mod_id]
        return mod_id

    # 依赖可写工坊键、目录名或 Mod 名，三者都映射回 Mod 键
    alias: dict[str, str] = {}
    for mod_id, (name, folder) in mods.items():
        alias[str(mod_id).casefold()] = mod_id
        if folder is not None:
            alias.setdefault(folder.name.casefold(), mod_id)
        if name:
            alias.setdefault(name.strip().casefold(), mod_id)
    for mod_id, (_name, folder) in mods.items():
        if folder is None:
            continue
        for dep in _declared_dependencies(folder):
            target = alias.get(dep)
            if target is not None:
                parent[find(mod_id)] = find(target)
    return {mod_id: find(mod_id) for mod_id in mods}


def find_duplicates(mods: dict[str, tuple[str, Path | None]]) -> DuplicateReport:
    """mods 为 {Mod 键: (modinfo 名称, Mod 目录)}，只传同时启用的 Mod；目录缺失的只参与同名比较。"""
    report = DuplicateReport()

    by_workshop_id = {str(mod_id).removeprefix("workshop-"): mod_id for mod_id in mods}
    similar_of: dict[str, int] = {}
    for kind, workshop_ids in SIMILAR_MOD_GROUPS:
        ids = tuple(by_workshop_id[wid] for wid in workshop_ids if wid in by_workshop_id)
        if len(ids) >= 2:
            similar_of.update((mod_id, len(report.similar)) for mod_id in ids)
            report.similar.append(SimilarGroup(kind=kind, mod_ids=ids))

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

    component_of = _dependency_components(mods)
    clash_paths: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for rel, files in owners.items():
        if len(files) < 2:
            continue
        # 全部由依赖关系连在一起的（扩展包覆盖本体的文件），是有意覆盖
        if len({component_of[mod_id] for mod_id in files}) < 2:
            continue
        digests = {mod_id: _file_digest(path) for mod_id, path in files.items()}
        if len(set(digests.values())) < 2:
            continue  # 内容完全相同（或都读不了）
        ids = tuple(sorted(files))
        # 同一个 Mod 的两份副本版本不同时文件也会不同，已在同名里提示，不再重复列出
        if len({same_name_of.get(mod_id, mod_id) for mod_id in ids}) < 2:
            continue
        # 全部属于同一组同类名单的，已在同类里提示
        if len({similar_of.get(mod_id, mod_id) for mod_id in ids}) < 2:
            continue
        clash_paths[ids].append(rel)
    for ids, paths in sorted(clash_paths.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        report.file_clashes.append(FileClash(mod_ids=ids, paths=tuple(sorted(paths))))
    return report
