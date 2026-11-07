"""世界设置分类、中英文名与排序。

字典完整性已用真实 Master/Caves 的 leveldataoverride.lua key 全集核对。洞穴存档里也会写入
day/specialevent 等森林专属规则（同步的备份值），但洞穴界面不显示它们，所以不收进洞穴表。
英文名按游戏"自定义世界"界面措辞翻译。
"""

from dstools.i18n import get_lang

# ── 分类定义 ─────────────────────────────────────────────────────────────

SURFACE_RULES = [
    ("global", {"zh": "全局", "en": "General"}),
    ("porkland_global", {"zh": "猪镇全局", "en": "Porkland General"}),
    ("events", {"zh": "活动", "en": "Events"}),
    ("survivor", {"zh": "冒险家", "en": "Survivor"}),
    ("world", {"zh": "世界", "en": "World"}),
    ("regrowth", {"zh": "资源再生", "en": "Regrowth"}),
    ("portal_resources", {"zh": "非自然传送门资源", "en": "Portal Resources"}),
    ("creatures", {"zh": "生物", "en": "Creatures"}),
    ("hostile_creatures", {"zh": "敌对生物", "en": "Hostile Creatures"}),
    ("bosses", {"zh": "巨兽", "en": "Giants"}),
    ("lunar", {"zh": "月亮变异", "en": "Lunar Mutations"}),
]

SURFACE_GEN = [
    ("global", {"zh": "全局", "en": "General"}),
    ("world", {"zh": "世界", "en": "World"}),
    ("resources", {"zh": "资源", "en": "Resources"}),
    ("creatures_spawners", {"zh": "生物以及刷新点", "en": "Creatures & Spawners"}),
    ("hostile_spawners", {"zh": "敌对生物以及刷新点", "en": "Hostile Creatures & Spawners"}),
]

CAVE_RULES = [
    ("world", {"zh": "世界", "en": "World"}),
    ("regrowth", {"zh": "资源再生", "en": "Regrowth"}),
    ("creatures", {"zh": "生物", "en": "Creatures"}),
    ("hostile_creatures", {"zh": "敌对生物", "en": "Hostile Creatures"}),
    ("bosses", {"zh": "巨兽", "en": "Giants"}),
    ("lunar", {"zh": "月亮变异", "en": "Lunar Mutations"}),
]

CAVE_GEN = [
    ("world", {"zh": "世界", "en": "World"}),
    ("resources", {"zh": "资源", "en": "Resources"}),
    ("creatures_spawners", {"zh": "生物以及刷新点", "en": "Creatures & Spawners"}),
    ("hostile_spawners", {"zh": "敌对生物以及刷新点", "en": "Hostile Creatures & Spawners"}),
]

CATEGORY_COLORS = {
    "global": "#607d8b", "porkland_global": "#c2185b", "events": "#ff9800", "survivor": "#e91e63",
    "world": "#4caf50", "regrowth": "#8bc34a", "portal_resources": "#9c27b0",
    "creatures": "#2196f3", "hostile_creatures": "#f44336",
    "bosses": "#d32f2f", "lunar": "#00bcd4",
    "resources": "#009688", "creatures_spawners": "#1565c0",
    "hostile_spawners": "#c62828",
    # 每个贡献世界设置的 Mod 各占一个分类（f"mod_{workshop_id}"）及颜色；新增 Mod 支持时在这里补一行
    "mod_1289779251": "#795548",  # 新版樱花林(Cherry Forest)
    "mod_3435352667": "#3949ab",  # 岛屿冒险 - 核心(Island Adventures - Core)
    "mod_1467214795": "#00838f",  # 岛屿冒险 - 海难(Island Adventures - Shipwrecked)
    "mod_3322803908": "#ad1457",  # 云霄国度(Above the Clouds / Porkland)
}

