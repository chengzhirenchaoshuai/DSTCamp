"""DST 存档管理工具的数据模型。"""

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class SaveSource(Enum):
    """存档来源：SERVER 为根目录下的专服存档（如 Cluster_3），LOCAL 为用户 ID 目录下的本地存档。"""
    SERVER = "server"
    LOCAL = "local"


class Platform(Enum):
    """发行平台：Steam 与 WeGame 的根目录和专服目录不同，配置文件格式一致。"""
    STEAM = "steam"
    WEGAME = "wegame"


@dataclass
class SaveMetadata:
    """存档元数据 (从 .meta 文件解析)."""

    day: int = 0
    season: str = ""
    days_in_season: float = 0.0
    days_left_in_season: float = 0.0
    phase: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class SaveSlot:
    """存档槽位."""

    slot_number: int
    save_file: Path
    meta_file: Path
    size: int = 0


@dataclass
class PlayerCharacterSave:
    """玩家在某个存档会话里的角色状态（解析其子文件夹最新槽位）。

    子文件夹名默认经 encode_user_path 混淆编码，不是 Klei 账号 ID 且无法还原，只能原样展示。
    """

    player_id: str
    character: str = ""
    slot_number: int | None = None
    save_file: Path | None = None
    meta_file: Path | None = None
    size: int = 0
    x: float | None = None
    z: float | None = None
    health: float | None = None
    sanity: float | None = None
    sanity_sane: bool | None = None
    hunger: float | None = None
    temperature: float | None = None
    moisture: float | None = None
    age: float | None = None
    skin_name: str = ""
    clothing: dict = field(default_factory=dict)
    recipes_count: int | None = None
    raw: dict = field(default_factory=dict)
    parse_error: str = ""


@dataclass
class SaveSession:
    """一个存档会话."""

    session_id: str
    path: Path
    slots: list[SaveSlot] = field(default_factory=list)
    metadata: SaveMetadata | None = None
    source: SaveSource = SaveSource.SERVER
    cluster_name: str = ""
    shard_name: str = ""
    players: list[PlayerCharacterSave] = field(default_factory=list)


@dataclass
class ModEntry:
    """单个 Mod 条目."""

    workshop_id: str
    enabled: bool = True
    configuration_options: dict = field(default_factory=dict)
    name: str = ""
    description: str = ""


@dataclass
class ModOverrides:
    """modoverrides.lua 的完整表示."""

    path: Path
    mods: dict[str, ModEntry] = field(default_factory=dict)
    # client_mods_disabled 键：不是 Mod，开启后加入的玩家本地客户端 Mod 被临时禁用
    client_mods_disabled: bool = False


@dataclass
class ClusterConfig:
    """cluster.ini 内容."""

    gameplay: dict = field(default_factory=dict)
    network: dict = field(default_factory=dict)
    misc: dict = field(default_factory=dict)
    shard: dict = field(default_factory=dict)
    steam: dict = field(default_factory=dict)


@dataclass
class ShardConfig:
    """server.ini 内容."""

    network: dict = field(default_factory=dict)
    shard: dict = field(default_factory=dict)
    account: dict = field(default_factory=dict)
    steam: dict = field(default_factory=dict)


@dataclass
class Shard:
    """Cluster 下的一个世界 (Master/Caves/...)."""

    name: str
    path: Path
    config: ShardConfig | None = None
    saves: list[SaveSession] = field(default_factory=list)
    mod_overrides_path: Path | None = None
    leveldata_path: Path | None = None


@dataclass
class Cluster:
    """一个服务器 Cluster 或本地 Cluster."""

    name: str
    path: Path
    source: SaveSource = SaveSource.SERVER   # 服务器存档 或 本地存档
    platform: Platform = Platform.STEAM      # 所属发行平台（Steam / WeGame）
    config: ClusterConfig | None = None
    shards: list[Shard] = field(default_factory=list)
    mod_overrides_path: Path | None = None
    adminlist_path: Path | None = None        # adminlist.txt
    token_path: Path | None = None            # cluster_token.txt
    blocklist_path: Path | None = None        # blocklist.txt (黑名单)
    account_id: str = ""                      # 本地存档所属的游戏账号 ID（服务器存档为空）


@dataclass
class Account:
    """Klei 根目录下的一个游戏账号目录（目录名为 Steam AccountID / WeGame 用户 ID）。"""

    id: str
    platform: Platform
    path: Path
    name: str = ""                            # Steam 昵称（loginusers.vdf），读不到为空
    steam_active: bool = False                # 是 Steam 客户端当前登录的账号


@dataclass
class DSTEnvironment:
    """DST 环境信息：klei_root 固定是 Steam 版根目录，WeGame 版另记在 wegame_klei_root，
    两边的存档一起放在 clusters 中按 platform 区分。
    同一平台可能有多个游戏账号（accounts），user_id/wegame_user_id 是各平台的当前账号。

    klei_root 是 Steam 版服务器存档的根目录（可手动指定）；游戏客户端只认默认位置，
    账号目录、本地存档、client.ini 都在 client_klei_root 下，未手动指定时两者相同。"""

    klei_root: Path | None = None
    wegame_klei_root: Path | None = None
    client_klei_root: Path | None = None
    user_id: str = ""
    wegame_user_id: str = ""
    clusters: list[Cluster] = field(default_factory=list)
    client_config: Path | None = None
    accounts: list[Account] = field(default_factory=list)
    steam_active_account: str = ""            # Steam 客户端当前登录的账号 ID，Steam 未运行时为空
    steam_names: dict[str, str] = field(default_factory=dict)  # 本机登录过的 Steam 账号 ID -> 昵称

    def accounts_for(self, platform: Platform) -> list[Account]:
        return [a for a in self.accounts if a.platform == platform]

    def current_account(self, platform: Platform) -> Account | None:
        current = self.wegame_user_id if platform == Platform.WEGAME else self.user_id
        return next((a for a in self.accounts_for(platform) if a.id == current), None)

    def klei_root_for(self, platform: Platform) -> Path | None:
        """按平台取根目录；在根目录下新建/复制存档时必须用它，不能直接用 klei_root。"""
        return self.wegame_klei_root if platform == Platform.WEGAME else self.klei_root
