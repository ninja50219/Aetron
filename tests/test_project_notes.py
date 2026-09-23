"""Tests for the summary Aetron keeps about a project between runs."""

import os
import time

from aetron import project_notes
from aetron.scanner import scan


def test_a_saved_summary_comes_back_fresh(make_project, tmp_path):
    root = make_project({"a.py": "x = 1\n"})
    saved = project_notes.save_summary(tmp_path / "notes", scan(root), "A small script.", "ollama (m)")
    loaded = project_notes.load_summary(tmp_path / "notes", scan(root))
    assert saved.fresh and loaded.fresh
    assert loaded.text == "A small script." and loaded.model == "ollama (m)"


def test_a_changed_project_marks_it_stale_not_gone(make_project, tmp_path):
    """A slightly old account of a project beats none; the page offers to
    rewrite it."""
    root = make_project({"a.py": "x = 1\n"})
    project_notes.save_summary(tmp_path / "notes", scan(root), "A small script.", "m")
    (root / "b.py").write_text("y = 2\n")
    loaded = project_notes.load_summary(tmp_path / "notes", scan(root))
    assert loaded.text == "A small script." and not loaded.fresh


def test_an_edited_file_changes_the_fingerprint(make_project):
    root = make_project({"a.py": "x = 1\n"})
    before = project_notes.fingerprint(scan(root))
    later = time.time() + 5
    (root / "a.py").write_text("x = 22\n")
    os.utime(root / "a.py", (later, later))
    assert project_notes.fingerprint(scan(root)) != before


def test_no_summary_is_none(make_project, tmp_path):
    root = make_project({"a.py": "x = 1\n"})
    assert project_notes.load_summary(tmp_path / "notes", scan(root)) is None


def test_it_is_never_written_inside_the_project(make_project, tmp_path):
    root = make_project({"a.py": "x = 1\n"})
    project_notes.save_summary(tmp_path / "notes", scan(root), "text", "m")
    assert sorted(p.name for p in root.iterdir()) == ["a.py"]


def test_an_unwritable_directory_costs_the_cache_not_the_answer(make_project, tmp_path):
    root = make_project({"a.py": "x = 1\n"})
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("")
    assert project_notes.save_summary(blocker / "notes", scan(root), "text", "m") is None
