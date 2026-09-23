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

    def test_an_empty_answer_is_not_an_answer(self, project):
        """It would otherwise end the loop with nothing to show and no error,
        which reads as a successful empty answer."""
        scan_result, analysis = project()
        model = Scripted("ANSWER", "ANSWER auth/login.py line 2")
        answer = ask(model, scan_result, analysis, "q")
        assert answer.steps[0].refused
        assert answer.text == "auth/login.py line 2"

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


class Recording(Provider):
    """A model that keeps everything it was sent, not just the last message."""

    name = "recording"
    model = "test"

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, list]] = []

    def complete(self, system: str, messages: list) -> str:
        self.calls.append((system, list(messages)))
        return self.replies.pop(0) if self.replies else "ANSWER done"


class TestWhatSurvivesALongConversation:
    """Found on a real Ollama, which drops the oldest messages of a
    conversation longer than its context and keeps the system prompt."""

    def test_the_question_is_restated_in_the_system_prompt(self, project):
        from aetron.ask import SYSTEM_PROMPT

        scan_result, analysis = project()
        model = Recording("SEARCH login", "ANSWER done")
        ask(model, scan_result, analysis, "where is login?")
        for system, _ in model.calls:
            assert system.startswith(SYSTEM_PROMPT)
            assert "where is login?" in system

    def test_a_reply_that_is_not_a_command_goes_back_shortened(self, project):
        """A looping model sends a thousand tokens of nothing per turn."""
        from aetron.ask import ECHO_LIMIT

        scan_result, analysis = project()
        rambling = "xy" * 2000
        model = Recording(rambling, "ANSWER done")
        answer = ask(model, scan_result, analysis, "where is login?")

        echoed = model.calls[1][1][1]
        assert echoed.role == "assistant"
        assert echoed.content.startswith(rambling[:ECHO_LIMIT])
        assert len(echoed.content) < ECHO_LIMIT + 50
        assert "3700 more characters" in echoed.content
        assert answer.text == "done"

    def test_a_short_reply_goes_back_whole(self, project):
        scan_result, analysis = project()
        model = Recording("I think I should search first.", "ANSWER done")
        ask(model, scan_result, analysis, "where is login?")
        assert model.calls[1][1][1].content == "I think I should search first."


MOVEMENT = {
    "Player/PlayerMovement.cs": (
        "using UnityEngine;\n"
        "\n"
        "public class PlayerMovement : MonoBehaviour\n"
        "{\n"
        "    private float speed = 5f;\n"
        "\n"
        "    void HandleWasdInput()\n"
        "    {\n"
        "        float x = Input.GetAxis(\"Horizontal\");\n"
        "        transform.Translate(new Vector3(x, 0, 0) * speed);\n"
        "    }\n"
        "\n"
        "    void Jump()\n"
        "    {\n"
        "        body.AddForce(Vector3.up);\n"
        "    }\n"
        "}\n"
    ),
    "Systems/SaveSystem.cs": (
        "public class SaveSystem\n"
        "{\n"
        "    public void WriteSlot(int slot)\n"
        "    {\n"
        "        File.WriteAllText(\"save.json\", \"{}\");\n"
        "    }\n"
        "}\n"
    ),
}

WALKED = (
    "SEARCH movement",
    "STRUCTURE Player/PlayerMovement.cs",
    "SOURCE Player/PlayerMovement.cs PlayerMovement.HandleWasdInput",
    "ANSWER Movement is in Player/PlayerMovement.cs, HandleWasdInput at line 7.",
)


