"""保存配置后的提示：存档有世界正在运行时说明要重启才生效，并可一键重启。

modoverrides.lua、leveldataoverride.lua、cluster.ini、管理员/黑名单和令牌都只在开服时读取，
运行中保存的内容会保留到下次开服（已实测关服不会重写它们）；server.ini 不同，关服时会被
专服写回，那里在保存前就拦下，不走这里。
"""

from dstools.i18n import t
from dstools.qt import dialogs


def running_hint(ctx, cluster) -> str:
    """存档有世界在运行时返回"重启后才会生效"的说明，否则返回空串。"""
    shards = ctx.running_shard_names(cluster) if cluster is not None else []
    return t("local.saved_running_hint", shards="、".join(shards)) if shards else ""


def show_saved(parent, ctx, cluster, title: str, text: str) -> None:
    """显示保存成功；有世界在运行时追加重启说明，给出「稍后重启」「立即重启」。

    回车和 Esc 都落在「稍后重启」，避免误触把在线玩家踢下线。"""
    hint = running_hint(ctx, cluster)
    if not hint:
        dialogs.show_info(parent, title, text)
        return
    choice = dialogs.ask_choice(
        parent, title, f"{text}\n\n{hint}",
        [(t("local.saved_restart_later_btn"), "cancel"), (t("local.saved_restart_now_btn"), "restart")],
        default="cancel")
    if choice == "restart":
        ctx.restart_running_shards(cluster)
