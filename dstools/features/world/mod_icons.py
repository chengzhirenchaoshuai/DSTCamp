"""Mod 贡献的世界设置图标：从 Mod 自己的图集（.tex + .xml）按 UV 裁出。

一张贴图常含十几个设置图标，贴图转 PNG（调用 ktech，较慢）按 (mod_id, 贴图名) 缓存后复用。
图集/贴图文件名因 Mod 而异，新增支持前先核对其 AddCustomizeItem 的 image/atlas 赋值（见 mod_settings.MOD_ICON_ATLAS）。
"""

from pathlib import Path

from PIL import Image

from dstools.shared.atlas_utils import crop_by_uv, parse_atlas_xml
from dstools.shared.resource_paths import cache_dir
from dstools.shared.tex_convert import tex_to_png

_CACHE_DIR = cache_dir("world_mod_icons")


def get_mod_setting_icon_path(mod_folder: Path, mod_id: str, atlas_rel: str, tex_rel: str,
                               element_name: str) -> Path | None:
    """从 Mod 图集中裁出名为 element_name 的图标并返回缓存 PNG 路径；文件或元素不存在、转换失败返回 None。"""
    xml_path = mod_folder / atlas_rel
    tex_path = mod_folder / tex_rel
    if not xml_path.exists() or not tex_path.exists():
        return None

    mod_cache_dir = _CACHE_DIR / mod_id
    cache_path = mod_cache_dir / f"{element_name}.png"
    src_mtime = tex_path.stat().st_mtime
    if cache_path.exists() and cache_path.stat().st_mtime >= src_mtime:
        return cache_path

    # 按贴图文件名缓存转换结果供同一 Mod 的其他设置复用（中间产物保留，不像 mod/icons.py 那样删除）
    atlas_png = mod_cache_dir / f"_atlas_{tex_path.stem}.png"
    if not atlas_png.exists() or atlas_png.stat().st_mtime < src_mtime:
        mod_cache_dir.mkdir(parents=True, exist_ok=True)
        if not tex_to_png(tex_path, atlas_png):
            return None

    try:
        elements = parse_atlas_xml(xml_path.read_text(encoding="utf-8", errors="replace"))
        target = next((e for e in elements if e[0] == element_name), None)
        if not target:
            return None
        mod_cache_dir.mkdir(parents=True, exist_ok=True)
        with Image.open(atlas_png) as img:
            crop_by_uv(img.convert("RGBA"), target[1:]).save(cache_path)
    except Exception:
        return None
    return cache_path


def resolve_mod_setting_icons(mod_settings: dict, platform, wegame_client_mods_dir=None,
                              steam_runtime_mods_dir=None, known_folders: dict | None = None) -> dict:
    """按 mod_settings 中各条的 icon_element 裁出图标，返回 {key: PIL.Image(RGBA)}；
    失败的条目跳过（调用方显示占位）。同一 Mod 的目录只查找一次。

    ``known_folders`` 为 Mod 列表已解析好的目录（键为 ``workshop-<id>`` 或纯数字 ID），优先使用；
    其余按 find_mod_folder 查找，``steam_runtime_mods_dir`` 为专服实际读取的 mods 目录，与 Mod 页一致。
    """
    from dstools.features.mod.parser import find_mod_folder
    from dstools.features.world.icons import get_icon_path
    from dstools.features.world.mod_settings import MOD_ICON_ATLAS

    images: dict[str, Image.Image] = {}
    mod_folder_cache: dict[str, Path | None] = {}

    for key, info in mod_settings.items():
        if info.vanilla_icon_key:
            vanilla_path = get_icon_path(info.vanilla_icon_key)
            if vanilla_path:
                try:
                    images[key] = Image.open(vanilla_path).convert("RGBA")
                except OSError:
                    pass
            continue
        if not info.icon_element:
            continue
        atlas_info = info.icon_atlas or MOD_ICON_ATLAS.get(info.mod_id)
        if not atlas_info:
            continue
        if info.mod_id not in mod_folder_cache:
            known = known_folders or {}
            folder = known.get(f"workshop-{info.mod_id}") or known.get(info.mod_id)
            mod_folder_cache[info.mod_id] = Path(folder) if folder else find_mod_folder(
                f"workshop-{info.mod_id}", platform, wegame_client_mods_dir, steam_runtime_mods_dir)
        mod_folder = mod_folder_cache[info.mod_id]
        if not mod_folder:
            continue
        atlas_rel, tex_rel = atlas_info
        icon_path = get_mod_setting_icon_path(mod_folder, info.mod_id, atlas_rel, tex_rel,
                                               info.icon_element)
        if not icon_path:
            continue
        try:
            images[key] = Image.open(icon_path).convert("RGBA")
        except OSError:
            continue
    return images
