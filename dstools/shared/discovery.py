"""自动发现 DST 数据目录、cluster、世界（shard）和存档。"""

from pathlib import Path

from dstools.features.local_service.dedicated_server import get_documents_dir
from dstools.models import (
    Account,
    Cluster,
    DSTEnvironment,
    Platform,
    SaveSource,
    Shard,
)
from dstools.shared.app_settings import get_klei_root_override, get_selected_account
from dstools.shared.steam_discovery import read_active_steam_account, read_steam_login_users

# Klei 根目录名：Steam 版 DoNotStarveTogether，WeGame 版 DoNotStarveTogetherRail，两棵独立目录树可并存
_STEAM_KLEI_FOLDER = "DoNotStarveTogether"
_WEGAME_KLEI_FOLDER = "DoNotStarveTogetherRail"

# 兜底候选，仅在注册表读取"文档"路径失败时使用。坑：被重定向的"文档"目录名可以是任意字符串，
# 穷举猜不全，必须优先读注册表
_EXTRA_SEARCH_DRIVE_SUBPATHS = [
    "系统目录/文档/Klei",
    "文档/Klei",
    "Documents/Klei",
]


def _find_klei_root_impl(folder_name: str) -> Path | None:
    p = get_documents_dir() / "Klei" / folder_name
    if p.exists():
        return p
    for drive_letter in ["D:", "E:", "F:", "G:"]:
        for subpath in _EXTRA_SEARCH_DRIVE_SUBPATHS:
            p = Path(drive_letter) / subpath / folder_name
            if p.exists():
                return p
    return None


def _is_cluster_dir(path: Path) -> bool:
    """判断是不是一个 DST cluster 目录（含 cluster.ini）。"""
    return path.is_dir() and (path / "cluster.ini").exists()


def _is_shard_dir(path: Path) -> bool:
    """判断是不是一个 DST 世界（shard）目录（含 server.ini）。"""
    return path.is_dir() and (path / "server.ini").exists()


# 游戏启动后才会在账号目录里写的文件；DSTCamp 自己只写 client_save/mod_config_data，不能当证据
_ACCOUNT_MARKERS = ("client.ini", "client_save/cached_userid", "client_save/boot_modindex")


def _is_user_dir(path: Path, known_ids=()) -> bool:
    """判断是不是 Steam/Rail 的账号目录：纯数字命名，且有游戏写入的痕迹或是本机 Steam 登录过的账号。
    纯数字的 Mod 文件夹（有 modinfo.lua）等误放进来的目录不算。"""
    if not (path.is_dir() and path.name.isdigit()) or (path / "modinfo.lua").exists():
        return False
    return path.name in known_ids or any((path / marker).exists() for marker in _ACCOUNT_MARKERS)


def find_klei_root() -> Path | None:
    """Steam 版存档根目录：手动指定的优先（即使暂时不存在也如实使用，不悄悄换回自动找到的），否则自动查找。"""
    return get_klei_root_override() or auto_klei_root()


def auto_klei_root() -> Path | None:
    """自动发现 Steam 版 Klei DoNotStarveTogether 根目录。"""
    return _find_klei_root_impl(_STEAM_KLEI_FOLDER)


def find_wegame_klei_root() -> Path | None:
    """自动发现 WeGame 版 DoNotStarveTogetherRail 根目录（与 Steam 版并列，内部结构完全一致）。"""
    return _find_klei_root_impl(_WEGAME_KLEI_FOLDER)


def list_user_dirs(klei_root: Path, known_ids=()) -> list[Path]:
    """列出 Klei 根目录下全部账号目录（同一台电脑登录过多个账号时会有多个）；known_ids 为本机 Steam 登录过的账号。"""
    if not klei_root.exists():
        return []
    return sorted((entry for entry in klei_root.iterdir() if _is_user_dir(entry, known_ids)), key=lambda p: p.name)


