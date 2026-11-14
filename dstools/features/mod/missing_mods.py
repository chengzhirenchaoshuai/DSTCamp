"""启动专服前找出存档已启用但专服加载不了的 Mod。

坑：专服会跳过加载不了的 Mod 照常启动，保存后依赖它的物品/生物可能被永久移除。按专服实际加载路径判断：
* Workshop V2：``-ugc_directory`` 下 ``content/322330/<id>/modinfo.lua``；
* Workshop V1：同目录 ``*_legacy.bin``（启动前解压），或专服 ``mods/workshop-<id>/modinfo.lua``；
* 本地 Mod：专服 ``mods/<名称>/modinfo.lua``。

文件齐全还不够：带 ``-ugc_directory`` 时引擎只列出 Steam 清单 ``appworkshop_322330.acf`` 的
``WorkshopItemsInstalled`` 里登记过的目录（实测见 ``world/shard_management.worldgate_installed``）。
订阅状态异常时会出现"文件下载了、Steam 没登记"的目录，于是：
* ``modindex.ResolveModname`` 解析不出名字，``ApplyEnabledOverrides`` 整段跳过，日志里一个字都不提它；
* 而客户端主菜单走 ``FrontendLoadMod``，直接从残留目录读 modinfo，所以模组页能看到、能勾选、能存配置。
这类 Mod 单列为 ``unregistered_ids``：文件在原地，重新订阅让 Steam 重新登记即可，与"没下载"的处置不同。
专服 ``mods/workshop-<id>`` 里的副本走 ``MODS_ROOT`` 兜底（``modindex.TryLoadMod``），与清单登记无关。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MissingMods:
    workshop_ids: tuple[str, ...] = ()
    local_names: tuple[str, ...] = ()
    # 文件在 content 目录里，但 Steam 清单没登记：专服静默跳过，重新订阅即可修复
    unregistered_ids: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.workshop_ids or self.local_names or self.unregistered_ids)


def _has_modinfo(path: Path) -> bool:
    try:
        return (path / "modinfo.lua").is_file()
    except OSError:
        return False


def _has_legacy_package(path: Path) -> bool:
    try:
        return any(item.is_file() for item in path.glob("*_legacy.bin"))
    except OSError:
        return False


def find_missing_enabled_mods(
    enabled_ids,
    *,
    ugc_content_root: Path | None,
    server_mods_root: Path | None,
    acf_installed_ids: set[int] | None = None,
) -> MissingMods:
    """``enabled_ids`` 为去掉 ``workshop-`` 前缀的启用项；路径未知时不判缺失。

    ``acf_installed_ids`` 是 Steam 清单登记的 Workshop ID（见 ``workshop_acf.read_acf_installed_ids``），
    传 ``None``（读不到清单）时不判"未登记"，避免把全部 Mod 误报一遍。"""
    workshop: list[str] = []
    local: list[str] = []
    unregistered: list[str] = []
    for raw in enabled_ids:
        mod_id = str(raw).strip()
        if not mod_id:
            continue
        if mod_id.isdigit():
            # 不传 -ugc_directory 时专服走自己的下载目录，这里无从判断
            if ugc_content_root is None:
                continue
            # 专服 mods/workshop-<id> 的副本由 MODS_ROOT 兜底加载，不受 -ugc_directory 和清单登记影响
            if server_mods_root is not None and _has_modinfo(Path(server_mods_root) / f"workshop-{mod_id}"):
                continue
            content = Path(ugc_content_root) / mod_id
            if not (_has_modinfo(content) or _has_legacy_package(content)):
                workshop.append(mod_id)
            elif acf_installed_ids is not None and int(mod_id) not in acf_installed_ids:
                unregistered.append(mod_id)
        else:
            if server_mods_root is None or _has_modinfo(Path(server_mods_root) / mod_id):
                continue
            local.append(mod_id)
    return MissingMods(
        tuple(sorted(set(workshop), key=int)),
        tuple(sorted(set(local), key=str.casefold)),
        tuple(sorted(set(unregistered), key=int)),
    )