# ── 森林-世界规则 ──
# 顺序、分类和 icons.py 的 FOREST_RULES_ICONS 保持一致，方便对照查看。
FOREST_RULES_DICT = {
    # 全局|global
    "specialevent": ("global", {"zh": "活动", "en": "Special Event"}),
    "autumn": ("global", {"zh": "秋", "en": "Autumn"}),
    "winter": ("global", {"zh": "冬", "en": "Winter"}),
    "spring": ("global", {"zh": "春", "en": "Spring"}),
    "summer": ("global", {"zh": "夏", "en": "Summer"}),
    "day": ("global", {"zh": "昼夜选项", "en": "Day/Night Cycle"}),
    "spawnmode": ("global", {"zh": "出生模式", "en": "Spawn Mode"}),
    "ghostenabled": ("global", {"zh": "冒险家死亡", "en": "Player Death"}),
    "portalresurection": ("global", {"zh": "在绚丽之门复活", "en": "Resurrection at Portal"}),
    "ghostsanitydrain": ("global", {"zh": "鬼魂理智值惩罚", "en": "Ghost Sanity Drain"}),
    "resettime": ("global", {"zh": "死亡重置倒计时", "en": "Reset Time"}),
    "beefaloheat": ("global", {"zh": "皮弗娄牛交配频率", "en": "Beefalo Heat"}),
    "krampus": ("global", {"zh": "坎普斯", "en": "Krampus"}),
    # 活动|events（游戏中"盛夏嘉年华"的真实 key 是 crow_carnival）
    "crow_carnival": ("events", {"zh": "盛夏嘉年华", "en": "Crow Carnival"}),
    "hallowed_nights": ("events", {"zh": "万圣夜", "en": "Hallowed Nights"}),
    "winters_feast": ("events", {"zh": "冬季盛宴", "en": "Winter's Feast"}),
    "year_of_the_gobbler": ("events", {"zh": "火鸡之年", "en": "Year of the Gobbler"}),
    "year_of_the_varg": ("events", {"zh": "座狼之年", "en": "Year of the Varg"}),
    "year_of_the_pig": ("events", {"zh": "猪王之年", "en": "Year of the Pig"}),
    "year_of_the_carrat": ("events", {"zh": "胡萝卜鼠之年", "en": "Year of the Carrat"}),
    "year_of_the_beefalo": ("events", {"zh": "皮弗娄牛之年", "en": "Year of the Beefalo"}),
    "year_of_the_catcoon": ("events", {"zh": "浣猫之年", "en": "Year of the Catcoon"}),
    "year_of_the_bunnyman": ("events", {"zh": "兔人之年", "en": "Year of the Bunnyman"}),
    "year_of_the_dragonfly": ("events", {"zh": "龙蝇之年", "en": "Year of the Dragonfly"}),
    "year_of_the_snake": ("events", {"zh": "洞穴蠕虫之年", "en": "Year of the Snake"}),
    "year_of_the_knight": ("events", {"zh": "发条骑士之年", "en": "Year of the Knight"}),
    # 冒险家|survivor
    "extrastartingitems": ("survivor", {"zh": "额外起始资源", "en": "Extra Starting Items"}),
    "seasonalstartingitems": ("survivor", {"zh": "季节起始物品", "en": "Seasonal Starting Items"}),
    "spawnprotection": ("survivor", {"zh": "防骚扰出生保护", "en": "Spawn Protection"}),
    "dropeverythingondespawn": ("survivor", {"zh": "离开游戏后物品掉落", "en": "Drop Everything on Despawn"}),
    "healthpenalty": ("survivor", {"zh": "生命值上限惩罚", "en": "Health Penalty"}),
    "lessdamagetaken": ("survivor", {"zh": "受到的伤害", "en": "Damage Taken"}),
    "temperaturedamage": ("survivor", {"zh": "温度伤害", "en": "Temperature Damage"}),
    "hunger": ("survivor", {"zh": "饥饿伤害", "en": "Hunger Damage"}),
    "darkness": ("survivor", {"zh": "黑暗伤害", "en": "Darkness Damage"}),
    "shadowcreatures": ("survivor", {"zh": "理智怪兽", "en": "Sanity Monsters"}),
    "brightmarecreatures": ("survivor", {"zh": "启蒙怪兽", "en": "Brightmare Monsters"}),
    # 世界|world
    "hounds": ("world", {"zh": "猎犬袭击", "en": "Hound Attacks"}),
    "winterhounds": ("world", {"zh": "寒冰猎犬群", "en": "Icehound Packs"}),
    "summerhounds": ("world", {"zh": "火焰猎犬群", "en": "Firehound Packs"}),
    "lunarhail_frequency": ("world", {"zh": "月雹", "en": "Moon Hail Frequency"}),
    "petrification": ("world", {"zh": "森林石化", "en": "Petrification"}),
    "meteorshowers": ("world", {"zh": "流星频率", "en": "Meteor Showers"}),
    "wanderingtrader_enabled": ("world", {"zh": "流浪商人", "en": "Wandering Trader"}),
    "hunt": ("world", {"zh": "狩猎", "en": "Hunting"}),
    "alternatehunt": ("world", {"zh": "狩猎惊喜", "en": "Hunt Surprises"}),
    "rifts_enabled": ("world", {"zh": "荒野裂隙", "en": "Wilderness Rifts"}),
    "rifts_frequency": ("world", {"zh": "荒野裂隙频率", "en": "Wilderness Rifts Frequency"}),
    "wildfires": ("world", {"zh": "野火", "en": "Wildfires"}),
    "lightning": ("world", {"zh": "闪电", "en": "Lightning"}),
    "weather": ("world", {"zh": "雨", "en": "Rain"}),
    "frograin": ("world", {"zh": "青蛙雨", "en": "Frog Rain"}),
    # 资源再生|regrowth
    "regrowth": ("regrowth", {"zh": "再生速度", "en": "Regrowth Rate"}),
    "cactus_regrowth": ("regrowth", {"zh": "仙人掌", "en": "Cactus"}),
    "basicresource_regrowth": ("regrowth", {"zh": "基础资源", "en": "Basic Resources"}),
    "twiggytrees_regrowth": ("regrowth", {"zh": "多枝树", "en": "Twiggy Trees"}),
    "evergreen_regrowth": ("regrowth", {"zh": "常青树", "en": "Evergreens"}),
    "moon_tree_regrowth": ("regrowth", {"zh": "月树", "en": "Moon Trees"}),
    "deciduoustree_regrowth": ("regrowth", {"zh": "桦栗树", "en": "Deciduous Trees"}),
    "palmconetree_regrowth": ("regrowth", {"zh": "棕榈松果树", "en": "Palm Trees"}),
    "saltstack_regrowth": ("regrowth", {"zh": "盐堆", "en": "Salt Formations"}),
    "carrots_regrowth": ("regrowth", {"zh": "胡萝卜", "en": "Carrots"}),
    "reeds_regrowth": ("regrowth", {"zh": "芦苇", "en": "Reeds"}),
    "flowers_regrowth": ("regrowth", {"zh": "花", "en": "Flowers"}),
    # 非自然传送门资源|portal_resources
    "portal_spawnrate": ("portal_resources", {"zh": "传送频率", "en": "Portal Spawn Rate"}),
    "lightcrab_portalrate": ("portal_resources", {"zh": "发光蟹", "en": "Lightcrab"}),
    "palmcone_seed_portalrate": ("portal_resources", {"zh": "棕榈松果树芽", "en": "Palm Cone Sprout"}),
    "powder_monkey_portalrate": ("portal_resources", {"zh": "火药猴", "en": "Powder Monkey"}),
    "monkeytail_portalrate": ("portal_resources", {"zh": "猴尾草", "en": "Monkey Tail Plant"}),
    "bananabush_portalrate": ("portal_resources", {"zh": "香蕉丛", "en": "Banana Bush"}),
    # 生物|creatures
    "gnarwail": ("creatures", {"zh": "一角鲸", "en": "Gnarwail"}),
    "penguins": ("creatures", {"zh": "企鹅", "en": "Penguins"}),
    "bunnymen_setting": ("creatures", {"zh": "兔人", "en": "Bunnymen"}),
    "rabbits_setting": ("creatures", {"zh": "兔子", "en": "Rabbits"}),
    "otters_setting": ("creatures", {"zh": "水獭掠夺者", "en": "Otters"}),
    "catcoons": ("creatures", {"zh": "浣猫", "en": "Catcoons"}),
    "perd": ("creatures", {"zh": "火鸡", "en": "Perds"}),
    "pigs_setting": ("creatures", {"zh": "猪", "en": "Pigs"}),
    "grassgekkos": ("creatures", {"zh": "草壁虎转化", "en": "Grass Gekkos"}),
    "bees_setting": ("creatures", {"zh": "蜜蜂", "en": "Bees"}),
    "butterfly": ("creatures", {"zh": "蝴蝶", "en": "Butterflies"}),
    "fishschools": ("creatures", {"zh": "鱼群", "en": "Fish Schools"}),
    "birds": ("creatures", {"zh": "鸟", "en": "Birds"}),
    "moles_setting": ("creatures", {"zh": "鼹鼠", "en": "Moles"}),
    "wobsters": ("creatures", {"zh": "龙虾", "en": "Wobsters"}),
    # 敌对生物|hostile_creatures
    "pirateraids": ("hostile_creatures", {"zh": "月亮码头海盗", "en": "Pirate Raids"}),
    "wasps": ("hostile_creatures", {"zh": "杀人蜂", "en": "Killer Bees"}),
    "walrus_setting": ("hostile_creatures", {"zh": "海象", "en": "Walruses"}),
    "hound_mounds": ("hostile_creatures", {"zh": "猎犬", "en": "Hound Mounds"}),
    "mosquitos": ("hostile_creatures", {"zh": "蚊子", "en": "Mosquitoes"}),
    "spiders_setting": ("hostile_creatures", {"zh": "蜘蛛", "en": "Spiders"}),
    "spider_warriors": ("hostile_creatures", {"zh": "蜘蛛战士", "en": "Spider Warriors"}),
    "bats_setting": ("hostile_creatures", {"zh": "蝙蝠", "en": "Bats"}),
    "frogs": ("hostile_creatures", {"zh": "青蛙", "en": "Frogs"}),
    "lureplants": ("hostile_creatures", {"zh": "食人花", "en": "Lure Plants"}),
    "cookiecutters": ("hostile_creatures", {"zh": "饼干切割机", "en": "Cookie Cutters"}),
    "merms": ("hostile_creatures", {"zh": "鱼人", "en": "Merms"}),
    "squid": ("hostile_creatures", {"zh": "鱿鱼", "en": "Squid"}),
    "sharks": ("hostile_creatures", {"zh": "鲨鱼", "en": "Sharks"}),
    # 巨兽|bosses
    "klaus": ("bosses", {"zh": "克劳斯", "en": "Klaus"}),
    "sharkboi": ("bosses", {"zh": "大霜鲨", "en": "Sharkboi"}),
    "crabking": ("bosses", {"zh": "帝王蟹", "en": "Crab King"}),
    "eyeofterror": ("bosses", {"zh": "恐怖之眼", "en": "Eye of Terror"}),
    "daywalker2": ("bosses", {"zh": "拾荒疯猪", "en": "Scrappy Werepig"}),
    "fruitfly": ("bosses", {"zh": "果蝇王", "en": "Fruit Fly King"}),
    "liefs": ("bosses", {"zh": "树精守卫", "en": "Treeguards"}),
    "deciduousmonster": ("bosses", {"zh": "毒桦栗树", "en": "Poison Birchnutter"}),
    "bearger": ("bosses", {"zh": "熊獾", "en": "Bearger"}),
    "deerclops": ("bosses", {"zh": "独眼巨鹿", "en": "Deerclops"}),
    "antliontribute": ("bosses", {"zh": "蚁狮贡品", "en": "Antlion Tribute"}),
    "beequeen": ("bosses", {"zh": "蜂王", "en": "Bee Queen"}),
    "spiderqueen": ("bosses", {"zh": "蜘蛛女王", "en": "Spider Queen"}),
    "malbatross": ("bosses", {"zh": "邪天翁", "en": "Malbatross"}),
    "goosemoose": ("bosses", {"zh": "麋鹿鹅", "en": "Moose/Goose"}),
    "dragonfly": ("bosses", {"zh": "龙蝇", "en": "Dragonfly"}),
    # 月亮变异|lunar
    "mutated_bird_gestalt": ("lunar", {"zh": "亮喙鸟", "en": "Lunar Tallbird"}),
    "mutated_birds": ("lunar", {"zh": "变异狗鸟", "en": "Mutated Birds"}),
    "mutated_merm": ("lunar", {"zh": "变异鱼人", "en": "Mutated Merms"}),
    "mutated_hounds": ("lunar", {"zh": "恐怖猎犬", "en": "Mutated Hounds"}),
    "mutated_deerclops": ("lunar", {"zh": "晶体独眼巨鹿", "en": "Mutated Deerclops"}),
    "mutated_buzzard_gestalt": ("lunar", {"zh": "水晶冠秃鹫", "en": "Mutated Buzzards"}),
    "penguins_moon": ("lunar", {"zh": "冰冻企鹅", "en": "Mutated Penguins"}),
    "moon_spider": ("lunar", {"zh": "破碎蜘蛛", "en": "Mutated Spiders"}),
    # 森林可调的是 mutated_spiderqueen（与洞穴相同），moon_spiders 属于世界生成
    "mutated_spiderqueen": ("lunar", {"zh": "破碎蜘蛛洞", "en": "Mutated Spider Den"}),
    "mutated_bearger": ("lunar", {"zh": "装甲熊獾", "en": "Mutated Bearger"}),
    "mutated_warg": ("lunar", {"zh": "附身座狼", "en": "Mutated Warg"}),
}

