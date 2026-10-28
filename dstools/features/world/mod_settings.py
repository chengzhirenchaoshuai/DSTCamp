"""登记从真实 Mod 源码核验过的世界设置。

信任来源只有 ``AddCustomizeItem``、``worldsettings_overrides.lua`` 和图集 XML，不猜测
任意 Mod；只展示实际注册过的 ``name``（孤立的 ``WSO.Pre`` 不算）；取值以 UI 的
``desc`` 为准，不抄 ``tuning_vars``。未登记 key 原样保留在 Lua 中，不在界面伪造。
"""

from dataclasses import dataclass, replace

from dstools.features.world.location_profiles import (
    CAVE_LOCATION,
    FOREST_LOCATION,
    PORKLAND_LOCATION,
    SHIPWRECKED_LOCATION,
    VOLCANO_LOCATION,
)


# AddCustomizeItem 的 group（游戏内部组名）→ DSTCamp 分类 key；登记了 group 的设置与原版混排
GROUP_TO_CATEGORY = {
    "global": "global",             # 全局
    "porkland_settings_global": "porkland_global",  # 猪镇全局（云霄国度 mod 单独建的组）
    "events": "events",             # 活动
    "survivors": "survivor",        # 冒险家
    "misc": "world",                # 世界
    "resources": "regrowth",        # 资源再生
    "portal_resources": "portal_resources",  # 非自然传送门资源
    "animals": "creatures",         # 生物
    "monsters": "hostile_creatures",  # 敌对生物
    "giants": "bosses",             # 巨兽
    "lunar_mutations": "lunar",     # 月亮变异
}

# 世界生成的 group → 分类 key。同名 group 在世界生成里指向不同分类（如 animals 是
# "生物以及刷新点"），与 GROUP_TO_CATEGORY 分开（已对照本体 customize.lua）。
GROUP_TO_CATEGORY_GEN = {
    "global": "global",               # 全局
    "misc": "world",                  # 世界
    "resources": "resources",         # 资源
    "animals": "creatures_spawners",  # 生物以及刷新点
    "monsters": "hostile_spawners",   # 敌对生物以及刷新点
}

# Mod 用 AddCustomizeGroup 自建的组名 -> 分类标题（取自该组的 text）。组名在游戏里全局唯一。
MOD_CUSTOM_GROUP_NAMES: dict[str, dict] = {}


@dataclass
class ModWorldSetting:
    """一条 mod 贡献的世界设置/生成条目。"""
    key: str
    is_rule: bool      # True = 世界设置(LEVELCATEGORY.SETTINGS，可编辑)
                        # False = 世界生成(LEVELCATEGORY.WORLDGEN，只读，
                        # 跟 DSTCamp 对待原版生成类设置一致)
    name: dict          # {"zh": "...", "en": "..."}
    values: list | None  # 合法取值(按 tuning_vars 表里出现的顺序)；
                         # None 表示只读展示，不需要取值列表
    mod_id: str          # 贡献这条设置的 workshop id（不带前缀）
    icon_element: str | None = None  # mod 图标图集里对应的 Element name
                                      # （含 ".tex" 后缀，与图集 XML 一致），None 表示没有
    # 存档里没有该 key 时的占位值。坑：不是所有 Mod 都有 "default" 档，
    # 如 Island Adventures 的 poison/dst_boats 等只有 "none"/"always"
    initial_value: str = "default"
    # 对应 AddCustomizeItem() 的 ``world`` 字段；None 表示源码没有限制。
    locations: frozenset[str] | None = None
    # 对应官方 ``master_controlled``，只在 Master 分片显示和编辑。
    master_controlled: bool = False
    # AddCustomizeItem 的 group；None 表示按 Mod 名单独分类，Mod 用 AddCustomizeGroup 自建的组
    # 登记在 MOD_CUSTOM_GROUP_NAMES（见 get_mod_categories）
    group: str | None = None
    # AddCustomizeItem 的 order（分类内排序键）；None 按显示名排序。
    # 用 float：樱花林等用 3.06 这类小数插在整数之间
    order: float | None = None
    # 本条图集 (xml, tex)，相对 Mod 根目录；None 时用 MOD_ICON_ATLAS 中该 Mod 的统一图集
    icon_atlas: tuple[str, str] | None = None
    # 图标取自本体图集时，借用 icons/world 下该原版 key 的图标
    vanilla_icon_key: str | None = None
    # 取值 -> {"zh", "en"}，Mod desc 文案与全局取值文案不同时按条覆盖
    value_labels: dict[str, dict] | None = None

    @property
    def category(self) -> str:
        if self.group is not None:
            if self.group in MOD_CUSTOM_GROUP_NAMES:
                return f"mod_group_{self.group}"
            mapping = GROUP_TO_CATEGORY if self.is_rule else GROUP_TO_CATEGORY_GEN
            return mapping.get(self.group, f"mod_{self.mod_id}")
        return f"mod_{self.mod_id}"

    def visible_in(self, location: str, is_master_world: bool = True) -> bool:
        if self.locations is not None and location not in self.locations:
            return False
        return is_master_world or not self.master_controlled


# workshop-1289779251 == Cherry Forest（新版樱花林），1.6.107
# 来源（content/322330/1289779251/）：
#   - key/category：scripts/map/cherry_customizations.lua 的 customizations 表
#     （init/init_worldgen.lua 循环调用 AddCustomizeItem）；
#   - 取值：同文件 WSO.Pre.<key> 的 tuning_vars 键。"default" 常以注释形式出现，
#     表示沿用 Mod 默认 TUNING，仍是真实可选档；
#   - 中文名：scripts/cherry_strings/ch/strings.lua 的 STRINGS.UI.CUSTOMIZATIONSCREEN；
#   - 图标：images/worldgen_cherry.xml，命名规则 worldsettings_/worldgen_ + name。
_CHERRY_FOREST_ID = "1289779251"

CHERRY_FOREST_SETTINGS: dict[str, ModWorldSetting] = {
    # ── 世界设置（可编辑）──
    "cherry_bugseason": ModWorldSetting(
        key="cherry_bugseason", is_rule=True, mod_id=_CHERRY_FOREST_ID, group="events",
        name={"zh": "甲虫风暴", "en": "Beetlegale"},
        values=["default", "enabled"], order=3.06,
        icon_element="worldsettings_cherry_bugseason.tex"),
    "cherrift": ModWorldSetting(
        key="cherrift", is_rule=True, mod_id=_CHERRY_FOREST_ID, group="events",
        name={"zh": "樱花裂隙", "en": "Cherrifts"},
        values=["default", "enabled"], order=3.07,
        icon_element="worldsettings_cherrift.tex"),
    "petalwind": ModWorldSetting(
        key="petalwind", is_rule=True, mod_id=_CHERRY_FOREST_ID, group="misc",
        name={"zh": "樱花雨", "en": "Petal Wind"},
        values=["never", "rare", "default", "often", "always"],
        icon_element="worldsettings_petalwind.tex"),
    "cherrylings": ModWorldSetting(
        key="cherrylings", is_rule=True, mod_id=_CHERRY_FOREST_ID, group="animals",
        name={"zh": "樱花小精灵", "en": "Cherrylings"},
        values=["never", "rare", "default", "often", "always"],
        icon_element="worldsettings_cherrylings.tex"),
    "cherry_dragonflies": ModWorldSetting(
        key="cherry_dragonflies", is_rule=True, mod_id=_CHERRY_FOREST_ID, group="animals",
        name={"zh": "蜻蜓", "en": "Butterdragons"},
        values=["never", "rare", "default", "often", "always"],
        icon_element="worldsettings_cherry_dragonflies.tex"),
    "cherry_watchers": ModWorldSetting(
        key="cherry_watchers", is_rule=True, mod_id=_CHERRY_FOREST_ID, group="animals",
        name={"zh": "守护者", "en": "Watchers"},
        values=["never", "rare", "default", "often", "always"],
        icon_element="worldsettings_cherry_watchers.tex"),

    # ── 世界生成（只读，跟原版生成类设置一致）──
    "cherry_trees": ModWorldSetting(
        key="cherry_trees", is_rule=False, mod_id=_CHERRY_FOREST_ID, group="resources",
        name={"zh": "樱花树", "en": "Cherry Trees"}, values=None,
        icon_element="worldgen_cherry_trees.tex"),
    "sapling_cherry": ModWorldSetting(
        key="sapling_cherry", is_rule=False, mod_id=_CHERRY_FOREST_ID, group="resources",
        name={"zh": "樱花树枝", "en": "Blooming Saplings"}, values=None,
        icon_element="worldgen_sapling_cherry.tex"),
    "grass_cherry": ModWorldSetting(
        key="grass_cherry", is_rule=False, mod_id=_CHERRY_FOREST_ID, group="resources",
        name={"zh": "樱花草丛", "en": "Blooming Grass"}, values=None,
        icon_element="worldgen_grass_cherry.tex"),
    "foreststatue_rock": ModWorldSetting(
        key="foreststatue_rock", is_rule=False, mod_id=_CHERRY_FOREST_ID, group="resources",
        name={"zh": "石化树根", "en": "Stone Roots"}, values=None,
        icon_element="worldgen_foreststatue_rock.tex"),
    "cherrytomato": ModWorldSetting(
        key="cherrytomato", is_rule=False, mod_id=_CHERRY_FOREST_ID, group="resources",
        name={"zh": "番茄植株", "en": "Cherry Tomatoes"}, values=None,
        icon_element="worldgen_cherrytomato.tex"),
    "bloomshrooms": ModWorldSetting(
        key="bloomshrooms", is_rule=False, mod_id=_CHERRY_FOREST_ID, group="resources",
        name={"zh": "繁花菇", "en": "Bloomshrooms"}, values=None,
        icon_element="worldgen_bloomshrooms.tex"),
    "goosebushes": ModWorldSetting(
        key="goosebushes", is_rule=False, mod_id=_CHERRY_FOREST_ID, group="resources",
        name={"zh": "鹅莓果丛", "en": "Gooseberry Bushes"}, values=None,
        icon_element="worldgen_goosebushes.tex"),
    "honeyvines": ModWorldSetting(
        key="honeyvines", is_rule=False, mod_id=_CHERRY_FOREST_ID, group="resources",
        name={"zh": "翠蜂巢", "en": "Ivyscus Hives"}, values=None,
        icon_element="worldgen_honeyvines.tex"),
    "rosebushes": ModWorldSetting(
        key="rosebushes", is_rule=False, mod_id=_CHERRY_FOREST_ID, group="resources",
        name={"zh": "野玫瑰丛", "en": "Wild Rose Bushes"}, values=None,
        icon_element="worldgen_rosebushes.tex"),
    "watchernests": ModWorldSetting(
        key="watchernests", is_rule=False, mod_id=_CHERRY_FOREST_ID, group="monsters",
        name={"zh": "守护者巢穴", "en": "Watcher Nests"}, values=None,
        icon_element="worldgen_watchernests.tex"),
}

