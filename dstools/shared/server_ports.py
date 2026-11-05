"""计算存档各分片最终使用的本机端口并做冲突检测（世界创建、服务器配置、开服和内网穿透共用）。"""

from __future__ import annotations

import csv
import copy
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from dstools.models import Cluster, ClusterConfig, Shard, ShardConfig
from dstools.shared.ini_parser import (
    parse_cluster_ini, parse_server_ini, write_cluster_ini, write_server_ini,
)
from dstools.shared.resource_paths import data_dir


DEFAULT_MASTER_PORT = 10888
DEFAULT_SERVER_PORT = 10999
DEFAULT_STEAM_MASTER_PORT = 27016
DEFAULT_STEAM_AUTH_PORT = 8766
LAN_SERVER_PORT_MIN = 10998
LAN_SERVER_PORT_MAX = 11018
LAN_SERVER_PORT_FALLBACK = 10999


@dataclass(frozen=True)
class PortClaim:
    """一个进程准备占用的本机 UDP 端口。

    ``binding=False`` 表示只是配置项、游戏运行时并不真正绑定（Steam 的 master_server_port /
    authentication_port）：冲突检测跳过，但分配新端口时仍会避开。
    """

    cluster_path: str
    cluster_name: str
    shard_name: str | None
    field: str
    port: int
    source: str = "explicit"
    pid: int | None = None
    binding: bool = True

    @property
    def owner_key(self) -> tuple[str, str | None, str, int | None]:
        return (self.cluster_path, self.shard_name, self.field, self.pid)

    def display_owner(self) -> str:
        if self.cluster_path == "<system>":
            return f"系统进程 PID {self.pid}" if self.pid is not None else "系统中的其他进程"
        # master_port 是 cluster 级别的端口（只由主世界监听），不归属于某个
        # 分片，展示时无需带上世界名。
        shard = f"/{self.shard_name}" if self.shard_name and self.field != "master_port" else ""
        return f"{self.cluster_name}{shard} ({self.field})"


@dataclass(frozen=True)
class PortIssue:
    cluster_path: str
    cluster_name: str
    shard_name: str | None
    field: str
    value: object
    message: str
    code: str = ""


@dataclass(frozen=True)
class PortConflict:
    port: int
    claims: tuple[PortClaim, ...]


@dataclass(frozen=True)
class UdpPortScan:
    ok: bool
    ports_by_pid: dict[int, frozenset[int]]
    error: str = ""


def _cluster_config(cluster: Cluster) -> ClusterConfig:
    path = cluster.path / "cluster.ini"
    if path.exists():
        return parse_cluster_ini(path)
    return cluster.config or ClusterConfig()


def _shard_config(shard: Shard) -> ShardConfig:
    path = shard.path / "server.ini"
    if path.exists():
        return parse_server_ini(path)
    return shard.config or ShardConfig()


def _effective_port(raw: object, default: int, *, cluster: Cluster,
                    shard: Shard | None, field: str,
                    issues: list[PortIssue]) -> tuple[int | None, str]:
    if raw in (None, ""):
        return default, "default"
    try:
        port = int(str(raw).strip())
    except (TypeError, ValueError):
        issues.append(PortIssue(
            str(cluster.path), cluster.name, shard.name if shard else None,
            field, raw, "端口不是有效整数",
        ))
        return None, "invalid"
    if not 1 <= port <= 65535:
        issues.append(PortIssue(
            str(cluster.path), cluster.name, shard.name if shard else None,
            field, raw, "端口必须在 1..65535 之间",
        ))
        return None, "invalid"
    return port, "explicit"


def _effective_server_port(raw: object, *, cluster: Cluster, shard: Shard,
                           lan_restricted: bool, restricted_mode: str,
                           issues: list[PortIssue]) -> tuple[int | None, str]:
    """解析游戏端口，并复现 DST 在 LAN/离线模式下的实际回退规则。"""
    port, source = _effective_port(
        raw, DEFAULT_SERVER_PORT, cluster=cluster, shard=shard,
        field="server_port", issues=issues,
    )
    if port is None or not lan_restricted:
        return port, source
    if LAN_SERVER_PORT_MIN <= port <= LAN_SERVER_PORT_MAX:
        return port, source
    issues.append(PortIssue(
        str(cluster.path), cluster.name, shard.name, "server_port", raw,
        f"{restricted_mode}要求端口在 {LAN_SERVER_PORT_MIN}..{LAN_SERVER_PORT_MAX} "
        f"之间；游戏会把当前值回退为 {LAN_SERVER_PORT_FALLBACK}",
        "lan_server_port_range",
    ))
    return LAN_SERVER_PORT_FALLBACK, "lan_fallback"


