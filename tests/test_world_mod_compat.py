"""已核对 Mod 的世界创建与设置隔离回归测试（不依赖本机存档、Steam 或网络）。"""

from dataclasses import replace
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dstools.features.world.catalog_resolver import (  # noqa: E402
    IA_SHIPWRECKED_VANILLA_KEYS,
    IA_VOLCANO_VANILLA_KEYS,
    resolve_vanilla_settings,
)
from dstools.features.world.creation import (  # noqa: E402
    WorldShardPlan,
    WorldCreationPlan,
    create_world,
    validate_creation_plan,
)
from dstools.features.world.defaults import (  # noqa: E402
    default_plan_for_location,
    default_plans_from_cluster,
    three_worlds_layout,
)
from dstools.features.world.location_profiles import (  # noqa: E402
    CAVE_LOCATION,
    CAVES_SHARD,
    CHERRY_FOREST_MOD_ID,
    FOREST_LOCATION,
    IA_CORE_MOD_ID,
    IA_SHIPWRECKED_MOD_ID,
    MASTER_SHARD,
    PORKLAND_LOCATION,
    PORKLAND_MOD_ID,
    PORKLAND_PATCH_MOD_ID,
    SHIPWRECKED_LOCATION,
    THREE_WORLDS_MOD_ID,
    THREE_WORLDS_PATCH_MOD_ID,
    VOLCANO_LOCATION,
    get_location_definition,
    location_requirements_met,
    get_verified_creation_level_data,
    resolve_world_location_profile,
)
from dstools.features.world.mod_settings import get_mod_categories, get_mod_world_settings  # noqa: E402
from dstools.features.world.value_sets import (  # noqa: E402
    get_value_set, is_mod_readonly, typed_override_value,
)
from dstools.features.world.reader import WorldOverride, WorldPreset  # noqa: E402
from dstools.features.world.view_model import build_world_view_model  # noqa: E402
from dstools.features.mod.parser import parse_modinfo  # noqa: E402
from dstools.features.mod.presets import ModPreset  # noqa: E402
from dstools.qt.creation_wizard import CreationWizardDialog  # noqa: E402
from dstools.qt.pages.mod import ModPage  # noqa: E402
from dstools.features.cluster_config.config_manager import load_shard_config  # noqa: E402
from dstools.models import ModEntry, SaveSource  # noqa: E402
from dstools.shared.lua_parser import (  # noqa: E402
    LuaParseError,
    parse_lua_file,
    parse_lua_table,
    parse_lua_value,
    serialize_lua_table,
)
from _harness import run  # noqa: E402


ISLAND_LOCATIONS = (
    FOREST_LOCATION,
    CAVE_LOCATION,
    SHIPWRECKED_LOCATION,
    VOLCANO_LOCATION,
)

ISLAND_REQUIRED_OVERRIDES = {
    SHIPWRECKED_LOCATION: {
        "task_set": "shipwrecked",
        "start_location": "shipwrecked_default",
        "layout_mode": "LinkNodesByKeys",
        "has_ocean": True,
    },
    VOLCANO_LOCATION: {
        "task_set": "volcano",
        "start_location": "volcano_default",
        "layout_mode": "LinkNodesByKeys",
        "has_ocean": False,
    },
}

# 原版森林/洞穴的完整关卡定义实际取自游戏生成的模板，测试只补上创建层校验的 task_set
VANILLA_TASK_SETS = {FOREST_LOCATION: "default", CAVE_LOCATION: "cave_default"}


def _complete_plan(location: str) -> WorldShardPlan:
    plan = default_plan_for_location(location)
    if location in VANILLA_TASK_SETS:
        plan = replace(plan, overrides={**plan.overrides, "task_set": VANILLA_TASK_SETS[location]})
    return plan


def _expect_value_error(callback, message: str) -> None:
    try:
        callback()
    except ValueError:
        return
    raise AssertionError(message)


def test_location_profiles() -> None:
    vanilla = resolve_world_location_profile(set())
    assert vanilla.master_locations == (FOREST_LOCATION,)
    assert vanilla.caves_locations == (CAVE_LOCATION,)

    cherry = resolve_world_location_profile({CHERRY_FOREST_MOD_ID})
    assert cherry.master_locations == vanilla.master_locations
    assert cherry.caves_locations == vanilla.caves_locations

    porkland = resolve_world_location_profile({PORKLAND_MOD_ID})
    assert porkland.master_locations == (PORKLAND_LOCATION,)
    assert porkland.caves_locations == (CAVE_LOCATION,)
    assert porkland.default_master == PORKLAND_LOCATION

    core = resolve_world_location_profile({IA_CORE_MOD_ID})
    assert core.master_locations == (FOREST_LOCATION, CAVE_LOCATION)
    assert core.caves_locations == core.master_locations

    islands = resolve_world_location_profile({IA_SHIPWRECKED_MOD_ID})
    assert islands.master_locations == ISLAND_LOCATIONS
    assert islands.caves_locations == ISLAND_LOCATIONS
    assert islands.default_master == SHIPWRECKED_LOCATION
    assert islands.default_caves == VOLCANO_LOCATION
    assert islands.effective_mod_ids == frozenset(
        {IA_CORE_MOD_ID, IA_SHIPWRECKED_MOD_ID}
    )

    # 三合一整合版：五种世界都可用，但不补独立版依赖，也不触发猪镇+岛屿冒险警告
    three = resolve_world_location_profile({THREE_WORLDS_MOD_ID})
    assert set(three.master_locations) == {
        FOREST_LOCATION, CAVE_LOCATION, SHIPWRECKED_LOCATION, VOLCANO_LOCATION, PORKLAND_LOCATION,
    }
    assert (three.default_master, three.default_caves) == (FOREST_LOCATION, CAVE_LOCATION)
    assert three.effective_mod_ids == frozenset({THREE_WORLDS_MOD_ID}) and not three.warnings
    assert location_requirements_met(PORKLAND_LOCATION, {THREE_WORLDS_MOD_ID})
    assert location_requirements_met(VOLCANO_LOCATION, {THREE_WORLDS_MOD_ID})
    # 与原 Mod（海难会连带补核心）同时启用时给出警告，创建校验据此拦下
    clash = resolve_world_location_profile({THREE_WORLDS_MOD_ID, IA_SHIPWRECKED_MOD_ID})
    assert clash.warnings and "岛屿冒险 - 核心" in clash.warnings[0]

    # 三界同行补丁：自动带上四个原 Mod，五种世界都可用，不触发猪镇+岛屿冒险警告
    patch_profile = resolve_world_location_profile({f"workshop-{THREE_WORLDS_PATCH_MOD_ID}"})
    assert patch_profile.effective_mod_ids == frozenset({
        THREE_WORLDS_PATCH_MOD_ID, IA_CORE_MOD_ID, IA_SHIPWRECKED_MOD_ID, PORKLAND_MOD_ID, PORKLAND_PATCH_MOD_ID,
    })
    assert set(patch_profile.caves_locations) == set(three.master_locations) and not patch_profile.warnings
    # 与旧整合版同时启用时只报一条取代提示
    legacy = resolve_world_location_profile({THREE_WORLDS_PATCH_MOD_ID, THREE_WORLDS_MOD_ID})
    assert len(legacy.warnings) == 1 and "三合一整合版" in legacy.warnings[0]