# workshop-3435352667 == Island Adventures - Core（岛屿冒险 - 核心）
# workshop-1467214795 == Island Adventures - Shipwrecked（岛屿冒险 - 海难，硬依赖 Core）
# 来源（content/322330/<id>/）：
#   - key/category：各自 modservercreationmain.lua 的 ia_settings_customize_table（可编辑）
#     与 ia_worldgen_customize_table（只读）。注册入口文件名因 Mod 而异，不能假设；
#   - 取值：以 AddCustomizeItem 的 desc 为准（未写则继承组默认 desc，animals/monsters/
#     giants 为 frequency_descriptions 5 档），tuning_vars 的 few/many 或 9 档 MULTIPLY
#     只是实现细节；
#   - 中文名：Core 的 languages/ia_sc.po（简体）msgctxt STRINGS.UI.CUSTOMIZATIONSCREEN.<KEY>，
#     Shipwrecked 共用这份翻译；
#   - 图标：Core 的 images/hud/customization_core.xml，Shipwrecked 的 customization_shipwrecked.xml。
# 注意：mosquito 注册为单数，但 Post 函数名是复数 mosquitos，属 Mod 自身 bug（调整可能不生效）。
_IA_CORE_ID = "3435352667"
_IA_SHIPWRECKED_ID = "1467214795"

IA_CORE_SETTINGS: dict[str, ModWorldSetting] = {
    # 这 4 个 desc 为 enableddisabled_descriptions，合法值只有 "none"/"always"，没有 "default"
    "poison": ModWorldSetting(
        key="poison", is_rule=True, mod_id=_IA_CORE_ID, group="global",
        name={"zh": "中毒", "en": "Poison"},
        values=["none", "always"], initial_value="always", order=25,
        icon_element="poison.tex"),
    "dst_boats": ModWorldSetting(
        key="dst_boats", is_rule=True, mod_id=_IA_CORE_ID, group="misc",
        name={"zh": "DST船", "en": "DST Boats"},
        values=["none", "always"], initial_value="always", order=4,
        icon_element="cookieboats.tex"),
    "ia_boats": ModWorldSetting(
        key="ia_boats", is_rule=True, mod_id=_IA_CORE_ID, group="misc",
        name={"zh": "IA船", "en": "IA Boats"},
        values=["none", "always"], initial_value="none", order=5,
        icon_element="smallboats.tex"),
    "ia_drowning": ModWorldSetting(
        key="ia_drowning", is_rule=True, mod_id=_IA_CORE_ID, group="misc",
        name={"zh": "溺死", "en": "Deadly Drowning"},
        values=["none", "always"], initial_value="none", order=6,
        icon_element="deadlydrowning.tex"),
    "primeape_setting": ModWorldSetting(
        key="primeape_setting", is_rule=True, mod_id=_IA_CORE_ID, group="animals",
        name={"zh": "猿猴", "en": "Prime Apes"},
        values=["never", "rare", "default", "often", "always"],
        icon_element="monkeys.tex"),
    "snake_setting": ModWorldSetting(
        key="snake_setting", is_rule=True, mod_id=_IA_CORE_ID, group="monsters",
        name={"zh": "蛇", "en": "Snakes"},
        values=["never", "rare", "default", "often", "always"],
        icon_element="snakes.tex"),
}

# 频率类 5 档：desc 为 frequency_descriptions 或继承 animals/monsters/giants 组默认 desc 的，
# UI 可选档就是这 5 个，WSO 内部用的 few/many 或 MULTIPLY 表只是实现细节
_IA_NRDOA = ["never", "rare", "default", "often", "always"]
# 再生速度类(desc = regrowth_descriptions)的 6 档取值。
_IA_REGROWTH = ["never", "veryslow", "slow", "default", "fast", "veryfast"]
# 季节长度类（mild/hurricane/monsoon/dry）：SEASON_FRIENDLY/HARSH_LENGTHS 的 key 加特殊值 "random"
_IA_SEASON_LENGTH = ["noseason", "veryshortseason", "shortseason", "default",
                     "longseason", "verylongseason", "random"]

