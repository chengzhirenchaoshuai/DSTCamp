"""世界设置页的数据装载与编辑逻辑（不含界面代码）。"""

from dataclasses import dataclass, field
from pathlib import Path

from dstools.features.mod.parser import resolve_wegame_client_mods_dir
from dstools.features.mod.sync import get_enabled_mod_ids
from dstools.features.world.categories import get_setting_info, localized_name
from dstools.features.world.location_profiles import get_location_definition
from dstools.features.world.mod_icons import resolve_mod_setting_icons
from dstools.features.world.mod_settings import (
    MOD_DISPLAY_NAMES, MOD_VERIFIED_VERSIONS, VOLCANO_ISLAND_KEY, effective_initial_value,
    filter_mod_world_settings, get_mod_categories, get_mod_world_settings, is_verified_version,
)
from dstools.features.mod.shardindex import ShardIndexError
from dstools.features.world.local_options import load_local_overrides, save_local_overrides
from dstools.features.world.reader import (
    LeveldataStatus, WorldOverride, WorldPreset, load_leveldata, merge_overrides, save_leveldata,
)
from dstools.features.world.value_sets import get_value_set
from dstools.features.world.view_model import build_world_view_model
from dstools.models import Cluster, SaveSource

STATUS_OK = "ok"
STATUS_NO_CLUSTER = "no_cluster"
STATUS_NO_LEVELDATA = "no_leveldata"
STATUS_INVALID = "invalid"


@dataclass
class WorldPageData:
    status: str
    is_server: bool = False
    shard_name: str = ""
    path: Path | None = None
    error: str = ""                      # STATUS_INVALID 时的具体异常文本，方便远程排障
    preset: WorldPreset | None = None
    location: str = "forest"
    location_label: str = ""
    mod_settings: dict = field(default_factory=dict)
    mod_categories: list = field(default_factory=list)
    mod_icons: dict = field(default_factory=dict)   # key -> PIL RGBA 图像（尚未转成界面控件的图片）
    rules_by_category: dict = field(default_factory=dict)
    rule_categories: list = field(default_factory=list)
    generation_by_category: dict = field(default_factory=dict)
    generation_categories: list = field(default_factory=list)
    # 本地存档的真实配置在分片 save/shardindex；读不了时退回 leveldataoverride 显示，并记下原因禁止保存
    local_index_error: str = ""
    # 本机版本与登记表核对版本不同的 Mod：[(显示名, 本机版本, 已核对版本)]
    unverified_mods: list = field(default_factory=list)

    @property
    def can_save(self) -> bool:
        return self.preset is not None and (self.is_server or not self.local_index_error)


