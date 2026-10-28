"""已核对的 Mod 世界类型及其创建界面行为。

官方创建界面只有 ``Master/forest`` 与 ``Caves/cave`` 两个槽位，Mod 可注册新 location 或在
``modservercreationmain.lua`` 中改写候选与默认值。只登记已从真实 Mod 源码核对过的行为，不执行 Mod Lua。
"""

import copy
from dataclasses import dataclass


FOREST_LOCATION = "forest"
CAVE_LOCATION = "cave"
PORKLAND_LOCATION = "porkland"
SHIPWRECKED_LOCATION = "shipwrecked"
VOLCANO_LOCATION = "volcanoworld"

CHERRY_FOREST_MOD_ID = "1289779251"
PORKLAND_MOD_ID = "3322803908"
IA_CORE_MOD_ID = "3435352667"
IA_SHIPWRECKED_MOD_ID = "1467214795"
PORKLAND_PATCH_MOD_ID = "3330056551"
# 三合一整合版（作者已下架，只为老存档保留）：一个 Mod 内含岛屿冒险核心/海难与云霄国度，只能专服多分片使用（建房界面勾选即报错）
THREE_WORLDS_MOD_ID = "3811652910"
# 三界同行 · 四模组独立补丁：取代整合版，四个原 Mod 分别启用，补丁只做兼容与扩展，不注册世界设置
THREE_WORLDS_PATCH_MOD_ID = "3826855460"
# Mod -> 已核对的硬依赖（取自各自 modinfo.lua 的 mod_dependencies）。游戏只在建房界面的模组页
# 自动勾选依赖，专服只按 modoverrides 加载，所以写配置时必须一并启用。
# 依赖列表写全（含间接依赖），补丁排在前面，界面补依赖时只问一次
MOD_HARD_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    THREE_WORLDS_PATCH_MOD_ID: (IA_CORE_MOD_ID, IA_SHIPWRECKED_MOD_ID, PORKLAND_MOD_ID, PORKLAND_PATCH_MOD_ID),
    IA_SHIPWRECKED_MOD_ID: (IA_CORE_MOD_ID,),
}
# 依赖提示里显示的 Mod 名称
DEPENDENCY_MOD_NAMES = {
    IA_CORE_MOD_ID: "岛屿冒险 - 核心",
    IA_SHIPWRECKED_MOD_ID: "岛屿冒险 - 海难",
    PORKLAND_MOD_ID: "云霄国度",
    PORKLAND_PATCH_MOD_ID: "云霄国度补丁",
    THREE_WORLDS_PATCH_MOD_ID: "三界同行 · 四模组独立补丁",
}
# 整合 Mod -> 它在世界类型上等同于哪些独立版（只用于能力判断，不写进 modoverrides）
BUNDLED_MOD_CAPABILITIES = {
    THREE_WORLDS_MOD_ID: frozenset({IA_CORE_MOD_ID, IA_SHIPWRECKED_MOD_ID, PORKLAND_MOD_ID}),
}
# 三合一整合版说明要求不能同时启用的原 Mod（ID 见包内 CREDITS.md），会重复加载同一份内容
THREE_WORLDS_CONFLICT_MODS = {
    mod_id: DEPENDENCY_MOD_NAMES[mod_id]
    for mod_id in (IA_CORE_MOD_ID, IA_SHIPWRECKED_MOD_ID, PORKLAND_MOD_ID, PORKLAND_PATCH_MOD_ID)
}
ALL_MOD_LOCATIONS = (
    FOREST_LOCATION, CAVE_LOCATION, SHIPWRECKED_LOCATION, VOLCANO_LOCATION, PORKLAND_LOCATION,
)
# 三合一整合版创意工坊说明与 README 要求的专服五分片：(目录名, location, 分片 ID, 额外 overrides)。
# 分片 ID 固定，"世界分组暂停"按它配对（森林+洞穴、海难+火山、猪镇）；Master 无 ID。
# 新补丁 twpatch/groups.lua 的分片 ID 与时钟表和整合版相同，沿用同一布局。
# 海难的 volcanoisland：IA postinit/map/levels/shipwrecked.lua 判断 ``~= "none"`` 才加火山岛，
# 缺 key 也会生成，有独立火山分片时必须显式写 "none"。
THREE_WORLDS_SHARD_LAYOUT = (
    ("Master", FOREST_LOCATION, None, {}),
    ("Shipwrecked", SHIPWRECKED_LOCATION, 2, {"volcanoisland": "none"}),
    ("Porkland", PORKLAND_LOCATION, 3, {}),
    ("Caves", CAVE_LOCATION, 4, {}),
    ("Volcano", VOLCANO_LOCATION, 5, {}),
)

