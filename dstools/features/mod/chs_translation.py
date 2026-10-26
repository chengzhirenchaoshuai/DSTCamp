"""借用汉化 Mod "Chinese++ Pro"（workshop-2941527805）的翻译，给 Mod 名称、简介和配置项叠加中文。

仅在用户订阅了该 Mod 且其中有目标 Mod 的翻译文件时生效，只改显示文字，不碰写入的值。
翻译文件位于 scripts/info_chs/workshop-<id>.lua，合并算法照抄其 main/mod_chs.lua。
约六成翻译文件含 Lua 逻辑，须用 sandbox.py 执行，解析不出就不叠加，不猜。
约四成翻译文件在原名/原简介上做替换或拼接（如 ``name = temp_modname.."(永不妥协)"``），
它们都留有给开服工具的 ``if gz_start then`` 分支，这里按该约定注入 gz_* 全局传入原始信息。
"""

import hashlib
import json
from pathlib import Path

from dstools.features.mod.parser import ModConfigOption, find_mod_folder
from dstools.features.mod.sandbox import run_lua_snippet
from dstools.models import Platform
from dstools.shared.lua_parser import _lua_value
from dstools.shared.resource_paths import cache_dir

CHS_PRO_WORKSHOP_ID = "workshop-2941527805"

_CACHE_DIR = cache_dir("mod_chs_translation")
_TIMEOUT = 3.0
# 缓存协议：v2 起结果含 name/description 并注入 gz_*，旧缓存（纯列表）自然因缺 digest 失效
_CACHE_PROTOCOL = 2


def find_translation_file(workshop_id: str, platform: Platform = Platform.STEAM,
                          wegame_client_mods_dir: Path | None = None) -> Path | None:
    """查找 Chinese++ Pro 中目标 Mod 的翻译文件，未订阅或没有翻译返回 None。"""
    chs_folder = find_mod_folder(CHS_PRO_WORKSHOP_ID, platform, wegame_client_mods_dir)
    if not chs_folder:
        return None
    wid = workshop_id if workshop_id.startswith("workshop-") else f"workshop-{workshop_id}"
    path = chs_folder / "scripts" / "info_chs" / f"{wid}.lua"
    return path if path.exists() else None


def _gz_preamble(name: str, description: str, version: str) -> str:
    """按翻译文件里的 gz_* 约定传入原始 name/description/version。原始 configuration_options 是静态解析
    结果、已不是 Lua 原表，只给空表：用到它的 3 份文件只是据此补"[故障]"前缀，缺了不影响翻译本身。"""
    return (
        "gz_start = true\n"
        f"gz_modname = {_lua_value(name)}\n"
        f"gz_description = {_lua_value(description)}\n"
        f"gz_modversion = {_lua_value(version)}\n"
        "gz_configuration_options = {}\n"
    )


def _parse_result(result) -> dict | None:
    """校验沙箱返回值的形状，只保留非空字符串的 name/description 与列表形式的 configuration_options。"""
    if not isinstance(result, dict):
        return None
    parsed = {}
    for key in ("name", "description"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            parsed[key] = value
    options = result.get("configuration_options")
    if isinstance(options, list):
        parsed["configuration_options"] = options
    return parsed or None


def resolve_translation(translation_path: Path, name: str = "", description: str = "",
                        version: str = "") -> dict | None:
    """沙箱执行翻译文件，返回 ``{"name", "description", "configuration_options"}`` 中解析到的部分；
    解析不出返回 None。结果依赖原始信息，缓存按文件 mtime 与输入摘要校验。"""
    preamble = _gz_preamble(name, description, version)
    digest = hashlib.sha256(f"{_CACHE_PROTOCOL}\0{preamble}".encode("utf-8")).hexdigest()
    cache_file = _CACHE_DIR / f"{translation_path.stem}.json"
    if cache_file.exists() and cache_file.stat().st_mtime >= translation_path.stat().st_mtime:
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if isinstance(cached, dict) and cached.get("digest") == digest:
                return cached.get("result")
        except (OSError, ValueError):
            pass

    try:
        text = translation_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    parsed = _parse_result(run_lua_snippet(preamble + text, timeout=_TIMEOUT))
    if parsed is None:
        return None

    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({"digest": digest, "result": parsed}, ensure_ascii=False),
                              encoding="utf-8")
    except (OSError, TypeError, ValueError):
        pass
    return parsed


def apply_translation(config_options: list[ModConfigOption], translation: list[dict]) -> None:
    """把翻译叠加到 config_options（原地修改），算法同 main/mod_chs.lua：先按 name 精确匹配，再按 label
    匹配（需带 CH_label）；命中后覆盖 label/hover，choices 内按 data 值覆盖 description/hover。"""
    for opt in config_options:
        match = None
        for t in translation:
            if not isinstance(t, dict):
                continue
            if opt.name and t.get("name") == opt.name:
                match = t
                break
            if t.get("CH_label") and opt.label and t.get("label") == opt.label:
                match = t
                break
        if not match:
            continue
        opt.label = match.get("CH_label") or match.get("label") or opt.label
        opt.hover = match.get("hover") or opt.hover
        t_choices = match.get("options")
        if not isinstance(t_choices, list):
            continue
        for choice in opt.choices:
            for tc in t_choices:
                if not isinstance(tc, dict) or tc.get("data") != choice.get("data"):
                    continue
                if tc.get("description"):
                    choice["description"] = tc["description"]
                if tc.get("hover"):
                    choice["hover"] = tc["hover"]
                break


# 已解析出的中文名称（Mod ID → 名称）：Mod 页加载后在后台填充、界面线程整体替换，localize_mod_name 读取
_display_names: dict[str, str] = {}


def display_name(mod_id: str) -> str:
    """Chinese++ Pro 给出的 Mod 名称，没有翻译或尚未解析时返回空串。"""
    return _display_names.get(str(mod_id), "")


def set_display_names(names: dict[str, str]) -> bool:
    """整体替换名称表（只在界面线程调用），返回是否有变化；退订 Chinese++ Pro 后重新加载即清空。"""
    global _display_names
    if names == _display_names:
        return False
    _display_names = dict(names)
    return True


def resolve_display_names(mod_infos: dict, platform: Platform = Platform.STEAM,
                          wegame_client_mods_dir: Path | None = None) -> dict[str, str]:
    """后台批量解析已安装 Mod 的中文名称。有缓存时只读文件，首次每份翻译约一次沙箱子进程。"""
    from concurrent.futures import ThreadPoolExecutor

    chs_folder = find_mod_folder(CHS_PRO_WORKSHOP_ID, platform, wegame_client_mods_dir)
    if not chs_folder:
        return {}
    info_dir = chs_folder / "scripts" / "info_chs"
    targets = []
    for mod_id, info in mod_infos.items():
        if info is None or not str(mod_id).startswith("workshop-"):
            continue
        path = info_dir / f"{mod_id}.lua"
        if path.is_file():
            targets.append((mod_id, path, info))

    def resolve(target):
        mod_id, path, info = target
        result = resolve_translation(path, info.name, info.description, info.version)
        # 少数译名带换行（原名与中文分两行），列表只有一行，收成空格
        name = " ".join(str((result or {}).get("name", "")).split())
        return mod_id, name

    with ThreadPoolExecutor(max_workers=4) as pool:
        return {mod_id: name for mod_id, name in pool.map(resolve, targets) if name}
