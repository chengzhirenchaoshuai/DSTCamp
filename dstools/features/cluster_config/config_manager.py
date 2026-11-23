"""DST 服务器配置读写：cluster.ini 与 server.ini。"""

from pathlib import Path
from typing import Any

from dstools.features.cluster_config.ini_field_info import CLUSTER_FIELD_INFO, NO_TYPE_COERCE_FIELDS
from dstools.shared.ini_parser import (
    parse_cluster_ini,
    parse_server_ini,
    write_cluster_ini,
    write_server_ini,
)
from dstools.models import ClusterConfig, ShardConfig


# ── 集群配置（cluster.ini） ─────────────────────────────────────────────

# 游戏只在值被改过时才写入 cluster.ini，缺失不代表没有默认行为。默认值来自 Klei 官方
# 《Dedicated Server Settings Guide》并经真实存档核对（见 reference/带注释版本的cluster.ini），只收录确认过的值。
CLUSTER_INI_DEFAULTS: dict[tuple[str, str], Any] = {
    ("GAMEPLAY", "pvp"): False,
    ("GAMEPLAY", "pause_when_empty"): True,
    ("GAMEPLAY", "vote_enabled"): True,
    ("NETWORK", "cluster_name"): "[Host]'s World",
    ("NETWORK", "cluster_description"): "",
    ("NETWORK", "cluster_password"): "",
    ("NETWORK", "lan_only_cluster"): False,
    ("NETWORK", "offline_cluster"): False,
    ("NETWORK", "cluster_language"): "en",
    ("NETWORK", "whitelist_slots"): 0,
    ("NETWORK", "tick_rate"): 15,
    ("NETWORK", "autosaver_enabled"): True,
    ("NETWORK", "connection_timeout"): 8000,
    ("NETWORK", "idle_timeout"): 1800,
    # override_dns 没有内置默认值，放空字符串只为让这一行始终可见可编辑（留空即不生效）
    ("NETWORK", "override_dns"): "",
    ("MISC", "console_enabled"): True,
    ("MISC", "max_snapshots"): 6,
    ("SHARD", "shard_enabled"): False,
    # 这 4 项在 shard_enabled=true 时由游戏生成，被手动删掉后服务器会拒绝启动；
    # 补上官方默认值，保存一次即可修复
    ("SHARD", "bind_ip"): "127.0.0.1",
    ("SHARD", "master_ip"): "127.0.0.1",
    ("SHARD", "master_port"): 10888,
    ("SHARD", "cluster_key"): "defaultPass",
    ("STEAM", "steam_group_only"): False,
    ("STEAM", "steam_group_id"): "",  # 同 override_dns，没有默认值但要常驻显示才能填
    ("STEAM", "steam_group_admins"): False,
}

# game_mode/max_players/cluster_cloud_id 没有确认过的默认值，不放进 CLUSTER_INI_DEFAULTS，
# 但 backfill_cluster_defaults() 仍用空字符串占位，避免这些行从界面消失


def backfill_cluster_defaults(config: ClusterConfig) -> None:
    """给 CLUSTER_FIELD_INFO 登记的每个字段补值，保证删掉任一设置后该行不会从配置页消失。

    已有非空值原样保留；缺失或为空时用 CLUSTER_INI_DEFAULTS 的确认值补上，没有确认值的
    补空字符串（不编造数据）。保存后补全结果写入 cluster.ini。"""
    section_map = {
        "GAMEPLAY": config.gameplay, "NETWORK": config.network,
        "MISC": config.misc, "SHARD": config.shard, "STEAM": config.steam,
    }
    for section, key in CLUSTER_FIELD_INFO:
        current = section_map[section].get(key)
        if current is None or current == "":
            section_map[section][key] = CLUSTER_INI_DEFAULTS.get((section, key), "")


def load_cluster_config(path: Path) -> ClusterConfig:
    """读取 cluster.ini（传文件路径或存档目录均可）。"""
    if path.is_dir():
        path = path / "cluster.ini"
    if not path.exists():
        return ClusterConfig()
    return parse_cluster_ini(path)


def save_cluster_config(config: ClusterConfig, path: Path) -> None:
    """写回 cluster.ini（传文件路径或存档目录均可）。"""
    if path.is_dir():
        path = path / "cluster.ini"
    write_cluster_ini(config, path)


def set_cluster_option(config: ClusterConfig, section: str, key: str,
                       value: Any) -> None:
    """设置 cluster.ini 中的单个选项（值会转换成合适的类型）。"""
    section_map = {
        "GAMEPLAY": config.gameplay,
        "NETWORK": config.network,
        "MISC": config.misc,
        "SHARD": config.shard,
        "STEAM": config.steam,
    }

    section_lower = section.upper()
    if section_lower not in section_map:
        raise ValueError(f"Unknown cluster.ini section: {section}. "
                         f"Valid sections: GAMEPLAY, NETWORK, MISC, SHARD, STEAM")

    # 类型转换——密码这类字段即使值看起来像数字/布尔（比如密码就是
    # "0"），也必须原样存成字符串，不然真值判断会把密码"0"当成"没有密码"。
    if isinstance(value, str) and (section_lower, key) not in NO_TYPE_COERCE_FIELDS:
        if value.lower() == "true":
            value = True
        elif value.lower() == "false":
            value = False
        else:
            try:
                value = int(value)
            except ValueError:
                try:
                    value = float(value)
                except ValueError:
                    pass

    section_map[section_lower][key] = value


