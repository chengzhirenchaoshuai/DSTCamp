"""各颜色主题的调色板（纯数据）。新增颜色键须全部主题同时添加（测试会校验键一致）。

ORNAMENT / ORNAMENT_ACCENT：窗口点缀外框描边的浅色段 / 左上角最浓处。
"""

THEMES = {
    "gray": {
        "PRIMARY": "#8A97A3", "PRIMARY_DARK": "#6B7A87", "PRIMARY_LIGHT": "#E7ECEF",
        "BG_SOFT": "#F4F6F7", "ACCENT": "#5C7A89", "TEXT": "#2E3438", "TEXT_MUTED": "#565E65",
        "CARD_BG": "#FDFEFE", "CARD_BG_ALT": "#EFF3F4", "CARD_BORDER": "#D6DEE1",
        "SHADOW": "#C9D3D6", "ERROR": "#c62828", "SUCCESS": "#2E7D32", "HEADING": "#33393D",
        "BANNER_BG": "#fff3cd", "BANNER_TEXT": "#856404",
        "ORNAMENT": "#B9C3CA", "ORNAMENT_ACCENT": "#5C7A89",
        "FONT_SIZE_XL": 18, "FONT_SIZE_LG": 15, "FONT_SIZE_MD": 12,
        "FONT_SIZE_BASE": 11, "FONT_SIZE_SM": 10, "FONT_SIZE_XS": 9,
    },
    "mint": {
        "PRIMARY": "#6FCF97", "PRIMARY_DARK": "#57BF84", "PRIMARY_LIGHT": "#D7F5E4",
        "BG_SOFT": "#E8F8F0", "ACCENT": "#2D9CDB", "TEXT": "#2F3E46", "TEXT_MUTED": "#525F63",
        "CARD_BG": "#FFFFFF", "CARD_BG_ALT": "#F4FBF7", "CARD_BORDER": "#CFEEDD",
        "SHADOW": "#C9E4D8", "ERROR": "#c62828", "SUCCESS": "#2E7D32", "HEADING": "#37474f",
        "BANNER_BG": "#fff3cd", "BANNER_TEXT": "#856404",
        "ORNAMENT": "#9BDDB8", "ORNAMENT_ACCENT": "#3FAE74",
        "FONT_SIZE_XL": 18, "FONT_SIZE_LG": 15, "FONT_SIZE_MD": 12,
        "FONT_SIZE_BASE": 11, "FONT_SIZE_SM": 10, "FONT_SIZE_XS": 9,
    },
    "sky": {
        "PRIMARY": "#4FB0E8", "PRIMARY_DARK": "#3596D1", "PRIMARY_LIGHT": "#D6EEFB",
        "BG_SOFT": "#EBF7FD", "ACCENT": "#1E7FC2", "TEXT": "#24384A", "TEXT_MUTED": "#4D606B",
        "CARD_BG": "#FFFFFF", "CARD_BG_ALT": "#F3FAFE", "CARD_BORDER": "#CBE7F7",
        "SHADOW": "#BFDDEE", "ERROR": "#c62828", "SUCCESS": "#2E7D32", "HEADING": "#2C4A63",
        "BANNER_BG": "#fff3cd", "BANNER_TEXT": "#856404",
        "ORNAMENT": "#A9DCF5", "ORNAMENT_ACCENT": "#3A9BD9",
        "FONT_SIZE_XL": 18, "FONT_SIZE_LG": 15, "FONT_SIZE_MD": 12,
        "FONT_SIZE_BASE": 11, "FONT_SIZE_SM": 10, "FONT_SIZE_XS": 9,
    },
    "twilight": {
        "PRIMARY": "#3E5BA8", "PRIMARY_DARK": "#2C437F", "PRIMARY_LIGHT": "#DCE2F3",
        "BG_SOFT": "#EDF0F8", "ACCENT": "#6C5FD3", "TEXT": "#232B40", "TEXT_MUTED": "#535A6B",
        "CARD_BG": "#FFFFFF", "CARD_BG_ALT": "#F4F6FB", "CARD_BORDER": "#D3DAEE",
        "SHADOW": "#C3CCE4", "ERROR": "#c62828", "SUCCESS": "#2E7D32", "HEADING": "#26335A",
        "BANNER_BG": "#fdecc8", "BANNER_TEXT": "#7a5a12",
        "ORNAMENT": "#8FA3D6", "ORNAMENT_ACCENT": "#23356E",
        "FONT_SIZE_XL": 18, "FONT_SIZE_LG": 15, "FONT_SIZE_MD": 12,
        "FONT_SIZE_BASE": 11, "FONT_SIZE_SM": 10, "FONT_SIZE_XS": 9,
    },
    "campfire": {
        "PRIMARY": "#E8A33D", "PRIMARY_DARK": "#C9822A", "PRIMARY_LIGHT": "#FBE7C6",
        "BG_SOFT": "#FBF2E3", "ACCENT": "#D9534F", "TEXT": "#4A3728", "TEXT_MUTED": "#685A4A",
        "CARD_BG": "#FFFFFF", "CARD_BG_ALT": "#FDF6EC", "CARD_BORDER": "#F0DBB4",
        "SHADOW": "#E8D2A0", "ERROR": "#c62828", "SUCCESS": "#2E7D32", "HEADING": "#6B4A28",
        "BANNER_BG": "#fde3df", "BANNER_TEXT": "#a3392f",
        "ORNAMENT": "#F2B54A", "ORNAMENT_ACCENT": "#E0583A",
        "FONT_SIZE_XL": 18, "FONT_SIZE_LG": 15, "FONT_SIZE_MD": 12,
        "FONT_SIZE_BASE": 11, "FONT_SIZE_SM": 10, "FONT_SIZE_XS": 9,
    },
    "sakura": {
        "PRIMARY": "#F27CA0", "PRIMARY_DARK": "#D85F87", "PRIMARY_LIGHT": "#FBD9E4",
        "BG_SOFT": "#FFF0F5", "ACCENT": "#9B6FB3", "TEXT": "#4A2E39", "TEXT_MUTED": "#70545F",
        "CARD_BG": "#FFFFFF", "CARD_BG_ALT": "#FFF5F8", "CARD_BORDER": "#F5C4D3",
        "SHADOW": "#F0B8CB", "ERROR": "#c62828", "SUCCESS": "#2E7D32", "HEADING": "#7A3B54",
        "BANNER_BG": "#fff3cd", "BANNER_TEXT": "#856404",
        "ORNAMENT": "#F9B3CA", "ORNAMENT_ACCENT": "#E0507F",
        "FONT_SIZE_XL": 18, "FONT_SIZE_LG": 15, "FONT_SIZE_MD": 12,
        "FONT_SIZE_BASE": 11, "FONT_SIZE_SM": 10, "FONT_SIZE_XS": 9,
    },
}
THEME_NAMES = ["gray", "mint", "sky", "twilight", "campfire", "sakura"]  # 菜单里出现的顺序