def collect_cluster_port_claims(
        cluster: Cluster, shard_names: Iterable[str] | None = None, *,
        cluster_config_override: ClusterConfig | None = None,
        shard_config_overrides: dict[str, ShardConfig] | None = None,
) -> tuple[list[PortClaim], list[PortIssue]]:
    """解析存档指定分片实际占用的全部 UDP 端口（``shard_names=None`` 为全部分片）。

    master_port 只有主世界监听；server.ini 中的 SHARD 字段优先于 cluster.ini（游戏的分片覆盖规则）。
    """
    selected = set(shard_names) if shard_names is not None else None
    cluster_config = cluster_config_override or _cluster_config(cluster)
    lan_only = bool(cluster_config.network.get("lan_only_cluster", False))
    offline = bool(cluster_config.network.get("offline_cluster", False))
    lan_restricted = lan_only or offline
    restricted_mode = "仅限局域网模式" if lan_only else "离线模式"
    config_overrides = shard_config_overrides or {}
    claims: list[PortClaim] = []
    issues: list[PortIssue] = []

    for shard in cluster.shards:
        if selected is not None and shard.name not in selected:
            continue
        config = config_overrides.get(shard.name) or _shard_config(shard)
        is_master = bool(config.shard.get("is_master", True))

        # Steam 内部端口运行时不真正绑定（见 ini_field_info.py），标 binding=False：分配时避开、冲突检测跳过，
        # 否则岛屿冒险等多世界存档都取默认 27016/8766 会被误判冲突
        server_port, server_source = _effective_server_port(
            config.network.get("server_port"), cluster=cluster, shard=shard,
            lan_restricted=lan_restricted,
            restricted_mode=restricted_mode,
            issues=issues,
        )
        if server_port is not None:
            claims.append(PortClaim(
                str(cluster.path), cluster.name, shard.name, "server_port",
                server_port, server_source,
            ))

        fields = (
            ("master_server_port", config.steam.get("master_server_port"), DEFAULT_STEAM_MASTER_PORT, False),
            ("authentication_port", config.steam.get("authentication_port"), DEFAULT_STEAM_AUTH_PORT, False),
        )
        for field, raw, default, binding in fields:
            port, source = _effective_port(
                raw, default, cluster=cluster, shard=shard, field=field, issues=issues,
            )
            if port is not None:
                claims.append(PortClaim(
                    str(cluster.path), cluster.name, shard.name, field, port, source,
                    binding=binding,
                ))

        if is_master:
            raw_master_port = config.shard.get(
                "master_port", cluster_config.shard.get("master_port")
            )
            port, source = _effective_port(
                raw_master_port, DEFAULT_MASTER_PORT, cluster=cluster,
                shard=shard, field="master_port", issues=issues,
            )
            # 实测：shard_enabled=false（单世界）时主世界不启动分片服务，不绑定 master_port；
            # 标 binding=False，冲突检测跳过，分配时仍避开（以后改成多世界也不会撞）
            shard_enabled = bool(config.shard.get(
                "shard_enabled", cluster_config.shard.get("shard_enabled", False)
            ))
            if port is not None:
                claims.append(PortClaim(
                    str(cluster.path), cluster.name, shard.name,
                    "master_port", port, source, binding=shard_enabled,
                ))

    if selected is not None:
        known = {s.name for s in cluster.shards}
        for missing in sorted(selected - known):
            issues.append(PortIssue(
                str(cluster.path), cluster.name, missing, "shard", missing,
                "找不到目标世界",
            ))
    return claims, issues


def find_port_conflicts(claims: Iterable[PortClaim]) -> list[PortConflict]:
    """按本机 UDP 端口做冲突检测（同一 claim 去重，跳过 binding=False 的端口）。"""
    by_port: dict[int, dict[tuple[str, str | None, str, int | None], PortClaim]] = {}
    for claim in claims:
        if not claim.binding:
            continue
        by_port.setdefault(claim.port, {})[claim.owner_key] = claim
    return [
        PortConflict(port, tuple(owners.values()))
        for port, owners in sorted(by_port.items())
        if len(owners) > 1
    ]


