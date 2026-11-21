"""Mod 文本里的手柄按键字形。

游戏字体在私用区（U+E000 起）放了一套手柄按键图标（A/B/X/Y、LB/RB、十字键、摇杆等），
贴图与坐标在 ``data/databundles/fonts.zip`` 的 ``fonts/controllers.zip``（``font.tex`` + BMFont
格式的 ``font.fnt``）。Mod 常把这些字符直接写进配置标题和说明（如工坊 3293693889），
游戏里显示为按键图标，普通字体则显示成乱码。

这里从本机游戏文件把每个字形裁成 PNG 缓存起来，界面按需嵌入；取不到时退回文字名称（如 ``[LB]``）。
"""

import logging
import re
import zipfile
from io import BytesIO
from pathlib import Path

from PIL import Image

from dstools.shared.resource_paths import cache_dir
from dstools.shared.tex_convert import tex_to_png

_logger = logging.getLogger(__name__)

_CACHE_DIR = cache_dir("controller_glyphs")
# 记录缓存来自哪份 fonts.zip（路径、大小、修改时间），游戏更新后重新提取
_STAMP_NAME = "source.txt"
_FONT_MEMBER = "fonts/controllers.zip"
# 私用区里游戏实际用到的范围（各手柄布局分段排在 U+E000～U+E5FF）
_PUA_RE = re.compile("[-]")
_CHAR_RE = re.compile(r'<char\s+id="(\d+)"\s+x="(\d+)"\s+y="(\d+)"\s+width="(\d+)"\s+height="(\d+)"')

# 默认布局（U+E000 段）的按键记号，取不到字形时代替显示；与语言无关（同手柄上印的字），不进 strings.py。
# 顺序按游戏贴图实际画的内容（已转出贴图核对）：L/R 是摇杆按下，LS/RS 加箭头是摇杆方向，DPad 是十字键
FALLBACK_NAMES = {
    0xE000: "A", 0xE001: "B", 0xE002: "X", 0xE003: "Y",
    0xE004: "Start", 0xE005: "Back",
    0xE006: "L", 0xE007: "LB", 0xE008: "LT",
    0xE009: "R", 0xE00A: "RB", 0xE00B: "RT",
    0xE00C: "DPad↓", 0xE00D: "DPad←", 0xE00E: "DPad→", 0xE00F: "DPad↑",
    0xE010: "LS↑", 0xE011: "LS↓", 0xE012: "LS←", 0xE013: "LS→",
    0xE014: "RS↑", 0xE015: "RS↓", 0xE016: "RS←", 0xE017: "RS→",
    0xE018: "RS", 0xE019: "DPad",
}

# 本次运行里 prepare() 的结果；None 表示还没准备过
_prepared: dict[int, Path] | None = None


def has_glyphs(text: str) -> bool:
    """文本里是否含有游戏手柄按键字符。"""
    return bool(text) and _PUA_RE.search(text) is not None


def split_glyphs(text: str) -> list[str | int]:
    """把文本拆成普通文字片段（str）与按键字符码位（int），顺序不变。"""
    parts: list[str | int] = []
    last = 0
    for m in _PUA_RE.finditer(text or ""):
        if m.start() > last:
            parts.append(text[last:m.start()])
        parts.append(ord(m.group()))
        last = m.end()
    if last < len(text or ""):
        parts.append(text[last:])
    return parts


def fallback_name(codepoint: int) -> str:
    """取不到字形时代替显示的文字，如 ``[LB]``；不认识的按键显示 ``[?]``。"""
    return f"[{FALLBACK_NAMES.get(codepoint, '?')}]"


def to_plain(text: str) -> str:
    """把按键字符换成文字名称，给下拉项等只能显示纯文本的地方和搜索用。"""
    if not has_glyphs(text):
        return text
    return _PUA_RE.sub(lambda m: fallback_name(ord(m.group())), text)


def _fonts_zip() -> Path | None:
    # 延迟导入：查找游戏目录会涉及开服程序与 Steam 库扫描
    from dstools.features.save_browser.character_icons import _find_official_install_dir
    install_dir = _find_official_install_dir()
    if install_dir is None:
        return None
    path = install_dir / "data" / "databundles" / "fonts.zip"
    return path if path.is_file() else None


def _stamp_of(path: Path) -> str:
    st = path.stat()
    return f"{path}|{st.st_size}|{int(st.st_mtime)}"


