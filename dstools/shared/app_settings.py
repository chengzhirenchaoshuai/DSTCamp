"""读写 DSTCamp 本地偏好；不处理游戏的 INI/Lua 配置。"""

import json
import os
import time
from pathlib import Path

_SETTINGS_FILE = "settings.json"
_REPLACE_ATTEMPTS = 5
_KEY_DEDICATED_SERVER_PATH = "dedicated_server_path"
_KEY_STEAMCMD_SERVER_PATH = "steamcmd_server_path"
_KEY_CLIENT_RUNTIME_PATH = "client_runtime_path"
_KEY_SERVER_RUNTIME_MODE = "server_runtime_mode"
_KEY_WEGAME_ROOT_PATH = "wegame_root_path"
_KEY_KLEI_ROOT_PATH = "klei_root_path"
_KEY_STEAM_MODS_PATH = "steam_mods_path"
_KEY_STEAMCMD_PATH = "steamcmd_path"
_KEY_STEAM_BACKEND = "steam_backend"
_KEY_STEAMCMD_ACCOUNT = "steamcmd_account"
_KEY_USAGE_SCENARIO_MIGRATED = "usage_scenario_migrated"
_KEY_THEME_NAME = "theme_name"
_DEFAULT_THEME_NAME = "gray"
_KEY_FONT_STYLE_CHOICE = "font_style_choice"
_DEFAULT_FONT_STYLE_CHOICE = "default"
_KEY_FONT_SIZE_LEVEL = "font_size_level"
_DEFAULT_FONT_SIZE_LEVEL = "normal"
_KEY_PLAYER_NOTES = "player_notes"
_KEY_MINIMIZE_ON_CLOSE = "minimize_on_close"
_KEY_REMIND_UPDATE_ENABLED = "remind_update_enabled"
_KEY_CACHE_USE_EXE_DIR = "cache_use_exe_dir"
_KEY_CACHE_DIR = "cache_dir"
_KEY_CUSTOM_BG_FILENAME = "custom_bg_filename"
_KEY_CUSTOM_BG_OPACITY = "custom_bg_opacity"
_KEY_WINDOW_ORNAMENTS = "window_ornaments_enabled"
_DEFAULT_CUSTOM_BG_OPACITY = 0.35
_KEY_WINDOW_POS = "window_pos"
_KEY_WINDOW_SIZE = "window_size"
_KEY_CREATION_WIZARD_SIZE = "creation_wizard_size"
_KEY_BACKUP_RETENTION = "backup_retention"
_DEFAULT_BACKUP_RETENTION = 20
_KEY_BACKUP_INTERVAL_MIN = "backup_interval_minutes"
_DEFAULT_BACKUP_INTERVAL_MIN = 10
_KEY_BACKUP_AUTO_ENABLED = "backup_auto_enabled"
_KEY_SAKURA_TOKEN = "sakura_api_token"
_KEY_SAKURA_LAST_NODE = "sakura_last_node_id"
_KEY_LUAJIT_ENABLED = "luajit_enabled"
_KEY_MOD_LIST_COLUMNS = "mod_list_columns"
MOD_LIST_COLUMN_CHOICES = (1, 2, 3)
_KEY_MOD_CONFIG_SYNC = "mod_config_sync_enabled"
_KEY_LAST_PLATFORM = "last_platform"
_KEY_NAT_SUB_TAB = "nat_sub_tab"
_DEFAULT_NAT_SUB_TAB = "sakura"
_KEY_LAST_CLUSTER_PATH = "last_cluster_path"
_KEY_SELECTED_ACCOUNTS = "selected_accounts"
_KEY_SELFHOST_FRP_SERVER = "selfhost_frp_server"
_KEY_SELFHOST_FRP_MAPPINGS = "selfhost_frp_mappings"
_KEY_SELFHOST_SSH_CONNECTION = "selfhost_ssh_connection"
_KEY_LOLIA_SOURCES = "lolia_sources"
_KEY_LOLIA_LAST_NODE = "lolia_last_node_id"
_KEY_LOLIA_CLIENT_ID = "lolia_oauth_client_id"
_KEY_LOLIA_MAPPINGS = "lolia_mappings"
_KEY_LOBBY_ACCEL_ENABLED = "lobby_accel_enabled"
_KEY_LOBBY_ACCEL_MIHOMO_PATH = "lobby_accel_mihomo_path"
_KEY_LOBBY_ACCEL_MIHOMO_SHA256 = "lobby_accel_mihomo_sha256"
_KEY_LOBBY_ACCEL_WG_PORT = "lobby_accel_wireguard_port"
_KEY_LOBBY_ACCEL_WG_SERVER_PUBLIC_KEY = "lobby_accel_wireguard_server_public_key"
_KEY_GLOBAL_TOKENS = "global_tokens"
_KEY_TOKEN_HOLDS = "token_holds"
_KEY_AUTO_RESTART_CLUSTERS = "auto_restart_clusters"
_KEY_TOKEN_SWITCH_ON_TIMEOUT = "token_switch_on_timeout"
_KEY_TOKEN_SWITCH_AFTER_MINUTES = "token_switch_after_minutes"
_KEY_MOD_PRESETS = "mod_presets"
_KEY_DEDICATED_SERVER_EXTRA_ARGS = "dedicated_server_extra_args"