# ── 森林-世界生成 ──
# 顺序、分类和 icons.py 的 FOREST_GEN_ICONS 保持一致。
FOREST_GEN_DICT = {
    # 全局|global
    "season_start": ("global", {"zh": "起始季节", "en": "Starting Season"}),
    # 世界|world
    "task_set": ("world", {"zh": "生物群落", "en": "Biomes"}),
    "start_location": ("world", {"zh": "出生点", "en": "Start Location"}),
    "world_size": ("world", {"zh": "世界大小", "en": "World Size"}),
    "branching": ("world", {"zh": "分支", "en": "Branching"}),
    "loop": ("world", {"zh": "环形", "en": "Loop"}),
    "roads": ("world", {"zh": "道路", "en": "Roads"}),
    "touchstone": ("world", {"zh": "试金石", "en": "Touch Stones"}),
    "boons": ("world", {"zh": "失败的冒险家", "en": "Failed Adventurer"}),
    "prefabswaps_start": ("world", {"zh": "开始资源多样化", "en": "Starting Resource Variety"}),
    "junkyard": ("world", {"zh": "垃圾场", "en": "Junk Yard"}),
    "moon_fissure": ("world", {"zh": "天体裂缝", "en": "Celestial Rifts"}),
    "balatro": ("world", {"zh": "小丑", "en": "Clown"}),
    "terrariumchest": ("world", {"zh": "盒中泰拉", "en": "Terrarium"}),
    "stageplays": ("world", {"zh": "舞台剧", "en": "Stage Plays"}),
    # 资源|resources
    "cactus": ("resources", {"zh": "仙人掌", "en": "Cactus"}),
    "ocean_bullkelp": ("resources", {"zh": "公牛海带", "en": "Bull Kelp"}),
    "marshbush": ("resources", {"zh": "尖刺灌木", "en": "Spiky Bush"}),
    "rock": ("resources", {"zh": "巨石", "en": "Boulders"}),
    "moon_sapling": ("resources", {"zh": "月亮树苗", "en": "Moon Sapling"}),
    "moon_rock": ("resources", {"zh": "月亮石", "en": "Moon Rocks"}),
    "moon_tree": ("resources", {"zh": "月树", "en": "Moon Trees"}),
    "sapling": ("resources", {"zh": "树苗", "en": "Saplings"}),
    "trees": ("resources", {"zh": "树（所有）", "en": "Trees (All)"}),
    "palmconetree": ("resources", {"zh": "棕榈松树", "en": "Palm Trees"}),
    "ponds": ("resources", {"zh": "池塘", "en": "Ponds"}),
    "meteorspawner": ("resources", {"zh": "流星区域", "en": "Meteor Zones"}),
    "berrybush": ("resources", {"zh": "浆果丛", "en": "Berry Bushes"}),
    "moon_bullkelp": ("resources", {"zh": "海岸公牛海带", "en": "Moon Bull Kelp"}),
    "moon_starfish": ("resources", {"zh": "海星", "en": "Starfish"}),
    "ocean_seastack": ("resources", {"zh": "海蚀柱", "en": "Sea Stacks"}),
    #"hotspring": ("resources", {"zh": "温泉", "en": "Hot Springs"}),
    "moon_hotspring": ("resources", {"zh": "温泉", "en": "Hot Springs"}),
    "flint": ("resources", {"zh": "燧石", "en": "Flint"}),
    "moon_berrybush": ("resources", {"zh": "石果灌木丛", "en": "Rock Berry Bush"}),
    "carrot": ("resources", {"zh": "胡萝卜", "en": "Carrots"}),
    "reeds": ("resources", {"zh": "芦苇", "en": "Reeds"}),
    "flowers": ("resources", {"zh": "花，邪恶花", "en": "Flowers (incl. Evil Flowers)"}),
    "grass": ("resources", {"zh": "草", "en": "Grass"}),
    "mushroom": ("resources", {"zh": "蘑菇", "en": "Mushrooms"}),
    "rock_ice": ("resources", {"zh": "迷你冰川", "en": "Mini Glaciers"}),
    "tumbleweed": ("resources", {"zh": "风滚草", "en": "Tumbleweeds"}),
    # 生物以及刷新点|creatures_spawners
    "lightninggoat": ("creatures_spawners", {"zh": "伏特羊", "en": "Volt Goats"}),
    "rabbits": ("creatures_spawners", {"zh": "兔洞", "en": "Rabbit Holes"}),
    "ocean_otterdens": ("creatures_spawners", {"zh": "水獭掠夺者窝点", "en": "Otter Dens"}),
    "moon_fruitdragon": ("creatures_spawners", {"zh": "沙拉蝾螈", "en": "Salamander"}),
    "pigs": ("creatures_spawners", {"zh": "猪屋", "en": "Pig Houses"}),
    "beefalo": ("creatures_spawners", {"zh": "皮弗娄牛", "en": "Beefalo"}),
    "buzzard": ("creatures_spawners", {"zh": "秃鹫", "en": "Buzzards"}),
    "catcoon": ("creatures_spawners", {"zh": "空心树桩", "en": "Hollow Stumps"}),
    "moon_carrot": ("creatures_spawners", {"zh": "胡萝卜鼠", "en": "Carrats"}),
    "bees": ("creatures_spawners", {"zh": "蜜蜂蜂窝", "en": "Beehives"}),
    "ocean_shoal": ("creatures_spawners", {"zh": "鱼群", "en": "Fish Schools"}),
    "moles": ("creatures_spawners", {"zh": "鼹鼠丘", "en": "Mole Hills"}),
    "ocean_wobsterden": ("creatures_spawners", {"zh": "龙虾窝", "en": "Wobster Dens"}),
    # 敌对生物以及刷新点|hostile_spawners
    "chess": ("hostile_spawners", {"zh": "发条装置", "en": "Clockworks"}),
    "angrybees": ("hostile_spawners", {"zh": "杀人蜂蜂窝", "en": "Killer Bee Hives"}),
    "ocean_waterplant": ("hostile_spawners", {"zh": "海草", "en": "Seaweed"}),
    "walrus": ("hostile_spawners", {"zh": "海象营地", "en": "Walrus Camps"}),
    "merm": ("hostile_spawners", {"zh": "漏雨的小屋", "en": "Leaky Shacks"}),
    "houndmound": ("hostile_spawners", {"zh": "猎犬丘", "en": "Hound Mounds"}),
    "moon_spiders": ("hostile_spawners", {"zh": "破碎蜘蛛洞", "en": "Fissure Spider Dens"}),
    "spiders": ("hostile_spawners", {"zh": "蜘蛛巢", "en": "Spider Dens"}),
    "tentacles": ("hostile_spawners", {"zh": "触手", "en": "Tentacles"}),
    "tallbirds": ("hostile_spawners", {"zh": "高脚鸟", "en": "Tallbirds"}),
}

