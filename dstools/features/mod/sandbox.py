"""在受限 Lua 5.1 沙箱子进程中执行 modinfo.lua，解析静态分析展开不了的动态配置。

不运行 modmain.lua；带硬超时。未定义的游戏全局、超时、执行错误或结果形状不符一律
返回 ``None``，调用方不得猜测缺失配置。
"""

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

_WORKER = Path(__file__).parent / "_sandbox_worker.py"

DEFAULT_TIMEOUT = 1.5

# 子进程是个控制台进程（普通 `python` 或重新执行打包后的 exe）——不加
# 这个标志，每次调用都会在 GUI 上方一闪而过一个黑色控制台窗口。
_CREATIONFLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

_BLOCK_OPENERS = re.compile(r"\b(?:if|for|while|function)\b")
_BLOCK_CLOSERS = re.compile(r"\bend\b")
_LONG_BRACKET_OPEN = re.compile(r"\[(=*)\[")


def _blank_strings(text: str) -> str:
    """返回等长副本，把引号/长括号字符串的内容替换成空格（保留引号与换行，下标仍对齐原文）。

    关键字和括号计数前先过一遍：描述文本里的英文单词（如 "for"）会被误当成 Lua 关键字。
    """
    out = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in ('"', "'"):
            quote = ch
            out.append(ch)
            i += 1
            while i < n:
                c = text[i]
                if c == "\\" and i + 1 < n:
                    out.append("  ")
                    i += 2
                    continue
                if c == quote:
                    out.append(c)
                    i += 1
                    break
                out.append(c if c == "\n" else " ")
                i += 1
            continue
        if ch == "[":
            lm = _LONG_BRACKET_OPEN.match(text, i)
            if lm:
                closer = "]" + lm.group(1) + "]"
                close_idx = text.find(closer, lm.end())
                if close_idx == -1:
                    inner_end, end, closer_text = n, n, ""
                else:
                    inner_end, end, closer_text = (
                        close_idx,
                        close_idx + len(closer),
                        closer,
                    )
                out.append(text[i : lm.end()])  # 保留开括号本身，例如 "[["
                out.extend(c if c == "\n" else " " for c in text[lm.end() : inner_end])
                out.append(closer_text)
                i = end
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _looks_balanced(text: str) -> bool:
    """粗略判断代码块是否配平（if/for/while/function 都有 end，花括号闭合，忽略字符串）。

    dynamic_preamble 可能切在未闭合的块中间（如整段 configuration_options 包在
    ``if locale == "zh" then`` 里），先拦一道省掉一次必然失败的子进程；误判两个方向都无害。
    """
    text = _blank_strings(text)
    if text.count("{") != text.count("}"):
        return False
    return len(_BLOCK_OPENERS.findall(text)) == len(_BLOCK_CLOSERS.findall(text))


def _largest_balanced_prefix(text: str) -> str:
    """截断到所有块都已闭合的最大前缀。

    preamble 尾部可能是未闭合的条件块，而动态选项依赖的局部辅助函数/表通常声明在它之前，
    退回最大闭合前缀即可保留这些依赖。判断粗略，切错最多是沙箱照旧失败，不会给出错误答案。
    """
    # 关键字在挖空副本上匹配，下标与原文一致，可直接用于切片
    blanked = _blank_strings(text)
    block_depth = 0
    brace_depth = 0
    last_safe = 0
    for m in re.finditer(r"\b(?:if|for|while|function|end)\b|[{}]", blanked):
        tok = m.group(0)
        if tok == "end":
            block_depth -= 1
        elif tok in ("if", "for", "while", "function"):
            block_depth += 1
        elif tok == "{":
            brace_depth += 1
        elif tok == "}":
            brace_depth -= 1
        if block_depth == 0 and brace_depth == 0:
            last_safe = m.end()
    return text[:last_safe]


