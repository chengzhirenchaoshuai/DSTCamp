"""游戏自定义世界预设（client_save/world_presets）的拆分、基础预设与套用规则。"""

from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dstools.features.world import builtin_presets, page_data, user_presets  # noqa: E402
from dstools.features.world.reader import WorldOverride, WorldPreset  # noqa: E402
from tests._harness import run  # noqa: E402


def test_save_splits_by_category_and_custom_base_resolves():
    with TemporaryDirectory() as tmp:
        user_dir = Path(tmp)
        # day 是世界规则，world_size 是世界生成，has_ocean 不是游戏的设置选项
        settings, worldgen, unknown = user_presets.split_overrides(
            {"day": "longday", "world_size": "small", "has_ocean": True}, "forest")
        assert (settings, worldgen, unknown) == ({"day": "longday"}, {"world_size": "small"}, ["has_ocean"])
        user_presets.save_preset(user_dir, "Mine", "", "ENDLESS", "ENDLESS", settings, worldgen)
        folder = user_presets.presets_dir(user_dir)
        assert sorted(p.name for p in folder.iterdir()) == ["CUSTOM_MINE.wgp", "CUSTOM_MINE.wsp"]
        # 缺 baseid 的文件游戏当无效，这里同样忽略
        (folder / "CUSTOM_BROKEN.wsp").write_bytes(b'KLEI     1 return {name="x", desc="", overrides={}}')
        presets, errors = user_presets.list_presets(user_dir)
        assert errors == [] and [p.preset_id for p in presets] == ["CUSTOM_MINE"]
        assert presets[0].location == "forest" and presets[0].settings == {"day": "longday"}
        # 由自定义预设生成的世界取那个预设的 baseid；找不到时退回该世界类型的默认预设
        raw = {"settings_id": "CUSTOM_MINE", "worldgen_id": "CUSTOM_GONE"}
        assert user_presets.resolve_base_ids(raw, "forest", user_dir) == ("ENDLESS", "SURVIVAL_TOGETHER")
        # 游戏把名称的 UTF-8 字节逐个当 Latin-1 字符拼文件名：游戏实测存「测试」得到这个文件名
        game_stem = "CUSTOM_" + bytes.fromhex("E6B58BE8AF95").decode("latin-1")
        user_presets.save_preset(user_dir, "测试", "", "ENDLESS", "ENDLESS", {}, {})
        assert (folder / (game_stem + ".wsp")).is_file()
        assert "CUSTOM_测试" in [p.preset_id for p in user_presets.list_presets(user_dir)[0]]
        try:
            user_presets.save_preset(user_dir, "a/b", "", "ENDLESS", "ENDLESS", {}, {})
        except ValueError:
            pass
        else:
            raise AssertionError("含文件名非法字符的名称应被拒绝")


def test_apply_skips_unknown_and_retyping_values():
    preset = WorldPreset(location="forest", overrides=[WorldOverride("day", "default"), WorldOverride("autumn", "default")])
    data = page_data.WorldPageData(page_data.STATUS_OK, is_server=True, shard_name="Master",
                                   preset=preset, location="forest", is_master_world=True)
    changed, skipped = page_data.apply_preset_values(
        data, {"day": "longday", "autumn": "default", "world_size": "small", "not_a_setting": "x", "wildfires": True})
    assert changed == ["day", "world_size"]
    # 非字符串值写成字符串会改变 Lua 类型，跳过
    assert skipped == ["not_a_setting", "wildfires"]
    assert {o.key: o.value for o in preset.overrides}["world_size"] == "small"
    assert any(row.key == "world_size" and row.value == "small"
               for rows in data.generation_by_category.values() for row in rows)


def test_builtin_settings_preset_resets_other_settings():
    # 轻松风格的主世界套用「无尽」：预设没写的原版设置回默认，世界生成项不动
    relaxed = next(p for p in builtin_presets.BUILTIN_SETTINGS_PRESETS if p.preset_id == "RELAXED")
    endless = next(p for p in builtin_presets.BUILTIN_SETTINGS_PRESETS if p.preset_id == "ENDLESS")
    overrides = [WorldOverride(k, v) for k, v in relaxed.overrides.items()] + [WorldOverride("world_size", "small")]
    preset = WorldPreset(location="forest", overrides=overrides)
    data = page_data.WorldPageData(page_data.STATUS_OK, is_server=True, shard_name="Master",
                                   preset=preset, location="forest", is_master_world=True)
    page_data.apply_builtin_settings_preset(data, endless)
    values = {o.key: o.value for o in preset.overrides}
    assert values["healthpenalty"] == "always" and values["hunger"] == "default"
    assert values["portalresurection"] == "always" and values["basicresource_regrowth"] == "always"
    assert values["world_size"] == "small"


