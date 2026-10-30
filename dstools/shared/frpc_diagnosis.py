"""从 frpc 输出诊断常见故障，供界面给出处理建议。

frpc 失败只有退出码 1，界面只显示"退出码 1"时用户无从下手（实测：共用的自建节点被别人重新部署换了 Token，
排查了一个小时）；映射端口被占用时 frpc 甚至不退出，只在输出里留一行 start error。
匹配的原文取自 frp v0.70.0 源码（pkg/auth/token.go、server/ports/ports.go、server/control.go、client/service.go）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_LOGIN_OK = re.compile(r"login to server success")
_RULES = (
    ("token_mismatch", re.compile(r"token in \w+ doesn't match token from configuration")),
    ("proxy_exists", re.compile(r"\[(?P<proxy>[^\]]+)\] start error: proxy \[[^\]]+\] already exists")),
    ("port_used", re.compile(r"\[(?P<proxy>[^\]]+)\] start error: port already used")),
    ("port_unavailable", re.compile(r"\[(?P<proxy>[^\]]+)\] start error: port unavailable")),
    ("port_not_allowed", re.compile(r"\[(?P<proxy>[^\]]+)\] start error: port not allowed")),
    ("server_unreachable", re.compile(
        r"connect to server error: .*(refused|i/o timeout|timed out|no such host|unreachable)", re.IGNORECASE)),
)


@dataclass(frozen=True)
class FrpcProblem:
    kind: str  # 见 _RULES 的第一列
    proxy: str = ""  # 端口类问题出错的代理名
    line: str = ""  # frpc 原文（去掉颜色码）


def diagnose(lines: Iterable[str]) -> FrpcProblem | None:
    """从最新一行往前找：先遇到登录成功说明之后（含重连后）没再出登录类问题，返回 None；
    映射启动失败的行在登录成功之后，所以会先被找到。"""
    for raw in reversed(list(lines)):
        line = _ANSI.sub("", raw).strip()
        if _LOGIN_OK.search(line):
            return None
        for kind, pattern in _RULES:
            if m := pattern.search(line):
                return FrpcProblem(kind, m.groupdict().get("proxy") or "", line)
    return None
