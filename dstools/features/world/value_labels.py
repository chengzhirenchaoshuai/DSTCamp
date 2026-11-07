"""世界设置取值的中英文显示文案（纯数据）。"""

from dstools.i18n import get_lang, t

_VALUE_LABELS = {
    "default": {"zh": "默认", "en": "Default"},
    "never": {"zh": "无", "en": "None"},
    "rare": {"zh": "很少", "en": "Little"},
    "often": {"zh": "较多", "en": "More"},
    "always": {"zh": "大量", "en": "Tons"},
    "few": {"zh": "很少", "en": "Few"},
    "many": {"zh": "大量", "en": "Many"},
    "none": {"zh": "禁用", "en": "Disabled"},
    "max": {"zh": "最多", "en": "Max"},
    "veryslow": {"zh": "极慢", "en": "Very Slow"},
    "slow": {"zh": "慢", "en": "Slow"},
    "fast": {"zh": "快", "en": "Fast"},
    "veryfast": {"zh": "极快", "en": "Very Fast"},
    "long": {"zh": "长", "en": "Long"},
    "short": {"zh": "短", "en": "Short"},
    "random": {"zh": "随机", "en": "Random"},
    "force": {"zh": "强制", "en": "Forced"},
    "squall": {"zh": "暴雨", "en": "Squall"},
    "more": {"zh": "较多", "en": "More"},
    "nonlethal": {"zh": "非致命", "en": "Non-lethal"},
    "noseason": {"zh": "无", "en": "None"},
    "veryshortseason": {"zh": "极短", "en": "Very Short"},
    "shortseason": {"zh": "短", "en": "Short"},
    "longseason": {"zh": "长", "en": "Long"},
    "verylongseason": {"zh": "极长", "en": "Very Long"},
    "onlyday": {"zh": "仅白天", "en": "Only Day"},
    "onlydusk": {"zh": "仅黄昏", "en": "Only Dusk"},
    "onlynight": {"zh": "仅夜晚", "en": "Only Night"},
    "longday": {"zh": "长白天", "en": "Long Day"},
    "longdusk": {"zh": "长黄昏", "en": "Long Dusk"},
    "longnight": {"zh": "长夜晚", "en": "Long Night"},
    "noday": {"zh": "无白天", "en": "No Day"},
    "nodusk": {"zh": "无黄昏", "en": "No Dusk"},
    "nonight": {"zh": "无夜晚", "en": "No Night"},
    "fixed": {"zh": "固定", "en": "Fixed"},
    "wandering": {"zh": "流浪", "en": "Wandering"},
    "scatter": {"zh": "随机", "en": "Random"},
    "disabled": {"zh": "禁用", "en": "Disabled"},
    "enabled": {"zh": "总是", "en": "Always"},
    "auto": {"zh": "自动", "en": "Auto"},
    "uncommon": {"zh": "较少", "en": "Less"},
    "mostly": {"zh": "很多", "en": "Lots"},
    "insane": {"zh": "疯狂", "en": "Insane"},
    # ocean_waterplant/ocean_seastack 的取值是 worldgen 频率表加 "ocean_" 前缀（见 customize.lua），
    # 文案与不带前缀的版本相同，每一档都要登记，否则会显示原始字符串
    "ocean_never": {"zh": "无", "en": "None"},
    "ocean_rare": {"zh": "很少", "en": "Little"},
    "ocean_uncommon": {"zh": "较少", "en": "Less"},
    "ocean_default": {"zh": "默认", "en": "Default"},
    "ocean_often": {"zh": "较多", "en": "More"},
    "ocean_mostly": {"zh": "很多", "en": "Lots"},
    "ocean_always": {"zh": "大量", "en": "Tons"},
    "ocean_insane": {"zh": "疯狂", "en": "Insane"},
    "least": {"zh": "最少", "en": "Least"},
    "most": {"zh": "最多", "en": "Most"},
    "classic": {"zh": "经典", "en": "Classic"},
    "True": {"zh": "是", "en": "Yes"},
    "False": {"zh": "否", "en": "No"},
    "LinkNodesByKeys": {"zh": "按关键节点连接", "en": "Link Nodes by Keys"},
    "wormhole": {"zh": "虫洞", "en": "Wormhole"},
    "small": {"zh": "小", "en": "Small"},
    "medium": {"zh": "中", "en": "Medium"},
    "large": {"zh": "大", "en": "Large"},
    "huge": {"zh": "巨大", "en": "Huge"},
}

