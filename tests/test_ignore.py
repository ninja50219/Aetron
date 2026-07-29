"""Tests for the four kinds of ignore rule.

This module holds the scanner's least visible logic: a wrong rule silently
removes real source code, which is the one failure the whole tool must avoid.
"""

import pytest

from aetron.scanner.ignore import (
    IgnoreRules,
    is_ignored_dir,
    is_project_root,
    load_rules,
)


@pytest.fixture
def rules(tmp_path):
    ignore_file = tmp_path / "ignore_dirs.txt"
    ignore_file.write_text(
        "\n".join(
            [
                "# comment line",
                "node_modules",
                "*.egg-info",
                "/Library",
                "Library/PackageCache",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return load_rules(ignore_file)


def test_load_rules_sorts_entries_into_buckets(rules: IgnoreRules):
    assert rules.names == frozenset({"node_modules"})
    assert rules.name_patterns == ("*.egg-info",)
    assert rules.anchored_paths == ("library",)
    assert rules.path_fragments == ("library/packagecache",)


def test_comments_and_blank_lines_are_ignored(rules: IgnoreRules):
    assert "# comment line" not in rules.names
    assert "" not in rules.names


class TestNameRules:
    def test_plain_name_matches_at_any_depth(self, rules):
        assert is_ignored_dir("node_modules", "a/b/node_modules", rules=rules)

    def test_matching_is_case_insensitive(self, rules):
        assert is_ignored_dir("Node_Modules", "Node_Modules", rules=rules)

    def test_wildcard_matches_generated_name(self, rules):
        assert is_ignored_dir("aetron.egg-info", "aetron.egg-info", rules=rules)

    def test_unrelated_name_is_kept(self, rules):
        assert not is_ignored_dir("src", "src", rules=rules)


class TestAnchoredRules:
    def test_matches_at_the_project_root(self, rules):
        assert is_ignored_dir("Library", "Library", "Library", rules=rules)

    def test_does_not_match_deeper_in_the_tree(self, rules):
        # The whole point of anchoring: src/Library may be real source code.
        assert not is_ignored_dir("Library", "src/Library", "src/Library", rules=rules)

    def test_matches_when_project_root_is_below_scan_root(self, rules):
        # Scanning a folder of projects must behave like scanning one project.
        assert is_ignored_dir("Library", "games/MyMC/Library", "Library", rules=rules)

    def test_ignored_without_a_path(self, rules):
        assert not is_ignored_dir("Library", rules=rules)


class TestFragmentRules:
    def test_matches_at_the_root(self, rules):
        assert is_ignored_dir(
            "PackageCache", "Library/PackageCache", "Library/PackageCache", rules=rules
        )

    def test_matches_at_any_depth(self, rules):
        assert is_ignored_dir(
            "PackageCache", "a/b/Library/PackageCache", "Library/PackageCache", rules=rules
        )

    def test_partial_fragment_does_not_match(self, rules):
        assert not is_ignored_dir("PackageCache", "Other/PackageCache", rules=rules)


class TestProjectRoot:
    @pytest.mark.parametrize(
        "entries",
        [
            {".git", "src"},
            {"ProjectSettings", "Assets", "Library"},
            {"package.json"},
            {"pyproject.toml"},
            {"Cargo.toml"},
        ],
    )
    def test_marker_makes_a_project_root(self, entries):
        assert is_project_root(entries)

    def test_plain_directory_is_not_a_project_root(self):
        assert not is_project_root({"src", "docs", "notes.txt"})

    def test_marker_match_is_case_insensitive(self):
        assert is_project_root({"projectsettings"})
        assert is_project_root({"PROJECTSETTINGS"})
