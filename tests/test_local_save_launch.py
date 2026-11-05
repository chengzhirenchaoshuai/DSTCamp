"""本地存档改由专服启动：启动参数与开服前把 shardindex 同步到专服读取的文件。"""

import tempfile
from pathlib import Path

from _harness import run

from dstools.features.cluster_config.config_manager import get_cluster_option, load_cluster_config
from dstools.features.local_service.local_save_launch import local_storage_args, sync_local_save_for_launch
from dstools.features.mod.manager import load_mod_overrides
from dstools.models import SaveSource
from dstools.shared.discovery import _build_cluster
from dstools.shared.lua_parser import parse_lua_file

# 游戏里改过设置但还没开过服：shardindex 是新的（含代理名），ini/lua 还是上次开服写的旧值
_MASTER_INDEX = """KLEI     1 return {
  enabled_mods={
    ["DontStarveLuaJIT2-Server"]={ configuration_options={ AngleBackend="auto" }, enabled=true },
    ["workshop-3597024951"]={ configuration_options={}, enabled=true }
  },
  server={ name="新名字", password="pw0", description="", max_players=4, pvp=true, privacy_type=2, game_mode="survival" },
  session_id="ABC",
  version=5,
  world={ options={ id="SURVIVAL_TOGETHER", overrides={ day="longday", wormhole_prefab="wormhole" } } }
}"""
_CAVES_INDEX = """KLEI     1 return {
  enabled_mods={},
  session_id="DEF",
  version=5,
  world={ options={ id="DST_CAVE", overrides={ cavelight="default" } } }
}"""


def _make_local_save(root: Path) -> Path:
    cluster = root / "280257116" / "Cluster_1"
    (cluster / "cluster.ini").parent.mkdir(parents=True)
    (cluster / "cluster.ini").write_text(
        "[GAMEPLAY]\nmax_players = 6\npvp = false\n\n[NETWORK]\ncluster_name = 旧名字\ncluster_password = \n"
        "lan_only_cluster = false\ncluster_cloud_id = 1234\n", encoding="utf-8")
    for shard, index, overrides in (("Master", _MASTER_INDEX, '{ day="default" }'), ("Caves", _CAVES_INDEX, "{}")):
        (cluster / shard / "save").mkdir(parents=True)
        (cluster / shard / "save" / "shardindex").write_text(index, encoding="utf-8")
        (cluster / shard / "server.ini").write_text("[NETWORK]\nserver_port = 10999\n", encoding="utf-8")
        (cluster / shard / "modoverrides.lua").write_text(
            'return { ["workshop-3597024951"]={ configuration_options={}, enabled=true } }', encoding="utf-8")
        (cluster / shard / "leveldataoverride.lua").write_text(
            f'return {{ id="X", name="保留", overrides={overrides} }}', encoding="utf-8")
    return cluster


def test_storage_args_use_owner_dir_and_steam_id():
    with tempfile.TemporaryDirectory() as tmp:
        cluster = _build_cluster(_make_local_save(Path(tmp)), SaveSource.LOCAL)
        args = local_storage_args(cluster)
        assert args[-4:] == ["-ownerdir", "280257116", "-ownernetid", "76561198240522844"], args
        # 存档不在默认位置时按存档自己的根目录拆成 -persistent_storage_root/-conf_dir
        assert args[:4] == ["-persistent_storage_root", str(Path(tmp).parent), "-conf_dir", Path(tmp).name], args


def test_sync_writes_what_the_client_would_before_start():
    with tempfile.TemporaryDirectory() as tmp:
        path = _make_local_save(Path(tmp))
        cluster = _build_cluster(path, SaveSource.LOCAL)
        changed = sync_local_save_for_launch(cluster)
        assert sorted(changed) == ["Caves/leveldataoverride.lua", "Caves/modoverrides.lua", "Master/leveldataoverride.lua",
                                   "Master/modoverrides.lua", "cluster.ini"], changed

        for shard in ("Master", "Caves"):
            mods = load_mod_overrides(path / shard / "modoverrides.lua").mods
            # 代理名换回专服认的工坊名，配置跟着走；各世界都按 Master shardindex 写
            assert sorted(mods) == ["workshop-3444078585", "workshop-3597024951"], mods
            assert mods["workshop-3444078585"].configuration_options == {"AngleBackend": "auto"}

        config = load_cluster_config(path)
        assert get_cluster_option(config, "NETWORK", "cluster_name") == "新名字"
        assert get_cluster_option(config, "NETWORK", "cluster_password") == "pw0"
        assert get_cluster_option(config, "GAMEPLAY", "max_players") == 4
        assert get_cluster_option(config, "GAMEPLAY", "pvp") is True
        assert get_cluster_option(config, "NETWORK", "lan_only_cluster") is True
        assert str(get_cluster_option(config, "NETWORK", "cluster_cloud_id")) == "1234"  # 不相关字段保留

        master = parse_lua_file(path / "Master" / "leveldataoverride.lua")
        assert master["overrides"] == {"day": "longday", "wormhole_prefab": "wormhole"}
        assert master["name"] == "保留"  # 只换 overrides，其余字段原样
        assert parse_lua_file(path / "Caves" / "leveldataoverride.lua")["overrides"] == {"cavelight": "default"}

        # 已同步过就不再改写文件
        assert sync_local_save_for_launch(_build_cluster(path, SaveSource.LOCAL)) == []


run(globals())
