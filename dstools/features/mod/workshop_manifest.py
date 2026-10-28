"""解析并验证《饥荒：联机版》的 ``mod.manifest``（MNFS）文件。"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from pathlib import Path



MANIFEST_MAGIC = b"MNFS"
MANIFEST_VERSION = 1


class ManifestFormatError(ValueError):
    """Manifest 头部、版本或长度不符合游戏格式。"""


@dataclass(frozen=True)
class ModManifest:
    version: int
    path_hashes: tuple[int, ...]


@dataclass(frozen=True)
class ManifestVerification:
    available: bool
    valid: bool | None
    expected_count: int = 0
    present_count: int = 0
    missing_hashes: tuple[int, ...] = ()
    error: str = ""


def sdbm_path_hash(relative_path: str) -> int:
    """游戏 Manifest 使用的 SDBM 路径哈希（路径统一为小写正斜杠，已用真实样本验证）。"""
    normalized = str(relative_path).replace("\\", "/").strip("/").lower()
    return _sdbm_extend(0, normalized)


def _sdbm_extend(value: int, text: str) -> int:
    """从已有哈希值接着累加 text：SDBM 逐字节流式计算，目录前缀的结果可以给下面所有文件复用。"""
    for byte in text.encode("utf-8"):
        # 游戏原生实现的 ``char`` 在 Windows 构建中为有符号类型；ASCII
        # 结果相同，但中文等 UTF-8 高位字节必须按 -128..-1 参与运算。
        if byte >= 0x80:
            byte -= 0x100
        value = (byte + (value << 6) + (value << 16) - value) & 0xFFFFFFFF
    return value


def parse_mod_manifest_bytes(data: bytes) -> ModManifest:
    if len(data) < 12:
        raise ManifestFormatError("Manifest 长度不足12字节")
    magic, version, count = struct.unpack_from("<4sII", data, 0)
    if magic != MANIFEST_MAGIC:
        raise ManifestFormatError("Manifest 文件头不是 MNFS")
    if version != MANIFEST_VERSION:
        raise ManifestFormatError(f"不支持的 Manifest 版本：{version}")
    expected_size = 12 + count * 4
    if len(data) != expected_size:
        raise ManifestFormatError(
            f"Manifest 长度不匹配：声明{count}项，应为{expected_size}字节，实际{len(data)}字节"
        )
    hashes = struct.unpack_from(f"<{count}I", data, 12) if count else ()
    return ModManifest(version, tuple(hashes))


def load_mod_manifest(path: Path) -> ModManifest:
    try:
        return parse_mod_manifest_bytes(Path(path).read_bytes())
    except OSError as exc:
        raise ManifestFormatError(f"无法读取 Manifest：{exc}") from exc


def verify_mod_manifest(
    mod_folder: Path, manifest_path: Path | None = None
) -> ManifestVerification:
    """检查 Manifest 声明的路径是否都存在（额外文件不算损坏）。MNFS 只存路径哈希，能发现缺失/改名，不能发现内容修改。"""
    mod_folder = Path(mod_folder)
    manifest_path = (
        Path(manifest_path) if manifest_path else mod_folder / "mod.manifest"
    )
    if not manifest_path.is_file():
        return ManifestVerification(False, None)
    try:
        manifest = load_mod_manifest(manifest_path)
        # Mod 多时文件有好几万个：只跟踪未命中的声明哈希，全部命中立即停止；
        # 每个目录的"相对路径/"前缀哈希只算一次，文件只再累加文件名
        expected = set(manifest.path_hashes)
        missing_set = set(expected)
        pending = [(str(mod_folder), 0)]
        while pending and missing_set:
            directory, prefix_hash = pending.pop()
            try:
                entries = list(os.scandir(directory))
            except OSError:
                continue  # 读不了的目录跳过
            for entry in entries:
                try:
                    is_dir = entry.is_dir()
                except OSError:
                    is_dir = False
                if not is_dir:
                    missing_set.discard(_sdbm_extend(prefix_hash, entry.name.lower()))
                elif not (entry.is_symlink() or entry.is_junction()):
                    # 与 os.walk 默认一致：链接目录不展开
                    pending.append((entry.path, _sdbm_extend(prefix_hash, entry.name.lower() + "/")))
        missing = tuple(sorted(missing_set))
        return ManifestVerification(
            True,
            not missing,
            expected_count=len(manifest.path_hashes),
            present_count=len(expected) - len(missing_set),
            missing_hashes=missing,
            error=(
                f"Manifest 中有{len(missing)}个文件在 Mod 目录中找不到"
                if missing
                else ""
            ),
        )
    except (ManifestFormatError, OSError, ValueError) as exc:
        return ManifestVerification(True, False, error=str(exc))
