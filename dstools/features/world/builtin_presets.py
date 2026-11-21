"""游戏自带的「选项预设」（世界设置类预设），套用后游戏界面显示的游戏风格（生存/轻松/无尽…）随之改变。

内容取自本体 scripts/map/levels/forest.lua、caves.lua 中前端可见（无 hideinfrontend）的 AddSettingsPreset，
名称与说明取自 scripts/languages/chinese_s.po 与 strings.lua。游戏选中一个选项预设时先清空全部改动、
再套用预设的取值（widgets/redux/worldsettings/worldsettingsmenu.lua 的 LoadPreset），预设没写的设置项
回到 customize.lua 里该项的默认值；世界生成项不受影响。游戏风格由 map/levels.lua 的
CalcPlaystyleForSettings 按主世界设置取值算出，不是单独的字段。
"""

from dataclasses import dataclass, field

from dstools.features.world.location_profiles import CAVE_LOCATION, FOREST_LOCATION
from dstools.i18n import get_lang

# customize.lua 的 WORLDSETTINGS_GROUP 中默认值不是 "default" 的设置项（其余全部为 "default"）
SETTINGS_NON_DEFAULT_VALUES = {
    "basicresource_regrowth": "none",
    "wanderingtrader_enabled": "always",
    "acidrain_enabled": "always",
    "healthpenalty": "always",
    "lessdamagetaken": "none",
    "spawnmode": "fixed",
    "ghostenabled": "always",
    "portalresurection": "none",
    "ghostsanitydrain": "always",
}


@dataclass(frozen=True)
class BuiltinSettingsPreset:
    preset_id: str
    location: str
    names: dict                  # {"zh": ..., "en": ...}
    descriptions: dict
    overrides: dict = field(default_factory=dict)
    # 预设自带的 playstyle 字段（forest.lua 中 TERRARIA 等写的 PLAYSTYLE_DEFAULT 即 "survival"；洞穴预设没有）
    playstyle: str | None = None

    @property
    def name(self) -> str:
        return self.names.get(get_lang(), self.names["en"])

    @property
    def description(self) -> str:
        return self.descriptions.get(get_lang(), self.descriptions["en"])


BUILTIN_SETTINGS_PRESETS = (
    BuiltinSettingsPreset(
        "SURVIVAL_TOGETHER", FOREST_LOCATION,
        {"zh": "生存", "en": "Survival"},
        {"zh": "标准《饥荒》体验。", "en": "The standard Don't Starve experience."},
        playstyle="survival",
    ),
    BuiltinSettingsPreset(
        "RELAXED", FOREST_LOCATION,
        {"zh": "轻松", "en": "Relaxed"},
        {"zh": "更轻松的游戏方式，更少受到来自世界的威胁。饥饿、寒冷、过热和黑暗将不会杀死冒险家。"
               "降低冒险家受到的伤害。永远可以在绚丽之门复活。",
         "en": "A more relaxed playstyle with less threats from the world. Survivors will not die from "
               "starvation, freezing, overheating, or the darkness. Damage to survivors is reduced. "
               "Resurrection is always available via the Florid Postern."},
        {
            "ghostsanitydrain": "none", "portalresurection": "always", "temperaturedamage": "nonlethal",
            "hunger": "nonlethal", "darkness": "nonlethal", "lessdamagetaken": "always",
            "healthpenalty": "none", "wildfires": "never", "hounds": "rare",
            "resettime": "none", "shadowcreatures": "rare", "brightmarecreatures": "rare",
        },
        playstyle="relaxed",
    ),
    BuiltinSettingsPreset(
        "ENDLESS", FOREST_LOCATION,
        {"zh": "无尽", "en": "Endless"},
        {"zh": "永不结束的饥荒沙盒模式。永远可以在绚丽之门复活。",
         "en": "A never-ending Don't Starve sandbox. Resurrection is always available via the Florid Postern."},
        {"portalresurection": "always", "basicresource_regrowth": "always", "resettime": "none",
         "ghostsanitydrain": "none"},
        playstyle="endless",
    ),
    BuiltinSettingsPreset(
        "WILDERNESS", FOREST_LOCATION,
        {"zh": "荒野", "en": "Wilderness"},
        {"zh": "外面就是荒野，充满了危险！随机进入世界的一个地方。死亡之后：选一名新冒险家试一下、再试一下。",
         "en": "It's wild and dangerous out there! Enter at a random location in the world. "
               "When you die: pick a new survivor and try, try again."},
        {"spawnmode": "scatter", "basicresource_regrowth": "always", "ghostenabled": "none",
         "ghostsanitydrain": "none", "resettime": "none"},
        playstyle="wilderness",
    ),
    BuiltinSettingsPreset(
        "LIGHTS_OUT", FOREST_LOCATION,
        {"zh": "暗无天日", "en": "Lights Out"},
        {"zh": "在标准《饥荒》体验的基础上添加黑暗基调。",
         "en": "A dark twist on the standard Don't Starve experience."},
        {"day": "onlynight"},
        playstyle="lightsout",
    ),
    BuiltinSettingsPreset(
        "TERRARIA", FOREST_LOCATION,
        {"zh": "泰拉瑞亚之味", "en": "Taste of Terraria"},
        {"zh": "《饥荒联机版》，带有一点《泰拉瑞亚》风情。",
         "en": "Don't Starve Together, with a Terraria-inspired twist."},
        {
            "day": "longnight", "shadowcreatures": "never", "lightning": "never", "weather": "often",
            "wildfires": "never", "regrowth": "fast", "carrots_regrowth": "never", "flowers_regrowth": "fast",
            "bees_setting": "never", "birds": "often", "rabbits_setting": "often", "bats_setting": "never",
            "wasps": "always", "sharks": "many", "lureplants": "often",
        },
        playstyle="survival",
    ),
    BuiltinSettingsPreset(
        "SURVIVAL_DEFAULT_PLUS", FOREST_LOCATION,
        {"zh": "丰富森林", "en": "Forest Plus"},
        {"zh": "在一个更艰难的世界中快速上手。", "en": "A quicker start in a harsher world."},
        playstyle="survival",
    ),
    BuiltinSettingsPreset(
        "DST_CAVE", CAVE_LOCATION,
        {"zh": "洞穴", "en": "The Caves"},
        {"zh": "探查洞穴…… 一起！", "en": "Delve into the caves... together!"},
    ),
    BuiltinSettingsPreset(
        "DST_CAVE_PLUS", CAVE_LOCATION,
        {"zh": "洞穴加强", "en": "Caves Plus"},
        {"zh": "更黑、更多蜘蛛的洞穴体验。", "en": "A darker, more arachnid-y cave experience."},
        {"flower_cave_regrowth": "rare"},
    ),
    BuiltinSettingsPreset(
        "TERRARIA_CAVE", CAVE_LOCATION,
        {"zh": "泰拉瑞亚的洞穴", "en": "Caves of Terraria"},
        {"zh": "《饥荒联机版》，带有一点《泰拉瑞亚》风情……的洞穴游！",
         "en": "Don't Starve Together, with a Terraria-inspired twist... in the caves!"},
        {
            "weather": "often", "wormattacks": "often", "flower_cave_regrowth": "fast",
            "mushtree_regrowth": "fast", "mushtree_moon_regrowth": "fast", "bats_setting": "often",
            "spider_dropper": "often",
        },
    ),
)