class TestTheAnswerPointsSomewhere:
    """An answer that cannot be opened is a paragraph, not an answer."""

    def test_a_walked_protocol_resolves_to_the_definition_that_was_read(self, project):
        scan_result, analysis = project(MOVEMENT)
        answer = ask(Scripted(*WALKED), scan_result, analysis, "where is movement?")

        citation = answer.citation
        assert citation is not None
        assert citation.rel_path == "Player/PlayerMovement.cs"
        assert citation.qualified_name == "PlayerMovement.HandleWasdInput"
        assert citation.kind == "method"
        assert citation.location == "Player/PlayerMovement.cs:7"
        assert "Input.GetAxis" in citation.text
        # One definition, not the file it lives in: Jump is next door and stays
        # there, which is the whole economy of the level this came from.
        assert "AddForce" not in citation.text

    def test_every_check_passing_is_what_a_hundred_means(self, project):
        scan_result, analysis = project(MOVEMENT)
        answer = ask(Scripted(*WALKED), scan_result, analysis, "where is movement?")

        assert answer.citation.confidence == 100
        assert answer.citation.checks_passed == 4
        assert all(check.passed for check in answer.citation.checks)

    def test_answering_from_an_outline_alone_scores_lower_than_reading_it(self, project):
        scan_result, analysis = project(MOVEMENT)
        answer = ask(
            Scripted(
                "SEARCH movement",
                "STRUCTURE Player/PlayerMovement.cs",
                "ANSWER Movement is in Player/PlayerMovement.cs, HandleWasdInput at line 7.",
            ),
            scan_result,
            analysis,
            "where is movement?",
        )

        assert answer.citation.qualified_name == "PlayerMovement.HandleWasdInput"
        assert answer.citation.confidence == 70
        failed = [check.name for check in answer.citation.checks if not check.passed]
        assert failed == ["read"]
        # The code is still shown. The person asking is not the model, and the
        # loop that the levels ration is over by the time this is fetched.
        assert "Input.GetAxis" in answer.citation.text

    def test_a_line_number_alone_finds_the_definition_around_it(self, project):
        scan_result, analysis = project(MOVEMENT)
        answer = ask(
            Scripted(
                "SEARCH movement",
                "STRUCTURE Player/PlayerMovement.cs",
                "ANSWER It is at Player/PlayerMovement.cs:9.",
            ),
            scan_result,
            analysis,
            "where is movement?",
        )

        assert answer.citation.qualified_name == "PlayerMovement.HandleWasdInput"

    def test_an_absent_feature_cites_nothing(self, project):
        scan_result, analysis = project(MOVEMENT)
        answer = ask(
            Scripted(
                "SEARCH multiplayer",
                "ANSWER This project contains no multiplayer code.",
            ),
            scan_result,
            analysis,
            "where is multiplayer?",
        )

        assert answer.text
        assert answer.citation is None

    def test_naming_a_file_it_never_looked_at_cites_nothing(self, project):
        scan_result, analysis = project(MOVEMENT)
        answer = ask(
            Scripted("ANSWER Movement is in Player/PlayerMovement.cs at line 7."),
            scan_result,
            analysis,
            "where is movement?",
        )

        # The sentence may even be right. It is still a guess, and a line
        # number beside a guess is the thing this project exists not to do.
        assert answer.citation is None

    def test_the_definition_named_in_the_answer_wins_over_the_rest(self, project):
        scan_result, analysis = project(MOVEMENT)
        answer = ask(
            Scripted(
                "SEARCH movement",
                "STRUCTURE Player/PlayerMovement.cs",
                "ANSWER Jumping is handled by Jump.",
            ),
            scan_result,
            analysis,
            "where is jumping?",
        )

        assert answer.citation.qualified_name == "PlayerMovement.Jump"

    def test_a_citation_survives_a_model_that_answers_in_prose(self, project):
        scan_result, analysis = project(MOVEMENT)
        answer = ask(
            Scripted(
                "SEARCH save",
                "STRUCTURE Systems/SaveSystem.cs",
                "SOURCE Systems/SaveSystem.cs SaveSystem.WriteSlot",
                "Sure!\n"
                "ANSWER Saving writes a slot in Systems/SaveSystem.cs, "
                "WriteSlot at line 3.",
            ),
            scan_result,
            analysis,
            "how does saving work?",
        )

        assert answer.citation.location == "Systems/SaveSystem.cs:3"
        assert answer.citation.confidence == 100

