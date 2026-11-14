"""精确修改 Steam 的 ``appworkshop_322330.acf``。

只删目录不够：清单仍记为已安装的条目会被 Steam 当成损坏重新下载。Steam 完全退出后，把未订阅条目从
WorkshopItemsInstalled/WorkshopItemDetails 中整行删除并同步 SizeOnDisk，其余字节不变，原子写入。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

DST_APP_ID = "322330"
ACF_NAME = f"appworkshop_{DST_APP_ID}.acf"

_TOKEN_RE = re.compile(
    r'\s+|//[^\r\n]*|(?P<quoted>"(?:\\.|[^"\\])*")|(?P<brace>[{}])', re.S
)


class WorkshopAcfError(ValueError):
    """清单格式异常或安全检查未通过，不允许改写。"""


@dataclass(frozen=True)
class _Node:
    key: str
    start: int
    end: int
    value: str | None = None
    value_start: int = 0
    value_end: int = 0
    children: tuple["_Node", ...] = ()

    @property
    def is_object(self) -> bool:
        return self.value is None

    def child(self, key: str, *, obj: bool) -> "_Node":
        matches = [node for node in self.children if node.key == key]
        if len(matches) != 1 or matches[0].is_object != obj:
            raise WorkshopAcfError(f"清单中 {key} 缺失、重复或类型错误")
        return matches[0]


@dataclass(frozen=True)
class AcfWorkshopItem:
    workshop_id: str
    size: int
    subscribed: bool
    subscribed_by: str = ""  # subscribedby 的值：订阅者的 Steam AccountID，多个账号订阅时以逗号分隔
    time_updated: int = 0  # 已安装版本在工坊的更新时间，与 Web API 的 time_updated 对应（实测）


@dataclass(frozen=True)
class WorkshopAcf:
    path: Path
    raw: bytes
    items: dict[str, AcfWorkshopItem]


def workshop_acf_path(content_root: Path) -> Path:
    """``.../workshop/content/322330`` 对应的清单路径。"""
    return Path(content_root).parent.parent / ACF_NAME


def _tokens(text: str) -> list[tuple[str, str, int, int]]:
    tokens = []
    offset = 0
    while offset < len(text):
        match = _TOKEN_RE.match(text, offset)
        if match is None or match.end() == offset:
            raise WorkshopAcfError(f"清单格式无法解析，字符位置 {offset}")
        if match.group("quoted"):
            tokens.append(("str", match.group(), offset, match.end()))
        elif match.group("brace"):
            tokens.append((match.group(), match.group(), offset, match.end()))
        offset = match.end()
    return tokens


def _parse_node(tokens, pos: int) -> tuple[_Node, int]:
    if pos >= len(tokens) or tokens[pos][0] != "str":
        raise WorkshopAcfError("清单缺少键名")
    key_tok = tokens[pos]
    key = key_tok[1][1:-1]
    pos += 1
    if pos >= len(tokens):
        raise WorkshopAcfError(f"清单键 {key} 没有值")
    val_tok = tokens[pos]
    pos += 1
    if val_tok[0] == "str":
        return _Node(key, key_tok[2], val_tok[3], val_tok[1][1:-1], val_tok[2], val_tok[3]), pos
    if val_tok[0] != "{":
        raise WorkshopAcfError(f"清单键 {key} 后需要值或左花括号")
    children = []
    while pos < len(tokens) and tokens[pos][0] != "}":
        node, pos = _parse_node(tokens, pos)
        children.append(node)
    if pos >= len(tokens):
        raise WorkshopAcfError(f"清单键 {key} 缺少右花括号")
    return _Node(key, key_tok[2], tokens[pos][3], children=tuple(children)), pos + 1


def _parse(text: str) -> _Node:
    tokens = _tokens(text)
    root, pos = _parse_node(tokens, 0)
    if pos != len(tokens) or root.key != "AppWorkshop" or not root.is_object:
        raise WorkshopAcfError("清单顶层结构不是单个 AppWorkshop 对象")
    if root.child("appid", obj=False).value != DST_APP_ID:
        raise WorkshopAcfError("清单 AppID 不匹配")
    return root


def _entries(section: _Node, name: str) -> dict[str, _Node]:
    found: dict[str, _Node] = {}
    for node in section.children:
        if not node.is_object or not node.key.isdigit() or node.key in found:
            raise WorkshopAcfError(f"{name} 含异常或重复条目")
        found[node.key] = node
    return found


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkshopAcfError("清单不是有效的 UTF-8 文本") from exc


def _sections(root: _Node) -> tuple[dict[str, _Node], dict[str, _Node]]:
    installed = _entries(root.child("WorkshopItemsInstalled", obj=True), "WorkshopItemsInstalled")
    details = _entries(root.child("WorkshopItemDetails", obj=True), "WorkshopItemDetails")
    return installed, details


def read_workshop_acf(path: Path) -> WorkshopAcf:
    """解析清单；``subscribed`` 以详情条目里的 ``subscribedby`` 为准。"""
    path = Path(path)
    raw = path.read_bytes()
    installed, details = _sections(_parse(_decode(raw)))
    items = {}
    for wid, node in installed.items():
        size_text = node.child("size", obj=False).value or "0"
        if not size_text.isdigit():
            raise WorkshopAcfError(f"条目 {wid} 的大小无效")
        detail = details.get(wid)
        subscriber = next((c for c in detail.children if c.key == "subscribedby"), None) if detail else None
        updated = next((c.value for c in node.children if c.key == "timeupdated"), "") or ""
        items[wid] = AcfWorkshopItem(wid, int(size_text), subscriber is not None,
                                     (subscriber.value or "") if subscriber is not None else "",
                                     int(updated) if updated.isdigit() else 0)
    return WorkshopAcf(path, raw, items)


def read_acf_subscribers(content_root: Path | None) -> dict[int, str]:
    """清单中带 ``subscribedby`` 的 Workshop ID 及订阅者 AccountID，即本机某个 Steam 账号仍订阅着它；读不到返回空。

    内容目录和清单是本机全部 Steam 账号共用的，而 Steam API 只回答当前登录账号是否订阅，
    这是判断"其他账号订阅"的唯一本地证据。"""
    if content_root is None:
        return {}
    try:
        acf = read_workshop_acf(workshop_acf_path(content_root))
    except (OSError, WorkshopAcfError):
        return {}
    return {int(wid): item.subscribed_by for wid, item in acf.items.items() if item.subscribed and wid.isdigit()}


def read_acf_installed_sizes(content_root: Path | None) -> dict[int, int]:
    """清单里 Steam 实际安装的大小（字节）；读不到返回空。"""
    if content_root is None:
        return {}
    try:
        acf = read_workshop_acf(workshop_acf_path(content_root))
    except (OSError, WorkshopAcfError):
        return {}
    return {int(wid): item.size for wid, item in acf.items.items() if wid.isdigit()}


def read_acf_time_updated(content_root: Path | None) -> dict[int, int]:
    """清单里已安装各项的工坊更新时间；读不到返回空。"""
    if content_root is None:
        return {}
    try:
        acf = read_workshop_acf(workshop_acf_path(content_root))
    except (OSError, WorkshopAcfError):
        return {}
    return {int(wid): item.time_updated for wid, item in acf.items.items() if wid.isdigit()}


def read_acf_subscribed_ids(content_root: Path | None) -> set[int]:
    """清单中仍有账号订阅的 Workshop ID。"""
    return set(read_acf_subscribers(content_root))


def read_acf_installed_ids(content_root: Path | None) -> set[int] | None:
    """清单 ``WorkshopItemsInstalled`` 里登记的 Workshop ID；清单缺失或读不出来返回 ``None``。

    与 :func:`read_acf_installed_sizes` 的区别只在读不到时：那个返回空字典，无法与"清单里真的没有
    任何条目"区分。判断"文件在但清单没登记"必须区分这两种情况，否则读不到清单就会把所有 Mod 误报成未登记。"""
    if content_root is None:
        return None
    try:
        acf = read_workshop_acf(workshop_acf_path(content_root))
    except (OSError, WorkshopAcfError):
        return None
    return {int(wid) for wid in acf.items if wid.isdigit()}


