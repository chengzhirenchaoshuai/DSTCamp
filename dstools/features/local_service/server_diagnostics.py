"""专服启动/运行期错误的离线日志诊断：只做保守的证据归类，区分"明确错误"与"疑似原因"，不修改任何文件。"""

from dataclasses import dataclass
import re
from typing import Iterable

from dstools.i18n import t


_WORKSHOP_RE = re.compile(r"workshop-\d+", re.IGNORECASE)
_TOKEN_CONFLICT_MARKER = "e_rowid_exist"
_RUNTIME_LUA_ERROR_MARKERS = (
    "lua error",
    "stack traceback",
)
_SERVER_REGISTRATION_SUCCESS_MARKERS = (
    "server registered via geo dns",
)
_STARTUP_FAILURE_MARKERS = (
    "server failed to start!",
    "unhandled exception during server startup:",
    "socket_port_already_in_use",
    "error loading worldgen_main.lua",
    "error loading main.lua",
    "failed msimulation->reset()",
    "error during game initialization!",
    "luaerror but no error string",
)
# 启动早期的最后一行；注入器在这之后、挂载数据包之前崩溃时日志就停在这里
_BOOT_STALL_MARKER = "curlrequestmanager::clientthread::main()"
_BOOT_PROGRESS_MARKER = "mounting file system"


@dataclass(frozen=True)
class DiagnosticReport:
    category: str
    title: str
    summary: str
    suggestions: tuple[str, ...]
    evidence: tuple[str, ...] = ()
    related_mods: tuple[str, ...] = ()

    @property
    def banner_text(self) -> str:
        """控制台顶部一行摘要；详情后续可由弹窗继续展示。"""
        return f"{self.title}：{self.summary}"


@dataclass(frozen=True)
class ModLoadStatus:
    """世界就绪后 Mod 的最终加载状态，供控制台横幅使用。"""

    failed_mods: tuple[str, ...] = ()
    visible_mod_count: int = 0


def analyze_mod_loading(
    *,
    enabled_mods: Iterable[str],
    loaded_mods: Iterable[str],
    failed_mods: Iterable[str] = (),
    visible_mod_count: int = 0,

) -> ModLoadStatus:
    """归纳世界就绪后的 Mod 加载结果（集合差集 + 服务器明确报告的禁用），控制台据此显示成功/失败横幅。"""
    missing = (set(enabled_mods) - set(loaded_mods)) | set(failed_mods)
    return ModLoadStatus(
        failed_mods=tuple(sorted(missing, key=str.lower)),
        visible_mod_count=visible_mod_count,
    )


def _tips(category: str) -> tuple[str, ...]:
    """每类诊断固定两条建议，文案按 diag.<类别>.tip1/tip2 存放在 i18n 中。"""
    return (t(f"diag.{category}.tip1"), t(f"diag.{category}.tip2"))


def _evidence(lines: list[str], patterns: tuple[str, ...], limit: int = 3) -> tuple[str, ...]:
    matched = [line.strip() for line in lines if any(p in line.lower() for p in patterns)]
    return tuple(matched[-limit:])


def _lua_evidence(lines: list[str], limit: int = 14) -> tuple[str, ...]:
    """截取第一段 Lua 堆栈，优先保留最接近根因的错误证据。"""
    markers = [i for i, line in enumerate(lines) if "lua error" in line.lower()
               or "stack traceback" in line.lower()]
    if not markers:
        return _evidence(lines, ("error loading", "../mods/", "attempt to", "wrong number"), limit)
    marker = markers[0]
    window = lines[max(0, marker - 4): marker + 45]
    result = []
    seen = set()
    for line in window:
        text = line.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if len(result) >= limit:
            break
    return tuple(result)