def _last_played(user_dir: Path) -> float:
    """账号最近一次被游戏使用的时间：client_save 下各文件/目录的最新修改时间。"""
    try:
        entries = list((user_dir / "client_save").iterdir())
    except OSError:
        return 0.0
    times = []
    for entry in entries:
        try:
            times.append(entry.stat().st_mtime)
        except OSError:
            continue
    return max(times, default=0.0)


def pick_current_account(account_ids: list[str], *, selected: str = "", selected_active: str = "",
                         active: str = "", recent: list[str] = (),
                         last_played: dict[str, float] | None = None) -> str:
    """按优先级选当前账号：用户手动选择 > Steam 当前登录 > Steam 最近登录顺序 > 最近游玩 > 第一个。
    手动选择只在 Steam 仍登录着选择时的账号（或 Steam 未运行）时有效，Steam 换了账号就跟随 Steam。
    每一级都只认 account_ids 中的账号；没有账号返回空串。"""
    if not account_ids:
        return ""
    if active and selected_active != active:
        selected = ""
    for candidate in (selected, active, *recent):
        if candidate and candidate in account_ids:
            return candidate
    played = last_played or {}
    if any(played.get(a, 0) > 0 for a in account_ids):
        return max(account_ids, key=lambda a: played.get(a, 0))
    return account_ids[0]


def list_clusters(klei_root: Path) -> list[Path]:
    """列出根目录下的有效存档；只有 cluster.ini 没有分片的半成品（创建被中断）不算，也不能让发现失败。"""
    clusters = []
    if not klei_root.exists():
        return clusters
    for entry in sorted(klei_root.iterdir()):
        if _is_cluster_dir(entry) and list_shards(entry):
            clusters.append(entry)
    return clusters


def list_shards(cluster_path: Path) -> list[Path]:
    """列出一个 cluster 下的全部世界（shard）目录。"""
    shards = []
    if not cluster_path.exists():
        return shards
    for entry in sorted(cluster_path.iterdir()):
        if _is_shard_dir(entry):
            shards.append(entry)
    return shards


# ── 环境发现 ────────────────────────────────────────────────────────────

def discover_environment(klei_root: Path | None = None,
                          wegame_klei_root: Path | None = None) -> DSTEnvironment:
    """扫描 Steam 与 WeGame 两棵目录树：根目录下的存档为 SERVER，用户 ID 目录下的为 LOCAL，
    合并到同一列表，用 Cluster.platform 区分。

    Steam 版手动指定了存档根目录时，服务器存档从指定目录扫描；游戏客户端只认默认位置，
    账号与本地存档仍从自动找到的目录扫描（见 DSTEnvironment.client_klei_root）。
    """
    override = get_klei_root_override()
    if klei_root is None:
        klei_root = override or auto_klei_root()
    client_root = auto_klei_root() if override is not None and klei_root == override else klei_root
    if wegame_klei_root is None:
        wegame_klei_root = find_wegame_klei_root()

    env = DSTEnvironment(klei_root=klei_root, wegame_klei_root=wegame_klei_root, client_klei_root=client_root)

    if any(root is not None and root.exists() for root in (klei_root, client_root)):
        _scan_platform_root(env, klei_root, Platform.STEAM, user_root=client_root)
    if wegame_klei_root is not None and wegame_klei_root.exists():
        _scan_platform_root(env, wegame_klei_root, Platform.WEGAME, user_root=wegame_klei_root)

    return env


