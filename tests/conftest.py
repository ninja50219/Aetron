"""Shared fixtures for building throwaway project trees on disk.

The scanner is filesystem code, so most tests need a real directory. Building
one from a dict keeps each test readable: the layout is visible at a glance.
"""

from pathlib import Path

import pytest


@pytest.fixture
def make_project(tmp_path: Path):
    """Create a directory tree from a {relative path: content} mapping."""

    def _make(layout: dict[str, str], name: str = "project") -> Path:
        root = tmp_path / name
        root.mkdir(parents=True, exist_ok=True)

        for rel_path, content in layout.items():
            target = root / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        return root

    return _make