def test_setting_location_isolation() -> None:
    cherry_forest = get_mod_world_settings(
        {CHERRY_FOREST_MOD_ID}, FOREST_LOCATION, True,
    )
    cherry_cave = get_mod_world_settings(
        {CHERRY_FOREST_MOD_ID}, CAVE_LOCATION, True,
    )
    cherry_volcano = get_mod_world_settings(
        {CHERRY_FOREST_MOD_ID}, VOLCANO_LOCATION, True,
    )
    assert "cherry_bugseason" in cherry_forest
    assert not cherry_cave
    assert "cherry_bugseason" in cherry_volcano
    assert "cherry_trees" not in cherry_volcano

    porkland = get_mod_world_settings(
        {PORKLAND_MOD_ID}, PORKLAND_LOCATION, True,
    )
    porkland_in_forest = get_mod_world_settings(
        {PORKLAND_MOD_ID}, FOREST_LOCATION, True,
    )
    assert porkland
    assert all(item.locations == frozenset({PORKLAND_LOCATION}) for item in porkland.values())
    assert not porkland_in_forest

    island_master = get_mod_world_settings(
        {IA_CORE_MOD_ID, IA_SHIPWRECKED_MOD_ID}, SHIPWRECKED_LOCATION, True,
    )
    island_secondary = get_mod_world_settings(
        {IA_CORE_MOD_ID, IA_SHIPWRECKED_MOD_ID}, SHIPWRECKED_LOCATION, False,
    )
    assert "poison" in island_master
    assert "poison" not in island_secondary
    assert "shipwrecked_season_start" in island_master
    assert "shipwrecked_season_start" not in island_secondary

    # 同名 poison 先注册（岛屿冒险核心 priority 5）的生效；它在非主分片不可见时也不能露出猪镇那条
    both = {IA_CORE_MOD_ID, PORKLAND_MOD_ID}
    assert get_mod_world_settings(both, PORKLAND_LOCATION, True)["poison"].mod_id == IA_CORE_MOD_ID
    assert "poison" not in get_mod_world_settings(both, PORKLAND_LOCATION, False)

    # 热带冒险用 AddCustomizeGroup 自建的两组必须各自成为分类，否则整组不显示
    tropical = get_mod_world_settings({"2986194136"}, FOREST_LOCATION, True)
    assert [key for key, _ in get_mod_categories(tropical)] == [
        "mod_group_ta_worldgen", "mod_group_ta_climate",
    ]
    # 创建向导可编辑世界生成：数字取值写回 Lua 数字，没登记取值的 Mod 生成项保持只读
    multi = get_value_set("world_size_multi", tropical, location=FOREST_LOCATION, is_rule=False)
    assert typed_override_value("2", multi) == 2 and typed_override_value("never", ["never", "5"]) == "never"
    island = get_mod_world_settings({IA_SHIPWRECKED_MOD_ID}, SHIPWRECKED_LOCATION, True)
    assert is_mod_readonly("volcano", island) and not is_mod_readonly("world_size", island)

    # 三合一整合版与三个独立版同时启用时看到的条目一致
    standalone = {IA_CORE_MOD_ID, IA_SHIPWRECKED_MOD_ID, PORKLAND_MOD_ID}
    for location, is_master in ((SHIPWRECKED_LOCATION, True), (PORKLAND_LOCATION, False)):
        assert get_mod_world_settings({"3811652910"}, location, is_master).keys() == (
            get_mod_world_settings(standalone, location, is_master).keys())

    bwb_forest = get_mod_world_settings({"3360553731"}, FOREST_LOCATION, True)
    bwb_cave = get_mod_world_settings({"3360553731"}, CAVE_LOCATION, True)
    bwb_secondary = get_mod_world_settings({"3360553731"}, CAVE_LOCATION, False)
    assert len(get_mod_world_settings({"3360553731"})) == 25
    assert {"horrorhounds", "wargwave", "lunarthrall_plant"} <= set(bwb_forest)
    assert "nightmareclock" not in bwb_forest
    assert {"nightmareclock", "fungusfog", "worm_megaboss_setting"} <= set(bwb_cave)
    assert "horrorhounds" not in bwb_cave
    assert "cave_season_start" in bwb_cave
    assert "cave_season_start" not in bwb_secondary

    # 新建存档向导的“世界设置/世界生成”子页签共用这个视图模型；两类
    # 条目都必须进入各自面板，而不是只在已有存档的世界设置页生效。
    creation_view = build_world_view_model(
        WorldPreset(location=CAVE_LOCATION), bwb_cave, is_master_world=True,
    )
    assert any(
        row.key == "nightmareclock"
        for rows in creation_view.rules_by_category.values() for row in rows
    )
    assert any(
        row.key == "cave_season_start"
        for rows in creation_view.generation_by_category.values() for row in rows
    )