IA_SHIPWRECKED_SETTINGS: dict[str, ModWorldSetting] = {
    # ── 世界设置（可编辑）──
    "mild": ModWorldSetting(key="mild", is_rule=True, mod_id=_IA_SHIPWRECKED_ID, group="global",
        name={"zh": "温和季", "en": "Mild"}, values=_IA_SEASON_LENGTH, order=20,
        icon_element="mild.tex"),
    "hurricane": ModWorldSetting(key="hurricane", is_rule=True, mod_id=_IA_SHIPWRECKED_ID, group="global",
        name={"zh": "飓风季", "en": "Hurricane"}, values=_IA_SEASON_LENGTH, order=21,
        icon_element="hurricane.tex"),
    "monsoon": ModWorldSetting(key="monsoon", is_rule=True, mod_id=_IA_SHIPWRECKED_ID, group="global",
        name={"zh": "雨季", "en": "Monsoon"}, values=_IA_SEASON_LENGTH, order=23,
        icon_element="monsoon.tex"),
    "dry": ModWorldSetting(key="dry", is_rule=True, mod_id=_IA_SHIPWRECKED_ID, group="global",
        name={"zh": "旱季", "en": "Dry"}, values=_IA_SEASON_LENGTH, order=24,
        icon_element="dry.tex"),
    "floods": ModWorldSetting(key="floods", is_rule=True, mod_id=_IA_SHIPWRECKED_ID, group="misc",
        name={"zh": "洪水", "en": "Floods"}, values=_IA_NRDOA,
        icon_element="floods.tex"),
    "tides": ModWorldSetting(key="tides", is_rule=True, mod_id=_IA_SHIPWRECKED_ID, group="misc",
        name={"zh": "潮汐", "en": "Tides"}, values=_IA_NRDOA,
        icon_element="tides.tex"),
    "dragoonegg": ModWorldSetting(key="dragoonegg", is_rule=True, mod_id=_IA_SHIPWRECKED_ID, group="misc",
        name={"zh": "火山爆发", "en": "Volcanic Eruptions"}, values=_IA_NRDOA,
        icon_element="dragooneggs.tex"),
    "oceanwaves": ModWorldSetting(key="oceanwaves", is_rule=True, mod_id=_IA_SHIPWRECKED_ID, group="misc",
        name={"zh": "海浪", "en": "Waves"}, values=_IA_NRDOA,
        icon_element="waves.tex"),
    "whalehunt": ModWorldSetting(key="whalehunt", is_rule=True, mod_id=_IA_SHIPWRECKED_ID, group="misc",
        name={"zh": "捕鲸", "en": "Whaling"}, values=_IA_NRDOA,
        icon_element="whales.tex"),
    "alternatewhalehunt": ModWorldSetting(key="alternatewhalehunt", is_rule=True, mod_id=_IA_SHIPWRECKED_ID, group="misc",
        name={"zh": "追鲸惊喜", "en": "Whaling Surprises"}, values=_IA_NRDOA,
        icon_element="alternatewhaling.tex"),
    "waterencounters": ModWorldSetting(key="waterencounters", is_rule=True, mod_id=_IA_SHIPWRECKED_ID, group="misc",
        name={"zh": "随机海洋奇遇", "en": "Random Ocean Encounters"}, values=_IA_NRDOA,
        icon_element="waterencounters.tex"),
    "crocodog": ModWorldSetting(key="crocodog", is_rule=True, mod_id=_IA_SHIPWRECKED_ID, group="misc",
        name={"zh": "鳄狗袭击", "en": "Crocodog Attacks"}, values=_IA_NRDOA, order=1,
        icon_element="crocodogattacks.tex"),
    "yellowcrocodog": ModWorldSetting(key="yellowcrocodog", is_rule=True, mod_id=_IA_SHIPWRECKED_ID, group="misc",
        name={"zh": "黄色鳄狗袭击", "en": "Poison Crocodog Waves"}, values=["never", "default"], order=2,
        icon_element="drycrocodogs.tex"),
    "bluecrocodog": ModWorldSetting(key="bluecrocodog", is_rule=True, mod_id=_IA_SHIPWRECKED_ID, group="misc",
        name={"zh": "蓝色鳄狗袭击", "en": "Water Crocodog Waves"}, values=["never", "default"], order=3,
        icon_element="monsooncrocodogs.tex"),
    "sweet_potato_regrowth": ModWorldSetting(key="sweet_potato_regrowth", is_rule=True, mod_id=_IA_SHIPWRECKED_ID, group="resources",
        name={"zh": "甘薯", "en": "Sweet Potatoes"}, values=_IA_REGROWTH,
        icon_element="sweetpotatos.tex"),
    "palmtree_regrowth": ModWorldSetting(key="palmtree_regrowth", is_rule=True, mod_id=_IA_SHIPWRECKED_ID, group="resources",
        name={"zh": "椰树", "en": "Palm Trees"}, values=_IA_REGROWTH,
        icon_element="trees.tex"),
    "jungletree_regrowth": ModWorldSetting(key="jungletree_regrowth", is_rule=True, mod_id=_IA_SHIPWRECKED_ID, group="resources",
        name={"zh": "丛林树", "en": "Jungle Trees"}, values=_IA_REGROWTH,
        icon_element="jungletree.tex"),
    "mangrovetree_regrowth": ModWorldSetting(key="mangrovetree_regrowth", is_rule=True, mod_id=_IA_SHIPWRECKED_ID, group="resources",
        name={"zh": "红树林", "en": "Mangroves"}, values=_IA_REGROWTH,
        icon_element="mangrovetree.tex"),
    "coral_brain_rock_regrowth": ModWorldSetting(key="coral_brain_rock_regrowth", is_rule=True, mod_id=_IA_SHIPWRECKED_ID, group="resources",
        name={"zh": "智慧树", "en": "Brainy Sprouts"}, values=_IA_REGROWTH,
        icon_element="braincoral.tex"),
    "seashell_regrowth": ModWorldSetting(key="seashell_regrowth", is_rule=True, mod_id=_IA_SHIPWRECKED_ID, group="resources",
        name={"zh": "贝壳", "en": "Seashells"}, values=_IA_REGROWTH,
        icon_element="seashell.tex"),
    "sandhill_regrowth": ModWorldSetting(key="sandhill_regrowth", is_rule=True, mod_id=_IA_SHIPWRECKED_ID, group="resources",
        name={"zh": "沙堆", "en": "Sandy Piles"}, values=_IA_REGROWTH,
        icon_element="sand.tex"),
    "rock_obsidian_regrowth": ModWorldSetting(key="rock_obsidian_regrowth", is_rule=True, mod_id=_IA_SHIPWRECKED_ID, group="resources",
        name={"zh": "黑曜石矿", "en": "Obsidian Boulders"}, values=_IA_REGROWTH,
        icon_element="rock_obsidian.tex"),
    "rock_charcoal_regrowth": ModWorldSetting(key="rock_charcoal_regrowth", is_rule=True, mod_id=_IA_SHIPWRECKED_ID, group="resources",
        name={"zh": "木炭矿", "en": "Charcoal Boulders"}, values=_IA_REGROWTH,
        icon_element="rock_charcoal.tex"),
    "volcano_shrub_regrowth": ModWorldSetting(key="volcano_shrub_regrowth", is_rule=True, mod_id=_IA_SHIPWRECKED_ID, group="resources",
        name={"zh": "灰烬树", "en": "Ash Trees"}, values=_IA_REGROWTH,
        icon_element="volcano_shrub.tex"),
    "magmarock_regrowth": ModWorldSetting(key="magmarock_regrowth", is_rule=True, mod_id=_IA_SHIPWRECKED_ID, group="resources",
        name={"zh": "熔岩矿堆", "en": "Magma Piles"}, values=_IA_REGROWTH,
        icon_element="magmarocks.tex"),
    "bioluminescence_regrowth": ModWorldSetting(key="bioluminescence_regrowth", is_rule=True, mod_id=_IA_SHIPWRECKED_ID, group="resources",
        name={"zh": "荧光生物", "en": "Bioluminescence"}, values=_IA_REGROWTH,
        icon_element="bioluminescence.tex"),
    "crab_setting": ModWorldSetting(key="crab_setting", is_rule=True, mod_id=_IA_SHIPWRECKED_ID, group="animals",
        name={"zh": "兔蟹", "en": "Crabbits"}, values=_IA_NRDOA,
        icon_element="crabbits.tex"),
    "wildbores_setting": ModWorldSetting(key="wildbores_setting", is_rule=True, mod_id=_IA_SHIPWRECKED_ID, group="animals",
        name={"zh": "野猪", "en": "Wildbores"}, values=_IA_NRDOA,
        icon_element="wildbores.tex"),
    "ballphin_setting": ModWorldSetting(key="ballphin_setting", is_rule=True, mod_id=_IA_SHIPWRECKED_ID, group="animals",
        name={"zh": "海豚", "en": "Ballphins"}, values=_IA_NRDOA,
        icon_element="ballphins.tex"),
    "fishermerm_setting": ModWorldSetting(key="fishermerm_setting", is_rule=True, mod_id=_IA_SHIPWRECKED_ID, group="animals",
        name={"zh": "渔人", "en": "Fisher Merms"}, values=_IA_NRDOA,
        icon_element="merms.tex"),
    "sharkitten_setting": ModWorldSetting(key="sharkitten_setting", is_rule=True, mod_id=_IA_SHIPWRECKED_ID, group="animals",
        name={"zh": "猫鲨", "en": "Sharkittens"}, values=_IA_NRDOA,
        icon_element="sharkitten.tex"),
    "lobster_setting": ModWorldSetting(key="lobster_setting", is_rule=True, mod_id=_IA_SHIPWRECKED_ID, group="animals",
        name={"zh": "龙虾", "en": "Wobsters"}, values=_IA_NRDOA,
        icon_element="lobsters.tex"),
    "jellyfish_setting": ModWorldSetting(key="jellyfish_setting", is_rule=True, mod_id=_IA_SHIPWRECKED_ID, group="animals",
        name={"zh": "水母", "en": "Jellyfish"}, values=_IA_NRDOA,
        icon_element="jellyfish.tex"),
    "rainbowjellyfish_setting": ModWorldSetting(key="rainbowjellyfish_setting", is_rule=True, mod_id=_IA_SHIPWRECKED_ID, group="animals",
        name={"zh": "彩虹水母", "en": "Rainbow Jellyfish"}, values=_IA_NRDOA,
        icon_element="rainbowjellyfish.tex"),
    "solofish_setting": ModWorldSetting(key="solofish_setting", is_rule=True, mod_id=_IA_SHIPWRECKED_ID, group="animals",
        name={"zh": "狗鱼", "en": "Dogfish"}, values=_IA_NRDOA,
        icon_element="dogfish.tex"),
    "mosquito": ModWorldSetting(key="mosquito", is_rule=True, mod_id=_IA_SHIPWRECKED_ID, group="monsters",
        name={"zh": "毒蚊子", "en": "Poison Mosquitos"}, values=_IA_NRDOA,
        icon_element="mosquitos.tex"),
    "swordfish_setting": ModWorldSetting(key="swordfish_setting", is_rule=True, mod_id=_IA_SHIPWRECKED_ID, group="monsters",
        name={"zh": "剑鱼", "en": "Swordfish"}, values=_IA_NRDOA,
        icon_element="swordfish.tex"),
    "stungray_setting": ModWorldSetting(key="stungray_setting", is_rule=True, mod_id=_IA_SHIPWRECKED_ID, group="monsters",
        name={"zh": "恶臭蝠鲼", "en": "Stink Rays"}, values=_IA_NRDOA,
        icon_element="stinkrays.tex"),
    "dragoon_setting": ModWorldSetting(key="dragoon_setting", is_rule=True, mod_id=_IA_SHIPWRECKED_ID, group="monsters",
        name={"zh": "呆龙", "en": "Dragoons"}, values=_IA_NRDOA,
        icon_element="dragoons.tex"),
    # chessnavy_setting 直接对 never/rare/often/always 做 if 判断，其余值（含 default）走默认分支，取标准 5 档
    "chessnavy_setting": ModWorldSetting(key="chessnavy_setting", is_rule=True, mod_id=_IA_SHIPWRECKED_ID, group="monsters",
        name={"zh": "浮船骑士", "en": "Floaty Boaty Knights"}, values=_IA_NRDOA,
        icon_element="chess_monsters.tex"),
    "twister": ModWorldSetting(key="twister", is_rule=True, mod_id=_IA_SHIPWRECKED_ID, group="giants",
        name={"zh": "豹卷风", "en": "Sealnado"}, values=_IA_NRDOA,
        icon_element="twister.tex"),
    # tigershark/kraken 未写 desc，继承 giants 组的 5 档；Post 里的 9 档/6 档 MULTIPLY 表在 UI 上选不到
    "tigershark": ModWorldSetting(key="tigershark", is_rule=True, mod_id=_IA_SHIPWRECKED_ID, group="giants",
        name={"zh": "虎鲨", "en": "Tiger Sharks"}, values=_IA_NRDOA,
        icon_element="tigershark.tex"),
    "kraken": ModWorldSetting(key="kraken", is_rule=True, mod_id=_IA_SHIPWRECKED_ID, group="giants",
        name={"zh": "海妖", "en": "Quacken"}, values=_IA_NRDOA,
        icon_element="kraken.tex"),

    # ── 世界生成（只读）──
    "shipwrecked_season_start": ModWorldSetting(key="shipwrecked_season_start", is_rule=False, mod_id=_IA_SHIPWRECKED_ID, group="global",
        name={"zh": "海难开局季节", "en": "Shipwrecked Starting Season"}, values=None, order=2,
        icon_element="season_start.tex"),
    "volcano": ModWorldSetting(key="volcano", is_rule=False, mod_id=_IA_SHIPWRECKED_ID, group="misc",
        name={"zh": "火山", "en": "Volcano"}, values=None, icon_element="volcano.tex"),
    "bermudatriangle": ModWorldSetting(key="bermudatriangle", is_rule=False, mod_id=_IA_SHIPWRECKED_ID, group="misc",
        name={"zh": "电光三角", "en": "Electric Isosceles"}, values=None, icon_element="bermudatriangle.tex"),
    "volcanoisland": ModWorldSetting(key="volcanoisland", is_rule=False, mod_id=_IA_SHIPWRECKED_ID, group="misc",
        name={"zh": "火山岛", "en": "Volcanic Island"}, values=None, icon_element="volcano_island.tex"),
    "sweet_potato": ModWorldSetting(key="sweet_potato", is_rule=False, mod_id=_IA_SHIPWRECKED_ID, group="resources",
        name={"zh": "甘薯", "en": "Sweet Potatoes"}, values=None, icon_element="sweetpotatos.tex"),
    "limpets": ModWorldSetting(key="limpets", is_rule=False, mod_id=_IA_SHIPWRECKED_ID, group="resources",
        name={"zh": "帽贝岩", "en": "Limpets"}, values=None, icon_element="limpets.tex"),
    "mussel_farm": ModWorldSetting(key="mussel_farm", is_rule=False, mod_id=_IA_SHIPWRECKED_ID, group="resources",
        name={"zh": "贻贝", "en": "Mussels"}, values=None, icon_element="mussels.tex"),
    "seaweed": ModWorldSetting(key="seaweed", is_rule=False, mod_id=_IA_SHIPWRECKED_ID, group="resources",
        name={"zh": "海带", "en": "Seaweeds"}, values=None, icon_element="seaweed.tex"),
    "seashell": ModWorldSetting(key="seashell", is_rule=False, mod_id=_IA_SHIPWRECKED_ID, group="resources",
        name={"zh": "贝壳", "en": "Seashells"}, values=None, icon_element="seashell.tex"),
    "bamboo": ModWorldSetting(key="bamboo", is_rule=False, mod_id=_IA_SHIPWRECKED_ID, group="resources",
        name={"zh": "竹子", "en": "Bamboo"}, values=None, icon_element="bamboo.tex"),
    "bush_vine": ModWorldSetting(key="bush_vine", is_rule=False, mod_id=_IA_SHIPWRECKED_ID, group="resources",
        name={"zh": "藤蔓根", "en": "Viney Bushes"}, values=None, icon_element="vines.tex"),
    "coral": ModWorldSetting(key="coral", is_rule=False, mod_id=_IA_SHIPWRECKED_ID, group="resources",
        name={"zh": "珊瑚", "en": "Corals"}, values=None, icon_element="coral.tex"),
    "coral_brain_rock": ModWorldSetting(key="coral_brain_rock", is_rule=False, mod_id=_IA_SHIPWRECKED_ID, group="resources",
        name={"zh": "智慧树", "en": "Brainy Sprouts"}, values=None, icon_element="braincoral.tex"),
    "crate": ModWorldSetting(key="crate", is_rule=False, mod_id=_IA_SHIPWRECKED_ID, group="resources",
        name={"zh": "板条箱", "en": "Crates"}, values=None, icon_element="crates.tex"),
    "tidalpool": ModWorldSetting(key="tidalpool", is_rule=False, mod_id=_IA_SHIPWRECKED_ID, group="resources",
        name={"zh": "潮汐池", "en": "Tidal Pools"}, values=None, icon_element="tidalpools.tex"),
    "sandhill": ModWorldSetting(key="sandhill", is_rule=False, mod_id=_IA_SHIPWRECKED_ID, group="resources",
        name={"zh": "沙堆", "en": "Sandy Piles"}, values=None, icon_element="sand.tex"),
    "poisonhole": ModWorldSetting(key="poisonhole", is_rule=False, mod_id=_IA_SHIPWRECKED_ID, group="resources",
        name={"zh": "毒穴", "en": "Poisonous Holes"}, values=None, icon_element="poisonhole.tex"),
    "bioluminescence": ModWorldSetting(key="bioluminescence", is_rule=False, mod_id=_IA_SHIPWRECKED_ID, group="resources",
        name={"zh": "荧光生物", "en": "Bioluminescence"}, values=None, icon_element="bioluminescence.tex"),
    "magma_rocks": ModWorldSetting(key="magma_rocks", is_rule=False, mod_id=_IA_SHIPWRECKED_ID, group="resources",
        name={"zh": "熔岩矿堆", "en": "Magma Piles"}, values=None, icon_element="magmarocks.tex"),
    "tar_pool": ModWorldSetting(key="tar_pool", is_rule=False, mod_id=_IA_SHIPWRECKED_ID, group="resources",
        name={"zh": "焦油", "en": "Tar Pools"}, values=None, icon_element="tarpools.tex"),
    "shipwrecked_trees": ModWorldSetting(key="shipwrecked_trees", is_rule=False, mod_id=_IA_SHIPWRECKED_ID, group="resources",
        name={"zh": "岛屿树木（所有）", "en": "Island Trees (All)"}, values=None, icon_element="trees.tex"),
    "shipwreck": ModWorldSetting(key="shipwreck", is_rule=False, mod_id=_IA_SHIPWRECKED_ID, group="resources",
        name={"zh": "沉船", "en": "Wrecks"}, values=None, icon_element="wrecks.tex"),
    "waterygrave": ModWorldSetting(key="waterygrave", is_rule=False, mod_id=_IA_SHIPWRECKED_ID, group="resources",
        name={"zh": "水墓", "en": "Watery Graves"}, values=None, icon_element="waterygraves.tex"),
    "coffeebush": ModWorldSetting(key="coffeebush", is_rule=False, mod_id=_IA_SHIPWRECKED_ID, group="resources",
        name={"zh": "咖啡丛", "en": "Coffee Bushes"}, values=None, icon_element="coffeebush.tex"),
    "elephantcactus": ModWorldSetting(key="elephantcactus", is_rule=False, mod_id=_IA_SHIPWRECKED_ID, group="resources",
        name={"zh": "象仙人掌", "en": "Elephant Cacti"}, values=None, icon_element="elephantcactus_active.tex"),
    "rock_obsidian": ModWorldSetting(key="rock_obsidian", is_rule=False, mod_id=_IA_SHIPWRECKED_ID, group="resources",
        name={"zh": "黑曜石矿", "en": "Obsidian Boulders"}, values=None, icon_element="rock_obsidian.tex"),
    "rock_charcoal": ModWorldSetting(key="rock_charcoal", is_rule=False, mod_id=_IA_SHIPWRECKED_ID, group="resources",
        name={"zh": "木炭矿", "en": "Charcoal Boulders"}, values=None, icon_element="rock_charcoal.tex"),
    "volcano_shrub": ModWorldSetting(key="volcano_shrub", is_rule=False, mod_id=_IA_SHIPWRECKED_ID, group="resources",
        name={"zh": "灰烬树", "en": "Ash Trees"}, values=None, icon_element="volcano_shrub.tex"),
    "crabhole": ModWorldSetting(key="crabhole", is_rule=False, mod_id=_IA_SHIPWRECKED_ID, group="animals",
        name={"zh": "兔蟹洞", "en": "Crabbit Dens"}, values=None, icon_element="crabbithole.tex"),
    "ox": ModWorldSetting(key="ox", is_rule=False, mod_id=_IA_SHIPWRECKED_ID, group="animals",
        name={"zh": "水牛", "en": "Water Beefalos"}, values=None, icon_element="ox.tex"),
    "doydoy": ModWorldSetting(key="doydoy", is_rule=False, mod_id=_IA_SHIPWRECKED_ID, group="animals",
        name={"zh": "渡渡鸟", "en": "Doydoys"}, values=None, icon_element="doydoy.tex"),
    "wildbores": ModWorldSetting(key="wildbores", is_rule=False, mod_id=_IA_SHIPWRECKED_ID, group="animals",
        name={"zh": "野猪舍", "en": "Wildbore Houses"}, values=None, icon_element="wildborehouse.tex"),
    "ballphin": ModWorldSetting(key="ballphin", is_rule=False, mod_id=_IA_SHIPWRECKED_ID, group="animals",
        name={"zh": "海豚宫殿", "en": "Ballphin Palaces"}, values=None, icon_element="ballphinhouse.tex"),
    "primeape": ModWorldSetting(key="primeape", is_rule=False, mod_id=_IA_SHIPWRECKED_ID, group="animals",
        name={"zh": "猿猴小窝", "en": "Prime Ape Huts"}, values=None, icon_element="primeapehut.tex"),
    "fishermerm": ModWorldSetting(key="fishermerm", is_rule=False, mod_id=_IA_SHIPWRECKED_ID, group="animals",
        name={"zh": "渔人小屋", "en": "Fisher Merm Huts"}, values=None, icon_element="fishermermhouse.tex"),
    "lobster": ModWorldSetting(key="lobster", is_rule=False, mod_id=_IA_SHIPWRECKED_ID, group="animals",
        name={"zh": "龙虾", "en": "Wobster Dens"}, values=None, icon_element="lobsterhole.tex"),
    "solofish": ModWorldSetting(key="solofish", is_rule=False, mod_id=_IA_SHIPWRECKED_ID, group="animals",
        name={"zh": "狗鱼", "en": "Dogfish"}, values=None, icon_element="dogfish.tex"),
    "jellyfish": ModWorldSetting(key="jellyfish", is_rule=False, mod_id=_IA_SHIPWRECKED_ID, group="animals",
        name={"zh": "水母", "en": "Jellyfish"}, values=None, icon_element="jellyfish.tex"),
    "rainbowjellyfish": ModWorldSetting(key="rainbowjellyfish", is_rule=False, mod_id=_IA_SHIPWRECKED_ID, group="animals",
        name={"zh": "彩虹水母", "en": "Rainbow Jellyfish"}, values=None, icon_element="rainbowjellyfish.tex"),
    "fishinhole": ModWorldSetting(key="fishinhole", is_rule=False, mod_id=_IA_SHIPWRECKED_ID, group="animals",
        name={"zh": "鱼群", "en": "Shoals"}, values=None, icon_element="shoals.tex"),
    "seagull": ModWorldSetting(key="seagull", is_rule=False, mod_id=_IA_SHIPWRECKED_ID, group="animals",
        name={"zh": "海鸥", "en": "Seagulls"}, values=None, icon_element="seagulls.tex"),
    "flup": ModWorldSetting(key="flup", is_rule=False, mod_id=_IA_SHIPWRECKED_ID, group="monsters",
        name={"zh": "独眼弹涂鱼", "en": "Flups"}, values=None, icon_element="flups.tex"),
    "swordfish": ModWorldSetting(key="swordfish", is_rule=False, mod_id=_IA_SHIPWRECKED_ID, group="monsters",
        name={"zh": "剑鱼", "en": "Swordfish"}, values=None, icon_element="swordfish.tex"),
    "stungray": ModWorldSetting(key="stungray", is_rule=False, mod_id=_IA_SHIPWRECKED_ID, group="monsters",
        name={"zh": "恶臭蝠鲼", "en": "Stink Rays"}, values=None, icon_element="stinkrays.tex"),
    "snake": ModWorldSetting(key="snake", is_rule=False, mod_id=_IA_SHIPWRECKED_ID, group="monsters",
        name={"zh": "蛇窝", "en": "Snake Dens"}, values=None, icon_element="snakeden.tex"),
    "dragoon": ModWorldSetting(key="dragoon", is_rule=False, mod_id=_IA_SHIPWRECKED_ID, group="monsters",
        name={"zh": "呆龙窝", "en": "Dragoon Dens"}, values=None, icon_element="dragoonden.tex"),
}

