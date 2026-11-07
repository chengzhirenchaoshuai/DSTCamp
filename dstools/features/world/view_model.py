"""把世界文件数据转换为可渲染的世界设置视图模型（不读写文件，编辑页与创建向导共用）。"""

from dataclasses import dataclass

from dstools.features.world.categories import (
    _get_settings,
    get_categories,
    get_order_key,
    get_setting_info,
    localized_name,
)
from dstools.features.world.reader import WorldOverride, WorldPreset
from dstools.features.world.mod_settings import (
    VOLCANO_ISLAND_KEY,
    effective_initial_value,
    get_mod_vanilla_world_patches,
    volcano_island_hidden_keys,
)


@dataclass
class WorldDisplayOverride:
    """仅用于显示的默认设置；被修改后才会变成 WorldOverride 并写入。"""

    key: str
    value: str
    name: str
    persisted: bool = False


@dataclass
class WorldViewModel:
    """一个 shard 的完整世界设置展示数据。"""

    location: str
    rules_by_category: dict[str, list[WorldOverride | WorldDisplayOverride]]
    generation_by_category: dict[str, list[WorldOverride | WorldDisplayOverride]]
    rule_categories: list[tuple[str, str]]
    generation_categories: list[tuple[str, str]]


def build_world_view_model(
    preset: WorldPreset, mod_settings: dict,
    mod_categories: list[tuple[str, str]] | None = None,
    is_master_world: bool = True,
    new_world: bool = False,
    active_mod_ids=None,
) -> WorldViewModel:
    """按位置和已启用 Mod 补齐可展示项，并保持未保存项显式可辨。

    ``new_world`` 为创建向导里尚未写盘的世界：缺项的 Mod 设置显示游戏默认值（创建时会写入），
    已有世界则显示缺项时的实际取值（见 ModWorldSetting.missing_values）。
    ``active_mod_ids`` 为已启用的 Mod（纯数字 ID）：有的 Mod 在当前世界没有设置项，却会隐藏原版项
    （如云霄国度把四季等限定到森林），不传时只按 ``mod_settings`` 里出现的 Mod 处理。"""
    location = preset.location or "forest"
    visible_mod_settings = {
        key: info for key, info in mod_settings.items()
        if not hasattr(info, "visible_in") or info.visible_in(location, is_master_world)
    }
    hidden_vanilla, rule_order, generation_order = get_mod_vanilla_world_patches(
        visible_mod_settings, location, active_mod_ids,
    )
    # 海难关闭火山岛时同游戏一样隐藏火山岛的项；存档里已有的取值不显示但原样保留
    island_value = next((o.value for o in preset.overrides if o.key == VOLCANO_ISLAND_KEY), None)
    island_info = visible_mod_settings.get(VOLCANO_ISLAND_KEY)
    if island_value is None and island_info is not None:
        island_value = effective_initial_value(island_info, location, new_world)
    island_hidden = volcano_island_hidden_keys(location, visible_mod_settings, island_value)
    if island_hidden:
        hidden_vanilla = hidden_vanilla | island_hidden
        visible_mod_settings = {
            key: info for key, info in visible_mod_settings.items() if key not in island_hidden
        }
    rules_by_category: dict[str, list[WorldOverride | WorldDisplayOverride]] = {}
    generation_by_category: dict[str, list[WorldOverride | WorldDisplayOverride]] = {}
    seen_keys: set[str] = set()

    for override in preset.overrides:
        if override.key in hidden_vanilla:
            seen_keys.add(override.key)
            continue
        category, is_rule, name = get_setting_info(
            override.key, location, visible_mod_settings, is_master_world,
        )
        override.name = name or override.key
        seen_keys.add(override.key)
        if category != "other":
            target = rules_by_category if is_rule else generation_by_category
            target.setdefault(category, []).append(override)

    def add_builtin_defaults(is_rule: bool, target: dict) -> None:
        # 存档里没有的原版设置补"默认"占位，世界生成也显示完整的资源/刷新点列表（Mod 世界同样原版 + Mod 混排）
        for key, (category, name) in _get_settings(location, is_rule, is_master_world).items():
            if key in seen_keys or key in hidden_vanilla:
                continue
            target.setdefault(category, []).append(
                WorldDisplayOverride(key=key, name=localized_name(name), value="default")
            )

    add_builtin_defaults(True, rules_by_category)
    add_builtin_defaults(False, generation_by_category)

    for key, info in visible_mod_settings.items():
        if key in seen_keys:
            continue
        target = rules_by_category if info.is_rule else generation_by_category
        target.setdefault(info.category, []).append(
            WorldDisplayOverride(key=key, name=localized_name(info.name),
                                 value=effective_initial_value(info, location, new_world))
        )

    for items in rules_by_category.values():
        items.sort(key=lambda override: get_order_key(
            override.key, override.name, location, True, visible_mod_settings,
            rule_order,
        ))
    for items in generation_by_category.values():
        items.sort(key=lambda override: get_order_key(
            override.key, override.name, location, False, visible_mod_settings,
            generation_order,
        ))

    return WorldViewModel(
        location=location,
        rules_by_category=rules_by_category,
        generation_by_category=generation_by_category,
        rule_categories=get_categories(location, "rules", mod_categories or [], is_master_world),
        generation_categories=get_categories(location, "generation", mod_categories or [], is_master_world),
    )
