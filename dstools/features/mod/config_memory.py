"""Mod 默认配置：对应游戏主菜单"模组"里的全局配置，与游戏客户端的 mod_config_data 双向同步。

游戏有两类配置文件（见游戏脚本 ModsTab:ConfigureSelectedMod、ModIndex:GetModConfigurationName）：
- 主菜单"模组"（全局）：client_config=true，普通 Mod 写 modconfiguration_<mod>_CLIENT，
  client_only Mod 写 modconfiguration_<mod>；这是默认配置的同步对象。
- 存档"模组"页：写存档 shardindex 和不带后缀的 modconfiguration_<mod>，后者每次选中存档都会被覆盖，
  只是中转，不同步。所以普通 Mod 不带后缀的文件一律忽略。

每条记录含 values、updated_at，以及每个游戏账号上次同步时双方一致的值 synced[账号]，作为三方比较的基准：
只有一方相对基准变化时向另一方同步；双方都变化时以较新的一方为准（游戏一方取文件修改时间），
覆盖游戏文件前备份原文件。写游戏文件只能在游戏关闭时进行，否则保持待推送，下次同步再推。
比较只看双方都有的选项。
"""

import json
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dstools.features.mod import game_mod_config
from dstools.shared.resource_paths import data_dir

# 第 1 版同步的是存档中转文件（每次选存档都会被覆盖），内容不可信：升级时丢弃，重新从游戏拉取
_VERSION = 2
_lock = threading.RLock()


def memory_dir() -> Path:
    return data_dir("mod_config_memory")


def _memory_file() -> Path:
    return memory_dir() / "memory.json"


def _backup_dir() -> Path:
    return memory_dir() / "game_backup"


def load_memory(path: Path | None = None) -> dict:
    path = path or _memory_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict) or data.get("version") != _VERSION:
        data = {"version": _VERSION}
    data.setdefault("defaults", {})
    return data


def save_memory(data: dict, path: Path | None = None) -> None:
    path = path or _memory_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def same_value(a: Any, b: Any) -> bool:
    """严格比较：Python 中 True == 1，但对 Mod 配置是不同的值。"""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    return type(a) is type(b) and a == b


def same_values(a: dict | None, b: dict | None) -> bool:
    if a is None or b is None:
        return False
    return all(same_value(a[k], b[k]) for k in a.keys() & b.keys())


def game_file_name(mod_key: str, client_only: bool) -> str:
    """默认配置对应的游戏全局配置文件名。"""
    return game_mod_config.file_name(mod_key, client=not client_only)


# ── DSTCamp 一方 ────────────────────────────────────────────────────────

def remember(mod_key: str, values: dict, *, replace: bool = False, now: float | None = None,
             path: Path | None = None) -> None:
    """保存在 DSTCamp 中修改的默认配置；replace=False 时与已有记录合并。"""
    with _lock:
        data = load_memory(path)
        rec = data["defaults"].setdefault(mod_key, {"values": {}, "synced": {}})
        old = rec.get("values") or {}
        merged = dict(values) if replace else {**old, **values}
        if same_values(merged, old) and merged.keys() == old.keys():
            return
        rec["values"] = merged
        rec["updated_at"] = time.time() if now is None else now
        save_memory(data, path)


def recall(mod_key: str, *, path: Path | None = None) -> dict | None:
    with _lock:
        rec = load_memory(path)["defaults"].get(mod_key)
    return dict(rec["values"]) if rec and rec.get("values") is not None else None


def sanitize_values(values: dict, mod_info, *, for_save: bool = True) -> dict:
    """按当前 modinfo 过滤默认配置：丢掉已不存在的选项，取值不在可选范围的不写入（即用 Mod 默认值）。
    for_save 时去掉 client=true 的客户端选项（不属于存档；client_only Mod 除外）。
    动态选项与 Configs Extended 的集合/数组/文本/字典类选项无法枚举取值，原样保留。"""
    result = {}
    for opt in getattr(mod_info, "config_options", None) or []:
        if opt.is_header or not opt.name or opt.name not in values:
            continue
        if for_save and opt.client and not mod_info.client_only:
            continue
        value = values[opt.name]
        free_form = (opt.is_dynamic or opt.is_set_config or opt.is_array_config
                     or opt.is_text_config or opt.is_dictionary_config or not opt.choices)
        if free_form or any(same_value(choice.get("data"), value) for choice in opt.choices):
            result[opt.name] = value
    return result


def recall_for(mod_key: str, mod_info, *, path: Path | None = None) -> dict:
    """取默认配置并按当前 modinfo 校验，用于写入存档；没有记录或 modinfo 时返回空字典。"""
    if mod_info is None:
        return {}
    try:
        values = recall(mod_key, path=path)
    except OSError:
        return {}
    return sanitize_values(values, mod_info) if values else {}


# ── 双向同步 ────────────────────────────────────────────────────────────

