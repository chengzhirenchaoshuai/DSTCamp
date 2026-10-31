"""专服崩溃自动重启的判定规则（纯逻辑）。

坑：新令牌在异常断开后 Klei 端不会立即释放，马上重启会注册冲突（E_ROWID_EXIST）。2026-10-05 实测
强杀后约 25～30 多分钟才释放，期间专服每 5 秒左右自动重试，释放后自行注册成功（"Server registered
via geo DNS"）。因此：
- 开启"崩溃后换用空闲令牌"（默认，等待 0 分钟）时，原令牌仍在等待期且池中有空闲令牌，拉起前直接换用，
  马上上线；换下的令牌等待标记到期后自动恢复可用；
- 没有空闲令牌（或设定了等待分钟数、尚未到时）时用原令牌拉起，冲突时不停服，让专服自己重试；
- 超过 TOKEN_WAIT_LIMIT 不再管理；结果都写日志，便于按真实释放时长调整。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# 不会因为重启而自愈的失败：重启只会原样再失败一次
NON_RECOVERABLE_CATEGORIES = frozenset({"runtime", "permission", "port", "world_generation", "luajit_boot_crash"})

CRASH_RESTART_DELAY = 15.0        # 崩溃后稍等再拉起，让旧进程、frpc 与日志收尾
CRASH_WINDOW = 30 * 60            # 统计崩溃次数的时间窗
MAX_CRASH_RESTARTS = 3            # 时间窗内最多自动重启几次，防止 Mod 报错导致无限循环
TOKEN_HOLD_DURATION = 30 * 60     # 崩溃/冲突后令牌按"未释放"处理的时长（实测强杀后约 25 分钟释放）
TOKEN_WAIT_LIMIT = 2 * 60 * 60    # 从崩溃起最多等待 Klei 释放令牌的总时长
TOKEN_BUSY_RECHECK = 5 * 60       # 令牌正被别的存档使用、暂时拉不起时，多久再查一次


def is_restartable(category: str, world_ready: bool, auto_attempt: bool) -> bool:
    """这次失败是否触发自动重启：只重启"跑起来之后崩溃"的世界（从未就绪的多半是配置/Mod 问题）；
    自动拉起的那一轮在就绪前又失败计入崩溃次数（CrashBudget 限流）。令牌冲突由重试流程单独处理。"""
    if category == "token_conflict" or category in NON_RECOVERABLE_CATEGORIES:
        return False
    return world_ready or auto_attempt


@dataclass
class CrashBudget:
    """时间窗内的自动重启次数限制。"""

    times: list[float] = field(default_factory=list)

    def allow(self, now: float) -> bool:
        """记一次崩溃；超出次数上限返回 False（不再自动重启）。"""
        self.times = [moment for moment in self.times if now - moment < CRASH_WINDOW]
        if len(self.times) >= MAX_CRASH_RESTARTS:
            return False
        self.times.append(now)
        return True

    def reset(self) -> None:
        self.times.clear()


def append_log(log_path: Path, cluster_name: str, message: str) -> None:
    """每次自动重启的动作与结果追加到日志，用于统计 Klei 实际的令牌释放时长。"""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write(f"{stamp} [{cluster_name}] {message}\n")
    except OSError:
        pass  # 写日志失败不能影响重启本身