def _line_span(text: str, node: _Node) -> tuple[int, int]:
    """返回条目所在的完整行区间；条目必须独占这些行。"""
    line_start = text.rfind("\n", 0, node.start) + 1
    if text[line_start:node.start].strip(" \t"):
        raise WorkshopAcfError(f"清单条目 {node.key} 不是独立行")
    end = node.end
    while end < len(text) and text[end] != "\n":
        if text[end] not in " \t\r":
            raise WorkshopAcfError(f"清单条目 {node.key} 的结束行含有其它内容")
        end += 1
    return line_start, min(end + 1, len(text))


def build_pruned_text(text: str, workshop_ids, *, allow_subscribed: bool = False) -> str:
    """生成删除指定未订阅条目后的清单文本；任何一项不满足条件都整体拒绝。
    allow_subscribed=True 只用于强制清理本机其他账号订阅的 Mod（调用方已确认当前账号未订阅）。"""
    root = _parse(text)
    installed, details = _sections(root)
    size_node = root.child("SizeOnDisk", obj=False)
    if not (size_node.value or "").isdigit():
        raise WorkshopAcfError("清单 SizeOnDisk 无效")
    edits: list[tuple[int, int, str]] = []
    removed_size = 0
    for wid in sorted(set(str(w) for w in workshop_ids)):
        node = installed.get(wid)
        if node is None:
            continue
        detail = details.get(wid)
        if (not allow_subscribed and detail is not None
                and any(c.key == "subscribedby" for c in detail.children)):
            raise WorkshopAcfError(f"Steam 仍标记已订阅，拒绝清除：{wid}")
        removed_size += int(node.child("size", obj=False).value or "0")
        start, end = _line_span(text, node)
        edits.append((start, end, ""))
        if detail is not None:
            start, end = _line_span(text, detail)
            edits.append((start, end, ""))
    if not edits:
        return text
    new_size = max(0, int(size_node.value) - removed_size)
    edits.append((size_node.value_start, size_node.value_end, f'"{new_size}"'))
    result = text
    for start, end, replacement in sorted(edits, reverse=True):
        result = result[:start] + replacement + result[end:]
    _parse(result)  # 改写后必须仍是合法清单
    return result