MASTER_SHARD = "Master"
CAVES_SHARD = "Caves"


@dataclass(frozen=True)
class WorldLocationDefinition:
    """一个可写入 ``leveldataoverride.lua`` 的世界 location。"""

    location: str
    name_zh: str
    name_en: str
    description_zh: str
    description_en: str
    default_preset_id: str
    required_mod_ids: frozenset[str] = frozenset()

    def name(self, language: str = "zh") -> str:
        return self.name_en if language == "en" else self.name_zh

    def description(self, language: str = "zh") -> str:
        return self.description_en if language == "en" else self.description_zh


LOCATION_DEFINITIONS: dict[str, WorldLocationDefinition] = {
    FOREST_LOCATION: WorldLocationDefinition(
        FOREST_LOCATION,
        "森林",
        "Forest",
        "一个荒凉的森林。\n（推荐搭配洞穴或海难！）",
        "A desolate forest.\n(Recommended with Caves or Shipwrecked.)",
        "SURVIVAL_TOGETHER",
    ),
    CAVE_LOCATION: WorldLocationDefinition(
        CAVE_LOCATION,
        "洞穴",
        "Caves",
        "一个庞大的洞穴。\n（推荐搭配森林！）",
        "A vast cave.\n(Recommended with Forest.)",
        "DST_CAVE",
    ),
    SHIPWRECKED_LOCATION: WorldLocationDefinition(
        SHIPWRECKED_LOCATION,
        "海难",
        "Shipwrecked",
        "一个热带天堂？\n（推荐与火山或森林搭配！）",
        "A tropical paradise?\n(Recommended with Volcano or Forest.)",
        "SURVIVAL_SHIPWRECKED_CLASSIC",
        frozenset({IA_CORE_MOD_ID, IA_SHIPWRECKED_MOD_ID}),
    ),
    VOLCANO_LOCATION: WorldLocationDefinition(
        VOLCANO_LOCATION,
        "火山",
        "Volcano",
        "热带火山的内部。\n（推荐与海难搭配！）",
        "Inside a tropical volcano.\n(Recommended with Shipwrecked.)",
        "SURVIVAL_VOLCANO_CLASSIC",
        frozenset({IA_CORE_MOD_ID, IA_SHIPWRECKED_MOD_ID}),
    ),
    PORKLAND_LOCATION: WorldLocationDefinition(
        PORKLAND_LOCATION,
        "猪镇",
        "Porkland",
        "一片极其危险的丛林？",
        "An extremely dangerous jungle?",
        "PORKLAND_DEFAULT",
        frozenset({PORKLAND_MOD_ID}),
    ),
}


# 取自 Island Adventures 1467214795 源码（sw_locations.lua、levels/shipwrecked.lua、levels/volcano.lua）。
# 官方创建界面会把 AddLocation 默认值合并进 AddWorldGenLevel 再写 leveldataoverride.lua，不能只写空 overrides
_MOD_CREATION_LEVEL_DATA: dict[str, dict[str, object]] = {
    SHIPWRECKED_LOCATION: {
        "version": 4,
        "hideminimap": False,
        "min_playlist_position": 0,
        "max_playlist_position": 999,
        "override_level_string": False,
        "numrandom_set_pieces": 0,
        "random_set_pieces": [],
        "background_node_range": [0, 2],
        "required_prefabs": ["multiplayer_portal"],
        "overrides": {
            "start_location": "shipwrecked_default",
            "season_start": "default",
            "world_size": "default",
            "task_set": "shipwrecked",
            "layout_mode": "LinkNodesByKeys",
            "keep_disconnected_tiles": True,
            "has_ocean": True,
            "clocktype": "tropical",
            "poi": "never",
            "dst_boats": "none",
            "ia_boats": "always",
            "ia_drowning": "always",
        },
    },
    VOLCANO_LOCATION: {
        "version": 4,
        "hideminimap": False,
        "min_playlist_position": 0,
        "max_playlist_position": 999,
        "override_level_string": False,
        "numrandom_set_pieces": 0,
        "random_set_pieces": [],
        "background_node_range": [0, 0],
        "required_prefabs": ["multiplayer_portal"],
        "overrides": {
            "start_location": "volcano_default",
            "season_start": "default",
            "world_size": "small",
            "task_set": "volcano",
            "layout_mode": "LinkNodesByKeys",
            "keep_disconnected_tiles": True,
            "has_ocean": False,
            "clocktype": "tropical",
            "boons": "never",
            "poi": "never",
            "traps": "never",
            "prefabswaps_start": "classic",
            "grassgekkos": "never",
            "dst_boats": "none",
            "ia_boats": "always",
            "ia_drowning": "always",
        },
    },
    # 取自 Above the Clouds 3322803908 源码（三合一 modules/pl 下同名文件逐字节相同）：
    # 本体 levels.lua 的 GetDataForLevelID = MergeMapsDeep(locations/porkland.lua 的 AddLocation,
    # levels/porkland.lua 的 AddLevel)，关卡预设覆盖地点默认值（如 no_joining_islands=false）。
    # 只写 id 不写 overrides 时游戏不会按预设补全，世界生成报 "Must specify the task set for a level!"（实测）。
    PORKLAND_LOCATION: {
        "version": 4,
        "hideminimap": False,
        "min_playlist_position": 0,
        "max_playlist_position": 999,
        "override_level_string": False,
        "numrandom_set_pieces": 0,
        "random_set_pieces": [],
        "background_node_range": [0, 1],
        "required_prefabs": ["multiplayer_portal"],
        "overrides": {
            "start_location": "PorkLandStart",
            "task_set": "porkland",
            "season_start": "default",
            "world_size": "default",
            "layout_mode": "RestrictNodesByKey",
            "wormhole_prefab": "wormhole",
            "roads": "never",
            "keep_disconnected_tiles": True,
            "no_wormholes_to_disconnected_tiles": True,
            "no_joining_islands": False,
            "has_ocean": True,
            "prefabswaps_start": "classic",
            "spawnmode": "fixed",
            "specialevent": "none",
            "grassgekkos": "never",
            "branching": "least",
            "frograin": "never",
            "wildfires": "never",
            "deerclops": "never",
            "bearger": "never",
            "deciduousmonster": "never",
            "perd": "never",
            "penguins": "never",
            "hunt": "never",
            "pl_clocktype": "plateau",
        },
    },
}


