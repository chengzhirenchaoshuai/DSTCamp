"""游戏"保存预设"的自定义世界预设：<账号目录>/client_save/world_presets/CUSTOM_<名称大写>.wsp/.wgp。

格式见游戏脚本 custompresets.lua：每个预设按类别拆成两个文件，.wsp 存世界设置、.wgp 存世界生成，
内容为 DataDumper 输出的 ``{baseid, name, desc, overrides, playstyle, version}``；baseid、name、desc、
overrides 缺一项游戏就当无效。baseid 是官方或 Mod 预设 ID（不能是另一个自定义预设），游戏据它补齐
location 等其余字段，找不到（Mod 没开）时静默忽略。playstyle 游戏读到 nil 也能用，这里不计算。

游戏启动时（CustomPresetManager:Load）读一次文件列表，所以 DSTCamp 写入的预设要等游戏回到开始页面或
重启后才出现。文件经 TheSim:SetPersistentString 写入，不压缩时带 11 字节明文文件头。
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dstools.features.mod.shardindex import _PLAIN_HEADER, decode_persistent_bytes
from dstools.features.world.categories import get_setting_info
from dstools.features.world.location_profiles import ALL_MOD_LOCATIONS, CAVE_LOCATION, FOREST_LOCATION
from dstools.features.world.location_profiles import get_location_definition
from dstools.shared.lua_parser import parse_lua_table, serialize_lua_table

PRESET_PREFIX = "CUSTOM_"
SETTINGS_EXT = ".wsp"
WORLDGEN_EXT = ".wgp"
_VERSION = 1

# 游戏自带预设所属的世界类型（scripts/map/levels/forest.lua、caves.lua 中的 AddSettingsPreset/AddWorldGenLevel）
_OFFICIAL_BASE_LOCATIONS = {
    **dict.fromkeys(("SURVIVAL_TOGETHER", "SURVIVAL_TOGETHER_CLASSIC", "SURVIVAL_DEFAULT_PLUS", "RELAXED",
                     "ENDLESS", "WILDERNESS", "LIGHTS_OUT", "COMPLETE_DARKNESS", "TERRARIA", "MOD_MISSING"),
                    FOREST_LOCATION),
    **dict.fromkeys(("DST_CAVE", "DST_CAVE_PLUS", "TERRARIA_CAVE"), CAVE_LOCATION),
}
# 文件名里 Windows 不允许的字符；游戏照样拼文件名，写不出来就保存失败
_INVALID_NAME_CHARS = set('\\/:*?"<>|')


class UserPresetError(Exception):
    """预设目录或文件无法读写。"""


@dataclass
class UserPreset:
    """一个自定义预设；只有一个类别文件时另一类为空表（游戏同样允许只存世界设置或只存世界生成）。"""

    preset_id: str
    name: str
    description: str
    settings_base: str = ""
    worldgen_base: str = ""
    settings: dict = field(default_factory=dict)
    worldgen: dict = field(default_factory=dict)

    @property
    def base_id(self) -> str:
        return self.settings_base or self.worldgen_base

    @property
    def location(self) -> str:
        """按 baseid 推断世界类型；认不出（未登记的 Mod 预设）返回空串。"""
        return base_location(self.base_id)

    @property
    def overrides(self) -> dict:
        return {**self.worldgen, **self.settings}


def base_location(base_id: str) -> str:
    key = (base_id or "").upper()
    if key in _OFFICIAL_BASE_LOCATIONS:
        return _OFFICIAL_BASE_LOCATIONS[key]
    for location in ALL_MOD_LOCATIONS:
        if get_location_definition(location).default_preset_id.upper() == key:
            return location
    return ""


def presets_dir(user_dir: Path) -> Path:
    return Path(user_dir) / "client_save" / "world_presets"


def preset_id_for_name(name: str) -> str:
    """与游戏 NamePresetScreen:GetID 一致：CUSTOM_ 加名称大写。Lua 的 string.upper 只转 ASCII 字母，
    Python 的 upper() 会连非 ASCII 字母一起转（ä→Ä、ß→SS），所以只转 ASCII。"""
    return PRESET_PREFIX + "".join(c.upper() if c.isascii() else c for c in name)


def file_stem(preset_id: str) -> str:
    """预设 ID 对应的磁盘文件名（不含扩展名）。游戏把 UTF-8 字节逐个当 Latin-1 字符拼文件名：
    「测试」(E6 B5 8B E8 AF 95) 落盘为 U+00E6 U+00B5 U+008B…，读时再反解（2026-10-23 用户实测游戏写的文件）。
    按正常 Unicode 命名的文件游戏反解后对不上 ID，会被忽略。纯 ASCII 名称两种写法相同。"""
    return preset_id.encode("utf-8").decode("latin-1")


def _preset_id_from_stem(stem: str) -> str | None:
    """file_stem 的反解；不是游戏这种编码的文件名（游戏也读不到）返回 None。"""
    try:
        return stem.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return None


def validate_name(name: str) -> str:
    """返回去掉首尾空白的名称；空名称或含文件名非法字符时抛 ValueError。"""
    name = (name or "").strip()
    if not name:
        raise ValueError("预设名称不能为空")
    bad = sorted(_INVALID_NAME_CHARS & set(name))
    if bad:
        raise ValueError("预设名称不能包含：" + " ".join(bad))
    return name


def _read_file(path: Path) -> dict | None:
    """读一个类别文件；字段不全（游戏也当无效）时返回 None，读不了抛 UserPresetError。"""
    try:
        text, _ = decode_persistent_bytes(path.read_bytes())
        data = parse_lua_table(text, str(path))
    except Exception as exc:
        raise UserPresetError(f"无法读取 {path}：{exc}") from exc
    if not all(data.get(key) is not None for key in ("baseid", "name", "desc", "overrides")):
        return None
    if not isinstance(data["overrides"], dict):
        return None
    return data


def list_presets(user_dir: Path) -> tuple[list[UserPreset], list[str]]:
    """列出全部自定义预设（按 ID 排序），并返回读取失败的文件说明；目录不存在时为空。"""
    folder = presets_dir(user_dir)
    if not folder.is_dir():
        return [], []
    by_id: dict[str, UserPreset] = {}
    errors = []
    for path in sorted(folder.iterdir()):
        ext = path.suffix.lower()
        if ext not in (SETTINGS_EXT, WORLDGEN_EXT) or not path.is_file():
            continue
        preset_id = _preset_id_from_stem(path.stem)
        if preset_id is None or not preset_id.startswith(PRESET_PREFIX):
            continue
        try:
            data = _read_file(path)
        except UserPresetError as exc:
            errors.append(str(exc))
            continue
        if data is None:
            continue
        preset = by_id.setdefault(preset_id, UserPreset(preset_id, str(data["name"]), str(data["desc"])))
        if ext == SETTINGS_EXT:
            preset.settings_base, preset.settings = str(data["baseid"]), dict(data["overrides"])
            # 名称与说明以世界设置文件为准（两文件由游戏同时写入，通常相同）
            preset.name, preset.description = str(data["name"]), str(data["desc"])
        else:
            preset.worldgen_base, preset.worldgen = str(data["baseid"]), dict(data["overrides"])
    return list(by_id.values()), errors


def split_overrides(overrides: dict, location: str, mod_settings: dict | None = None) -> tuple[dict, dict, list[str]]:
    """按世界设置/世界生成拆分 overrides，返回 (设置, 生成, 未登记的 key)；未登记的 key 不知道归哪类，不写入。"""
    settings, worldgen, unknown = {}, {}, []
    for key, value in overrides.items():
        category, is_rule, _ = get_setting_info(key, location, mod_settings)
        if category == "other":
            unknown.append(key)
        elif is_rule:
            settings[key] = value
        else:
            worldgen[key] = value
    return settings, worldgen, sorted(unknown)


def applicable_overrides(overrides: dict, location: str, mod_settings: dict | None = None,
                         is_master_world: bool = True, active_mod_ids=None) -> tuple[dict, list[str]]:
    """套用预设时能改的取值与跳过的 key：当前世界不显示的 key 不套用，包括未登记的、从世界里仅主世界的
    原版项、被已启用 Mod 隐藏的原版项（如云霄国度限定到森林的四季，``active_mod_ids`` 见
    build_world_view_model），以及只读的 Mod 项。"""
    from dstools.features.world.mod_settings import get_mod_vanilla_world_patches
    from dstools.features.world.value_sets import is_mod_readonly

    hidden, _, _ = get_mod_vanilla_world_patches(mod_settings or {}, location, active_mod_ids)
    values, skipped = {}, []
    for key, value in overrides.items():
        category, _, _ = get_setting_info(key, location, mod_settings, is_master_world)
        if category == "other" or key in hidden or is_mod_readonly(key, mod_settings):
            skipped.append(key)
        else:
            values[key] = value
    return values, sorted(skipped)


def _write_file(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".dstcamp.tmp")
    try:
        tmp.write_bytes(_PLAIN_HEADER + serialize_lua_table(data).encode("utf-8"))
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def save_preset(user_dir: Path, name: str, description: str, settings_base: str, worldgen_base: str,
                settings: dict, worldgen: dict) -> UserPreset:
    """写入一对 .wsp/.wgp（同名覆盖，是否覆盖由调用方先确认）；返回写入的预设。"""
    name = validate_name(name)
    if not settings_base or not worldgen_base:
        raise ValueError("缺少基础预设 ID")
    if settings_base.upper().startswith(PRESET_PREFIX) or worldgen_base.upper().startswith(PRESET_PREFIX):
        raise ValueError("基础预设不能是自定义预设")
    preset_id = preset_id_for_name(name)
    folder = presets_dir(user_dir)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        for ext, base, overrides in ((SETTINGS_EXT, settings_base, settings), (WORLDGEN_EXT, worldgen_base, worldgen)):
            _write_file(folder / (file_stem(preset_id) + ext), {
                "baseid": base, "desc": description, "name": name, "overrides": dict(overrides),
                "version": _VERSION,
            })
    except OSError as exc:
        raise UserPresetError(f"无法写入预设：{exc}") from exc
    return UserPreset(preset_id, name, description, settings_base, worldgen_base, dict(settings), dict(worldgen))


def delete_preset(user_dir: Path, preset_id: str) -> None:
    folder = presets_dir(user_dir)
    try:
        for ext in (SETTINGS_EXT, WORLDGEN_EXT):
            path = folder / (file_stem(preset_id) + ext)
            if path.exists():
                path.unlink()
    except OSError as exc:
        raise UserPresetError(f"无法删除预设：{exc}") from exc


def resolve_base_ids(raw: dict, location: str, user_dir: Path | None = None) -> tuple[str, str]:
    """从 leveldataoverride 取 (设置, 生成) 的基础预设 ID。世界本身由自定义预设生成时，改取那个预设的 baseid；
    找不到时与游戏迁移旧预设的做法一致，退回该世界类型的默认预设。"""
    fallback = get_location_definition(location).default_preset_id if location else ""
    custom = {}
    result = []
    for key in ("settings_id", "worldgen_id"):
        base = str(raw.get(key) or raw.get("id") or "")
        if base.upper().startswith(PRESET_PREFIX):
            if user_dir is not None and not custom:
                custom = {p.preset_id.upper(): p for p in list_presets(user_dir)[0]}
            preset = custom.get(base.upper())
            base = (preset.settings_base if key == "settings_id" else preset.worldgen_base) if preset else ""
        result.append(base or fallback)
    return result[0], result[1]
