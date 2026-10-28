"""读取存档 .meta 与会话目录中的世界信息（天数、季节、时段等），不解析二进制存档。"""

from pathlib import Path

from dstools.shared.lua_parser import LuaParseError, parse_lua_file, parse_lua_table
from dstools.models import PlayerCharacterSave, SaveMetadata, SaveSession, SaveSlot, SaveSource


def list_save_sessions(shard_path: Path) -> list[SaveSession]:
    """列出世界目录（如 Cluster_3/Master/）下 save/session/ 中的全部存档会话。"""
    sessions = []
    session_dir = shard_path / "save" / "session"

    if not session_dir.exists():
        return sessions

    for entry in sorted(session_dir.iterdir()):
        if not entry.is_dir():
            continue

        # 会话 ID 通常是 16 位十六进制字符串；不是目录的条目上面已经跳过了
        session = _build_session(entry)
        if session.slots:  # 只保留真正有存档数据的会话
            try:
                session.metadata = read_session_metadata(session)
            except Exception:
                pass  # 元数据是可选的，读不到不影响其余字段
            sessions.append(session)

    return sessions


def _read_meta_file(meta_path: Path) -> SaveMetadata | None:
    """解析一个 .meta 文件，不存在或解析失败返回 None。"""
    if not meta_path.exists():
        return None

    try:
        raw = parse_lua_file(meta_path)
    except Exception:
        return None

    metadata = SaveMetadata()

    clock = raw.get("clock", {})
    if isinstance(clock, dict):
        metadata.day = clock.get("cycles", 0)
        metadata.phase = clock.get("phase", "")

    seasons = raw.get("seasons", {})
    if isinstance(seasons, dict):
        metadata.season = seasons.get("season", "")
        metadata.days_in_season = seasons.get("elapseddaysinseason", 0)

    return metadata


def _build_session(session_path: Path) -> SaveSession:
    """从会话目录路径构建一个 SaveSession。"""
    session = SaveSession(
        session_id=session_path.name,
        path=session_path,
        source=SaveSource.SERVER,
    )

    # 找出存档槽文件（数字文件名，各自可能配一个同名 .meta 文件）
    for entry in sorted(session_path.iterdir()):
        if entry.is_file() and entry.name.isdigit():
            meta_file = session_path / f"{entry.name}.meta"
            slot = SaveSlot(
                slot_number=int(entry.name),
                save_file=entry,
                meta_file=meta_file if meta_file.exists() else None,
                size=entry.stat().st_size,
            )
            session.slots.append(slot)

    return session


def read_session_metadata(session: SaveSession) -> SaveMetadata | None:
    """读取会话中最新 .meta 的元数据，没有则返回 None。"""
    meta_files = [s.meta_file for s in session.slots if s.meta_file and s.meta_file.exists()]
    if not meta_files:
        return None

    return _read_meta_file(meta_files[-1])


def get_save_summary(session: SaveSession) -> str:
    """生成会话摘要，如 "第417天, 夏季第12天, 白天"。"""
    parts = []

    if session.metadata:
        meta = session.metadata
        if meta.day > 0:
            parts.append(f"第{meta.day}天")

        season_names = {
            "summer": "夏季", "winter": "冬季",
            "autumn": "秋季", "spring": "春季",
        }
        if meta.season:
            season_cn = season_names.get(meta.season, meta.season)
            parts.append(f"{season_cn}")
            if meta.days_in_season > 0:
                parts[-1] = f"{season_cn}第{int(meta.days_in_season)}天"

        phase_names = {
            "day": "白天", "dusk": "黄昏", "night": "夜晚",
        }
        if meta.phase:
            phase_cn = phase_names.get(meta.phase, meta.phase)
            parts.append(phase_cn)

    if not parts:
        parts.append("(无元数据)")

    # 附加存档槽数量
    parts.append(f"[{len(session.slots)}个存档槽]")

    return ", ".join(parts)


def _extract_lua_table_text(raw: bytes) -> str:
    """从玩家存档槽原始字节中截取 ``return {...}`` 文本。

    文件开头有二进制前缀，结尾可能残留游戏覆写时没截断的垃圾数据；从 ``return`` 起按
    花括号深度（跳过字符串）找到表结尾，之后的内容全部丢弃。不能直接找最后一个 ``}``。
    """
    idx = raw.find(b"return")
    if idx == -1:
        raise LuaParseError("player save file has no 'return' table")
    depth = 0
    in_str = None
    start_brace = None
    i = idx
    n = len(raw)
    while i < n:
        ch = raw[i]
        c = chr(ch) if ch < 128 else None
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == in_str:
                in_str = None
        else:
            if c in ('"', "'"):
                in_str = c
            elif c == "{":
                if start_brace is None:
                    start_brace = i
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0 and start_brace is not None:
                    return raw[idx:i + 1].decode("utf-8")
        i += 1
    raise LuaParseError("player save file has unbalanced braces")


def _read_player_slot_table(path: Path) -> dict:
    """解析一个玩家角色存档槽位的主文件 (不是 .meta)，返回完整的表."""
    raw = path.read_bytes()
    text = _extract_lua_table_text(raw)
    return parse_lua_table(text, str(path))


# 角色状态：(属性名, 存档里的组件名, 组件内的数值键)
_PLAYER_STATS = (
    ("health", "health", "health"),
    ("sanity", "sanity", "current"),
    ("hunger", "hunger", "hunger"),
    ("temperature", "temperature", "current"),
)