def _mods(lines: list[str], enabled: Iterable[str], loaded: Iterable[str]) -> tuple[str, ...]:
    def normalize(values):
        return {str(value).lower() for value in values if str(value).strip()}

    # 疑似 Mod 只从配置里已启用的集合中产生（日志搜索路径会列出大量未启用 Mod）；loaded 仅作没有预读配置时的兜底
    enabled_set = normalize(enabled)
    loaded_set = normalize(loaded)
    allowed = enabled_set or loaded_set
    stack_mods = set()
    traceback_indexes = [i for i, line in enumerate(lines)
                         if "lua error" in line.lower() or "stack traceback" in line.lower()]
    for index, line in enumerate(lines):
        ids = {m.group(0) for m in _WORKSHOP_RE.finditer(line)}
        # 只接受明确的错误归因行；模块搜索失败列表里的路径只是加载器
        # 的尝试顺序，不能把其中列出的所有 Mod 都误报成嫌疑对象。
        lower = line.lower()
        if "mod error:" in lower or "error calling" in lower and "mod workshop-" in lower:
            stack_mods.update(ids)
        if any(index >= marker and index <= marker + 80 for marker in traceback_indexes):
            if "../mods/" in lower and re.search(r"../mods/workshop-\d+[/\\].*:\d+", lower):
                stack_mods.update(ids)
        # Lua 堆栈前一行常用 [string "../mods/.../modmain.lua"] 写出根错误。
        if any(index < marker and marker - index <= 6 for marker in traceback_indexes):
            if "[string \"../mods/" in lower and re.search(r"../mods/workshop-\d+[/\\].*:\d+", lower):
                stack_mods.update(ids)
    # Lua 堆栈里出现的 Mod 路径是最有价值的归因证据；有堆栈证据时不把
    # 整个启用列表都显示成“疑似相关”，避免用户误以为每个 Mod 都有问题。
    if stack_mods:
        # 没有可读启用集合时（测试样例、少数旧日志）保留错误堆栈中明确出现的 ID
        return tuple(sorted(stack_mods & allowed if allowed else stack_mods,
                            key=str.lower))
    # 有 Lua 错误但没有明确 Mod 路径时，不显示整个启用列表，改由 UI 展示
    # 第一段错误堆栈，避免 Insight 等无关 Mod 被误认为冲突原因。
    return ()


def _lua_report(
    shard_name: str,
    world_ready: bool,
    lines: list[str],
    related_mods: tuple[str, ...],
) -> DiagnosticReport:
    phase = t("diag.phase_running" if world_ready else "diag.phase_startup")
    return DiagnosticReport(
        "mod_conflict", t("diag.mod_conflict.title"),
        t("diag.mod_conflict.summary", shard=shard_name, phase=phase),
        _tips("mod_conflict"),
        _lua_evidence(lines), related_mods,
    )