# ── 洞穴-世界规则 ──
# 顺序、分类和 icons.py 的 CAVE_RULES_ICONS 保持一致。
CAVE_RULES_DICT = {
    # 世界|world
    "earthquakes": ("world", {"zh": "地震", "en": "Earthquakes"}),
    "wormattacks_boss": ("world", {"zh": "大蠕虫", "en": "Giant Worms"}),
    "wormattacks": ("world", {"zh": "洞穴蠕虫袭击", "en": "Cave Worm Attacks"}),
    "rifts_enabled_cave": ("world", {"zh": "荒野裂隙", "en": "Wilderness Rifts"}),
    "rifts_frequency_cave": ("world", {"zh": "荒野裂隙频率", "en": "Wilderness Rifts Frequency"}),
    "atriumgate": ("world", {"zh": "远古大门", "en": "Ancient Gateway"}),
    "acidrain_enabled": ("world", {"zh": "酸雨", "en": "Acid Rain"}),
    "weather": ("world", {"zh": "雨", "en": "Rain"}),
    # 资源再生|regrowth
    "regrowth": ("regrowth", {"zh": "再生速度", "en": "Regrowth Rate"}),
    "lightflier_flower_regrowth": ("regrowth", {"zh": "光虫花", "en": "Lureflower"}),
    "twiggytrees_regrowth": ("regrowth", {"zh": "多枝树", "en": "Twiggy Trees"}),
    "tree_rock_regrowth": ("regrowth", {"zh": "巨石枝", "en": "Stalagmite Trees"}),
    "evergreen_regrowth": ("regrowth", {"zh": "常青树", "en": "Evergreens"}),
    "mushtree_moon_regrowth": ("regrowth", {"zh": "月亮蘑菇树", "en": "Moon Mushtrees"}),
    "reeds_regrowth": ("regrowth", {"zh": "芦苇", "en": "Reeds"}),
    "flower_cave_regrowth": ("regrowth", {"zh": "荧光花", "en": "Light Flowers"}),
    "mushtree_regrowth": ("regrowth", {"zh": "蘑菇树", "en": "Mushtrees"}),
    # 生物|creatures
    "bunnymen_setting": ("creatures", {"zh": "兔人", "en": "Bunnymen"}),
    "dustmoths": ("creatures", {"zh": "尘蛾", "en": "Dust Moths"}),
    "pigs_setting": ("creatures", {"zh": "猪", "en": "Pigs"}),
    "lightfliers": ("creatures", {"zh": "球状光虫", "en": "Lightfliers"}),
    "rocky_setting": ("creatures", {"zh": "石虾", "en": "Rock Lobsters"}),
    "monkey_setting": ("creatures", {"zh": "穴居猴", "en": "Splumonkeys"}),
    "grassgekkos": ("creatures", {"zh": "草壁虎转化", "en": "Grass Gekkos"}),
    "mushgnome": ("creatures", {"zh": "蘑菇地精", "en": "Mushroom Gnomes"}),
    "slurtles_setting": ("creatures", {"zh": "蛞蝓龟", "en": "Slurtles"}),
    "snurtles": ("creatures", {"zh": "蜗牛龟", "en": "Snurtles"}),
    "moles_setting": ("creatures", {"zh": "鼹鼠", "en": "Moles"}),
    # 敌对生物|hostile_creatures
    "spider_spitter": ("hostile_creatures", {"zh": "喷射蜘蛛", "en": "Spitter Spiders"}),
    "itemmimics": ("hostile_creatures", {"zh": "拟态蠕虫", "en": "Mimics"}),
    "chest_mimics": ("hostile_creatures", {"zh": "暴躁箱子", "en": "Angry Chests"}),
    "spider_hider": ("hostile_creatures", {"zh": "洞穴蜘蛛", "en": "Cave Spiders"}),
    "spider_dropper": ("hostile_creatures", {"zh": "穴居悬蛛", "en": "Dropper Spiders"}),
    "spiders_setting": ("hostile_creatures", {"zh": "蜘蛛", "en": "Spiders"}),
    "spider_warriors": ("hostile_creatures", {"zh": "蜘蛛战士", "en": "Spider Warriors"}),
    "bats_setting": ("hostile_creatures", {"zh": "蝙蝠", "en": "Bats"}),
    "molebats": ("hostile_creatures", {"zh": "裸露蝠", "en": "Mole Bats"}),
    "bat_boss": ("hostile_creatures", {"zh": "猎犬蝙蝠", "en": "Hound Batilisks"}),
    "rocky_boss": ("hostile_creatures", {"zh": "岩石大师", "en": "Rock Masters"}),
    "nightmarecreatures": ("hostile_creatures", {"zh": "遗迹梦魇", "en": "Nightmare Creatures"}),
    "merms": ("hostile_creatures", {"zh": "鱼人", "en": "Merms"}),
    # 巨兽|bosses
    "fruitfly": ("bosses", {"zh": "果蝇王", "en": "Fruit Fly King"}),
    "liefs": ("bosses", {"zh": "树精守卫", "en": "Treeguards"}),
    "daywalker": ("bosses", {"zh": "梦魇疯猪", "en": "Nightmare Werepig"}),
    "toadstool": ("bosses", {"zh": "毒菌蟾蜍", "en": "Toadstool"}),
    "spiderqueen": ("bosses", {"zh": "蜘蛛女王", "en": "Spider Queen"}),
    # 月亮变异|lunar
    "mutated_birds": ("lunar", {"zh": "变异的鸟", "en": "Mutated Birds"}),
    "mutated_merm": ("lunar", {"zh": "变异鱼人", "en": "Mutated Merms"}),
    "moon_spider": ("lunar", {"zh": "破碎蜘蛛", "en": "Mutated Spiders"}),
    "mutated_spiderqueen": ("lunar", {"zh": "破碎蜘蛛洞", "en": "Mutated Spider Den"}),
}