def get_cluster_option(config: ClusterConfig, section: str, key: str) -> Any:
    """读取 cluster.ini 中的单个选项，找不到返回 None。"""
    section_map = {
        "GAMEPLAY": config.gameplay,
        "NETWORK": config.network,
        "MISC": config.misc,
        "SHARD": config.shard,
        "STEAM": config.steam,
    }
    section_lower = section.upper()
    if section_lower not in section_map:
        return None
    return section_map[section_lower].get(key)


# ── 世界配置（server.ini） ───────────────────────────────────────────────

def load_shard_config(path: Path) -> ShardConfig:
    """读取 server.ini（传文件路径或世界目录均可）。"""
    if path.is_dir():
        path = path / "server.ini"
    if not path.exists():
        return ShardConfig()
    return parse_server_ini(path)


def save_shard_config(config: ShardConfig, path: Path) -> None:
    """写回 server.ini（传文件路径或世界目录均可）。"""
    if path.is_dir():
        path = path / "server.ini"
    write_server_ini(config, path)


class ServerIniDamagedError(ValueError):
    """server.ini 不存在或读不出任何配置，不能只补一个字段写回。"""

    def __init__(self, path: Path) -> None:
        from dstools.i18n import t
        super().__init__(t("cluster.server_ini_damaged", path=str(path)))
        self.path = path


def _is_blank(config: ShardConfig) -> bool:
    return not (config.network or config.shard or config.account or config.steam)


def set_server_port(shard_path: Path, port: int) -> bool:
    """只改 server.ini 的 server_port，其余内容原样保留；值没变不写，返回是否写了。

    文件不存在或读不出任何内容（空文件、断电留下的整块 NUL）时抛 ServerIniDamagedError：
    此时按"读出来的空配置 + 端口"写回，会得到只有一行端口、缺了 is_master 的文件，
    多世界存档里所有世界都被游戏当成没开分片，用户只能看到看不懂的端口冲突（11-23 zhanshen 实例）。"""
    path = shard_path / "server.ini"
    if not path.is_file():
        raise ServerIniDamagedError(path)
    config = parse_server_ini(path)
    if _is_blank(config):
        raise ServerIniDamagedError(path)
    if config.network.get("server_port") == port:
        return False
    config.network["server_port"] = port
    write_server_ini(config, path)
    return True


def find_server_ini_problems(cluster) -> list[tuple[str, str]]:
    """开服前检查各世界 server.ini 是否完整，返回 [(世界名, 问题)]，问题为 "damaged" 或 "no_is_master"。

    damaged：文件不存在或读不出任何内容。no_is_master：多世界且开了分片，却没写 is_master——
    游戏会关掉分片模式（日志 Missing 'is_master'），各世界互不相连，还都去监听 master_port。"""
    problems: list[tuple[str, str]] = []
    try:
        cluster_shard = load_cluster_config(Path(cluster.path)).shard
    except OSError:
        cluster_shard = {}
    multi_shard = len(cluster.shards) > 1 and bool(cluster_shard.get("shard_enabled", False))
    for shard in cluster.shards:
        path = Path(shard.path) / "server.ini"
        try:
            config = parse_server_ini(path) if path.is_file() else None
        except OSError:
            continue  # 一时读不了（被占用）不算损坏，交给后面的流程
        if config is None or _is_blank(config):
            problems.append((shard.name, "damaged"))
        elif multi_shard and "is_master" not in config.shard:
            problems.append((shard.name, "no_is_master"))
    return problems


def set_shard_option(config: ShardConfig, section: str, key: str,
                     value: Any) -> None:
    """设置 server.ini 中的单个选项。"""
    section_map = {
        "NETWORK": config.network,
        "SHARD": config.shard,
        "ACCOUNT": config.account,
        "STEAM": config.steam,
    }

    section_upper = section.upper()
    if section_upper not in section_map:
        raise ValueError(f"Unknown server.ini section: {section}. "
                         f"Valid sections: NETWORK, SHARD, ACCOUNT, STEAM")

    # 类型转换
    if isinstance(value, str):
        if value.lower() == "true":
            value = True
        elif value.lower() == "false":
            value = False
        else:
            try:
                value = int(value)
            except ValueError:
                pass

    section_map[section_upper][key] = value


def get_shard_option(config: ShardConfig, section: str, key: str) -> Any:
    """读取世界配置里的单个选项。"""
    section_map = {
        "NETWORK": config.network,
        "SHARD": config.shard,
        "ACCOUNT": config.account,
        "STEAM": config.steam,
    }
    section_upper = section.upper()
    if section_upper not in section_map:
        return None
    return section_map[section_upper].get(key)
