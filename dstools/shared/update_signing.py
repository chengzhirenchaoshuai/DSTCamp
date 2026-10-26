"""更新清单（DSTCamp-X.Y.Z.sha256.json）的 Ed25519 签名与校验。

清单里的 sha256 决定程序会安装什么，而清单可能来自 Gitee/GitHub，也可能来自蓝奏云等网盘的离线包；
有了签名，断网导入时也能确认清单出自发布者。签名放在清单自己的 ``signature`` 字段，
对去掉该字段后的规范化 JSON 签名。私钥只在发布机项目外的密钥目录（见 scripts/build_exe.py 的 _project_key_dir()），不进 Git。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

# 与密钥目录里 update_signing_key.pem 配对的公钥（32 字节原始格式）
PUBLIC_KEY_HEX = "05eaa6e9e54bbcee6e8a6342972b489154942d970edaa7c7715b996951df1e76"
SIGNATURE_FIELD = "signature"


def _canonical_bytes(manifest: dict) -> bytes:
    unsigned = {key: value for key, value in manifest.items() if key != SIGNATURE_FIELD}
    return json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign_manifest(manifest: dict, private_key_pem: Path) -> dict:
    """返回带签名的清单副本（发布构建用）。"""
    from cryptography.hazmat.primitives import serialization

    key = serialization.load_pem_private_key(private_key_pem.read_bytes(), password=None)
    signature = key.sign(_canonical_bytes(manifest))
    return {**manifest, SIGNATURE_FIELD: base64.b64encode(signature).decode("ascii")}


def verify_manifest(manifest: dict) -> bool:
    """签名存在且与内置公钥匹配时返回 True。"""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        signature = base64.b64decode(str(manifest.get(SIGNATURE_FIELD) or ""), validate=True)
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(PUBLIC_KEY_HEX)).verify(
            signature, _canonical_bytes(manifest)
        )
    except (InvalidSignature, ValueError, TypeError):
        return False
    return True
