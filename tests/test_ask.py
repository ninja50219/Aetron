"""Tests for driving the retrieval protocol with a model.

No model is involved. A scripted provider replays a fixed sequence of replies,
which is what makes the interesting cases testable at all: a model that skips a
level, a model that wraps its command in prose, a model that never answers.
"""

import pytest

from aetron.ai_providers import ProviderError
from aetron.ai_providers.base import Provider
from aetron.analyzer import analyze
from aetron.ask import Answer, ask, parse_command
from aetron.scanner import scan


class Scripted(Provider):
    """A model that says exactly what it was told to say."""

    name = "scripted"
    model = "test"

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    def complete(self, system: str, messages: list) -> str:
        self.prompts.append(messages[-1].content)
        if not self.replies:
            return "ANSWER I have run out of things to say."
        return self.replies.pop(0)


class Broken(Provider):
    name = "broken"
    model = "test"

    def complete(self, system: str, messages: list) -> str:
        raise ProviderError("the model is not running")


PROJECT = {
    "auth/login.py": (
        "class LoginController:\n"
        "    def login_handler(self, user, password):\n"
        "        return check(password)\n"
    ),
    "ui/view.py": "def render():\n    return 'page'\n",
}


@pytest.fixture
def project(make_project):
    def _build(layout=None):
        root = make_project(layout or PROJECT)
        scan_result = scan(root)
        return scan_result, analyze(scan_result)

    return _build


def commands(answer: Answer):
    return [(s.command, s.refused) for s in answer.steps]


class TestParsingAReply:
    def test_a_bare_command(self):
        assert parse_command("SEARCH login") == ("SEARCH", "login")

    def test_a_fenced_command(self):
        assert parse_command("```\nSEARCH login\n```") == ("SEARCH", "login")

    def test_a_command_after_prose(self):
        assert parse_command("Sure, let me look.\nSEARCH login") == ("SEARCH", "login")

    def test_lowercase_and_a_colon(self):
        assert parse_command("search: login") == ("SEARCH", "login")

    def test_a_reply_with_no_command(self):
        assert parse_command("I think it is in auth.py") == ("", "")

    def test_an_answer_keeps_its_whole_text(self):
        command, argument = parse_command("ANSWER It is in a.py\nat line 4.")
        assert command == "ANSWER"
        assert "line 4" in argument


class TestAFullRun:
    def test_the_protocol_end_to_end(self, project):
        scan_result, analysis = project()
        model = Scripted(
            "SEARCH login",
            "STRUCTURE auth/login.py",
            "SOURCE auth/login.py login_handler",
            "ANSWER Login is in auth/login.py, login_handler at line 2.",
        )
        answer = ask(model, scan_result, analysis, "where is login?")

        assert "auth/login.py" in answer.text
        assert commands(answer) == [
            ("SEARCH", False),
            ("STRUCTURE", False),
            ("SOURCE", False),
            ("ANSWER", False),
        ]

    def test_what_the_answer_cost_is_recorded(self, project):
        scan_result, analysis = project()
        model = Scripted(
            "SEARCH login",
            "STRUCTURE auth/login.py",
            "SOURCE auth/login.py login_handler",
            "ANSWER Found it.",
        )
        answer = ask(model, scan_result, analysis, "where is login?")
        assert answer.files_read == ["auth/login.py"]

    def test_an_answer_without_reading_any_source(self, project):
        """Often the structure is enough, and then no code leaves the project."""
        scan_result, analysis = project()
        model = Scripted("SEARCH login", "STRUCTURE auth/login.py", "ANSWER line 2.")
        answer = ask(model, scan_result, analysis, "where is login?")
        assert answer.files_read == []

    def test_steps_are_reported_as_they_happen(self, project):
        scan_result, analysis = project()
        seen = []
        model = Scripted("SEARCH login", "ANSWER done")
        ask(model, scan_result, analysis, "q", on_step=seen.append)
        assert [s.command for s in seen] == ["SEARCH", "ANSWER"]


