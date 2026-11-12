"""存档信息页的展示数据（只取数整理，不含界面代码）。"""

from dataclasses import dataclass, field
from pathlib import Path

from dstools.features.cluster_config.config_manager import load_cluster_config
from dstools.features.cluster_config.ini_field_info import get_enum_choices
from dstools.features.mod.manager import list_mods, load_mod_overrides
from dstools.features.mod.parser import resolve_wegame_client_mods_dir
from dstools.features.save_browser.character_icons import resolve_character
from dstools.features.save_browser.connection_log import collect_player_connection_log
from dstools.features.save_browser.reader import (
    get_save_summary, list_save_sessions, list_session_players, make_identity_resolver,
)
from dstools.models import Cluster, SaveSource
from dstools.shared.app_settings import get_player_note
from dstools.shared.resource_paths import bundled_resource_dir
from dstools.shared.shard_roles import master_shard_name

# 角色名/头像都查不到时的兜底头像（问号图标），是打包进 icons/ui/ 的固定素材。
DEFAULT_AVATAR_PATH = bundled_resource_dir() / "icons" / "ui" / "character_icon_default.png"


@dataclass
class ShardBrief:
    name: str
    mod_count: int
    session_count: int


@dataclass
class ClusterOverview:
    path: Path
    is_server: bool
    game_mode: str
    max_players: str
    cluster_name: str
    shards: list[ShardBrief] = field(default_factory=list)


@dataclass
class PlayerView:
    player_id: str
    parse_error: bool
    character_name: str
    icon_path: Path | None
    save_file: Path | None
    health: object
    sanity: object
    hunger: object
    temperature: object
    note: str
    connection_times: list[str] = field(default_factory=list)
    identity: tuple[str, str] | None = None  # (科雷账号 ID, 昵称)，查不到为 None


@dataclass
class SessionView:
    session_id: str
    summary: str
    slot_count: int
    size_mb: float
    extra_sessions: int
    players: list[PlayerView] = field(default_factory=list)


def shard_names(cluster: Cluster | None) -> list[str]:
    return [s.name for s in cluster.shards] if cluster else []


def default_shard_name(cluster: Cluster, previous: str | None = None) -> str:
    """保持上次选的世界；没有则优先主世界（按 is_master 判定），再退回第一个。"""
    names = shard_names(cluster)
    if previous in names:
        return previous
    return master_shard_name(cluster) or (names[0] if names else "")


def load_cluster_overview(cluster: Cluster) -> ClusterOverview:
    config = load_cluster_config(cluster.path)
    raw_mode = config.gameplay.get("game_mode", "?")
    # 跟"服务器配置"页签的游戏模式下拉框用同一张翻译表；查不到（比如 mod 塞了游戏不认识的
    # 自定义模式）就照原样显示原始值，不瞎猜。
    choices = get_enum_choices("GAMEPLAY", "game_mode") or []
    game_mode = next((display for raw, display in choices if raw == raw_mode), raw_mode)
    shards = []
    for shard in cluster.shards:
        mod_count = 0
        if shard.mod_overrides_path:
            mod_count = len(list_mods(load_mod_overrides(shard.mod_overrides_path)))
        shards.append(ShardBrief(shard.name, mod_count, len(list_save_sessions(shard.path))))
    return ClusterOverview(
        path=cluster.path,
        is_server=cluster.source == SaveSource.SERVER,
        game_mode=str(game_mode),
        max_players=str(config.gameplay.get("max_players", "?")),
        cluster_name=str(config.network.get("cluster_name", "?")),
        shards=shards,
    )


def load_session_view(cluster: Cluster, shard_name: str) -> SessionView | None:
    """当前世界的会话信息和玩家列表；没有任何存档会话时返回 None。"""
    shard = next((s for s in cluster.shards if s.name == shard_name), None)
    if shard is None:
        return None
    sessions = list_save_sessions(shard.path)
    if not sessions:
        return None
    for session in sessions:
        session.cluster_name = cluster.name
        session.shard_name = shard.name
        session.source = cluster.source
    # 一个世界正常只有一个会话（只有"生成新世界"才会开新的）；真遇到多个只展示第一个，
    # 另用 extra_sessions 提示还有几个。
    session = sessions[0]
    session.players = list_session_players(session)
    platform = cluster.platform
    wegame_mods = resolve_wegame_client_mods_dir(platform)
    # 日志解析对这批玩家共用一份；账号标识靠跨存档登记簿认，新存档也能认出老玩家。
    connection_log = collect_player_connection_log(shard.path)
    resolve_identity = make_identity_resolver(shard.path)

    players = []
    for player in session.players:
        name, icon = "?", None
        if not player.parse_error and player.character:
            name, icon = resolve_character(
                player.character, shard.mod_overrides_path, platform, wegame_mods)
        if not icon and DEFAULT_AVATAR_PATH.exists():
            icon = DEFAULT_AVATAR_PATH
        players.append(PlayerView(
            player_id=player.player_id,
            parse_error=bool(player.parse_error),
            character_name=name,
            icon_path=Path(icon) if icon else None,
            save_file=player.save_file,
            health=player.health, sanity=player.sanity,
            hunger=player.hunger, temperature=player.temperature,
            note=get_player_note(player.player_id),
            connection_times=list(connection_log.get((session.session_id, player.player_id), [])),
            identity=resolve_identity(player.player_id),
        ))
    return SessionView(
        session_id=session.session_id,
        summary=get_save_summary(session),
        slot_count=len(session.slots),
        size_mb=sum(slot.size for slot in session.slots) / (1024 * 1024),
        extra_sessions=len(sessions) - 1,
        players=players,
    )