def diagnose_server_failure(
    *,
    shard_name: str,
    exit_code: int | None,
    world_ready: bool,
    log_lines: Iterable[str],
    enabled_mods: Iterable[str] = (),
    loaded_mods: Iterable[str] = (),
    intentional_stop: bool = False,
    ignore_token_conflict: bool = False,
    luajit_injected: bool = False,
) -> DiagnosticReport | None:
    """根据一次世界启动或运行日志生成保守诊断，``None`` 表示无需提醒（正常停止不生成）。

    令牌注册冲突不会让进程退出，可能发生在世界就绪后，所以要在"进程仍在运行"的快速返回之前判断。
    ``luajit_injected`` 表示启动目录里有 LuaJIT 注入壳，用于识别注入器启动即崩。
    """
    lines = [str(line) for line in log_lines]
    if intentional_stop:
        return None

    lower = "\n".join(lines).lower()
    related_mods = _mods(lines, enabled_mods, loaded_mods)

    # 已报过令牌冲突、之后进程又退出时由调用方传 ignore_token_conflict，按退出原因重新归类
    if not ignore_token_conflict and _TOKEN_CONFLICT_MARKER in lower and (
        "master server broadcast error" in lower or "http_500" in lower
    ):
        return DiagnosticReport(
            "token_conflict", t("diag.token_conflict.title"),
            t("diag.token_conflict.summary", shard=shard_name),
            _tips("token_conflict"),
            _evidence(lines, ("e_rowid_exist", "master server broadcast error")),
            (),
        )

    # 已就绪且仍存活的世界通常不属于“启动/退出诊断”，但 Lua 错误可能只
    # 让当前世界停止模拟或持续刷堆栈，并不保证操作系统进程退出。
    if exit_code in (None, 0) and world_ready:
        if any(marker in lower for marker in _RUNTIME_LUA_ERROR_MARKERS):
            return _lua_report(shard_name, world_ready, lines, related_mods)
        return None

    if any(token in lower for token in (
        "vcruntime140.dll", "msvcp140.dll", "vcomp120.dll", "cannot find the module",
        "找不到指定模块", "找不到 vcruntime", "找不到 vcomp",
    )):
        return DiagnosticReport(
            "runtime", t("diag.runtime.title"), t("diag.runtime.summary"), _tips("runtime"),
            _evidence(lines, ("dll", "找不到指定模块", "cannot find")), related_mods,
        )

    if any(token in lower for token in (
        "address already in use", "bind failed", "socket_port_already_in_use",
        "port_already_in_use", "端口已被占用", "only one usage",
    )):
        return DiagnosticReport(
            "port", t("diag.port.title"), t("diag.port.summary"), _tips("port"),
            _evidence(lines, ("address already", "bind", "端口")), related_mods,
        )

    if any(token in lower for token in (
        "access is denied", "permission denied", "拒绝访问", "permissionerror",
        "unable to write to config directory", "config_dir_write_permission",
        "check for write access: false", "check for read access: false",
    )):
        return DiagnosticReport(
            "permission", t("diag.permission.title"), t("diag.permission.summary"), _tips("permission"),
            _evidence(lines, ("access is denied", "permission", "拒绝访问")), related_mods,
        )

    if "must specify the task set for a level" in lower or "error loading worldgen_main.lua" in lower:
        return DiagnosticReport(
            "world_generation", t("diag.world_generation.title"),
            t("diag.world_generation.summary", shard=shard_name), _tips("world_generation"),
            _evidence(lines, ("task set", "worldgen_main.lua")), related_mods,
        )

    if any(marker in lower for marker in _RUNTIME_LUA_ERROR_MARKERS):
        return _lua_report(shard_name, world_ready, lines, related_mods)

    # 2026-10 用户实例：游戏更新后 LuaJIT 配套 Mod 残留文件不匹配，专服还没挂载数据包就退出，
    # 日志停在 CurlRequestManager 一行；Lua Mod 此时尚未加载，只可能是原生注入器
    if (luajit_injected and not world_ready and exit_code not in (None, 0)
            and _BOOT_STALL_MARKER in lower and _BOOT_PROGRESS_MARKER not in lower):
        return DiagnosticReport(
            "luajit_boot_crash", t("diag.luajit_boot_crash.title"),
            t("diag.luajit_boot_crash.summary", shard=shard_name), _tips("luajit_boot_crash"),
            _evidence(lines, (_BOOT_STALL_MARKER,), limit=1), (),
        )

    # 世界已就绪过说明是运行中退出（崩溃、被强制结束等），不能再叫"启动失败"
    return DiagnosticReport(
        "unknown", t("diag.unknown.title_crashed" if world_ready else "diag.unknown.title_startup"),
        t("diag.unknown.summary"), _tips("unknown"),
        tuple(line.strip() for line in lines[-3:] if line.strip()), related_mods,
    )


def contains_startup_failure(lines: Iterable[str]) -> bool:
    """日志是否已明确进入启动失败状态（某些错误打印后进程仍存活，不能只靠 poll()）。"""
    text = "\n".join(str(line) for line in lines).lower()
    return any(marker in text for marker in _STARTUP_FAILURE_MARKERS)


def contains_token_conflict(lines: Iterable[str]) -> bool:
    """判断本批日志是否出现不会主动结束进程的令牌注册冲突。"""
    text = "\n".join(str(line) for line in lines).lower()
    return _TOKEN_CONFLICT_MARKER in text


def contains_runtime_lua_error(lines: Iterable[str]) -> bool:
    """判断本批运行期日志是否出现 Lua 错误堆栈。"""
    text = "\n".join(str(line) for line in lines).lower()
    return any(marker in text for marker in _RUNTIME_LUA_ERROR_MARKERS)


def contains_server_registration_success(lines: Iterable[str]) -> bool:
    """判断 Master 是否已经明确完成 Klei 房间注册。"""
    text = "\n".join(str(line) for line in lines).lower()
    return any(marker in text for marker in _SERVER_REGISTRATION_SUCCESS_MARKERS)
