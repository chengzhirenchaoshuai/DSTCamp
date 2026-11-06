"""「客户端Mod转为服务器Mod」（3486375086）兼容：开服前刷新 client_mods_list。"""

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import run  # noqa: E402

from dstools.features.mod import client_mod_bridge as bridge  # noqa: E402
from dstools.shared.lua_parser import parse_lua_file  # noqa: E402

_MODINFO = '''
name = "测试客户端Mod"
version = " 1.2.A "
client_only_mod = true
configuration_options = {
    { name = "", label = "分组", options = { { description = "", data = 0 } }, default = 0 },
    { name = "a", label = "A", options = { { description = "开", data = true }, { description = "关", data = false } }, default = true },
    { name = "b", label = "B", options = { { description = "5", data = 5 }, { description = "7", data = 7 } }, default = 5 },
}
'''

_OVERRIDES = '''return {
  ["workshop-3486375086"] = { enabled = true, configuration_options = {
    language = "auto",
    client_mods_list = {
      ["workshop-111"] = { version = "old", config = {} },
      ["workshop-999"] = { version = "9", config = { x = 1 } },
    },
  } },
  ["workshop-222"] = { enabled = true, configuration_options = { k = "v" } },
}
'''


def test_refresh_cluster_rebuilds_entries_like_game_create():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mod_dir = root / "mods" / "111"
        mod_dir.mkdir(parents=True)
        (mod_dir / "modinfo.lua").write_text(_MODINFO, encoding="utf-8")
        master = root / "Master" / "modoverrides.lua"
        caves = root / "Caves" / "modoverrides.lua"
        master.parent.mkdir()
        caves.parent.mkdir()
        master.write_text(_OVERRIDES, encoding="utf-8")
        broken = "return { -- 写了一半"
        caves.write_text(broken, encoding="utf-8")
        cluster = SimpleNamespace(shards=[SimpleNamespace(name="Master", mod_overrides_path=master),
                                          SimpleNamespace(name="Caves", mod_overrides_path=caves)])

        def lookup(key):
            return mod_dir if key == "workshop-111" else None

        # 本机保存过的值优先，没保存的取默认值（游戏里是 saved or default）
        with patch.object(bridge.config_memory, "recall", lambda key: {"b": 7} if key == "workshop-111" else None):
            assert bridge.refresh_cluster(cluster, lookup) == ["Master"]
            data = parse_lua_file(master)
            converter = data["workshop-3486375086"]["configuration_options"]
            listed = converter["client_mods_list"]
            # 版本按游戏规则去空白转小写；分组标题不进 config
            assert listed["workshop-111"] == {"version": "1.2.a", "config": {"a": True, "b": 7}}
            # 本机没装的 Mod 原样保留，其他配置与其他 Mod 不受影响
            assert listed["workshop-999"] == {"version": "9", "config": {"x": 1}}
            assert converter["language"] == "auto"
            assert data["workshop-222"]["configuration_options"] == {"k": "v"}
            # 解析失败的文件不能被改写（读失败会得到空配置，写回会清空整个文件）
            assert caves.read_text(encoding="utf-8") == broken
            # 没有变化时不重写
            assert bridge.refresh_cluster(cluster, lookup) == []


if __name__ == "__main__":
    run(globals())
