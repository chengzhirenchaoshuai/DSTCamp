"""世界设置目录解析：原版目录只覆盖 forest/cave，Mod 世界（如 porkland）在这里组合原版基础与 Mod 的增删规则。"""

from typing import Any


# Above the Clouds（3322803908）猪镇显示的原版 key 白名单，来自 modcustomizeitems.lua：
# change_items 扩展进 porkland 的 12 个原版 key，加上 world=nil 的全局项中未被 delete_items 删除的
# global/survivors 项。猪镇世界 = 这些原版 key + Mod 新增 key（PORKLAND_SETTINGS）。
PORKLAND_MOD_ID = "3322803908"
PORKLAND_VANILLA_KEYS = frozenset({
    # change_items
    "rock", "sapling", "grass", "flowers", "reeds", "mushroom",  # worldgen resources
    "task_set", "world_size", "boons",  # worldgen misc
    "butterfly",  # world_settings animals
    "lightning", "weather",  # world_settings misc
    # world=nil 且未被 delete_items 删除的全局项
    "day", "ghostenabled", "portalresurection", "ghostsanitydrain",
    "resettime", "krampus",  # global
    "extrastartingitems", "spawnprotection", "dropeverythingondespawn",
    "healthpenalty", "lessdamagetaken", "hunger", "darkness",
    "shadowcreatures",  # survivors
})

# 1467214795 会把这些原版 OPTIONS 的 ``world`` 列表扩展到新 location。
# 两个集合逐项来自该 Mod 的 vanilla_options_in_islands / in_volcano。
IA_SHIPWRECKED_VANILLA_KEYS = frozenset({
    "task_set", "start_location", "world_size", "grass", "sapling", "flowers",
    "touchstone", "boons", "balatro", "terrariumchest", "junkyard", "marshbush", "reeds",
    "flint", "rock", "berrybush", "mushroom", "bees", "spiders", "merm",
    "angrybees", "tallbirds", "liefs", "beequeen", "fruitfly", "klaus",
    "spiderqueen", "daywalker2", "eyeofterror", "lureplants", "wasps", "merms",
    "daywalker",
    "spiders_setting", "spider_warriors", "butterfly", "birds", "bees_setting",
    "pigs_setting", "bunnymen_setting", "grassgekkos", "regrowth",
    "flowers_regrowth", "reeds_regrowth", "lightning", "weather",
    "moles_setting", "prefabswaps_start", "twiggytrees_regrowth", "fishschools",
})
IA_VOLCANO_VANILLA_KEYS = frozenset({
    "task_set", "start_location", "fruitfly", "spiderqueen", "daywalker", "liefs", "merms",
    "spiders_setting", "spider_warriors", "bees_setting", "pigs_setting",
    "bunnymen_setting", "lightning", "weather", "regrowth", "moles_setting",
})


# 本体 customize.lua 里 world=nil 的条目，全部带 master_controlled：不限世界类型，只在主世界（第一个
# 世界）显示。GetOptionsFromGroup 的规则是"world 为空或含当前地点"且"是主世界或不是 master_controlled"，
# 所以 Mod 允许洞穴、火山等做主世界时，这些项跟着主世界走，从世界一律不显示。
MASTER_ONLY_VANILLA_KEYS = frozenset({
    # 世界规则 global
    "specialevent", "autumn", "winter", "spring", "summer", "day", "spawnmode", "ghostenabled",
    "portalresurection", "ghostsanitydrain", "resettime", "beefaloheat", "krampus",
    # 世界规则 events
    "crow_carnival", "hallowed_nights", "winters_feast", "year_of_the_gobbler", "year_of_the_varg",
    "year_of_the_pig", "year_of_the_carrat", "year_of_the_beefalo", "year_of_the_catcoon",
    "year_of_the_bunnyman", "year_of_the_dragonfly", "year_of_the_snake", "year_of_the_knight",
    # 世界规则 survivors
    "extrastartingitems", "seasonalstartingitems", "spawnprotection", "dropeverythingondespawn",
    "healthpenalty", "lessdamagetaken", "temperaturedamage", "hunger", "darkness", "shadowcreatures",
    "brightmarecreatures",
    # 世界规则 resources
    "basicresource_regrowth",
    # 世界生成 global
    "season_start",
})


def _vanilla_sources(is_rule: bool):
    from dstools.features.world import categories

    return (
        (categories.FOREST_RULES_DICT, categories.CAVE_ALL_RULES_DICT)
        if is_rule else
        (categories.FOREST_GEN_DICT, categories.CAVE_ALL_GEN_DICT)
    )


