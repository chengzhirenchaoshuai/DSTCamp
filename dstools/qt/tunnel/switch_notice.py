"""映射切换节点完成后的提示（樱花、Lolia 共用）：节点地址必变，旧直连代码随即失效，
给出新代码供复制、提醒发给朋友；端口变了且世界在运行时一并说明要重启。"""

from dstools.features.cluster_config.config_manager import get_cluster_option, load_cluster_config
from dstools.i18n import t
from dstools.qt import dialogs


def show_switch_done(parent, cluster, host: str, port, restart_shards: list[str]) -> None:
    password = get_cluster_option(load_cluster_config(cluster.path), "NETWORK", "cluster_password")
    code = f'c_connect("{host}", {port}, "{password}")' if password else f'c_connect("{host}", {port})'
    # 弹窗里不显示明文密码，复制出去的是完整代码（同本地服务器页的直连代码）
    shown = f'c_connect("{host}", {port}, "***")' if password else code
    text = t("dlg.switch_done_msg", code=shown)
    if restart_shards:
        text += "\n\n" + t("sakura.setup_done_msg_restart", shards="、".join(restart_shards))
    dialogs.show_info_with_copy(parent, t("dlg.switch_done_title"), text, t("sakura.copy_connect_btn"), code,
                                t("sakura.connect_copied"))
