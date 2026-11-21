"""静态解析 ``modinfo.lua`` 的结果缓存：Mod 很多时，每次启动逐个解析要好几秒。

命中条件：源文件内容哈希、Mod 目录路径、解析器代码指纹与缓存格式完全一致。解析器任何函数或
数据类字段变化都会改变指纹，旧缓存自动失效，不需要手动递增版本号。
"""

import hashlib
import json
import marshal
import types
from dataclasses import asdict, fields
from pathlib import Path

from dstools.features.mod import parser
from dstools.features.mod.parser import ModConfigOption, ModInfo, parse_modinfo
from dstools.shared.lua_parser import unescape_lua_string
from dstools.shared.resource_paths import cache_dir

_CACHE_DIR = cache_dir("modinfo_static")
_CACHE_FORMAT_VERSION = 1


def _parser_fingerprint() -> str:
    """parser 模块全部函数的字节码与两个数据类的字段名；EXE 里没有源码也能算。"""
    digest = hashlib.sha256()
    for name in sorted(vars(parser)):
        value = getattr(parser, name)
        if isinstance(value, types.FunctionType) and value.__module__ == parser.__name__:
            digest.update(name.encode("utf-8"))
            digest.update(marshal.dumps(value.__code__))
    # 字符串转义解码在 lua_parser 里，改它同样会改变解析结果
    digest.update(marshal.dumps(unescape_lua_string.__code__))
    for cls in (ModInfo, ModConfigOption):
        digest.update(",".join(f.name for f in fields(cls)).encode("utf-8"))
    return digest.hexdigest()


_PARSER_FINGERPRINT = _parser_fingerprint()


def _cache_path(source_path: str) -> Path:
    return _CACHE_DIR / f"{hashlib.sha1(source_path.encode('utf-8')).hexdigest()}.json"


def _to_payload(info: ModInfo) -> dict:
    return asdict(info)


def _from_payload(data: dict) -> ModInfo:
    data = dict(data)
    data["config_options"] = [ModConfigOption(**o) for o in data.get("config_options") or ()]
    return ModInfo(**data)


def parse_modinfo_cached(mod_folder: Path) -> ModInfo | None:
    """与 parse_modinfo 结果相同；源文件没变时直接读缓存。缓存读写失败一律退回实时解析。"""
    mod_folder = Path(mod_folder)
    modinfo_path = mod_folder / "modinfo.lua"
    try:
        content_hash = hashlib.sha256(modinfo_path.read_bytes()).hexdigest()
        source_path = str(mod_folder.resolve())
    except OSError:
        return parse_modinfo(mod_folder)
    cache_path = _cache_path(source_path)
    try:
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
        if (raw.get("cache_format") == _CACHE_FORMAT_VERSION and raw.get("parser") == _PARSER_FINGERPRINT
                and raw.get("sha256") == content_hash and raw.get("source_path") == source_path):
            return _from_payload(raw["info"])
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        pass

    info = parse_modinfo(mod_folder)
    if info is None:
        return None
    try:
        payload = _to_payload(info)
        text = json.dumps({
            "cache_format": _CACHE_FORMAT_VERSION,
            "parser": _PARSER_FINGERPRINT,
            "sha256": content_hash,
            "source_path": source_path,
            "info": payload,
        }, ensure_ascii=False)
        # JSON 会把数字键变成字符串、元组变成列表：读回来不完全相等的结果不缓存
        if _from_payload(json.loads(text)["info"]) != info:
            return info
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(text, encoding="utf-8")
    except (OSError, TypeError, ValueError):
        pass
    return info