def test_builtin_settings_preset_saves_preset_name_for_local_save():
    # 本地存档：settings_* 与 world.options 里已有的 playstyle 跟着改，world.options 以外原样保留
    from dstools.features.world.local_options import load_local_overrides
    from dstools.features.world.reader import load_leveldata
    from dstools.shared.lua_parser import parse_lua_table

    survival = next(p for p in builtin_presets.BUILTIN_SETTINGS_PRESETS if p.preset_id == "SURVIVAL_TOGETHER")
    with TemporaryDirectory() as tmp:
        shard_dir = Path(tmp) / "Master"
        (shard_dir / "save").mkdir(parents=True)
        index_file = shard_dir / "save" / "shardindex"
        index_file.write_bytes(
            b'KLEI     1 return {enabled_mods={["workshop-1"]={enabled=true}}, server={playstyle="relaxed"}, '
            b'world={options={id="RELAXED", playstyle="relaxed", settings_id="RELAXED", '
            b'overrides={hunger="nonlethal", world_size="small"}}}}')
        leveldata = shard_dir / "leveldataoverride.lua"
        leveldata.write_text('return {id="RELAXED", name="x", desc="", location="forest", settings_id="RELAXED", '
                             'overrides={hunger="nonlethal", world_size="small"}}', encoding="utf-8")
        preset = load_leveldata(leveldata).preset
        preset.overrides = [WorldOverride(k, v) for k, v in load_local_overrides(shard_dir).items()]
        data = page_data.WorldPageData(page_data.STATUS_OK, is_server=False, shard_name="Master", path=leveldata,
                                       preset=preset, location="forest", is_master_world=True)
        page_data.apply_builtin_settings_preset(data, survival)
        page_data.save_world_page(data)

        saved = parse_lua_table(index_file.read_bytes()[11:].decode("utf-8"))
        options = saved["world"]["options"]
        assert options["settings_id"] == "SURVIVAL_TOGETHER" and options["playstyle"] == "survival"
        assert options["id"] == "RELAXED" and options["overrides"] == {"hunger": "default", "world_size": "small"}
        assert saved["enabled_mods"] == {"workshop-1": {"enabled": True}} and saved["server"] == {"playstyle": "relaxed"}
        raw = load_leveldata(leveldata).preset.raw
        assert raw["settings_id"] == "SURVIVAL_TOGETHER" and raw["overrides"]["hunger"] == "default"
        # leveldataoverride 原本没有 playstyle，不新增
        assert "playstyle" not in raw


def test_user_preset_then_builtin_preset_names():
    # 与游戏一致：{类别}_id 写基础预设、custom_{类别}_* 写自定义预设；之后改选官方选项预设时删掉 custom_settings_*
    from dstools.features.world.reader import load_leveldata

    custom = user_presets.UserPreset("CUSTOM_MINE", "Mine", "d", "ENDLESS", "ENDLESS",
                                     {"day": "longday"}, {"world_size": "small"})
    relaxed = next(p for p in builtin_presets.BUILTIN_SETTINGS_PRESETS if p.preset_id == "RELAXED")
    with TemporaryDirectory() as tmp:
        leveldata = Path(tmp) / "leveldataoverride.lua"
        leveldata.write_text('return {id="SURVIVAL_TOGETHER", name="x", desc="", location="forest", '
                             'settings_id="SURVIVAL_TOGETHER", worldgen_id="SURVIVAL_TOGETHER", '
                             'overrides={day="longday", world_size="small"}}', encoding="utf-8")

        def apply_and_save(apply, preset):
            data = page_data.WorldPageData(page_data.STATUS_OK, is_server=True, shard_name="Master", path=leveldata,
                                           preset=load_leveldata(leveldata).preset, location="forest",
                                           is_master_world=True)
            changed, _ = apply(data, preset)
            pending = page_data.preset_fields_pending(data)
            page_data.save_world_page(data)
            return changed, pending, load_leveldata(leveldata).preset.raw

        # 取值已一致也要能保存，否则预设名改不过来
        changed, pending, raw = apply_and_save(page_data.apply_user_preset, custom)
        assert changed == [] and pending
        assert raw["settings_id"] == "ENDLESS" and raw["worldgen_id"] == "ENDLESS"
        assert raw["custom_settings_id"] == "CUSTOM_MINE" and raw["custom_worldgen_name"] == "Mine"
        _, _, raw = apply_and_save(page_data.apply_builtin_settings_preset, relaxed)
        assert raw["settings_id"] == "RELAXED" and "custom_settings_id" not in raw
        assert raw["worldgen_id"] == "ENDLESS" and raw["custom_worldgen_id"] == "CUSTOM_MINE"


if __name__ == "__main__":
    run(globals())
