"""Tests for level 3 of the retrieval protocol: the code of one definition."""

from aetron.analyzer import analyze
from aetron.context.source import get_source
from aetron.scanner import scan


def slice_of(make_project, layout, rel_path, name, **kwargs):
    root = make_project(layout)
    scan_result = scan(root)
    return get_source(scan_result, analyze(scan_result), rel_path, name, **kwargs)


class TestSlicing:
    def test_returns_only_the_definition(self, make_project):
        layout = {
            "a.py": "def before():\n    return 1\n\n\ndef wanted():\n    return 2\n\n\ndef after():\n    return 3\n"
        }
        text = slice_of(make_project, layout, "a.py", "wanted").text
        assert "wanted" in text
        assert "before" not in text
        assert "after" not in text

    def test_the_location_is_a_file_and_a_line(self, make_project):
        layout = {"a.py": "x = 1\n\n\ndef login():\n    pass\n"}
        assert slice_of(make_project, layout, "a.py", "login").location == "a.py:4"

    def test_a_method_is_found_by_its_qualified_name(self, make_project):
        layout = {"a.py": "class Login:\n    def handle(self):\n        return 'login'\n"}
        result = slice_of(make_project, layout, "a.py", "Login.handle")
        assert result.available
        assert "return 'login'" in result.text

    def test_a_method_is_also_found_by_its_plain_name(self, make_project):
        layout = {"a.py": "class Login:\n    def handle(self):\n        pass\n"}
        assert slice_of(make_project, layout, "a.py", "handle").available

    def test_a_qualified_name_does_not_match_another_class(self, make_project):
        layout = {
            "a.py": "class Session:\n    def handle(self):\n        return 'session'\n\n\n"
            "class Login:\n    def handle(self):\n        return 'login'\n"
        }
        result = slice_of(make_project, layout, "a.py", "Login.handle")
        assert "return 'login'" in result.text
        assert "return 'session'" not in result.text

    def test_numbered_output_starts_at_the_real_line(self, make_project):
        layout = {"a.py": "x = 1\ny = 2\n\n\ndef login():\n    pass\n"}
        assert slice_of(make_project, layout, "a.py", "login").numbered().startswith("5|")


class TestAttachedContext:
    def test_a_decorator_is_kept(self, make_project):
        layout = {"a.py": "import functools\n\n\n@functools.cache\ndef login():\n    pass\n"}
        assert "@functools.cache" in slice_of(make_project, layout, "a.py", "login").text

    def test_a_comment_directly_above_is_kept(self, make_project):
        layout = {"a.py": "x = 1\n\n\n# why this exists\ndef login():\n    pass\n"}
        assert "# why this exists" in slice_of(make_project, layout, "a.py", "login").text

    def test_the_previous_definition_is_not_reached_into(self, make_project):
        layout = {"a.py": "def before():\n    return 'private'\n\n\ndef login():\n    pass\n"}
        assert "private" not in slice_of(make_project, layout, "a.py", "login").text

    def test_a_blank_line_stops_the_lead_in(self, make_project):
        layout = {"a.py": "# unrelated banner\n\ndef login():\n    pass\n"}
        assert "unrelated banner" not in slice_of(make_project, layout, "a.py", "login").text


class TestStaleIndex:
    """An index is a photograph. Returning lines from a file that has changed
    since it was taken is the one way this design can quietly mislead."""

    def test_a_shortened_file_is_reported_not_sliced(self, make_project):
        root = make_project({"a.py": "def before():\n    pass\n\n\ndef login():\n    pass\n"})
        scan_result = scan(root)
        analysis = analyze(scan_result)

        (root / "a.py").write_text("x = 1\n", encoding="utf-8")

        result = get_source(scan_result, analysis, "a.py", "login")
        assert result.available is False
        assert "changed since it was indexed" in result.problem
        assert result.text == ""

    def test_a_moved_definition_is_reported(self, make_project):
        root = make_project({"a.py": "def login():\n    pass\n\n\ndef other():\n    pass\n"})
        scan_result = scan(root)
        analysis = analyze(scan_result)

        (root / "a.py").write_text(
            "def something_else():\n    pass\n\n\ndef other():\n    pass\n", encoding="utf-8"
        )

        result = get_source(scan_result, analysis, "a.py", "login")
        assert result.available is False
        assert "no longer defines" in result.problem

    def test_an_unchanged_file_is_not_flagged(self, make_project):
        layout = {"a.py": "def login():\n    pass\n"}
        assert slice_of(make_project, layout, "a.py", "login").available is True


class TestMissing:
    def test_an_unknown_name_says_so(self, make_project):
        result = slice_of(make_project, {"a.py": "def login():\n    pass\n"}, "a.py", "nope")
        assert result.available is False
        assert "no definition called" in result.problem

    def test_an_unknown_file_says_so(self, make_project):
        result = slice_of(make_project, {"a.py": "x = 1\n"}, "elsewhere.py", "login")
        assert result.available is False
        assert "not an analysed file" in result.problem

    def test_an_ambiguous_plain_name_is_flagged_rather_than_picked_silently(self, make_project):
        layout = {
            "a.py": "class A:\n    def handle(self):\n        return 1\n\n\n"
            "class B:\n    def handle(self):\n        return 2\n"
        }
        result = slice_of(make_project, layout, "a.py", "handle")
        assert "definitions share this name" in result.problem
        # Still returns the first, so an ambiguous ask is answered and labelled
        # rather than refused.
        assert result.text

    def test_a_qualified_name_resolves_what_a_plain_one_cannot(self, make_project):
        layout = {
            "a.py": "class A:\n    def handle(self):\n        return 1\n\n\n"
            "class B:\n    def handle(self):\n        return 2\n"
        }
        result = slice_of(make_project, layout, "a.py", "B.handle")
        assert result.problem == ""
        assert "return 2" in result.text

    def test_a_unique_name_is_not_flagged(self, make_project):
        layout = {"a.py": "def handle():\n    return 1\n"}
        assert slice_of(make_project, layout, "a.py", "handle").problem == ""


class TestCost:
    def test_one_definition_costs_its_own_lines(self, make_project):
        """The economy of the whole protocol: a method in a large file costs
        the method, not the file."""
        filler = "\n".join(f"def filler_{n}():\n    return {n}\n" for n in range(300))
        layout = {"big.py": f"{filler}\n\ndef wanted():\n    return 'here'\n"}
        root = make_project(layout)
        scan_result = scan(root)
        result = get_source(scan_result, analyze(scan_result), "big.py", "wanted")
        assert result.available
        assert len(result.text) < len(layout["big.py"]) / 100


class TestSuggestingWhatWasMeant:
    def test_a_mistyped_name_is_matched_on_its_plain_form(self, make_project):
        """"dealDamge" is nowhere near "CombatService.dealDamage" by any string
        measure, but the plain name is what the caller was reaching for."""
        layout = {"a.py": "class CombatService:\n    def deal_damage(self):\n        pass\n"}
        result = slice_of(make_project, layout, "a.py", "deal_damge")
        assert "did you mean CombatService.deal_damage?" in result.problem

    def test_a_name_nothing_resembles_gets_no_guess(self, make_project):
        layout = {"a.py": "def login():\n    pass\n"}
        assert "did you mean" not in slice_of(
            make_project, layout, "a.py", "kubernetes"
        ).problem