def _scan_platform_root(env: DSTEnvironment, root: Path | None, platform: Platform,
                        user_root: Path | None) -> None:
    """扫描一个平台的 Klei 根目录，把发现的 Cluster 追加进 env.clusters。
    root 下是服务器存档，user_root 下是账号目录与本地存档；任一为 None 或不存在时只扫另一部分。"""
    if platform == Platform.STEAM:
        login_users = read_steam_login_users()
        names = {u.account_id: u.persona_name for u in login_users}
        # 登录记录按 MostRecent、登录时间倒序，作为没有当前登录账号时的依据
        recent = [u.account_id for u in sorted(login_users, key=lambda u: (u.most_recent, u.timestamp), reverse=True)]
        active = read_active_steam_account()
    else:
        names, recent, active = {}, [], ""
    user_dirs = list_user_dirs(user_root, known_ids=names) if user_root is not None else []
    account_base = user_root if user_root is not None else root
    ids = [d.name for d in user_dirs]
    # 只有一个账号时不必读修改时间
    played = {d.name: _last_played(d) for d in user_dirs} if len(user_dirs) > 1 else None
    # Steam 当前登录的账号即使还没在本机运行过饥荒（没有账号目录）也列出来，工具跟随它
    if active and active not in ids:
        ids.append(active)
    selected, selected_active = get_selected_account(platform.value)
    current = pick_current_account(ids, selected=selected, selected_active=selected_active, active=active,
                                   recent=recent, last_played=played)
    for account_id in ids:
        path = account_base / account_id
        env.accounts.append(Account(account_id, platform, path, names.get(account_id, ""),
                                    steam_active=account_id == active))

    if platform == Platform.STEAM:
        env.user_id = current
        env.steam_active_account = active
        env.steam_names = names
    else:
        # WeGame 版用户 ID 单独存一份（状态栏按存档类型切换显示）
        env.wegame_user_id = current

    # 根目录下的 cluster → SERVER
    for cluster_path in (list_clusters(root) if root is not None else []):
        cluster = _build_cluster(cluster_path, SaveSource.SERVER, platform)
        env.clusters.append(cluster)

    # 各账号目录下的存档 → LOCAL，当前账号排在前面。不与 SERVER 按名字去重：两棵目录树中的同名存档是不同的存档
    for user_dir in sorted(user_dirs, key=lambda d: d.name != current):
        for cluster_path in list_clusters(user_dir):
            cluster = _build_cluster(cluster_path, SaveSource.LOCAL, platform)
            cluster.account_id = user_dir.name
            env.clusters.append(cluster)


def _build_cluster(cluster_path: Path, source: SaveSource,
                    platform: Platform = Platform.STEAM) -> Cluster:
    """从一个 cluster 目录构造 Cluster 对象。"""
    cluster = Cluster(name=cluster_path.name, path=cluster_path, source=source, platform=platform)

    # modoverrides.lua
    mod_path = cluster_path / "modoverrides.lua"
    if mod_path.exists():
        cluster.mod_overrides_path = mod_path

    # adminlist.txt（通常只有 SERVER cluster 才有）
    admin_path = cluster_path / "adminlist.txt"
    if admin_path.exists():
        cluster.adminlist_path = admin_path

    # blocklist.txt（黑名单，通常只有 SERVER cluster 才有）——跟 adminlist.txt
    # 一样是每行一个 Klei ID 的格式，只是语义是拉黑而不是授权。
    block_path = cluster_path / "blocklist.txt"
    if block_path.exists():
        cluster.blocklist_path = block_path

    # cluster_token.txt（通常只有 SERVER cluster 才有）
    token_path = cluster_path / "cluster_token.txt"
    if token_path.exists():
        cluster.token_path = token_path

    # 世界（shard）
    for shard_path in list_shards(cluster_path):
        shard = _build_shard(shard_path)
        cluster.shards.append(shard)

    return cluster


def _build_shard(shard_path: Path) -> Shard:
    """从一个世界（shard）目录构造 Shard 对象。"""
    shard = Shard(name=shard_path.name, path=shard_path)

    mod_path = shard_path / "modoverrides.lua"
    if mod_path.exists():
        shard.mod_overrides_path = mod_path

    leveldata_path = shard_path / "leveldataoverride.lua"
    if leveldata_path.exists():
        shard.leveldata_path = leveldata_path

    return shard