# workshop-3322803908 == 云霄国度-Above the Clouds（移植单机版猪镇 Porkland）
# 来源（content/322330/3322803908/）：
#   - key/category：modcustomizeitems.lua 的 customize_items 表，直接调用 AddCustomizeItem；
#   - 未写 desc 的条目继承所属原版 group 的 desc（本体 customize.lua：
#     ``item.desc or item.group.desc``），monsters/animals 为 frequency_descriptions；
#   - enable_descriptions 是 Mod 局部表，与原版 yesno_descriptions 相同（never/default）；
#   - temperate/humid/lush 用 season_length_descriptions 七档；
#   - 中文名：scripts/languages/pl_chinese_s.po；
#   - 图标：images/hud/customization_porkland.xml（全部条目统一指向该图集，40 项已核对）。
# 注意：poison 与岛屿冒险的 poison 撞名但含义不同；本体 AddCustomizeItem 遇到已注册的 name 直接
# return，同时启用时界面上只有先加载的 Mod（岛屿冒险核心 priority 5）那一条，见 get_mod_world_settings。
_PORKLAND_ID = "3322803908"

# 原版 monsters/animals 组的默认 desc，以及这个 mod 自己复刻的
# yesno_descriptions 局部表，均已读游戏本体源码核对，见上方大段注释。
_PL_FREQUENCY = ["never", "rare", "default", "often", "always"]
_PL_NEVER_DEFAULT = ["never", "default"]

