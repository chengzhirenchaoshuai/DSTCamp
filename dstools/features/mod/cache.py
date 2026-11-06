"""缓存完整 Lua 沙箱解析结果，避免重复执行未变化的 ``modinfo.lua``。

命中条件：modinfo.lua 内容哈希、Mod 目录路径与缓存格式完全一致。不用修改时间判断：同步脚本
（robocopy 等）会保留源文件的旧时间，改过的 modinfo 可能比缓存还"旧"，缓存就一直不失效。
"""

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from dstools.features.mod.parser import ModConfigOption
from dstools.shared.resource_paths import cache_dir

_CACHE_DIR = cache_dir("mod_full_resolve")

# 修改 ModConfigOption 字段、选项判定规则（如标题识别）或缓存结构时递增：缓存只按 modinfo
# 内容判断，不会随解析器代码自动失效。
_CACHE_FORMAT_VERSION = 4


def _cache_path(workshop_id: str) -> Path:
    return _CACHE_DIR / f"{workshop_id}.json"


def _source_key(modinfo_path: Path) -> tuple[str, str] | None:
    """modinfo.lua 的内容哈希与所在目录；读不了时返回 None（不用缓存）。Mod 目录本来就是绝对路径，
    不用 resolve()（Windows 上很慢）。"""
    try:
        return hashlib.sha256(modinfo_path.read_bytes()).hexdigest(), str(modinfo_path.parent)
    except OSError:
        return None


def load_cached_result(workshop_id: str, modinfo_path: Path) -> dict[str, Any] | None:
    """返回仍匹配源文件和缓存格式的沙箱结果。"""
    key = _source_key(modinfo_path)
    if key is None:
        return None
    cache_path = _cache_path(workshop_id)
    try:
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (raw.get("_cache_format_version") != _CACHE_FORMAT_VERSION
            or [raw.get("_sha256"), raw.get("_source_path")] != list(key)):
        return None
    for name in ("_cache_format_version", "_sha256", "_source_path"):
        raw.pop(name, None)
    if "config_options" in raw:
        try:
            raw["config_options"] = [ModConfigOption(**o) for o in raw["config_options"]]
        except TypeError:
            # 缓存结构损坏时重新解析，不能让单个 Mod 阻断列表加载。
            return None
    return raw


def save_result(workshop_id: str, modinfo_path: Path, result: dict[str, Any]) -> None:
    """尽力保存解析结果；缓存写入失败不影响业务。"""
    key = _source_key(modinfo_path)
    if not result or key is None:
        return
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        serializable = dict(result)
        if "config_options" in serializable:
            serializable["config_options"] = [asdict(o) for o in serializable["config_options"]]
        serializable["_cache_format_version"] = _CACHE_FORMAT_VERSION
        serializable["_sha256"], serializable["_source_path"] = key
        _cache_path(workshop_id).write_text(
            json.dumps(serializable, ensure_ascii=False), encoding="utf-8")
    except (OSError, TypeError, ValueError):
        pass
