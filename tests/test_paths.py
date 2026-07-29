"""Tests for normalising whatever the user types as a path."""

import pytest

from aetron.scanner.paths import InvalidPathError, normalize_path


def test_plain_directory(tmp_path):
    assert normalize_path(str(tmp_path)) == tmp_path.resolve()


def test_surrounding_double_quotes_are_stripped(tmp_path):
    # Windows "Copy as path" wraps the value in quotes.
    assert normalize_path(f'"{tmp_path}"') == tmp_path.resolve()


def test_surrounding_single_quotes_are_stripped(tmp_path):
    assert normalize_path(f"'{tmp_path}'") == tmp_path.resolve()


def test_surrounding_whitespace_is_stripped(tmp_path):
    assert normalize_path(f"  {tmp_path}  ") == tmp_path.resolve()


def test_home_shortcut_is_expanded():
    assert normalize_path("~").is_absolute()


def test_relative_path_becomes_absolute(tmp_path, monkeypatch):
    (tmp_path / "sub").mkdir()
    monkeypatch.chdir(tmp_path)
    assert normalize_path("sub") == (tmp_path / "sub").resolve()


class TestRejections:
    def test_empty_input(self):
        with pytest.raises(InvalidPathError, match="No path"):
            normalize_path("   ")

    def test_missing_path(self, tmp_path):
        with pytest.raises(InvalidPathError, match="does not exist"):
            normalize_path(str(tmp_path / "nope"))

    def test_file_instead_of_directory(self, tmp_path):
        target = tmp_path / "file.py"
        target.write_text("x = 1", encoding="utf-8")
        with pytest.raises(InvalidPathError, match="Not a directory"):
            normalize_path(str(target))
