"""世界规则各 key 的合法取值（按顺序）。

取值词表因 key 而异（5 档频率、季节长度、昼夜形态、开关等），用错列表循环切换会静默改坏设置，
所以逐 key 记录。来源是游戏 worldsettings_overrides.lua 的 tuning_vars 键（顺序与游戏 UI 一致），
走 applyoverrides_post 直接转发原始值的条目为手工从源码读出。

排列顺序与 categories.py / icons.py 一致（先森林规则顺序，再补洞穴专属 key），便于三份表对照修改。
只服务可编辑的"世界规则"，"世界生成"见 GEN_VALUE_SETS。
"""

WORLDGEN_FREQUENCY_SET = [
    "never", "rare", "uncommon", "default", "often", "mostly", "always", "insane",
]

VALUE_SETS = {
    # ══════════════ 森林-世界规则 (对照 FOREST_RULES_DICT 顺序) ══════════════
    # 全局|global
    "specialevent": ["none", "default"],
    "autumn": ["noseason", "veryshortseason", "shortseason", "default", "longseason", "verylongseason", "random"],
    "winter": ["noseason", "veryshortseason", "shortseason", "default", "longseason", "verylongseason", "random"],
    "spring": ["noseason", "veryshortseason", "shortseason", "default", "longseason", "verylongseason", "random"],
    "summer": ["noseason", "veryshortseason", "shortseason", "default", "longseason", "verylongseason", "random"],
    "day": ["default", "longday", "longdusk", "longnight", "noday", "nodusk", "nonight", "onlyday", "onlydusk", "onlynight"],
    # 出生模式真实只有 2 档：fixed(绚丽之门)/scatter(随机)，customize.lua 的
    # spawnmode_descriptions 确认。
    "spawnmode": ["fixed", "scatter"],
    # 以下三项游戏里只有 2 个真实选项："default" 是 "always" 的内部别名（见 worldsettings_overrides.lua）
    "ghostenabled": ["none", "always"],
    "portalresurection": ["none", "always"],
    "ghostsanitydrain": ["none", "always"],
    "resettime": ["none", "slow", "default", "fast", "always"],
    "beefaloheat": ["never", "rare", "default", "often", "always"],
    "krampus": ["never", "rare", "default", "often", "always"],
    # 活动|events
    "crow_carnival": ["default", "enabled"],
    # TODO（未确认）：这些年度活动 key 是否在自定义世界界面中独立可调，还是由 specialevent 统一控制
    "hallowed_nights": ["default", "enabled"],
    "winters_feast": ["default", "enabled"],
    "year_of_the_gobbler": ["default", "enabled"],
    "year_of_the_varg": ["default", "enabled"],
    "year_of_the_pig": ["default", "enabled"],
    "year_of_the_carrat": ["default", "enabled"],
    "year_of_the_beefalo": ["default", "enabled"],
    "year_of_the_catcoon": ["default", "enabled"],
    "year_of_the_bunnyman": ["default", "enabled"],
    "year_of_the_dragonfly": ["default", "enabled"],
    "year_of_the_snake": ["default", "enabled"],
    "year_of_the_knight": ["default", "enabled"],
    # 冒险家|survivor
    # 游戏源码中 DAY_10 的 data 实际是 "default"，不是 "10"。
    "extrastartingitems": ["0", "5", "default", "15", "20", "none"],
    "seasonalstartingitems": ["never", "default"],
    "spawnprotection": ["never", "default", "always"],
    "dropeverythingondespawn": ["default", "always"],
    "healthpenalty": ["none", "always"],
    "lessdamagetaken": ["always", "none", "more"],
    "temperaturedamage": ["nonlethal", "default"],
    "hunger": ["nonlethal", "default"],
    "darkness": ["nonlethal", "default"],
    "shadowcreatures": ["never", "rare", "default", "often", "always"],
    "brightmarecreatures": ["never", "rare", "default", "often", "always"],
    # 世界|world
    "hounds": ["never", "rare", "default", "often", "always"],
    "winterhounds": ["never", "default"],
    "summerhounds": ["never", "default"],
    "lunarhail_frequency": ["never", "rare", "default", "often", "always"],
    "petrification": ["none", "few", "default", "many", "max"],
    "meteorshowers": ["never", "rare", "default", "often", "always"],
    "wanderingtrader_enabled": ["none", "always"],
    "hunt": ["never", "rare", "default", "often", "always"],
    "alternatehunt": ["never", "rare", "default", "often", "always"],
    "rifts_enabled": ["never", "default", "always"],
    "rifts_frequency": ["never", "rare", "default", "often", "always"],
    "wildfires": ["never", "rare", "default", "often", "always"],
    "lightning": ["never", "rare", "default", "often", "always"],
    "weather": ["never", "rare", "default", "often", "always"],
    "frograin": ["never", "rare", "default", "often", "always"],
    # 资源再生|regrowth
    "regrowth": ["never", "veryslow", "slow", "default", "fast", "veryfast"],
    "cactus_regrowth": ["never", "veryslow", "slow", "default", "fast", "veryfast"],
    "basicresource_regrowth": ["none", "always"],
    "twiggytrees_regrowth": ["never", "veryslow", "slow", "default", "fast", "veryfast"],
    "evergreen_regrowth": ["never", "veryslow", "slow", "default", "fast", "veryfast"],
    "moon_tree_regrowth": ["never", "veryslow", "slow", "default", "fast", "veryfast"],
    "deciduoustree_regrowth": ["never", "veryslow", "slow", "default", "fast", "veryfast"],
    "palmconetree_regrowth": ["never", "veryslow", "slow", "default", "fast", "veryfast"],
    "saltstack_regrowth": ["never", "veryslow", "slow", "default", "fast", "veryfast"],
    "carrots_regrowth": ["never", "veryslow", "slow", "default", "fast", "veryfast"],
    "reeds_regrowth": ["never", "veryslow", "slow", "default", "fast", "veryfast"],
    "flowers_regrowth": ["never", "veryslow", "slow", "default", "fast", "veryfast"],
    # 非自然传送门资源|portal_resources
    "portal_spawnrate": ["never", "rare", "default", "often", "always"],
    "lightcrab_portalrate": ["never", "rare", "default", "often", "always"],
    "palmcone_seed_portalrate": ["never", "rare", "default", "often", "always"],
    "powder_monkey_portalrate": ["never", "rare", "default", "often", "always"],
    "monkeytail_portalrate": ["never", "rare", "default", "often", "always"],
    "bananabush_portalrate": ["never", "rare", "default", "often", "always"],
    # 生物|creatures
    "gnarwail": ["never", "rare", "default", "often", "always"],
    "penguins": ["never", "rare", "default", "often", "always"],
    "bunnymen_setting": ["never", "rare", "default", "often", "always"],
    "rabbits_setting": ["never", "rare", "default", "often", "always"],
    "otters_setting": ["never", "rare", "default", "often", "always"],
    "catcoons": ["never", "rare", "default", "often", "always"],
    "perd": ["never", "rare", "default", "often", "always"],
    "pigs_setting": ["never", "rare", "default", "often", "always"],
    "grassgekkos": ["never", "rare", "default", "often", "always"],
    "bees_setting": ["never", "rare", "default", "often", "always"],
    "butterfly": ["never", "rare", "default", "often", "always"],
    "fishschools": ["never", "rare", "default", "often", "always"],
    "birds": ["never", "rare", "default", "often", "always"],
    "moles_setting": ["never", "rare", "default", "often", "always"],
    "wobsters": ["never", "rare", "default", "often", "always"],
    # 敌对生物|hostile_creatures
    "pirateraids": ["never", "rare", "default", "often", "always"],
    "wasps": ["never", "rare", "default", "often", "always"],
    "walrus_setting": ["never", "rare", "default", "often", "always"],
    "hound_mounds": ["never", "rare", "default", "often", "always"],
    "mosquitos": ["never", "rare", "default", "often", "always"],
    "spiders_setting": ["never", "rare", "default", "often", "always"],
    "spider_warriors": ["never", "default"],
    "bats_setting": ["never", "rare", "default", "often", "always"],
    "frogs": ["never", "rare", "default", "often", "always"],
    "lureplants": ["never", "rare", "default", "often", "always"],
    "cookiecutters": ["never", "rare", "default", "often", "always"],
    "merms": ["never", "rare", "default", "often", "always"],
    "squid": ["never", "rare", "default", "often", "always"],
    "sharks": ["never", "rare", "default", "often", "always"],
    # 巨兽|bosses
    "klaus": ["never", "rare", "default", "often", "always"],
    "sharkboi": ["never", "rare", "default", "often", "always"],
    "crabking": ["never", "rare", "default", "often", "always"],
    "eyeofterror": ["never", "rare", "default", "often", "always"],
    "daywalker2": ["never", "rare", "default", "often", "always"],
    "fruitfly": ["never", "rare", "default", "often", "always"],
    "liefs": ["never", "rare", "default", "often", "always"],
    "deciduousmonster": ["never", "rare", "default", "often", "always"],
    "bearger": ["never", "rare", "default", "often", "always"],
    "deerclops": ["never", "rare", "default", "often", "always"],
    "antliontribute": ["never", "rare", "default", "often", "always"],
    "beequeen": ["never", "rare", "default", "often", "always"],
    "spiderqueen": ["never", "rare", "default", "often", "always"],
    "malbatross": ["never", "rare", "default", "often", "always"],
    "goosemoose": ["never", "rare", "default", "often", "always"],
    "dragonfly": ["never", "rare", "default", "often", "always"],
    # 月亮变异|lunar
    "mutated_bird_gestalt": ["never", "default"],
    "mutated_birds": ["never", "default"],
    "mutated_merm": ["never", "default"],
    "mutated_hounds": ["never", "default"],
    "mutated_deerclops": ["never", "default"],
    "mutated_buzzard_gestalt": ["never", "default"],
    "penguins_moon": ["never", "default"],
    "moon_spider": ["never", "rare", "default", "often", "always"],
    # mutated_spiderqueen（森林/洞穴共用）：可调项是它而不是 moon_spiders；取值按同组 mutated_* 推断为 never/default
    "mutated_spiderqueen": ["never", "default"],
    "mutated_bearger": ["never", "default"],
    "mutated_warg": ["never", "default"],

    # ══════════ 洞穴-世界规则（森林没有的专属 key，按 CAVE_RULES_DICT 顺序） ══════════
    # 世界|world
    "earthquakes": ["never", "rare", "default", "often", "always"],
    # 取值同常规五档，只是中文文案不同（从不/稀有/常见）
    "wormattacks_boss": ["never", "rare", "default", "often", "always"],
    "wormattacks": ["never", "rare", "default", "often", "always"],
    "rifts_enabled_cave": ["never", "default", "always"],
    "rifts_frequency_cave": ["never", "rare", "default", "often", "always"],
    "atriumgate": ["veryslow", "slow", "default", "fast", "veryfast"],
    "acidrain_enabled": ["none", "always"],
    # 资源再生|regrowth
    "lightflier_flower_regrowth": ["never", "veryslow", "slow", "default", "fast", "veryfast"],
    "tree_rock_regrowth": ["never", "veryslow", "slow", "default", "fast", "veryfast"],
    "mushtree_moon_regrowth": ["never", "veryslow", "slow", "default", "fast", "veryfast"],
    "flower_cave_regrowth": ["never", "veryslow", "slow", "default", "fast", "veryfast"],
    "mushtree_regrowth": ["never", "veryslow", "slow", "default", "fast", "veryfast"],
    # 生物|creatures
    "dustmoths": ["never", "rare", "default", "often", "always"],
    "lightfliers": ["never", "rare", "default", "often", "always"],
    "rocky_setting": ["never", "rare", "default", "often", "always"],
    "monkey_setting": ["never", "rare", "default", "often", "always"],
    "mushgnome": ["never", "rare", "default", "often", "always"],
    "slurtles_setting": ["never", "rare", "default", "often", "always"],
    "snurtles": ["never", "rare", "default", "often", "always"],
    # 敌对生物|hostile_creatures
    "spider_spitter": ["never", "rare", "default", "often", "always"],
    "itemmimics": ["never", "rare", "default", "often", "always"],
    "chest_mimics": ["never", "rare", "default", "often", "always"],
    "spider_hider": ["never", "rare", "default", "often", "always"],
    "spider_dropper": ["never", "rare", "default", "often", "always"],
    "molebats": ["never", "rare", "default", "often", "always"],
    "bat_boss": ["never", "rare", "default", "often", "always"],
    "rocky_boss": ["never", "rare", "default", "often", "always"],
    "nightmarecreatures": ["never", "rare", "default", "often", "always"],
    # 巨兽|bosses
    "daywalker": ["never", "rare", "default", "often", "always"],
    "toadstool": ["never", "rare", "default", "often", "always"],
}

