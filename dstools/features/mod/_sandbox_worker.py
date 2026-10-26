"""在独立 Lua 5.1 子进程中执行片段并通过 JSON 返回结果（独立进程是超时边界，内嵌解释器的死循环无法可靠中止）。"""

import json
import sys


def _configure_worker_streams() -> None:
    """把 Worker 的标准流统一为 UTF-8；无控制台的 EXE 中标准流可能是 None，导入时不能崩溃。"""
    streams = (
        (sys.stdin, "replace"),
        (sys.stdout, "replace"),
        (sys.stderr, "backslashreplace"),
    )
    for stream, errors in streams:
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors=errors)


def _to_plain(value, _seen=None):
    """把 Lua 表递归转成可 JSON 化的 Python 数据；函数、userdata 等退化为 str()，形状由调用方校验。"""
    _seen = _seen if _seen is not None else set()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if id(value) in _seen:
        return None  # 循环引用的表——直接放弃，不无限递归下去
    try:
        items = list(value.items())
    except AttributeError:
        return str(value)
    _seen = _seen | {id(value)}
    keys = [k for k, _ in items]
    if keys and all(isinstance(k, int) for k in keys) and sorted(keys) == list(range(1, len(keys) + 1)):
        return [_to_plain(v, _seen) for _, v in sorted(items)]
    return {str(k): _to_plain(v, _seen) for k, v in items}


def main():
    _configure_worker_streams()
    if sys.stdin is None or sys.stdout is None:
        raise RuntimeError("Lua 沙箱 Worker 缺少标准输入/输出管道")
    lua_code = sys.stdin.read()

    from lupa.lua51 import LuaRuntime
    rt = LuaRuntime(unpack_returned_tuples=True, register_eval=False)
    g = rt.globals()
    # 纵深防御：输入是不可信的第三方代码，运行前清空所有能接触文件系统/系统/进程/加载代码的接口
    for name in ("os", "io", "require", "dofile", "loadfile", "load",
                 "loadstring", "package", "debug", "collectgarbage"):
        g[name] = None

    # 引擎会提供 locale，Mod 普遍用 ``locale == "zh"`` 选择中文文本；不设置时所有选项都会解析成英文
    g["locale"] = "zh"

    # 引擎注入的 ChooseTranslationTable(tbl) -> tbl[locale] or tbl[1]（见 modindex.lua）。
    # Insight 等会把它拷到局部后清掉全局，缺了就回退英文；多余参数与真实 Lua 一样忽略
    def _choose_translation_table(tbl, *_args):
        try:
            val = tbl["zh"]
        except Exception:
            val = None
        if val is None:
            try:
                val = tbl[1]
            except Exception:
                val = None
        return val

    g["ChooseTranslationTable"] = _choose_translation_table

    # KnownModIndex:InitializeModInfo 的桩：返回空表即可。Chinese++ Pro 的翻译文件由 chs_translation.py
    # 注入 gz_start 走开服工具分支，不再调用它；保留桩兜底其他照抄这种写法、却没有 gz_start 分支的文件
    def _known_mod_index_stub(_self, *_args):
        return rt.table_from({"description": ""})

    g["KnownModIndex"] = rt.table_from({"InitializeModInfo": _known_mod_index_stub})

    result = rt.execute(lua_code)
    sys.stdout.write(json.dumps(_to_plain(result)))


def run_worker_main() -> None:
    """崩溃安全的 Worker 入口（源码直接运行与打包 EXE 带 ``--lua-sandbox-worker`` 两条路径都走这里）。

    Lua 运行时错误（如引用沙箱没有的引擎全局）是常见的预期情况，只以退出码 1 表示解析失败，
    父进程不读 stderr。坑：异常处理不能只写在 ``if __name__ == "__main__"`` 里，打包模式直接
    调用 main()，未处理的 LuaError 会弹出 PyInstaller 的崩溃窗口。
    """
    try:
        main()
    except Exception as e:
        # 写 stderr 本身也不能再抛异常（错误消息可能含 Mod 源码的异常字节）
        try:
            sys.stderr.write(str(e))
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    run_worker_main()
