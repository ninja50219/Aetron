"""Tests for resolving imports to files inside the project."""

import pytest

from aetron.analyzer.resolver import build_module_map, module_name, resolve
from aetron.analyzer.symbols import ImportRef


class TestModuleName:
    @pytest.mark.parametrize(
        ("rel_path", "expected"),
        [
            ("app.py", "app"),
            ("pkg/module.py", "pkg.module"),
            ("pkg/__init__.py", "pkg"),
            ("a/b/c.py", "a.b.c"),
            ("pkg\\windows.py", "pkg.windows"),
        ],
    )
    def test_dotted_name(self, rel_path, expected):
        assert module_name(rel_path) == expected


class TestModuleMap:
    def test_registers_full_and_suffix_names(self):
        modules = build_module_map(["aetron/scanner/ignore.py"])
        # The suffix forms let an import resolve when the scan root sits above
        # the package.
        assert modules["aetron.scanner.ignore"] == "aetron/scanner/ignore.py"
        assert modules["scanner.ignore"] == "aetron/scanner/ignore.py"
        assert modules["ignore"] == "aetron/scanner/ignore.py"

    def test_full_name_wins_over_a_suffix(self):
        modules = build_module_map(["ignore.py", "aetron/scanner/ignore.py"])
        assert modules["ignore"] == "ignore.py"

    def test_non_python_files_are_ignored(self):
        assert build_module_map(["main.cs", "app.js"]) == {}


class TestResolve:
    @pytest.fixture
    def modules(self):
        return build_module_map(
            [
                "pkg/__init__.py",
                "pkg/core.py",
                "pkg/sub/__init__.py",
                "pkg/sub/deep.py",
                "top.py",
            ]
        )

    def test_absolute_import(self, modules):
        reference = ImportRef(module="pkg.core")
        assert resolve(reference, "top.py", modules) == "pkg/core.py"

    def test_external_package_is_not_an_edge(self, modules):
        assert resolve(ImportRef(module="os.path"), "top.py", modules) is None

    def test_from_import_of_a_symbol(self, modules):
        reference = ImportRef(module="pkg.core", names=["Thing"], from_import=True)
        assert resolve(reference, "top.py", modules) == "pkg/core.py"

    def test_from_import_of_a_submodule_beats_the_package(self, modules):
        # "from pkg import core" must point at core.py, not pkg/__init__.py.
        reference = ImportRef(module="pkg", names=["core"], from_import=True)
        assert resolve(reference, "top.py", modules) == "pkg/core.py"

    def test_relative_sibling(self, modules):
        reference = ImportRef(module="core", level=1, from_import=True)
        assert resolve(reference, "pkg/other.py", modules) == "pkg/core.py"

    def test_relative_import_does_not_reach_a_different_package(self, modules):
        # One dot from pkg/sub means pkg.sub, which has no "top" module.
        reference = ImportRef(module="top", level=1, from_import=True)
        assert resolve(reference, "pkg/sub/deep.py", modules) is None

    def test_bare_relative_import(self, modules):
        reference = ImportRef(module="", names=["core"], level=1, from_import=True)
        assert resolve(reference, "pkg/other.py", modules) == "pkg/core.py"

    def test_relative_from_a_package_init(self, modules):
        # Inside __init__.py one dot is the package itself, not its parent.
        reference = ImportRef(module="core", level=1, from_import=True)
        assert resolve(reference, "pkg/__init__.py", modules) == "pkg/core.py"

    def test_parent_relative_import(self, modules):
        reference = ImportRef(module="core", level=2, from_import=True)
        assert resolve(reference, "pkg/sub/deep.py", modules) == "pkg/core.py"

    def test_deeper_relative_import(self, modules):
        reference = ImportRef(module="sub.deep", level=2, from_import=True)
        assert resolve(reference, "pkg/sub/deep.py", modules) == "pkg/sub/deep.py"