def get_verified_creation_level_data(location: str) -> dict[str, object]:
    """返回已从真实 Mod 源码核对过的创建数据副本。"""
    return copy.deepcopy(_MOD_CREATION_LEVEL_DATA.get(location, {}))


@dataclass(frozen=True)
class WorldLocationProfile:
    """一组已启用 Mod 对两个官方分片槽位产生的最终约束。"""

    enabled_mod_ids: frozenset[str]
    effective_mod_ids: frozenset[str]
    master_locations: tuple[str, ...]
    caves_locations: tuple[str, ...]
    default_master: str
    default_caves: str
    warnings: tuple[str, ...] = ()

    def available_locations(self, shard: str) -> tuple[str, ...]:
        if shard == MASTER_SHARD:
            return self.master_locations
        if shard == CAVES_SHARD:
            return self.caves_locations
        raise ValueError(f"未知世界分片: {shard}")

    def default_location(self, shard: str) -> str:
        if shard == MASTER_SHARD:
            return self.default_master
        if shard == CAVES_SHARD:
            return self.default_caves
        raise ValueError(f"未知世界分片: {shard}")


def normalize_mod_ids(mod_ids) -> frozenset[str]:
    """统一 workshop id，兼容 ``workshop-123`` 与 ``123`` 两种形式。"""
    return frozenset(str(value).removeprefix("workshop-") for value in mod_ids)


def find_mod_key(mod_ids, mod_id: str) -> str | None:
    """在映射或 ID 集合中找到指定 Mod 的实际键名（``workshop-<id>`` 或纯数字），依赖联动须保留调用方的真实键名。"""
    target = str(mod_id).removeprefix("workshop-")
    return next(
        (str(value) for value in mod_ids
         if str(value).removeprefix("workshop-") == target),
        None,
    )


def location_capability_ids(mod_ids) -> frozenset[str]:
    """展开整合 Mod 后的能力集合，只用于判断世界类型可用性。"""
    normalized = set(normalize_mod_ids(mod_ids))
    for mod_id in tuple(normalized):
        normalized |= BUNDLED_MOD_CAPABILITIES.get(mod_id, frozenset())
    return frozenset(normalized)


def with_required_dependencies(mod_ids) -> frozenset[str]:
    """返回加入已验证硬依赖后的 Mod 集合，不修改调用方容器。"""
    normalized = set(normalize_mod_ids(mod_ids))
    for mod_id in tuple(normalized):
        normalized.update(MOD_HARD_DEPENDENCIES.get(mod_id, ()))
    return frozenset(normalized)


def dependent_mod_ids(mod_id: str) -> tuple[str, ...]:
    """硬依赖 ``mod_id`` 的 Mod，关闭它时这些 Mod 也要一起关闭。"""
    target = str(mod_id).removeprefix("workshop-")
    return tuple(child for child, deps in MOD_HARD_DEPENDENCIES.items() if target in deps)