def scan_udp_ports() -> UdpPortScan:
    """读取 Windows 当前 UDP 监听端口；失败与“没有监听端口”明确区分。"""
    if sys.platform != "win32":
        return UdpPortScan(False, {}, "当前只实现了 Windows UDP 端口扫描")
    try:
        completed = subprocess.run(
            ["netstat", "-ano", "-p", "UDP"], capture_output=True,
            # netstat 按系统代码页输出（中文系统为 GBK）；不指定时 UTF-8 模式下解码失败、stdout 变成 None
            text=True, encoding="mbcs", errors="replace", timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return UdpPortScan(False, {}, f"{type(exc).__name__}: {exc}")
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"exit code {completed.returncode}"
        return UdpPortScan(False, {}, detail)

    result: dict[int, set[int]] = {}
    for row in csv.reader(completed.stdout.splitlines(), delimiter=" ", skipinitialspace=True):
        parts = [part for part in row if part]
        if len(parts) < 3 or parts[0] != "UDP":
            continue
        try:
            pid = int(parts[-1])
            port = int(parts[1].rsplit(":", 1)[-1])
        except ValueError:
            continue
        result.setdefault(pid, set()).add(port)
    return UdpPortScan(True, {pid: frozenset(ports) for pid, ports in result.items()})


def system_port_claims(scan: UdpPortScan, *, exclude_pids: Iterable[int] = (),
                       exclude_bindings: Iterable[tuple[int, int]] = ()) -> list[PortClaim]:
    excluded = set(exclude_pids)
    excluded_pairs = set(exclude_bindings)
    return [
        PortClaim("<system>", "系统进程", None, "udp", port, "runtime", pid)
        for pid, ports in scan.ports_by_pid.items() if pid not in excluded
        for port in sorted(ports) if (pid, port) not in excluded_pairs
    ]


def next_free_port(start: int, used: Iterable[int]) -> int:
    """从 start 起返回第一个未使用的合法端口。"""
    occupied = set(used)
    for port in range(max(1, int(start)), 65536):
        if port not in occupied:
            return port
    raise ValueError("没有可用端口")


def allocate_cluster_port_values(shard_names: Iterable[str], used: Iterable[int]) -> tuple[int, dict[str, dict[str, int]]]:
    """为一个新存档分配一整组互不重复的常规端口，不写任何配置文件。"""
    occupied = set(used)

    def take(start: int) -> int:
        port = next_free_port(start, occupied)
        occupied.add(port)
        return port

    master_port = take(DEFAULT_MASTER_PORT)
    result: dict[str, dict[str, int]] = {}
    ordered = sorted(set(shard_names), key=lambda name: name != "Master")
    for index, shard_name in enumerate(ordered):
        server_start = 10999 if index == 0 else 10998 + index - 1
        result[shard_name] = {
            "server_port": take(server_start),
            "master_server_port": take(DEFAULT_STEAM_MASTER_PORT + index),
            "authentication_port": take(DEFAULT_STEAM_AUTH_PORT + index),
        }
    return master_port, result


def allocate_lan_server_ports(
        shard_names: Iterable[str], used: Iterable[int],
) -> dict[str, int]:
    """在 DST 的 LAN 专用范围内为各世界分配互不冲突的游戏端口。"""
    occupied = set(used)
    candidates = [
        LAN_SERVER_PORT_FALLBACK,
        LAN_SERVER_PORT_MIN,
        *range(LAN_SERVER_PORT_MIN + 2, LAN_SERVER_PORT_MAX + 1),
    ]
    available = [port for port in candidates if port not in occupied]
    ordered = sorted(set(shard_names), key=lambda name: name != "Master")
    if len(available) < len(ordered):
        raise ValueError(
            f"{LAN_SERVER_PORT_MIN}..{LAN_SERVER_PORT_MAX} 中没有足够的可用端口"
        )
    return dict(zip(ordered, available))


def _write_configs_atomic(
        cluster: Cluster, targets: list[tuple[Path, object, object]], *,
        create_backup: bool,
) -> None:
    """原子写入一组配置文件；失败时恢复每个文件的原始字节。"""
    originals = {path: path.read_bytes() if path.exists() else None for path, _, _ in targets}
    if create_backup:
        backup_root = (
            data_dir("port_backups", legacy_cache_name="port_backups")
            / stable_path_key(cluster.path)
            / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        )
        for target, content in originals.items():
            if content is None:
                continue
            relative = target.relative_to(cluster.path)
            backup_path = backup_root / relative
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            backup_path.write_bytes(content)
    prepared: list[tuple[Path, Path]] = []
    try:
        for target, config, writer in targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, raw_temp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
            os.close(fd)
            temp_path = Path(raw_temp)
            prepared.append((temp_path, target))
            writer(config, temp_path)
        for temp_path, target in prepared:
            os.replace(temp_path, target)
    except Exception:
        for target, content in originals.items():
            if content is None:
                target.unlink(missing_ok=True)
                continue
            fd, raw_restore = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".restore", dir=target.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content)
                os.replace(raw_restore, target)
            finally:
                Path(raw_restore).unlink(missing_ok=True)
        raise
    finally:
        for temp_path, _ in prepared:
            temp_path.unlink(missing_ok=True)