@dataclass
class SyncReport:
    pulled: list[str] = field(default_factory=list)
    pushed: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    # pushed 中游戏运行时写入的（client_only Mod）：退回主菜单或进一次世界后生效
    pushed_live: list[str] = field(default_factory=list)
    conflicts: list[tuple[str, str]] = field(default_factory=list)  # (文件名, "game"/"dstcamp" 胜出方)
    errors: list[tuple[str, str]] = field(default_factory=list)
    # mod_key -> "synced"（与游戏一致）/"pending"（等游戏关闭后写入）/"error"
    states: dict[str, str] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return bool(self.pulled or self.pushed or self.conflicts)


_LOG_MAX_LINES = 1000


def append_log(report: SyncReport, path: Path | None = None) -> None:
    """把有动作的同步结果追加到 sync.log（只保留最近若干行），供事后核对被覆盖的一方。"""
    lines = [f"拉取 {name}" for name in report.pulled] + [f"推送 {name}" for name in report.pushed]
    lines += [f"冲突 {name}：{'游戏' if winner == 'game' else 'DSTCamp'} 较新，以其为准" for name, winner in report.conflicts]
    lines += [f"错误 {name}：{message}" for name, message in report.errors]
    if not lines:
        return
    path = path or memory_dir() / "sync.log"
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        old = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join((old + [f"{stamp} {line}" for line in lines])[-_LOG_MAX_LINES:]) + "\n",
                        encoding="utf-8")
    except OSError:
        pass


def _backup(path: Path, backup_dir: Path) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup_dir / path.name)


def sync_with_game(user_dir: Path, account: str, client_only_keys: set[str], *, can_write: bool,
                   memory_path: Path | None = None, backup_dir: Path | None = None) -> SyncReport:
    """与 user_dir 下游戏的全局配置双向同步。client_only_keys 为本机已安装的 client_only Mod，决定对应哪个文件；
    未安装（无法判断）的 Mod 只认 _CLIENT 文件。can_write=False（游戏运行中）时只拉取，需要推送的记为 pending；
    client_only Mod 例外，照常写入并记入 pushed_live：游戏只在自己的配置界面"应用"时写它的文件，每次加载 Mod
    （进世界、退回主菜单）都从磁盘重读（2026-11 真机实测）。其它 Mod 运行中写入的情形没有实测，仍等游戏关闭。"""
    report = SyncReport()
    config_dir = game_mod_config.mod_config_dir(user_dir)
    # 按账号分目录：多个账号的同名配置文件备份不能互相覆盖
    backup_dir = backup_dir or _backup_dir() / account
    with _lock:
        data = load_memory(memory_path)
        files = game_mod_config.list_config_files(config_dir)
        keys = {key for key, client in files if client != (key in client_only_keys)}
        keys.update(data["defaults"])
        dirty = False
        for key in sorted(keys):
            path = config_dir / game_file_name(key, key in client_only_keys)
            name = path.name
            try:
                game = game_mod_config.read_values(path) if path.exists() else None
            except game_mod_config.GameConfigError as exc:
                report.errors.append((name, str(exc)))  # 解析不了的文件既不拉取也不覆盖
                report.states[key] = "error"
                continue
            rec = data["defaults"].get(key)
            mine = rec.get("values") if rec else None
            synced = rec.setdefault("synced", {}) if rec else {}
            base = synced.get(account)

            # 注意区分"没有记录"（None）与"记录为空"（没有选项的 Mod，值为 {}）
            if game is None and not mine:
                continue
            if game is not None and mine is not None and same_values(game, mine):
                if base is None or not same_values(base, mine):
                    synced[account] = dict(mine)
                    dirty = True
                report.states[key] = "synced"
                continue
            if game is None:
                action = "push"
            elif mine is None:
                action = "pull"
            else:
                game_changed, mine_changed = not same_values(game, base), not same_values(mine, base)
                if game_changed and not mine_changed:
                    action = "pull"
                elif mine_changed and not game_changed:
                    action = "push"
                else:
                    newer_game = path.stat().st_mtime >= float(rec.get("updated_at") or 0)
                    action = "pull" if newer_game else "push"
                    report.conflicts.append((name, "game" if newer_game else "dstcamp"))

            if action == "pull":
                data["defaults"][key] = {"values": dict(game), "updated_at": path.stat().st_mtime,
                                         "synced": {**synced, account: dict(game)}}
                report.pulled.append(name)
                report.states[key] = "synced"
                dirty = True
            elif not can_write and key not in client_only_keys:
                report.pending.append(name)
                report.states[key] = "pending"
            else:
                try:
                    if path.exists():
                        _backup(path, backup_dir)
                    game_mod_config.write_values(path, mine)
                except (OSError, game_mod_config.GameConfigError) as exc:
                    report.errors.append((name, str(exc)))
                    report.states[key] = "error"
                    continue
                synced[account] = dict(mine)
                report.pushed.append(name)
                if not can_write:
                    report.pushed_live.append(name)
                report.states[key] = "synced"
                dirty = True
        if dirty:
            save_memory(data, memory_path)
    return report
