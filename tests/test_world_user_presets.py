"""游戏自定义世界预设（client_save/world_presets）的拆分、基础预设与套用规则。"""

from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dstools.features.world import page_data, user_presets  # noqa: E402
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


if __name__ == "__main__":
    run(globals())