def test_bwb_dstu_combination_settings() -> None:
    bwb_id = "3360553731"
    dstu_live_id = "2039181790"
    dstu_beta_id = "3193922031"

    assert "widow_setting" not in get_mod_world_settings({bwb_id})
    assert "widow_bags" not in get_mod_world_settings({dstu_beta_id})

    for dstu_id in (dstu_live_id, dstu_beta_id):
        combined = get_mod_world_settings({
            f"workshop-{bwb_id}", f"workshop-{dstu_id}",
        })
        assert len(combined) == 27
        assert combined["widow_setting"].name == {
            "zh": "黑寡妇", "en": "Hooded Widow",
        }
        assert combined["widow_setting"].category == "bosses"
        assert combined["widow_setting"].values == [
            "never", "rare", "default", "often", "always",
        ]
        assert combined["widow_setting"].icon_element == "widow.tex"
        assert combined["widow_bags"].name == {
            "zh": "寡妇茧", "en": "Silky Cocoons",
        }
        assert combined["widow_bags"].category == "world"
        assert combined["widow_bags"].values == [
            "never", "rare", "default", "often", "always",
        ]
        assert combined["widow_bags"].icon_element == "widow_bags.tex"

    assert len(get_mod_world_settings({
        bwb_id, dstu_live_id, dstu_beta_id,
    })) == 27

    forest = get_mod_world_settings(
        {bwb_id, dstu_beta_id}, FOREST_LOCATION, True,
    )
    cave = get_mod_world_settings(
        {bwb_id, dstu_beta_id}, CAVE_LOCATION, True,
    )
    assert {"widow_setting", "widow_bags"} <= set(forest)
    assert not {"widow_setting", "widow_bags"}.intersection(cave)

    creation_view = build_world_view_model(
        WorldPreset(location=FOREST_LOCATION), forest, is_master_world=True,
    )
    assert any(
        row.key == "widow_setting"
        for row in creation_view.rules_by_category["bosses"]
    )
    assert any(
        row.key == "widow_bags"
        for row in creation_view.rules_by_category["world"]
    )


def test_island_vanilla_catalogs() -> None:
    shipwrecked = {
        **resolve_vanilla_settings(SHIPWRECKED_LOCATION, True),
        **resolve_vanilla_settings(SHIPWRECKED_LOCATION, False),
    }
    volcano = {
        **resolve_vanilla_settings(VOLCANO_LOCATION, True),
        **resolve_vanilla_settings(VOLCANO_LOCATION, False),
    }
    # shipwrecked 是 Master，show_global=True：白名单 + world=nil 的
    # global/events/survivor 全局项都要显示。
    assert IA_SHIPWRECKED_VANILLA_KEYS <= set(shipwrecked)
    assert {"day", "ghostenabled", "krampus", "specialevent", "autumn"} <= set(shipwrecked)
    assert "crow_carnival" in shipwrecked and "extrastartingitems" in shipwrecked
    # volcano 是 Caves，show_global=False：只显示白名单。
    assert set(volcano) == IA_VOLCANO_VANILLA_KEYS


def test_every_registered_mod_has_verified_version() -> None:
    # 漏写已核对版本时 Mod 更新后不会有任何提示
    from dstools.features.world.mod_settings import MOD_VERIFIED_VERSIONS, MOD_WORLD_SETTINGS
    assert set(MOD_VERIFIED_VERSIONS) == set(MOD_WORLD_SETTINGS)


def test_bwb_hides_and_reorders_patched_vanilla_settings() -> None:
    vanilla = build_world_view_model(WorldPreset(location=CAVE_LOCATION), {})
    assert any(
        row.key == "wormattacks_boss"
        for rows in vanilla.rules_by_category.values() for row in rows
    )

    persisted = WorldOverride("wormattacks_boss", "often")
    bwb = build_world_view_model(
        WorldPreset(location=CAVE_LOCATION, overrides=[persisted]),
        get_mod_world_settings({"3360553731"}, CAVE_LOCATION, True),
    )
    visible_keys = {
        row.key for rows in bwb.rules_by_category.values() for row in rows
    }
    assert "wormattacks_boss" not in visible_keys
    # 隐藏只影响界面；已有存档中的值必须继续原样保留，避免保存时误删。
    assert persisted.value == "often"

    world_keys = [row.key for row in bwb.rules_by_category["world"]]
    assert world_keys[:6] == [
        "wormattacks", "stealworms", "starworms", "vineworms",
        "tideworms", "worm_ancient",
    ]
    creature_keys = [row.key for row in bwb.rules_by_category["creatures"]]
    assert creature_keys[0] == "rocky_setting"


def test_island_creation_defaults_are_complete() -> None:
    """回归真实报错：火山没有 task_set 时 Level:ChooseTasks 会直接断言。"""
    for location, required in ISLAND_REQUIRED_OVERRIDES.items():
        source = get_verified_creation_level_data(location)
        plan = default_plan_for_location(location)
        assert plan.overrides == source["overrides"]
        assert plan.level_data["version"] == 4
        assert plan.level_data["required_prefabs"] == ["multiplayer_portal"]
        for key, value in required.items():
            assert plan.overrides[key] == value

    assert get_value_set(
        "task_set", location=SHIPWRECKED_LOCATION, is_rule=False,
    ) == ["shipwrecked"]
    assert get_value_set(
        "start_location", location=SHIPWRECKED_LOCATION, is_rule=False,
    ) == ["shipwrecked_default", "shipwrecked_plus", "shipwrecked_darkness"]
    assert get_value_set(
        "task_set", location=VOLCANO_LOCATION, is_rule=False,
    ) == ["volcano"]
    assert get_value_set(
        "start_location", location=VOLCANO_LOCATION, is_rule=False,
    ) == ["volcano_default"]