# ── 洞穴-世界生成 ──
# 顺序、分类和 icons.py 的 CAVE_GEN_ICONS 保持一致。
CAVE_GEN_DICT = {
    # 世界|world
    "task_set": ("world", {"zh": "生物群落", "en": "Biomes"}),
    "start_location": ("world", {"zh": "出生点", "en": "Start Location"}),
    "world_size": ("world", {"zh": "世界大小", "en": "World Size"}),
    "branching": ("world", {"zh": "分支", "en": "Branching"}),
    "loop": ("world", {"zh": "环形", "en": "Loop"}),
    "touchstone": ("world", {"zh": "试金石", "en": "Touch Stones"}),
    "boons": ("world", {"zh": "失败的冒险家", "en": "Failed Adventurer"}),
    "cavelight": ("world", {"zh": "洞穴光照", "en": "Cave Light"}),
    "prefabswaps_start": ("world", {"zh": "开始资源多样化", "en": "Starting Resource Variety"}),
    # 资源|resources
    "wormlights": ("resources", {"zh": "发光浆果", "en": "Wormlights"}),
    "marshbush": ("resources", {"zh": "尖刺灌木", "en": "Spiky Bush"}),
    "rock": ("resources", {"zh": "巨石", "en": "Boulders"}),
    "tree_rock": ("resources", {"zh": "巨石枝", "en": "Stalagmite Trees"}),
    "sapling": ("resources", {"zh": "树苗", "en": "Saplings"}),
    "trees": ("resources", {"zh": "树（所有）", "en": "Trees (All)"}),
    "cave_ponds": ("resources", {"zh": "池塘", "en": "Ponds"}),
    "fern": ("resources", {"zh": "洞穴蕨类", "en": "Cave Ferns"}),
    "berrybush": ("resources", {"zh": "浆果丛", "en": "Berry Bushes"}),
    "flint": ("resources", {"zh": "燧石", "en": "Flint"}),
    "reeds": ("resources", {"zh": "芦苇", "en": "Reeds"}),
    "lichen": ("resources", {"zh": "苔藓", "en": "Lichen"}),
    "grass": ("resources", {"zh": "草", "en": "Grass"}),
    "flower_cave": ("resources", {"zh": "荧光花", "en": "Light Flowers"}),
    "mushroom": ("resources", {"zh": "蘑菇", "en": "Mushrooms"}),
    "mushtree": ("resources", {"zh": "蘑菇树", "en": "Mushtrees"}),
    "banana": ("resources", {"zh": "香蕉", "en": "Bananas"}),
    # 生物以及刷新点|creatures_spawners
    "bunnymen": ("creatures_spawners", {"zh": "兔屋", "en": "Bunnymen Houses"}),
    "slurper": ("creatures_spawners", {"zh": "啜食者", "en": "Slurpers"}),
    "rocky": ("creatures_spawners", {"zh": "石虾", "en": "Rock Lobsters"}),
    "monkey": ("creatures_spawners", {"zh": "穴居猴桶", "en": "Splumonkey Barrels"}),
    "slurtles": ("creatures_spawners", {"zh": "蝓龟窝", "en": "Slurtle Dens"}),
    # 敌对生物以及刷新点|hostile_spawners
    "chess": ("hostile_spawners", {"zh": "发条装置", "en": "Clockworks"}),
    "fissure": ("hostile_spawners", {"zh": "梦魇裂缝", "en": "Nightmare Fissures"}),
    "worms": ("hostile_spawners", {"zh": "洞穴蠕虫", "en": "Cave Worms"}),
    "cave_spiders": ("hostile_spawners", {"zh": "蛛网岩", "en": "Spider Web Rocks"}),
    "spiders": ("hostile_spawners", {"zh": "蜘蛛巢", "en": "Spider Dens"}),
    "bats": ("hostile_spawners", {"zh": "蝙蝠", "en": "Bats"}),
    "tentacles": ("hostile_spawners", {"zh": "触手", "en": "Tentacles"}),
}