def get_settings_dir() -> Path:
    """返回设置文件所在目录：优先 %APPDATA%/DSTCamp，取不到则退回 ~/.dstcamp。"""
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "DSTCamp"
    return Path.home() / ".dstcamp"


# (路径, 修改时间, 大小) -> 文件原文。界面轮询里每秒要读很多次设置，云盘加杀软扫描下每次打开文件都很慢；
# 文件没变就复用原文，只多一次 stat。缓存原文而不是字典：每次重新解析，调用方改返回值不会污染缓存。
_cached_text: tuple[tuple[str, int, int], str] | None = None


def _stat_key(path: Path) -> tuple[str, int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return str(path), st.st_mtime_ns, st.st_size


def load_settings() -> dict:
    """读取设置文件，不存在或损坏都返回空字典（从不抛异常）。"""
    global _cached_text
    path = get_settings_dir() / _SETTINGS_FILE
    key = _stat_key(path)
    if key is None:
        return {}
    try:
        if _cached_text is not None and _cached_text[0] == key:
            text = _cached_text[1]
        else:
            text = path.read_text(encoding="utf-8")
            _cached_text = (key, text)
        return json.loads(text)
    except (OSError, ValueError):
        return {}


def save_settings(data: dict) -> None:
    """写入设置文件：先写临时文件再 os.replace() 原子替换，避免进程中途崩溃留下半个文件。

    坑：目标文件偶尔被杀软扫描、搜索索引等短暂打开，替换会报 WinError 5（拒绝访问），
    稍等重试几次（最多约 0.5 秒）；仍失败才抛出，原文件保持完好。"""
    settings_dir = get_settings_dir()
    settings_dir.mkdir(parents=True, exist_ok=True)
    path = settings_dir / _SETTINGS_FILE
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(tmp_path, path)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


def get_dedicated_server_path() -> Path | None:
    """用户手动确认过的 Steam 客户端通道专服安装目录（Steam 库里的），未设置返回 None。"""
    raw = load_settings().get(_KEY_DEDICATED_SERVER_PATH)
    return Path(raw) if raw else None


def set_dedicated_server_path(path: Path) -> None:
    """记住用户手动确认过的专用服务器安装目录。"""
    data = load_settings()
    data[_KEY_DEDICATED_SERVER_PATH] = str(path)
    save_settings(data)


def clear_dedicated_server_path() -> None:
    """忘掉手动确认过的专用服务器安装目录（迁移到客户端路径时用）。"""
    data = load_settings()
    if data.pop(_KEY_DEDICATED_SERVER_PATH, None) is not None:
        save_settings(data)


def get_steamcmd_server_path() -> Path | None:
    """SteamCMD 通道的独立专服目录（SteamCMD 安装或用户选的，不在 Steam 库里），未设置返回 None。"""
    raw = load_settings().get(_KEY_STEAMCMD_SERVER_PATH)
    return Path(raw) if raw else None


def set_steamcmd_server_path(path: Path) -> None:
    data = load_settings()
    data[_KEY_STEAMCMD_SERVER_PATH] = str(path)
    save_settings(data)


def get_client_runtime_path() -> Path | None:
    """取用户手动确认过的游戏客户端安装目录（开服程序为游戏客户端时用），没设置过则返回 None。"""
    raw = load_settings().get(_KEY_CLIENT_RUNTIME_PATH)
    return Path(raw) if raw else None


def set_client_runtime_path(path: Path) -> None:
    """记住用户手动确认过的游戏客户端安装目录。"""
    data = load_settings()
    data[_KEY_CLIENT_RUNTIME_PATH] = str(path)
    save_settings(data)


def get_server_runtime_mode() -> str | None:
    """开服程序选项原始值（auto/client/dedicated），没设置过返回 None；取值校验由调用方负责。"""
    raw = load_settings().get(_KEY_SERVER_RUNTIME_MODE)
    return str(raw) if raw else None


def set_server_runtime_mode(value: str) -> None:
    """保存开服程序选项。"""
    data = load_settings()
    data[_KEY_SERVER_RUNTIME_MODE] = value
    save_settings(data)


def get_dedicated_server_extra_args() -> str:
    """返回专用服务器启动时追加的命令行参数。"""
    return str(load_settings().get(_KEY_DEDICATED_SERVER_EXTRA_ARGS, ""))


def set_dedicated_server_extra_args(value: str) -> None:
    """保存专用服务器启动时追加的命令行参数。"""
    data = load_settings()
    value = value.strip()
    if value:
        data[_KEY_DEDICATED_SERVER_EXTRA_ARGS] = value
    else:
        data.pop(_KEY_DEDICATED_SERVER_EXTRA_ARGS, None)
    save_settings(data)


def get_klei_root_override() -> Path | None:
    """用户手动指定的 Steam 版存档根目录（替代自动找到的 文档/Klei/DoNotStarveTogether），未设置返回 None。"""
    raw = load_settings().get(_KEY_KLEI_ROOT_PATH)
    return Path(raw) if raw else None


def set_klei_root_override(path: Path | None) -> None:
    """记住手动指定的存档根目录；传 None 恢复自动查找。"""
    data = load_settings()
    if path:
        data[_KEY_KLEI_ROOT_PATH] = str(path)
    else:
        data.pop(_KEY_KLEI_ROOT_PATH, None)
    save_settings(data)


def get_wegame_root_path() -> Path | None:
    """返回用户确认的 WeGame ``rail_apps`` 目录。"""
    raw = load_settings().get(_KEY_WEGAME_ROOT_PATH)
    return Path(raw) if raw else None


def set_wegame_root_path(path: Path | None) -> None:
    """记住用户手动确认过的 WeGame 安装根目录；传 None 清空。"""
    data = load_settings()
    if path:
        data[_KEY_WEGAME_ROOT_PATH] = str(path)
    else:
        data.pop(_KEY_WEGAME_ROOT_PATH, None)
    save_settings(data)


def get_steamcmd_path() -> Path | None:
    """用户选过的 steamcmd.exe，未设置返回 None。"""
    raw = load_settings().get(_KEY_STEAMCMD_PATH)
    return Path(raw) if raw else None


def set_steamcmd_path(path: Path | None) -> None:
    """记住用户选过的 steamcmd.exe；传 None 清空。"""
    data = load_settings()
    if path:
        data[_KEY_STEAMCMD_PATH] = str(path)
    else:
        data.pop(_KEY_STEAMCMD_PATH, None)
    save_settings(data)


def get_steamcmd_account() -> str:
    """SteamCMD 登录用的 Steam 用户名，空字符串表示匿名；密码从不保存。"""
    return load_settings().get(_KEY_STEAMCMD_ACCOUNT) or ""


def set_steamcmd_account(username: str) -> None:
    data = load_settings()
    if username:
        data[_KEY_STEAMCMD_ACCOUNT] = username
    else:
        data.pop(_KEY_STEAMCMD_ACCOUNT, None)
    save_settings(data)


def get_steam_backend() -> str | None:
    """Steam 通道（见 features/local_service/steam_backend.py），未设置返回 None。"""
    return load_settings().get(_KEY_STEAM_BACKEND)


def set_steam_backend(value: str) -> None:
    data = load_settings()
    data[_KEY_STEAM_BACKEND] = value
    save_settings(data)


def get_usage_scenario_migrated() -> bool:
    """开服场景迁移（见 features/local_service/usage_scenario.py）是否已做过。"""
    return bool(load_settings().get(_KEY_USAGE_SCENARIO_MIGRATED))


def set_usage_scenario_migrated() -> None:
    data = load_settings()
    data[_KEY_USAGE_SCENARIO_MIGRATED] = True
    save_settings(data)


def get_steam_mods_path() -> Path | None:
    """返回 Steam 客户端 Mod 目录的手动覆盖值。"""
    raw = load_settings().get(_KEY_STEAM_MODS_PATH)
    return Path(raw) if raw else None


def set_steam_mods_path(path: Path | None) -> None:
    """记住用户手动确认过的 Steam 客户端 mods 文件夹路径；传 None 清空
    （清空后退回自动识别）。"""
    data = load_settings()
    if path:
        data[_KEY_STEAM_MODS_PATH] = str(path)
    else:
        data.pop(_KEY_STEAM_MODS_PATH, None)
    save_settings(data)


def get_theme_name() -> str:
    """返回主题名；合法性由主题模块统一校验。"""
    return load_settings().get(_KEY_THEME_NAME, _DEFAULT_THEME_NAME)


def set_theme_name(name: str) -> None:
    """记住用户选定的界面主题名——下次启动时 gui/theme.py 据此初始化调色板。"""
    data = load_settings()
    data[_KEY_THEME_NAME] = name
    save_settings(data)


def get_font_style_choice() -> str:
    """返回字体样式名；合法性由主题模块统一校验。"""
    return load_settings().get(_KEY_FONT_STYLE_CHOICE, _DEFAULT_FONT_STYLE_CHOICE)


def set_font_style_choice(choice: str) -> None:
    """记住用户选定的字体样式——下次启动时 gui/theme.py 据此初始化。"""
    data = load_settings()
    data[_KEY_FONT_STYLE_CHOICE] = choice
    save_settings(data)


def get_font_size_level() -> str:
    """返回全局字体大小档位；合法性由主题模块统一校验。"""
    return load_settings().get(_KEY_FONT_SIZE_LEVEL, _DEFAULT_FONT_SIZE_LEVEL)


def set_font_size_level(level: str) -> None:
    """记住用户选定的字体大小档位——下次启动时 gui/theme.py 据此初始化。"""
    data = load_settings()
    data[_KEY_FONT_SIZE_LEVEL] = level
    save_settings(data)


def get_last_platform() -> str | None:
    """返回上次选择的存档平台。"""
    return load_settings().get(_KEY_LAST_PLATFORM)


def set_last_platform(name: str) -> None:
    data = load_settings()
    data[_KEY_LAST_PLATFORM] = name
    save_settings(data)


def get_nat_sub_tab() -> str:
    """返回上次选择的内网穿透子页。"""
    return load_settings().get(_KEY_NAT_SUB_TAB, _DEFAULT_NAT_SUB_TAB)


def set_nat_sub_tab(key: str) -> None:
    data = load_settings()
    data[_KEY_NAT_SUB_TAB] = key
    save_settings(data)


def get_last_cluster_path() -> str | None:
    """返回上次选择的存档路径。"""
    return load_settings().get(_KEY_LAST_CLUSTER_PATH)


def set_last_cluster_path(path: str) -> None:
    data = load_settings()
    data[_KEY_LAST_CLUSTER_PATH] = path
    save_settings(data)


def get_selected_account(platform: str) -> tuple[str, str]:
    """返回用户手动选择的游戏账号 ID（按平台 steam/wegame 分开记录）及选择时 Steam 登录的账号 ID；
    没选过返回空串。旧版本只存了账号 ID，选择时的 Steam 账号视为未知。"""
    accounts = load_settings().get(_KEY_SELECTED_ACCOUNTS)
    value = accounts.get(platform) if isinstance(accounts, dict) else None
    if isinstance(value, str):
        return value, ""
    if isinstance(value, dict):
        account, active = value.get("id"), value.get("steam_active")
        return (account if isinstance(account, str) else "", active if isinstance(active, str) else "")
    return "", ""


def set_selected_account(platform: str, account_id: str, steam_active: str = "") -> None:
    data = load_settings()
    accounts = data.get(_KEY_SELECTED_ACCOUNTS)
    accounts = dict(accounts) if isinstance(accounts, dict) else {}
    accounts[platform] = {"id": account_id, "steam_active": steam_active}
    data[_KEY_SELECTED_ACCOUNTS] = accounts
    save_settings(data)


def get_player_note(player_id: str) -> str:
    """按 Klei 玩家标识返回跨存档共享的备注。"""
    return load_settings().get(_KEY_PLAYER_NOTES, {}).get(player_id, "")


def set_player_note(player_id: str, note: str) -> None:
    """记住用户给某个玩家标识设的备注；备注清空为空字符串时删掉这一条，
    不在设置文件里留一堆空值。"""
    data = load_settings()
    notes = data.get(_KEY_PLAYER_NOTES, {})
    if note:
        notes[player_id] = note
    else:
        notes.pop(player_id, None)
    data[_KEY_PLAYER_NOTES] = notes
    save_settings(data)


def get_window_position() -> tuple[int, int] | None:
    """返回主窗口左上角坐标；屏幕范围由 GUI 校验。"""
    raw = load_settings().get(_KEY_WINDOW_POS)
    if not raw or not isinstance(raw, list) or len(raw) != 2:
        return None
    try:
        return int(raw[0]), int(raw[1])
    except (TypeError, ValueError):
        return None


def set_window_position(x: int, y: int) -> None:
    """记住主窗口关闭前的左上角坐标，下次启动还原。"""
    data = load_settings()
    data[_KEY_WINDOW_POS] = [x, y]
    save_settings(data)


def get_window_size() -> tuple[int, int] | None:
    """返回主窗口上次关闭时的宽高（Qt 逻辑像素）；是否放得下由 GUI 校验。"""
    raw = load_settings().get(_KEY_WINDOW_SIZE)
    if not raw or not isinstance(raw, list) or len(raw) != 2:
        return None
    try:
        width, height = int(raw[0]), int(raw[1])
    except (TypeError, ValueError):
        return None
    return (width, height) if width > 0 and height > 0 else None


def set_window_size(width: int, height: int) -> None:
    """记住主窗口关闭前的宽高，下次启动沿用，不用每次重新拖。"""
    data = load_settings()
    data[_KEY_WINDOW_SIZE] = [width, height]
    save_settings(data)


def get_creation_wizard_size() -> tuple[int, int] | None:
    """返回"创建服务器存档"窗口上次关闭时的宽高（Qt 逻辑像素）；是否放得下由 GUI 校验。"""
    raw = load_settings().get(_KEY_CREATION_WIZARD_SIZE)
    if not raw or not isinstance(raw, list) or len(raw) != 2:
        return None
    try:
        width, height = int(raw[0]), int(raw[1])
    except (TypeError, ValueError):
        return None
    return (width, height) if width > 0 and height > 0 else None


def set_creation_wizard_size(width: int, height: int) -> None:
    """记住"创建服务器存档"窗口关闭前的宽高，下次打开沿用。"""
    data = load_settings()
    data[_KEY_CREATION_WIZARD_SIZE] = [width, height]
    save_settings(data)


def get_minimize_on_close() -> bool:
    """关闭窗口（右上角 X）时是否直接最小化到系统托盘而不弹窗确认，
    默认开启。"""
    return load_settings().get(_KEY_MINIMIZE_ON_CLOSE, True)


def set_minimize_on_close(value: bool) -> None:
    data = load_settings()
    data[_KEY_MINIMIZE_ON_CLOSE] = value
    save_settings(data)


def get_remind_update_enabled() -> bool:
    """启动时检测到新版本是否自动弹出更新窗口，默认开启。"""
    return load_settings().get(_KEY_REMIND_UPDATE_ENABLED, True)


def set_remind_update_enabled(value: bool) -> None:
    data = load_settings()
    data[_KEY_REMIND_UPDATE_ENABLED] = value
    save_settings(data)


def get_cache_use_exe_dir() -> bool:
    """是否把可重建缓存改放到 EXE 同级目录；重启后生效。"""
    return load_settings().get(_KEY_CACHE_USE_EXE_DIR, False)




def get_cache_dir_override() -> Path | None:
    """返回用户明确选择的缓存目录；未设置时由资源路径模块决定默认值。"""
    raw = load_settings().get(_KEY_CACHE_DIR)
    return Path(raw) if isinstance(raw, str) and raw.strip() else None


def set_cache_dir_override(path: Path | None) -> None:
    """保存自定义缓存目录，并结束旧版“跟随 EXE”布尔设置的迁移期。"""
    data = load_settings()
    data.pop(_KEY_CACHE_USE_EXE_DIR, None)
    if path is None:
        data.pop(_KEY_CACHE_DIR, None)
    else:
        data[_KEY_CACHE_DIR] = str(Path(path))
    save_settings(data)


def get_custom_bg_filename() -> str | None:
    """返回持久化背景图的文件名。"""
    return load_settings().get(_KEY_CUSTOM_BG_FILENAME)


def set_custom_bg_filename(name: str | None) -> None:
    data = load_settings()
    if name:
        data[_KEY_CUSTOM_BG_FILENAME] = name
    else:
        data.pop(_KEY_CUSTOM_BG_FILENAME, None)
    save_settings(data)


def get_backup_retention() -> int:
    """存档备份最多保留份数（5~99，默认 20），全局设置；读取时再夹一次范围防止手改配置文件。"""
    value = load_settings().get(_KEY_BACKUP_RETENTION, _DEFAULT_BACKUP_RETENTION)
    try:
        value = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_BACKUP_RETENTION
    return min(99, max(5, value))


def set_backup_retention(value: int) -> None:
    data = load_settings()
    data[_KEY_BACKUP_RETENTION] = min(99, max(5, int(value)))
    save_settings(data)


def get_backup_interval_minutes() -> int:
    """服务器运行时自动备份的间隔分钟数，范围 2~30，默认 10。"""
    value = load_settings().get(_KEY_BACKUP_INTERVAL_MIN, _DEFAULT_BACKUP_INTERVAL_MIN)
    try:
        value = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_BACKUP_INTERVAL_MIN
    return min(30, max(2, value))


def set_backup_interval_minutes(value: int) -> None:
    data = load_settings()
    data[_KEY_BACKUP_INTERVAL_MIN] = min(30, max(2, int(value)))
    save_settings(data)


def get_backup_auto_enabled() -> bool:
    """是否启用停止后及运行期间的自动备份。"""
    return load_settings().get(_KEY_BACKUP_AUTO_ENABLED, True)


def set_backup_auto_enabled(value: bool) -> None:
    data = load_settings()
    data[_KEY_BACKUP_AUTO_ENABLED] = bool(value)
    save_settings(data)


def get_sakura_token() -> str | None:
    """取用户设置过的 SakuraFrp API Token（从樱花网页后台复制来的凭据），
    没设置过返回 None。"""
    return load_settings().get(_KEY_SAKURA_TOKEN) or None


def set_sakura_token(token: str | None) -> None:
    data = load_settings()
    if token:
        data[_KEY_SAKURA_TOKEN] = token
    else:
        data.pop(_KEY_SAKURA_TOKEN, None)
    save_settings(data)


def get_sakura_last_node_id() -> int | None:
    """记住上次选中的樱花节点 ID，纯 UI 偏好（下次预选），不是隧道映射状态。"""
    raw = load_settings().get(_KEY_SAKURA_LAST_NODE)
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def set_sakura_last_node_id(node_id: int | None) -> None:
    data = load_settings()
    if node_id is not None:
        data[_KEY_SAKURA_LAST_NODE] = int(node_id)
    else:
        data.pop(_KEY_SAKURA_LAST_NODE, None)
    save_settings(data)


def get_window_ornaments_enabled() -> bool:
    """是否显示窗口点缀，默认关闭。"""
    return load_settings().get(_KEY_WINDOW_ORNAMENTS, False)


def set_window_ornaments_enabled(value: bool) -> None:
    data = load_settings()
    data[_KEY_WINDOW_ORNAMENTS] = value
    save_settings(data)


def get_custom_bg_opacity() -> float:
    """背景图与主题背景色混合的不透明度（0 全是主题色，1 全是原图），默认 0.35。"""
    return load_settings().get(_KEY_CUSTOM_BG_OPACITY, _DEFAULT_CUSTOM_BG_OPACITY)


def set_custom_bg_opacity(value: float) -> None:
    data = load_settings()
    data[_KEY_CUSTOM_BG_OPACITY] = value
    save_settings(data)


def get_luajit_enabled() -> bool:
    """是否使用全局共享的 LuaJIT 隔离副本。"""
    return load_settings().get(_KEY_LUAJIT_ENABLED, False)


def set_luajit_enabled(value: bool) -> None:
    data = load_settings()
    data[_KEY_LUAJIT_ENABLED] = bool(value)
    save_settings(data)


def get_mod_list_columns() -> int:
    """Mod 管理页列表显示几列（1/2/3），默认 1 列；配置里是非法值时也退回 1。"""
    value = load_settings().get(_KEY_MOD_LIST_COLUMNS, 1)
    return value if value in MOD_LIST_COLUMN_CHOICES else 1


def set_mod_list_columns(value: int) -> None:
    data = load_settings()
    data[_KEY_MOD_LIST_COLUMNS] = value if value in MOD_LIST_COLUMN_CHOICES else 1
    save_settings(data)


def get_mod_config_sync_enabled() -> bool:
    """Mod 配置记忆与游戏客户端 mod_config_data 双向同步，默认开启。"""
    return bool(load_settings().get(_KEY_MOD_CONFIG_SYNC, True))


def set_mod_config_sync_enabled(enabled: bool) -> None:
    data = load_settings()
    data[_KEY_MOD_CONFIG_SYNC] = bool(enabled)
    save_settings(data)


def get_selfhost_frp_server() -> dict | None:
    """自建 frps 服务器连接信息（host/bind_port/token），全局一份供多个存档复用，未配置返回 None。"""
    return load_settings().get(_KEY_SELFHOST_FRP_SERVER) or None


def set_selfhost_frp_server(host: str, bind_port: int, token: str) -> None:
    data = load_settings()
    data[_KEY_SELFHOST_FRP_SERVER] = {"host": host, "bind_port": int(bind_port), "token": token}
    save_settings(data)




def _selfhost_mapping_key(cluster_path: Path, shard_name: str) -> str:
    return f"{cluster_path}::{shard_name}"


def get_selfhost_frp_mapping(cluster_path: Path, shard_name: str) -> int | None:
    """该世界分到的自建 frps 远程端口（自建服务器没有分配 API，只能本地记账，见 frp_selfhost/deploy.py）。"""
    mappings = load_settings().get(_KEY_SELFHOST_FRP_MAPPINGS) or {}
    raw = mappings.get(_selfhost_mapping_key(cluster_path, shard_name))
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def set_selfhost_frp_mapping(cluster_path: Path, shard_name: str, remote_port: int | None) -> None:
    data = load_settings()
    mappings = data.get(_KEY_SELFHOST_FRP_MAPPINGS) or {}
    key = _selfhost_mapping_key(cluster_path, shard_name)
    if remote_port is not None:
        mappings[key] = int(remote_port)
    else:
        mappings.pop(key, None)
    data[_KEY_SELFHOST_FRP_MAPPINGS] = mappings
    save_settings(data)


def get_all_selfhost_frp_ports() -> list[int]:
    """本地记账过的所有已分配远程端口（不分存档）——分配新端口时用来避
    免跟其它存档/世界已经占用的端口撞车。"""
    mappings = load_settings().get(_KEY_SELFHOST_FRP_MAPPINGS) or {}
    ports = []
    for raw in mappings.values():
        try:
            ports.append(int(raw))
        except (TypeError, ValueError):
            continue
    return ports



def get_lolia_source(cluster_path: Path, shard_name: str) -> dict | None:
    """用户给这个世界粘贴过的 Lolia 隧道来源（features/lolia/config.py 的 parse_source
    结果：原版 frpc 配置或快捷启动命令，含节点凭据）。关闭映射后仍保留，方便下次直接开启。"""
    raw = (load_settings().get(_KEY_LOLIA_SOURCES) or {}).get(_selfhost_mapping_key(cluster_path, shard_name))
    return raw if isinstance(raw, dict) and raw.get("kind") in ("config", "cli") else None


def set_lolia_source(cluster_path: Path, shard_name: str, source: dict | None) -> None:
    data = load_settings()
    sources = data.get(_KEY_LOLIA_SOURCES) or {}
    key = _selfhost_mapping_key(cluster_path, shard_name)
    if source is not None:
        sources[key] = source
    else:
        sources.pop(key, None)
    data[_KEY_LOLIA_SOURCES] = sources
    save_settings(data)


def get_lolia_mapping(cluster_path: Path, shard_name: str) -> dict | None:
    """已生效的 Lolia 映射 {"remote_port": int, "host": str, "tunnel": str | None}；没开启返回 None。
    `tunnel` 是 DSTCamp 通过 OAuth 自动创建的隧道名，关闭映射时据此删除；粘贴配置方式为 None。"""
    raw = (load_settings().get(_KEY_LOLIA_MAPPINGS) or {}).get(_selfhost_mapping_key(cluster_path, shard_name))
    if not isinstance(raw, dict):
        return None
    try:
        return {"remote_port": int(raw["remote_port"]), "host": str(raw.get("host", "")),
                "tunnel": raw.get("tunnel") or None}
    except (KeyError, TypeError, ValueError):
        return None


def set_lolia_mapping(cluster_path: Path, shard_name: str, remote_port: int | None, host: str = "",
                      tunnel: str | None = None) -> None:
    data = load_settings()
    mappings = data.get(_KEY_LOLIA_MAPPINGS) or {}
    key = _selfhost_mapping_key(cluster_path, shard_name)
    if remote_port is not None:
        mappings[key] = {"remote_port": int(remote_port), "host": host, "tunnel": tunnel}
    else:
        mappings.pop(key, None)
    data[_KEY_LOLIA_MAPPINGS] = mappings
    save_settings(data)


def get_lolia_client_id() -> str | None:
    """用户自己创建的 Lolia OAuth 应用 client_id；没填过返回 None（用内置的）。"""
    return load_settings().get(_KEY_LOLIA_CLIENT_ID) or None


def set_lolia_client_id(client_id: str | None) -> None:
    data = load_settings()
    if client_id:
        data[_KEY_LOLIA_CLIENT_ID] = client_id
    else:
        data.pop(_KEY_LOLIA_CLIENT_ID, None)
    save_settings(data)


def get_lolia_last_node_id() -> int | None:
    """记住上次选中的 Lolia 节点 ID，纯 UI 偏好（下次预选）。"""
    raw = load_settings().get(_KEY_LOLIA_LAST_NODE)
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def set_lolia_last_node_id(node_id: int | None) -> None:
    data = load_settings()
    if node_id is not None:
        data[_KEY_LOLIA_LAST_NODE] = int(node_id)
    else:
        data.pop(_KEY_LOLIA_LAST_NODE, None)
    save_settings(data)

def get_selfhost_ssh_connection() -> dict | None:
    """SSH 远程部署对话框记住的上次连接信息（host/port/username）——
    绝不包含密码，密码按用户明确要求从不落盘，每次都要现输。"""
    return load_settings().get(_KEY_SELFHOST_SSH_CONNECTION) or None


def set_selfhost_ssh_connection(host: str, port: int, username: str) -> None:
    data = load_settings()
    data[_KEY_SELFHOST_SSH_CONNECTION] = {"host": host, "port": int(port), "username": username}
    save_settings(data)


def get_lobby_accel_enabled() -> bool:
    """是否启用自建 frps 的实验性大厅加速。"""
    return bool(load_settings().get(_KEY_LOBBY_ACCEL_ENABLED, False))


def set_lobby_accel_enabled(value: bool) -> None:
    data = load_settings()
    data[_KEY_LOBBY_ACCEL_ENABLED] = bool(value)
    save_settings(data)


def get_lobby_accel_mihomo_path() -> Path | None:
    """用户自行提供的 Mihomo 可执行文件；第三方二进制不随设置复制。"""
    raw = load_settings().get(_KEY_LOBBY_ACCEL_MIHOMO_PATH)
    return Path(raw) if raw else None


def get_lobby_accel_mihomo_sha256() -> str | None:
    raw = load_settings().get(_KEY_LOBBY_ACCEL_MIHOMO_SHA256)
    return str(raw).lower() if raw else None


def set_lobby_accel_mihomo_path(path: Path | None, sha256: str | None = None) -> None:
    data = load_settings()
    if path is None:
        data.pop(_KEY_LOBBY_ACCEL_MIHOMO_PATH, None)
        data.pop(_KEY_LOBBY_ACCEL_MIHOMO_SHA256, None)
    else:
        data[_KEY_LOBBY_ACCEL_MIHOMO_PATH] = str(path)
        if sha256:
            data[_KEY_LOBBY_ACCEL_MIHOMO_SHA256] = str(sha256).lower()
        else:
            data.pop(_KEY_LOBBY_ACCEL_MIHOMO_SHA256, None)
    save_settings(data)


def get_lobby_accel_wireguard() -> dict | None:
    data = load_settings()
    public_key = data.get(_KEY_LOBBY_ACCEL_WG_SERVER_PUBLIC_KEY)
    if not public_key:
        return None
    try:
        port = int(data.get(_KEY_LOBBY_ACCEL_WG_PORT, 51820))
    except (TypeError, ValueError):
        return None
    if not 1 <= port <= 65535:
        return None
    return {"port": port, "server_public_key": str(public_key)}


def set_lobby_accel_wireguard(port: int, server_public_key: str) -> None:
    port = int(port)
    if not 1 <= port <= 65535:
        raise ValueError("WireGuard 端口必须在 1..65535")
    data = load_settings()
    data[_KEY_LOBBY_ACCEL_WG_PORT] = port
    data[_KEY_LOBBY_ACCEL_WG_SERVER_PUBLIC_KEY] = server_public_key.strip()
    save_settings(data)


def get_global_tokens() -> list[str]:
    """全局令牌池（所有存档共享）：复制出的服务器存档没有令牌时自动取第一个；启动时再按新旧格式和占用状态
    选择可用项（见 token_scheduler.py）。"""
    tokens = load_settings().get(_KEY_GLOBAL_TOKENS) or []
    return [tok for tok in tokens if isinstance(tok, str) and tok]


def set_global_tokens(tokens: list[str]) -> None:
    data = load_settings()
    data[_KEY_GLOBAL_TOKENS] = list(tokens)
    save_settings(data)


def get_token_holds() -> dict[str, dict]:
    """读取新令牌疑似未在 Klei 端释放的本机记录。

    ``retry_at`` 之前不自动选用；``failures`` 为连续冲突次数，决定下次等待时长；缺这两项的旧记录视为可立即重试。"""
    raw = load_settings().get(_KEY_TOKEN_HOLDS) or {}
    if not isinstance(raw, dict):
        return {}
    result = {}
    for fingerprint, item in raw.items():
        if (
            isinstance(fingerprint, str)
            and isinstance(item, dict)
            and item.get("state") in ("crashed", "conflict")
        ):
            try:
                since = float(item.get("since", 0) or 0)
            except (TypeError, ValueError):
                since = 0.0
            try:
                retry_at = float(item.get("retry_at", 0) or 0)
                failures = int(item.get("failures", 0) or 0)
            except (TypeError, ValueError):
                retry_at, failures = 0.0, 0
            result[fingerprint] = {
                "state": item["state"],
                "cluster_key": str(item.get("cluster_key", "")),
                "cluster_name": str(item.get("cluster_name", "")),
                "since": since,
                "retry_at": retry_at,
                "failures": failures,
            }
    return result


def blocking_token_holds(now: float) -> dict[str, dict]:
    """仍在等待期内（``retry_at`` 未到）的令牌记录；过了等待期的令牌允许再试一次。"""
    return {fingerprint: item for fingerprint, item in get_token_holds().items() if item["retry_at"] > now}


def set_token_hold(
    fingerprint: str, *, state: str, cluster_key: str,
    cluster_name: str, since: float, retry_at: float = 0.0, failures: int = 0,
) -> None:
    if state not in ("crashed", "conflict"):
        raise ValueError(f"unsupported token hold state: {state}")
    data = load_settings()
    holds = get_token_holds()
    holds[fingerprint] = {
        "state": state,
        "cluster_key": cluster_key,
        "cluster_name": cluster_name,
        "since": float(since),
        "retry_at": float(retry_at),
        "failures": int(failures),
    }
    data[_KEY_TOKEN_HOLDS] = holds
    save_settings(data)


def clear_token_hold(fingerprint: str) -> None:
    data = load_settings()
    holds = get_token_holds()
    if fingerprint not in holds:
        return
    del holds[fingerprint]
    if holds:
        data[_KEY_TOKEN_HOLDS] = holds
    else:
        data.pop(_KEY_TOKEN_HOLDS, None)
    save_settings(data)


def get_auto_restart_enabled(cluster_key: str) -> bool:
    """存档是否开启崩溃自动重启（按存档单独设置，默认关闭）。"""
    raw = load_settings().get(_KEY_AUTO_RESTART_CLUSTERS) or []
    return isinstance(raw, list) and cluster_key in raw


def set_auto_restart_enabled(cluster_key: str, enabled: bool) -> None:
    data = load_settings()
    raw = data.get(_KEY_AUTO_RESTART_CLUSTERS)
    keys = [key for key in raw if isinstance(key, str)] if isinstance(raw, list) else []
    keys = [key for key in keys if key != cluster_key]
    if enabled:
        keys.append(cluster_key)
    if keys:
        data[_KEY_AUTO_RESTART_CLUSTERS] = keys
    else:
        data.pop(_KEY_AUTO_RESTART_CLUSTERS, None)
    save_settings(data)


# 崩溃后换用空闲令牌前的等待分钟数：默认 0 = 有空闲令牌就立即换、马上上线（换下的令牌约半小时后
# 释放、等待标记到期即恢复可用）；想省令牌可调大，让原令牌先等 Klei 释放。
# 上限要小于自动重启总等待 2 小时，否则永远轮不到换令牌
TOKEN_SWITCH_MINUTES_DEFAULT = 0
TOKEN_SWITCH_MINUTES_RANGE = (0, 110)


def get_token_switch_on_timeout() -> bool:
    """自动重启等原令牌超时（get_token_switch_after_minutes()）仍在注册冲突时，是否换用令牌池里的其它令牌（默认开启）。"""
    return load_settings().get(_KEY_TOKEN_SWITCH_ON_TIMEOUT, True) is not False


def set_token_switch_on_timeout(enabled: bool) -> None:
    data = load_settings()
    data[_KEY_TOKEN_SWITCH_ON_TIMEOUT] = bool(enabled)
    save_settings(data)


def _clamp_switch_minutes(value) -> int:
    low, high = TOKEN_SWITCH_MINUTES_RANGE
    try:
        return min(max(int(value), low), high)
    except (TypeError, ValueError):
        return TOKEN_SWITCH_MINUTES_DEFAULT


def get_token_switch_after_minutes() -> int:
    """崩溃后原令牌持续注册冲突多少分钟才换令牌，超出范围的旧值按边界处理。"""
    return _clamp_switch_minutes(load_settings().get(_KEY_TOKEN_SWITCH_AFTER_MINUTES, TOKEN_SWITCH_MINUTES_DEFAULT))


def set_token_switch_after_minutes(minutes: int) -> int:
    """保存换令牌等待分钟数，返回实际保存（夹到合法范围后）的值。"""
    value = _clamp_switch_minutes(minutes)
    data = load_settings()
    data[_KEY_TOKEN_SWITCH_AFTER_MINUTES] = value
    save_settings(data)
    return value


def prune_token_holds(tokens: list[str], expire_before: float | None = None) -> None:
    """删除已不在令牌池中的等待记录；给了 ``expire_before`` 时，更早的记录也删除
    （存档换用别的令牌后，旧令牌的记录永远不会因注册成功而清除）。"""
    from dstools.shared.token_manager import token_fingerprint

    allowed = {token_fingerprint(token) for token in tokens if token}
    data = load_settings()
    holds = {
        fingerprint: item
        for fingerprint, item in get_token_holds().items()
        if fingerprint in allowed and (expire_before is None or item["since"] >= expire_before)
    }
    if holds:
        data[_KEY_TOKEN_HOLDS] = holds
    else:
        data.pop(_KEY_TOKEN_HOLDS, None)
    save_settings(data)


def get_mod_presets() -> list[dict]:
    """取全部 Mod 配置集原始数据（list），转换与校验由 mod/presets.py 负责。"""
    raw = load_settings().get(_KEY_MOD_PRESETS) or []
    return raw if isinstance(raw, list) else []


def set_mod_presets(presets: list[dict]) -> None:
    data = load_settings()
    data[_KEY_MOD_PRESETS] = list(presets)
    save_settings(data)