def test_island_writer_repairs_partial_legacy_plan() -> None:
    """创建层必须兜住旧草稿中的空 overrides，不能再次生成本次坏存档。"""
    with TemporaryDirectory() as directory:
        volcano = get_location_definition(VOLCANO_LOCATION)
        shipwrecked = get_location_definition(SHIPWRECKED_LOCATION)
        plan = WorldCreationPlan(
            "legacy_partial",
            WorldShardPlan(
                SHIPWRECKED_LOCATION, shipwrecked.default_preset_id,
                shipwrecked.name_zh,
            ),
            WorldShardPlan(
                VOLCANO_LOCATION, volcano.default_preset_id, volcano.name_zh,
            ),
            mod_ids=frozenset({IA_SHIPWRECKED_MOD_ID}),
        )
        output = create_world(plan, Path(directory))
        master = parse_lua_file(output / "Master" / "leveldataoverride.lua")
        caves = parse_lua_file(output / "Caves" / "leveldataoverride.lua")
        assert master["overrides"]["task_set"] == "shipwrecked"
        assert caves["overrides"]["task_set"] == "volcano"
        assert caves["background_node_range"] == {"1": 0, "2": 0}


def test_island_cross_shard_reuses_verified_vanilla_template() -> None:
    """森林/洞穴互换槽位时必须复制完整官方模板，而非创建空计划。"""
    tab = CreationWizardDialog.__new__(CreationWizardDialog)
    forest = WorldShardPlan(
        FOREST_LOCATION, "SURVIVAL_TOGETHER", "森林",
        overrides={"task_set": "default"}, level_data={"version": 4},
    )
    cave = WorldShardPlan(
        CAVE_LOCATION, "DST_CAVE", "洞穴",
        overrides={"task_set": "cave_default"}, level_data={"version": 4},
    )
    tab._plan_master = forest
    tab._plan_caves = cave
    tab._location_drafts = {
        (MASTER_SHARD, FOREST_LOCATION): forest,
        (CAVES_SHARD, CAVE_LOCATION): cave,
    }
    tab._world_profile = resolve_world_location_profile({IA_SHIPWRECKED_MOD_ID})
    tab._switch_shard_location(CAVES_SHARD, FOREST_LOCATION, render=False)
    assert tab._plan_caves is not forest
    assert tab._plan_caves.overrides == forest.overrides
    assert tab._plan_caves.level_data == forest.level_data


def test_lua_multiline_roundtrip() -> None:
    original = {"desc": "第一行\n第二行\t完成", "enabled": True}
    assert parse_lua_table(serialize_lua_table(original)) == original


def test_lua_file_strips_klei_persistent_string_header() -> None:
    # TheSim:SetPersistentString() 写入的 "KLEI<版本号> " 文件头必须被去掉
    with TemporaryDirectory() as directory:
        path = Path(directory) / "leveldataoverride.lua"
        path.write_text('KLEI     1 return { id="DST_CAVE" }', encoding="utf-8")
        assert parse_lua_file(path) == {"id": "DST_CAVE"}


def test_lua_value_rejects_function_call_instead_of_guessing() -> None:
    # 作者把 default 写成 en_zh("en", "zh") 这类函数调用时必须报错（调用方回退保留原始文本），不能得到字面量 "en_zh"
    try:
        parse_lua_value('en_zh("en", "zh")')
        assert False, "应该因为有没吃完的 token 而报错"
    except LuaParseError:
        pass


def test_lua_value_rejects_bare_identifier_without_trailing_tokens() -> None:
    # 不是 true/false/nil 的裸标识符（如 default = SomeConstant）没有残留 token，也必须报错
    try:
        parse_lua_value("SomeConstant")
        assert False, "裸标识符（非 true/false/nil）应该报错，不能当字面量"
    except LuaParseError:
        pass
    # true/false/nil 三个真正的关键字必须继续正常工作，不能被误伤。
    assert parse_lua_value("true") is True
    assert parse_lua_value("false") is False
    assert parse_lua_value("nil") is None


def test_en_zh_mod_metadata() -> None:
    with TemporaryDirectory() as directory:
        mod = Path(directory) / "1234567890"
        mod.mkdir()
        (mod / "modinfo.lua").write_text(
            'local function en_zh(en, zh) return zh end\n'
            'name = en_zh("Island Adventures - Core", "岛屿冒险 - 核心")\n'
            'author = en_zh("Island Adventures Team", "岛屿冒险团队")\n'
            'description = en_zh("Core content", "核心内容")\n'
            'version = "1.0"\n',
            encoding="utf-8",
        )
        info = parse_modinfo(mod)
        assert info is not None
        assert info.name == "岛屿冒险 - 核心"
        assert info.author == "岛屿冒险团队"
        assert info.description == "核心内容"



class _LabelProbe:
    def __init__(self):
        self.value = ""

    def setText(self, value):
        self.value = value


def _dependency_wizard() -> CreationWizardDialog:
    tab = CreationWizardDialog.__new__(CreationWizardDialog)
    tab._status_label = _LabelProbe()
    tab._selected_mod_ids = {f"workshop-{IA_SHIPWRECKED_MOD_ID}"}
    tab._mod_data = {
        f"workshop-{IA_SHIPWRECKED_MOD_ID}": ModEntry(
            workshop_id=f"workshop-{IA_SHIPWRECKED_MOD_ID}", enabled=True,
        ),
        f"workshop-{IA_CORE_MOD_ID}": ModEntry(
            workshop_id=f"workshop-{IA_CORE_MOD_ID}", enabled=False,
        ),
    }
    return tab