def load_world_page(
    cluster: Cluster | None, shard_name: str, enabled_mod_ids=None, steam_runtime_mods_dir=None,
) -> WorldPageData:
    """``enabled_mod_ids`` 为 Mod 页尚未保存的启用集合，传入时优先于磁盘上的 modoverrides.lua（即时预览），
    为 None 时按磁盘内容。``steam_runtime_mods_dir`` 为专服实际读取的 mods 目录（与 Mod 页一致），
    只装在那里的 Mod 也能找到图标和版本。"""
    if cluster is None:
        return WorldPageData(STATUS_NO_CLUSTER)
    is_server = cluster.source == SaveSource.SERVER
    shard = next((s for s in cluster.shards if s.name == shard_name), None)
    if shard is None or not shard.leveldata_path:
        return WorldPageData(STATUS_NO_LEVELDATA, is_server=is_server, shard_name=shard_name)

    load_result = load_leveldata(shard.leveldata_path)
    if load_result.status != LeveldataStatus.OK:
        invalid = load_result.status == LeveldataStatus.INVALID
        return WorldPageData(
            STATUS_INVALID if invalid else STATUS_NO_LEVELDATA, is_server=is_server,
            shard_name=shard_name, path=shard.leveldata_path,
            error=str(load_result.error) if invalid and load_result.error else "",
        )

    preset = load_result.preset
    local_index_error = ""
    if not is_server:
        # 游戏选存档时读的是 shardindex 的 world.options，开服时再据此重写 leveldataoverride.lua
        try:
            local = load_local_overrides(shard.path)
            preset.overrides = [
                WorldOverride(key=key, value=value if isinstance(value, str) else str(value))
                for key, value in local.items()
            ]
        except (OSError, ShardIndexError) as exc:
            local_index_error = str(exc)
    location = preset.location or "forest"
    # 已启用 mod 里登记过的条目贡献了哪些"世界设置"/"世界生成"——按整个存档算（get_enabled_mod_ids
    # 本来就是并集所有世界的 modoverrides.lua），不分具体哪个世界。
    all_mod_settings = get_mod_world_settings(
        enabled_mod_ids if enabled_mod_ids is not None else get_enabled_mod_ids(cluster))
    # 图标解析要读 mod 自己的图集文件（第一次或 mod 更新过才会真的调 ktech，其余命中磁盘缓存）
    mod_icons = resolve_mod_setting_icons(
        all_mod_settings, cluster.platform, resolve_wegame_client_mods_dir(cluster.platform),
        steam_runtime_mods_dir)
    is_master_world = shard.name == "Master"
    mod_settings = filter_mod_world_settings(all_mod_settings, location, is_master_world)
    mod_categories = get_mod_categories(mod_settings)
    try:
        location_label = get_location_definition(location).name_zh
    except ValueError:
        location_label = location
    view_model = build_world_view_model(
        preset, mod_settings, mod_categories, is_master_world=is_master_world)
    return WorldPageData(
        STATUS_OK, is_server=is_server, shard_name=shard_name, path=shard.leveldata_path,
        preset=preset, location=location, location_label=location_label,
        mod_settings=mod_settings, mod_categories=mod_categories, mod_icons=mod_icons,
        rules_by_category=view_model.rules_by_category, rule_categories=view_model.rule_categories,
        generation_by_category=view_model.generation_by_category,
        generation_categories=view_model.generation_categories,
        local_index_error=local_index_error,
        unverified_mods=find_unverified_mods(all_mod_settings, cluster.platform, steam_runtime_mods_dir),
    )


def find_unverified_mods(mod_settings: dict, platform,
                         steam_runtime_mods_dir=None) -> list[tuple[str, str, str]]:
    """列出贡献了世界设置、但本机版本不是登记表核对版本的 Mod；版本读不出（目录缺失、沙箱失败）的不报，
    免得误报。版本读取走内容指纹缓存，Mod 没变时不重跑沙箱。"""
    from dstools.features.mod.local_version import VERSION_CONFIRMED, resolve_local_mod_version
    from dstools.features.mod.parser import find_mod_folder

    result = []
    for mod_id in sorted({info.mod_id for info in mod_settings.values()}):
        verified = MOD_VERIFIED_VERSIONS.get(mod_id)
        folder = find_mod_folder(f"workshop-{mod_id}", platform, resolve_wegame_client_mods_dir(platform),
                                 steam_runtime_mods_dir) if verified else None
        if folder is None:
            continue
        version = resolve_local_mod_version(mod_id, folder)
        if version.status != VERSION_CONFIRMED or is_verified_version(mod_id, version.version):
            continue
        name = localized_name(MOD_DISPLAY_NAMES[mod_id]) if mod_id in MOD_DISPLAY_NAMES else (version.name or mod_id)
        result.append((name, version.version, verified))
    return result


def step_rule_value(data: WorldPageData, key: str, delta: int, is_rule: bool = True) -> bool:
    """把设置取值前/后移一格（两端钳制不绕回，与游戏一致）；``is_rule=False`` 为世界生成项。

    存档已有该 key 直接改；没有（刚启用的 Mod、游戏未写过的设置）则从其初始值起移动，转为会被保存的 WorldOverride。
    返回 True 表示显示的项有变化（如切换了火山岛），已重新生成分类列表，调用方需要重新填充面板。
    """
    _step_value(data, key, delta, is_rule)
    if key == VOLCANO_ISLAND_KEY:
        rebuild_view(data)
        return True
    return False