def _resolve_whitelisted(allowed_keys, is_rule: bool, is_master_world: bool) -> dict[str, tuple[str, dict[str, str]]]:
    """从森林、洞穴两张原版表里取白名单内的项；仅主世界的项只在主世界保留。"""
    resolved: dict[str, tuple[str, dict[str, str]]] = {}
    for source in _vanilla_sources(is_rule):
        for key, item in source.items():
            if key in allowed_keys and (is_master_world or key not in MASTER_ONLY_VANILLA_KEYS):
                resolved[key] = item
    return resolved


def resolve_vanilla_settings(
    location: str, is_rule: bool, is_master_world: bool = True,
) -> dict[str, tuple[str, dict[str, str]]]:
    """返回某地点实际继承的原版设置，不包含任何 Mod 新增项。

    ``is_master_world`` 为是否主世界：MASTER_ONLY_VANILLA_KEYS 只在主世界显示，与世界类型无关。"""
    # 延迟导入，避免 categories.py 的查询函数与本模块互相导入。
    from dstools.features.world import categories

    if location in ("forest", "cave"):
        # 森林表本身就含全部仅主世界的项，洞穴表一个都不含
        if location == "forest":
            own = categories.FOREST_RULES_DICT if is_rule else categories.FOREST_GEN_DICT
        else:
            own = categories.CAVE_ALL_RULES_DICT if is_rule else categories.CAVE_ALL_GEN_DICT
        allowed = set(own) | (MASTER_ONLY_VANILLA_KEYS if is_master_world else set())
        return _resolve_whitelisted(allowed, is_rule, is_master_world)
    # 猪镇白名单本身已含未被 delete_items 删掉的仅主世界项；海难、火山的白名单只有 Mod 扩展进来的项，
    # 主世界时再补上全部仅主世界的项
    if location == "porkland":
        return _resolve_whitelisted(PORKLAND_VANILLA_KEYS, is_rule, is_master_world)
    if location == "shipwrecked":
        return _resolve_whitelisted(IA_SHIPWRECKED_VANILLA_KEYS | MASTER_ONLY_VANILLA_KEYS, is_rule, is_master_world)
    if location == "volcanoworld":
        return _resolve_whitelisted(IA_VOLCANO_VANILLA_KEYS | MASTER_ONLY_VANILLA_KEYS, is_rule, is_master_world)
    return {}


def resolve_vanilla_categories(
    location: str, setting_type: str, is_master_world: bool = True,
) -> list[tuple[str, dict[str, str]]]:
    """返回某地点的原版分类；Mod 分类由调用方另行追加。空分类由调用方按实际条目过滤。"""
    from dstools.features.world import categories

    if location == "forest":
        return list(categories.SURFACE_RULES if setting_type == "rules" else categories.SURFACE_GEN)
    if location == "cave" and not is_master_world:
        return list(categories.CAVE_RULES if setting_type == "rules" else categories.CAVE_GEN)
    if location in {"cave", "porkland", "shipwrecked", "volcanoworld"}:
        settings = resolve_vanilla_settings(location, setting_type == "rules", is_master_world)
        used_categories = {category for category, _names in settings.values()}
        # Mod 设置映射到的官方分类也要加入分类列表，否则如海难的 global 组会因分类缺失整组不显示。
        # 规则与生成用两套分类 key（GROUP_TO_CATEGORY / _GEN）；空分类之后会被过滤
        from dstools.features.world.mod_settings import GROUP_TO_CATEGORY, GROUP_TO_CATEGORY_GEN
        mapping = GROUP_TO_CATEGORY if setting_type == "rules" else GROUP_TO_CATEGORY_GEN
        used_categories |= set(mapping.values())
        source = (
            list(categories.SURFACE_RULES) + list(categories.CAVE_RULES)
            if setting_type == "rules" else
            list(categories.SURFACE_GEN) + list(categories.CAVE_GEN)
        )
        result = []
        seen = set()
        for item in source:
            if item[0] in used_categories and item[0] not in seen:
                result.append(item)
                seen.add(item[0])
        return result
    return []


def resolve_setting_info(
    key: str, location: str, mod_settings: dict[str, Any] | None = None, is_master_world: bool = True,
) -> tuple[str, bool, str]:
    """按“原版基础层 → 当前启用 Mod 覆盖层”解析单个设置。"""
    from dstools.features.world.categories import localized_name

    for is_rule in (True, False):
        settings = resolve_vanilla_settings(location, is_rule, is_master_world)
        if key in settings:
            category, names = settings[key]
            return category, is_rule, localized_name(names)
    if mod_settings and key in mod_settings:
        info = mod_settings[key]
        return info.category, info.is_rule, localized_name(info.name)
    return "other", False, key
