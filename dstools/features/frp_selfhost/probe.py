"""只读探测自建服务器状态（状态面板展示、分配远程端口时避开已占用端口）。

只用初次鉴权推上去的密钥做一次性短连接，不接受密码、不保持会话；连不上或缺少 ss/free/nproc
等工具时不抛异常，结果标为"探测不到"。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from dstools.features.frp_selfhost.remote_deploy import (
    KNOWN_HOSTS_PATH, SSH_KEY_PATH, classify_permission, has_local_key,
)

# 所有只读检查放在一次 exec_command 里；UID 是 bash 只读内置变量，改用 MY_UID
_PROBE_SCRIPT = """
MY_UID="$(id -u)"
echo "UID:${MY_UID}"
if [ "${MY_UID}" != "0" ]; then
    if sudo -n true 2>/dev/null; then echo "SUDO:ok"; else echo "SUDO:no"; fi
fi
if systemctl is-active --quiet dstcamp-frps 2>/dev/null; then
    echo "SERVICE:active"
else
    echo "SERVICE:inactive"
fi
echo "CPU:$(nproc 2>/dev/null)"
free -m 2>/dev/null | awk '/^Mem:/{print "MEM:"$2","$3}'
if command -v ss >/dev/null 2>&1; then
    # 不按列号取本地地址（各版本 ss 是否输出 Netid 列不一致），只匹配"以冒号+数字结尾"的字段，
    # LISTEN/UNCONN 状态下对端固定为 ":*"，不会误匹配
    echo "PORTS:$(ss -Htlun 2>/dev/null | awk '{for(i=1;i<=NF;i++) if ($i ~ /:[0-9]+$/) print $i}' | awk -F: '{print $NF}' | sort -un | tr '\\n' ',')"
else
    echo "PORTS:"
fi
# dstcamp-frps 当前绑定的端口，用于判断"端口占用者是否就是 frps 自己"；未安装时为空
echo "FRPSPORT:$(grep -oP '^bindPort\\s*=\\s*\\K[0-9]+' /opt/dstcamp-frp/frps.toml 2>/dev/null)"
""".strip()


@dataclass
class ServerStatus:
    reachable: bool
    error: str | None = None
    permission: str | None = None  # "root" / "sudo_nopasswd" / "no_permission"
    service_active: bool | None = None
    cpu_count: int | None = None
    mem_total_mb: int | None = None
    mem_used_mb: int | None = None
    used_ports: frozenset = field(default_factory=frozenset)
    frps_bind_port: int | None = None  # dstcamp-frps 当前实际绑定的端口
    checked_at: float = 0.0

    def port_conflicts(self, port: int) -> bool:
        """端口被服务器上其他服务占用（占用者是 frps 自己时不算冲突）。"""
        return self.reachable and port in self.used_ports and port != self.frps_bind_port


def _parse_probe_output(output: str) -> ServerStatus:
    fields: dict[str, str] = {}
    for line in output.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key] = value

    permission = None
    if "UID" in fields:
        permission = classify_permission(fields["UID"], fields.get("SUDO") == "ok")

    cpu_count = int(fields["CPU"]) if fields.get("CPU", "").isdigit() else None

    mem_total_mb = mem_used_mb = None
    total_s, _, used_s = fields.get("MEM", "").partition(",")
    if total_s.isdigit() and used_s.isdigit():
        mem_total_mb, mem_used_mb = int(total_s), int(used_s)

    used_ports = frozenset(int(p) for p in fields.get("PORTS", "").split(",") if p.isdigit())
    frps_port_s = fields.get("FRPSPORT", "").strip()
    frps_bind_port = int(frps_port_s) if frps_port_s.isdigit() else None

    return ServerStatus(
        reachable=True,
        permission=permission,
        service_active=fields.get("SERVICE") == "active",
        cpu_count=cpu_count,
        mem_total_mb=mem_total_mb,
        mem_used_mb=mem_used_mb,
        used_ports=used_ports,
        frps_bind_port=frps_bind_port,
        checked_at=time.time(),
    )


def probe_server_status(host: str, port: int, username: str, connect_timeout: float = 8.0) -> ServerStatus:
    """同步阻塞，调用方要自己放到后台线程跑。"""
    if not has_local_key():
        return ServerStatus(reachable=False, error="尚未完成初次鉴权")

    import paramiko

    try:
        pkey = paramiko.Ed25519Key.from_private_key_file(str(SSH_KEY_PATH))
    except (paramiko.SSHException, OSError) as e:
        return ServerStatus(reachable=False, error=f"读取本地密钥失败: {e}")

    client = paramiko.SSHClient()
    if KNOWN_HOSTS_PATH.exists():
        client.load_host_keys(str(KNOWN_HOSTS_PATH))
    # 不走首次见到主机的确认流程：初次鉴权时已 TOFU，主机密钥不符直接视为探测失败
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        client.connect(hostname=host, port=port, username=username, pkey=pkey,
                        timeout=connect_timeout, banner_timeout=connect_timeout,
                        auth_timeout=connect_timeout)
        _stdin, stdout, _stderr = client.exec_command(_PROBE_SCRIPT, timeout=15)
        output = stdout.read().decode("utf-8", errors="replace")
        stdout.channel.recv_exit_status()
        return _parse_probe_output(output)
    except (paramiko.SSHException, OSError) as e:
        return ServerStatus(reachable=False, error=str(e))
    finally:
        client.close()


def read_frps_connection(host: str, port: int, username: str) -> tuple[int | None, str | None]:
    """只读服务器上 DSTCamp 部署的 frps.toml，返回 (端口, Token)；同步阻塞，失败抛 host_firewall.FirewallError。"""
    from dstools.features.frp_selfhost import deploy, host_firewall

    with host_firewall.ssh_session(host, port, username) as (client, sudo):
        _stdin, stdout, _stderr = client.exec_command(f"{sudo}cat {deploy.FRPS_TOML_PATH}", timeout=15)
        text = stdout.read().decode("utf-8", errors="replace")
    return deploy.parse_frps_toml(text)