def _main_mod_dependency_page() -> ModPage:
    tab = ModPage.__new__(ModPage)
    tab.get_cluster = lambda: SimpleNamespace(source=SaveSource.SERVER)
    tab.window = lambda: None
    tab._luajit_mod_locked = False
    tab._mod_infos = {}
    tab._mod_data = {
        f"workshop-{IA_SHIPWRECKED_MOD_ID}": ModEntry(
            workshop_id=f"workshop-{IA_SHIPWRECKED_MOD_ID}", enabled=False,
        ),
        f"workshop-{IA_CORE_MOD_ID}": ModEntry(
            workshop_id=f"workshop-{IA_CORE_MOD_ID}", enabled=False,
        ),
    }
    tab.dirty_marked = False
    tab._mark_dirty = lambda: setattr(tab, "dirty_marked", True)
    tab._update_enabled_count = lambda: None
    tab._render_list = lambda: None
    return tab


def test_creation_dependency_confirmation() -> None:
    accepted = _dependency_wizard()
    with patch(
        "dstools.qt.creation_wizard.dialogs.ask_yes_no", return_value=True,
    ) as confirm:
        assert accepted._ensure_mod_dependencies(show_dialog=True)
    confirm.assert_called_once()
    core_key = f"workshop-{IA_CORE_MOD_ID}"
    child_key = f"workshop-{IA_SHIPWRECKED_MOD_ID}"
    assert accepted._mod_data[core_key].enabled
    assert core_key in accepted._selected_mod_ids

    declined = _dependency_wizard()
    with patch(
        "dstools.qt.creation_wizard.dialogs.ask_yes_no", return_value=False,
    ):
        assert not declined._ensure_mod_dependencies(show_dialog=True)
    assert not declined._mod_data[child_key].enabled
    assert child_key not in declined._selected_mod_ids
    assert not declined._mod_data[core_key].enabled


def test_main_mod_dependency_confirmation() -> None:
    child_key = f"workshop-{IA_SHIPWRECKED_MOD_ID}"
    core_key = f"workshop-{IA_CORE_MOD_ID}"

    accepted = _main_mod_dependency_page()
    with patch(
        "dstools.qt.pages.mod.dialogs.ask_yes_no", return_value=True,
    ) as confirm:
        accepted._on_toggle(child_key)
    confirm.assert_called_once()
    assert accepted._mod_data[child_key].enabled
    assert accepted._mod_data[core_key].enabled
    assert accepted.dirty_marked

    declined = _main_mod_dependency_page()
    with patch(
        "dstools.qt.pages.mod.dialogs.ask_yes_no", return_value=False,
    ):
        declined._on_toggle(child_key)
    assert not declined._mod_data[child_key].enabled
    assert not declined._mod_data[core_key].enabled
    assert not declined.dirty_marked

    # 三界同行补丁：一次确认启用四个依赖；关闭其中任一依赖时补丁一起关闭
    patch_key = f"workshop-{THREE_WORLDS_PATCH_MOD_ID}"
    page = _main_mod_dependency_page()
    for mod_id in (THREE_WORLDS_PATCH_MOD_ID, PORKLAND_MOD_ID, PORKLAND_PATCH_MOD_ID):
        page._mod_data[f"workshop-{mod_id}"] = ModEntry(workshop_id=f"workshop-{mod_id}", enabled=False)
    with patch("dstools.qt.pages.mod.dialogs.ask_yes_no", return_value=True) as confirm:
        page._on_toggle(patch_key)
    confirm.assert_called_once()
    assert all(mod.enabled for mod in page._mod_data.values())
    page._on_toggle(f"workshop-{PORKLAND_PATCH_MOD_ID}")
    assert not page._mod_data[patch_key].enabled and page._mod_data[core_key].enabled


def test_pending_mod_world_preview() -> None:
    cluster = SimpleNamespace(name="Cluster_Test", path=Path("C:/saves/Cluster_Test"))
    tab = ModPage.__new__(ModPage)
    tab._dirty = True
    tab._loading = False
    tab._loading_key = (cluster.name, "Master")
    tab._mod_data = {
        "workshop-1289779251": ModEntry(
            workshop_id="workshop-1289779251", enabled=True,
        ),
        "workshop-3322803908": ModEntry(
            workshop_id="workshop-3322803908", enabled=False,
        ),
    }
    tab.get_cluster = lambda: cluster
    assert tab.get_pending_enabled_mod_ids(cluster) == frozenset({CHERRY_FOREST_MOD_ID})

    # 没有待保存状态，或请求的是另一个存档时，必须退回磁盘数据。
    tab._dirty = False
    assert tab.get_pending_enabled_mod_ids(cluster) is None
    tab._dirty = True
    other = SimpleNamespace(name="Other", path=Path("C:/saves/Other"))
    assert tab.get_pending_enabled_mod_ids(other) is None


def test_creation_preset_apply_refreshes_mod_list() -> None:
    refreshed = []
    statuses = []
    tab = CreationWizardDialog.__new__(CreationWizardDialog)
    tab._mod_overrides = {}
    tab._selected_mod_ids = set()
    tab._mod_data = {
        "workshop-123": ModEntry(workshop_id="workshop-123", enabled=False),
    }
    tab._initialized_pages = set()
    tab._ensure_mod_dependencies = lambda show_dialog: True
    tab._scan_installed_mods = lambda force=False: refreshed.append(force)
    tab._status_label = SimpleNamespace(setText=statuses.append)
    preset = ModPreset(
        name="启用测试",
        mods={
            "workshop-123": {
                "enabled": True,
                "configuration_options": {"mode": "hard"},
            }
        },
    )

    tab._apply_preset_to_session(preset)

    assert tab._selected_mod_ids == {"workshop-123"}
    assert tab._mod_data["workshop-123"].enabled is True
    assert tab._mod_data["workshop-123"].configuration_options == {"mode": "hard"}
    assert refreshed == [False]
    assert statuses == ["已载入 Mod 配置集：启用测试"]