def rewrite_lan_server_ports_atomic(
        cluster: Cluster, used: Iterable[int], *,
        cluster_config_override: ClusterConfig | None = None,
) -> dict[str, int]:
    """只调整各世界的 server_port，不生成持久备份。"""
    shard_configs = {
        shard.name: copy.deepcopy(_shard_config(shard)) for shard in cluster.shards
    }
    values = allocate_lan_server_ports(shard_configs, used)
    for shard_name, port in values.items():
        shard_configs[shard_name].network["server_port"] = port

    targets: list[tuple[Path, object, object]] = []
    if cluster_config_override is not None:
        targets.append((
            cluster.path / "cluster.ini", copy.deepcopy(cluster_config_override),
            write_cluster_ini,
        ))
    targets.extend(
        (shard.path / "server.ini", shard_configs[shard.name], write_server_ini)
        for shard in cluster.shards
    )
    # LAN 调整由用户当场确认，且只改 server_port；不在 port_backups 中
    # 额外保留副本。原子写入失败时的内存回滚仍由底层负责。
    _write_configs_atomic(cluster, targets, create_backup=False)
    return values


def format_lan_port_issues(issues: Iterable[PortIssue]) -> str:
    """把 LAN/离线端口越界整理成逐行说明，不暴露 repr 和内部回退细节。"""
    return "\n".join(
        f"{issue.cluster_name}/{issue.shard_name or '-'}：当前端口 {issue.value}"
        f"（需要 {LAN_SERVER_PORT_MIN}~{LAN_SERVER_PORT_MAX}）"
        for issue in issues
    )


def lan_restriction_names(
        cluster: Cluster, config: ClusterConfig | None = None,
) -> list[str]:
    """返回当前开启的 LAN 限制名称（仅限局域网/离线模式）；``config`` 为空时读磁盘，保存流程传入未落盘的表单值。"""
    network = (config or _cluster_config(cluster)).network
    names = []
    if network.get("lan_only_cluster", False):
        names.append("仅限局域网")
    if network.get("offline_cluster", False):
        names.append("离线模式")
    return names


def disable_lan_restrictions(cluster: Cluster) -> None:
    """关闭仅限局域网和离线模式并原子写回 cluster.ini（调用方负责确认且存档未运行）。"""
    config = copy.deepcopy(_cluster_config(cluster))
    config.network["lan_only_cluster"] = False
    config.network["offline_cluster"] = False
    _write_configs_atomic(
        cluster, [(cluster.path / "cluster.ini", config, write_cluster_ini)],
        create_backup=False,
    )


def rewrite_cluster_ports_atomic(cluster: Cluster, used: Iterable[int], *,
                                 create_backup: bool = True) -> tuple[int, dict[str, dict[str, int]]]:
    """把已停止存档的整组端口原子改成不冲突值（调用方负责确认，且没有进程或映射在运行）。

    多个 INI 没有跨文件事务：保留各原文件字节，任一替换失败就逐个恢复。
    """
    cluster_path = cluster.path / "cluster.ini"
    cluster_config = copy.deepcopy(_cluster_config(cluster))
    shard_configs = {shard.name: copy.deepcopy(_shard_config(shard)) for shard in cluster.shards}
    master_port, values = allocate_cluster_port_values(shard_configs, used)
    cluster_config.shard["master_port"] = master_port
    for shard_name, ports in values.items():
        config = shard_configs[shard_name]
        config.network["server_port"] = ports["server_port"]
        config.steam["master_server_port"] = ports["master_server_port"]
        config.steam["authentication_port"] = ports["authentication_port"]

    targets: list[tuple[Path, object, object]] = [
        (cluster_path, cluster_config, write_cluster_ini),
    ]
    targets.extend(
        (shard.path / "server.ini", shard_configs[shard.name], write_server_ini)
        for shard in cluster.shards
    )
    _write_configs_atomic(cluster, targets, create_backup=create_backup)
    return master_port, values


def stable_path_key(path: Path, *, length: int = 12) -> str:
    """给缓存/远端资源生成不泄露完整路径且不会只按目录名碰撞的键。"""
    import hashlib

    try:
        normalized = str(path.resolve()).casefold()
    except OSError:
        normalized = str(path.absolute()).casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:length]