def presets_for_location(location: str) -> list[BuiltinSettingsPreset]:
    """当前世界类型可用的内置选项预设；Mod 世界类型（海难、猪镇等）没有，返回空表。"""
    return [p for p in BUILTIN_SETTINGS_PRESETS if p.location == location]


# 写进 leveldataoverride / shardindex world.options 的预设信息，与游戏一致（worldsettingsmenu.lua）：
# 保存时 CollectOptions 以基础预设为底，{类别}_id/name/desc 是官方（或 Mod）基础预设；选的是自定义预设时
# 另写 custom_{类别}_id/name/desc，选官方预设时没有这几项。读档时 SetDataFromOptions 按 {类别}_id 载入预设、
# 按 custom_{类别}_id 显示自定义预设名，并据此统计"已修改"项数。{类别} 为 settings（世界设置）或 worldgen
# （世界生成）。下面的字段表中值为 None 表示删除该字段。顶层 id/name/desc 不动。
SETTINGS = "settings"
WORLDGEN = "worldgen"


def find_preset(preset_id: str) -> BuiltinSettingsPreset | None:
    return next((p for p in BUILTIN_SETTINGS_PRESETS if p.preset_id == (preset_id or "").upper()), None)


def official_fields(category: str, base_id: str) -> dict:
    """以官方预设 base_id 为底时的字段。名称与说明按当前界面语言写（游戏同样存当时语言的文字）；
    本表里的预设在世界生成类别下名称、说明与选项预设相同（forest.lua/caves.lua 用同一组字符串）。
    认不出的基础预设（Mod 预设）只写 ID，游戏读档也只用 ID。playstyle 只随选项预设写。"""
    preset = find_preset(base_id)
    if preset is None:
        return {f"{category}_id": base_id} if base_id else {}
    fields = {f"{category}_id": preset.preset_id, f"{category}_name": preset.name,
              f"{category}_desc": preset.description}
    if category == SETTINGS and preset.playstyle:
        fields["playstyle"] = preset.playstyle
    return fields


def custom_fields(category: str, custom_id: str | None, name: str = "", desc: str = "") -> dict:
    """自定义预设字段；custom_id 为 None 表示选的是官方预设，删掉残留的自定义预设信息。"""
    keys = (f"custom_{category}_id", f"custom_{category}_name", f"custom_{category}_desc")
    if custom_id is None:
        return dict.fromkeys(keys)
    return dict(zip(keys, (custom_id, name, desc)))


def preset_fields(preset: BuiltinSettingsPreset) -> dict:
    """套用内置选项预设后的字段：世界设置类别改为该官方预设，世界生成类别不动。"""
    return {**official_fields(SETTINGS, preset.preset_id), **custom_fields(SETTINGS, None)}


def apply_preset_fields(target: dict, fields: dict) -> None:
    """把字段表写进一张 options 表：值为 None 的删除；playstyle 只在原来就有时改（只是预设附带的元数据，
    游戏每次开服按设置重新算游戏风格），其余总是写入。"""
    for key, value in fields.items():
        if value is None:
            target.pop(key, None)
        elif key != "playstyle" or key in target:
            target[key] = value


def fields_differ(target: dict, fields: dict) -> bool:
    """options 表套用字段表后会不会变（用来判断取值没变时是否仍需保存）。"""
    updated = dict(target)
    apply_preset_fields(updated, fields)
    return updated != target


def vanilla_setting_default(key: str) -> str:
    """原版世界设置项在 customize.lua 中的默认值。"""
    return SETTINGS_NON_DEFAULT_VALUES.get(key, "default")
