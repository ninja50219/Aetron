"""End-to-end tests for scan(), against real directory trees."""

import pytest

from aetron.scanner import scan
from aetron.scanner.paths import InvalidPathError


def rel_paths(items):
    return {item.rel_path.replace("\\", "/") for item in items}


class TestBasics:
    def test_collects_source_files(self, make_project):
        root = make_project({"src/app.py": "x = 1\n", "src/util.js": "let y = 2;\n"})
        result = scan(root)
        assert rel_paths(result.files) == {"src/app.py", "src/util.js"}

    def test_records_language_and_line_count(self, make_project):
        root = make_project({"app.py": "a = 1\nb = 2\nc = 3\n"})
        found = scan(root).files[0]
        assert found.language == "python"
        assert found.lines == 4  # three lines plus the trailing newline

    def test_non_source_files_are_not_reported(self, make_project):
        root = make_project({"app.py": "x = 1\n", "logo.png": "binary-ish"})
        result = scan(root)
        assert rel_paths(result.files) == {"app.py"}
        assert result.skipped == []

    def test_results_are_sorted(self, make_project):
        root = make_project({"z.py": "x = 1\n", "a.py": "x = 1\n", "m.py": "x = 1\n"})
        assert [f.rel_path for f in scan(root).files] == ["a.py", "m.py", "z.py"]

    def test_missing_directory_is_rejected(self, tmp_path):
        with pytest.raises(InvalidPathError):
            scan(tmp_path / "nope")


class TestPruning:
    def test_ignored_directory_is_not_walked(self, make_project):
        root = make_project({"src/app.py": "x = 1\n", "node_modules/dep/index.js": "x"})
        result = scan(root)
        assert rel_paths(result.files) == {"src/app.py"}
        assert "node_modules" in result.pruned_dirs

    def test_unity_library_is_pruned_below_the_scan_root(self, make_project):
        # The real case: scanning a folder that holds several projects.
        root = make_project(
            {
                "game/MyMC/ProjectSettings/ProjectVersion.txt": "x",
                "game/MyMC/Assets/Player.cs": "class P {}\n",
                "game/MyMC/Library/PackageCache/pkg/Thing.cs": "class T {}\n",
                "game/MyMC/Temp/Bin/Tmp.cs": "class T {}\n",
            }
        )
        result = scan(root)
        assert rel_paths(result.files) == {"game/MyMC/Assets/Player.cs"}

    def test_generic_directory_name_is_kept_outside_a_project(self, make_project):
        root = make_project({"loose/src/Library/real.py": "x = 1\n"})
        assert rel_paths(scan(root).files) == {"loose/src/Library/real.py"}


class TestSkipReporting:
    def test_generated_file_is_reported_with_a_reason(self, make_project):
        root = make_project({"app.min.js": "var a=1;", "app.js": "let a = 1;\n"})
        result = scan(root)
        assert rel_paths(result.files) == {"app.js"}
        assert [s.reason for s in result.skipped] == ["generated file name"]

    def test_large_handwritten_file_is_kept(self, make_project):
        big = "\n".join(f"CONSTANT_{i} = {i}" for i in range(60_000))
        root = make_project({"legacy.py": big})
        result = scan(root)
        assert rel_paths(result.files) == {"legacy.py"}
        assert result.skipped == []

    def test_undecodable_bytes_do_not_end_the_scan(self, make_project):
        root = make_project({"ok.py": "x = 1\n"})
        (root / "latin.py").write_bytes(b"# nag\xf3wek\nx = 1\n")
        result = scan(root)
        assert rel_paths(result.files) == {"ok.py", "latin.py"}


class TestGitignore:
    def test_directory_pattern(self, make_project):
        root = make_project(
            {".gitignore": "generated/\n", "generated/a.py": "x = 1\n", "b.py": "x = 1\n"}
        )
        result = scan(root)
        assert rel_paths(result.files) == {"b.py"}
        assert "generated" in result.pruned_dirs

    def test_file_pattern_and_negation(self, make_project):
        root = make_project(
            {
                ".gitignore": "*.gen.py\n!keep.gen.py\n",
                "out.gen.py": "x = 1\n",
                "keep.gen.py": "x = 1\n",
            }
        )
        assert rel_paths(scan(root).files) == {"keep.gen.py"}

    def test_nested_gitignore_only_governs_its_subtree(self, make_project):
        root = make_project(
            {
                "sub/.gitignore": "hidden/\n",
                "sub/hidden/a.py": "x = 1\n",
                "other/hidden/b.py": "x = 1\n",
            }
        )
        assert rel_paths(scan(root).files) == {"other/hidden/b.py"}

    def test_can_be_turned_off(self, make_project):
        root = make_project({".gitignore": "*.py\n", "a.py": "x = 1\n"})
        assert scan(root).files == []
        assert rel_paths(scan(root, use_gitignore=False).files) == {"a.py"}


class TestExtraCategories:
    def test_documentation_is_separate_from_source(self, make_project):
        root = make_project(
            {"README.md": "# Title\n", "docs/guide.md": "# Guide\n", "app.py": "x = 1\n"}
        )
        result = scan(root)
        assert rel_paths(result.files) == {"app.py"}
        assert rel_paths(result.docs) == {"README.md", "docs/guide.md"}

    def test_dependencies_are_extracted(self, make_project):
        root = make_project({"package.json": '{"dependencies": {"react": "^18.0.0"}}'})
        deps = scan(root).dependencies
        assert [(d.name, d.version, d.ecosystem) for d in deps] == [
            ("react", "^18.0.0", "node")
        ]

    def test_manifest_is_not_counted_as_source(self, make_project):
        root = make_project({"pyproject.toml": '[project]\ndependencies = ["x"]\n'})
        assert scan(root).files == []


class TestProgress:
    def test_callback_receives_every_candidate(self, make_project):
        root = make_project({"a.py": "x = 1\n", "b.py": "x = 1\n"})
        seen = []
        scan(root, on_progress=lambda count, path: seen.append((count, path)))
        assert [count for count, _ in seen] == [1, 2]