# 按 key 单独覆盖的文案：同一原始值在不同设置中含义不同（如活动的 "default" 是"自动"）
_PER_KEY_LABELS = {
    # 活动：default 是"自动"，none 是"无"
    "specialevent": {"default": {"zh": "自动", "en": "Auto"}, "none": {"zh": "无", "en": "None"}},
    # 冒险家死亡：none=更改冒险家, always=变鬼魂
    "ghostenabled": {"none": {"zh": "更改冒险家", "en": "Change Survivor"},
                     "always": {"zh": "变鬼魂", "en": "Become a Ghost"}},
    # 禁用/启用 二档开关（enableddisabled_descriptions）
    "portalresurection": {"always": {"zh": "启用", "en": "Enabled"}, "none": {"zh": "禁用", "en": "Disabled"}},
    "ghostsanitydrain": {"always": {"zh": "启用", "en": "Enabled"}, "none": {"zh": "禁用", "en": "Disabled"}},
    "healthpenalty": {"always": {"zh": "启用", "en": "Enabled"}, "none": {"zh": "禁用", "en": "Disabled"}},
    "basicresource_regrowth": {"always": {"zh": "启用", "en": "Enabled"}, "none": {"zh": "禁用", "en": "Disabled"}},
    "acidrain_enabled": {"always": {"zh": "启用", "en": "Enabled"}, "none": {"zh": "禁用", "en": "Disabled"}},
    "wanderingtrader_enabled": {"always": {"zh": "启用", "en": "Enabled"}, "none": {"zh": "禁用", "en": "Disabled"}},
    # 岛屿冒险(Island Adventures)核心 mod 的 4 个 enableddisabled 开关，
    # desc 都是 enableddisabled_descriptions，none=禁用/always=启用。
    "poison": {"always": {"zh": "启用", "en": "Enabled"}, "none": {"zh": "禁用", "en": "Disabled"}},
    "dst_boats": {"always": {"zh": "启用", "en": "Enabled"}, "none": {"zh": "禁用", "en": "Disabled"}},
    "ia_boats": {"always": {"zh": "启用", "en": "Enabled"}, "none": {"zh": "禁用", "en": "Disabled"}},
    "ia_drowning": {"always": {"zh": "启用", "en": "Enabled"}, "none": {"zh": "禁用", "en": "Disabled"}},
    "lunarthrall_plant_remove": {
        "always": {"zh": "启用", "en": "Enabled"},
        "none": {"zh": "禁用", "en": "Disabled"},
    },
    # 受到的伤害：较少=always, 默认=none, 较多=more
    "lessdamagetaken": {"always": {"zh": "较少", "en": "Less"}, "none": {"zh": "默认", "en": "Default"},
                        "more": {"zh": "较多", "en": "More"}},
    # "0" → 总是, "none" → 从不；其余数字走下面 get_value_label 里的动态格式化("第N天后")
    "extrastartingitems": {"0": {"zh": "总是", "en": "Always"}, "none": {"zh": "从不", "en": "Never"}},
    # 环形：never=从不, always=总是（default 走全局"默认"）
    "loop": {"never": {"zh": "从不", "en": "Never"}, "always": {"zh": "总是", "en": "Always"}},
    # 分支：never=从不（BRANCHINGNEVER，非 SLIDENEVER 的"无"）
    "branching": {"never": {"zh": "从不", "en": "Never"}},
    "task_set": {"default": {"zh": "联机版", "en": "Together"}, "classic": {"zh": "经典", "en": "Classic"},
                 "cave_default": {"zh": "地下", "en": "Caves"},
                 "shipwrecked": {"zh": "海难", "en": "Shipwrecked"}, "volcano": {"zh": "火山", "en": "Volcano"}},
    "start_location": {"default": {"zh": "默认", "en": "Default"}, "plus": {"zh": "额外资源", "en": "Plus"},
                       "darkness": {"zh": "黑暗", "en": "Dark"}, "caves": {"zh": "洞穴", "en": "Caves"},
                       "shipwrecked_default": {"zh": "默认", "en": "Default"},
                       "shipwrecked_plus": {"zh": "额外资源", "en": "Plus"},
                       "shipwrecked_darkness": {"zh": "黑暗", "en": "Dark"},
                       "volcano_default": {"zh": "火山", "en": "Volcano"}},
    # 防骚扰出生保护：中间档"自动检测"（DETECT_AUTO），always=总是（DETECT_ALWAYS）
    "spawnprotection": {"default": {"zh": "自动检测", "en": "Auto Detect"},
                        "always": {"zh": "总是", "en": "Always"}},
    # 大蠕虫：从不/稀有/常见/总是 是这个 key 专属文案（LOOP 系列）
    "wormattacks_boss": {"never": {"zh": "从不", "en": "Never"}, "rare": {"zh": "稀有", "en": "Rare"},
                         "often": {"zh": "常见", "en": "Often"}, "always": {"zh": "总是", "en": "Always"}},
    # 森林石化：无/慢/默认/快/极快（petrification_descriptions）
    "petrification": {"none": {"zh": "无", "en": "None"}, "few": {"zh": "慢", "en": "Slow"},
                      "many": {"zh": "快", "en": "Fast"}, "max": {"zh": "极快", "en": "Very Fast"}},
    # 起始季节（season_start_descriptions）
    "season_start": {"default": {"zh": "秋", "en": "Autumn"}, "winter": {"zh": "冬", "en": "Winter"},
                     "spring": {"zh": "春", "en": "Spring"}, "summer": {"zh": "夏", "en": "Summer"},
                     "autumn|spring": {"zh": "春或秋", "en": "Autumn or Spring"},
                     "winter|summer": {"zh": "冬或夏", "en": "Winter or Summer"},
                     "autumn|winter|spring|summer": {"zh": "随机", "en": "Random"}},
    # 海难开局季节（shipwrecked_season_start_descriptions，岛屿 mod 自定义
    # 的 7 档，中文取自 ia_sc.po 的 CUSTOMIZATIONSCREEN 对应条目）
    "shipwrecked_season_start": {
        "default": {"zh": "温和季", "en": "Mild"},
        "wet": {"zh": "飓风季", "en": "Hurricane"},
        "green": {"zh": "雨季", "en": "Monsoon"},
        "dry": {"zh": "旱季", "en": "Dry"},
        "mild|green": {"zh": "温和季或雨季", "en": "Mild or Monsoon"},
        "wet|dry": {"zh": "飓风季或旱季", "en": "Hurricane or Dry"},
        "mild|wet|green|dry": {"zh": "随机", "en": "Random"},
    },
    # 火山岛（enableddisabled_descriptions）：always=启用
    "volcanoisland": {"always": {"zh": "启用", "en": "Enabled"}, "none": {"zh": "禁用", "en": "Disabled"}},
    # 猪镇起始季节（云霄国度 mod 的 season_start_descriptions，中文取自
    # pl_chinese_s.po 的 SANDBOXMENU 对应条目）
    "porkland_season_start": {
        "default": {"zh": "平和季", "en": "Temperate"},
        "humid": {"zh": "潮湿季", "en": "Humid"},
        "lush": {"zh": "繁茂季", "en": "Lush"},
        "temperate|humid|lush": {"zh": "随机", "en": "Random"},
    },
    "cave_season_start": {
        "default": {"zh": "稳定季", "en": "Tranquil"},
        "frost": {"zh": "凛冬季", "en": "Frost"},
        "verdant": {"zh": "丰沃季", "en": "Verdant"},
        "umbral": {"zh": "暗影季", "en": "Umbral"},
        "tranquil|frost|verdant|umbral": {"zh": "随机", "en": "Random"},
    },
    "nightmareclock": {
        "morelongseason": {"zh": "更长", "en": "Longer"},
        "superlongseason": {"zh": "超长", "en": "Super Long"},
    },
    # 世界大小：default 档显示"大"（size_descriptions 用 SLIDESLARGE）
    "world_size": {"default": {"zh": "大", "en": "Large"}},
    # 荒野裂隙：default 档是"自动检测"，always=总是（DETECT_ALWAYS）
    "rifts_enabled": {"default": {"zh": "自动检测", "en": "Auto Detect"},
                      "always": {"zh": "总是", "en": "Always"}},
    "rifts_enabled_cave": {"default": {"zh": "自动检测", "en": "Auto Detect"},
                           "always": {"zh": "总是", "en": "Always"}},
    # 开始资源多样化：highly random 是"非常随机"
    "prefabswaps_start": {"highly random": {"zh": "非常随机", "en": "Highly Random"}},
    # 离开游戏后物品掉落：always=所有（EVERYTHING）
    "dropeverythingondespawn": {"always": {"zh": "所有", "en": "Everything"}},
    # 死亡重置倒计时：none=禁用, always=立刻（INSTANT）
    "resettime": {"none": {"zh": "禁用", "en": "Disabled"}, "always": {"zh": "立刻", "en": "Instant"}},
    # 出生模式：fixed=绚丽之门（PORTAL）
    "spawnmode": {"fixed": {"zh": "绚丽之门", "en": "Florid Postern"}},
}

def _localized_value(names: dict) -> str:
    return names.get(get_lang()) or names.get("zh") or ""


def get_value_label(key: str, raw_value: str, mod_settings: dict | None = None) -> str:
    """按当前界面语言取一个原始设置值的显示文案，支持按 key 单独覆盖。"""
    # Mod 登记了本条 desc 文案的优先（同名取值在不同 Mod 里含义不同）
    mod_info = mod_settings.get(key) if mod_settings else None
    if mod_info is not None and mod_info.value_labels:
        names = mod_info.value_labels.get(raw_value)
        if names is not None:
            return _localized_value(names)
    # 先查有没有针对这个 key 的专属覆盖
    if key in _PER_KEY_LABELS:
        override = _PER_KEY_LABELS[key].get(raw_value)
        if override is not None:
            return _localized_value(override)
    # extrastartingitems：原始值是天数
    if key == "extrastartingitems":
        try:
            n = int(raw_value)
            return t("world.after_day_n", n=n)
        except (ValueError, TypeError):
            pass
    names = _VALUE_LABELS.get(raw_value)
    if names is not None:
        return _localized_value(names)
    return str(raw_value)
