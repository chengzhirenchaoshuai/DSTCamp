"""同类 Mod 名单：功能相同、不该同时启用的 Mod，供 duplicates.py 检测。

由 scripts/mod_conflict_tool.py 生成和维护（会整份重写，手改的注释会丢）。字段：
  key            类别键（也是工具命令行里的名字）
  zh / en        类别显示名
  search         在创意工坊搜索候选时用的关键词
  note           备注（只给人看）
  mods           收录的 Mod：{工坊 ID: 标题}，同时启用其中两个以上就提示
  excluded       审核时排除的 Mod，以后搜索不再列出
  cms_key        常用mod集合 modmain.lua 里对应的 safeImport 配置键；None 表示集合里没有这个功能
  cms_extra      只写进常用mod集合冲突列表、DSTCamp 不提示的 Mod（如多功能合集 Lag Remover）
"""

GROUPS = [
    {
        'key': 'global_positions',
        'zh': '全图定位',
        'en': 'Global positions',
        'search': ['global positions', '全图定位', '全球定位'],
        'note': '378160973、3138571948、3590648641 来自用户截图',
        'mods': {
            '378160973': 'Global Positions',
            '3138571948': 'Global Positions (CompleteSync)',
            '3590648641': '简易全球定位',
            '1860955902': '[DST]Global positioning system',
            '2373375099': '全图定位（幽冥汉化版）',
            '2998725086': '😶\u200d🌫️全图定位 Global Positions',
            '2528036914': 'Global Positions - Sinicized Settings',
            '2536583497': '8全图定位',
            '2902364746': 'Global Positions -Remapped',
        },
        'excluded': {
        },
        'cms_key': 'global_positions',
        'cms_extra': {
        },
    },
    {
        'key': 'item_info',
        'zh': '物品信息显示',
        'en': 'Item info display',
        'search': ['show me', 'insight', '显示信息'],
        'note': 'Show Me(中文) 检测到 Insight 会自行停止加载（见其 modmain.lua）',
        'mods': {
            '2189004162': 'Insight (Show Me+)',
            '2287303119': 'Show Me(New)',
        },
        'excluded': {
        },
        'cms_key': None,
        'cms_extra': {
        },
    },
    {
        'key': 'dont_drop',
        'zh': '死亡不掉落',
        'en': 'Keep items on death',
        'search': ["don't drop", '死亡不掉落'],
        'note': '',
        'mods': {
            '661253977': "Don't Drop Everything",
            '2845410726': "灵魂携带物品 - Don't Drop Everything(fix)",
        },
        'excluded': {
            '2572244884': '41',
        },
        'cms_key': 'dont_drop',
        'cms_extra': {
            '2572244884': '41',
        },
    },
    {
        'key': 'no_tree_regrowth',
        'zh': '停止树木再生',
        'en': 'No tree regrowth',
        'search': ['no tree regrowth', '树木 不再生'],
        'note': '',
        'mods': {
            '3210773634': 'NO TREE-REGROWTH',
            '1938252586': 'No Tree Regrowth',
            '3359637342': 'NO TREE REGROWTH AND NO WITHER PLANTS -- MOD ADAPTED',
        },
        'excluded': {
        },
        'cms_key': 'no_tree_regrowth',
        'cms_extra': {
            '3050607025': 'Lag Remover',
        },
    },
    {
        'key': 'auto_clean',
        'zh': '自动清理',
        'en': 'Auto clean',
        'search': ['auto clean', '自动清理'],
        'note': '',
        'mods': {
            '1984703361': 'AutoClean 自动清理',
            '3365509623': 'DST垃圾自动清理',
            '2950657933': 'Auto Clean Plus',
            '1883003425': '自定义清理服务器(自用调整修改)',
        },
        'excluded': {
        },
        'cms_key': 'auto_clean',
        'cms_extra': {
            '3050607025': 'Lag Remover',
        },
    },
    {
        'key': 'batch_trade',
        'zh': '批量交易',
        'en': 'Batch trading',
        'search': ['quick trade', '批量交易'],
        'note': '',
        'mods': {
            '3046339764': 'Stacked Trade',
            '3098537083': '快速交易(Quick_deal)',
            '1780476441': 'Fast Pigking[dst]',
        },
        'excluded': {
        },
        'cms_key': 'batch_trade',
        'cms_extra': {
            '3050607025': 'Lag Remover',
        },
    },
    {
        'key': 'auto_stack',
        'zh': '掉落物品自动堆叠',
        'en': 'Auto stack drops',
        'search': ['auto stack', '自动堆叠'],
        'note': '',
        'mods': {
            '2952059327': '自动堆叠',
            '2999581232': '自动堆叠(Automatic Stacking)',
            '1998081438': '掉落堆叠(Drop & Stack)',
            '3441482195': 'Auto Stack Drops 2024',
        },
        'excluded': {
        },
        'cms_key': 'auto_stack_range',
        'cms_extra': {
            '3050607025': 'Lag Remover',
        },
    },
    {
        'key': 'stack_size',
        'zh': '物品堆叠上限',
        'en': 'Stack size',
        'search': ['stack size', '堆叠上限'],
        'note': '',
        'mods': {
            '831523966': '999 Stack Size',
            '3203848267': '无限堆叠（Infinite stacking）',
            '2774093373': 'Modify Items Stack',
            '2754982954': 'StackSize&KeepFresh',
            '1571581787': 'Increased Stack size MAX9999',
            '463621557': '999 Item Stack叠加数量999',
            '2677672662': '999 Stack Size (PT-BR)',
            '3175756875': 'True Stack Size',
            '374550642': 'Increased Stack size',
            '2624832309': '物品叠加上限',
            '3289112324': 'Super Stack Size',
            '3007715893': '更多物品堆叠/More Items Stack',
        },
        'excluded': {
        },
        'cms_key': 'stack_size',
        'cms_extra': {
            '3050607025': 'Lag Remover',
        },
    },
    {
        'key': 'quick_work',
        'zh': '快速工作',
        'en': 'Quick work',
        'search': ['quick work', 'quick pick', '快速采集'],
        'note': '',
        'mods': {
            '1200745268': 'Quick Work',
            '2527328931': 'Quick Work/Action (Updated)',
            '2158549297': '快速采集。',
            '501385076': 'Quick Pick',
            '2791452067': 'Quick Action',
            '2921270365': 'Quick Pick',
        },
        'excluded': {
        },
        'cms_key': 'quick_work',
        'cms_extra': {
        },
    },
    {
        'key': 'extra_equip_slots',
        'zh': '多格装备栏',
        'en': 'Extra equip slots',
        'search': ['extra equip slots', '装备栏'],
        'note': '',
        'mods': {
            '3281681895': '六格装备栏',
            '1819567085': 'DST 四格装备栏',
            '2373346252': '五格装备栏（幽冥汉化版）',
            '3173431824': 'Extra Equip Slot Plus +1/2/3（璇儿，海洋传说，武器成长）',
            '2945533555': '4/5/6格装备栏（适配[时崎狂三Tokisaki Kurumi]）',
            '2798599672': 'Extra Equip Slots+3',
            '3234180527': '更多装备栏（自用版）',
            '2946486855': 'extra-equip-slot-eqs',
            '2937603714': '白石—4/5/6格装备栏（适配mod版）',
            '2885978369': '自用五格装备栏（不妥协暴雨更新）',
            '2950481491': 'Extra Equip Slots+1/2/3 (Tell me what you want)',
            '3104797181': 'Extra Equip Slots(keep updating)',
            '2754944282': 'Extra Equip Slots (Update for Tropical Experience)',
            '3045668614': 'Extra Equip Slots',
            '2823530744': 'Extra Equip Slot Plus +',
        },
        'excluded': {
        },
        'cms_key': 'extra_equip_slots',
        'cms_extra': {
        },
    },
    {
        'key': 'fast_travel',
        'zh': '木牌传送',
        'en': 'Fast travel',
        'search': ['fast travel', '木牌传送'],
        'note': '',
        'mods': {
            '2119742489': '木牌传送 (GUI)',
            '2919458307': '木牌传送',
            '3353852416': 'Fast Travel(skined home sign)',
            '3243698152': '木牌传送(GUI)(Fixed)',
            '3034630375': '快速传送 (GUI)',
            '1530801499': 'Fast Travel (GUI)',
        },
        'excluded': {
        },
        'cms_key': 'fast_travel',
        'cms_extra': {
        },
    },
    {
        'key': 'icebox_fresh',
        'zh': '冰箱保鲜',
        'en': 'Icebox freshness',
        'search': ['icebox fresh', '冰箱 保鲜'],
        'note': '',
        'mods': {
            '2371484058': 'IceBox Refresh Food unlimited Day',
            '1898181913': '冰箱保鲜，反鲜',
            '2373374485': '冰箱永久保鲜（幽冥汉化版）',
            '1992091838': 'icefresh',
            '1247040097': '西米冰箱（保鲜，腐烂缓慢，反鲜）Ximi icebox (preservative, slow rotting, anti fresh)',
        },
        'excluded': {
        },
        'cms_key': 'better_icebox',
        'cms_extra': {
        },
    },
    {
        'key': 'invincible_structures',
        'zh': '建筑无敌',
        'en': 'Invincible structures',
        'search': ['invincible structures', '建筑无敌'],
        'note': '',
        'mods': {
            '503795626': 'Invincible Structures',
            '1614253006': '建筑无敌',
            '2516739401': 'Invincible Buildings / 建筑无敌',
        },
        'excluded': {
        },
        'cms_key': 'invincible_build',
        'cms_extra': {
        },
    },
    {
        'key': 'range_display',
        'zh': '范围显示',
        'en': 'Range display',
        'search': ['range indicator', '范围显示'],
        'note': '',
        'mods': {
            '3066199391': 'Range Indicator',
            '2575190872': '[Client] Display ALL Range',
            '3170953574': '范围显示',
            '3556247959': 'Display Attack Range [fixed]',
        },
        'excluded': {
        },
        'cms_key': 'attack_range',
        'cms_extra': {
        },
    },
    {
        'key': 'storm_cellar',
        'zh': '风暴地窖',
        'en': 'Storm cellar',
        'search': ['storm cellar', '风暴地窖'],
        'note': '',
        'mods': {
            '382177939': 'DST Storm Cellar',
            '2602484004': 'DST Storm Cellar with Working Fridge',
        },
        'excluded': {
        },
        'cms_key': 'storm_cellar',
        'cms_extra': {
        },
    },
    {
        'key': 'trap_enhance',
        'zh': '陷阱增强',
        'en': 'Trap enhancement',
        'search': ['trap teeth', '陷阱'],
        'note': '',
        'mods': {
            '1818848085': 'Trap Teeth Enhance',
            '2793625433': '犬牙陷阱和荆棘陷阱自动重置',
            '2048852282': 'smart teethtraps(智能无限犬牙陷阱）',
            '3068778455': 'enhanced trap teeth(configurable)——No durability + stackable + autoreset',
            '2566447922': 'AllTrapSet',
        },
        'excluded': {
        },
        'cms_key': 'trap_enhance',
        'cms_extra': {
        },
    },
]
