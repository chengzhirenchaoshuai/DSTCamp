"""服务器配置保存前的范围校验与端口冲突检查（纯逻辑，存档列表、运行状态、映射接管等由参数传入）。"""

from typing import Callable

from dstools.features.cluster_config.config_manager import (
    load_cluster_config, load_shard_config, set_cluster_option, set_shard_option,
)
from dstools.features.cluster_config.form_logic import (
    CLUSTER_SECTIONS, REMOVED_CLUSTER_FIELDS, SHARD_PORT_OPTIONAL_FIELDS, SHARD_SLAVE_ONLY_FIELDS,
)
from dstools.features.cluster_config.ini_field_info import get_field_info, get_range_limits
from dstools.i18n import t
from dstools.models import Cluster, Platform, SaveSource, Shard
from dstools.shared.server_ports import (
    collect_cluster_port_claims, describe_conflict_owners, find_port_conflicts,
)

Values = dict[tuple[str, str], object]   # (分区, 键) -> 界面上当前的值（不含只读字段）


def validate_ranges(values: Values, *, shard: bool) -> str | None:
    """校验所有带范围约束的字段，返回第一条错误文案，全部合法返回 None（输入过滤之外仍需完整校验旧配置与空值）。"""
    for (section, key), raw in values.items():
        is_shard_section = section.startswith("SHARD_")
        if is_shard_section != shard:
            continue
        if not shard and section not in CLUSTER_SECTIONS:
            continue
        ini_section = section[len("SHARD_"):] if shard else section
        limits = get_range_limits(ini_section, key)
        if limits is None:
            continue
        if shard and (ini_section, key) in SHARD_PORT_OPTIONAL_FIELDS and str(raw).strip() == "":
            continue
        lo, hi = limits
        info = get_field_info(ini_section, key, is_shard=shard)
        field_name = info[0] if info else key
        try:
            number = int(raw)
        except (TypeError, ValueError):
            return t("cluster.range_error", field=field_name, min=lo, max=hi)
        if not lo <= number <= hi:
            return t("cluster.range_error", field=field_name, min=lo, max=hi)
    return None


def build_cluster_config(cluster: Cluster, values: Values):
    """按界面当前值改出要保存的 cluster.ini 配置（从磁盘重读一份再改，不带走过期状态）。"""
    config = load_cluster_config(cluster.path)
    for (section, key), raw in values.items():
        if section in CLUSTER_SECTIONS:
            set_cluster_option(config, section, key, raw)
    if cluster.source == SaveSource.SERVER and len(cluster.shards) > 1:
        # 世界互联在配置页只读、按多世界显示为开启，保存时与显示保持一致（开服前同样会打开）
        config.shard["shard_enabled"] = True
    # 这两项不渲染在表单里，上面循环碰不到；不主动清掉的话每次保存都会原样写回
    for section, key in REMOVED_CLUSTER_FIELDS:
        getattr(config, section.lower()).pop(key, None)
    return config


def build_shard_config(shard: Shard, values: Values):
    shard_config = load_shard_config(shard.path)
    for (section, key), raw in values.items():
        if section.startswith("SHARD_"):
            set_shard_option(shard_config, section.replace("SHARD_", ""), key, raw)
    # 切回主世界保存时清掉磁盘上残留的从世界 name/id，否则旧值会原样写回
    if shard_config.shard.get("is_master", True):
        for section, key in SHARD_SLAVE_ONLY_FIELDS:
            getattr(shard_config, section.lower()).pop(key, None)
    return shard_config


def format_port_issues(issues) -> str:
    return "\n".join(
        f"{issue.cluster_name}/{issue.shard_name or '-'} {issue.field}={issue.value!r}：{issue.message}"
        for issue in issues
    )


def cluster_ports_locked(cluster: Cluster, is_running: Callable[[Cluster], bool],
                         mapping_active: Callable[[Cluster, Shard], bool]) -> bool:
    """运行中或有端口映射时不能改端口。"""
    return is_running(cluster) or any(mapping_active(cluster, shard) for shard in cluster.shards)


