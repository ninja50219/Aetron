"""Tests for the project map a model is handed before its first step."""

from aetron.analyzer import analyze
from aetron.context.overview import (
    build_overview,
    estimate_tokens,
    list_folder,
    list_skipped,
)
from aetron.scanner import scan


def overview_of(make_project, layout, **kwargs):
    root = make_project(layout)
    scan_result = scan(root)
    return build_overview(scan_result, analyze(scan_result), **kwargs)


SCRIPTS = {
    "APPopen.py": 'def open_app(name):\n    pass\n\n\ndef main():\n    open_app("x")\n\n\nif __name__ == "__main__":\n    main()\n',
    "calibration.py": "def calibrate():\n    pass\n",
    "ScreenReader.py": "class ScreenReader:\n    def read(self):\n        pass\n\n\nif __name__ == '__main__':\n    ScreenReader().read()\n",
}


class TestWhatStartsTheProgram:
    """The question the first real model could not answer: it searched "main"
    eleven times in a folder of scripts."""

    def test_scripts_are_named_as_where_the_program_starts(self, make_project):
        text = overview_of(make_project, SCRIPTS).text
        assert "Starts at: APPopen.py (script), ScreenReader.py (script)" in text
        assert "calibration.py (script)" not in text

    def test_a_unity_component_is_an_entry_point(self, make_project):
        layout = {"Assets/Scripts/Player.cs": "public class Player : MonoBehaviour\n{\n    void Update() { }\n}\n"}
        text = overview_of(make_project, layout).text
        assert "Assets/Scripts/Player.cs (Unity script)" in text
        assert "class Player (Update)" in text

    def test_roblox_server_and_client_scripts_are_entry_points(self, make_project):
        layout = {
            "src/server/Main.server.luau": "local function start()\nend\n",
            "src/client/Input.client.lua": "local function onInput()\nend\n",
            "src/shared/Util.luau": "local Util = {}\nreturn Util\n",
        }
        text = overview_of(make_project, layout).text
        assert "Main.server.luau (server script)" in text
        assert "Input.client.lua (client script)" in text

    def test_starting_files_come_first(self, make_project):
        overview = overview_of(make_project, SCRIPTS)
        files = overview.text.split("most important first):\n")[1].splitlines()
        assert files[0].startswith("APPopen.py [script]")


class TestItNeverSendsCode:
    def test_names_only(self, make_project):
        text = overview_of(make_project, SCRIPTS).text
        assert "open_app" in text
        assert 'open_app("x")' not in text
        assert "__name__" not in text

    def test_every_shown_file_counts_as_found(self, make_project):
        overview = overview_of(make_project, SCRIPTS)
        assert overview.shown == {"APPopen.py", "calibration.py", "ScreenReader.py"}


class TestTheBudget:
    BIG = {f"pkg/mod{i:03d}.py": f"def function_{i}_a():\n    pass\n\n\ndef function_{i}_b():\n    pass\n" for i in range(300)}

    def test_the_map_fits_its_budget(self, make_project):
        for budget in (300, 800, 1500):
            assert overview_of(make_project, self.BIG, budget=budget).tokens <= budget

    def test_what_did_not_fit_is_said_not_dropped(self, make_project):
        """Nothing is dropped silently."""
        overview = overview_of(make_project, self.BIG, budget=300)
        assert overview.omitted > 0
        assert f"+{overview.omitted} more files" in overview.text
        assert "FILES <folder>" in overview.text
        assert len(overview.shown) + overview.omitted == 300

    def test_a_small_project_fits_whole(self, make_project):
        overview = overview_of(make_project, SCRIPTS)
        assert overview.omitted == 0
        assert "more files" not in overview.text

    def test_a_folder_is_named_once_not_on_every_line(self, make_project):
        lines = overview_of(make_project, self.BIG, budget=800).text.splitlines()
        assert lines.count("pkg/") == 1
        assert all(not line.startswith("  pkg/") for line in lines)

    def test_estimate_is_proportional(self):
        assert estimate_tokens("x" * 350) == 101


class TestDescribingAFile:
    def test_public_names_come_before_private_helpers(self, make_project):
        layout = {"a.py": "def _helper():\n    pass\n\n\ndef run():\n    pass\n"}
        line = [l for l in overview_of(make_project, layout).text.splitlines() if l.startswith("a.py")][0]
        assert line.index("run") < line.index("_helper")

    def test_a_long_list_is_cut_with_a_count(self, make_project):
        layout = {"a.py": "".join(f"def f{i}():\n    pass\n" for i in range(10))}
        line = [l for l in overview_of(make_project, layout).text.splitlines() if l.startswith("a.py")][0]
        assert line.endswith("+4")

    def test_a_file_without_a_parser_is_listed_by_name(self, make_project):
        layout = {"main.go": "package main\n", "a.py": "x = 1\n"}
        assert "main.go [entry name]: (no parser: name only)" in overview_of(make_project, layout).text

    def test_dependencies_and_docs_are_named(self, make_project):
        layout = dict(SCRIPTS)
        layout["requirements.txt"] = "pyautogui==0.9\nopencv-python\n"
        layout["README.md"] = "# Screen tools\n"
        text = overview_of(make_project, layout).text
        depends = next(line for line in text.splitlines() if line.startswith("Depends on:"))
        assert "pyautogui" in depends and "opencv-python" in depends
        assert "Docs: README.md" in text

    def test_an_earlier_summary_is_carried(self, make_project):
        text = overview_of(make_project, SCRIPTS, notes="Desktop automation scripts.").text
        assert "Summary from an earlier look: Desktop automation scripts." in text


class TestAskingForMore:
    def test_files_lists_one_folder(self, make_project):
        root = make_project(TestTheBudget.BIG | {"other/x.py": "def other():\n    pass\n"})
        scan_result = scan(root)
        listing = list_folder(scan_result, analyze(scan_result), "other")
        assert listing.shown == {"other/x.py"}
        assert "1 files in other/" in listing.text

    def test_files_of_an_unknown_folder_says_so(self, make_project):
        root = make_project(SCRIPTS)
        scan_result = scan(root)
        assert "No indexed files" in list_folder(scan_result, analyze(scan_result), "nope").text

    def test_skipped_names_each_file_and_its_reason(self, make_project):
        root = make_project({"a.py": "x = 1\n", "dist/bundle.min.js": "var a=1;" * 400})
        scan_result = scan(root)
        text = list_skipped(scan_result, analyze(scan_result))
        assert "dist" in text
