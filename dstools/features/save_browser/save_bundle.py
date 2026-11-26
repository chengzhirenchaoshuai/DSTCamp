"""把完整存档目录导出为可分享的 ZIP 文件。"""

from __future__ import annotations

import datetime as _dt
import os
import zipfile
from pathlib import Path

from dstools.features.local_service.dedicated_server import get_documents_dir


def default_save_bundle_output_dir() -> Path:
    """返回用户容易找到、且不会被一起打进存档的默认输出目录。"""
    return get_documents_dir() / "DSTCamp" / "存档"


def _is_link_or_junction(path: Path) -> bool:
    """链接可能指向存档目录外，完整导出时不跟随它们。"""
    return path.is_symlink() or os.path.isjunction(path)


def _raise_walk_error(error: OSError) -> None:
    raise error


def create_save_bundle(
    cluster_path: Path, output_dir: Path | None = None,
) -> Path:
    """把整个存档目录（含根目录名）压缩为 ZIP 并返回路径；不跟随符号链接或 junction。"""
    cluster_path = Path(cluster_path).resolve()
    if not cluster_path.is_dir():
        raise FileNotFoundError(f"存档目录不存在：{cluster_path}")

    output_dir = Path(output_dir) if output_dir else default_save_bundle_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_path = output_dir / f"DSTCamp_存档_{cluster_path.name}_{stamp}.zip"
    suffix = 2
    while zip_path.exists():
        zip_path = output_dir / (
            f"DSTCamp_存档_{cluster_path.name}_{stamp}_{suffix}.zip"
        )
        suffix += 1

    with zipfile.ZipFile(
        zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6,
    ) as archive:
        for current_root, dir_names, file_names in os.walk(
            cluster_path, followlinks=False, onerror=_raise_walk_error,
        ):
            current = Path(current_root)
            dir_names[:] = [
                name for name in dir_names
                if not _is_link_or_junction(current / name)
            ]
            relative_root = current.relative_to(cluster_path)
            archive_root = Path(cluster_path.name) / relative_root
            regular_files = [
                current / name for name in file_names
                if not _is_link_or_junction(current / name)
                and (current / name).resolve() != zip_path.resolve()
            ]
            if not dir_names and not regular_files:
                archive.writestr(archive_root.as_posix().rstrip("/") + "/", b"")
            for source in regular_files:
                archive.write(source, (archive_root / source.name).as_posix())
    return zip_path