def list_session_players(session: SaveSession) -> list[PlayerCharacterSave]:
    """列出存档会话里每个玩家的最新角色状态。

    玩家子目录名是混淆编码，不是真实账号 ID；单个玩家解析失败只标记 parse_error，不影响其他玩家。
    """
    players: list[PlayerCharacterSave] = []
    if not session.path.exists():
        return players

    for entry in sorted(session.path.iterdir()):
        if not entry.is_dir():
            continue
        player = PlayerCharacterSave(player_id=entry.name)
        try:
            slot_files = sorted(
                (f for f in entry.iterdir() if f.is_file() and f.name.isdigit()),
                key=lambda f: int(f.name),
            )
            if not slot_files:
                player.parse_error = "no save slot found"
                players.append(player)
                continue
            # 跨世界传送或保存被打断时，最新槽位可能是 0 字节占位文件（真机复现），
            # 优先取最新的非空槽位；全为空才退回最新编号，交给下面按原样报错
            non_empty = [f for f in slot_files if f.stat().st_size > 0]
            latest = non_empty[-1] if non_empty else slot_files[-1]
            meta_path = entry / f"{latest.name}.meta"
            player.save_file = latest

            if meta_path.exists():
                meta_table = parse_lua_file(meta_path)
                player.character = meta_table.get("character", "")

            data = _read_player_slot_table(latest).get("data")
            if isinstance(data, dict):
                for attr, component, key in _PLAYER_STATS:
                    value = data.get(component)
                    if isinstance(value, dict):
                        setattr(player, attr, value.get(key))
        except Exception as e:
            player.parse_error = str(e)
        players.append(player)

    return players


def refresh_player_registry(shards: list) -> bool:
    """把这批世界 server_log.txt 中出现的账号合并进跨存档玩家登记簿（历史日志走解析缓存）。"""
    from dstools.features.save_browser.connection_log import sync_registry_from_shard_paths

    return sync_registry_from_shard_paths([shard.path for shard in shards])


def make_identity_resolver(shard_path: Path):
    """返回 resolve(player_id) -> (账号ID, 昵称) | None，按以下有依据的顺序认人：

    1. 本世界日志中的关联（"Resuming user" 紧跟 "User ID assigned ownership"）；
    2. 跨存档登记簿记录过的文件夹标识（同一账号的混淆值固定）；
    3. 明文文件夹名（账号 ID + 尾部 "_"，见 player_registry.account_from_plain_folder）。
    昵称取登记簿中的最新值，没有则为空字符串。
    """
    from dstools.features.save_browser.connection_log import (
        collect_player_identity_log, sync_registry_from_shard_paths,
    )
    from dstools.shared import player_registry

    sync_registry_from_shard_paths([shard_path])
    identity_log = collect_player_identity_log(shard_path)
    registry = player_registry.load()
    index = player_registry.folder_index(registry)

    def resolve(player_id: str) -> tuple[str, str] | None:
        linked = identity_log.get(player_id)
        account_id = linked[0] if linked else index.get(player_id)
        if not account_id:
            account_id = player_registry.account_from_plain_folder(player_id)
        if not account_id:
            return None
        nickname = registry.get(account_id, {}).get("nickname") or (linked[1] if linked else "")
        return account_id, nickname

    return resolve


def list_known_player_ids(shards: list) -> list[tuple[str, str, bool]]:
    """列出可加入管理员/黑名单的真实账号：登记簿中的全部账号，加上这批世界存档能认出的账号。

    认不出的混淆文件夹跳过，不拿混淆值冒充账号 ID；明文文件夹名的尾部 "_" 需去掉。

    Returns:
        按账号排序的 (账号ID, 辨识提示, 是否属于这批世界)；提示优先用昵称，其次角色名。
    """
    from dstools.features.save_browser.character_names import get_character_display_name
    from dstools.features.save_browser.connection_log import collect_shard_accounts
    from dstools.shared import player_registry

    refresh_player_registry(shards)
    registry = player_registry.load()
    seen_ids: set[str] = set(registry)
    nicknames = {a: info["nickname"] for a, info in registry.items() if info.get("nickname")}

    # 遍历存档只为认出文件夹对应的账号、标记"当前存档的人"，并给无昵称账号补角色名提示
    in_current: set[str] = set()
    character_hints: dict[str, str] = {}
    for shard in shards:
        in_current.update(collect_shard_accounts(shard.path))
        resolve = make_identity_resolver(shard.path)
        for session in list_save_sessions(shard.path):
            for player in list_session_players(session):
                identity = resolve(player.player_id)
                if not identity:
                    continue
                account_id = identity[0]
                seen_ids.add(account_id)
                in_current.add(account_id)
                if account_id not in nicknames and player.character and account_id not in character_hints:
                    character_hints[account_id] = get_character_display_name(player.character)
    return sorted(
        (pid, nicknames.get(pid) or character_hints.get(pid, ""), pid in in_current)
        for pid in seen_ids
    )


def known_nicknames(shards: list) -> dict[str, str]:
    """账号ID -> 已确认昵称（仅来自 "Client authenticated" 记录，不用角色名凑数）。"""
    from dstools.shared import player_registry

    refresh_player_registry(shards)
    return {a: info["nickname"] for a, info in player_registry.load().items() if info.get("nickname")}
