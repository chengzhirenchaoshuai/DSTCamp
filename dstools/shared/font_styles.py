"""字体样式注册表（纯数据）。

新增样式需同时添加字体文件与许可证（tools/fonts/）、``FONT_STYLES`` 条目和 i18n 文案
``settings.font_style_<key>``。``family`` 必须是字体文件真实的族名，写错只会静默回退到系统字体。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class FontStyleDef:
    key: str  # 设置里保存的值，也是文案 key 的后缀；字体设置弹窗按列表顺序显示
    family: str  # 字体族名（须真机核对）
    filename: str | None  # tools/fonts/ 下的文件名，None 表示用系统自带字体
    scale: float = 1.0  # 字号整体缩放倍数


FONT_STYLES: list[FontStyleDef] = [
    # 常规粗细：原来用 Light 细体，1080P/24 寸这类约 92 DPI 的屏上笔画发虚、看着费劲（用户反馈）
    FontStyleDef(key="default", family="Microsoft YaHei UI", filename=None),
    FontStyleDef(key="cute", family="KN Maiyuan", filename="KNMaiyuan-Regular.ttf"),
    # Fusion Pixel Font 简体中文版（TakWolf/fusion-pixel-font，SIL OFL 1.1），已核对覆盖文案用到的全部汉字。
    # 只用 12px 版本（8/10px 字形风格不同，混用不统一）；同磅值下字形尺寸与雅黑基本一致，不缩放。
    FontStyleDef(key="pixel", family="Fusion Pixel 12px Prop zh_hans",
                 filename="fusion-pixel-12px-proportional-zh_hans.ttf"),
]

FONT_STYLE_NAMES: list[str] = [d.key for d in FONT_STYLES]
FONT_FAMILY_BY_STYLE: dict[str, str] = {d.key: d.family for d in FONT_STYLES}
FONT_SIZE_SCALE_BY_STYLE: dict[str, float] = {d.key: d.scale for d in FONT_STYLES}
