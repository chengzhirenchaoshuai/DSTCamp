"""modinfo.lua 静态解析：发现 Mod 目录，读取元数据与配置项定义。"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dstools.shared.lua_parser import parse_lua_value
from dstools.shared.steam_discovery import find_all_steam_libraries
from dstools.models import Platform

# 双引号字符串内容，支持 `\"` 转义（朴素的 `[^"]*` 会在转义引号处截断）
_QSTR = r'(?:[^"\\]|\\.)*'
# 单引号字符串同理——Lua 把 ' 和 " 一视同仁，不少 mod 整个文件都用单引号。
_QSTR_SINGLE = r"(?:[^'\\]|\\.)*"
# 两种引号风格作为两个可选捕获组——配合 _pick_quoted() 取实际命中的那个。
_QUOTED_ALT = rf'"({_QSTR})"|\'({_QSTR_SINGLE})\''


def _pick_quoted(m: re.Match) -> str:
    """给定一个匹配了包含 _QUOTED_ALT 的模式的 re.Match，返回两个可选捕获
    组里实际命中的那一个。"""
    return m.group(1) if m.group(1) is not None else m.group(2)


def _contains_cjk(s: str) -> bool:
    """字符串里是否含有 CJK 统一表意文字（汉字）——用于判断一个字符串字
    面量是不是中文，见 _extract_quoted 的三元双语名处理。"""
    return any("一" <= ch <= "鿿" for ch in s)


_LUA_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "'": "'"}

# 一整个带引号的字符串（任一风格），供 _replace_idents_outside_strings 整
# 段跳过字符串内容用，避免误匹配到字符串内部一个长得像标识符的子串。
_ANY_STRING = re.compile(rf'"{_QSTR}"|\'{_QSTR_SINGLE}\'')


def _replace_idents_outside_strings(text: str, subst_map: dict[str, str]) -> str:
    """一次扫描把 ``subst_map`` 中的标识符替换为对应值，跳过引号字符串内部。

    坑：参数名常与字符串里的英文单词相同（如 "default"），按单词边界替换会把
    字符串内容改坏；逐个标识符 re.sub 在 400+ 选项的 Mod 上耗时 700ms+，
    所以合并成一个交替分支正则一次扫完。
    """
    if not subst_map:
        return text
    idents = "|".join(re.escape(i) for i in subst_map)
    pattern = re.compile(rf"{_ANY_STRING.pattern}|\b(?:{idents})\b(?!\s*=(?!=))")

    def repl(m):
        s = m.group(0)
        if s and s[0] in ('"', "'"):
            return s
        return subst_map.get(s, s)

    return pattern.sub(repl, text)


_LONG_BRACKET_OPEN = re.compile(r"\[(=*)\[")


def _strip_lua_comments(text: str) -> str:
    """把 Lua 行注释/块注释替换为等长空白（保留换行），其余字符原位不动。

    本模块其他函数都靠括号深度计数定位，不认识注释；注释里多出的 ``{``/``}``
    会让外层表提前闭合、截断后续选项，所以每个文件先过一遍这里。对引号和
    长括号字符串敏感，文本里的 "--" 不会被当成注释。
    """
    out = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in ('"', "'"):
            quote = ch
            out.append(ch)
            i += 1
            while i < n:
                c = text[i]
                out.append(c)
                if c == "\\" and i + 1 < n:
                    i += 1
                    out.append(text[i])
                elif c == quote:
                    i += 1
                    break
                i += 1
            continue
        if ch == "[":
            lm = _LONG_BRACKET_OPEN.match(text, i)
            if lm:
                closer = "]" + lm.group(1) + "]"
                close_idx = text.find(closer, lm.end())
                end = close_idx + len(closer) if close_idx != -1 else n
                out.append(text[i:end])
                i = end
                continue
        if text.startswith("--", i):
            j = i + 2
            lm = _LONG_BRACKET_OPEN.match(text, j)
            if lm:
                closer = "]" + lm.group(1) + "]"
                close_idx = text.find(closer, lm.end())
                end = close_idx + len(closer) if close_idx != -1 else n
            else:
                nl = text.find("\n", j)
                end = nl if nl != -1 else n
            out.append("".join(c if c == "\n" else " " for c in text[i:end]))
            i = end
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _unescape_lua_string(s: str) -> str:
    """解码 Lua 字符串转义（\\n、\\t 等），正则捕获的字符串里转义原样保留。"""
    return re.sub(r"\\(.)", lambda m: _LUA_ESCAPES.get(m.group(1), m.group(0)), s)


@dataclass
class ModConfigOption:
    """modinfo.lua 中的单条配置项定义。

    坑：增删字段时必须把 mod/cache.py 的 _CACHE_FORMAT_VERSION 加一，否则旧缓存
    会以默认值补齐新字段，表现为"修了 bug 界面不变"。"""

    name: str = ""  # 配置键名
    label: str = ""  # 显示标签
    hover: str = ""  # 悬浮提示
    default: Any = None  # 默认值
    choices: list[dict] = field(default_factory=list)
    # 每个选项：{"description": "...", "data": value, "hover": "..."}
    is_header: bool = False  # 纯视觉分区标题，不是真实设置项
    # options 存在但需运行 Lua 才能得出（函数调用或循环拼出的变量），区别于"本来就没有选项"
    is_dynamic: bool = False
    # is_dynamic 时保留 options 的原始表达式，供沙箱按需求值（见 resolve_dynamic_option）
    raw_options_expr: str = ""
    # 选项自己的 client = true：作者约定的纯客户端设置，管理工具不显示（见 visible_config_options）
    client: bool = False
    # 以下四项是 "Configs Extended"（3317960157）的约定，值形状下拉框表达不了，配置弹窗用专门控件：
    # 集合 {["k"]=true}、有序数组、纯字符串、字符串键值对表
    is_set_config: bool = False
    is_array_config: bool = False
    is_text_config: bool = False
    is_dictionary_config: bool = False


@dataclass
class ModInfo:
    """来自 modinfo.lua 的 mod 元数据与配置。"""

    name: str = ""
    author: str = ""
    version: str = ""
    # 版本只信任完整沙箱执行后的最终值：pending/confirmed/undeclared/unresolved；
    # 静态解析到的 version 保留但不能冒充 confirmed
    version_status: str = "pending"
    version_source: str = ""
    version_compatible: str = ""
    version_compatible_status: str = "pending"
    description: str = ""
    workshop_id: str = ""  # 从文件夹名派生
    icon: str = ""  # 例如 "modicon.tex"，相对于 icon_atlas 所在文件夹
    icon_atlas: str = ""  # 例如 "images/modicon.xml"，相对于 mod_folder
    config_options: list[ModConfigOption] = field(default_factory=list)
    # configuration_options 赋值之前的全部源码（辅助函数/局部表），供沙箱按需求值动态选项
    dynamic_preamble: str = ""
    # 声明了 configuration_options 但没有一条能识别（如 Insight 以选项名作键），界面需说明原因
    unsupported_schema: bool = False
    # 本次会话是否已经为这个 mod 尝试过 resolve_full_modinfo()（不管成
    # 败都会设置），避免弹窗反复打开时重复跑一遍较慢的沙箱解析。
    full_sandbox_tried: bool = False
    # client_only_mod = true：只影响客户端，不经 modoverrides.lua 同步。
    # 坑：server_only_mod / all_clients_require_mod 任一为真时要盖过 client_only_mod
    # （引擎不读，是给开服工具的约定，DontStarveLuaJIT2 作者确认）
    client_only: bool = False
    # 本会话是否已尝试叠加 Chinese++ Pro 的配置项翻译（一次性开关，避免反复起沙箱，见 chs_translation.py）
    chs_translation_tried: bool = False


def visible_config_options(
    config_options: list[ModConfigOption],
) -> list[ModConfigOption]:
    """过滤 client=true 的纯客户端配置项；分组标题下的选项全被滤掉时，标题一并去掉。"""
    sections: list[tuple[ModConfigOption | None, list[ModConfigOption]]] = []
    current_header: ModConfigOption | None = None
    current_options: list[ModConfigOption] = []
    for opt in config_options:
        if opt.is_header:
            sections.append((current_header, current_options))
            current_header, current_options = opt, []
        else:
            current_options.append(opt)
    sections.append((current_header, current_options))

    result: list[ModConfigOption] = []
    for header, options in sections:
        visible = [o for o in options if not o.client]
        if not visible:
            continue
        if header is not None:
            result.append(header)
        result.extend(visible)
    return result


# ── Steam / Mod 路径发现 ─────────────────────────────────────────────

# 已知的 DST Steam Workshop App ID
DST_APP_ID = "322330"
_MAX_PUBLISHED_FILE_ID = (1 << 64) - 1


def is_workshop_content_id(value: str | int) -> bool:
    """是否为 Steam ``content/322330/<PublishedFileId_t>`` 的标准目录名。

    非零 uint64、无前导零的 ASCII 十进制；不能用 ``str.isdigit()``（会接受全角数字），
    也不能限制位数。
    """
    text = str(value)
    if not text or not text.isascii() or not text.isdecimal():
        return False
    if text[0] == "0":
        return False
    try:
        return int(text) <= _MAX_PUBLISHED_FILE_ID
    except ValueError:
        return False


def is_custom_steam_mod_id(value: str | int) -> bool:
    """是否为 Steam 游戏 ``mods/`` 下手动放入、非 ``workshop-<id>`` 命名的自定义 Mod。"""
    text = str(value)
    prefix = "workshop-"
    return not (text.startswith(prefix) and is_workshop_content_id(text[len(prefix) :]))


def split_installed_mod_counts(mod_ids, platform: Platform) -> tuple[int, int]:
    """返回 ``(普通模组数, 自定义模组数)``；WeGame 的 ID 不带 ``workshop-`` 前缀，全部计为普通。"""
    ids = list(mod_ids)
    if platform != Platform.STEAM:
        return len(ids), 0
    custom = sum(1 for mod_id in ids if is_custom_steam_mod_id(mod_id))
    return len(ids) - custom, custom


def _steamcmd_workshop_dir() -> Path | None:
    """Steam 通道为 SteamCMD 时返回它的工坊目录，否则返回 None。两种通道不混用：选了 SteamCMD 就不再看 Steam 库。"""
    # 延迟导入：steam_backend 经 server_runtime 间接依赖 Mod 模块
    from dstools.features.local_service.steam_backend import SteamBackend, get_backend
    from dstools.features.local_service.steamcmd_updater import steamcmd_workshop_dir
    if get_backend() is SteamBackend.STEAMCMD:
        return steamcmd_workshop_dir()
    return None


def find_workshop_dir() -> Path | None:
    """查找 DST Workshop 内容目录：SteamCMD 通道用它自己的工坊目录；否则遍历注册表里的全部 Steam 库，不能只看默认库。"""
    steamcmd_workshop = _steamcmd_workshop_dir()
    if steamcmd_workshop is not None:
        content = steamcmd_workshop / "content" / DST_APP_ID
        return content if content.exists() else None
    for steam in find_all_steam_libraries():
        workshop = steam / "steamapps" / "workshop" / "content" / DST_APP_ID
        if workshop.exists():
            return workshop
    return None


def is_mod_subscribed(workshop_id: str) -> bool:
    """以本地是否存在带 modinfo.lua 的 Workshop 目录判断已订阅（DSTCamp 无法查询账号订阅）。"""
    workshop_dir = find_workshop_dir()
    if workshop_dir is None or not is_workshop_content_id(workshop_id):
        return False
    candidate = workshop_dir / workshop_id
    return candidate.exists() and (candidate / "modinfo.lua").exists()


def detect_mod_format(workshop_id, workshop_root: Path | None, steam_state=None) -> str | None:
    """返回 Workshop Mod 格式 "V1"/"V2"，无法判断（如本地手动安装）返回 None。

    有 Steam 状态时以 LegacyItem 位为准；否则根目录有 modinfo.lua 为 V2，只有
    ``*_legacy.bin`` 为 V1。"""
    if steam_state is not None:
        return "V1" if steam_state.legacy_item else "V2"
    text = str(workshop_id).removeprefix("workshop-")
    if workshop_root is None or not text.isdigit():
        return None
    item_dir = workshop_root / text
    if (item_dir / "modinfo.lua").is_file():
        return "V2"
    try:
        if any(path.is_file() for path in item_dir.glob("*_legacy.bin")):
            return "V1"
    except OSError:
        return None
    return None


def find_shared_ugc_directory() -> Path | None:
    """专服 ``-ugc_directory`` 参数：直接用 Steam 的 ``steamapps/workshop``（真机验证），
    所有存档共享一份内容，不再在每个 shard 下生成 ugc_mods 副本。找不到返回 None，
    调用方不传该参数即可。

    SteamCMD 通道始终返回它的工坊目录（还没下载过 Mod 时也返回），启动前的缺失检查才能提示去下载。"""
    steamcmd_workshop = _steamcmd_workshop_dir()
    if steamcmd_workshop is not None:
        return steamcmd_workshop
    for steam in find_all_steam_libraries():
        workshop = steam / "steamapps" / "workshop"
        if (workshop / "content" / DST_APP_ID).exists():
            return workshop
    return None


def find_game_mods_dir() -> Path | None:
    """查找 DST 游戏 mods 目录：优先用户在 Mod 页手动指定的路径，失效时再自动识别。"""
    from dstools.shared import app_settings

    override = app_settings.get_steam_mods_path()
    if override and override.exists() and not is_dedicated_server_mods_dir(override):
        return override

    for steam in find_all_steam_libraries():
        mods = steam / "steamapps" / "common" / "Don't Starve Together" / "mods"
        if mods.exists():
            return mods
    return None


def default_steam_mods_dir() -> Path | None:
    """调用方没指定 mods 目录时的默认值：SteamCMD 通道用当前独立专服的 mods（一般没有游戏客户端），
    否则用游戏客户端 mods 目录。"""
    if _steamcmd_workshop_dir() is not None:
        # 延迟导入：server_runtime 间接依赖本模块
        from dstools.features.local_service.server_runtime import current_runtime
        runtime = current_runtime()
        return runtime.mods_dir if runtime is not None else None
    return find_game_mods_dir()


def is_dedicated_server_mods_dir(path: Path) -> bool:
    """路径是否就是独立专服的 ``mods``，用于阻止同步时源和目标指向同一目录。"""
    from dstools.shared import app_settings

    candidate = Path(path)
    targets: list[Path] = []
    configured = app_settings.get_dedicated_server_path()
    # 开服路径也可能手动选成游戏客户端目录，客户端 mods 不是专服 mods。
    if configured is not None and "dedicated server" in Path(configured).name.lower():
        targets.append(Path(configured) / "mods")
    steamcmd_server = app_settings.get_steamcmd_server_path()
    if steamcmd_server is not None:
        targets.append(Path(steamcmd_server) / "mods")
    for steam in find_all_steam_libraries():
        targets.append(
            steam
            / "steamapps"
            / "common"
            / "Don't Starve Together Dedicated Server"
            / "mods"
        )
    for target in targets:
        try:
            if candidate.resolve(strict=False) == target.resolve(strict=False):
                return True
        except OSError:
            if (
                str(candidate.absolute()).casefold()
                == str(target.absolute()).casefold()
            ):
                return True
    return False


# ── WeGame(Rail) / Mod 路径发现 ──────────────────────────────────────
# WeGame 没有独立的 Workshop 内容缓存，Mod 都在各产品自己的 mods/ 下；安装根目录
# 无可靠注册表项，只能用用户手动确认过的路径。


def _find_wegame_product_dir(root: Path, name_prefix: str) -> Path | None:
    """在 rail_apps 下按名称前缀通配匹配客户端/专服目录（数字 ID 因安装而异），取第一个带 mods/ 的。"""
    if not root.exists():
        return None
    for candidate in sorted(root.glob(f"{name_prefix}(*)")):
        if (candidate / "mods").exists():
            return candidate
    return None


def find_wegame_client_dir(wegame_root: Path) -> Path | None:
    """WeGame 版《饥荒：联机版》客户端安装目录（wegame_root 是 rail_apps
    那一层，来自 app_settings.get_wegame_root_path()，调用方负责取）。"""
    return _find_wegame_product_dir(wegame_root, "饥荒：联机版")


def find_wegame_server_dir(wegame_root: Path) -> Path | None:
    """WeGame 版《饥荒联机版专用服务器》安装目录。"""
    return _find_wegame_product_dir(wegame_root, "饥荒联机版专用服务器")


def resolve_wegame_client_mods_dir(platform: Platform) -> Path | None:
    """WeGame 平台返回用户设置的客户端 mods 目录，Steam 或未设置时返回 None（调用方不弹窗打扰）。"""
    if platform != Platform.WEGAME:
        return None
    from dstools.shared.app_settings import get_wegame_root_path

    root = get_wegame_root_path()
    if not root:
        return None
    client_dir = find_wegame_client_dir(root)
    return client_dir / "mods" if client_dir else None


def find_mod_folder(
    workshop_id: str,
    platform: Platform = Platform.STEAM,
    wegame_client_mods_dir: Path | None = None,
    steam_runtime_mods_dir: Path | None = None,
) -> Path | None:
    """按 Workshop ID（``workshop-123`` 或 ``123``）查找 Mod 目录，找不到返回 None。

    Steam：先查 Workshop 内容目录，再查游戏 mods 目录。
    WeGame：只查调用方传入的 ``wegame_client_mods_dir``，不能落到 Steam 目录。
    """
    raw_id = str(workshop_id)
    mod_id = raw_id.removeprefix("workshop-")
    canonical_id = f"workshop-{mod_id}" if is_workshop_content_id(mod_id) else raw_id

    if platform == Platform.WEGAME:
        game_mods = wegame_client_mods_dir
    else:
        workshop_dir = find_workshop_dir()
        if workshop_dir and is_workshop_content_id(mod_id):
            candidate = workshop_dir / mod_id
            if candidate.exists() and (candidate / "modinfo.lua").exists():
                return candidate
        # DSTCamp 主 Mod 管理页以专服实际消费目录为准；客户端 mods/ 仅
        # 作为未部署时的兼容回退。
        game_mods = steam_runtime_mods_dir or default_steam_mods_dir()

    if game_mods:
        candidate = game_mods / canonical_id  # Workshop 纯数字输入也规范为带前缀
        if candidate.exists() and (candidate / "modinfo.lua").exists():
            return candidate
        # 也试一下不带前缀的
        candidate = game_mods / mod_id
        if candidate.exists() and (candidate / "modinfo.lua").exists():
            return candidate

    return None


def list_installed_mod_ids(
    platform: Platform = Platform.STEAM,
    wegame_client_mods_dir: Path | None = None,
    legacy_packages: dict[int, Path] | None = None,
    steam_runtime_mods_dir: Path | None = None,
) -> list[str]:
    """枚举所有可读取的 Mod ID（目录式内容和有效 V1 包）。

    modoverrides.lua 只记录玩家改过的 Mod，必须扫描安装目录再交叉核对。
    WeGame 只扫 ``wegame_client_mods_dir``，避免混入 Steam 本地 Mod。
    """
    ids = []
    seen = set()

    if platform == Platform.WEGAME:
        if wegame_client_mods_dir and wegame_client_mods_dir.exists():
            for child in sorted(wegame_client_mods_dir.iterdir()):
                if (
                    child.is_dir()
                    and (child / "modinfo.lua").exists()
                    and child.name not in seen
                ):
                    seen.add(child.name)
                    ids.append(child.name)
        return ids

    workshop_dir = find_workshop_dir()
    if workshop_dir and workshop_dir.exists():
        for child in sorted(workshop_dir.iterdir()):
            if (
                child.is_dir()
                and is_workshop_content_id(child.name)
                and (child / "modinfo.lua").exists()
            ):
                wid = "workshop-" + child.name
                if wid not in seen:
                    seen.add(wid)
                    ids.append(wid)

    game_mods = steam_runtime_mods_dir or default_steam_mods_dir()
    if game_mods and game_mods.exists():
        for child in sorted(game_mods.iterdir()):
            if child.is_dir() and (child / "modinfo.lua").exists():
                wid = child.name
                if wid not in seen:
                    seen.add(wid)
                    ids.append(wid)

    if legacy_packages is None:
        from dstools.features.mod.legacy_v1 import find_legacy_packages

        legacy_packages = find_legacy_packages()
    for workshop_id in legacy_packages:
        wid = f"workshop-{workshop_id}"
        if wid not in seen:
            seen.add(wid)
            ids.append(wid)

    return ids


def find_workshop_content_dirs() -> dict[int, Path]:
    """返回 322330 下现存的标准 Workshop 数字目录。"""
    workshop_dir = find_workshop_dir()
    if workshop_dir is None or not workshop_dir.is_dir():
        return {}
    result = {}
    try:
        children = list(workshop_dir.iterdir())
    except OSError:
        return result
    for child in children:
        if child.is_dir() and is_workshop_content_id(child.name):
            result[int(child.name)] = child
    return result


def find_workshop_residual_dirs() -> dict[int, Path]:
    """返回缺少 ``modinfo.lua`` 的标准 Workshop 数字目录。"""
    return {
        workshop_id: path
        for workshop_id, path in find_workshop_content_dirs().items()
        if not (path / "modinfo.lua").is_file()
    }


# ── modinfo.lua 解析器 ───────────────────────────────────────────────


def _workshop_id_from_folder(mod_folder: Path) -> str:
    """文件夹名统一成 ``workshop-<id>``，与引擎注入 modinfo.lua 的 ``folder_name`` 一致。"""
    return (
        "workshop-" + mod_folder.name
        if not mod_folder.name.startswith("workshop-")
        else mod_folder.name
    )


def engine_folder_name(mod_folder: Path) -> str:
    """引擎注入 modinfo.lua 的 ``folder_name``：V2 工坊目录（纯数字）是 ``workshop-<id>``，
    其余（``workshop-<id>`` 或手动放进 mods 的本地 Mod，如 ``Chinese-Pro``）就是目录名本身。"""
    name = Path(mod_folder).name
    return f"workshop-{name}" if name.isascii() and name.isdigit() else name


def parse_modinfo(mod_folder: Path) -> ModInfo | None:
    """解析一个 Mod 目录的 modinfo.lua，无法解析返回 None。"""
    modinfo_path = mod_folder / "modinfo.lua"
    if not modinfo_path.exists():
        return None

    text = modinfo_path.read_text(encoding="utf-8", errors="replace")
    text = _strip_lua_comments(text)

    workshop_id = _workshop_id_from_folder(mod_folder)
    info = ModInfo(workshop_id=workshop_id)

    # 顶层字段只在 configuration_options 之前搜索：全文件搜索会把同名的选项字段
    # （如名为 "Language" 的选项的 name）误当成 Mod 名
    idx = text.find("configuration_options")
    header = text[:idx] if idx != -1 else text

    _extract_string(header, "name", info)
    _extract_string(header, "author", info)
    _extract_string(header, "version", info)
    _extract_string(header, "icon", info)
    _extract_string(header, "icon_atlas", info)
    _extract_description(header, info)

    def _flag(name: str) -> bool:
        fm = re.search(rf"\b{name}\s*=\s*(true|false)\b", header)
        return bool(fm) and fm.group(1) == "true"

    # server_only_mod / all_clients_require_mod 任一为真即按服务器 Mod 处理（见 ModInfo.client_only）
    if _flag("client_only_mod") and not (
        _flag("server_only_mod") or _flag("all_clients_require_mod")
    ):
        info.client_only = True

    # 解析 configuration_options 表
    config_opts = _extract_configuration_options(text)
    if config_opts is not None:
        info.config_options = config_opts

    if idx != -1:
        info.dynamic_preamble = header
        if not info.config_options and _has_nontrivial_table(text, idx):
            info.unsupported_schema = True

    return info


def _extract_quoted(text: str, key: str) -> str | None:
    """查找 ``key = 字面量``，遇到本地化写法时取中文：

    - 三元写法 ``key = IDENT and "A" or "B"``：取含汉字的一侧；
    - ``ChooseTranslationTable({...})`` 或同形状裸表（官方约定，见 _extract_localized_table）。

    只认紧跟 ``=`` 的简单形状，更复杂的表达式（拼接、循环）返回 None 而不是去猜。
    返回仍带 Lua 转义的原始字符串。
    """
    m = re.search(rf"\b{re.escape(key)}\s*=\s*\w+\s+and\s+(?:{_QUOTED_ALT})", text)
    if m:
        first = _pick_quoted(m)
        # 三元写法的中英顺序因 Mod 而异，不按条件变量名猜，直接取含汉字的一侧
        if not _contains_cjk(first):
            or_m = re.search(rf"\s+or\s+(?:{_QUOTED_ALT})", text[m.end() :])
            if or_m and _contains_cjk(_pick_quoted(or_m)):
                return _pick_quoted(or_m)
        return first
    # Island Adventures 等用 ``en_zh("English", "中文")``：只认两个参数都是字面量的形状，取中文
    m = re.search(
        rf"\b{re.escape(key)}\s*=\s*en_zh\s*\(\s*"
        rf"(?:{_QUOTED_ALT})\s*,\s*(?:{_QUOTED_ALT})\s*\)",
        text,
        re.DOTALL,
    )
    if m:
        return m.group(3) if m.group(3) is not None else m.group(4)
    # 同样的三元写法但用 [[...]] 长括号字符串，可跨行
    m = re.search(
        rf"\b{re.escape(key)}\s*=\s*\w+\s+and\s+\[\[(.*?)\]\]", text, re.DOTALL
    )
    if m:
        return m.group(1)
    m = re.search(rf"\b{re.escape(key)}\s*=\s*(?:{_QUOTED_ALT})", text)
    if m:
        return _pick_quoted(m)
    return _extract_localized_table(text, key)


def _extract_localized_table(text: str, key: str) -> str | None:
    """查找 ``ChooseTranslationTable({...})`` 或裸表：优先 ``["zh"]``，否则取第一个无键字符串
    （与官方 ``tbl[locale] or tbl[1]`` 一致）。返回仍带转义的原始字符串或 None。
    """
    m = re.search(
        rf"\b{re.escape(key)}\s*=\s*(?:ChooseTranslationTable\s*\(\s*)?(\{{)", text
    )
    if not m:
        return None
    brace_start = m.start(1)
    depth = 0
    end = None
    for i in range(brace_start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end is None:
        return None
    block = text[brace_start : end + 1]

    zm = re.search(rf'\[\s*(?:"zh"|\'zh\')\s*\]\s*=\s*(?:{_QUOTED_ALT})', block)
    if zm:
        return _pick_quoted(zm)

    # 没有 zh 条目时退回第一个无键字符串，即 tbl[1]
    for entry_m in re.finditer(rf"{_QUOTED_ALT}", block):
        preceding = block[: entry_m.start()]
        if re.search(r'\[\s*[\'"]?\w*[\'"]?\s*\]\s*=\s*$', preceding):
            continue  # 这个字符串是某个 `[key] = "..."` 条目的值
        return _pick_quoted(entry_m)
    return None


def _extract_label_or_hover(
    block: str, key: str, local_tables: dict | None
) -> str | None:
    """提取选项的 label/hover；值是本地表的单层点号引用（如 ``configs.language``）时先解引用。"""
    val = _extract_quoted(block, key)
    if val is not None:
        return val
    raw = _extract_field_raw(block, key)
    if raw is None or "." not in raw:
        return None
    resolved = _resolve_dotted_ref(raw, local_tables)
    if resolved is None:
        return None
    # 包装成一句赋值后复用 _extract_quoted 的全部提取规则
    return _extract_quoted(f"__resolved__ = {resolved}", "__resolved__")


def _extract_string(text: str, key: str, info: ModInfo):
    """提取一个简单的字符串字段，比如 name = \"...\" 或 author = \"...\"。"""
    quoted = _extract_quoted(text, key)
    if quoted is not None:
        setattr(info, key, _unescape_lua_string(quoted).strip())
        return
    # 匹配：key = 'value' 或 key = [[value]]
    patterns = [
        rf"{key}\s*=\s*\'([^\']*)\'",
        rf"{key}\s*=\s*\[\[(.*?)\]\]",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.DOTALL)
        if m:
            setattr(info, key, _unescape_lua_string(m.group(1)).strip())
            return