# 以下条目是 Master/Caves 共享的规则/生成项（已用真实洞穴存档核对），复用地面表定义，
# 洞穴专属表仍优先覆盖同名项。洞穴界面不显示森林专属的全局/活动/冒险家目录、基础资源再生、
# 世界生成-全局和道路，这些 key 即使出现在洞穴存档里也不并入洞穴目录。
CAVE_ALL_RULES_DICT = dict(CAVE_RULES_DICT)
CAVE_ALL_GEN_DICT = dict(CAVE_GEN_DICT)

# ── 查询函数 ─────────────────────────────────────────────────────────────

def localized_name(names: dict) -> str:
    """从 {"zh", "en"} 取当前界面语言的名称，缺失时回退中文。"""
    return names.get(get_lang()) or names.get("zh") or ""


# 内部沿用旧名字，避免下面几处调用都要改。
_localized = localized_name


def _get_settings(location: str, is_rule: bool, is_master_world: bool = True):
    """按地点/类型/是否主世界取对应的设置字典。"""
    from dstools.features.world.catalog_resolver import resolve_vanilla_settings
    return resolve_vanilla_settings(location, is_rule, is_master_world)


def get_setting_info(key: str, location: str = "forest", mod_settings: dict | None = None,
                     is_master_world: bool = True):
    """取设置的 (category, is_rule, name)：依次查原版规则表、生成表、已启用 Mod 的登记；
    都没有返回 ("other", False, key)（未登记 key 不显示）。仅主世界的原版项在从世界算作未登记。
    """
    from dstools.features.world.catalog_resolver import resolve_setting_info
    return resolve_setting_info(key, location, mod_settings, is_master_world)


