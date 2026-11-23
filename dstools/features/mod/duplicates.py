"""检测同时启用的 Mod 里可能重复的组合（只读，不依赖界面）。

三类信号：
0. 同类名单：similar_mods.py 里人工审核登记的功能相同的 Mod（文件结构不同、靠上面两条查不出）。
1. 同名：modinfo 名称规范化后相同，多半是同一个 Mod 装了两份（工坊版与本地副本、作者重新上传等）。
2. 同路径脚本：两个 Mod 都在 ``scripts/`` 下带了相对路径相同、内容不同的 ``.lua``。游戏把每个启用 Mod
   的 ``scripts`` 目录依次加到 ``package.path`` 前面（scripts/mods.lua），``require`` 按模块名缓存，
   同名模块只会有一份生效，另一份等于被覆盖；内容相同的只是一起带的公共库，不提示。
   在 modinfo 的 ``mod_dependencies`` 里直接或间接依赖对方的（扩展包依赖本体，如岛屿冒险海难依赖核心），
   覆盖是作者有意为之，也不提示；modinfo 的 ``author`` 完全相同的（同一作者的系列 Mod，如神话书说与
   只在介绍里要求先开本体、没写依赖的神话人物）同样视为有意共用，不提示。
   ``modimport`` 按 Mod 自己的根目录加载（scripts/mods.lua 里 ``kleiloadlua(env.MODROOT..modulename)``），
   不走 ``package.path``；每个带该文件的 Mod 都只用 modimport 加载、没有 require/loadfile 到它时，也不提示。
"""

from __future__ import annotations

import hashlib
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from dstools.features.mod import similar_mods


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
    text = _strip_comments_fast(text)
    result: set[str] = set()
    for match in _DEPENDENCIES_START.finditer(text):
        depth, end = 1, match.end()
        while end < len(text) and depth:
            depth += {"{": 1, "}": -1}.get(text[end], 0)
            end += 1
        for item in _DEPENDENCY_ITEM.finditer(text, match.end(), end):
            result.add((item.group(1) or item.group(2)).strip().casefold())
    return result


_AUTHOR = re.compile(r"(?m)^[ \t]*author[ \t]*=[ \t]*(.+?)[ \t]*;?[ \t]*$")


def _declared_author(mod_folder: Path) -> str:
    """modinfo.lua 顶层 author 赋值的右侧原文（去空白、不分大小写），用于判断同一作者；没写或写空串返回 ""。

    只比较原文，不执行 Lua：写成表达式（如按语言选择）的两个 Mod 原文相同也算同一作者。"""
    try:
        text = (mod_folder / "modinfo.lua").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    match = _AUTHOR.search(_strip_comments_fast(text))
    if match is None:
        return ""
    value = "".join(match.group(1).split()).casefold()
    return "" if value in ('""', "''") else value


def _dependency_components(mods: dict[str, tuple[str, Path | None]]) -> dict[str, str]:
    """按 mod_dependencies 与相同的 author 把已启用 Mod 连成组（不分方向、可传递），返回 {Mod 键: 组代表}。"""
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
    # 同一作者的系列 Mod 共用脚本是作者自己维护的，与声明依赖同等对待
    first_of_author: dict[str, str] = {}
    for mod_id, (_name, folder) in mods.items():
        author = _declared_author(folder) if folder is not None else ""
        if not author:
            continue
        if author in first_of_author:
            parent[find(mod_id)] = find(first_of_author[author])
        else:
            first_of_author[author] = mod_id
    return {mod_id: find(mod_id) for mod_id in mods}


# 游戏自己按名字 require 的目录（AddComponent、PrefabFiles、SetStateGraph），Mod 文本里看不到，不按 modimport 排除
_GAME_REQUIRED_DIRS = ("components/", "prefabs/", "stategraphs/")
_LOADER_CALL = re.compile(r"\b(modimport|require|loadfile)\b\s*")
_LUA_STRING = re.compile(r""""((?:[^"\\\n]|\\.)*)"|'((?:[^'\\\n]|\\.)*)'""")
# 一次扫过字符串与注释：字符串原样保留（里面的 "--" 不是注释），注释换成空格
_LUA_STRING_OR_COMMENT = re.compile(
    r"""(?P<str>"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'|\[(?P<eq>=*)\[.*?\](?P=eq)\])"""
    r"""|--\[(?P<ceq>=*)\[.*?\](?P=ceq)\]|--[^\n]*""",
    re.DOTALL)


def _strip_comments_fast(text: str) -> str:
    """去掉 Lua 注释（不保留字符位置，比 parser._strip_lua_comments 快一个数量级）。"""
    return _LUA_STRING_OR_COMMENT.sub(lambda m: m.group("str") or " ", text)