def dependency_label(mod_ids, with_id: bool = True) -> str:
    """依赖提示里的 Mod 列表，如 ``岛屿冒险 - 核心 (3435352667)、云霄国度 (3322803908)``。"""
    names = []
    for mod_id in mod_ids:
        mod_id = str(mod_id).removeprefix("workshop-")
        name = DEPENDENCY_MOD_NAMES.get(mod_id, mod_id)
        names.append(f"{name} ({mod_id})" if with_id and name != mod_id else name)
    return "、".join(names)


def three_worlds_layout_available(mod_ids) -> bool:
    """启用了整合版或新补丁时，创建向导提供专服五分片布局。"""
    normalized = normalize_mod_ids(mod_ids)
    return THREE_WORLDS_MOD_ID in normalized or THREE_WORLDS_PATCH_MOD_ID in normalized




def resolve_world_location_profile(enabled_mod_ids) -> WorldLocationProfile:
    """按真实前端源码解析两个分片的候选 location 和新建默认值。"""
    selected = normalize_mod_ids(enabled_mod_ids)
    effective = with_required_dependencies(selected)
    warnings: list[str] = []

    if THREE_WORLDS_PATCH_MOD_ID in effective:
        # 新补丁接管岛屿冒险与云霄国度的兼容，并打开云霄国度的世界类型切换，两个槽位放开全部世界
        if THREE_WORLDS_MOD_ID in effective:
            warnings.append(
                "三界同行补丁已取代三合一整合版，二者不能同时启用，请关闭三合一整合版。"
            )
        return WorldLocationProfile(
            selected,
            effective,
            ALL_MOD_LOCATIONS,
            ALL_MOD_LOCATIONS,
            FOREST_LOCATION,
            CAVE_LOCATION,
            tuple(warnings),
        )

    if THREE_WORLDS_MOD_ID in effective:
        # 整合版自带两套扩展的兼容层，面向专服五分片（Master 森林 + 海难/猪镇/洞穴/火山）；
        # 不走建房界面的选世界逻辑，两个固定槽位都放开全部世界，默认仍是森林+洞穴
        conflicts = [name for mod_id, name in THREE_WORLDS_CONFLICT_MODS.items() if mod_id in effective]
        if conflicts:
            warnings.append(
                "三合一整合版已包含这些内容，不能同时启用：" + "、".join(conflicts) + "，请先关闭它们。"
            )
        return WorldLocationProfile(
            selected,
            effective,
            ALL_MOD_LOCATIONS,
            ALL_MOD_LOCATIONS,
            FOREST_LOCATION,
            CAVE_LOCATION,
            tuple(warnings),
        )

    if PORKLAND_MOD_ID in effective and IA_CORE_MOD_ID in effective:
        warnings.append(
            "猪镇与岛屿冒险都会修改选择世界界面；该组合的加载结果需要真机验证。"
        )

    if IA_SHIPWRECKED_MOD_ID in effective:
        island_locations = (
            FOREST_LOCATION,
            CAVE_LOCATION,
            SHIPWRECKED_LOCATION,
            VOLCANO_LOCATION,
        )
        return WorldLocationProfile(
            selected,
            effective,
            island_locations,
            island_locations,
            SHIPWRECKED_LOCATION,
            VOLCANO_LOCATION,
            tuple(warnings),
        )

    if IA_CORE_MOD_ID in effective:
        core_locations = (FOREST_LOCATION, CAVE_LOCATION)
        return WorldLocationProfile(
            selected,
            effective,
            core_locations,
            core_locations,
            FOREST_LOCATION,
            CAVE_LOCATION,
            tuple(warnings),
        )

    if PORKLAND_MOD_ID in effective:
        return WorldLocationProfile(
            selected,
            effective,
            (PORKLAND_LOCATION,),
            (CAVE_LOCATION,),
            PORKLAND_LOCATION,
            CAVE_LOCATION,
        )

    return WorldLocationProfile(
        selected,
        effective,
        (FOREST_LOCATION,),
        (CAVE_LOCATION,),
        FOREST_LOCATION,
        CAVE_LOCATION,
    )


def get_location_definition(location: str) -> WorldLocationDefinition:
    try:
        return LOCATION_DEFINITIONS[location]
    except KeyError as exc:
        raise ValueError(f"不支持的世界类型: {location}") from exc


def location_requirements_met(location: str, enabled_mod_ids) -> bool:
    definition = get_location_definition(location)
    return definition.required_mod_ids.issubset(location_capability_ids(enabled_mod_ids))