def _step_value(data: WorldPageData, key: str, delta: int, is_rule: bool) -> None:
    preset = data.preset
    values = get_value_set(key, data.mod_settings, location=data.location, is_rule=is_rule)
    override = next((o for o in preset.overrides if o.key == key), None)
    if override is not None:
        try:
            index = values.index(override.value)
        except ValueError:
            index = 0
        override.value = values[max(0, min(len(values) - 1, index + delta))]
        return
    mod_info = data.mod_settings.get(key)
    initial = effective_initial_value(mod_info, data.location, new_world=False) if mod_info else "default"
    try:
        base_index = values.index(initial)
    except ValueError:
        base_index = 0
    new_index = max(0, min(len(values) - 1, base_index + delta))
    _, _, name = get_setting_info(key, data.location, data.mod_settings)
    override = WorldOverride(key=key, value=values[new_index], name=name or key)
    preset.overrides.append(override)
    for by_category in (data.rules_by_category, data.generation_by_category):
        for items in by_category.values():
            for i, row in enumerate(items):
                if row.key == key:
                    items[i] = override
                    break


def generation_requires_reset(data: WorldPageData) -> bool:
    """世界是否已生成过：已生成的地图不会因世界生成设置改变，需要重置世界（c_regenerateworld 会重读
    leveldataoverride.lua 后重新生成）。以分片目录下是否已有 save/session 判断。"""
    return data.path is not None and (data.path.parent / "save" / "session").is_dir()


def lua_value_types(data: WorldPageData) -> dict[str, list[str]]:
    """保存时用于还原 Lua 数字类型的取值表（只有 Mod 登记的全数字取值会被转成数字）。"""
    return {key: info.values for key, info in data.mod_settings.items() if info.values}


def current_override_table(data: WorldPageData) -> dict:
    """当前取值（含未保存的修改）的 overrides 表；数字、布尔等非字符串原值保持原类型，供保存为预设。"""
    if data.preset is None:
        raise ValueError("当前世界没有可用的设置")
    if not data.is_server and not data.local_index_error and data.path is not None:
        existing = load_local_overrides(data.path.parent)
    else:
        existing = data.preset.raw.get("overrides") or {}
    return merge_overrides(existing, data.preset.overrides, lua_value_types(data))


def apply_preset_values(data: WorldPageData, overrides: dict) -> tuple[list[str], list[str]]:
    """把预设取值套到页面（不写文件，由用户再点保存），并重建视图模型；返回 (改了值的 key, 没有套用的 key)。

    当前世界类型未登记的 key、只读的 Mod 项，以及与当前值不同的非字符串值（界面只能按字符串编辑，
    写成字符串会让 Mod 的 == true 判断失效）都跳过。"""
    from dstools.features.world.reader import _display_value
    from dstools.features.world.user_presets import applicable_overrides

    preset = data.preset
    current = {o.key: o for o in preset.overrides}
    changed = []
    values, skipped = applicable_overrides(overrides, data.location, data.mod_settings)
    for key, value in values.items():
        override = current.get(key)
        text = _display_value(value)
        if override is not None and override.value == text:
            continue
        elif not isinstance(value, str):
            skipped.append(key)
        elif override is not None:
            override.value = text
            changed.append(key)
        else:
            _, _, name = get_setting_info(key, data.location, data.mod_settings)
            override = WorldOverride(key=key, value=text, name=name or key)
            preset.overrides.append(override)
            current[key] = override
            changed.append(key)
    rebuild_view(data)
    return changed, sorted(skipped)


def rebuild_view(data: WorldPageData) -> None:
    """按当前取值重新生成分类列表（显示哪些项取决于取值时调用，如海难的火山岛开关）。"""
    view_model = build_world_view_model(
        data.preset, data.mod_settings, data.mod_categories, is_master_world=data.shard_name == "Master")
    data.rules_by_category, data.rule_categories = view_model.rules_by_category, view_model.rule_categories
    data.generation_by_category = view_model.generation_by_category
    data.generation_categories = view_model.generation_categories


def save_world_page(data: WorldPageData) -> None:
    """保存当前取值。服务器存档写 leveldataoverride.lua；本地存档先写游戏真正读取的各分片
    save/shardindex，再同步写 leveldataoverride.lua 保持一致。本地存档须在游戏关闭时调用。"""
    if not data.can_save or data.path is None:
        raise ValueError(data.local_index_error or "当前世界无法保存")
    value_sets = lua_value_types(data)
    if not data.is_server:
        shard_dir = data.path.parent
        merged = merge_overrides(load_local_overrides(shard_dir), data.preset.overrides, value_sets)
        save_local_overrides(shard_dir, merged)
    save_leveldata(data.preset, data.path, value_sets)
