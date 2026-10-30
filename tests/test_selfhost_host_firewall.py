"""自建 frps 服务器的 ufw 放行判断（实测漏放 UDP 时玩家直连无应答）。"""

from dstools.features.frp_selfhost.host_firewall import parse_ufw_status, ufw_allows
from _harness import run  # noqa: E402

# 取自真实服务器的 ufw status verbose（节选）
_UFW_TCP_ONLY = """Status: active
Logging: on (low)
Default: deny (incoming), allow (outgoing), deny (routed)
New profiles: skip

To                         Action      From
--                         ------      ----
2323/tcp                   ALLOW IN    Anywhere                   # SSH/SFTP
51820                      ALLOW IN    Anywhere
11000:11008/tcp            ALLOW IN    Anywhere
80,443/tcp                 ALLOW IN    Anywhere
3478/udp                   ALLOW IN    192.168.0.0/16
11000:11008/tcp (v6)       ALLOW IN    Anywhere (v6)
"""


def test_ufw_rules_match_by_port_range_and_protocol() -> None:
    parsed = parse_ufw_status(_UFW_TCP_ONLY)
    assert ufw_allows(parsed, 11005, "tcp")
    assert not ufw_allows(parsed, 11007, "udp")  # 只放了 TCP 段
    assert ufw_allows(parsed, 51820, "udp")  # 不写协议的规则两种都放
    assert ufw_allows(parsed, 443, "tcp")
    assert not ufw_allows(parsed, 3478, "udp")  # 限定来源的不算对玩家放行


def test_ufw_inactive_or_default_allow_never_blocks() -> None:
    assert parse_ufw_status("Status: inactive\n") is None
    parsed = parse_ufw_status("Status: active\nDefault: allow (incoming), allow (outgoing)\n")
    assert ufw_allows(parsed, 11007, "udp")


if __name__ == "__main__":
    run(globals())
