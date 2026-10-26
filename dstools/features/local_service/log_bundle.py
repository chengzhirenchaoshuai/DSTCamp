"""收集并压缩当前存档的诊断日志：不包含令牌、管理员列表等敏感文件，备份日志只取最新几份。"""

from __future__ import annotations

import datetime as _dt
import re
import zipfile
from pathlib import Path

from dstools.features.local_service.dedicated_server import get_documents_dir

_ERROR_MARKERS = (
    "LUA ERROR",
    "stack traceback",
    "server failed to start",
    "unhandled exception",
    "error loading",
    "failed msimulation",
    "socket_port_already_in_use",
)
_MAX_LOG_BYTES = 32 * 1024 * 1024
_MAX_BACKUP_FILES_PER_SHARD = 5
_MOD_LINE_RE = re.compile(
    r"(?:inserting\s+modname,|Loading\s+mod:|Mod:)\s*"
    r"(workshop-\d+)(?:\s*\(([^)]*)\))?",
    re.IGNORECASE,
)
# SteamCMD 通道附带的日志：只按名字取这几份，config.vdf（登录凭据）等绝不收；每份只留末尾一段
_STEAMCMD_LOGS = ("console_log.txt", "content_log.txt", "workshop_log.txt")
_STEAMCMD_LOG_TAIL_BYTES = 256 * 1024
# 实测日志里账号信息只以这两种形式出现：Logging in user '<用户名>'、[U:1:<SteamID>]
_STEAMCMD_USER_RE = re.compile(r"(Logging in user ')[^']*(')")
_STEAMCMD_STEAMID_RE = re.compile(r"\[U:1:[1-9]\d*\]")
_FRPC_LOG_TAIL_BYTES = 512 * 1024
_NAT_LABELS = {"sakura": "樱花", "lolia": "Lolia", "selfhost": "自建 frp"}


def default_log_output_dir() -> Path:
    """用户可直接找到的默认输出目录。"""
    return get_documents_dir() / "DSTCamp" / "日志"


def _looks_like_error(path: Path) -> bool:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                if any(marker.lower() in line.lower() for marker in _ERROR_MARKERS):
                    return True
    except OSError:
        return False
    return False


def _add_file(files: list[tuple[Path, str]], source: Path, arcname: str) -> None:
    try:
        if source.is_file() and source.stat().st_size <= _MAX_LOG_BYTES:
            files.append((source, arcname))
    except OSError:
        pass