def _extract_description(text: str, info: ModInfo):
    """提取 description，可能是拼接起来的字符串。"""
    localized = _extract_quoted(text, "description")
    if localized is not None:
        info.description = _unescape_lua_string(localized).strip()
        return
    # 先试简单的带引号形式
    for pat in [rf'description\s*=\s*"({_QSTR})"', r"description\s*=\s*'([^']*)'"]:
        m = re.search(pat, text, re.DOTALL)
        if m:
            info.description = _unescape_lua_string(m.group(1)).strip()
            return

    # 再试 [[...]] 多行形式
    m = re.search(r"description\s*=\s*\[\[(.*?)\]\]", text, re.DOTALL)
    if m:
        info.description = m.group(1).strip()


def _extract_configuration_options(text: str) -> list[ModConfigOption] | None:
    """从 modinfo.lua 文本中提取并解析 ``configuration_options = {...}``。"""
    # 查找 configuration_options = {
    idx = text.find("configuration_options")
    if idx == -1:
        return None

    # 找开花括号
    brace_start = text.find("{", idx)
    if brace_start == -1:
        return None

    # 找匹配的闭花括号（计数深度）
    depth = 0
    brace_end = brace_start
    for i in range(brace_start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                brace_end = i
                break

    if depth != 0:
        return None  # 花括号不匹配

    table_text = text[brace_start : brace_end + 1]
    local_functions = _find_local_functions(text)
    local_tables = _find_local_tables(text)

    # 从这张表里解析出各条选项
    return _parse_options_table(table_text, local_functions, local_tables)


def _has_nontrivial_table(text: str, idx: int) -> bool:
    """``configuration_options`` 的表里是否有实际内容，用于区分"零个选项"和"形状不认识"。"""
    brace_start = text.find("{", idx)
    if brace_start == -1:
        return False
    depth = 0
    for i in range(brace_start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return len(text[brace_start + 1 : i].strip()) > 10
    return False


def _find_local_tables(text: str) -> dict:
    """查找 ``local NAME = {...}`` 表定义（多个选项共享一份选项列表时用），返回 名字 -> 表文本。"""
    tables = {}
    for m in re.finditer(r"local\s+(\w+)\s*=\s*\n?\s*\{", text):
        name = m.group(1)
        brace_start = m.end() - 1
        depth = 0
        for i in range(brace_start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    tables[name] = text[brace_start : i + 1]
                    break
    return tables


def _resolve_dotted_ref(expr: str, local_tables: dict | None) -> str | None:
    """解析对本地表的单层 ``IDENT.FIELD`` 引用，返回该字段值的原始文本，否则 None。

    坑：``options = options.toggle`` 必须取到 toggle 那一条，当成整张 options 表会让
    所有选项解析成同一份列表。
    """
    m = re.match(r"^(\w+)\.(\w+)$", expr.strip())
    if not m or not local_tables:
        return None
    ident, field = m.groups()
    table_text = local_tables.get(ident)
    if table_text is None:
        return None
    return _extract_field_raw(table_text, field)


def _find_local_functions(text: str) -> dict:
    """查找 ``local function NAME(params) ... end``，返回 名字 -> (参数列表, 函数体)。

    很多 Mod 用 AddOption 之类的辅助函数批量生成选项，供 _inline_helper_call 展开。
    """
    functions = {}
    for m in re.finditer(r"local\s+function\s+(\w+)\s*\(([^)]*)\)", text):
        name = m.group(1)
        params = [p.strip() for p in m.group(2).split(",") if p.strip()]
        start = m.end()
        # 按关键字粗略计数找到匹配的 end，够应付 Mod 里短小的辅助函数，不是完整 Lua 解析
        depth = 1
        body_end = None
        for line_m in re.finditer(r".*\n?", text[start:]):
            line = line_m.group(0)
            if not line:
                break
            depth += len(re.findall(r"\b(?:if|for|while|function)\b", line))
            depth -= len(re.findall(r"\bend\b", line))
            if depth <= 0:
                body_end = start + line_m.end()
                break
        if body_end is None:
            continue
        functions[name] = (params, text[start:body_end])
    return functions


def _split_call_args(text: str, open_paren_idx: int):
    """从开括号下标起按顶层逗号切分调用参数，返回 (参数列表, 闭括号后的下标)。"""
    depth = 1  # 已经在调用自己的开圆括号内部了
    i = open_paren_idx + 1
    in_str = None
    current = []
    args = []
    while i < len(text):
        ch = text[i]
        if in_str:
            current.append(ch)
            if ch == "\\" and i + 1 < len(text):
                i += 1
                current.append(text[i])
            elif ch == in_str:
                in_str = None
        elif ch in ('"', "'"):
            in_str = ch
            current.append(ch)
        elif ch in "({":
            depth += 1
            current.append(ch)
        elif ch in ")}":
            depth -= 1
            if depth == 0 and ch == ")":
                tail = "".join(current).strip()
                if tail:
                    args.append(tail)
                return args, i + 1
            current.append(ch)
        elif ch == "," and depth == 1:
            args.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
        i += 1
    return None, None


def _inline_helper_call(name: str, args: list, local_functions: dict) -> str | None:
    """把 ``AddOption("key", "Label", ...)`` 这类调用展开成函数体产生的字面量表文本。

    参数不是纯字面量、函数未知或函数体化简不成单张表时返回 None，调用方跳过而不是猜。
    """
    if name not in local_functions:
        return None
    params, body = local_functions[name]
    if len(args) > len(params):
        return None

    subst = dict(zip(params, args))
    # 调用处没提供的形参（比如一个可选的尾部参数）按 Lua 隐式 `nil`
    # 处理。
    for param in params[len(args) :]:
        subst[param] = "nil"

    # 坑：参数名常与表字段键（name = ...）或字符串里的英文单词撞名——跳过紧跟 "=" 的位置
    # 和引号内容；分两遍先换成占位符，防止已替换进来的字面量再被当成另一个参数名。
    placeholders = {param: f"\x00{i}\x00" for i, param in enumerate(subst)}
    result = _replace_idents_outside_strings(body, placeholders)
    for param, placeholder in placeholders.items():
        result = result.replace(placeholder, subst[param])

    # 只化简一层字面量比较的 if/else（辅助函数选默认开关措辞的常见写法），其余放弃
    if_m = re.search(r"if\s+(.+?)\s+then\b(.*?)\belse\b(.*?)\bend\b", result, re.DOTALL)
    if if_m:
        cond, then_branch, else_branch = if_m.groups()
        cond_m = re.match(r"\s*(\S+)\s*(==|~=)\s*(\S+)\s*$", cond.strip())
        if not cond_m:
            return None
        lhs, op, rhs = cond_m.groups()
        lhs, rhs = lhs.strip("\"'"), rhs.strip("\"'")
        is_eq = lhs == rhs
        taken = then_branch if (is_eq == (op == "==")) else else_branch
        result = result[: if_m.start()] + taken + result[if_m.end() :]

    ret_m = re.search(r"return\s*(\{.*)", result, re.DOTALL)
    if not ret_m:
        return None
    brace_start = ret_m.start(1)
    depth = 0
    for i in range(brace_start, len(result)):
        if result[i] == "{":
            depth += 1
        elif result[i] == "}":
            depth -= 1
            if depth == 0:
                return result[brace_start : i + 1]
    return None


def _parse_options_table(
    table_text: str,
    local_functions: dict | None = None,
    local_tables: dict | None = None,
) -> list[ModConfigOption]:
    """解析 configuration_options 表的各条目，结果顺序与源文件一致。

    条目可以是字面量表 ``{name=..., label=..., hover=..., options={...}, default=...}``，
    也可以是本地辅助函数调用（由 _inline_helper_call 展开）。
    """
    local_functions = local_functions or {}
    options = []
    inner = table_text[1:-1]  # 去掉外层的 { }

    i = 0
    while i < len(inner):
        while i < len(inner) and inner[i] in " \t\n\r,":
            i += 1
        if i >= len(inner):
            break

        if inner[i] == "{":
            depth = 0
            start = i
            while i < len(inner):
                if inner[i] == "{":
                    depth += 1
                elif inner[i] == "}":
                    depth -= 1
                    if depth == 0:
                        block = inner[start : i + 1]
                        opt = _parse_single_option(block, local_tables)
                        if opt:
                            options.append(opt)
                        i += 1
                        break
                i += 1
        else:
            call_m = re.match(r"(\w+)\s*\(", inner[i:])
            if call_m:
                paren_idx = i + call_m.end() - 1
                args, after = _split_call_args(inner, paren_idx)
                if args is not None:
                    block = _inline_helper_call(call_m.group(1), args, local_functions)
                    if block:
                        opt = _parse_single_option(block, local_tables)
                        if opt:
                            if opt.is_header and not opt.label.strip() and args:
                                opt.label = _unescape_lua_string(args[0].strip("\"'"))
                            options.append(opt)
                    i = after
                    continue
            i += 1

    return options


def _parse_single_option(
    block: str, local_tables: dict | None = None
) -> ModConfigOption | None:
    """解析单个配置选项块。"""
    opt = ModConfigOption(name="")

    # 提取 name
    m = re.search(rf"name\s*=\s*(?:{_QUOTED_ALT})", block)
    if not m:
        return None  # 没有 name 的选项是标题/分隔符，跳过
    opt.name = _unescape_lua_string(_pick_quoted(m))

    # 提取 label
    label = _extract_label_or_hover(block, "label", local_tables)
    if label is not None:
        opt.label = _unescape_lua_string(label)

    # 提取 hover
    m = re.search(r"hover\s*=\s*\[\[(.*?)\]\]", block, re.DOTALL)
    if m:
        opt.hover = m.group(1).strip()
    else:
        hover = _extract_label_or_hover(block, "hover", local_tables)
        if hover is not None:
            opt.hover = _unescape_lua_string(hover)

    # default 可能是表（如 {["1"] = 8}），要按括号/引号深度提取，不能截到第一个逗号
    default_raw = _extract_field_raw(block, "default")
    if default_raw is not None:
        opt.default = _coerce_lua_value(default_raw)

    opt.client = bool(re.search(r"\bclient\s*=\s*true\b", block))
    opt.is_set_config = bool(re.search(r"\bis_set_config\s*=\s*true\b", block))
    opt.is_array_config = bool(re.search(r"\bis_array_config\s*=\s*true\b", block))
    opt.is_text_config = bool(re.search(r"\bis_text_config\s*=\s*true\b", block))
    opt.is_dictionary_config = bool(
        re.search(r"\bis_dictionary_config\s*=\s*true\b", block)
    )

    opt.choices = _extract_choices(block, local_tables)

    # 标题/分隔条目的两种信号：name 为空（无法存回 modoverrides.lua），
    # 或只有一个空描述选项（如自定义 AddTitle 返回 name="null" 的写法）
    if opt.name == "" or (
        len(opt.choices) == 1 and opt.choices[0].get("description") == ""
    ):
        opt.is_header = True
    elif not opt.choices and re.search(r"\boptions\s*=", block):
    # 声明了 options 却解析为空：是函数调用或循环拼出的变量，需执行 Lua 才能得到（见沙箱解析）
        opt.is_dynamic = True
        opt.raw_options_expr = _extract_field_raw(block, "options") or ""

    return opt


def _extract_field_raw(block: str, key: str) -> str | None:
    """在 Lua 表块中查找 ``key = <value>``，返回值的原始文本。

    按括号/引号深度找到字段结束位置：值本身可能是表或多参数函数调用，含逗号和括号。
    """
    m = re.search(rf"\b{re.escape(key)}\s*=\s*", block)
    if m is None:
        return None
    i = m.end()
    n = len(block)
    start = i
    depth = 0
    in_str = None
    while i < n:
        ch = block[i]
        if in_str:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == in_str:
                in_str = None
            i += 1
            continue
        if ch in ('"', "'"):
            in_str = ch
        elif ch in "{[(":
            depth += 1
        elif ch in "}])":
            if depth == 0:
                break  # 外层块自己的闭括号
            depth -= 1
        elif ch == "," and depth == 0:
            break
        i += 1
    return block[start:i].strip()


def _extract_choices(block: str, local_tables: dict | None = None) -> list[dict]:
    """提取一个配置项的 options 列表。

    支持字面量表、整体共享的局部变量（``options = color_options``）和按名索引的共享字典
    （``options = options.toggle``，必须取到对应字段而不是整张表）。
    """
    # 锚定紧跟 "=" 的 options 字段，避免匹配到 hover 文案里的 "options" 单词
    field_m = re.search(r"\boptions\s*=", block)
    if not field_m:
        return []

    # 找出 "options" 被赋值成了什么：一个字面量 "{"，或者对某个本地表
    # 变量的裸标识符/`identifier.field` 引用。
    m = re.match(r"\s*(\{)|\s*(\w+(?:\.\w+)?)", block[field_m.end() :])
    if not m:
        return []

    if m.group(1):
        brace_start = field_m.end() + m.start(1)
        depth = 0
        brace_end = brace_start
        for i in range(brace_start, len(block)):
            if block[i] == "{":
                depth += 1
            elif block[i] == "}":
                depth -= 1
                if depth == 0:
                    brace_end = i
                    break
        options_text = block[brace_start : brace_end + 1]
    else:
        ref = m.group(2)
        if "." in ref:
            options_text = _resolve_dotted_ref(ref, local_tables)
        else:
            options_text = local_tables.get(ref) if local_tables else None
        if options_text is None:
            return []

    # 按括号深度逐条解析，单条选项的 data 可能是含逗号的嵌套表
    choices = []
    inner = options_text[1:-1] if len(options_text) >= 2 else ""
    i = 0
    n = len(inner)
    while i < n:
        while i < n and inner[i] in " \t\r\n,":
            i += 1
        if i >= n or inner[i] != "{":
            i += 1
            continue
        depth = 0
        start = i
        while i < n:
            if inner[i] == "{":
                depth += 1
            elif inner[i] == "}":
                depth -= 1
                if depth == 0:
                    i += 1
                    break
            i += 1
        choice_block = inner[start:i]

        desc_raw = _extract_quoted(choice_block, "description")
        if desc_raw is None:
            continue
        desc = _unescape_lua_string(desc_raw)
        data_raw = _extract_field_raw(choice_block, "data")
        data = _coerce_lua_value(data_raw) if data_raw is not None else None
        hover_raw = _extract_quoted(choice_block, "hover")
        hover = _unescape_lua_string(hover_raw) if hover_raw is not None else ""
        choices.append({"description": desc, "data": data, "hover": hover})

    return choices


def _coerce_lua_value(val_str: str) -> Any:
    """用真正的 Lua 解析器把字面量转成 Python 值，保证与 load_mod_overrides() 读出的形状一致可比较。"""
    val_str = val_str.strip()
    try:
        return parse_lua_value(val_str)
    except Exception:
        return val_str


# ── 配置值解析 ────────────────────────────────────────────────────────


def resolve_config_value(mod_info: ModInfo, key: str, current_value: Any) -> tuple:
    """返回 (可选项列表, 当前显示值, 是否有效)；可选项为 {"description", "data"} 字典列表。"""
    for opt in mod_info.config_options:
        if opt.name == key:
            choices = opt.choices
            # 找出哪个选项匹配当前值
            current_display = str(current_value)
            for c in choices:
                if c["data"] == current_value:
                    current_display = c["description"]
                    break
            return choices, current_display, True

    # modinfo 里没有这个配置键——自由形式的值
    return [], str(current_value), False


# ── 整份文件的 Lua 沙箱解析 ───────────────────────────────────────────
# 配置弹窗按需调用，优先于上面的静态解析。输入是已执行完的 Python 值，
# 与静态解析形状不同，少量逻辑（标题识别、本地化取值）分开实现。


def _resolve_localized_value(val: Any) -> str:
    """从已执行的 label/hover/description 值取显示文本：字符串直接用；本地化表
    （JSON 往返后形如 {"1": "English", "zh": "中文"}）优先取中文，否则取第一个元素。不抛异常。"""
    if val is None:
        return ""
    if isinstance(val, str):
        return val
    if isinstance(val, dict):
        zh = val.get("zh")
        if isinstance(zh, str) and zh:
            return zh
        one = val.get("1")
        if isinstance(one, str):
            return one
        for v in val.values():
            if isinstance(v, str) and v:
                return v
        return ""
    return str(val)


def _choices_from_lua_value(val: Any) -> list[dict]:
    """把已执行的 options 值转成 _extract_choices 的形状。

    支持普通的选项数组和以 data 值为键的写法（Insight：``{[false] = {...}}``）；后者 JSON
    往返后键变成字符串，需用 _coerce_lua_value 还原类型才能与存档值比较。
    """
    if isinstance(val, list):
        choices = []
        for item in val:
            if not isinstance(item, dict) or "description" not in item:
                continue
            choices.append(
                {
                    "description": _resolve_localized_value(item.get("description")),
                    "data": item.get("data", item.get("description")),
                    "hover": _resolve_localized_value(item.get("hover")),
                }
            )
        return choices
    if isinstance(val, dict):
        choices = []
        for key, item in val.items():
            if not isinstance(item, dict):
                continue
            choices.append(
                {
                    "description": _resolve_localized_value(item.get("description")),
                    "data": _coerce_lua_value(key),
                    "hover": _resolve_localized_value(item.get("hover")),
                }
            )
        return choices
    return []


def _options_from_lua_result(result: Any) -> list[ModConfigOption] | None:
    """把已执行的 configuration_options 转成 ModConfigOption 列表。

    支持标准数组和以选项名为键的表（Insight）；两种都不像时返回 None，调用方保留静态解析结果。
    """
    entries: list[tuple[Any, dict]] = []
    if isinstance(result, list):
        entries = [(None, item) for item in result if isinstance(item, dict)]
    elif isinstance(result, dict):
        entries = [
            (key, item) for key, item in result.items() if isinstance(item, dict)
        ]
    else:
        return None
    if not entries:
        return None

    options = []
    for key, d in entries:
        opt = ModConfigOption()
        name = d.get("name")
        opt.name = (
            str(name)
            if name not in (None, "")
            else (str(key) if key is not None else "")
        )
        opt.label = _resolve_localized_value(d.get("label"))
        opt.hover = _resolve_localized_value(d.get("hover"))
        if "default" in d:
            opt.default = d["default"]
        opt.client = bool(d.get("client"))
        opt.is_set_config = bool(d.get("is_set_config"))
        opt.is_array_config = bool(d.get("is_array_config"))
        opt.is_text_config = bool(d.get("is_text_config"))
        opt.is_dictionary_config = bool(d.get("is_dictionary_config"))
        opt.choices = _choices_from_lua_value(d.get("options"))

        # 与 _parse_single_option 相同的两个标题信号
        if opt.name == "" or (
            len(opt.choices) == 1 and opt.choices[0].get("description") == ""
        ):
            opt.is_header = True
        elif not opt.choices and "options" in d:
            opt.is_dynamic = True

        options.append(opt)
    return options


def resolve_full_modinfo(mod_folder: Path, timeout: float | None = None) -> dict | None:
    """把整份 modinfo.lua 放进 Lua 沙箱执行，解析元数据和 configuration_options。

    只在打开配置弹窗时按需调用，不用于批量扫描。能处理静态解析覆盖不到的写法
    （条件赋值的中文名、共享表、ChooseTranslationTable 等）；引用了沙箱没有的引擎
    全局变量时会快速失败。

    返回已本地化的字段（name/author/version/version_compatible/description/icon/
    icon_atlas，仅包含 Mod 实际设置的）及可识别时的 "config_options"；读取或执行
    失败返回 None。
    """
    modinfo_path = mod_folder / "modinfo.lua"
    if not modinfo_path.exists():
        return None
    try:
        text = modinfo_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    text = _strip_lua_comments(text)

    from dstools.features.mod.sandbox import (
        FULL_FILE_TIMEOUT,
        resolve_full_config_options,
    )

    folder_name = engine_folder_name(mod_folder)
    result = resolve_full_config_options(
        text, timeout=timeout or FULL_FILE_TIMEOUT, folder_name=folder_name
    )
    if not isinstance(result, dict):
        return None

    out = {}
    for key in (
        "name",
        "author",
        "version",
        "version_compatible",
        "description",
        "icon",
        "icon_atlas",
    ):
        val = result.get(key)
        if val is not None:
            out[key] = val if isinstance(val, str) else _resolve_localized_value(val)
    options = _options_from_lua_result(result.get("configuration_options"))
    if options is not None:
        out["config_options"] = options
    return out or None
