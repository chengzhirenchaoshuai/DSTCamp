"""三种内网穿透映射共用的界面辅助。

- 开启前检查存档是否开着仅局域网/离线模式：映射会把 server_port 改成远程端口，而这两个模式要求端口在
  10998~11018，二者不能同时生效；开启映射时就提示。
- 开启前检查世界是否已被另一种映射方式接管：多种方式会互相覆盖 server_port 等配置。
- 删除存档时列出要一并关闭的映射方式，汇总未能删除的远程隧道。
"""

from __future__ import annotations

from dstools.i18n import t
from dstools.qt import dialogs
from dstools.shared.server_ports import disable_lan_restrictions, lan_restriction_names


def ensure_lan_free_for_mapping(parent, ctx, cluster) -> bool:
    """返回 True 表示可以继续开启映射；用户取消或关闭失败返回 False。"""
    modes = lan_restriction_names(cluster)
    if not modes:
        return True
    names = "、".join(modes)
    choice = dialogs.ask_choice(
        parent, t("lan_conflict.title"), t("lan_conflict.enable_msg", modes=names),
        [(t("lan_conflict.disable_and_map_btn", modes=names), "disable"), (t("dlg.cancel_btn"), "cancel")],
        default="cancel", min_width=840)
    if choice != "disable":
        return False
    try:
        disable_lan_restrictions(cluster)
    except (OSError, ValueError) as exc:
        dialogs.show_error(parent, t("local.port_repair_title"),
                           t("local.port_repair_failed", detail=f"{type(exc).__name__}: {exc}"))
        return False
    ctx.cluster_config_saved.emit(cluster)  # 服务器配置页、本地服务器页据此刷新
    return True


# mapping_owner 返回值 → 对应子页签名称（用户据此找到要关闭的那边）
_OWNER_LABEL_KEYS = {"sakura": "selfhost.tab_sakura", "lolia": "selfhost.tab_lolia", "selfhost": "selfhost.tab_selfhost"}


def ensure_no_other_mapping(parent, ctx, cluster, own: str, title: str) -> bool:
    """当前存档有世界已被另一种映射方式（非 own）接管时提示并返回 False，否则返回 True。"""
    owners = {s.name: ctx.mapping_owner(cluster, s) for s in cluster.shards}
    conflicting = {name: owner for name, owner in owners.items() if owner not in (None, own)}
    if not conflicting:
        return True
    methods = list(dict.fromkeys(t(_OWNER_LABEL_KEYS[o]) for o in conflicting.values()))
    dialogs.show_warning(parent, title, t("sakura.other_mapping_conflict_msg",
                                          shards="、".join(conflicting), methods="、".join(methods)))
    return False


def shard_mapping_name(ctx, cluster, shard) -> str:
    """这个世界正在用的映射方式名称，没有映射时为空字符串。"""
    owner = ctx.mapping_owner(cluster, shard)
    return t(_OWNER_LABEL_KEYS[owner]) if owner else ""


def mapping_method_names(ctx, cluster) -> list[str]:
    """这个存档各世界正在用的映射方式名称（去重，按世界顺序）。"""
    owners = (ctx.mapping_owner(cluster, s) for s in cluster.shards)
    return list(dict.fromkeys(t(_OWNER_LABEL_KEYS[o]) for o in owners if o))


def remote_failure_item(owner: str, shard_names, error) -> str:
    """一条"未能从账号上删除的隧道"说明，删除存档完成后汇总提示用户手动处理。"""
    return t("save.delete_mapping_failed_item", method=t(_OWNER_LABEL_KEYS[owner]),
             shards="、".join(shard_names), error=str(error))
