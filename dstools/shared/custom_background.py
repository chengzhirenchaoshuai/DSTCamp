"""自定义界面背景图：把用户选的图片拷进数据目录并记录文件名（裁剪、混合由 qt/background.py 负责）。"""

import shutil
from pathlib import Path

from dstools.shared.app_settings import get_custom_bg_filename, set_custom_bg_filename
from dstools.shared.resource_paths import data_dir

_DATA_NAME = "background"


def _data_dir() -> Path:
    return data_dir(_DATA_NAME, legacy_cache_name=_DATA_NAME)


def get_custom_bg_path() -> Path | None:
    """返回已保存的背景图路径；未设置或文件已被删除时返回 None。"""
    name = get_custom_bg_filename()
    if not name:
        return None
    path = _data_dir() / name
    return path if path.exists() else None


def set_custom_bg_image(source: Path) -> Path:
    """复制一份到数据目录（保留扩展名）并覆盖旧图，原图移动或删除不影响已选背景。"""
    _clear_saved_file()
    d = _data_dir()
    d.mkdir(parents=True, exist_ok=True)
    dest = d / f"custom_bg{source.suffix.lower()}"
    shutil.copyfile(source, dest)
    set_custom_bg_filename(dest.name)
    return dest


def clear_custom_bg_image() -> None:
    """删除背景图文件并清空设置。"""
    _clear_saved_file()
    set_custom_bg_filename(None)


def _clear_saved_file() -> None:
    d = _data_dir()
    if not d.exists():
        return
    for f in d.glob("custom_bg.*"):  # 只删本模块写入的背景图
        try:
            f.unlink()
        except OSError:
            pass