class TestTheRulesAreEnforced:
    """A prompt is a request. These are the rules that matter, so they are
    checked in code - a model that ignored them would get source code it never
    justified asking for."""

    def test_source_without_structure_is_refused(self, project):
        scan_result, analysis = project()
        model = Scripted(
            "SEARCH login",
            "SOURCE auth/login.py login_handler",
            "ANSWER gave up",
        )
        answer = ask(model, scan_result, analysis, "where is login?")
        source_step = next(s for s in answer.steps if s.command == "SOURCE")
        assert source_step.refused
        assert "STRUCTURE" in source_step.observation

    def test_structure_of_an_unsearched_file_is_refused(self, project):
        scan_result, analysis = project()
        model = Scripted("STRUCTURE auth/login.py", "ANSWER gave up")
        answer = ask(model, scan_result, analysis, "where is login?")
        assert answer.steps[0].refused
        assert "SEARCH" in answer.steps[0].observation

    def test_a_refusal_returns_no_code(self, project):
        scan_result, analysis = project()
        model = Scripted("SOURCE auth/login.py login_handler", "ANSWER gave up")
        answer = ask(model, scan_result, analysis, "where is login?")
        assert "check(password)" not in answer.steps[0].observation

    def test_a_refusal_says_what_to_do_instead(self, project):
        """A refusal the model cannot act on just wastes a step."""
        scan_result, analysis = project()
        model = Scripted("STRUCTURE auth/login.py", "SEARCH login", "ANSWER ok")
        answer = ask(model, scan_result, analysis, "q")
        assert "SEARCH for it first" in answer.steps[0].observation
        assert answer.steps[1].refused is False

    def test_the_rules_can_be_satisfied_after_being_refused(self, project):
        scan_result, analysis = project()
        model = Scripted(
            "SOURCE auth/login.py login_handler",
            "SEARCH login",
            "STRUCTURE auth/login.py",
            "SOURCE auth/login.py login_handler",
            "ANSWER auth/login.py line 2",
        )
        answer = ask(model, scan_result, analysis, "q")
        assert [s.refused for s in answer.steps] == [True, False, False, False, False]


class TestWhenThingsGoWrong:
    def test_a_model_that_never_answers_is_stopped(self, project):
        scan_result, analysis = project()
        model = Scripted(*["SEARCH login"] * 20)
        answer = ask(model, scan_result, analysis, "q", max_steps=4)
        assert answer.text == ""
        assert "did not reach an answer" in answer.incomplete
        assert len(answer.steps) == 4

    def test_a_reply_with_no_command_is_corrected(self, project):
        scan_result, analysis = project()
        model = Scripted("I think it is in auth.py", "SEARCH login", "ANSWER done")
        answer = ask(model, scan_result, analysis, "q")
        assert answer.steps[0].refused
        assert answer.text == "done"

    def test_a_provider_failure_is_reported_not_raised(self, project):
        scan_result, analysis = project()
        answer = ask(Broken(), scan_result, analysis, "q")
        assert answer.text == ""
        assert "not running" in answer.incomplete

    def test_a_search_that_finds_nothing_says_so(self, project):
        scan_result, analysis = project()
        model = Scripted("SEARCH kubernetes", "ANSWER not in this project")
        answer = ask(model, scan_result, analysis, "q")
        assert "Nothing in this project" in answer.steps[0].observation


class TestWhatTheModelIsTold:
    def test_the_project_is_described_without_listing_its_files(self, project):
        scan_result, analysis = project()
        model = Scripted("ANSWER done")
        ask(model, scan_result, analysis, "where is login?")
        opening = model.prompts[0]
        assert "2 files" in opening
        assert "auth/login.py" not in opening

    def test_the_question_is_passed_through(self, project):
        scan_result, analysis = project()
        model = Scripted("ANSWER done")
        ask(model, scan_result, analysis, "where is login?")
        assert "where is login?" in model.prompts[0]

    def test_unparsed_files_are_declared(self, project):
        scan_result, analysis = project({"a.py": "x = 1\n", "b.go": "package b\n"})
        model = Scripted("ANSWER done")
        ask(model, scan_result, analysis, "q")
        assert "no parser" in model.prompts[0]