def test_creation_matrix() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        count = 0
        for master_location in ISLAND_LOCATIONS:
            for caves_location in ISLAND_LOCATIONS:
                plan = WorldCreationPlan(
                    cluster_name=f"islands_{count}",
                    master=_complete_plan(master_location),
                    caves=_complete_plan(caves_location),
                    # 故意只传内容包，验证创建层会补齐硬依赖核心。
                    mod_ids=frozenset({IA_SHIPWRECKED_MOD_ID}),
                    mod_overrides={
                        IA_SHIPWRECKED_MOD_ID: {
                            "enabled": True,
                            "configuration_options": {"regression": "kept"},
                        },
                    },
                )
                output = create_world(plan, root)
                for shard in ("Master", "Caves"):
                    leveldata = parse_lua_file(
                        output / shard / "leveldataoverride.lua"
                    )
                    expected = master_location if shard == "Master" else caves_location
                    assert leveldata["location"] == expected
                    if expected in ISLAND_REQUIRED_OVERRIDES:
                        assert leveldata["version"] == 4
                        # Lua 数组由项目解析器表示为从 "1" 开始的映射。
                        assert set(leveldata["required_prefabs"].values()) == {
                            "multiplayer_portal"
                        }
                        for key, value in ISLAND_REQUIRED_OVERRIDES[expected].items():
                            assert leveldata["overrides"][key] == value

                    mods = parse_lua_file(output / shard / "modoverrides.lua")
                    assert mods[f"workshop-{IA_CORE_MOD_ID}"]["enabled"] is True
                    content = mods[f"workshop-{IA_SHIPWRECKED_MOD_ID}"]
                    assert content["enabled"] is True
                    assert content["configuration_options"]["regression"] == "kept"

                # 读取器不得再把 Caves 文件夹硬编码成 cave location。
                loaded_master, loaded_caves = default_plans_from_cluster(output)
                assert loaded_master.location == master_location
                assert loaded_caves.location == caves_location
                count += 1
        assert count == 16


def test_creation_rejects_invalid_combinations() -> None:
    invalid_shipwrecked = WorldCreationPlan(
        "invalid_shipwrecked",
        _complete_plan(SHIPWRECKED_LOCATION),
        _complete_plan(CAVE_LOCATION),
    )
    _expect_value_error(
        lambda: validate_creation_plan(invalid_shipwrecked),
        "未启用岛屿冒险时不应允许海难世界",
    )

    # 两个 Mod 都会重写官方创建界面的候选 location；真实加载顺序尚未
    # 用游戏存档确认前，禁止生成一个看似可用但实际含义不确定的组合。
    conflict = WorldCreationPlan(
        "unverified_frontend_conflict",
        _complete_plan(SHIPWRECKED_LOCATION),
        _complete_plan(VOLCANO_LOCATION),
        mod_ids=frozenset({PORKLAND_MOD_ID, IA_SHIPWRECKED_MOD_ID}),
    )
    _expect_value_error(
        lambda: validate_creation_plan(conflict),
        "猪镇和岛屿冒险的未验证前端冲突组合不应静默通过",
    )


def test_porkland_creation() -> None:
    with TemporaryDirectory() as directory:
        plan = WorldCreationPlan(
            "porkland",
            _complete_plan(PORKLAND_LOCATION),
            _complete_plan(CAVE_LOCATION),
            mod_ids=frozenset({PORKLAND_MOD_ID}),
        )
        output = create_world(plan, Path(directory))
        master = parse_lua_file(output / "Master" / "leveldataoverride.lua")
        caves = parse_lua_file(output / "Caves" / "leveldataoverride.lua")
        assert (master["location"], master["id"]) == (
            PORKLAND_LOCATION,
            "PORKLAND_DEFAULT",
        )
        assert (caves["location"], caves["id"]) == (CAVE_LOCATION, "DST_CAVE")
        # leveldataoverride 必须是完整定义，缺 task_set 世界生成直接报错
        assert master["overrides"]["task_set"] == "porkland"


def test_multi_shard_creation() -> None:
    """原版模板可重复添加多层世界，并为每层写出独立配置。"""
    with TemporaryDirectory() as directory:
        plan = WorldCreationPlan(
            "multi_vanilla",
            _complete_plan(FOREST_LOCATION),
            _complete_plan(CAVE_LOCATION),
            extra_shards={
                "Forest": _complete_plan(FOREST_LOCATION),
                "Caves_2": _complete_plan(CAVE_LOCATION),
            },
        )
        output = create_world(plan, Path(directory))
        assert parse_lua_file(output / "Forest" / "leveldataoverride.lua")["location"] == FOREST_LOCATION
        assert parse_lua_file(output / "Caves_2" / "leveldataoverride.lua")["location"] == CAVE_LOCATION
        configs = {
            name: load_shard_config(output / name)
            for name in ("Master", "Caves", "Forest", "Caves_2")
        }
        assert configs["Master"].shard["is_master"] is True
        assert all(configs[name].shard["is_master"] is False for name in ("Caves", "Forest", "Caves_2"))
        assert configs["Forest"].shard["name"] == "Forest"
        assert configs["Caves_2"].shard["name"] == "Caves_2"
        assert {configs[name].shard["id"] for name in ("Caves", "Forest", "Caves_2")} == {2, 3, 4}
        assert len({config.network["server_port"] for config in configs.values()}) == 4


def test_vanilla_world_without_task_set_is_rejected() -> None:
    """回归真实报错：额外森林没带模板数据，开服报 Must specify the task set for a level!"""
    plan = WorldCreationPlan(
        "missing_task_set",
        _complete_plan(FOREST_LOCATION),
        _complete_plan(CAVE_LOCATION),
        extra_shards={"Forest": default_plan_for_location(FOREST_LOCATION)},
    )
    _expect_value_error(lambda: validate_creation_plan(plan), "缺 task_set 的原版世界不应写盘")


