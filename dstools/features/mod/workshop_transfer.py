"""把一个工坊目录里已下载的 Mod 复制到另一个工坊目录（不依赖界面），换 SteamCMD 或从 Steam 客户端改用 SteamCMD 时免去重新下载。

实测 Steam 客户端与 SteamCMD 下载的内容逐字节一致、清单 appworkshop_322330.acf 格式相同。只复制内容目录不够：
SteamCMD 通道按清单里的 timeupdated 判断是否最新，没有清单条目的会被当成"有更新"重新下载，所以把源清单里
对应条目原样搬过去（去掉 Steam 客户端的订阅者记录 subscribedby，SteamCMD 没有订阅），效果与它自己下载的一样。
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from dstools.features.mod.workshop_acf import (
    WorkshopAcfError, _decode, _line_span, _parse, _sections, read_acf_installed_sizes, read_acf_time_updated,
    workshop_acf_path,
)

# 目标还没有清单时用的空清单（字段与 SteamCMD 实际写出的一致）
_EMPTY_ACF = (
    '"AppWorkshop"\n{\n'
    '\t"appid"\t\t"322330"\n'
    '\t"SizeOnDisk"\t\t"0"\n'
    '\t"NeedsUpdate"\t\t"0"\n'
    '\t"NeedsDownload"\t\t"0"\n'
    '\t"TimeLastUpdated"\t\t"0"\n'
    '\t"TimeLastAppRan"\t\t"0"\n'
    '\t"LastBuildID"\t\t"0"\n'
    '\t"WorkshopItemsInstalled"\n\t{\n\t}\n'
    '\t"WorkshopItemDetails"\n\t{\n\t}\n'
    '}\n'
)
_SUBSCRIBED_BY_RE = re.compile(r'^[ \t]*"subscribedby"[^\n]*\n', re.MULTILINE)
_TEMP_SUFFIX = ".dstcamp-copy"


def _has_files(item_dir: Path) -> bool:
    """V2 有 modinfo.lua，V1 有 *_legacy.bin。"""
    try:
        return (item_dir / "modinfo.lua").is_file() or any(p.is_file() for p in item_dir.glob("*_legacy.bin"))
    except OSError:
        return False


def transferable_ids(source_content: Path, target_content: Path) -> list[str]:
    """源目录里文件完整且清单有记录、目标目录里还没有的工坊 ID，按数值排序。"""
    source_content, target_content = Path(source_content), Path(target_content)
    if source_content.resolve() == target_content.resolve():
        return []
    recorded = read_acf_time_updated(source_content)
    try:
        candidates = [p.name for p in source_content.iterdir() if p.is_dir() and p.name.isdigit()]
    except OSError:
        return []
    return sorted((wid for wid in candidates if int(wid) in recorded and _has_files(source_content / wid)
                   and not _has_files(target_content / wid)), key=int)


def transfer_size(source_content: Path, ids) -> int:
    """按源清单记录的安装大小估算要复制的字节数。"""
    sizes = read_acf_installed_sizes(Path(source_content))
    return sum(sizes.get(int(wid), 0) for wid in ids)


def build_merged_text(target_text: str | None, source_text: str, workshop_ids) -> str:
    """把源清单里这些 ID 的条目原样追加到目标清单（目标已有的不动），同步 SizeOnDisk；目标为 None 时从空清单开始。"""
    s_installed, s_details = _sections(_parse(source_text))
    text = target_text if target_text is not None else _EMPTY_ACF
    root = _parse(text)
    t_installed, t_details = _sections(root)
    size_node = root.child("SizeOnDisk", obj=False)
    if not (size_node.value or "").isdigit():
        raise WorkshopAcfError("清单 SizeOnDisk 无效")
    add_installed, add_details, added_size = [], [], 0
    for wid in dict.fromkeys(str(w) for w in workshop_ids):
        node = s_installed.get(wid)
        if node is None or wid in t_installed:
            continue
        start, end = _line_span(source_text, node)
        add_installed.append(source_text[start:end])
        added_size += int(node.child("size", obj=False).value or "0")
        detail = s_details.get(wid)
        if detail is not None and wid not in t_details:
            start, end = _line_span(source_text, detail)
            add_details.append(_SUBSCRIBED_BY_RE.sub("", source_text[start:end]))
    if not add_installed:
        return text
    edits = [(size_node.value_start, size_node.value_end, f'"{int(size_node.value) + added_size}"')]
    for section_name, blocks in (("WorkshopItemsInstalled", add_installed), ("WorkshopItemDetails", add_details)):
        if not blocks:
            continue
        section = root.child(section_name, obj=True)
        close_line = text.rfind("\n", 0, section.end - 1) + 1  # 节的右花括号必须独占一行，插在它前面
        if text[close_line:section.end].strip() != "}":
            raise WorkshopAcfError(f"清单 {section_name} 的结束括号不是独立行")
        edits.append((close_line, close_line, "".join(blocks)))
    result = text
    for start, end, replacement in sorted(edits, reverse=True):
        result = result[:start] + replacement + result[end:]
    _parse(result)  # 改写后必须仍是合法清单
    return result


def copy_workshop_items(source_content: Path, target_content: Path, workshop_ids,
                        on_item=None) -> tuple[list[str], list[tuple[str, str]]]:
    """逐个复制内容目录（先复制到临时目录再改名，中断不留半成品），全部复制完后一次合并清单。
    返回 (复制成功的 ID, [(失败的 ID, 原因)])；on_item(工坊 ID) 在每个开始复制前回调。
    调用方须保证这期间没有 SteamCMD 在运行（steamcmd_updater.run_when_idle）。"""
    source_content, target_content = Path(source_content), Path(target_content)
    target_content.mkdir(parents=True, exist_ok=True)
    copied, failures = [], []
    for wid in dict.fromkeys(str(w) for w in workshop_ids):
        if not (wid.isascii() and wid.isdigit()):
            # 会拼进路径并在失败时整目录删除，只接受纯数字 ID
            failures.append((wid, "工坊 ID 无效"))
            continue
        if on_item is not None:
            on_item(wid)
        target = target_content / wid
        temporary = target_content / f"{wid}{_TEMP_SUFFIX}"
        try:
            if _has_files(target):
                continue
            if temporary.exists():
                shutil.rmtree(temporary)
            shutil.copytree(source_content / wid, temporary)
            if target.exists():  # 没有完整文件的残留目录（中断的下载等）
                if os.path.isjunction(target) or target.is_symlink():
                    raise OSError("目标位置是联接，已拒绝覆盖")
                shutil.rmtree(target)
            os.replace(temporary, target)
            copied.append(wid)
        except OSError as exc:
            failures.append((wid, str(exc)))
            shutil.rmtree(temporary, ignore_errors=True)
    if copied:
        try:
            _merge_acf(source_content, target_content, copied)
        except (OSError, WorkshopAcfError) as exc:
            # 内容已复制但清单没合并：这些 Mod 会被当成"有更新"，重新下载即可恢复
            failures.extend((wid, f"清单合并失败：{exc}") for wid in copied)
            copied = []
    return copied, failures


def _merge_acf(source_content: Path, target_content: Path, workshop_ids) -> None:
    source_text = _decode(workshop_acf_path(source_content).read_bytes())
    target_path = workshop_acf_path(target_content)
    target_text = _decode(target_path.read_bytes()) if target_path.is_file() else None
    merged = build_merged_text(target_text, source_text, workshop_ids)
    if merged == target_text:
        return
    temporary = target_path.with_name(f"{target_path.name}.tmp.{os.getpid()}")
    try:
        temporary.write_bytes(merged.encode("utf-8"))
        os.replace(temporary, target_path)
    finally:
        if temporary.exists():
            temporary.unlink()
