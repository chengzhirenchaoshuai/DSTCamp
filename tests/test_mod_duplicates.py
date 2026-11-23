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


def test_similar_list_matches_workshop_keys_and_absorbs_file_clash():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        gp = _mod(root, "gp", {"components/globalposition.lua": "A"})
        cs = _mod(root, "cs", {"components/globalposition.lua": "B"})
        report = find_duplicates({"workshop-378160973": ("Global Positions", gp),
                                  "workshop-3138571948": ("全图定位(完全同步)", cs),
                                  "workshop-2189004162": ("Insight", None)})
        assert [(g.kind, g.mod_ids) for g in report.similar] == [
            ("global_positions", ("workshop-378160973", "workshop-3138571948"))]
        assert not report.file_clashes and report.group_count == 1


def test_clash_between_mods_linked_by_dependencies_is_intentional():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        core = _mod(root, "100", {"a.lua": "core", "b.lua": "core"})
        dlc = _mod(root, "200", {"a.lua": "dlc"})
        pack = _mod(root, "300", {"b.lua": "pack"})
        other = _mod(root, "my-mod", {"a.lua": "other"})
        (dlc / "modinfo.lua").write_text(
            'mod_dependencies = {}\nif gitlab then mod_dependencies = {{ ["ia-core"] = true }}\n'
            'else mod_dependencies = {{ workshop = "workshop-100" }} end\n', encoding="utf-8")
        # 只依赖扩展包，经传递与本体连在一起；注释里的依赖不算
        (pack / "modinfo.lua").write_text(
            '-- mod_dependencies = {{ workshop = "workshop-999" }}\n'
            'mod_dependencies = {\n  { workshop = "workshop-200" },\n}\n', encoding="utf-8")
        report = find_duplicates({"workshop-100": ("Core", core), "workshop-200": ("DLC", dlc),
                                  "workshop-300": ("Pack", pack), "my-mod": ("Mine", other)})
        assert [(c.mod_ids, c.paths) for c in report.file_clashes] == [
            (("my-mod", "workshop-100", "workshop-200"), ("a.lua",))]


def test_clash_between_mods_of_same_author_is_intentional():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        main = _mod(root, "main", {"a.lua": "main"})
        chars = _mod(root, "chars", {"a.lua": "chars"})
        other = _mod(root, "other", {"a.lua": "other"})
        blank = _mod(root, "blank", {"a.lua": "blank"})
        (main / "modinfo.lua").write_text('name = "Main"\nauthor = "羽中, 小明"\n', encoding="utf-8")
        (chars / "modinfo.lua").write_text('author="羽中,  小明" -- 同一作者\n', encoding="utf-8")
        (other / "modinfo.lua").write_text('author = "Someone"\n', encoding="utf-8")
        (blank / "modinfo.lua").write_text('author = ""\n', encoding="utf-8")
        report = find_duplicates({"main": ("Main", main), "chars": ("Chars", chars)})
        assert not report.file_clashes
        # 空作者不互相合并；同作者那组内部不算，与其他 Mod 之间照常提示
        report = find_duplicates({"main": ("Main", main), "chars": ("Chars", chars),
                                  "other": ("Other", other), "blank": ("Blank", blank)})
        assert [c.mod_ids for c in report.file_clashes] == [("blank", "chars", "main", "other")]


def test_scripts_loaded_only_by_modimport_do_not_clash():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        files = {"languages/strings_chinese.lua": "", "lib/util.lua": "", "components/foo.lua": ""}
        mods = {}
        for key, main in (
            ("a", 'modimport("scripts/languages/strings_chinese.lua")\nmodimport "scripts/lib/util"\n'
                  'modimport("scripts/components/foo.lua")\n'),
            # 拼接的文件名按一级通配；自己又 require 了 lib.util，这个文件仍会经 package.path 互相顶掉
            ("b", 'local lang = "chinese"\nmodimport("scripts/languages/strings_" .. lang .. ".lua")\n'
                  'modimport("scripts/lib/util.lua")\nlocal u = GLOBAL.require("lib.util")\n'
                  '-- modimport("scripts/components/foo.lua")\nmodimport("scripts/components/foo.lua")\n'),
        ):
            folder = _mod(root, key, {rel: key for rel in files})
            (folder / "modmain.lua").write_text(main, encoding="utf-8")
            mods[key] = (key.upper(), folder)
        report = find_duplicates(mods)
        # components/ 由游戏按名字 require，不按 modimport 排除
        assert [c.paths for c in report.file_clashes] == [("components/foo.lua", "lib/util.lua")]


def test_modimport_in_comment_or_without_scripts_prefix_is_not_evidence():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        a = _mod(root, "a", {"strings.lua": "A"})
        b = _mod(root, "b", {"strings.lua": "B"})
        (a / "modmain.lua").write_text('--[[ modimport("scripts/strings.lua") ]]\n', encoding="utf-8")
        (b / "modmain.lua").write_text('modimport("strings.lua")\nmodimport("scripts/" .. x)\n',
                                       encoding="utf-8")
        report = find_duplicates({"a": ("A", a), "b": ("B", b)})
        assert [c.paths for c in report.file_clashes] == [("strings.lua",)]


if __name__ == "__main__":
    run(globals())
