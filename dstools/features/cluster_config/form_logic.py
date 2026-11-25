"""服务器配置页的表单描述与数据装载（纯逻辑）：界面按 FieldSpec 画控件并把当前值交给 save_checks.py。"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from dstools.features.cluster_config.admin_manager import read_adminlist
from dstools.features.cluster_config.config_manager import (
    backfill_cluster_defaults, load_cluster_config, load_shard_config,
)
from dstools.features.cluster_config.ini_field_info import (
    ALWAYS_READONLY_FIELDS, get_enum_choices, get_field_info, get_range_limits,
)
from dstools.features.cluster_config.local_server_data import LOCAL_MAX_PLAYERS_LIMITS, local_form_values
from dstools.features.save_browser.reader import known_nicknames
from dstools.i18n import t
from dstools.models import Cluster, SaveSource, Shard
from dstools.shared import app_settings
from dstools.shared.token_manager import (
    extract_token_owner_id, is_valid_token, read_token, write_token,
)

# cluster.ini 四五个分区合并成一个页签，每个分区一个组内标题
SECTION_HEADER_KEYS = {
    "GAMEPLAY": "cluster.tab_gameplay", "NETWORK": "cluster.tab_network",
    "MISC": "cluster.tab_misc", "SHARD": "cluster.tab_shard", "STEAM": "cluster.tab_steam",
}
# 显示顺序覆盖：默认按 ini 里的物理书写顺序，这几节要求固定顺序，未列出的字段跟在后面
SECTION_FIELD_ORDER = {
    "SHARD": ["shard_enabled"],
    "NETWORK": [
        "cluster_name", "cluster_description", "cluster_password",
        "lan_only_cluster", "offline_cluster", "cluster_language", "tick_rate",
        "autosaver_enabled", "whitelist_slots", "connection_timeout", "idle_timeout",
        "override_dns", "cluster_cloud_id",
    ],
    "STEAM": ["steam_group_only", "steam_group_admins", "steam_group_id"],
}
# 官方已不再读取这两项：不显示/编辑，加载和保存时都从配置里清掉
REMOVED_CLUSTER_FIELDS = [("GAMEPLAY", "vote_kick_enabled"), ("NETWORK", "cluster_intention")]
CLUSTER_SECTIONS = ("GAMEPLAY", "NETWORK", "MISC", "SHARD", "STEAM")
# 只有从世界(is_master=false)才需要的字段
SHARD_SLAVE_ONLY_FIELDS = [("SHARD", "name"), ("SHARD", "id")]
# 任何世界都常驻显示的可选端口（留空不影响运行），不自动填值
SHARD_PORT_OPTIONAL_FIELDS = [("STEAM", "master_server_port"), ("STEAM", "authentication_port")]
# 可能填很长文字、但游戏并不支持真正换行符的字段（服务器描述）：多行展示，内容始终折成单行
WRAPPED_TEXT_FIELDS = {("NETWORK", "cluster_description")}
WRAPPED_TEXT_LINES = 3
# 三列分组：网络设置字段最多单独一列，玩法+杂项、多层世界+Steam 各配一列，高度接近
CLUSTER_COLUMNS = [
    [("NETWORK", "network")],
    [("GAMEPLAY", "gameplay"), ("MISC", "misc")],
    [("SHARD", "shard"), ("STEAM", "steam")],
]
SHARD_SECTIONS = ["NETWORK", "SHARD", "ACCOUNT", "STEAM"]


@dataclass
class FieldSpec:
    section: str           # cluster.ini 分区名；世界配置的分区带 "SHARD_" 前缀
    key: str
    label: str
    description: str
    kind: str              # bool / enum / int / text / wrapped / readonly
    value: object = None
    readonly: bool = False
    choices: list = field(default_factory=list)   # enum：[(原始值, 显示名)]
    limits: tuple | None = None                   # int：(最小, 最大)
    display_text: str = ""                        # readonly：显示的文字
    tooltip: str | None = None                    # 额外说明（比如端口被映射接管）

    @property
    def ini_section(self) -> str:
        return self.section[len("SHARD_"):] if self.section.startswith("SHARD_") else self.section

    @property
    def is_shard(self) -> bool:
        return self.section.startswith("SHARD_")


def make_field(section: str, key: str, value, *, readonly: bool = False, tooltip: str | None = None,
               limits: tuple | None = None) -> FieldSpec:
    is_shard = section.startswith("SHARD_")
    ini_section = section[len("SHARD_"):] if is_shard else section
    # 游戏自己生成、没有官方说明用途的字段（如 cluster_cloud_id）：一律只读
    if not is_shard and (ini_section, key) in ALWAYS_READONLY_FIELDS:
        readonly = True
    info = get_field_info(ini_section, key, is_shard=is_shard)
    label, desc = info if info else (key, "")
    # bool 值来自 ini 解析的类型转换结果（不是靠猜的），可以放心据此画成开关
    is_bool = isinstance(value, bool)
    choices = None if is_shard else get_enum_choices(ini_section, key)
    limits = limits or get_range_limits(ini_section, key)
    spec = FieldSpec(section, key, label, desc, "text", value, readonly, tooltip=tooltip)
    if readonly:
        spec.kind = "readonly"
        if is_bool:
            spec.display_text = t("cluster.bool_on") if value else t("cluster.bool_off")
        elif choices:
            spec.display_text = next((disp for raw, disp in choices if raw == value), str(value))
        else:
            spec.display_text = str(value) if value is not None else ""
        if not spec.display_text:
            # 空值显示说明文字，不留空白；只改显示，spec.value 仍为空（只读字段保存时不写回）。
            # 游戏自己生成的字段（如 cluster_cloud_id）在首次开服前没有值
            generated = not is_shard and (ini_section, key) in ALWAYS_READONLY_FIELDS
            spec.display_text = t("cluster.value_not_generated" if generated else "cluster.value_unset")
    elif is_bool:
        spec.kind = "bool"
    elif choices:
        spec.kind, spec.choices = "enum", list(choices)
    elif (ini_section, key) in WRAPPED_TEXT_FIELDS:
        spec.kind = "wrapped"
    elif limits is not None:
        spec.kind, spec.limits = "int", limits
    return spec


# ── cluster.ini ─────────────────────────────────────────────────────────

@dataclass
class ClusterGroup:
    section: str                       # GAMEPLAY 等
    title: str
    fields: list[FieldSpec] = field(default_factory=list)


def build_cluster_columns(cluster: Cluster, local_server: dict | None = None) -> list[list[ClusterGroup]]:
    """返回三列，每列若干分区组。本地存档的 cluster.ini 由游戏开服时按 shardindex 重写，改了留不住：
    传入 local_server（Master shardindex 的 server 表）时，LOCAL_SERVER_FIELDS 几项按它显示并可编辑，
    其余一律只读；不传则全部只读。"""
    is_server = cluster.source == SaveSource.SERVER
    config = load_cluster_config(cluster.path)
    for section, key in REMOVED_CLUSTER_FIELDS:
        getattr(config, section.lower()).pop(key, None)
    if is_server:
        # 游戏没写进文件不代表没有默认行为，只是这份存档还没存过；本地存档由客户端管理，不替它补
        backfill_cluster_defaults(config)
    local_values = {} if is_server or local_server is None else local_form_values(local_server)
    for (section, key), value in local_values.items():
        getattr(config, section.lower())[key] = value
    columns = []
    for column in CLUSTER_COLUMNS:
        groups = []
        for section, attr in column:
            data = getattr(config, attr)
            if not data:
                continue
            order = SECTION_FIELD_ORDER.get(section, [])
            keys = [k for k in order if k in data] + [k for k in data if k not in order]
            groups.append(ClusterGroup(section, t(SECTION_HEADER_KEYS[section]), [
                _cluster_field(section, key, data[key], is_server, (section, key) in local_values)
                for key in keys
            ]))
        columns.append(groups)
    return columns


def _cluster_field(section: str, key: str, value, is_server: bool, local_editable: bool) -> FieldSpec:
    if is_server:
        return make_field(section, key, value)
    if not local_editable:
        return make_field(section, key, value, readonly=True)
    # 游戏内建服人数上限比专服小
    limits = LOCAL_MAX_PLAYERS_LIMITS if (section, key) == ("GAMEPLAY", "max_players") else None
    return make_field(section, key, value, limits=limits)


# ── server.ini（每个世界一份）──────────────────────────────────────────

MappingOwner = Callable[[Cluster, Shard], "str | None"]   # 返回 None / "sakura" / "lolia" / "selfhost"


class ShardForm:
    """一个世界的 server.ini 编辑状态。字段值在界面上被改动但还没保存时，靠 snapshot() 带回来。"""

    def __init__(self, cluster: Cluster, shard: Shard):
        self.cluster, self.shard = cluster, shard
        self.is_server = cluster.source == SaveSource.SERVER
        self.config = load_shard_config(shard.path)
        if self.is_server:
            if not self.config.shard.get("is_master", True):
                self.backfill_slave_fields()
            else:
                # 从世界改回主世界时文件里可能残留 name/id，主世界不需要
                self.drop_slave_only_fields()
            # 这两个端口不分主从、常驻显示；文件里没有就放空字符串占位，不自动填值
            for section, key in SHARD_PORT_OPTIONAL_FIELDS:
                getattr(self.config, section.lower()).setdefault(key, "")

    def drop_slave_only_fields(self) -> None:
        for section, key in SHARD_SLAVE_ONLY_FIELDS:
            getattr(self.config, section.lower()).pop(key, None)

    def backfill_slave_fields(self) -> None:
        """给缺失的 name/id 生成默认值（只填缺的），保证和集群里其它世界不冲突。
        master_server_port/authentication_port 不在这里生成：从世界留空是正常状态。"""
        siblings = [load_shard_config(s.path) for s in self.cluster.shards if s.path != self.shard.path]

        def next_free(getter, start):
            used = set()
            for sibling in siblings:
                raw = getter(sibling)
                if str(raw).strip().lstrip("-").isdigit():
                    used.add(int(raw))
            n = start
            while n in used:
                n += 1
            return n

        if not str(self.config.shard.get("name", "")).strip():
            self.config.shard["name"] = self.shard.name
        if not str(self.config.shard.get("id", "")).strip():
            self.config.shard["id"] = next_free(lambda sc: sc.shard.get("id", ""), 2)

    def snapshot(self, values: dict) -> None:
        """把界面上当前（可能没保存）的值写回配置字典，重画时不丢用户正在编辑的其它字段。"""
        for (section, key), value in values.items():
            data = getattr(self.config, section[len("SHARD_"):].lower(), None)
            if data is not None:
                data[key] = value

    def set_is_master(self, is_master: bool) -> None:
        """"是否为主世界"开关被实时切换（还没保存）：现场增删从世界专属的 name/id。"""
        if not is_master:
            self.backfill_slave_fields()
        else:
            self.drop_slave_only_fields()

    def groups(self, mapping_owner: MappingOwner | None = None) -> list[tuple[str, list[FieldSpec]]]:
        result = []
        for sec in SHARD_SECTIONS:
            data = getattr(self.config, sec.lower(), {})
            if not data:
                continue
            fields = []
            for key, value in data.items():
                readonly = not self.is_server
                tooltip = None
                # server_port 一旦被"樱花映射/自建 frps"接管（远程端口回写进这里），就不能再手改
                if sec == "NETWORK" and key == "server_port" and self.is_server and mapping_owner:
                    owner = mapping_owner(self.cluster, self.shard)
                    if owner:
                        readonly = True
                        tooltip = t(f"cluster.server_port_{owner}_locked")
                fields.append(make_field(f"SHARD_{sec}", key, value, readonly=readonly, tooltip=tooltip))
            result.append((sec, fields))
        return result


# ── 管理员 / 黑名单 / 令牌 ───────────────────────────────────────────────

@dataclass
class IdList:
    labels: list[str]
    row_ids: list[str | None]      # 与 labels 一一对应的真实 ID；提示行/空状态行为 None


def load_id_list(cluster: Cluster, path_attr: str) -> IdList:
    """管理员/黑名单共用："每行一个 Klei ID"的纯文本文件，路径存在 Cluster 的不同属性上。"""
    path = getattr(cluster, path_attr)
    ids = read_adminlist(path) if path else []
    # 日志里能确认昵称的账号带上昵称方便辨认；写文件/删除只认 ID，所以显示文字和真实 ID 分开存
    nicknames = known_nicknames(cluster.shards) if cluster.source == SaveSource.SERVER else {}
    labels = [f"{a}（{nicknames[a]}）" if nicknames.get(a) else a for a in ids]
    row_ids: list[str | None] = list(ids)
    if path_attr == "adminlist_path" and cluster.source == SaveSource.SERVER:
        # 令牌所有者天然是管理员（引擎认定），不写进文件，只追加一行只读提示
        owner_id = extract_token_owner_id(read_token(cluster.token_path)) if cluster.token_path else None
        if owner_id and owner_id not in ids:
            labels.append(t("admin.token_owner_hint", id=owner_id))
            row_ids.append(None)
    if not labels:
        labels.append(t("blocklist.empty") if path_attr == "blocklist_path" else t("admin.empty"))
        row_ids.append(None)
    return IdList(labels, row_ids)


def is_valid_dst_user_id(value: str) -> bool:
    """只校验 DST 用户 ID 的前三位前缀，保留游戏实际 ID 的完整格式。"""
    return value.strip()[:3] in ("KU_", "OU_")


def load_cluster_token(cluster: Cluster) -> str:
    """读取令牌；服务器存档的令牌无效且全局令牌池非空时自动补上第一个（已有老存档也受益）。"""
    raw = read_token(cluster.token_path) if cluster.token_path else ""
    if not is_valid_token(raw) and cluster.source == SaveSource.SERVER:
        pool = app_settings.get_global_tokens()
        if pool:
            token_path: Path = cluster.token_path or (cluster.path / "cluster_token.txt")
            write_token(token_path, pool[0])
            cluster.token_path = token_path
            raw = pool[0]
    return raw
