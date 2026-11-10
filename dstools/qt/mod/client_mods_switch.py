"""Mod 列表里的"禁用客户端 Mod"开关行：Mod 管理页与创建世界向导共用。

这一行不是 Mod，是 modoverrides.lua 里科雷提供的存档级开关（CLIENT_MODS_DISABLED_KEY），
借用 Mod 行的样式显示，开关随各自的"保存"/"创建"写入。只对服务器存档有效。
"""

from dstools.features.mod.list_model import mod_name_cmp
from dstools.features.mod.manager import CLIENT_MODS_DISABLED_KEY
from dstools.i18n import t
from dstools.shared.resource_paths import bundled_resource_dir

_ICON_PATH = bundled_resource_dir() / "icons" / "ui" / "client_mods_disabled.png"
_icon = None


def client_mods_disabled_icon():
    """开关行的图标（PIL 图像，首次用到时读取）；读不到时返回 None，面板画默认图标。"""
    global _icon
    if _icon is None:
        from PIL import Image
        try:
            with Image.open(_ICON_PATH) as image:
                _icon = image.convert("RGBA")
        except OSError:
            _icon = False
    return _icon or None


def switch_row(enabled: bool, show: str, query: str) -> dict | None:
    """按当前筛选页签（show）和搜索词生成开关行；不该出现时返回 None。
    它不来自创意工坊、只在服务端生效，归入"服务端 Mod""本地 Mod"；"客户端 Mod"页签不显示它，
    "已启用""已禁用"按开关状态归类。"""
    if show == "client" or show == ("disabled" if enabled else "enabled"):
        return None
    name = t("mod.client_mods_disabled")
    needle = query.strip().casefold()
    if needle and needle not in name.casefold() and needle not in CLIENT_MODS_DISABLED_KEY:
        return None
    return {"workshop_id": CLIENT_MODS_DISABLED_KEY, "name": name,
            "version_text": t("mod.client_mods_disabled_desc"), "enabled": enabled,
            "is_local": False, "has_config": False, "has_link": False,
            "has_folder": False, "mod_format": None}


def switch_row_index(rows: list[dict], name: str, sort_enabled: bool, sort_enabled_ids) -> int:
    """按 sort_mod_data 的规则（启用在前、再按名称）找开关行的位置，置顶的 LuaJIT 行之后。
    各行和开关自己的启用状态都取上次排序时的快照：行顺序没有随切换重排，用当前状态会让开关行
    跳到刚关掉的 Mod 前面。"""
    for index, row in enumerate(rows):
        if row.get("locked"):
            continue
        if sort_enabled != (row["workshop_id"] in sort_enabled_ids):
            if sort_enabled:
                return index
            continue
        if mod_name_cmp(name, row["name"] or row["workshop_id"]) < 0:
            return index
    return len(rows)