def test_add_shard_to_existing_cluster() -> None:
    """单世界旧存档连加两个世界：打开多世界、编号与端口不撞、各世界启用同一份万界之门配置（含自定义名称）、旧存档不动。"""
    from dstools.features.world.shard_management import (
        add_shard, list_worlds, planned_world_names, shard_name_error, world_name_error,
    )
    import shutil

    from dstools.models import ClusterConfig
    from dstools.shared.discovery import _build_cluster
    from dstools.shared.ini_parser import parse_cluster_ini

    with TemporaryDirectory() as directory:
        single = WorldCreationPlan(
            "single", _complete_plan(FOREST_LOCATION), None,
            cluster_ini=ClusterConfig(network={"cluster_name": "single"}, shard={"shard_enabled": False}),
            mod_overrides={"workshop-123": {"enabled": True, "configuration_options": {"mode": "hard"}}},
        )
        output = create_world(single, Path(directory))
        save_file = output / "Master" / "save" / "session.bin"
        save_file.parent.mkdir()
        save_file.write_bytes(b"old world")

        add_shard(_build_cluster(output, SaveSource.SERVER), "Forest2",
                  _complete_plan(FOREST_LOCATION), used_ports={10998})
        cluster = _build_cluster(output, SaveSource.SERVER)
        assert planned_world_names(cluster, CAVE_LOCATION, "zh") == ("洞穴", {"森林", "森林 2"})
        assert shard_name_error(cluster, "Caves 2") and shard_name_error(cluster, "forest2")
        assert world_name_error(cluster, CAVE_LOCATION, "森林 2", "zh")  # 和已有世界重名
        assert world_name_error(cluster, CAVE_LOCATION, "  ", "zh")
        second = add_shard(cluster, "Caves", _complete_plan(CAVE_LOCATION), world_name="地下城")
        # 编号 3 的 Caves 也排在编号 2 的 Forest2 前面：不能改文件夹的原版世界放最前
        assert [e.shard_name for e in list_worlds(_build_cluster(output, SaveSource.SERVER))] == [
            "Master", "Caves", "Forest2"]

        cluster_ini = parse_cluster_ini(output / "cluster.ini")
        assert cluster_ini.shard["shard_enabled"] is True
        assert cluster_ini.shard["cluster_key"] and cluster_ini.shard["master_port"]
        configs = {name: load_shard_config(output / name) for name in ("Master", "Forest2", "Caves")}
        assert configs["Master"].shard["is_master"] is True
        assert [configs[name].shard["id"] for name in ("Forest2", "Caves")] == [2, 3]
        assert configs["Forest2"].network["server_port"] != 10998  # 避开外部占用
        ports = [config.network.get("server_port", 10999) for config in configs.values()]
        ports.append(cluster_ini.shard["master_port"])
        assert len(set(ports)) == len(ports)
        worlds = {"1": {"name": "森林"}, "2": {"name": "森林 2"}, "3": {"name": "地下城"}}
        for name in ("Master", "Forest2", "Caves"):
            mods = parse_lua_file(output / name / "modoverrides.lua")
            assert mods["workshop-123"]["configuration_options"] == {"mode": "hard"}  # 原有 Mod 原样保留
            gate = mods["workshop-3822260794"]
            assert gate["enabled"] is True
            assert gate["configuration_options"] == {"worlds": worlds, "language": "zh"}
            # 数字键会让游戏把 worlds 当数组 json 编码，Shard ID 很大时新世界生成拿不到 Mod
            assert '["3"] = {' in (output / name / "modoverrides.lua").read_text(encoding="utf-8")
        assert parse_lua_file(second / "leveldataoverride.lua")["overrides"]["task_set"] == "cave_default"
        assert save_file.read_bytes() == b"old world"

        cluster = _build_cluster(output, SaveSource.SERVER)
        _expect_value_error(lambda: add_shard(cluster, "caves", _complete_plan(CAVE_LOCATION)),
                            "同名世界（不区分大小写）不应覆盖")
        _expect_value_error(lambda: add_shard(cluster, "Forest3", default_plan_for_location(FOREST_LOCATION)),
                            "缺 task_set 的原版世界不应写盘")
        assert sorted(path.name for path in output.iterdir() if path.is_dir()) == ["Caves", "Forest2", "Master"]

        # 手动删掉的世界不再占名字，新世界复用编号也不继承旧名称
        shutil.rmtree(output / "Forest2")
        assert planned_world_names(_build_cluster(output, SaveSource.SERVER), FOREST_LOCATION, "zh") == (
            "森林 2", {"森林", "地下城"})


