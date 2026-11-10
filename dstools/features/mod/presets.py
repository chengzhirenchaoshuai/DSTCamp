"""Mod 配置集：保存一批 Mod 的启用状态与配置项，一键套用到任意存档。

形状与 ModEntry 一致（{enabled, configuration_options}），复用 manager.py 的读写逻辑。
套用时需处理（见 plan_apply_preset）：
1. Mod 已取消订阅：仍写入（游戏容忍缺失），但在报告中提示；
2. Mod 更新删除了配置项：跳过不写；新增的项保持默认；
3. 候选值变化：按当前 choices 核对，不在范围内只提示不拦截；
4. 依赖 Configs Extended 的自由文本配置：只检查 key 是否存在，并提示该共享库是否已安装。
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from dstools.shared import app_settings
from dstools.i18n import t
from dstools.models import ModEntry

if TYPE_CHECKING:
    from dstools.features.mod.parser import ModInfo
    from dstools.models import Cluster

_FORMAT_VERSION = 1

# Configs Extended 共享库：集合/数组/文本/字典类配置要靠它才能生效
CONFIGS_EXTENDED_WORKSHOP_ID = "workshop-3317960157"


@dataclass
class ModPreset:
    """Mod 状态快照：mods 为 workshop_id -> {"enabled", "configuration_options"}（不存展示信息）。"""
    name: str
    mods: dict[str, dict] = field(default_factory=dict)
    created_at: str = ""
    source_platform: str = ""  # 仅展示用（"steam"/"wegame"），不做强制校验
    # "禁用客户端 Mod"开关；None 表示配置集没带它（旧配置集或保存时没勾选），套用时不改存档现状
    client_mods_disabled: bool | None = None


@dataclass
class ApplyIssue:
    """plan_apply_preset() 发现的一条需要用户注意的情况——不阻止写入，
    只是不能悄悄过去。"""
    workshop_id: str
    display_name: str
    kind: str  # "missing" | "stale_option" | "invalid_value"
    detail: str


@dataclass
class ApplyPlan:
    """apply_preset() 写盘前的只读计划，供界面确认。"""
    preset: ModPreset
    ok_ids: list[str] = field(default_factory=list)  # 会被写入的 mod id（含带 issue 的）
    issues: list[ApplyIssue] = field(default_factory=list)
    needs_configs_extended: bool = False  # 用了自由文本配置，但这台机器没有 Configs Extended
    # wid -> 判定为已废弃的配置项 key，apply_preset() 直接跳过（只判定一次）
    stale_options: dict = field(default_factory=dict)


def list_presets() -> list[ModPreset]:
    """按名字排序返回全部配置集；单条数据损坏时跳过，不影响整个列表。"""
    presets = []
    for item in app_settings.get_mod_presets():
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        mods = item.get("mods")
        if not name or not isinstance(mods, dict):
            continue
        switch = item.get("client_mods_disabled")
        presets.append(ModPreset(
            name=name,
            mods=mods,
            created_at=item.get("created_at", ""),
            source_platform=item.get("source_platform", ""),
            client_mods_disabled=switch if isinstance(switch, bool) else None,
        ))
    presets.sort(key=lambda p: p.name)
    return presets


def find_preset(name: str) -> ModPreset | None:
    return next((p for p in list_presets() if p.name == name), None)


def _to_raw(preset: ModPreset) -> dict:
    raw = {
        "format_version": _FORMAT_VERSION,
        "name": preset.name,
        "mods": preset.mods,
        "created_at": preset.created_at,
        "source_platform": preset.source_platform,
    }
    # 没带开关时不写这个键，旧版 DSTCamp 读到的配置集与原来完全一样
    if preset.client_mods_disabled is not None:
        raw["client_mods_disabled"] = preset.client_mods_disabled
    return raw


def save_preset(preset: ModPreset, overwrite: bool = False) -> bool:
    """保存一个配置集；已存在同名的且 overwrite=False 时不覆盖、返回
    False，调用方据此弹"是否覆盖"确认。"""
    raw = app_settings.get_mod_presets()
    existing_idx = next((i for i, item in enumerate(raw)
                          if isinstance(item, dict) and item.get("name") == preset.name), None)
    if existing_idx is not None and not overwrite:
        return False
    entry = _to_raw(preset)
    if existing_idx is not None:
        raw[existing_idx] = entry
    else:
        raw.append(entry)
    app_settings.set_mod_presets(raw)
    return True


def delete_preset(name: str) -> None:
    raw = [item for item in app_settings.get_mod_presets()
           if not (isinstance(item, dict) and item.get("name") == name)]
    app_settings.set_mod_presets(raw)


def capture_preset(name: str, mod_data: dict, mod_infos: dict, selected_ids: set,
                    source_platform: str, client_mods_disabled: bool | None = None) -> ModPreset:
    """把当前界面状态（可能含未保存改动）中 selected_ids 的 Mod 打包成配置集；
    client_mods_disabled 为 None 表示不带"禁用客户端 Mod"开关。

    坑：modoverrides.lua 里可能混有标题项的占位键（如 AddTitle 生成的 "null"），打包时按 Mod
    当前声明的非标题选项过滤，否则套用时会被误报为"已不再声明的选项"。拿不到可信的选项名单
    （未解析或 schema 不识别）时整段原样保留，不丢用户的真实设置。
    """
    mods = {}
    for wid in selected_ids:
        entry = mod_data.get(wid)
        if entry is None:
            continue
        config = dict(entry.configuration_options)
        info = mod_infos.get(wid)
        if info is not None and not info.unsupported_schema:
            valid_names = {o.name for o in info.config_options if not o.is_header}
            config = {k: v for k, v in config.items() if k in valid_names}
        mods[wid] = {"enabled": entry.enabled, "configuration_options": config}
    return ModPreset(name=name, mods=mods,
                      created_at=datetime.now().isoformat(timespec="seconds"),
                      source_platform=source_platform, client_mods_disabled=client_mods_disabled)


def _is_freeform_option(opt) -> bool:
    return opt.is_set_config or opt.is_array_config or opt.is_text_config or opt.is_dictionary_config


def plan_apply_preset(preset: ModPreset, mod_infos: dict) -> ApplyPlan:
    """对照本机解析出的 Mod 信息生成只读计划，界面展示 plan.issues 后再决定是否 apply_preset()。

    mod_infos 应覆盖本机全部已安装 Mod，键不存在表示本机没有该 Mod。
    """
    plan = ApplyPlan(preset=preset)
    needs_ce = False
    has_configs_extended = CONFIGS_EXTENDED_WORKSHOP_ID in mod_infos

    for wid, saved in preset.mods.items():
        info: "ModInfo | None" = mod_infos.get(wid) if wid in mod_infos else None
        display_name = (info.name if info else "") or wid

        if wid not in mod_infos:
            plan.issues.append(ApplyIssue(wid, display_name, "missing", t("preset.issue_missing")))
            plan.ok_ids.append(wid)
            continue

        plan.ok_ids.append(wid)
        if info is None:
            # 装过但 modinfo.lua 这次解析失败：仍写入，只是无法做选项级校验
            continue

        current_opts = {o.name: o for o in info.config_options if not o.is_header}
        saved_options = saved.get("configuration_options") or {}
        for key, value in saved_options.items():
            opt = current_opts.get(key)
            if opt is None:
                plan.issues.append(ApplyIssue(wid, display_name, "stale_option",
                                               t("preset.issue_stale_option", option=key)))
                plan.stale_options.setdefault(wid, set()).add(key)
                continue
            if _is_freeform_option(opt):
                if wid != CONFIGS_EXTENDED_WORKSHOP_ID and not has_configs_extended:
                    needs_ce = True
                continue
            # 只核对有固定候选列表的非动态选项。坑：default 可以是候选列表之外的哨兵值
            # （如 workshop-1553396970 的 default=0 而选项为 1~100%），等于 default 不算异常
            if opt.choices and not opt.is_dynamic and value != opt.default:
                if not any(c.get("data") == value for c in opt.choices):
                    plan.issues.append(ApplyIssue(wid, display_name, "invalid_value",
                                                   t("preset.issue_invalid_value", option=key)))

    plan.needs_configs_extended = needs_ce
    return plan


def apply_preset(cluster: "Cluster", plan: ApplyPlan, clear_first: bool = False) -> int:
    """把 plan.ok_ids 的 Mod 状态写入存档每个世界的 modoverrides.lua，返回处理的世界数。

    默认合并：只覆盖配置集中的 Mod；clear_first=True 先清空现有 Mod 状态（调用方须已获用户确认）。
    "禁用客户端 Mod"开关只写服务器存档：本地存档开服时游戏会重写 modoverrides.lua，留不住它。
    """
    from dstools.features.mod.manager import load_mod_overrides, save_mod_overrides
    from dstools.models import SaveSource

    switch_supported = cluster.source == SaveSource.SERVER
    count = 0
    for shard in cluster.shards:
        if not shard.mod_overrides_path:
            continue
        overrides = load_mod_overrides(shard.mod_overrides_path)
        if clear_first:
            overrides.mods.clear()
            overrides.client_mods_disabled = False
        if switch_supported and plan.preset.client_mods_disabled is not None:
            overrides.client_mods_disabled = plan.preset.client_mods_disabled
        for wid in plan.ok_ids:
            saved = plan.preset.mods.get(wid)
            if not saved:
                continue
            config = dict(saved.get("configuration_options") or {})
            for stale_key in plan.stale_options.get(wid, ()):
                config.pop(stale_key, None)
            overrides.mods[wid] = ModEntry(
                workshop_id=wid,
                enabled=bool(saved.get("enabled", True)),
                configuration_options=config,
            )
        save_mod_overrides(overrides)
        count += 1
    return count
