"""服务器系统防火墙（ufw/firewalld）对 frps 端口的放行检查与一键放行。

云安全组放通后，主机上的 ufw 默认拒绝入站仍会丢包；connectivity.TcpdumpProbe 的抓包点在 netfilter 之前，
被丢弃的包也抓得到，所以"抓到包"不等于放行（实测：腾讯云 Ubuntu 的 ufw 只放了 TCP 段，玩家的 UDP 全被丢，
直连显示"服务器无应答"）。只认 ufw 和 firewalld；直接写 iptables/nftables 规则的服务器判断不了，按放行处理。
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import dataclass, field

from dstools.features.frp_selfhost.remote_deploy import (
    KNOWN_HOSTS_PATH, SSH_KEY_PATH, check_remote_permission, has_local_key,
)

# 只认对任意来源放行的规则；"(v6)" 行与 IPv4 行成对出现，一并接受
_UFW_RULE_RE = re.compile(r"^(?P<ports>[\d,:]+)(?:/(?P<proto>tcp|udp))?(?:\s+\(v6\))?\s+ALLOW(?:\s+IN)?\s+Anywhere\b")
_UFW_DEFAULT_RE = re.compile(r"^Default:\s*(\w+)\s*\(incoming\)")


class FirewallError(Exception):
    pass


@dataclass
class FirewallCheck:
    tool: str | None = None  # "ufw" / "firewalld"；都没启用时为 None
    blocked: list[tuple[int, str]] = field(default_factory=list)  # [(端口, "udp"/"tcp")]


def parse_ufw_status(text: str) -> tuple[bool, list[tuple[int, int, str | None]]] | None:
    """解析 ``ufw status verbose``：未启用返回 None，否则返回 (入站默认是否拦截, [(起始端口, 结束端口, 协议或 None)])。"""
    lines = text.splitlines()
    if not any(line.strip() == "Status: active" for line in lines):
        return None
    deny_incoming = True  # 没有 Default 行（非 verbose 输出）时按拦截处理
    rules: list[tuple[int, int, str | None]] = []
    for line in lines:
        line = line.strip()
        if m := _UFW_DEFAULT_RE.match(line):
            deny_incoming = m.group(1) != "allow"
        elif m := _UFW_RULE_RE.match(line):
            for part in m.group("ports").split(","):
                low, _, high = part.partition(":")
                if low.isdigit():
                    rules.append((int(low), int(high) if high.isdigit() else int(low), m.group("proto")))
    return deny_incoming, rules


def ufw_allows(parsed: tuple[bool, list[tuple[int, int, str | None]]], port: int, proto: str) -> bool:
    deny_incoming, rules = parsed
    if not deny_incoming:
        return True
    return any(low <= port <= high and rule_proto in (None, proto) for low, high, rule_proto in rules)


@contextmanager
def ssh_session(ssh_host: str, ssh_port: int, ssh_username: str, connect_timeout: float = 8.0):
    """用初次鉴权的密钥登录，产出 (client, sudo 前缀)；改防火墙需要 root 或免密 sudo。"""
    import paramiko

    if not has_local_key():
        raise FirewallError("尚未完成初次鉴权")
    client = paramiko.SSHClient()
    if KNOWN_HOSTS_PATH.exists():
        client.load_host_keys(str(KNOWN_HOSTS_PATH))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        pkey = paramiko.Ed25519Key.from_private_key_file(str(SSH_KEY_PATH))
        client.connect(hostname=ssh_host, port=ssh_port, username=ssh_username, pkey=pkey,
                       timeout=connect_timeout, banner_timeout=connect_timeout, auth_timeout=connect_timeout)
        permission = check_remote_permission(client)
        if permission == "no_permission":
            raise FirewallError("账号没有 root 或免密 sudo 权限")
        yield client, ("" if permission == "root" else "sudo -n ")
    except (paramiko.SSHException, OSError) as e:
        raise FirewallError(str(e)) from e
    finally:
        client.close()


def _run(client, cmd: str) -> tuple[int, str]:
    _stdin, stdout, _stderr = client.exec_command(cmd, timeout=20)
    output = stdout.read().decode("utf-8", errors="replace")
    return stdout.channel.recv_exit_status(), output


def check(client, sudo: str, ports: list[tuple[int, str]]) -> FirewallCheck:
    _code, output = _run(client, f"command -v ufw >/dev/null 2>&1 && {sudo}ufw status verbose 2>/dev/null")
    parsed = parse_ufw_status(output)
    if parsed is not None:
        return FirewallCheck("ufw", [(p, proto) for p, proto in ports if not ufw_allows(parsed, p, proto)])
    _code, output = _run(client, f"command -v firewall-cmd >/dev/null 2>&1 && {sudo}firewall-cmd --state 2>/dev/null")
    if output.strip() == "running":
        blocked = [(p, proto) for p, proto in ports
                   if _run(client, f"{sudo}firewall-cmd --query-port={p}/{proto} >/dev/null 2>&1")[0] != 0]
        return FirewallCheck("firewalld", blocked)
    return FirewallCheck()


def open_ports(client, sudo: str, result: FirewallCheck) -> None:
    """只新增放行规则，不改动已有规则；失败抛 FirewallError。"""
    if result.tool == "ufw":
        cmds = [f"{sudo}ufw allow {p}/{proto} comment 'dstcamp-frp'" for p, proto in result.blocked]
    elif result.tool == "firewalld":
        cmds = [f"{sudo}firewall-cmd --permanent --add-port={p}/{proto}" for p, proto in result.blocked]
        cmds.append(f"{sudo}firewall-cmd --reload")
    else:
        return
    for cmd in cmds:
        code, output = _run(client, f"{cmd} 2>&1")
        if code != 0:
            raise FirewallError(output.strip()[:200] or f"exit {code}")
