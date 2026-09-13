"""Tests for telling a declaration from a use, without an AST.

A parser built on patterns finds the name on the line that declares something
just as readily as the name on a line that calls it. Left alone, that makes
every definition its own user, and dead code detection - which asks exactly
"does anything refer to this?" - silently answers yes for everything.
"""

from aetron.analyzer import analyze
from aetron.analyzer.csharp_parser import parse as parse_cs
from aetron.analyzer.javascript_parser import parse as parse_js
from aetron.scanner import scan


class TestDeclarationsAreNotUses:
    def test_a_javascript_definition_does_not_use_itself(self):
        result = parse_js("export function lonely() { return 1; }\n", "a.js")
        assert "lonely" not in result.references

    def test_a_csharp_definition_does_not_use_itself(self):
        result = parse_cs("public class A\n{\n    public void Go() { }\n}\n", "a.cs")
        assert "Go" not in result.references

    def test_a_real_use_is_still_a_use(self):
        source = "function helper() { return 1; }\nfunction main() { return helper(); }\n"
        assert "helper" in parse_js(source, "a.js").references

    def test_a_recursive_function_uses_itself(self):
        source = "function walk(n) { return n ? walk(n - 1) : 0; }\n"
        assert "walk" in parse_js(source, "a.js").references

    def test_a_module_symbol_spends_nothing(self):
        """It is named after the file, not after anything written in it."""
        source = "import { helpers } from './x';\nexport const a = helpers;\n"
        assert "helpers" in parse_js(source, "helpers.js").references


class TestDeadCodeWorksInEveryLanguage:
    def test_unused_javascript_is_reported(self, make_project):
        layout = {
            "helpers.js": (
                "export function unusedThing() { return 1; }\n"
                "export function usedThing() { return 2; }\n"
            ),
            "app.js": (
                'import { usedThing } from "./helpers";\n'
                "export function main() { return usedThing(); }\n"
            ),
        }
        root = make_project(layout)
        found = {c.symbol.name for c in analyze(scan(root)).dead_code()}
        assert "unusedThing" in found
        assert "usedThing" not in found

    def test_unused_csharp_is_reported(self, make_project):
        layout = {
            "Svc.cs": (
                "public class Svc\n{\n"
                "    public void NeverCalled() { }\n"
                "    public void Called() { }\n}\n"
            ),
            "Caller.cs": (
                "public class Caller\n{\n"
                "    public void Go() { new Svc().Called(); }\n}\n"
            ),
        }
        root = make_project(layout)
        found = {c.symbol.name for c in analyze(scan(root)).dead_code()}
        assert "NeverCalled" in found
        assert "Called" not in found

    def test_a_framework_subclass_is_not_reported(self, make_project):
        """React calls render; the project never does. Detecting that
        structurally is what stopped 32 false positives in a Blender addon."""
        layout = {
            "LoginForm.jsx": (
                'import React from "react";\n\n'
                "export default class LoginForm extends React.Component {\n"
                "  handleSubmit(event) { return event; }\n"
                "  render() { return null; }\n}\n"
            )
        }
        root = make_project(layout)
        found = {c.symbol.name for c in analyze(scan(root)).dead_code()}
        assert "render" not in found
        assert "handleSubmit" not in found