PORKLAND_SETTINGS: dict[str, ModWorldSetting] = {
    # ── 世界设置（可编辑）──
    "temperate": ModWorldSetting(key="temperate", is_rule=True, mod_id=_PORKLAND_ID, group="porkland_settings_global",
        name={"zh": "平和季", "en": "Temperate"}, values=_IA_SEASON_LENGTH, order=2,
        icon_element="temperate.tex"),
    "humid": ModWorldSetting(key="humid", is_rule=True, mod_id=_PORKLAND_ID, group="porkland_settings_global",
        name={"zh": "潮湿季", "en": "Humid"}, values=_IA_SEASON_LENGTH, order=3,
        icon_element="humid.tex"),
    "lush": ModWorldSetting(key="lush", is_rule=True, mod_id=_PORKLAND_ID, group="porkland_settings_global",
        name={"zh": "繁茂季", "en": "Lush"}, values=_IA_SEASON_LENGTH, order=4,
        icon_element="lush.tex"),
    "bill_setting": ModWorldSetting(key="bill_setting", is_rule=True, mod_id=_PORKLAND_ID, group="monsters",
        name={"zh": "鸭嘴豪猪", "en": "Platapines"}, values=_PL_FREQUENCY,
        icon_element="platypine.tex"),
    "frog_poison_setting": ModWorldSetting(key="frog_poison_setting", is_rule=True, mod_id=_PORKLAND_ID, group="monsters",
        name={"zh": "箭毒蛙", "en": "Poison Dartfrogs"}, values=_PL_FREQUENCY,
        icon_element="poison_dart_frogs.tex"),
    "giantgrub_setting": ModWorldSetting(key="giantgrub_setting", is_rule=True, mod_id=_PORKLAND_ID, group="monsters",
        name={"zh": "巨型蛆虫", "en": "Giant Grub"}, values=_PL_FREQUENCY,
        icon_element="giant_grubs.tex"),
    "mosquito_setting": ModWorldSetting(key="mosquito_setting", is_rule=True, mod_id=_PORKLAND_ID, group="monsters",
        name={"zh": "蚊子", "en": "Mosquitos"}, values=_PL_FREQUENCY,
        icon_element="mosquitos.tex"),
    "roc_setting": ModWorldSetting(key="roc_setting", is_rule=True, mod_id=_PORKLAND_ID, group="monsters",
        name={"zh": "友善的大鹏", "en": "BFB"}, values=_PL_NEVER_DEFAULT,
        icon_element="roc.tex"),
    "weevole_setting": ModWorldSetting(key="weevole_setting", is_rule=True, mod_id=_PORKLAND_ID, group="monsters",
        name={"zh": "象鼻鼠虫", "en": "Weevole"}, values=_PL_FREQUENCY,
        icon_element="weevole.tex"),
    "pugalisk_fountain": ModWorldSetting(key="pugalisk_fountain", is_rule=True, mod_id=_PORKLAND_ID, group="monsters",
        name={"zh": "不老泉", "en": "Fountain of Youth"}, values=_PL_FREQUENCY,
        icon_element="pugalisk_fountain.tex"),
    "dungbeetle_setting": ModWorldSetting(key="dungbeetle_setting", is_rule=True, mod_id=_PORKLAND_ID, group="animals",
        name={"zh": "屎壳郎", "en": "Dung Beetle"}, values=_PL_FREQUENCY,
        icon_element="dungbeetle.tex"),
    "glowfly_setting": ModWorldSetting(key="glowfly_setting", is_rule=True, mod_id=_PORKLAND_ID, group="animals",
        name={"zh": "发光飞虫", "en": "Glowfly"}, values=_PL_FREQUENCY,
        icon_element="glowflies.tex"),
    "hanging_vine_setting": ModWorldSetting(key="hanging_vine_setting", is_rule=True, mod_id=_PORKLAND_ID, group="animals",
        name={"zh": "垂下的藤蔓", "en": "Hanging Vine"}, values=_PL_FREQUENCY,
        icon_element="grabbing_vine.tex"),
    "hippopotamoose_setting": ModWorldSetting(key="hippopotamoose_setting", is_rule=True, mod_id=_PORKLAND_ID, group="animals",
        name={"zh": "河鹿", "en": "Hippopotamooses"}, values=_PL_FREQUENCY,
        icon_element="hippopotamoose.tex"),
    "mandrakeman_setting": ModWorldSetting(key="mandrakeman_setting", is_rule=True, mod_id=_PORKLAND_ID, group="animals",
        name={"zh": "曼德拉长者", "en": "Elder Mandrakes"}, values=_PL_FREQUENCY,
        icon_element="mandrake_men.tex"),
    "piko_setting": ModWorldSetting(key="piko_setting", is_rule=True, mod_id=_PORKLAND_ID, group="animals",
        name={"zh": "异食松鼠", "en": "Piko"}, values=_PL_FREQUENCY,
        icon_element="orange_pikos.tex"),
    "thunderbird_setting": ModWorldSetting(key="thunderbird_setting", is_rule=True, mod_id=_PORKLAND_ID, group="animals",
        name={"zh": "雷鸟", "en": "Thunderbirds"}, values=_PL_FREQUENCY,
        icon_element="thunderbirds.tex"),
    "brambles": ModWorldSetting(key="brambles", is_rule=True, mod_id=_PORKLAND_ID, group="misc",
        name={"zh": "荆棘", "en": "Brambles"}, values=_PL_NEVER_DEFAULT,
        icon_element="brambles.tex"),
    "fog": ModWorldSetting(key="fog", is_rule=True, mod_id=_PORKLAND_ID, group="misc",
        name={"zh": "雾", "en": "Fog"}, values=_PL_NEVER_DEFAULT,
        icon_element="fog.tex"),
    "glowflycycle": ModWorldSetting(key="glowflycycle", is_rule=True, mod_id=_PORKLAND_ID, group="misc",
        name={"zh": "发光飞虫周期", "en": "Glowfly Cycle"}, values=_PL_NEVER_DEFAULT,
        icon_element="glowfly_life_cycle.tex"),
    "poison": ModWorldSetting(key="poison", is_rule=True, mod_id=_PORKLAND_ID, group="misc",
        name={"zh": "毒", "en": "Poison"}, values=_PL_NEVER_DEFAULT,
        icon_element="poison.tex"),
    "hayfever": ModWorldSetting(key="hayfever", is_rule=True, mod_id=_PORKLAND_ID, group="misc",
        name={"zh": "花粉症", "en": "Hayfever"}, values=_PL_NEVER_DEFAULT,
        icon_element="hayfever.tex"),
    "pigbandit": ModWorldSetting(key="pigbandit", is_rule=True, mod_id=_PORKLAND_ID, group="misc",
        name={"zh": "蒙面猪人", "en": "Masked Pig"}, values=_PL_FREQUENCY,
        icon_element="pig_bandit.tex"),
    "vampirebat": ModWorldSetting(key="vampirebat", is_rule=True, mod_id=_PORKLAND_ID, group="misc",
        name={"zh": "吸血蝙蝠袭击", "en": "Vampire Bat Attacks"}, values=_PL_FREQUENCY,
        icon_element="vampire_bats.tex"),

    # ── 世界生成（只读）──
    "porkland_season_start": ModWorldSetting(key="porkland_season_start", is_rule=False, mod_id=_PORKLAND_ID, group="global",
        name={"zh": "猪镇起始季节", "en": "Hamlet Starting Season"},
        values=["default", "humid", "lush", "temperate|humid|lush"], order=2,
        icon_element="season_start.tex"),
    "dungpile": ModWorldSetting(key="dungpile", is_rule=False, mod_id=_PORKLAND_ID, group="animals",
        name={"zh": "粪堆", "en": "Dung Pile"}, values=None, icon_element="dungpile.tex"),
    "hippopotamoose": ModWorldSetting(key="hippopotamoose", is_rule=False, mod_id=_PORKLAND_ID, group="animals",
        name={"zh": "河鹿", "en": "Hippopotamooses"}, values=None, icon_element="hippopotamoose.tex"),
    "peagawk": ModWorldSetting(key="peagawk", is_rule=False, mod_id=_PORKLAND_ID, group="animals",
        name={"zh": "呆望雀", "en": "Peagawk"}, values=None, icon_element="peagawk.tex"),
    "pog": ModWorldSetting(key="pog", is_rule=False, mod_id=_PORKLAND_ID, group="animals",
        name={"zh": "哈巴狸", "en": "Pogs"}, values=None, icon_element="pogs.tex"),
    "pangolden": ModWorldSetting(key="pangolden", is_rule=False, mod_id=_PORKLAND_ID, group="animals",
        name={"zh": "淘金兽", "en": "Pangolden"}, values=None, icon_element="pangolden.tex"),
    "hanging_vine_patch": ModWorldSetting(key="hanging_vine_patch", is_rule=False, mod_id=_PORKLAND_ID, group="animals",
        name={"zh": "垂下的藤蔓", "en": "Hanging Vine"}, values=None, icon_element="hanging_vine.tex"),
    "thunderbirdnest": ModWorldSetting(key="thunderbirdnest", is_rule=False, mod_id=_PORKLAND_ID, group="animals",
        name={"zh": "雷鸟巢", "en": "Thundernest"}, values=None, icon_element="thunderbirds.tex"),
    "asparagus": ModWorldSetting(key="asparagus", is_rule=False, mod_id=_PORKLAND_ID, group="resources",
        name={"zh": "芦笋", "en": "Asparagus"}, values=None, icon_element="asparagus.tex"),
    "grass_tall": ModWorldSetting(key="grass_tall", is_rule=False, mod_id=_PORKLAND_ID, group="resources",
        name={"zh": "高草", "en": "Tall Grass"}, values=None, icon_element="grass_tall.tex"),
    "grass_tall_bunches": ModWorldSetting(key="grass_tall_bunches", is_rule=False, mod_id=_PORKLAND_ID, group="resources",
        name={"zh": "高草丛田", "en": "Tall Grass Fields"}, values=None, icon_element="grass_tall_bunches.tex"),
    "lotus": ModWorldSetting(key="lotus", is_rule=False, mod_id=_PORKLAND_ID, group="resources",
        name={"zh": "睡莲", "en": "lotus"}, values=None, icon_element="lotus.tex"),
    "lost_relics": ModWorldSetting(key="lost_relics", is_rule=False, mod_id=_PORKLAND_ID, group="resources",
        name={"zh": "失落的文物", "en": "Lost Relics"}, values=None, icon_element="lost_relics.tex"),
    "ruined_sculptures": ModWorldSetting(key="ruined_sculptures", is_rule=False, mod_id=_PORKLAND_ID, group="resources",
        name={"zh": "毁坏的雕塑", "en": "Ruined Sculptures"}, values=None, icon_element="lost_sculptures.tex"),
    "jungle_border_vine": ModWorldSetting(key="jungle_border_vine", is_rule=False, mod_id=_PORKLAND_ID, group="misc",
        name={"zh": "雨林树冠的藤蔓", "en": "Jungle Canopy Vines"}, values=None, icon_element="jungle_border_vine.tex"),
    "deep_jungle_fern_noise": ModWorldSetting(key="deep_jungle_fern_noise", is_rule=False, mod_id=_PORKLAND_ID, group="misc",
        name={"zh": "雨林地皮上的蕨类植物", "en": "Jungle Floor Ferns"}, values=None, icon_element="deep_jungle_fern_noise.tex"),
}


def _set_verified_scope(
    settings: dict[str, ModWorldSetting],
    keys,
    locations,
    *,
    master_controlled: bool = False,
) -> None:
    """把真实 AddCustomizeItem 字段写回登记项，并校验 key 没有抄错。"""
    scope = frozenset(locations) if locations is not None else None
    for key in keys:
        if key not in settings:
            raise KeyError(f"未登记的 Mod 世界设置 key: {key}")
        settings[key].locations = scope
        if master_controlled:
            settings[key].master_controlled = True


_ALL_VERIFIED_LOCATIONS = {
    FOREST_LOCATION, CAVE_LOCATION, SHIPWRECKED_LOCATION,
    VOLCANO_LOCATION, PORKLAND_LOCATION,
}
_OCEAN_LOCATIONS = {FOREST_LOCATION, SHIPWRECKED_LOCATION, PORKLAND_LOCATION}

# Cherry Forest 的 world 缺省值由 init_worldgen.lua 明确补成这三个地点；
# 世界生成条目则在 cherry_customizations.lua 中逐项限制为 forest/shipwrecked。
_set_verified_scope(
    CHERRY_FOREST_SETTINGS,
    [key for key, info in CHERRY_FOREST_SETTINGS.items() if info.is_rule],
    {FOREST_LOCATION, SHIPWRECKED_LOCATION, VOLCANO_LOCATION},
)
_set_verified_scope(
    CHERRY_FOREST_SETTINGS,
    [key for key, info in CHERRY_FOREST_SETTINGS.items() if not info.is_rule],
    {FOREST_LOCATION, SHIPWRECKED_LOCATION},
)
_set_verified_scope(
    CHERRY_FOREST_SETTINGS,
    {"cherry_bugseason", "cherrift", "petalwind"},
    {FOREST_LOCATION, SHIPWRECKED_LOCATION, VOLCANO_LOCATION},
    master_controlled=True,
)

