"""公网直连代码的自定义地址（IP 或域名）校验。

地址会原样拼进 c_connect("<地址>", 端口, "密码")。本体 c_connect 只是把它交给引擎 TheNet:StartClient，
社区与主机商教程（如 Nodecraft）都以域名写 c_connect，域名可用；游戏不支持 IPv6（与自动检测一致）。
端口取自存档 Master 的 server.ini，这里不收端口。
"""

import ipaddress
import re

_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def check_public_host(text: str) -> str | None:
    """检查自定义地址，合法返回 None，否则返回错误类别：
    scheme（带了 http:// 等前缀）、port（带了端口）、ipv6、invalid（其它不合法写法）。"""
    value = text.strip()
    if "://" in value:
        return "scheme"
    if not value or any(ch.isspace() or ch in "\"'\\/" for ch in value):
        return "invalid"
    if ":" in value:
        try:
            ipaddress.IPv6Address(value.strip("[]"))
            return "ipv6"
        except ValueError:
            return "port"
    try:
        ipaddress.IPv4Address(value)
        return None
    except ValueError:
        pass
    labels = value.rstrip(".").split(".")
    # 全是数字的点分写法是写错的 IPv4，不当域名放过
    if all(label.isdigit() for label in labels):
        return "invalid"
    if len(value) > 253 or len(labels) < 2 or not all(_LABEL_RE.fullmatch(label) for label in labels):
        return "invalid"
    return None
