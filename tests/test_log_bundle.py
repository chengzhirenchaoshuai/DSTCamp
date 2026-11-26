"""日志包中服务器大日志的折叠与截断。"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import run  # noqa: E402

from dstools.features.local_service import log_bundle  # noqa: E402


def test_collapse_repeated_lines_then_keep_head_and_tail() -> None:
    """11-26 用户日志：Mod 同一行连续刷屏 17 万行，折叠后保留第一行、重复次数和最后一次的时间。"""
    spam = "Stale Component Reference: GUID 1, ../mods/workshop-1392778117/scripts/prefabs/items_legion.lua:2662\n"
    lines = ["[00:00:01]: start\n"]
    lines += [f"[00:00:{second:02d}]: {spam}" for second in range(2, 50)]
    lines += ["[00:00:50]: Shutting down"]
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "server_log.txt"
        path.write_text("".join(lines), encoding="utf-8", newline="")
        text = log_bundle._collapse_server_log(path)
        assert text.count("Stale Component") == 1, text
        assert "又连续重复 47 次（最后一次 [00:00:49]）" in text, text
        assert text.startswith("[00:00:01]: start") and text.endswith("Shutting down"), text

        # 不重复的输出折叠不了，超过上限时只留开头与末尾
        path.write_text("".join(f"[00:00:00]: line {i}\n" for i in range(1000)), encoding="utf-8", newline="")
        old = log_bundle._SERVER_LOG_PACKED_BYTES, log_bundle._SERVER_LOG_HEAD_BYTES
        log_bundle._SERVER_LOG_PACKED_BYTES, log_bundle._SERVER_LOG_HEAD_BYTES = 2000, 500
        try:
            text = log_bundle._collapse_server_log(path)
        finally:
            log_bundle._SERVER_LOG_PACKED_BYTES, log_bundle._SERVER_LOG_HEAD_BYTES = old
        assert "line 0\n" in text and "line 999\n" in text and "line 500\n" not in text, text
        assert "中间省略" in text and len(text.encode("utf-8")) < 2200, len(text)


run(globals())