# Island Adventures - Core。
_set_verified_scope(IA_CORE_SETTINGS, {"poison"}, None, master_controlled=True)
_set_verified_scope(
    IA_CORE_SETTINGS,
    {"dst_boats", "ia_boats", "primeape_setting", "snake_setting"},
    _ALL_VERIFIED_LOCATIONS,
)
_set_verified_scope(IA_CORE_SETTINGS, {"ia_drowning"}, _OCEAN_LOCATIONS)

# Island Adventures - Shipwrecked：以下集合逐项对应
# modservercreationmain.lua 的 ia_settings_customize_table / ia_worldgen_customize_table。
_set_verified_scope(
    IA_SHIPWRECKED_SETTINGS,
    {"mild", "hurricane", "monsoon", "dry"},
    None,
    master_controlled=True,
)
_set_verified_scope(
    IA_SHIPWRECKED_SETTINGS,
    {"shipwrecked_season_start"},
    None,
    master_controlled=True,
)
_set_verified_scope(
    IA_SHIPWRECKED_SETTINGS,
    {
        "floods", "tides", "oceanwaves", "whalehunt", "alternatewhalehunt",
        "waterencounters", "crocodog", "yellowcrocodog", "bluecrocodog",
        "sweet_potato_regrowth", "palmtree_regrowth",
        "jungletree_regrowth", "mangrovetree_regrowth",
        "coral_brain_rock_regrowth", "seashell_regrowth", "sandhill_regrowth",
        "bioluminescence_regrowth", "crab_setting", "sharkitten_setting",
        "lobster_setting", "jellyfish_setting", "rainbowjellyfish_setting",
        "solofish_setting", "mosquito", "swordfish_setting", "stungray_setting",
        "chessnavy_setting", "twister", "tigershark", "kraken",
    },
    {SHIPWRECKED_LOCATION},
)
_set_verified_scope(
    IA_SHIPWRECKED_SETTINGS,
    {"rock_obsidian_regrowth", "rock_charcoal_regrowth", "volcano_shrub_regrowth", "magmarock_regrowth"},
    {VOLCANO_LOCATION},
)
_set_verified_scope(
    IA_SHIPWRECKED_SETTINGS,
    {"dragoonegg"},
    {SHIPWRECKED_LOCATION, VOLCANO_LOCATION},
)
_set_verified_scope(
    IA_SHIPWRECKED_SETTINGS,
    {"wildbores_setting", "fishermerm_setting", "dragoon_setting"},
    _ALL_VERIFIED_LOCATIONS,
)
_set_verified_scope(IA_SHIPWRECKED_SETTINGS, {"ballphin_setting"}, _OCEAN_LOCATIONS)

_set_verified_scope(
    IA_SHIPWRECKED_SETTINGS,
    {
        "volcano", "bermudatriangle", "volcanoisland", "sweet_potato",
        "limpets", "mussel_farm", "seaweed", "seashell", "bamboo",
        "bush_vine", "coral", "coral_brain_rock", "crate", "tidalpool",
        "sandhill", "poisonhole", "bioluminescence", "tar_pool",
        "shipwrecked_trees", "shipwreck", "waterygrave", "crabhole", "ox",
        "doydoy", "wildbores", "ballphin", "primeape", "fishermerm",
        "lobster", "solofish", "jellyfish", "rainbowjellyfish", "fishinhole",
        "seagull", "flup", "swordfish", "stungray", "snake",
    },
    {SHIPWRECKED_LOCATION},
)
_set_verified_scope(
    IA_SHIPWRECKED_SETTINGS,
    {"coffeebush", "elephantcactus", "rock_obsidian", "rock_charcoal", "volcano_shrub", "dragoon"},
    {VOLCANO_LOCATION},
)
_set_verified_scope(
    IA_SHIPWRECKED_SETTINGS,
    {"magma_rocks"},
    {SHIPWRECKED_LOCATION, VOLCANO_LOCATION},
)

# Above the Clouds 新增条目在注册前统一补 ``world={"porkland"}``。
_set_verified_scope(PORKLAND_SETTINGS, PORKLAND_SETTINGS, {PORKLAND_LOCATION})
_set_verified_scope(
    PORKLAND_SETTINGS,
    {"temperate", "humid", "lush", "porkland_season_start"},
    {PORKLAND_LOCATION},
    master_controlled=True,
)

# workshop-3360553731 == Beneath the World Below（深埋之下），0.4.16.6
# 来源（content/322330/3360553731/）：
#   - 25 个无条件条目：scripts/mains/init/bwb_customizations.lua，init_worldgen.lua 逐条注册；
#     0.4.16 起原版测试版重做石虾，Mod 注释掉了 rocky_gold/rocky_master（旧存档里的会被换成普通石虾），
#     新增 stalagmites_regrowth/slurtlehole_regrowth；
#   - desc 为标准描述表时用游戏真实值，nightmareclock/cave_season_start 用 Mod 自己的 data 顺序；
#     widow_setting/widow_bags 仅同时启用 DSTU 时加入（见 BENEATH_WORLD_BELOW_DSTU_SETTINGS）；
#   - 中英文名：scripts/wormstrings.lua、wormstrings_en.lua；图标取自 Mod 图集 XML。
_BWB_ID = "3360553731"
_BWB_FREQUENCY = ["never", "rare", "default", "often", "always"]
_BWB_YES_NO = ["never", "default"]
_BWB_SPEED = ["never", "veryslow", "slow", "default", "fast", "veryfast"]
_BWB_SEASON_LENGTH = [
    "noseason", "veryshortseason", "shortseason", "default",
    "longseason", "verylongseason", "random",
]
_BWB_NIGHTMARE_LENGTH = [
    "noseason", "veryshortseason", "shortseason", "default",
    "longseason", "morelongseason", "verylongseason",
    "superlongseason", "random",
]

BENEATH_WORLD_BELOW_SETTINGS: dict[str, ModWorldSetting] = {
    "nightmareclock": ModWorldSetting(
        "nightmareclock", True, {"zh": "梦魇循环", "en": "Nightmare Cycles"},
        _BWB_NIGHTMARE_LENGTH, _BWB_ID, "nightmare_time.tex",
        locations=frozenset({CAVE_LOCATION}), group="misc",
    ),
    "fungusfog": ModWorldSetting(
        "fungusfog", True, {"zh": "孢子雾", "en": "Fungus Fog"},
        _BWB_FREQUENCY, _BWB_ID, "fungusfog.tex",
        locations=frozenset({CAVE_LOCATION}), group="misc",
    ),
    "tickle": ModWorldSetting(
        "tickle", True, {"zh": "挠痒", "en": "Tickle"},
        _BWB_FREQUENCY, _BWB_ID, "tickle.tex",
        locations=frozenset({CAVE_LOCATION}), group="misc",
    ),
    "horrorhounds": ModWorldSetting(
        "horrorhounds", True, {"zh": "恐惧猎犬群", "en": "Horror Hound Waves"},
        _BWB_YES_NO, _BWB_ID, "horrorhounds.tex",
        locations=frozenset({FOREST_LOCATION}), group="misc", order=3.1,
    ),
    "wargwave": ModWorldSetting(
        "wargwave", True, {"zh": "狼群", "en": "Varg Waves"},
        _BWB_FREQUENCY, _BWB_ID, "wargwave.tex",
        locations=frozenset({FOREST_LOCATION}), group="misc", order=3.2,
    ),
    "lunarthrall_plant_remove": ModWorldSetting(
        "lunarthrall_plant_remove", True,
        {"zh": "亮茄枯萎", "en": "Brightshade No-Rift Death"},
        ["none", "always"], _BWB_ID, "lunarthrall_plant_remove.tex",
        initial_value="none", locations=frozenset({FOREST_LOCATION}), group="misc",
    ),
    "stealworms": ModWorldSetting(
        "stealworms", True, {"zh": "贪婪蠕虫群", "en": "Greedy Worm Waves"},
        _BWB_YES_NO, _BWB_ID, "stealworms.tex",
        locations=frozenset({CAVE_LOCATION}), group="misc", order=2.1,
    ),
    "starworms": ModWorldSetting(
        "starworms", True, {"zh": "恒星蠕虫群", "en": "Star Worm Waves"},
        _BWB_YES_NO, _BWB_ID, "starworms.tex",
        locations=frozenset({CAVE_LOCATION}), group="misc", order=2.2,
    ),
    "vineworms": ModWorldSetting(
        "vineworms", True, {"zh": "缠藤蠕虫群", "en": "Viney Worm Waves"},
        _BWB_YES_NO, _BWB_ID, "vineworms.tex",
        locations=frozenset({CAVE_LOCATION}), group="misc", order=2.3,
    ),
    "tideworms": ModWorldSetting(
        "tideworms", True, {"zh": "潮行蠕虫群", "en": "Tidal Worm Waves"},
        _BWB_YES_NO, _BWB_ID, "tideworms.tex",
        locations=frozenset({CAVE_LOCATION}), group="misc", order=2.4,
    ),
    "worm_ancient": ModWorldSetting(
        "worm_ancient", True, {"zh": "远古深渊蠕虫", "en": "Ancient Abyssal Worms"},
        _BWB_YES_NO, _BWB_ID, "worm_ancient.tex",
        locations=frozenset({CAVE_LOCATION}), group="misc", order=2.5,
    ),
    "worm_boss_setting": ModWorldSetting(
        "worm_boss_setting", True, {"zh": "时令大蠕虫", "en": "Seasonal Great Worm"},
        _BWB_FREQUENCY, _BWB_ID, locations=frozenset({CAVE_LOCATION}), group="giants",
    ),
    "worm_megaboss_setting": ModWorldSetting(
        "worm_megaboss_setting", True,
        {"zh": "巨大深渊蠕虫", "en": "Mega Depths Worm"},
        _BWB_FREQUENCY, _BWB_ID, "wormbosshole.tex",
        locations=frozenset({CAVE_LOCATION}), group="giants",
    ),
    "lunarthrall_plant": ModWorldSetting(
        "lunarthrall_plant", True, {"zh": "致命亮茄", "en": "Deadly Brightshades"},
        _BWB_FREQUENCY, _BWB_ID, "lunarthrall_plant.tex",
        locations=frozenset({FOREST_LOCATION}), group="lunar_mutations",
    ),
    "icker": ModWorldSetting(
        "icker", True, {"zh": "恶液", "en": "Ickers"},
        _BWB_FREQUENCY, _BWB_ID, "icker.tex",
        locations=frozenset({CAVE_LOCATION}), group="monsters",
    ),
    "lurking_shadows": ModWorldSetting(
        "lurking_shadows", True, {"zh": "潜伏暗影", "en": "Lurking Shadows"},
        _BWB_FREQUENCY, _BWB_ID, "ruinsshadow.tex",
        locations=frozenset({CAVE_LOCATION}), group="monsters",
    ),
    "parasitehat": ModWorldSetting(
        "parasitehat", True, {"zh": "暗域先驱", "en": "Void Masques"},
        _BWB_FREQUENCY, _BWB_ID, "parasitehat.tex",
        locations=frozenset({CAVE_LOCATION}), group="monsters",
    ),
    "inkblight": ModWorldSetting(
        "inkblight", True, {"zh": "墨荒", "en": "Ink Blights"},
        _BWB_FREQUENCY, _BWB_ID, "inkblight.tex",
        locations=frozenset({CAVE_LOCATION}), group="monsters",
    ),
    "cave_season_start": ModWorldSetting(
        "cave_season_start", False,
        {"zh": "洞穴起始季节", "en": "Cave Start Season"},
        ["default", "frost", "verdant", "umbral", "tranquil|frost|verdant|umbral"],
        _BWB_ID, master_controlled=True, group="global", order=2,
    ),
    "tranquil": ModWorldSetting(
        "tranquil", True, {"zh": "稳定季", "en": "Tranquil"},
        _BWB_SEASON_LENGTH, _BWB_ID, "tranquil.tex",
        master_controlled=True, group="global", order=30,
    ),
    "frostseason": ModWorldSetting(
        "frostseason", True, {"zh": "冬（洞穴）", "en": "Winter (Cave)"},
        _BWB_SEASON_LENGTH, _BWB_ID,
        master_controlled=True, group="global", order=31,
    ),
    "verdant": ModWorldSetting(
        "verdant", True, {"zh": "丰沃季", "en": "Verdant"},
        _BWB_SEASON_LENGTH, _BWB_ID, "verdant.tex",
        master_controlled=True, group="global", order=32,
    ),
    "umbral": ModWorldSetting(
        "umbral", True, {"zh": "夏（洞穴）", "en": "Summer (Cave)"},
        _BWB_SEASON_LENGTH, _BWB_ID,
        master_controlled=True, group="global", order=33,
    ),
    "stalagmites_regrowth": ModWorldSetting(
        "stalagmites_regrowth", True, {"zh": "石笋", "en": "Stalagmites"},
        _BWB_SPEED, _BWB_ID, "stalagmites_regrowth.tex",
        locations=frozenset({CAVE_LOCATION}), group="resources",
    ),
    # 图标是本体 worldgen_customization 图集的 slurtleden.tex，即原版 slurtles 的图标
    "slurtlehole_regrowth": ModWorldSetting(
        "slurtlehole_regrowth", True, {"zh": "蛞蝓龟窝", "en": "Slurtle Mound"},
        _BWB_SPEED, _BWB_ID, locations=frozenset({CAVE_LOCATION}), group="resources",
        vanilla_icon_key="slurtles",
    ),
}