def _call_argument(text: str, pos: int) -> str | None:
    """pos 处若是 ``(表达式)`` 返回括号内文本，若是单个字符串字面量（Lua 的 f"x" 调用写法）返回它，否则 None。"""
    if pos < len(text) and text[pos] in "\"'":
        match = _LUA_STRING.match(text, pos)
        return match.group(0) if match else None
    if pos >= len(text) or text[pos] != "(":
        return None
    depth, i = 0, pos
    while i < len(text):
        char = text[i]
        if char in "\"'":
            match = _LUA_STRING.match(text, i)
            if match is None:
                return None
            i = match.end()
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[pos + 1:i]
        i += 1
    return None


def _argument_parts(expr: str) -> list[str | None]:
    """把参数表达式拆成字符串字面量（小写、反斜杠转正斜杠）与非字面量（None），相邻非字面量合并。"""
    parts: list[str | None] = []
    last = 0
    for match in _LUA_STRING.finditer(expr):
        gap = expr[last:match.start()].replace("..", "")
        if gap.strip() and (not parts or parts[-1] is not None):
            parts.append(None)
        literal = match.group(1) if match.group(1) is not None else match.group(2)
        parts.append(literal.replace("\\\\", "/").replace("\\", "/").casefold())
        last = match.end()
    if expr[last:].replace("..", "").strip() and (not parts or parts[-1] is not None):
        parts.append(None)
    return parts


def _loader_patterns(kind: str, parts: list[str | None]) -> re.Pattern | None:
    """把一次加载调用换成匹配「相对 scripts/ 的小写路径」的正则；认不出或完全动态的返回 None。"""
    literals = "".join(part for part in parts if part is not None)
    if kind == "modimport":
        # 相对 Mod 根目录，只关心 scripts/ 下的；拼接部分只当一级路径，且要有 scripts/ 之外的字面量
        if not parts or parts[0] is None or not parts[0].startswith("scripts/"):
            return None
        if len(literals.removesuffix(".lua")) <= len("scripts/"):
            return None
        parts = [parts[0][len("scripts/"):], *parts[1:]]
        wildcard = "[^/]*"
    else:
        if not literals:
            return None  # 完全动态（如 require(v)）认不出加载了什么，忽略
        if kind == "require":
            parts = [None if p is None else p.replace(".", "/") for p in parts]  # 游戏把模块名的 . 换成 /
        else:  # 游戏改写的 loadfile 去掉 .lua 与 scripts/ 后按模块名查找（scripts/main.lua）
            parts = [None if p is None else p.replace(".lua", "").replace("scripts/", "") for p in parts]
        wildcard = ".*"
    body = "".join(wildcard if part is None else re.escape(part) for part in parts)
    return re.compile(rf"{body}(?:\.lua)?")


def _loader_refs(mod_folder: Path, kinds: tuple[str, ...]) -> list[re.Pattern]:
    """扫描 Mod 全部 .lua 里 kinds 这几种加载调用，返回路径正则。

    先按原文是否含调用名过滤文件：modimport 只出现在少数文件里，require 则几乎处处都有，
    所以调用方先查 modimport、确有需要才查 require/loadfile。"""
    patterns: list[re.Pattern] = []
    for dirpath, _dirnames, filenames in os.walk(mod_folder):
        for filename in filenames:
            if not filename.lower().endswith(".lua"):
                continue
            try:
                text = (Path(dirpath) / filename).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if not any(kind in text for kind in kinds):
                continue
            text = _strip_comments_fast(text)
            for match in _LOADER_CALL.finditer(text):
                if match.group(1) not in kinds:
                    continue
                expr = _call_argument(text, match.end())
                if expr is None:
                    continue
                pattern = _loader_patterns(match.group(1), _argument_parts(expr))
                if pattern is not None:
                    patterns.append(pattern)
    return patterns


def _only_modimported(rel: str, ids: tuple[str, ...], mods: dict[str, tuple[str, Path | None]],
                      cache: dict[tuple[str, str], list[re.Pattern]]) -> bool:
    """rel 在每个 Mod 里都由 modimport 加载、且没有 require/loadfile 到它（不会经 package.path 互相顶掉）。"""
    if rel.startswith(_GAME_REQUIRED_DIRS):
        return False

    def refs(mod_id: str, kind: str) -> list[re.Pattern]:
        key = (mod_id, kind)
        if key not in cache:
            kinds = ("modimport",) if kind == "import" else ("require", "loadfile")
            cache[key] = _loader_refs(mods[mod_id][1], kinds)
        return cache[key]

    if not all(any(p.fullmatch(rel) for p in refs(mod_id, "import")) for mod_id in ids):
        return False
    return not any(p.fullmatch(rel) for mod_id in ids for p in refs(mod_id, "load"))


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
    loader_refs: dict[tuple[str, str], list[re.Pattern]] = {}  # 按需扫描，只扫有冲突候选的 Mod
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
        if _only_modimported(rel, ids, mods, loader_refs):
            continue
        clash_paths[ids].append(rel)
    for ids, paths in sorted(clash_paths.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        report.file_clashes.append(FileClash(mod_ids=ids, paths=tuple(sorted(paths))))
    return report
