"""兼容「客户端Mod转为服务器Mod」（工坊 3486375086）：在 DSTCamp 里选择要转换的客户端 Mod。

该 Mod 的 ``client_mods_list`` 配置原本只能在游戏建服界面里勾选，结构为::

    client_mods_list = {
        ["workshop-376333686"] = { version = "1.9.2", config = { 选项名 = 值, ... } },
    }

专服把这些 Mod 加进服务器 Mod 列表，玩家进服时自动下载启用；专服自己没下载某个 Mod 时，
用这里记的 version 对外报告版本、用 config 作为服务器给的配置。

生成规则照抄该 Mod 的 modservercreationmain.lua（游戏里点"创建"时执行）：
- version：modinfo 的 version 去首尾空白后转小写；
- config：逐个配置项取"本机保存的值，没有则取默认值"。本机保存的值来自 DSTCamp 的默认配置记忆
  （与游戏 client_save/mod_config_data 双向同步，见 config_memory）。
- 只列 client_only 的 Mod：声明了 all_clients_require_mod 的由该 Mod 自己隐藏、不允许转换。

DSTCamp 每次开服前按本机当前版本重写一遍（refresh_cluster），相当于替用户在游戏里点一次"创建"，
避免被转换的 Mod 更新后记录的版本过时。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from dstools.features.mod import config_memory
from dstools.features.mod.cache import load_cached_result, save_result
from dstools.features.mod.local_version import (
    VERSION_CONFIRMED, VERSION_UNDECLARED, normalize_version_for_compare, resolve_local_mod_version,
)
from dstools.features.mod.manager import load_mod_overrides, save_mod_overrides
from dstools.features.mod.parser import ModInfo, parse_modinfo, resolve_full_modinfo
from dstools.features.mod.sandbox_apply import apply_full_sandbox_result
from dstools.shared.lua_parser import parse_lua_file

logger = logging.getLogger(__name__)

CONVERTER_ID = "workshop-3486375086"
LIST_OPTION = "client_mods_list"

# mod_key -> 该 Mod 的目录；找不到返回 None
FolderLookup = Callable[[str], "Path | None"]


def is_list_option(workshop_id: str, option_name: str) -> bool:
    return workshop_id == CONVERTER_ID and option_name == LIST_OPTION


def is_eligible(info: ModInfo | None) -> bool:
    """能否被转换：只有纯客户端 Mod（ModInfo.client_only 已排除声明了 all_clients_require_mod 的）。"""
    return bool(info and info.client_only)


def full_mod_info(mod_key: str, folder: Path) -> ModInfo | None:
    """静态解析 + 整份文件沙箱解析（命中缓存时很快），得到完整配置项列表。耗时，只在后台线程调用。"""
    info = parse_modinfo(folder)
    if info is None:
        return None
    modinfo_path = folder / "modinfo.lua"
    result = load_cached_result(mod_key, modinfo_path)
    if result is None:
        result = resolve_full_modinfo(folder)
        save_result(mod_key, modinfo_path, result)
    if result:
        apply_full_sandbox_result(info, result)
    return info


def build_entry(mod_key: str, folder: Path, previous: dict | None = None) -> dict | None:
    """按该 Mod 在游戏里的规则生成一条 {version, config}；读不了 modinfo 返回 None。

    版本解析失败（沙箱出错）时沿用上次记录的版本，不能写成空串让玩家进服时对不上。
    """
    info = full_mod_info(mod_key, folder)
    if info is None:
        return None
    local = resolve_local_mod_version(mod_key, folder)
    if local.status == VERSION_CONFIRMED:
        version = normalize_version_for_compare(local.version)
    elif local.status == VERSION_UNDECLARED:
        version = ""  # 该 Mod 写的是 (version or "")
    else:
        version = str((previous or {}).get("version") or "")

    try:
        saved = config_memory.recall(mod_key) or {}
    except OSError:
        saved = {}
    config: dict[str, Any] = {}
    for opt in info.config_options:
        if opt.is_header or not opt.name:
            continue
        value = saved.get(opt.name, opt.default)
        if value is not None:  # Lua 里 nil 值等于没有这个键
            config[opt.name] = value
    return {"version": version, "config": config}


def refresh_list(current: dict, lookup: FolderLookup) -> dict:
    """重新生成列表里每个 Mod 的 version 与 config。本机找不到的 Mod 原样保留（可能只装在别处），
    不像游戏界面那样直接删掉。耗时（可能跑沙箱），只在后台线程或开服前调用。"""
    result: dict = {}
    for mod_key, previous in (current or {}).items():
        previous = previous if isinstance(previous, dict) else {}
        folder = lookup(str(mod_key))
        entry = None
        if folder is not None:
            try:
                entry = build_entry(str(mod_key), folder, previous)
            except Exception:  # 单个 Mod 解析出错不能连累整张表
                logger.exception("生成客户端 Mod 转换记录失败：%s", mod_key)
        result[mod_key] = entry if entry is not None else previous
    return result


def _canonical(value: Any) -> str:
    """比较嵌套表用：JSON 区分 true 与 1（Python 里 True == 1），键排序后与顺序无关。"""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def refresh_cluster(cluster, lookup: FolderLookup) -> list[str]:
    """开服前刷新存档各世界 modoverrides.lua 里的 client_mods_list，返回实际改写的世界名。

    只处理启用了转换 Mod 且列表非空的世界；文件解析失败时不写（load_mod_overrides 失败会返回空配置，
    写回会清空整个文件）。同一份列表只生成一次，各世界写入同样的内容（该 Mod 要求主世界和洞穴一致）。
    """
    changed: list[str] = []
    cache: dict[str, dict] = {}
    for shard in cluster.shards:
        path = shard.mod_overrides_path
        if not path or not Path(path).exists():
            continue
        try:
            parse_lua_file(Path(path))
        except Exception:
            logger.warning("modoverrides.lua 解析失败，跳过客户端 Mod 转换刷新：%s", path)
            continue
        overrides = load_mod_overrides(Path(path))
        entry = overrides.mods.get(CONVERTER_ID)
        if entry is None or not entry.enabled:
            continue
        current = entry.configuration_options.get(LIST_OPTION)
        if not isinstance(current, dict) or not current:
            continue
        key = _canonical(current)
        if key not in cache:
            cache[key] = refresh_list(current, lookup)
        fresh = cache[key]
        if _canonical(fresh) == key:
            continue
        entry.configuration_options[LIST_OPTION] = fresh
        save_mod_overrides(overrides)
        changed.append(shard.name)
    return changed
