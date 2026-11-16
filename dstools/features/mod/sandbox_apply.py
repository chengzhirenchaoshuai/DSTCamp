"""把整份文件沙箱解析结果应用到已静态解析的 ModInfo（批量加载与配置弹窗共用，保证结果一致）。"""


def apply_full_sandbox_result(mod_info, result: dict | None) -> None:
    if not result:
        mod_info.version_status = "unresolved"
        mod_info.full_sandbox_tried = True
        return
    if "config_options" in result:
        mod_info.config_options = result["config_options"]
        mod_info.unsupported_schema = False
        mod_info.sandbox_resolved = True
    for key in ("name", "author", "version", "version_compatible", "description", "icon", "icon_atlas"):
        if key in result:
            setattr(mod_info, key, result[key])
    if "version" not in result or result["version"] is None:
        mod_info.version, mod_info.version_status = "", "undeclared"
    else:
        value = result["version"]
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            mod_info.version, mod_info.version_status = "", "unresolved"
        elif str(value).strip():
            mod_info.version, mod_info.version_status = str(value).strip(), "confirmed"
        else:
            mod_info.version, mod_info.version_status = "", "undeclared"
    mod_info.full_sandbox_tried = True
    compatible = result.get("version_compatible")
    valid = compatible is not None and not isinstance(compatible, bool) and isinstance(compatible, (str, int, float))
    mod_info.version_compatible = str(compatible).strip() if valid else ""


def trusted_defaults(mod_info) -> dict:
    """补进配置用的默认值：只认沙箱求值过的结果，没解析成功时返回空表。

    写空表是安全的：modoverrides 里没有的键，游戏按 modinfo 的 default 取值（GetModConfigData）。
    静态解析的默认值不能写：曾把 DontStarveLuaJit2 的 ``luavmtype.jit`` 原样写成字符串，
    Mod 每次加载都判定要切换虚拟机并 c_reset()，世界无限重载。
    """
    if mod_info is None or not mod_info.sandbox_resolved:
        return {}
    return {opt.name: opt.default for opt in mod_info.config_options
            if not opt.is_header and opt.name and opt.default is not None}