# 世界生成（只读）设置的取值：不参与规则循环，但创建存档和只读展示需要正确取值，不能退回五档频率
GEN_VALUE_SETS = {
    # task_set/start_location 的森林/洞穴取值是不同子集（见 get_value_set 按 location 分支），这里的合并值仅兜底
    "task_set": ["default", "classic", "cave_default", "porkland"],
    "start_location": ["default", "plus", "darkness", "caves", "PorkLandStart"],
    "world_size": ["small", "medium", "default", "huge"],
    "branching": ["never", "least", "default", "most", "random"],
    "loop": ["never", "default", "always"],
    "roads": ["never", "default"],
    "season_start": [
        "default", "winter", "spring", "summer", "autumn|spring",
        "winter|summer", "autumn|winter|spring|summer",
    ],
    "prefabswaps_start": ["classic", "default", "highly random"],
    "touchstone": WORLDGEN_FREQUENCY_SET,
    "boons": WORLDGEN_FREQUENCY_SET,
    # 洞穴光照是"世界生成(只读)"项，且是 speed_descriptions 六档
    # （customize.lua 确认，末档 veryfast 而非 always）。
    "cavelight": ["never", "veryslow", "slow", "default", "fast", "veryfast"],
    "junkyard": ["never", "default"],
    "terrariumchest": ["never", "default"],
    "balatro": ["never", "default"],
    "stageplays": ["never", "default"],
}

