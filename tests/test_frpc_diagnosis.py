"""frpc 输出诊断：退出码 1 时要能说出原因（实测共用节点 Token 被换、端口被占用排查了一个小时）。"""

from dstools.shared.frpc_diagnosis import diagnose
from _harness import run  # noqa: E402

# 取自真实日志（2026-10-30，自建节点被共用者重新部署后）
_TOKEN_FAIL = [
    "\x1b[1;34m2026-10-30 22:03:19.199 [I] [client/service.go:311] try to connect to server...",
    "\x1b[0m\x1b[1;33m2026-10-30 22:03:19.280 [W] [client/service.go:322] connect to server error: "
    "token in login doesn't match token from configuration",
    "\x1b[0mlogin to the server failed: token in login doesn't match token from configuration. "
    "With loginFailExit enabled, no additional retries will be attempted",
]
_LOGIN_OK = "2026-10-30 22:12:37.285 [I] [client/service.go:331] [caedd8674438e419] login to server success, get run id [caedd8674438e419]"


def test_token_mismatch_and_recovery() -> None:
    assert diagnose(_TOKEN_FAIL).kind == "token_mismatch"
    # 之后重连成功，旧的失败不再提示
    assert diagnose(_TOKEN_FAIL + [_LOGIN_OK]) is None


def test_port_conflict_while_running_names_the_proxy() -> None:
    lines = [_LOGIN_OK,
             "2026-10-30 22:12:37.307 [I] [client/control.go:165] [caedd8674438e419] [save-a0e01ff4-Caves] start proxy success",
             "2026-10-30 22:12:37.308 [W] [client/control.go:163] [caedd8674438e419] [save-a0e01ff4-Master] start error: port already used"]
    problem = diagnose(lines)
    assert problem.kind == "port_used" and problem.proxy == "save-a0e01ff4-Master"


if __name__ == "__main__":
    run(globals())