def used_ports_for_lan_repair(cluster: Cluster, scan, env_clusters: list[Cluster]) -> set[int]:
    used = {port for ports in scan.ports_by_pid.values() for port in ports}
    own_claims, _ = collect_cluster_port_claims(cluster)
    used.update(claim.port for claim in own_claims if claim.field != "server_port")
    for other in env_clusters:
        if other.source != SaveSource.SERVER or str(other.path) == str(cluster.path):
            continue
        claims, _ = collect_cluster_port_claims(other)
        used.update(claim.port for claim in claims)
    return used


def server_ini_port_claims(cluster: Cluster, shard: Shard, shard_config):
    """当前 server.ini 实际控制的端口声明：主世界从 cluster.ini 继承的 master_port 不算本文件的配置。"""
    claims, _ = collect_cluster_port_claims(
        cluster, [shard.name], shard_config_overrides={shard.name: shard_config})
    if "master_port" not in shard_config.shard:
        claims = [claim for claim in claims if claim.field != "master_port"]
    return claims


def cluster_ini_port_claims(cluster: Cluster, cluster_config):
    """当前 cluster.ini 实际控制的 master_port 声明（server.ini 自己没写时才归 cluster.ini）。"""
    claims, _ = collect_cluster_port_claims(cluster, cluster_config_override=cluster_config)
    result = []
    for claim in claims:
        if claim.field != "master_port" or not claim.shard_name:
            continue
        shard = next((item for item in cluster.shards if item.name == claim.shard_name), None)
        if shard is None:
            continue
        if "master_port" not in load_shard_config(shard.path).shard:
            result.append(claim)
    return result


def first_target_port_conflict(target_claims, all_claims) -> str | None:
    target_keys = {claim.owner_key for claim in target_claims}
    conflicts = [c for c in find_port_conflicts(all_claims)
                 if any(claim.owner_key in target_keys for claim in c.claims)]
    if not conflicts:
        return None
    conflict = conflicts[0]
    owners = describe_conflict_owners(conflict.claims)
    return t("cluster.port_conflict_effective", value=conflict.port, owners=owners)


def cross_cluster_port_conflicts(cluster: Cluster, target_claims, env_clusters: list[Cluster]) -> list[str]:
    other_claims = []
    for other in env_clusters:
        if other.source != SaveSource.SERVER or other.platform != Platform.STEAM:
            continue
        if str(other.path) == str(cluster.path):
            continue
        claims, _ = collect_cluster_port_claims(other)
        other_claims.extend(claims)
    target_keys = {claim.owner_key for claim in target_claims}
    return [
        f"{conflict.port}: " + describe_conflict_owners(conflict.claims)
        for conflict in find_port_conflicts(target_claims + other_claims)
        if any(claim.owner_key in target_keys for claim in conflict.claims)
    ]


def find_cluster_port_conflict(cluster: Cluster, cluster_config) -> str | None:
    """cluster.ini 的 master_port 与本存档世界端口是否冲突。"""
    target = cluster_ini_port_claims(cluster, cluster_config)
    all_claims, _ = collect_cluster_port_claims(cluster, cluster_config_override=cluster_config)
    return first_target_port_conflict(target, all_claims)


def find_cross_cluster_cluster_port_conflicts(cluster: Cluster, cluster_config,
                                              env_clusters: list[Cluster]) -> list[str]:
    return cross_cluster_port_conflicts(cluster, cluster_ini_port_claims(cluster, cluster_config), env_clusters)


def find_port_conflict(cluster: Cluster, shard: Shard, shard_config) -> str | None:
    """同一存档内的有效端口冲突，包含默认值与跨字段冲突。"""
    target = server_ini_port_claims(cluster, shard, shard_config)
    runtime, _ = collect_cluster_port_claims(
        cluster, [shard.name], shard_config_overrides={shard.name: shard_config})
    siblings, _ = collect_cluster_port_claims(
        cluster, [item.name for item in cluster.shards if item.path != shard.path])
    return first_target_port_conflict(target, runtime + siblings)


def find_cross_cluster_port_conflicts(cluster: Cluster, shard: Shard, shard_config,
                                      env_clusters: list[Cluster]) -> list[str]:
    return cross_cluster_port_conflicts(
        cluster, server_ini_port_claims(cluster, shard, shard_config), env_clusters)
