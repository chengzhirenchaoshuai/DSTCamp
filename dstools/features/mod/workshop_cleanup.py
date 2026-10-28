"""安全删除 Steam Workshop 的不完整残留目录。"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from dstools.features.mod.parser import find_workshop_dir, is_workshop_content_id
from dstools.features.mod.workshop_acf import read_acf_subscribed_ids
from dstools.features.mod.workshop_api import WorkshopItemState

# Steam API 只回答当前登录账号是否订阅；清单里仍有 subscribedby 时删掉会被 Steam 重新下载
_OTHER_ACCOUNT_ERROR = "本机其他 Steam 账号仍订阅此 Mod，删除后会被 Steam 重新下载"


@dataclass(frozen=True)
class ResidualCleanupContext:
    """一次批量清理共享的只读安全证据。"""

    workshop_root: Path | None
    legacy_runtime_roots: tuple[Path, ...]
    running_processes: tuple[str, ...]


def build_residual_cleanup_context(
    *, running_processes: tuple[str, ...] | None = None
) -> ResidualCleanupContext:
    """集中读取一次进程、根目录和 Legacy 包证据，避免逐项重复扫描。"""
    from dstools.features.mod.legacy_v1 import (
        discover_legacy_runtime_roots,
        running_dst_processes,
    )

    processes = (
        tuple(running_processes)
        if running_processes is not None
        else running_dst_processes()
    )
    return ResidualCleanupContext(
        workshop_root=find_workshop_dir(),
        legacy_runtime_roots=tuple(discover_legacy_runtime_roots()),
        running_processes=processes,
    )


def format_residual_directory_tree(paths: tuple[Path, ...]) -> str:
    """生成确认框使用的浅层目录结构，避免递归枚举大型残留目录。"""
    blocks = []
    for raw_path in paths:
        path = Path(raw_path)
        lines = [f"{path}{os.sep}"]
        try:
            children = sorted(path.iterdir(), key=lambda item: item.name.casefold())
        except OSError:
            children = []
        if not children:
            lines.append("（空文件夹）")
        else:
            limit = 80
            for child in children[:limit]:
                suffix = os.sep if child.is_dir() else ""
                lines.append(f"{child.name}{suffix}")
            if len(children) > limit:
                lines.append(f"……另有 {len(children) - limit} 项")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def delete_workshop_residual(
    workshop_id: int,
    path: Path,
    steam_state: WorkshopItemState | None,
    *,
    context: ResidualCleanupContext | None = None,
) -> Path:
    """永久删除确认未受 Steam 管理的标准 322330 残留目录。"""
    from dstools.features.mod.legacy_v1 import running_dst_processes

    text_id = str(int(workshop_id))
    if not is_workshop_content_id(text_id):
        raise ValueError("Workshop ID 无效")
    root = context.workshop_root if context is not None else find_workshop_dir()
    if root is None:
        raise ValueError("找不到 Steam Workshop 内容目录")
    candidate = Path(path)
    try:
        if candidate.parent.resolve() != root.resolve() or candidate.name != text_id:
            raise ValueError("残留目录不在标准 Workshop 路径内")
    except OSError as exc:
        raise ValueError(f"无法确认残留目录：{exc}") from exc
    if candidate.is_symlink() or (
        hasattr(os.path, "isjunction") and os.path.isjunction(candidate)
    ):
        raise ValueError("拒绝处理链接或目录联接")
    if not candidate.is_dir():
        raise ValueError("残留目录不存在")
    # 已取消订阅的 V1 Mod 在这里只剩 *_legacy.bin 下载包，与 V2 目录同样按残留清理
    if steam_state is None:
        raise ValueError("无法确认 Steam 状态")
    if (
        steam_state.subscribed
        or steam_state.downloading
        or steam_state.download_pending
    ):
        raise ValueError("Steam 仍在订阅或下载此 Mod")
    if int(text_id) in read_acf_subscribed_ids(root):
        raise ValueError(_OTHER_ACCOUNT_ERROR)
    running = (
        context.running_processes
        if context is not None
        else running_dst_processes()
    )
    if running:
        raise ValueError("游戏或专用服务器正在运行，请退出后再清理")

    deleted = candidate.resolve()
    shutil.rmtree(candidate)
    return deleted


def delete_legacy_runtime_residual(
    workshop_id: int,
    path: Path,
    steam_state: WorkshopItemState | None,
    *,
    context: ResidualCleanupContext | None = None,
) -> Path:
    """永久删除 Steam 已取消管理的 V1 ``mods/workshop-<id>`` 目录。"""
    from dstools.features.mod.legacy_v1 import (
        discover_legacy_runtime_roots,
        find_legacy_package_ids,
        running_dst_processes,
        same_location,
    )

    text_id = str(int(workshop_id))
    if not is_workshop_content_id(text_id):
        raise ValueError("Workshop ID 无效")
    candidate = Path(path)
    roots = (
        context.legacy_runtime_roots
        if context is not None
        else tuple(discover_legacy_runtime_roots())
    )
    if not any(same_location(candidate.parent, root) for root in roots):
        raise ValueError("V1 残留目录不在标准 mods 路径内")
    if candidate.name != f"workshop-{text_id}":
        raise ValueError("V1 残留目录名称不符合规范")
    if candidate.is_symlink() or (
        hasattr(os.path, "isjunction") and os.path.isjunction(candidate)
    ):
        raise ValueError("拒绝处理链接或目录联接")
    if not candidate.is_dir() or not (candidate / "modinfo.lua").is_file():
        raise ValueError("V1 残留目录不存在或内容不完整")
    # 批量清理会先删 322330 下的包再删运行目录，必须按当前磁盘状态重新确认，
    # 不能沿用批量开始时的快照
    if context is not None:
        content_dir = (
            context.workshop_root / text_id if context.workshop_root is not None else None
        )
        package_exists = content_dir is not None and any(
            path.is_file() for path in content_dir.glob("*_legacy.bin")
        )
    else:
        package_exists = int(text_id) in find_legacy_package_ids()
        root = find_workshop_dir()
        content_dir = root / text_id if root is not None else None
    if package_exists:
        raise ValueError("Legacy 下载包仍然存在，不能按取消订阅残留处理")
    if steam_state is None:
        raise ValueError("无法确认 Steam 状态")
    # Installed 位只在内容目录仍存在时才代表 Steam 管理着它；目录已删时只是
    # 清单里的陈旧记录（另由 Steam 下载记录清理处理）
    content_present = content_dir is not None and os.path.lexists(content_dir)
    if (
        steam_state.subscribed
        or (steam_state.installed and content_present)
        or steam_state.downloading
        or steam_state.download_pending
    ):
        raise ValueError("Steam 仍在订阅、安装或下载此 Mod")
    workshop_root = context.workshop_root if context is not None else find_workshop_dir()
    if int(text_id) in read_acf_subscribed_ids(workshop_root):
        raise ValueError(_OTHER_ACCOUNT_ERROR)
    running = (
        context.running_processes
        if context is not None
        else running_dst_processes()
    )
    if running:
        raise ValueError("游戏或专用服务器正在运行，请退出后再清理")

    deleted = candidate.resolve()
    shutil.rmtree(candidate)
    return deleted


@dataclass(frozen=True)
class OtherAccountCleanupResult:
    removed: tuple[str, ...]
    errors: dict[str, str]
    steam_restarted: bool


def force_remove_other_account_items(
    content_root: Path,
    workshop_ids,
    *,
    is_steam_running,
    shutdown_steam,
    launch_steam,
    running_dst_processes,
) -> OtherAccountCleanupResult:
    """强制清理本机其他 Steam 账号订阅的 Mod：退出 Steam，删除 322330 内容目录并清除清单记录，再按原状态重启 Steam。

    调用方必须已确认当前登录账号未订阅这些 Mod。订阅仍在对方账号上，对方下次在本机登录 Steam 时会重新下载。
    清单必须在 Steam 退出后改写，否则 Steam 退出时会用内存状态覆盖。"""
    from dstools.features.mod.workshop_acf import (
        WorkshopAcfError, prune_workshop_acf, read_workshop_acf, workshop_acf_path,
    )

    if running_dst_processes():
        raise ValueError("游戏或专用服务器正在运行，请退出后再清理")
    root = Path(content_root)
    acf_path = workshop_acf_path(root)
    if not acf_path.is_file():
        raise ValueError(f"找不到 Steam 清单：{acf_path}")
    was_running = bool(is_steam_running())
    if was_running:
        shutdown_steam()
    removed: list[str] = []
    errors: dict[str, str] = {}
    try:
        if is_steam_running():
            raise ValueError("Steam 未能退出，已停止清理")
        acf = read_workshop_acf(acf_path)
        for wid in dict.fromkeys(str(int(w)) for w in workshop_ids):
            item = acf.items.get(wid)
            # 只处理退出 Steam 后仍是"有人订阅"的记录；不再订阅的交给普通残留清理
            if not is_workshop_content_id(wid) or item is None or not item.subscribed:
                errors[wid] = "Steam 清单中已没有其他账号的订阅记录"
                continue
            candidate = root / wid
            try:
                if candidate.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(candidate)):
                    raise ValueError("拒绝处理链接或目录联接")
                if candidate.exists():
                    if not candidate.is_dir():
                        raise ValueError("内容路径不是目录")
                    shutil.rmtree(candidate)
                removed.append(wid)
            except (OSError, ValueError) as exc:
                errors[wid] = str(exc)
        if removed:
            try:
                prune_workshop_acf(acf, removed, allow_subscribed=True)
            except (OSError, WorkshopAcfError) as exc:
                # 文件已删但记录还在：Steam 会重新下载，如实报告
                errors.update({wid: f"文件已删除，但清除 Steam 记录失败：{exc}" for wid in removed})
                removed = []
    finally:
        if was_running:
            launch_steam()
    return OtherAccountCleanupResult(tuple(removed), errors, was_running)