# 官方 customize.lua 中各设置的 order（分类内排序的权威来源）；没有 order 的按显示名排序（见 get_order_key）
RULE_ITEM_ORDER = {
    # 全局
    "specialevent": 1, "autumn": 2, "winter": 3, "spring": 4, "summer": 5,
    "day": 6, "spawnmode": 7, "ghostenabled": 8, "portalresurection": 9,
    "ghostsanitydrain": 10, "resettime": 11, "beefaloheat": 12, "krampus": 13,
    # 活动
    "crow_carnival": 1, "hallowed_nights": 2, "winters_feast": 3,
    "year_of_the_gobbler": 4, "year_of_the_varg": 5, "year_of_the_pig": 6,
    "year_of_the_carrat": 7, "year_of_the_beefalo": 8, "year_of_the_catcoon": 9,
    "year_of_the_bunnyman": 10, "year_of_the_dragonfly": 11,
    "year_of_the_snake": 12, "year_of_the_knight": 13,
    # 冒险家
    "extrastartingitems": 1, "seasonalstartingitems": 2, "spawnprotection": 3,
    "dropeverythingondespawn": 4, "healthpenalty": 5, "lessdamagetaken": 6,
    "temperaturedamage": 7, "hunger": 8, "darkness": 9, "shadowcreatures": 10,
    "brightmarecreatures": 11,
    # 世界
    "hounds": 1, "winterhounds": 2, "summerhounds": 3,
    # 资源再生
    "regrowth": 1,
}