def _worker_command() -> list:
    """沙箱子进程的启动命令。

    源码运行时是 ``python _sandbox_worker.py``；PyInstaller onefile 下 sys.executable 就是
    DSTCamp.exe，改为带特殊参数重启自身，由 run_gui.py 分发到 worker。子进程可随时被杀掉，
    这是沙箱超时依赖的前提。
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "--lua-sandbox-worker"]
    return [sys.executable, str(_WORKER)]


def run_lua_snippet(lua_code: str, timeout: float = DEFAULT_TIMEOUT) -> Any:
    """在沙箱子进程中执行 ``lua_code``（须以 ``return <expr>`` 结尾），返回解码后的 Python 值；
    失败、超时或结果无法 JSON 化时返回 None，不抛异常。
    """
    import json

    from dstools.shared.resource_paths import bundled_runtime_damaged

    # 解压目录被清理后 Worker 必定起不来，还会弹启动器错误框；直接当作解析失败
    if bundled_runtime_damaged():
        return None
    try:
        proc = subprocess.run(
            _worker_command(),
            input=lua_code,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            creationflags=_CREATIONFLAGS,
        )
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return None


FULL_FILE_TIMEOUT = 3.0

# 版本读取协议：追加在末尾的 return 只有整份文件无错执行完才会运行，不会把报错前的临时赋值当最终值。
# v2 加 version_compatible，v3 加最终 name，v4 加图标声明。
VERSION_CONTRACT_VERSION = 4


def resolve_mod_versions(
    file_text: str, timeout: float = FULL_FILE_TIMEOUT, folder_name: str | None = None
) -> dict[str, Any] | None:
    """完整执行 ``modinfo.lua`` 并返回列表所需的可信简单元数据。"""
    preamble = ""
    if folder_name is not None:
        escaped = folder_name.replace("\\", "\\\\").replace('"', '\\"')
        preamble = f'folder_name = "{escaped}"\n'
    code = (
        f"{preamble}{file_text}\n"
        "return {\n"
        "  name = {declared = name ~= nil, value = name},\n"
        "  icon = {declared = icon ~= nil, value = icon},\n"
        "  icon_atlas = {declared = icon_atlas ~= nil, value = icon_atlas},\n"
        "  version = {declared = version ~= nil, value = version},\n"
        "  version_compatible = {declared = version_compatible ~= nil, "
        "value = version_compatible},\n"
        "}\n"
    )
    result = run_lua_snippet(code, timeout=timeout)
    if not isinstance(result, dict):
        return None
    for field in ("name", "icon", "icon_atlas", "version", "version_compatible"):
        item = result.get(field)
        if not isinstance(item, dict) or not isinstance(item.get("declared"), bool):
            return None
    return result




_FIELDS_TO_READ_BACK = (
    "name",
    "author",
    "version",
    "version_compatible",
    "description",
    "icon",
    "icon_atlas",
    "configuration_options",
)


def resolve_full_config_options(
    file_text: str, timeout: float = FULL_FILE_TIMEOUT, folder_name: str | None = None
) -> Any:
    """执行整份 modinfo.lua，一次读回 name/author/version/version_compatible/description/
    icon/icon_atlas/configuration_options。

    folder_name：引擎会注入该全局（modindex.lua 的 ``env.folder_name = modname``），Mod 常用
    ``folder_name:find("workshop-")`` 区分来源；不注入会让整次执行报错，连之前已算好的字段也丢失。
    以一句 Lua 赋值拼在源码前面传入。

    执行整份文件能拿到条件重新赋值后的最终值（如 locale 为 zh 时改写的 name），这是静态
    解析做不到的。引用沙箱没有的引擎全局（GLOBAL、STRINGS、TheNet 等）时会快速失败，调用方
    保留静态结果。入口见 parser.resolve_full_modinfo()。

    返回字段 dict（未设置的字段可能缺失），整体失败/超时返回 None。
    """
    fields = ", ".join(f"{f} = {f}" for f in _FIELDS_TO_READ_BACK)
    preamble = ""
    if folder_name is not None:
        escaped = folder_name.replace("\\", "\\\\").replace('"', '\\"')
        preamble = f'folder_name = "{escaped}"\n'
    return run_lua_snippet(
        f"{preamble}{file_text}\nreturn {{{fields}}}\n", timeout=timeout
    )


def resolve_dynamic_option(
    preamble: str, raw_options_expr: str, timeout: float = DEFAULT_TIMEOUT
) -> list[dict] | None:
    """运行 preamble 后对 ``raw_options_expr`` 求值，得到动态选项列表。

    返回与 _extract_choices() 相同形状的列表，形状不符或执行失败返回 None，不做猜测。
    """
    if not preamble or not raw_options_expr:
        return None
    if not _looks_balanced(preamble):
        # preamble 不完整时退回最大闭合前缀（见 _largest_balanced_prefix）
        preamble = _largest_balanced_prefix(preamble)
        if not preamble:
            return None
    result = run_lua_snippet(
        f"{preamble}\nreturn ({raw_options_expr})\n", timeout=timeout
    )
    if not isinstance(result, list) or not result:
        return None
    choices = []
    for item in result:
        if not isinstance(item, dict) or "description" not in item:
            return None  # 形状不对——不对部分结果做猜测
        choices.append(
            {
                "description": str(item["description"]),
                "data": item.get("data", item["description"]),
                "hover": item.get("hover") or "",
            }
        )
    return choices
