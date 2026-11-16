"""重复 Mod 检测：同名与同路径脚本覆盖。"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import run  # noqa: E402

from dstools.features.mod.duplicates import find_duplicates


def _mod(root: Path, name: str, files: dict[str, str]) -> Path:
    folder = root / name
    for rel, text in files.items():
        path = folder / "scripts" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def test_different_content_same_path_is_clash_identical_is_not():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        a = _mod(root, "a", {"components/infester.lua": "A", "lib/json.lua": "same"})
        b = _mod(root, "b", {"Components/Infester.lua": "B", "lib/json.lua": "same"})
        c = _mod(root, "c", {"widgets/other.lua": "C"})
        report = find_duplicates({"a": ("Mod A", a), "b": ("Mod B", b), "c": ("Mod C", c)})
        assert not report.same_name
        assert len(report.file_clashes) == 1
        clash = report.file_clashes[0]
        assert clash.mod_ids == ("a", "b") and clash.paths == ("components/infester.lua",)


def test_same_name_copy_reported_once_not_again_as_file_clash():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ws = _mod(root, "workshop-1", {"x.lua": "v2"})
        local = _mod(root, "my copy", {"x.lua": "v1"})
        report = find_duplicates({"workshop-1": ("Show Me", ws), "my copy": (" show  me", local),
                                  "missing": ("", None)})
        assert [g.mod_ids for g in report.same_name] == [("workshop-1", "my copy")]
        assert not report.file_clashes
        assert report.group_count == 1


if __name__ == "__main__":
    run(globals())
