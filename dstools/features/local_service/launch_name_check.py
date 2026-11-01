"""开服前的名字检查：专服读不了非英文的存档/世界文件夹名，LuaJIT 注入器遇到系统编码转不了的文件名会崩。

两个坑都在 2026-11-01 实测确认：
- 专服按系统 ANSI 代码页读命令行，``-cluster 疯狂老虎机`` 变成 GBK 字节后再逐字节当字符，
  读写的是 Klei 根目录下新建的乱码文件夹 ``·è¿ñÀÏ»¢»ú``，真正的存档没被读到，也找不到令牌。
- 这个乱码文件夹留在 Klei 根目录后，LuaJIT 的 Injector.dll 扫描根目录时把名字转成 ANSI，
  ``»``、``¿`` 等字符转不了，抛未捕获的 C++ 异常，专服启动即崩（0xE06D7363）。只扫根目录一层。
"""

from __future__ import annotations

import sys
from pathlib import Path

# Windows 的 mbcs 编解码器就是系统 ANSI 代码页，严格模式下转不了即报错，与注入器的行为一致
_ANSI_ENCODING = "mbcs" if sys.platform == "win32" else "gbk"


def non_ascii_names(names) -> list[str]:
    """专服读不了的存档/世界文件夹名（含非 ASCII 字符的）。"""
    return [name for name in names if not name.isascii()]


def unencodable_entries(klei_root: Path, encoding: str = _ANSI_ENCODING) -> list[str]:
    """Klei 根目录下名字转不成系统 ANSI 编码的条目（只看第一层，与注入器一致）。"""
    result = []
    try:
        entries = sorted(p.name for p in Path(klei_root).iterdir())
    except OSError:
        return result
    for name in entries:
        try:
            name.encode(encoding)
        except UnicodeEncodeError:
            result.append(name)
    return result


def guess_original_name(name: str, encoding: str = _ANSI_ENCODING) -> str | None:
    """乱码名字是 ANSI 字节被逐个当字符落盘时，还原出原来的名字（如 ``·è¿ñÀÏ»¢»ú`` → 疯狂老虎机）。"""
    if not name or any(ord(c) > 0xFF for c in name):
        return None
    try:
        original = bytes(ord(c) for c in name).decode(encoding)
    except UnicodeDecodeError:
        return None
    return original if original != name else None