# 深埋之下通过 KnownModIndex:IsModEnabledAny() 同时兼容永不妥协正式版与
# 测试版；只有两边同时启用时才把这两项插入 customizations 表。
_BWB_DSTU_IDS = frozenset({"2039181790", "3193922031"})
BENEATH_WORLD_BELOW_DSTU_SETTINGS: dict[str, ModWorldSetting] = {
    "widow_setting": ModWorldSetting(
        "widow_setting", True, {"zh": "黑寡妇", "en": "Hooded Widow"},
        _BWB_FREQUENCY, _BWB_ID, "widow.tex",
        locations=frozenset({FOREST_LOCATION}), group="giants",
    ),
    "widow_bags": ModWorldSetting(
        "widow_bags", True, {"zh": "寡妇茧", "en": "Silky Cocoons"},
        _BWB_FREQUENCY, _BWB_ID, "widow_bags.tex",
        locations=frozenset({FOREST_LOCATION}), group="misc",
    ),
}


# workshop-2986194136 == 热带冒险|忒修斯之船（Tropical Adventures），26.09.19
# 来源（content/322330/2986194136/）：
#   - modworldgenmain 导入 main/ta_customize.lua：AddCustomizeGroup 自建 ta_worldgen（世界生成）与
#     ta_climate（世界设置）两组，条目来自 scripts/datadefs/customization.lua；
#   - 取值/文案：各条 options（desc），中英文为源码 en_zh() 的两个参数，原样保留（含原文拼写）；
#   - 存档缺 key 时 ta_config.lua 依次取 overrides → 同名 Mod 配置 → default，modinfo 没有这些同名
#     配置项，故 initial_value 即源码 default；
#   - 图标：sw_atlas/ham_atlas 是本 Mod 的 images/hud 图集，dst_atlas/dstset_atlas 是本体图集。
_TA_ID = "2986194136"
_TA_SW_ATLAS = ("images/hud/customization_shipwrecked.xml", "images/hud/customization_shipwrecked.tex")
_TA_HAM_ATLAS = ("images/hud/customization_porkland.xml", "images/hud/customization_porkland.tex")
_TA_FOREST = frozenset({FOREST_LOCATION})
_TA_CAVE = frozenset({CAVE_LOCATION})
_TA_BOTH = frozenset({FOREST_LOCATION, CAVE_LOCATION})
# options_enable / options_enable2
_TA_ENABLE_LABELS = {
    "enabled": {"zh": "开启", "en": "Enabled"},
    "disabled": {"zh": "关闭", "en": "Disabled"},
}

MOD_CUSTOM_GROUP_NAMES.update({
    "ta_worldgen": {"zh": "热带冒险 | 忒修斯之船", "en": "Tropical Adventures | Ship of Theseus"},
    "ta_climate": {"zh": "热带冒险气候", "en": "Tropical Adventures climates"},
})


def _ta(key, is_rule, zh, en, values, default, order, locations, *,
        atlas=_TA_SW_ATLAS, element=None, vanilla_icon=None, labels=None) -> ModWorldSetting:
    return ModWorldSetting(
        key=key, is_rule=is_rule, name={"zh": zh, "en": en}, values=values, mod_id=_TA_ID,
        icon_element=element, initial_value=default, locations=locations,
        group="ta_climate" if is_rule else "ta_worldgen", order=order,
        icon_atlas=None if vanilla_icon else atlas, vanilla_icon_key=vanilla_icon,
        value_labels=labels or _TA_ENABLE_LABELS,
    )


_TA_ENABLE = ["enabled", "disabled"]
_TA_DISABLED_ONLY = ["disabled"]

TROPICAL_ADVENTURES_SETTINGS: dict[str, ModWorldSetting] = {s.key: s for s in (
    # ── 世界生成（ta_worldgen，只读）──
    _ta("rog", False, "巨人国", "Region of Gaints", ["default", "fixed", "disabled"], "fixed", 1,
        _TA_FOREST, vanilla_icon="deerclops", labels={
            "default": {"zh": "默认", "en": "Default"},
            "fixed": {"zh": "固定的随机地形", "en": "Fxied Random Tasks"},
            "disabled": {"zh": "关闭(不推荐)", "en": "Disabled(Not Recommended)"},
        }),
    _ta("shipwrecked", False, "海难", "Shipwrecked", _TA_ENABLE, "enabled", 2, _TA_FOREST,
        element="birds.tex"),
    _ta("hamlet", False, "哈姆雷特", "Hamlet", _TA_ENABLE, "enabled", 3, _TA_FOREST,
        atlas=_TA_HAM_ATLAS, element="pig_houses.tex"),
    _ta("ocean_content", False, "联机海洋内容", "DST Ocean Contents", _TA_ENABLE, "enabled", 4,
        _TA_FOREST, element="blank_world.tex"),
    _ta("cave_content", False, "联机洞穴内容", "Together Caves", ["default", "part"], "default", 5,
        _TA_CAVE, element="blank_world.tex", labels={
            "default": {"zh": "默认", "en": "Default"},
            "part": {"zh": "没有楼梯", "en": "No ladder"},
        }),
    _ta("ruins", False, "猪人遗迹", "Pig Ruins", _TA_ENABLE, "enabled", 5.5, _TA_CAVE,
        element="blank_world.tex"),
    _ta("multiplayerportal", False, "绚丽之门位置", "Florid Postern location",
        ["rog", "shipwrecked", "hamlet"], "hamlet", 6, _TA_FOREST, vanilla_icon="spawnmode", labels={
            "rog": {"zh": "默认", "en": "Default"},
            "shipwrecked": {"zh": "海难区域", "en": "Shipwrecked region"},
            "hamlet": {"zh": "哈姆雷特区域", "en": "Hamlet region"},
        }),
    # data 是 Lua 数字，存档读入后按 str() 显示（1 → "1"、1.5 → "1.5"）
    _ta("world_size_multi", False, "世界面积乘数", "World area multi",
        ["0.5", "0.75", "1", "1.25", "1.5", "2"], "1.5", 8, _TA_BOTH,
        vanilla_icon="world_size", labels={
            "0.5": {"zh": "极小 , 0.5×", "en": "Tiny, 0.5×"},
            "0.75": {"zh": "更小, 0.75×", "en": "Smaller, 0.75×"},
            "1": {"zh": "默认, 1×", "en": "Default, 1×"},
            "1.25": {"zh": "更大, 1.25×", "en": "Larger, 1.25×"},
            "1.5": {"zh": "巨大, 1.5×", "en": "Huger, 1.5×"},
            "2": {"zh": "超巨大, 2×", "en": "xHuger, 2×"},
        }),
    _ta("coastline", False, "海岸线", "Coastline", _TA_ENABLE, "enabled", 9, _TA_FOREST,
        element="blank_world.tex", labels={
            "enabled": {"zh": "更平滑的海岸线", "en": "Smoother"},
            "disabled": {"zh": "默认", "en": "Default"},
        }),

    # ── 世界设置（ta_climate，可编辑）──
    _ta("wind", True, "海风", "Wind", _TA_ENABLE, "enabled", 11, _TA_FOREST, element="blank_world.tex"),
    _ta("hail", True, "冰雹", "Hail", _TA_ENABLE, "enabled", 12, _TA_FOREST, element="blank_world.tex"),
    _ta("waves", True, "海浪", "Waves", _TA_ENABLE, "enabled", 13, _TA_FOREST, element="waves.tex"),
    # flood/volcano 只有"关闭"一档（options_enable2），源码即如此
    _ta("flood", True, "洪水", "Flood", _TA_DISABLED_ONLY, "disabled", 14, _TA_FOREST,
        element="floods.tex"),
    _ta("volcano", True, "火山喷发", "Volcano Eruption", _TA_DISABLED_ONLY, "disabled", 15,
        _TA_FOREST, element="volcano.tex"),
    _ta("sealnado", True, "豹卷风", "sealnado", _TA_ENABLE, "enabled", 16, _TA_FOREST,
        element="twister.tex"),
    _ta("fog", True, "雾", "Fog", _TA_ENABLE, "enabled", 17, _TA_FOREST,
        atlas=_TA_HAM_ATLAS, element="fog.tex"),
    _ta("hayfever", True, "花粉过敏", "Hayfever", _TA_ENABLE, "disabled", 18, _TA_FOREST,
        atlas=_TA_HAM_ATLAS, element="hayfever.tex"),
    _ta("aporkalypse", True, "毁灭季", "Aporkalypse", _TA_ENABLE, "enabled", 19, _TA_BOTH,
        atlas=_TA_HAM_ATLAS, element="aporkalypse.tex"),
    _ta("roc", True, "大鹏", "ROC", _TA_ENABLE, "enabled", 20, _TA_FOREST,
        atlas=_TA_HAM_ATLAS, element="roc.tex"),
)}