def _files_for_shard(shard_path: Path, shard_name: str) -> list[tuple[Path, str]]:
    files: list[tuple[Path, str]] = []
    _add_file(files, shard_path / "server_log.txt", f"{shard_name}/server_log.txt")
    # 运行中报错通常会滚动到 backup/server_log；优先带错误日志，再补最新日志。
    backup_dir = shard_path / "backup" / "server_log"
    try:
        backups = sorted(
            (p for p in backup_dir.glob("server_log_*.txt") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        backups = []
    selected: list[Path] = []
    for candidate in backups:
        if _looks_like_error(candidate):
            selected.append(candidate)
        if len(selected) >= _MAX_BACKUP_FILES_PER_SHARD:
            break
    if len(selected) < _MAX_BACKUP_FILES_PER_SHARD:
        for candidate in backups:
            if candidate not in selected:
                selected.append(candidate)
            if len(selected) >= _MAX_BACKUP_FILES_PER_SHARD:
                break
    for candidate in selected:
        _add_file(files, candidate, f"{shard_name}/backup/server_log/{candidate.name}")

    # 这些配置有助于定位 Mod/世界配置问题，但不包含 cluster_token.txt。
    for filename in ("server.ini", "modoverrides.lua", "leveldataoverride.lua"):
        _add_file(files, shard_path / filename, f"{shard_name}/{filename}")
    return files


def _extract_mod_list(files: list[tuple[Path, str]]) -> str:
    """从已收集的日志提取实际出现过的 Mod ID 和名称。"""
    mods: dict[str, str] = {}
    for source, arcname in files:
        if not arcname.endswith(".txt") or "server_log" not in arcname:
            continue
        try:
            text = source.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in _MOD_LINE_RE.finditer(text):
            mod_id = match.group(1).lower()
            name = (match.group(2) or "").strip()
            if mod_id not in mods or (name and not mods[mod_id]):
                mods[mod_id] = name
    lines = ["DSTCamp Mod 列表", "", "格式：Mod ID（名称）", ""]
    if not mods:
        lines.append("未从当前日志中识别到创意工坊 Mod。")
    else:
        for mod_id, name in mods.items():
            lines.append(f"{mod_id}" + (f"（{name}）" if name else ""))
    return "\n".join(lines) + "\n"


def _redact_steamcmd_log(text: str, account: str = "") -> str:
    text = _STEAMCMD_USER_RE.sub(r"\1<账号>\2", text)
    text = _STEAMCMD_STEAMID_RE.sub("[U:1:<SteamID>]", text)
    if len(account) >= 3:  # 太短的名字全文替换会误伤正常内容
        text = re.sub(re.escape(account), "<账号>", text, flags=re.IGNORECASE)
    return text


def _steamcmd_log_entries() -> list[tuple[str, str]]:
    """SteamCMD 通道下附带它自己的日志末尾（去掉账号与 SteamID），返回 [(包内路径, 内容)]。
    只算路径，不为此释放内置 SteamCMD。"""
    from dstools.features.local_service import steamcmd_updater
    from dstools.features.local_service.steam_backend import SteamBackend, get_backend
    from dstools.shared.app_settings import get_steamcmd_account

    if get_backend() is not SteamBackend.STEAMCMD:
        return []
    root = steamcmd_updater.custom_steamcmd_path() or steamcmd_updater.bundled_steamcmd_path()
    account = get_steamcmd_account()
    entries = []
    for name in _STEAMCMD_LOGS:
        text = _read_tail(root.parent / "logs" / name, _STEAMCMD_LOG_TAIL_BYTES)
        if text is not None:
            entries.append((f"SteamCMD/{name}", _redact_steamcmd_log(text, account)))
    return entries


def _read_tail(path: Path, limit: int) -> str | None:
    """读文件末尾至多 limit 字节，读不到返回 None。"""
    try:
        with path.open("rb") as file:
            size = file.seek(0, 2)
            file.seek(max(0, size - limit))
            data = file.read()
    except OSError:
        return None
    text = data.decode("utf-8", errors="replace")
    if size > limit:
        text = text.split("\n", 1)[-1]  # 截断处的半行丢掉
    return text


def _nat_summary(shard_names, nat_owners) -> list[str]:
    """穿透方式与大厅加速开关，写进 manifest。nat_owners 为 {世界名: 映射归属}，由界面按实际映射算好。"""
    from dstools.shared.app_settings import get_lobby_accel_enabled

    if nat_owners is None:
        nat = "未知"
    else:
        parts = [f"{name}={_NAT_LABELS.get(nat_owners.get(name), '未映射')}" for name in shard_names]
        nat = "、".join(parts) or "未知"
    return [f"内网穿透：{nat}", f"大厅加速：{'已开启' if get_lobby_accel_enabled() else '未开启'}"]


def create_log_bundle(cluster_path: Path, shard_names=None, output_dir: Path | None = None,
                      nat_owners: dict[str, str | None] | None = None) -> Path:
    """打包一个服务器存档的当前日志、近期错误日志、frpc 输出和安全配置文件。"""
    cluster_path = Path(cluster_path)
    if not cluster_path.is_dir():
        raise FileNotFoundError(f"服务器存档不存在：{cluster_path}")
    if shard_names is None:
        shard_names = sorted(
            p.name for p in cluster_path.iterdir()
            if p.is_dir() and (p / "server_log.txt").exists()
        )
    shard_names = list(shard_names)
    files: list[tuple[Path, str]] = []
    for shard_name in shard_names:
        files.extend(_files_for_shard(cluster_path / str(shard_name), str(shard_name)))
    if not files:
        raise FileNotFoundError("当前存档没有可收集的服务器日志")
    # V1 Mod 目录多余文件的清理记录，用来追查这些文件从哪来
    from dstools.features.mod.legacy_v1 import legacy_extras_log_path

    _add_file(files, legacy_extras_log_path(), "DSTCamp/v1_extras.log")

    output_dir = Path(output_dir) if output_dir else default_log_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_path = output_dir / f"DSTCamp_日志_{cluster_path.name}_{stamp}.zip"
    suffix = 2
    while zip_path.exists():
        zip_path = output_dir / f"DSTCamp_日志_{cluster_path.name}_{stamp}_{suffix}.zip"
        suffix += 1
    manifest = [
        "DSTCamp 本地服务器日志包",
        f"生成时间：{_dt.datetime.now().isoformat(timespec='seconds')}",
        f"存档目录：{cluster_path}",
        f"世界：{'、'.join(shard_names) or '未识别'}",
        *_nat_summary(shard_names, nat_owners),
        "说明：已跳过 cluster_token、管理员列表等敏感文件；SteamCMD 日志已去掉账号与 SteamID，不含登录凭据；"
        "frpc 日志已去掉樱花 Token。",
        "",
    ]
    from dstools.shared.frpc_log import frpc_log_path

    tails = _steamcmd_log_entries()
    frpc_text = _read_tail(frpc_log_path(cluster_path), _FRPC_LOG_TAIL_BYTES)
    if frpc_text is not None:
        tails.append(("DSTCamp/frpc.log", frpc_text))
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for source, arcname in files:
            archive.write(source, arcname)
            manifest.append(f"{arcname} ({source.stat().st_size} bytes)")
        for arcname, text in tails:
            archive.writestr(arcname, text)
            manifest.append(f"{arcname}（末尾 {len(text.encode('utf-8'))} bytes）")
        if frpc_text is None:
            # 接管上次运行遗留的 frpc 时没有输出管道，这种情况也没有记录
            manifest.append("DSTCamp/frpc.log：没有记录（本存档没由 DSTCamp 启动过 frpc，或只接管过遗留的 frpc 进程）")
        archive.writestr("manifest.txt", "\n".join(manifest) + "\n")
        archive.writestr("mod_list.txt", _extract_mod_list(files))
    return zip_path
