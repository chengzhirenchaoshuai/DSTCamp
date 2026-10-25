"""DST 服务器 cluster_token.txt 令牌的分类、脱敏与文件读写。"""

import hashlib
from enum import Enum
from pathlib import Path

# Klei 账号的服务器令牌管理页（“申请令牌”入口）
KLEI_SERVERS_URL = "https://accounts.klei.com/account/game/servers?game=DontStarveTogether"


class ServerTokenKind(str, Enum):
    """Klei 服务器令牌格式：只识别实测过的三段式与四段式，未知格式可手动使用但不参与自动调度。"""

    OLD = "old"
    NEW = "new"
    UNKNOWN = "unknown"


def classify_token(token: str) -> ServerTokenKind:
    """按结构区分旧三段式、新四段式令牌，不依赖具体账号或总长度。"""
    parts = token.strip().split("^")
    if not parts or parts[0] != "pds-g" or any(not part for part in parts[1:]):
        return ServerTokenKind.UNKNOWN
    if len(parts) not in (3, 4) or not parts[1].startswith(("KU_", "OU_")):
        return ServerTokenKind.UNKNOWN
    return ServerTokenKind.OLD if len(parts) == 3 else ServerTokenKind.NEW


def extract_token_owner_id(token: str) -> str | None:
    """从令牌第二段取出所有者的 KU_/OU_ ID（引擎默认其为管理员，无需写入 adminlist）；格式不认识返回 None。"""
    parts = token.strip().split("^")
    if len(parts) < 2 or parts[0] != "pds-g" or not parts[1].startswith(("KU_", "OU_")):
        return None
    return parts[1]


def token_fingerprint(token: str) -> str:
    """返回只用于本机占用状态关联的不可逆指纹，不暴露令牌正文。"""
    return hashlib.sha256(token.strip().encode("utf-8")).hexdigest()


def read_token(path: Path) -> str:
    """从 cluster_token.txt 读取集群令牌，文件不存在时返回空字符串。"""
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def write_token(path: Path, token: str) -> None:
    """把集群令牌写入 cluster_token.txt。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token.strip() + "\n", encoding="utf-8")


def mask_token(token: str, show_chars: int = 8) -> str:
    """脱敏显示令牌，只保留首尾各几位，形如 "pds-g^KU...c0w="。"""
    if len(token) <= show_chars * 2 + 3:
        return "*" * len(token)
    return token[:show_chars] + "..." + token[-show_chars:]


def is_valid_token(token: str) -> bool:
    """粗略校验令牌是否像样（非空且长度合理）。"""
    return bool(token and len(token) > 20)