def prune_workshop_acf(acf: WorkshopAcf, workshop_ids, *, allow_subscribed: bool = False) -> bool:
    """从清单删除指定未订阅条目，返回是否改写（Steam 必须已退出，否则退出时会用内存状态覆盖）。"""
    path = Path(acf.path)
    current = path.read_bytes()
    if current != acf.raw:
        raise WorkshopAcfError("清单在检查期间发生变化，已停止写入")
    text = _decode(current)
    updated = build_pruned_text(text, workshop_ids, allow_subscribed=allow_subscribed)
    if updated == text:
        return False
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        temporary.write_bytes(updated.encode("utf-8"))
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return True


def find_orphan_records(acf: WorkshopAcf, content_root: Path, exclude_ids=()) -> list[str]:
    """未订阅、内容目录已不存在且未被排除的记录——Steam 会把它们重新下载。"""
    excluded = {str(wid) for wid in exclude_ids}
    root = Path(content_root)
    return sorted(
        (wid for wid, item in acf.items.items()
         if not item.subscribed and wid not in excluded and not os.path.lexists(root / wid)),
        key=int,
    )


@dataclass(frozen=True)
class OrphanCleanupResult:
    cleared: tuple[str, ...]
    steam_restarted: bool


def clear_orphan_records(
    content_root: Path,
    workshop_ids,
    *,
    is_steam_running,
    shutdown_steam,
    launch_steam,
    running_dst_processes,
) -> OrphanCleanupResult:
    """退出 Steam 后重新读取并筛选清单、清除孤立记录，再按原状态重启 Steam。"""
    if running_dst_processes():
        raise WorkshopAcfError("游戏或专用服务器正在运行，请退出后再清理")
    path = workshop_acf_path(content_root)
    if not path.is_file():
        raise WorkshopAcfError(f"找不到 Steam 清单：{path}")
    was_running = bool(is_steam_running())
    if was_running:
        shutdown_steam()
    try:
        acf = read_workshop_acf(path)
        still_orphan = set(find_orphan_records(acf, content_root))
        targets = [wid for wid in (str(w) for w in workshop_ids) if wid in still_orphan]
        if is_steam_running():
            raise WorkshopAcfError("Steam 已重新启动，已停止写入")
        if targets:
            prune_workshop_acf(acf, targets)
    finally:
        if was_running:
            launch_steam()
    return OrphanCleanupResult(tuple(targets), was_running)