GEN_ITEM_ORDER = {
    # 世界
    "task_set": 1, "start_location": 2, "world_size": 3, "branching": 4,
    "loop": 5, "roads": 6, "touchstone": 17, "boons": 18, "cavelight": 18,
    "prefabswaps_start": 20,
    # 全局
    "season_start": 1,
}


def get_order_key(key: str, name: str, location: str = "forest",
                  is_rule: bool = True, mod_settings: dict | None = None,
                  order_overrides: dict[str, float] | None = None):
    """分类内排序键，复刻官方 GetOptionsFromGroup：有 order 的按 order 升序在前，没有的按显示名在后；
    Mod 登记的 order 一并参与。"""
    order = order_overrides.get(key) if order_overrides else None
    if order is None:
        order = (RULE_ITEM_ORDER if is_rule else GEN_ITEM_ORDER).get(key)
    if order is None and mod_settings:
        info = mod_settings.get(key)
        if info is not None and getattr(info, "order", None) is not None:
            order = info.order
    if order is not None:
        return (0, order, "")
    return (1, 0, name)




def get_categories(location: str, setting_type: str, mod_categories=None,
                   is_master_world: bool = True) -> list[tuple[str, str]]:
    """取指定地点和设置类型的分类列表。

    Mod 分类（get_mod_categories() 的结果）排在最前面并保持给定顺序，便于用户找到 Mod 新增的
    设置；分类是否显示由调用方按实际条目过滤。
    """
    from dstools.features.world.catalog_resolver import resolve_vanilla_categories
    raw = resolve_vanilla_categories(location, setting_type, is_master_world)
    cats = [(key, _localized(names)) for key, names in mod_categories] if mod_categories else []
    cats.extend((key, _localized(names)) for key, names in raw)
    return cats