def test_manage_existing_shards() -> None:
    """管理世界：改万界之门名称/隐藏/上限各世界同步；Master/Caves 不能改文件夹；改名后还原旧备份
    不留下多余世界；删除世界从 worlds 去掉它，目录移不走时配置恢复原样。"""
    from dstools.features.local_service.backup_manager import (
        create_backup, restore_backup, shards_missing_from_backup,
    )
    from dstools.features.world.shard_management import (
        add_shard, delete_shard, list_worlds, rename_shard_error, rename_shard_folder,
        renamed_world_error, unreachable_shards, update_world_entry,
    )
    import shutil

    from dstools.shared.discovery import _build_cluster

    with TemporaryDirectory() as directory:
        two = WorldCreationPlan("two", _complete_plan(FOREST_LOCATION), _complete_plan(CAVE_LOCATION))
        output = create_world(two, Path(directory))
        assert unreachable_shards(_build_cluster(output, SaveSource.SERVER)) == []  # Master/Caves 靠原版洞口
        add_shard(_build_cluster(output, SaveSource.SERVER), "Forest2", _complete_plan(FOREST_LOCATION))
        cluster = _build_cluster(output, SaveSource.SERVER)
        assert unreachable_shards(cluster) == []
        originals = {name: (output / name / "modoverrides.lua").read_bytes() for name in ("Master", "Caves", "Forest2")}
        for name in originals:  # 用户在 Mod 页关掉万界之门
            path = output / name / "modoverrides.lua"
            path.write_text(path.read_text(encoding="utf-8").replace('["enabled"] = true', '["enabled"] = false'),
                            encoding="utf-8")
        assert unreachable_shards(cluster) == ["Forest2"]
        # 三合一（含岛屿冒险核心）按世界类型连接，海洋之椅能到 Forest2，不拦
        path = output / "Caves" / "modoverrides.lua"
        path.write_text(path.read_text(encoding="utf-8").replace(
            "return {", 'return {\n  ["workshop-3811652910"] = { ["enabled"] = true },', 1), encoding="utf-8")
        assert unreachable_shards(cluster) == []
        for name, content in originals.items():
            (output / name / "modoverrides.lua").write_bytes(content)

        assert renamed_world_error(cluster, "Forest2", "森林")  # 和主世界重名
        update_world_entry(cluster, "Forest2", " 新大陆 ", hidden=True, limit=6)
        for name in ("Master", "Caves", "Forest2"):
            worlds = parse_lua_file(output / name / "modoverrides.lua")["workshop-3822260794"][
                "configuration_options"]["worlds"]
            assert worlds["3"] == {"name": "新大陆", "hidden": True, "limit": 6}
        update_world_entry(_build_cluster(output, SaveSource.SERVER), "Forest2", "新大陆", hidden=False, limit=0)
        entry = next(e for e in list_worlds(_build_cluster(output, SaveSource.SERVER)) if e.shard_name == "Forest2")
        assert (entry.shard_id, entry.name, entry.hidden, entry.limit) == (3, "新大陆", False, 0)

        assert rename_shard_error(cluster, "Master", "Home") and rename_shard_error(cluster, "Caves", "Under")
        assert rename_shard_error(cluster, "Forest2", "caves")  # 不能占用原版洞口用的名字
        backup = create_backup(output)
        rename_shard_folder(cluster, "Forest2", "NewLand")
        assert load_shard_config(output / "NewLand").shard["name"] == "NewLand"
        assert not (output / "Forest2").exists()
        assert [e.name for e in list_worlds(_build_cluster(output, SaveSource.SERVER))
                if e.shard_name == "NewLand"] == ["新大陆"]  # 编号不变，名称跟着走
        assert shards_missing_from_backup(output, backup) == ["NewLand"]
        restore_backup(output, backup)
        assert sorted(p.name for p in output.iterdir() if p.is_dir()) == ["Caves", "Forest2", "Master"]

        cluster = _build_cluster(output, SaveSource.SERVER)
        _expect_value_error(lambda: delete_shard(cluster, "Master", remove=shutil.rmtree), "主世界不应能删除")
        before = (output / "Master" / "modoverrides.lua").read_bytes()

        def locked(_path):
            raise OSError("文件被占用")

        try:
            delete_shard(cluster, "Forest2", remove=locked)
            raise AssertionError("目录移不走时应抛出异常")
        except OSError:
            pass
        assert (output / "Master" / "modoverrides.lua").read_bytes() == before
        delete_shard(cluster, "Forest2", remove=shutil.rmtree)
        assert not (output / "Forest2").exists()
        for name in ("Master", "Caves"):
            worlds = parse_lua_file(output / name / "modoverrides.lua")["workshop-3822260794"][
                "configuration_options"]["worlds"]
            assert sorted(worlds) == ["1", "2"]


def test_multi_shard_mod_templates_require_enabled_mods() -> None:
    extras = {
        "Shipwrecked": _complete_plan(SHIPWRECKED_LOCATION),
        "Volcano": _complete_plan(VOLCANO_LOCATION),
    }
    invalid = WorldCreationPlan(
        "multi_islands_invalid",
        _complete_plan(FOREST_LOCATION),
        _complete_plan(CAVE_LOCATION),
        extra_shards=extras,
    )
    _expect_value_error(
        lambda: validate_creation_plan(invalid),
        "未启用岛屿 Mod 时不应允许添加海难/火山额外世界",
    )

    with TemporaryDirectory() as directory:
        valid = WorldCreationPlan(
            "multi_islands",
            _complete_plan(SHIPWRECKED_LOCATION),
            _complete_plan(VOLCANO_LOCATION),
            mod_ids=frozenset({IA_SHIPWRECKED_MOD_ID}),
            extra_shards=extras,
        )
        output = create_world(valid, Path(directory))
        assert parse_lua_file(output / "Shipwrecked" / "leveldataoverride.lua")["location"] == SHIPWRECKED_LOCATION
        assert parse_lua_file(output / "Volcano" / "leveldataoverride.lua")["location"] == VOLCANO_LOCATION

        # 三合一整合版五分片：编号固定，海难不生成火山岛，modoverrides 不混入独立版
        layout = {name: (plan, config) for name, plan, config in three_worlds_layout()}
        # 向导里 Master/Caves 沿用模板计划，只切换世界类型
        three = WorldCreationPlan(
            "three_worlds", _complete_plan(layout["Master"][0].location),
            _complete_plan(layout["Caves"][0].location),
            mod_ids=frozenset({THREE_WORLDS_MOD_ID}),
            shard_configs={name: config for name, (_plan, config) in layout.items()},
            extra_shards={name: plan for name, (plan, _config) in layout.items()
                          if name not in (MASTER_SHARD, CAVES_SHARD)},
        )
        output = create_world(three, Path(directory))
        ids = {name: load_shard_config(output / name).shard.get("id")
               for name in ("Shipwrecked", "Porkland", "Caves", "Volcano")}
        assert ids == {"Shipwrecked": 2, "Porkland": 3, "Caves": 4, "Volcano": 5}
        shipwrecked = parse_lua_file(output / "Shipwrecked" / "leveldataoverride.lua")
        assert shipwrecked["overrides"]["volcanoisland"] == "none"
        assert parse_lua_file(output / "Porkland" / "modoverrides.lua") == {
            f"workshop-{THREE_WORLDS_MOD_ID}": {"enabled": True},
        }


if __name__ == "__main__":
    run(globals())