# workshop id（不带 "workshop-" 前缀）-> 该 mod 贡献的世界设置登记表。
MOD_WORLD_SETTINGS: dict[str, dict[str, ModWorldSetting]] = {
    _CHERRY_FOREST_ID: CHERRY_FOREST_SETTINGS,
    _IA_CORE_ID: IA_CORE_SETTINGS,
    _IA_SHIPWRECKED_ID: IA_SHIPWRECKED_SETTINGS,
    _PORKLAND_ID: PORKLAND_SETTINGS,
    _BWB_ID: BENEATH_WORLD_BELOW_SETTINGS,
    _TA_ID: TROPICAL_ADVENTURES_SETTINGS,
}

# workshop id -> modinfo.lua 的 priority（未写按 0），本体 mods.lua 按它降序加载 Mod，
# 决定同名世界设置谁先注册。同 priority 时游戏按 modinfo.name 排序，已登记的 Mod 之间没有这种情况。
MOD_LOAD_PRIORITY: dict[str, float] = {
    _CHERRY_FOREST_ID: 0,
    _IA_CORE_ID: 5,
    _IA_SHIPWRECKED_ID: 4,
    _PORKLAND_ID: -1,
    _BWB_ID: -9999999,
    _TA_ID: -100,
}

# 深埋之下在前端修改了原版 OPTIONS：隐藏"大蠕虫"，把"石虾""洞穴蠕虫袭击"排到新增项之前。
# 已有存档里的 override 仍保留，只是不再作为可编辑项展示。
MOD_VANILLA_WORLD_PATCHES = {
    _BWB_ID: {
        CAVE_LOCATION: {
            "hidden": frozenset({"wormattacks_boss"}),
            "rule_order": {"rocky_setting": 1, "wormattacks": 2},
        },
    },
}

# workshop id -> Mod 显示名（中英文），用于世界设置分类标题和 Mod 列表名称本地化。
# 山河表里（3401927745）没有世界设置，只为 Mod 列表中文名保留。
MOD_DISPLAY_NAMES: dict[str, dict] = {
    _CHERRY_FOREST_ID: {"zh": "新版樱花林", "en": "Cherry Forest"},
    _IA_CORE_ID: {"zh": "岛屿冒险 - 核心", "en": "Island Adventures - Core"},
    _IA_SHIPWRECKED_ID: {"zh": "岛屿冒险 - 海难", "en": "Island Adventures - Shipwrecked"},
    _PORKLAND_ID: {"zh": "云霄国度", "en": "Above the Clouds"},
    _BWB_ID: {"zh": "深埋之下", "en": "Beneath the World Below"},
    _TA_ID: {"zh": "热带冒险 | 忒修斯之船", "en": "Tropical Adventures | Ship of Theseus"},
    "3401927745": {"zh": "山河表里", "en": "Montfluv"},
    # 以下两个没有世界设置，只为 Mod 列表名称本地化
    "3330056551": {"zh": "云霄国度补丁", "en": "Above the Clouds Patch"},
    "3826855460": {"zh": "三界同行 · 四模组独立补丁", "en": "Three Worlds Patch"},
}

# workshop id -> (图标图集 .xml 相对路径, 贴图 .tex 相对路径)，都是相对
# mod 文件夹自己的根目录。
MOD_ICON_ATLAS: dict[str, tuple[str, str]] = {
    _CHERRY_FOREST_ID: ("images/worldgen_cherry.xml", "images/worldgen_cherry.tex"),
    _IA_CORE_ID: ("images/hud/customization_core.xml", "images/hud/customization_core.tex"),
    _IA_SHIPWRECKED_ID: ("images/hud/customization_shipwrecked.xml", "images/hud/customization_shipwrecked.tex"),
    _PORKLAND_ID: ("images/hud/customization_porkland.xml", "images/hud/customization_porkland.tex"),
    _BWB_ID: ("images/worldsettings_customization_bwb.xml", "images/worldsettings_customization_bwb.tex"),
    _TA_ID: _TA_SW_ATLAS,
}


# workshop-3811652910 == 三合一测试mod（岛屿冒险核心/海难 + 云霄国度 + 补丁 + 兼容层整合版），
# 已核对到 1.2.8；作者已下架，改为补丁 3826855460（不注册世界设置，直接用四个原 Mod 的登记表），这里只为老存档保留。integrated_loader.lua 按
# core→sw→pl→patch→compat 在各自子环境执行原 Mod 入口；注册世界设置的 modservercreationmain.lua /
# modcustomizeitems.lua 及三张图集与本机独立版 3435352667 1.0.80 / 1467214795 1.0.63 /
# 3322803908 1.0.50 逐字节相同，patch/compat 不注册
# 新条目，故直接复用三张登记表。图集被 shared_assets.lua 映射到 Mod 根目录的同名路径。
# 同名 poison 按 core 先注册生效（AddCustomizeItem 遇已注册 name 直接 return）。
_THREE_WORLDS_ID = "3811652910"
THREE_WORLDS_SETTINGS: dict[str, ModWorldSetting] = {}
for _source_id, _source in (
    (_IA_CORE_ID, IA_CORE_SETTINGS),
    (_IA_SHIPWRECKED_ID, IA_SHIPWRECKED_SETTINGS),
    (_PORKLAND_ID, PORKLAND_SETTINGS),
):
    for _key, _info in _source.items():
        THREE_WORLDS_SETTINGS.setdefault(_key, replace(
            _info, mod_id=_THREE_WORLDS_ID,
            icon_atlas=_info.icon_atlas or MOD_ICON_ATLAS[_source_id],
        ))
MOD_WORLD_SETTINGS[_THREE_WORLDS_ID] = THREE_WORLDS_SETTINGS
MOD_LOAD_PRIORITY[_THREE_WORLDS_ID] = 5

# workshop id -> 登记表核对时的 modinfo version。本机版本不同时世界设置页提示"可能未适配"；
# Mod 更新后用 reference/dev_scripts/audit_world_settings.py 核对差异，改完登记表再更新这里。
MOD_VERIFIED_VERSIONS: dict[str, str] = {
    _CHERRY_FOREST_ID: "1.6.107",
    _IA_CORE_ID: "1.0.80",
    _IA_SHIPWRECKED_ID: "1.0.63",
    _PORKLAND_ID: "1.0.50",
    _BWB_ID: "0.4.16.6",
    _TA_ID: "26.09.19",
    _THREE_WORLDS_ID: "1.2.8",
}


def is_verified_version(mod_id: str, installed_version: str) -> bool:
    """本机版本是否就是登记表核对过的版本；比较方式同游戏 ModIndex（去首尾空白、转小写）。"""
    verified = MOD_VERIFIED_VERSIONS.get(mod_id)
    return verified is None or verified.strip().lower() == installed_version.strip().lower()


def get_mod_world_settings(
    enabled_mod_ids,
    location: str | None = None,
    is_master_world: bool = True,
) -> dict[str, ModWorldSetting]:
    """合并 ``enabled_mod_ids``（纯数字 ID）中已登记 Mod 的世界设置。

    按游戏加载顺序（MOD_LOAD_PRIORITY 降序）合并，同名 key 先注册的生效：本体 customize.lua 的
    AddCustomizeItem 开头 ``if GetItemFromName(name) ~= nil then return end``。先全量合并再按
    location 过滤，否则先注册项在当前世界不可见时会错误露出后注册的同名项。"""
    normalized = {str(mod_id).removeprefix("workshop-") for mod_id in enabled_mod_ids}
    merged: dict[str, ModWorldSetting] = {}
    ordered_ids = sorted(
        (mod_id for mod_id in MOD_WORLD_SETTINGS if mod_id in normalized),
        key=lambda mod_id: -MOD_LOAD_PRIORITY.get(mod_id, 0),
    )
    for mod_id in ordered_ids:
        settings = dict(MOD_WORLD_SETTINGS[mod_id])
        if mod_id == _BWB_ID and normalized.intersection(_BWB_DSTU_IDS):
            settings.update(BENEATH_WORLD_BELOW_DSTU_SETTINGS)
        for key, info in settings.items():
            merged.setdefault(key, info)
    if location is None:
        return merged
    return filter_mod_world_settings(merged, location, is_master_world)


def get_mod_vanilla_world_patches(
    mod_settings: dict[str, ModWorldSetting], location: str,
) -> tuple[set[str], dict[str, float], dict[str, float]]:
    """合并当前已启用 Mod 对原版目录的隐藏和排序补丁。"""
    active_mod_ids = {info.mod_id for info in mod_settings.values()}
    hidden: set[str] = set()
    rule_order: dict[str, float] = {}
    generation_order: dict[str, float] = {}
    for mod_id, location_patches in MOD_VANILLA_WORLD_PATCHES.items():
        if mod_id not in active_mod_ids:
            continue
        patch = location_patches.get(location)
        if not patch:
            continue
        hidden.update(patch.get("hidden", ()))
        rule_order.update(patch.get("rule_order", {}))
        generation_order.update(patch.get("generation_order", {}))
    return hidden, rule_order, generation_order


def filter_mod_world_settings(
    mod_settings: dict[str, ModWorldSetting],
    location: str,
    is_master_world: bool = True,
) -> dict[str, ModWorldSetting]:
    """按当前 location/分片过滤已经合并的登记表。"""
    return {
        key: info for key, info in mod_settings.items()
        if info.visible_in(location, is_master_world)
    }


def get_mod_categories(mod_settings: dict) -> list[tuple[str, dict]]:
    """为 Mod 自建组和未登记 group 的 Mod 各生成一条 (分类 key, 显示名)，按首次出现顺序去重。"""
    cats: list[tuple[str, dict]] = []
    seen: set[str] = set()
    for info in mod_settings.values():
        if info.group in MOD_CUSTOM_GROUP_NAMES:
            name = MOD_CUSTOM_GROUP_NAMES[info.group]
        elif info.group is None:
            name = MOD_DISPLAY_NAMES.get(info.mod_id, {"zh": info.mod_id, "en": info.mod_id})
        else:
            continue
        if info.category in seen:
            continue
        seen.add(info.category)
        cats.append((info.category, name))
    return cats
