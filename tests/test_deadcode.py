"""Tests for dead-code candidates.

False positives are the failure that matters here: a tool that cries wolf gets
switched off, so most of these tests assert that something is *not* reported.
"""

from aetron.analyzer import analyze
from aetron.analyzer.deadcode import Confidence
from aetron.scanner import scan


def candidates(make_project, layout):
    return analyze(scan(make_project(layout))).dead_code()


def names(found):
    return {c.symbol.qualified_name for c in found}


class TestDetection:
    def test_unreferenced_function(self, make_project):
        layout = {"a.py": "def used():\n    pass\n\ndef orphan():\n    pass\n", "b.py": "import a\na.used()\n"}
        assert names(candidates(make_project, layout)) == {"orphan"}

    def test_private_name_is_high_confidence(self, make_project):
        found = candidates(make_project, {"a.py": "def _helper():\n    pass\n"})
        assert [c.confidence for c in found] == [Confidence.HIGH]

    def test_called_function_is_not_reported(self, make_project):
        layout = {"a.py": "def helper():\n    pass\n\ndef run():\n    helper()\n"}
        assert "helper" not in names(candidates(make_project, layout))

    def test_method_called_on_an_instance(self, make_project):
        layout = {
            "a.py": "class Thing:\n    def act(self):\n        pass\n",
            "b.py": "from a import Thing\nThing().act()\n",
        }
        assert "Thing.act" not in names(candidates(make_project, layout))


class TestFalsePositiveGuards:
    def test_dunder_methods_are_never_reported(self, make_project):
        layout = {"a.py": "class A:\n    def __init__(self):\n        pass\n"}
        assert names(candidates(make_project, layout)) == {"A"}

    def test_framework_subclass_methods_are_skipped(self, make_project):
        # The real case: a Blender operator's execute/invoke/poll are called by
        # Blender, so the project never mentions them.
        layout = {
            "addon.py": (
                "import bpy\n"
                "class OBJECT_OT_Thing(bpy.types.Operator):\n"
                "    def execute(self, context):\n        pass\n"
                "    def invoke(self, context, event):\n        pass\n"
            )
        }
        found = candidates(make_project, layout)
        # The methods are silenced entirely; the class itself stays visible but
        # at low confidence, because an operator nobody registers is dead.
        assert names(found) == {"OBJECT_OT_Thing"}
        assert [c.confidence for c in found] == [Confidence.LOW]

    def test_indirect_framework_subclass(self, make_project):
        layout = {
            "a.py": (
                "import bpy\n"
                "class Base(bpy.types.Operator):\n    pass\n"
                "class Child(Base):\n"
                "    def execute(self):\n        pass\n"
            )
        }
        assert "Child.execute" not in names(candidates(make_project, layout))

    def test_subclass_of_a_project_class_is_still_checked(self, make_project):
        # Inheriting from our own class gives no framework excuse.
        layout = {
            "a.py": "class Base:\n    pass\n\nclass Child(Base):\n    def orphan(self):\n        pass\n",
            "b.py": "from a import Child\nChild()\n",
        }
        assert "Child.orphan" in names(candidates(make_project, layout))

    def test_test_functions_are_skipped(self, make_project):
        layout = {"tests/test_x.py": "def test_something():\n    pass\n"}
        assert names(candidates(make_project, layout)) == set()

    def test_lifecycle_hooks_are_low_confidence(self, make_project):
        found = candidates(make_project, {"addon.py": "def unregister():\n    pass\n"})
        assert [c.confidence for c in found] == [Confidence.LOW]

    def test_string_reference_counts_as_use(self, make_project):
        # __all__ and getattr() targets appear only as strings.
        layout = {"a.py": 'def exported():\n    pass\n\n__all__ = ["exported"]\n'}
        assert "exported" not in names(candidates(make_project, layout))


class TestReporting:
    def test_candidates_carry_location_and_reason(self, make_project):
        found = candidates(make_project, {"a.py": "\ndef orphan():\n    pass\n"})
        candidate = found[0]
        assert candidate.rel_path == "a.py"
        assert candidate.symbol.line == 2
        assert candidate.reason

    def test_sorted_by_file_then_line(self, make_project):
        layout = {
            "b.py": "def second():\n    pass\n",
            "a.py": "def first():\n    pass\n\ndef also_first():\n    pass\n",
        }
        found = candidates(make_project, layout)
        assert [(c.rel_path, c.symbol.line) for c in found] == [
            ("a.py", 1),
            ("a.py", 4),
            ("b.py", 1),
        ]