OCEAN_WORLDGEN_SET = [f"ocean_{value}" for value in WORLDGEN_FREQUENCY_SET]
# 只有海洋生成频率类 key 使用 ``ocean_`` 前缀取值；ocean_otterdens 等实体密度仍用普通频率值
OCEAN_FREQUENCY_KEYS = {"ocean_seastack", "ocean_waterplant"}

def get_value_set(
    key: str, mod_settings: dict | None = None,
    location: str | None = None, is_rule: bool = True,
) -> list[str]:
    """取世界规则 key 的合法取值列表：Mod 登记的取值优先（不一定是标准 5 档，如樱花林的
    cherry_bugseason 只有两档），原版未特殊登记的退回标准 5 档。
    """
    if mod_settings:
        info = mod_settings.get(key)
        if info is not None and info.values:
            return info.values
    # 岛屿冒险把这两项固定为各自世界生成所需的唯一值，循环到其他 task_set 会导致世界生成崩溃
    if not is_rule and location == "shipwrecked":
        if key == "task_set":
            return ["shipwrecked"]
        if key == "start_location":
            return ["shipwrecked_default", "shipwrecked_plus", "shipwrecked_darkness"]
    if not is_rule and location == "volcanoworld":
        if key == "task_set":
            return ["volcano"]
        if key == "start_location":
            return ["volcano_default"]
    # 原版森林/洞穴的 task_set/start_location 各取不同子集
    if not is_rule and key == "task_set":
        if location == "cave":
            return ["cave_default"]
        return ["default", "classic"]
    if not is_rule and key == "start_location":
        if location == "cave":
            return ["caves"]
        return ["default", "plus", "darkness"]
    if key in OCEAN_FREQUENCY_KEYS:
        return OCEAN_WORLDGEN_SET
    if not is_rule:
        return GEN_VALUE_SETS.get(key, WORLDGEN_FREQUENCY_SET)
    values = VALUE_SETS.get(key)
    if values is not None:
        return values
    # 海洋生成 key 的取值是 worldgen 频率表加 ocean_ 前缀，新增的 ocean_* 也不能退回普通 5 档；
    # 其余原版资源密度类统一用完整的 worldgen 频率表兜底，避免新 key（如 angrybees）丢失
    # uncommon/mostly/insane 档
    return WORLDGEN_FREQUENCY_SET


def _parse_number(text: str) -> int | float | None:
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def is_mod_readonly(key: str, mod_settings: dict | None) -> bool:
    """Mod 登记了该条但没有取值列表（values=None）：只展示，不能循环到通用档位。"""
    info = mod_settings.get(key) if mod_settings else None
    return info is not None and info.values is None


def typed_override_value(value: str, values: list[str]):
    """写入 leveldataoverride 前还原 Lua 类型：取值表全是数字时（如热带冒险 world_size_multi 的
    0.5~2）写成数字，Mod 才能按数值使用；原版取值表没有这种情况，其余一律保持字符串。"""
    if values and all(_parse_number(item) is not None for item in values):
        number = _parse_number(value)
        if number is not None:
            return number
    return value
