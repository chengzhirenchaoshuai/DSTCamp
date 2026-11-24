"""专服启动/运行期错误的离线日志诊断：只做保守的证据归类，区分"明确错误"与"疑似原因"，不修改任何文件。"""

from dataclasses import dataclass
import re
from typing import Iterable

from dstools.i18n import t


_WORKSHOP_RE = re.compile(r"workshop-\d+", re.IGNORECASE)
_TOKEN_CONFLICT_MARKER = "e_rowid_exist"
# Klei 不认这个令牌（被删除或重新生成过、内容残缺）或令牌文件为空：进程不退出，但服务器不会上线
_TOKEN_INVALID_MARKERS = (
    "e_invalid_token",
    "no auth token could be found",
)
_RUNTIME_LUA_ERROR_MARKERS = (
    "lua error",
    "stack traceback",
)
_SERVER_REGISTRATION_SUCCESS_MARKERS = (
    "server registered via geo dns",
)
_STARTUP_FAILURE_MARKERS = (
    "server failed to start!",
    "your server will not start",
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
# Lua 读不到 Mod 文件的两种报错（cannot open ...lua、module '...' not found）。
# 2026-11-14/15 两次是 LuaJIT 补丁当时版本的 bug，作者已修复；配套 Mod 强制更新，不再单独归给 LuaJIT
_MISSING_FILE_RE = re.compile(r"cannot open \S+?\.lua|module '[^']+' not found", re.IGNORECASE)
# DontStarveLuaJit2 发现当前虚拟机与配置的 LuaVmType 不同时打印这一行并 c_reset()。正常切换一次就好；
# 同一进程出现两次以上说明配置值无效：2026-11-16 modoverrides 里写成了字符串 "luavmtype.jit"，每次加载都
# 判定要切换，主世界约 10 秒重载一次（Received world rollback request: count=0），玩家进服即被断开
_LUAJIT_RESTART_RE = re.compile(r"\[luajit\]\s*need restart", re.IGNORECASE)
LUAJIT_RELOAD_LOOP_THRESHOLD = 2
# 运行库/依赖库类 Mod：替别的 Mod 加载文件、包装 require，别的 Mod 出错时堆栈中间常经过它。
# 只在根错误就出在它自己文件里时才列为嫌疑（11-15 两次误让人禁用模组运行库，作者不认可）
_LIBRARY_MODS = frozenset({
    "workshop-3750536829",  # 模组运行库
})
# 世界生成每轮校验必需 prefab，缺了打印 PANIC: missing required prefab 并重试（worldgen_main.lua 最多 5 次），
# 重试成功后读档时打印 World generated on build。重试本身是正常现象（11-21 一人 14 个包，缺的 prefab 每次都不同），
# 只有 5 次都失败打印 give up、之后没再读入世界才算生成失败
_WORLDGEN_GAVE_UP_MARKER = "an error occured during world and we give up"
_WORLD_LOADED_MARKER = "world generated on build"
# 读入世界之后 Lua 的错误处理函数自己又出错，进程随即以 0xC0000409 退出；日志不留 Mod 路径，只能按 Mod 二分。
# 11-21 实例是 45 格物品栏类 Mod。末尾的 Reset() returning 正常启动时也有，不能当证据
_ERROR_HANDLER_CRASH_MARKER = "error in error handling"
_STACK_BUFFER_OVERRUN = 0xC0000409
# 读档时存档文件本身坏了（本体 shardindex.lua OnLoadSaveDataFile 的断言原文）。11-24 用户强制重启后最新一份快照
# 大小正常、内容全是 NUL，读档报 Encoded Savedata is empty，进程以 0xC0000409 退出；此前被当成 Mod 冲突。
# 离线专服实测：删掉（移走）这一份即从上一份读档
_SAVE_CORRUPT_MARKERS = (
    "shardindex:getsavedata: encoded savedata is empty on load",
    "shardindex:getsavedata: encoded savedata is nil on load",
    "shardindex:getsavedata: savedata is nil on load",
    "shardindex:getsavedata: savedata is empty on load",
    "shardindex:getsavedata: load failed for file",
    "corrupt save file [",
)
# 不会让进程退出的刷屏行，不能当成兜底证据：域名解析失败只是网络提示（11-21 18:32 一份日志里有 5598 次，
# 曾被当成根因），世界生成重试是正常校验
_NOISE_MARKERS = (
    "could not resolve host",
    "panic: missing required prefab",
    "during world gen we will retry",
)


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


# Mod 加载出错时游戏打印的错误块（本体 mods.lua / modutil.lua 原文，10-30 真机日志核对过）：
#   Mod: <id> (<名称>)\t  Error loading mod!            modmain/modworldgenmain 编译失败，之后 Disabling
#   MOD ERROR: <id> (<名称>): Mod: <id> (<名称>)  Error loading mod!   运行出错（moderror）
#   Error loading mod: <id> (<名称>)!                   modinfo.lua 出错
# 错误正文从下一行起，没有时间戳前缀，以空行（或只有空白的行）结束；运行出错还跟着 LUA ERROR 堆栈。
# modinfo 这一种在启动时对目录里所有 Mod 都会读，未启用的 Mod 也会打印，只能提供原文，不能用来判定失败
_MOD_ERROR_LOADING_MARKER = "error loading mod!"
_MOD_ERROR_CONTEXT_RE = re.compile(r"\bMod:\s+([^\s(]+)", re.IGNORECASE)
_MODINFO_ERROR_RE = re.compile(r"Error loading mod:\s+([^\s(!]+)", re.IGNORECASE)
_LOG_TIMESTAMP_RE = re.compile(r"^\[\d{2}:\d{2}:\d{2}\]:")
_MOD_DISABLED_MARKER = "because it had an error"
MOD_ERROR_MAX_LINES = 30
# [string "../mods/workshop-1/modmain.lua"]:7: → modmain.lua:7:（游戏会把过长的路径截成 ...）
_LUA_CHUNK_RE = re.compile(r'\[string "\.\./mods/[^/"\\]+[/\\]([^"]*)"\]')
_MISSING_MOD_FILE_RE = re.compile(r"module '[^']+' not found|cannot open \S+", re.IGNORECASE)


class ModErrorCollector:
    """逐行接收专服日志，收集每个 Mod 第一次加载出错时的错误原文（mod_id -> 文本）。"""

    def __init__(self) -> None:
        self.errors: dict[str, str] = {}
        self._mod_id: str | None = None
        self._lines: list[str] = []

    def feed(self, line: str) -> None:
        if self._mod_id is not None:
            if self._block_ended(line):
                self._close()
            else:
                if len(self._lines) < MOD_ERROR_MAX_LINES:
                    self._lines.append(line.rstrip())
                return
        lower = line.lower()
        mod_id = None
        if _MOD_ERROR_LOADING_MARKER in lower:
            match = _MOD_ERROR_CONTEXT_RE.search(line)
            mod_id = match.group(1) if match else None
        elif "error loading mod:" in lower:
            match = _MODINFO_ERROR_RE.search(line)
            mod_id = match.group(1) if match else None
        if mod_id:
            self._mod_id, self._lines = mod_id, []

    def finish(self) -> None:
        """日志读完时收尾（错误块可能在最后一行之后才结束）。"""
        if self._mod_id is not None:
            self._close()

    @staticmethod
    def _block_ended(line: str) -> bool:
        return (not line.strip() or _LOG_TIMESTAMP_RE.match(line) is not None
                or _MOD_DISABLED_MARKER in line.lower())

    def _close(self) -> None:
        text = "\n".join(self._lines).strip()
        # 同一个错误游戏常会再打印一遍，只留第一次
        if text and self._mod_id not in self.errors:
            self.errors[self._mod_id] = text
        self._mod_id, self._lines = None, []


def summarize_mod_error(text: str) -> str:
    """错误原文的第一行，Lua 代码块名简化成 Mod 内的相对路径。"""
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return _LUA_CHUNK_RE.sub(r"\1", first)


def mod_error_is_missing_file(text: str) -> bool:
    """错误是读不到 Mod 自己的文件（module not found / cannot open），而不是代码写错。"""
    return _MISSING_MOD_FILE_RE.search(summarize_mod_error(text)) is not None


def _tips(category: str) -> tuple[str, ...]:
    """每类诊断固定两条建议，文案按 diag.<类别>.tip1/tip2 存放在 i18n 中。"""
    return (t(f"diag.{category}.tip1"), t(f"diag.{category}.tip2"))


def _evidence(lines: list[str], patterns: tuple[str, ...], limit: int = 3) -> tuple[str, ...]:
    matched = [line.strip() for line in lines if any(p in line.lower() for p in patterns)]
    return tuple(matched[-limit:])


def _last_index(lines: list[str], marker: str) -> int:
    """最后一行含 marker（小写）的下标，没有返回 -1。"""
    for index in range(len(lines) - 1, -1, -1):
        if marker in lines[index].lower():
            return index
    return -1


def _worldgen_gave_up(lines: list[str]) -> bool:
    """5 次世界生成全部失败，且之后没有再读入世界。"""
    gave_up = _last_index(lines, _WORLDGEN_GAVE_UP_MARKER)
    return gave_up >= 0 and _last_index(lines, _WORLD_LOADED_MARKER) < gave_up


def _error_handler_crash(lines: list[str], exit_code: int | None) -> bool:
    """读入世界后错误处理崩溃、以 0xC0000409 退出，且这之后没有可归因的 Lua 堆栈。"""
    if exit_code is None or exit_code & 0xFFFFFFFF != _STACK_BUFFER_OVERRUN:
        return False
    loaded = _last_index(lines, _WORLD_LOADED_MARKER)
    crashed = _last_index(lines, _ERROR_HANDLER_CRASH_MARKER)
    if loaded < 0 or crashed < loaded:
        return False
    # 读档后有正常的 Lua 堆栈时它更有价值，交给 Mod 冲突诊断
    return not any(marker in line.lower() for line in lines[loaded:]
                   for marker in _RUNTIME_LUA_ERROR_MARKERS)


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
                # 堆栈中间帧经过运行库不代表是它的问题
                stack_mods.update(ids - {m for m in ids if m.lower() in _LIBRARY_MODS})
        # Lua 堆栈前一行常用 [string "../mods/.../modmain.lua"] 写出根错误。
        if any(index < marker and marker - index <= 6 for marker in traceback_indexes):
            if "[string \"../mods/" in lower and re.search(r"../mods/workshop-\d+[/\\].*:\d+", lower):
                # 运行库替别人 require 时报的"找不到文件"也写在它自己的文件名下，同样不算它的
                if _MISSING_FILE_RE.search(line):
                    ids = {m for m in ids if m.lower() not in _LIBRARY_MODS}
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
    luajit_restarts: int = 0,
) -> DiagnosticReport | None:
    """根据一次世界启动或运行日志生成保守诊断，``None`` 表示无需提醒（正常停止不生成）。

    令牌注册冲突不会让进程退出，可能发生在世界就绪后，所以要在"进程仍在运行"的快速返回之前判断。
    ``luajit_injected`` 表示开着 LuaJIT（启动目录里有注入壳，离线日志按启用了配套 Mod 推断），
    用于识别注入器启动即崩。
    ``luajit_restarts`` 是控制台从进程启动起累计的 LuaJIT 重置次数：一轮重载有几千行，
    传进来的日志窗口装不下两轮，离线分析整份日志时则直接从日志里数。
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

    if any(marker in lower for marker in _TOKEN_INVALID_MARKERS):
        return DiagnosticReport(
            "token_invalid", t("diag.token_invalid.title"),
            t("diag.token_invalid.summary", shard=shard_name), _tips("token_invalid"),
            _evidence(lines, _TOKEN_INVALID_MARKERS + ("your server will not start",)), (),
        )

    # 读档断言同样带 LUA ERROR 堆栈，要在 Mod 冲突之前认出来
    if any(marker in lower for marker in _SAVE_CORRUPT_MARKERS):
        return DiagnosticReport(
            "save_corrupt", t("diag.save_corrupt.title"),
            t("diag.save_corrupt.summary", shard=shard_name), _tips("save_corrupt"),
            _evidence(lines, _SAVE_CORRUPT_MARKERS, limit=1), (),
        )

    # 进程不退出、世界也算就绪过，所以要在"仍在运行"的快速返回之前判断
    if max(luajit_restarts, count_luajit_restarts(lines)) >= LUAJIT_RELOAD_LOOP_THRESHOLD:
        return DiagnosticReport(
            "luajit_reload_loop", t("diag.luajit_reload_loop.title"),
            t("diag.luajit_reload_loop.summary", shard=shard_name), _tips("luajit_reload_loop"),
            _evidence(lines, ("target vm type", "need restart", "world rollback request")), (),
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

    if _worldgen_gave_up(lines):
        return DiagnosticReport(
            "worldgen_failed", t("diag.worldgen_failed.title"),
            t("diag.worldgen_failed.summary", shard=shard_name), _tips("worldgen_failed"),
            _evidence(lines, ("panic: missing required prefab", _WORLDGEN_GAVE_UP_MARKER)), related_mods,
        )

    if _error_handler_crash(lines, exit_code):
        return DiagnosticReport(
            "error_handler_crash", t("diag.error_handler_crash.title"),
            t("diag.error_handler_crash.summary", shard=shard_name), _tips("error_handler_crash"),
            _evidence(lines, (_WORLD_LOADED_MARKER, _ERROR_HANDLER_CRASH_MARKER)), (),
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
        _tail_evidence(lines), related_mods,
    )


def _tail_evidence(lines: list[str], limit: int = 3) -> tuple[str, ...]:
    """日志末尾几行作兜底证据，跳过域名解析失败、世界生成重试等不会让进程退出的刷屏行。"""
    result: list[str] = []
    for line in reversed(lines):
        text = line.strip()
        if text and not any(marker in text.lower() for marker in _NOISE_MARKERS):
            result.append(text)
            if len(result) >= limit:
                break
    return tuple(reversed(result))


def contains_startup_failure(lines: Iterable[str]) -> bool:
    """日志是否已明确进入启动失败状态（某些错误打印后进程仍存活，不能只靠 poll()）。"""
    text = "\n".join(str(line) for line in lines).lower()
    return any(marker in text for marker in _STARTUP_FAILURE_MARKERS)


def count_luajit_restarts(lines: Iterable[str]) -> int:
    """这批日志里 LuaJIT 因虚拟机类型不符而重置世界的次数。"""
    return sum(1 for line in lines if _LUAJIT_RESTART_RE.search(str(line)))


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