def _read_cache() -> dict[int, Path]:
    result: dict[int, Path] = {}
    if not (_CACHE_DIR / _STAMP_NAME).is_file():
        return result
    for png in _CACHE_DIR.glob("*.png"):
        try:
            result[int(png.stem, 16)] = png
        except ValueError:
            continue
    return result


def cached() -> dict[int, Path]:
    """已提取好的字形 {码位: PNG 路径}，只读磁盘缓存、不跑转换，可在界面线程调用。"""
    if _prepared is not None:
        return _prepared
    return _read_cache()


def prepare() -> dict[int, Path]:
    """确保字形已从本机游戏文件提取（耗时：解压 + ktech 转换，放后台线程调用）。
    游戏文件更新后重新提取；取不到返回空字典，调用方退回文字名称。"""
    global _prepared
    if _prepared is not None:
        return _prepared
    fonts_zip = _fonts_zip()
    if fonts_zip is None:
        _prepared = _read_cache()
        return _prepared
    stamp = _stamp_of(fonts_zip)
    stamp_path = _CACHE_DIR / _STAMP_NAME
    try:
        if stamp_path.is_file() and stamp_path.read_text(encoding="utf-8") == stamp:
            _prepared = _read_cache()
            return _prepared
    except OSError:
        pass
    try:
        _prepared = _extract(fonts_zip, stamp)
    except Exception as exc:  # 游戏文件结构变化或转换失败：按"没有字形"处理
        _logger.warning("提取手柄按键字形失败：%s", exc)
        _prepared = _read_cache()
    return _prepared


def _extract(fonts_zip: Path, stamp: str) -> dict[int, Path]:
    with zipfile.ZipFile(fonts_zip) as outer:
        inner_bytes = outer.read(_FONT_MEMBER)
    with zipfile.ZipFile(BytesIO(inner_bytes)) as inner:
        fnt_text = inner.read("font.fnt").decode("utf-8", errors="replace")
        tex_bytes = inner.read("font.tex")

    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    raw_tex = _CACHE_DIR / "_font.tex"
    raw_png = _CACHE_DIR / "_font.png"
    raw_tex.write_bytes(tex_bytes)
    try:
        if not tex_to_png(raw_tex, raw_png):
            return _read_cache()
        with Image.open(raw_png) as img:
            sheet = img.convert("RGBA")
    finally:
        raw_tex.unlink(missing_ok=True)
        raw_png.unlink(missing_ok=True)

    # 清掉旧字形（含各尺寸的缩放副本），再按 .fnt 坐标逐个裁出
    for old in _CACHE_DIR.rglob("*.png"):
        old.unlink(missing_ok=True)
    result: dict[int, Path] = {}
    for m in _CHAR_RE.finditer(fnt_text):
        cid, x, y, w, h = map(int, m.groups())
        if not (0xE000 <= cid <= 0xE5FF) or w <= 0 or h <= 0:
            continue
        path = _CACHE_DIR / f"{cid:04x}.png"
        sheet.crop((x, y, x + w, y + h)).save(path)
        result[cid] = path
    if result:
        (_CACHE_DIR / _STAMP_NAME).write_text(stamp, encoding="utf-8")
    return result


def image_size(path: Path) -> tuple[int, int] | None:
    """PNG 的像素尺寸（只读文件头）；读不了返回 None。"""
    try:
        with Image.open(path) as img:
            return img.size
    except (OSError, ValueError):
        return None


def scaled_path(codepoint: int, height_px: int) -> Path | None:
    """按目标物理像素高度缩放后的字形 PNG（缩放结果缓存在 h<高度>/ 子目录）；没有该字形返回 None。
    字形贴图都按同一字号绘制，统一按 52 像素基准缩放，保持各按键之间的相对大小。"""
    source = cached().get(codepoint)
    if source is None or height_px <= 0:
        return None
    target = _CACHE_DIR / f"h{height_px}" / source.name
    if target.is_file():
        return target
    try:
        with Image.open(source) as img:
            glyph = img.convert("RGBA")
        scale = height_px / 52
        size = (max(1, round(glyph.width * scale)), max(1, round(glyph.height * scale)))
        target.parent.mkdir(parents=True, exist_ok=True)
        glyph.resize(size, Image.Resampling.LANCZOS).save(target)
    except (OSError, ValueError):
        return None
    return target
