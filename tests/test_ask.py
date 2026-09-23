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


def crowded(layout):
    """``layout`` buried under enough other files that the map leaves it out.

    Four hundred files with three definitions each outrank a file with one,
    so the map's budget is spent before it reaches the files under test -
    which is how the rule that a file must be shown before it is read can
    still be tested now that the map shows most of a small project.
    """
    filler = {
        f"filler/f{i:03d}.py": "def a():\n    pass\n\n\ndef b():\n    pass\n\n\ndef c():\n    pass\n"
        for i in range(400)
    }
    return {**filler, **layout}


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

    def test_structure_of_a_file_never_shown_is_refused(self, project):
        scan_result, analysis = project(crowded(PROJECT))
        model = Scripted("STRUCTURE auth/login.py", "ANSWER gave up")
        answer = ask(model, scan_result, analysis, "where is login?")
        assert answer.steps[0].refused
        assert "SEARCH" in answer.steps[0].observation

    def test_a_file_the_map_showed_needs_no_search(self, project):
        """The map puts files forward exactly as a search does."""
        scan_result, analysis = project()
        model = Scripted("STRUCTURE auth/login.py", "ANSWER gave up")
        answer = ask(model, scan_result, analysis, "where is login?")
        assert not answer.steps[0].refused

    def test_a_refusal_returns_no_code(self, project):
        scan_result, analysis = project()
        model = Scripted("SOURCE auth/login.py login_handler", "ANSWER gave up")
        answer = ask(model, scan_result, analysis, "where is login?")
        assert "check(password)" not in answer.steps[0].observation

    def test_a_refusal_says_what_to_do_instead(self, project):
        """A refusal the model cannot act on just wastes a step."""
        scan_result, analysis = project(crowded(PROJECT))
        model = Scripted("STRUCTURE auth/login.py", "SEARCH login", "ANSWER ok")
        answer = ask(model, scan_result, analysis, "q")
        assert "SEARCH for a word in its name" in answer.steps[0].observation
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
        # Different searches, so it is the budget that stops it rather than
        # the guard against a model repeating itself.
        model = Scripted(*[f"SEARCH login{i}" for i in range(20)])
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
    def test_the_project_is_mapped_by_name_never_by_code(self, project):
        """The design changed on 2026-09-23: the first real model, starting
        from two lines about the project, searched "main" eleven times. It now
        starts from a map - names of files and definitions - and still never
        from code."""
        scan_result, analysis = project()
        model = Recording("ANSWER done")
        ask(model, scan_result, analysis, "where is login?")
        system, messages = model.calls[0]
        assert "2 files" in system
        assert "login.py" in system and "class LoginController" in system
        sent = system + "".join(m.content for m in messages)
        assert "check(password)" not in sent

    def test_the_question_is_passed_through(self, project):
        scan_result, analysis = project()
        model = Scripted("ANSWER done")
        ask(model, scan_result, analysis, "where is login?")
        assert "where is login?" in model.prompts[0]

    def test_unparsed_files_are_declared(self, project):
        scan_result, analysis = project({"a.py": "x = 1\n", "b.go": "package b\n"})
        model = Recording("ANSWER done")
        ask(model, scan_result, analysis, "q")
        assert "b.go: (no parser: name only)" in model.calls[0][0]


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


class TestAKeyInTheProjectNeverReachesTheModel:
    """Level 3 sends one definition, and one definition can hold a key. A
    hosted model is a third party, so the key is hidden on the way out."""

    def test_the_source_the_model_reads_has_the_key_hidden(self, project):
        # Built at runtime so no key-shaped literal is committed.
        key = "sk-proj-" + "Q1w2E3r4" * 5
        scan_result, analysis = project(
            {"config.py": f"def connect():\n    return client(api_key=\"{key}\")\n"}
        )
        model = Recording(
            "SEARCH connect",
            "STRUCTURE config.py",
            "SOURCE config.py connect",
            "ANSWER The key is hard-coded in config.py, connect at line 1.",
        )
        answer = ask(model, scan_result, analysis, "where is the api key set?")

        sent = "\n".join(
            system + "".join(m.content for m in messages) for system, messages in model.calls
        )
        assert key not in sent
        assert "[hidden by Aetron: OpenAI API key]" in sent
        # The person asking, on their own machine, still sees the real line.
        assert key in answer.citation.text


UNITY = {
    "Assets/Scripts/PlayerMovment.cs": (
        "public class PlayerMovment : MonoBehaviour\n"
        "{\n"
        "    void Update()\n"
        "    {\n"
        "        transform.Translate(Input.GetAxis(\"Horizontal\"), 0, 0);\n"
        "    }\n"
        "}\n"
    ),
    "Assets/Scripts/UI/HealthBar.cs": "public class HealthBar\n{\n    void Show() { }\n}\n",
}


class TestAFileNamedTheWayAModelWritesIt:
    """From the first real model to run the protocol, qwen2.5-coder in the
    page: it searched, was shown Assets/Scripts/PlayerMovment.cs, asked for
    STRUCTURE PlayerMovment.cs, was told the file had never come up in a
    search, searched again, and repeated that until twelve requests ran out."""

    def test_the_real_trail_now_gets_past_the_outline(self, project):
        scan_result, analysis = project(UNITY)
        model = Scripted(
            "SEARCH PlayerMovment",
            "STRUCTURE PlayerMovment.cs",
            "SOURCE PlayerMovment.cs PlayerMovment.Update",
            "ANSWER Movement is in PlayerMovment.cs, PlayerMovment.Update at line 3.",
        )
        answer = ask(model, scan_result, analysis, "where is my movment script")

        assert [s.refused for s in answer.steps] == [False, False, False, False]
        # Recorded under the path that was read, so the trail names the file.
        assert answer.steps[1].argument == "Assets/Scripts/PlayerMovment.cs"
        assert answer.files_read == ["Assets/Scripts/PlayerMovment.cs"]
        assert answer.citation.location == "Assets/Scripts/PlayerMovment.cs:3"
        assert answer.citation.confidence == 100

    def test_a_short_name_still_needs_a_search_first(self, project):
        """The rule the refusal enforced is unchanged: nothing is read that no
        search, map or listing put forward."""
        # Not a Unity component, which the map would rank first and show.
        scan_result, analysis = project(
            crowded({"Assets/Scripts/Inventory.cs": "public class Inventory\n{\n}\n"})
        )
        model = Scripted("STRUCTURE Inventory.cs", "ANSWER gave up")
        answer = ask(model, scan_result, analysis, "q")
        assert answer.steps[0].refused
        assert "use the path exactly as the results give it" in answer.steps[0].observation

    def test_a_name_that_fits_two_found_files_lists_both(self, project):
        scan_result, analysis = project(
            {"client/Input.cs": "class A { }\n", "server/Input.cs": "class B { }\n"}
        )
        model = Scripted("SEARCH input", "STRUCTURE Input.cs", "ANSWER gave up")
        answer = ask(model, scan_result, analysis, "q")
        refusal = answer.steps[1]
        assert refusal.refused
        assert "client/Input.cs" in refusal.observation
        assert "server/Input.cs" in refusal.observation

    def test_windows_separators_and_any_case_resolve(self, project):
        scan_result, analysis = project(UNITY)
        model = Scripted(
            "SEARCH PlayerMovment",
            "STRUCTURE assets\\scripts\\playermovment.cs",
            "ANSWER done",
        )
        answer = ask(model, scan_result, analysis, "q")
        assert not answer.steps[1].refused
        assert answer.steps[1].argument == "Assets/Scripts/PlayerMovment.cs"

    def test_a_path_with_a_space_can_be_read(self, project):
        """SOURCE split its argument at the first space, and Unity folders on
        Windows often have one."""
        scan_result, analysis = project(
            {"Assets/My Scripts/Jump.cs": "public class Jump\n{\n    void Leap() { }\n}\n"}
        )
        model = Scripted(
            "SEARCH jump",
            "STRUCTURE Assets/My Scripts/Jump.cs",
            "SOURCE Assets/My Scripts/Jump.cs Jump.Leap()",
            "ANSWER Jump.Leap at line 3.",
        )
        answer = ask(model, scan_result, analysis, "q")
        assert [s.refused for s in answer.steps] == [False, False, False, False]
        assert answer.files_read == ["Assets/My Scripts/Jump.cs"]


SCRIPTS = {
    "APPopen.py": 'def open_app(name):\n    pass\n\n\ndef main():\n    open_app("x")\n\n\nif __name__ == "__main__":\n    main()\n',
    "calibration.py": "def calibrate():\n    pass\n",
    "ScreenReader.py": "class ScreenReader:\n    def read(self):\n        pass\n",
}


class TestTheSecondRealRun:
    """qwen2.5-coder, asked "What starts the program?" of a folder of Python
    scripts, sent SEARCH startup and then SEARCH main eleven times."""

    def test_the_map_already_says_what_starts_it(self, project):
        scan_result, analysis = project(SCRIPTS)
        model = Recording("ANSWER APPopen.py starts it, main at line 5.")
        answer = ask(model, scan_result, analysis, "What starts the program?")
        assert "Starts at: APPopen.py (script)" in model.calls[0][0]
        assert answer.steps == [answer.steps[0]] and answer.text

    def test_a_repeated_request_is_refused_not_run_again(self, project):
        scan_result, analysis = project(SCRIPTS)
        model = Scripted("SEARCH startup", *["SEARCH main"] * 11)
        answer = ask(model, scan_result, analysis, "What starts the program?", max_steps=12)
        mains = [s for s in answer.steps if s.argument == "main"]
        assert not mains[0].refused
        assert all(s.refused for s in mains[1:])
        assert "You already asked for this (request 2)" in mains[1].observation

    def test_the_model_is_told_when_requests_run_out(self, project):
        scan_result, analysis = project(SCRIPTS)
        model = Recording("SEARCH a", "SEARCH b", "SEARCH c", "ANSWER done")
        ask(model, scan_result, analysis, "q", max_steps=4)
        last_observation = model.calls[3][1][-1].content
        assert "One request left: ANSWER now" in last_observation


class TestAskingForMoreOfTheMap:
    def test_files_lists_a_folder_and_makes_it_readable(self, project):
        scan_result, analysis = project(crowded({"deep/inner/kept.py": "def kept():\n    pass\n"}))
        model = Scripted("FILES deep/inner", "STRUCTURE deep/inner/kept.py", "ANSWER done")
        answer = ask(model, scan_result, analysis, "q")
        assert "kept.py: kept" in answer.steps[0].observation
        assert not answer.steps[1].refused

    def test_skipped_lists_what_the_index_left_out(self, project):
        scan_result, analysis = project({"a.py": "x = 1\n", "dist/app.min.js": "var a=1;" * 400})
        model = Scripted("SKIPPED", "ANSWER done")
        answer = ask(model, scan_result, analysis, "q")
        assert answer.steps[0].command == "SKIPPED"
        assert "dist" in answer.steps[0].observation

    def test_outline_is_understood_as_structure(self):
        """The page labels the step OUTLINE, and a model may copy the label."""
        assert parse_command("OUTLINE a.py") == ("STRUCTURE", "a.py")


class TestEffort:
    def test_each_effort_has_its_own_request_budget(self, project):
        from aetron.ask import EFFORTS

        scan_result, analysis = project(SCRIPTS)
        for name in ("low", "medium", "high"):
            model = Scripted(*[f"SEARCH word{i}" for i in range(30)])
            answer = ask(model, scan_result, analysis, "q", effort=name)
            assert len(answer.steps) == EFFORTS[name].max_steps
            assert answer.effort == name

    def test_low_effort_asks_for_the_command_alone(self, project):
        scan_result, analysis = project(SCRIPTS)
        low, medium = Recording("ANSWER a"), Recording("ANSWER b")
        ask(low, scan_result, analysis, "q", effort="low")
        ask(medium, scan_result, analysis, "q", effort="medium")
        assert "Reply with the command only" in low.calls[0][0]
        assert "THINK:" in medium.calls[0][0]

    def test_an_unknown_effort_falls_back_to_the_default(self, project):
        scan_result, analysis = project(SCRIPTS)
        assert ask(Scripted("ANSWER x"), scan_result, analysis, "q", effort="max").effort == "medium"


class TestThinking:
    def test_the_reason_line_becomes_the_steps_thought(self, project):
        scan_result, analysis = project(SCRIPTS)
        model = Scripted("THINK: the map says APPopen.py runs as a script\nSTRUCTURE APPopen.py", "ANSWER done")
        answer = ask(model, scan_result, analysis, "q")
        assert answer.steps[0].thought == "the map says APPopen.py runs as a script"
        assert answer.steps[0].command == "STRUCTURE"

    def test_a_thought_is_not_sent_back_to_the_model(self, project):
        """It was for the reader. Sending it back costs tokens every turn."""
        scan_result, analysis = project(SCRIPTS)
        model = Recording("THINK: a long reason\nSTRUCTURE APPopen.py", "ANSWER done")
        ask(model, scan_result, analysis, "q")
        assert model.calls[1][1][1].content == "STRUCTURE APPopen.py"

    def test_a_models_own_thinking_is_preferred(self, project):
        scan_result, analysis = project(SCRIPTS)

        class Thinker(Scripted):
            def complete(self, system, messages):
                self.last_thinking = "I should look at the script."
                return super().complete(system, messages)

        answer = ask(Thinker("STRUCTURE APPopen.py", "ANSWER done"), scan_result, analysis, "q")
        assert answer.steps[0].thought == "I should look at the script."


class TestAConversation:
    def test_earlier_questions_are_carried_as_text(self, project):
        scan_result, analysis = project(SCRIPTS)
        model = Recording("ANSWER done")
        ask(model, scan_result, analysis, "and what does it open?",
            history=[("What starts the program?", "APPopen.py, main at line 5.")])
        opening = model.calls[0][1][0].content
        assert "Earlier question: What starts the program?" in opening
        assert "Your answer: APPopen.py, main at line 5." in opening
        assert opening.endswith("Question: and what does it open?")

    def test_only_the_last_few_are_kept_and_long_answers_are_cut(self, project):
        from aetron.ask import HISTORY_ANSWER_CHARS, HISTORY_TURNS

        scan_result, analysis = project(SCRIPTS)
        model = Recording("ANSWER done")
        history = [(f"q{i}", "x" * 1000) for i in range(6)]
        ask(model, scan_result, analysis, "next", history=history)
        opening = model.calls[0][1][0].content
        assert opening.count("Earlier question") == HISTORY_TURNS
        assert "q0" not in opening
        assert "x" * (HISTORY_ANSWER_CHARS + 1) not in opening

    def test_an_earlier_summary_is_in_the_map(self, project):
        scan_result, analysis = project(SCRIPTS)
        model = Recording("ANSWER done")
        ask(model, scan_result, analysis, "q", notes="Desktop automation scripts.")
        assert "Summary from an earlier look: Desktop automation scripts." in model.calls[0][0]

    def test_the_stable_part_comes_first(self, project):
        """The instructions and the map are the same for every question about
        a project, so a provider's cache can reuse them; only the end changes."""
        scan_result, analysis = project(SCRIPTS)
        one, two = Recording("ANSWER a"), Recording("ANSWER b")
        ask(one, scan_result, analysis, "first question")
        ask(two, scan_result, analysis, "a different one")
        a, b = one.calls[0][0], two.calls[0][0]
        common = len(a.split("The question you are answering")[0])
        assert a[:common] == b[:common]


class TestALongConversationShrinks:
    def test_old_results_are_shortened_and_recent_ones_kept(self, project):
        from aetron.ask import EFFORTS

        big = {f"m{i}.py": "".join(f"def f{i}_{j}():\n    pass\n" for j in range(80)) for i in range(8)}
        scan_result, analysis = project(big)
        model = Recording(*[f"STRUCTURE m{i}.py" for i in range(8)], "ANSWER done")
        ask(model, scan_result, analysis, "q", effort="low", max_steps=9)

        last = model.calls[-1][1]
        results = [m.content for m in last[1:] if m.role == "user"]
        shortened = [r for r in results if r.startswith("[Earlier result, shortened")]
        assert shortened, "a long conversation should shrink"
        keep = EFFORTS["low"].keep_full
        assert all(not r.startswith("[Earlier result") for r in results[-keep:])
        assert sum(len(m.content) for m in last) <= EFFORTS["low"].conversation_budget + 5000

    def test_a_short_question_is_never_shortened(self, project):
        scan_result, analysis = project(SCRIPTS)
        model = Recording("STRUCTURE APPopen.py", "STRUCTURE calibration.py", "ANSWER done")
        ask(model, scan_result, analysis, "q")
        assert not any("shortened" in m.content for m in model.calls[-1][1])

    def test_a_shortened_result_may_be_asked_for_again(self, project):
        big = {f"m{i}.py": "".join(f"def f{i}_{j}():\n    pass\n" for j in range(80)) for i in range(8)}
        scan_result, analysis = project(big)
        replies = [f"STRUCTURE m{i}.py" for i in range(8)] + ["STRUCTURE m0.py", "ANSWER done"]
        answer = ask(Scripted(*replies), scan_result, analysis, "q", effort="low", max_steps=10)
        again = answer.steps[8]
        assert again.argument == "m0.py" and not again.refused


class TestWhatAQuestionCost:
    def test_an_estimate_when_the_provider_reports_nothing(self, project):
        scan_result, analysis = project(SCRIPTS)
        answer = ask(Scripted("STRUCTURE APPopen.py", "ANSWER done"), scan_result, analysis, "q")
        assert answer.tokens_in > 0 and answer.tokens_out > 0
        assert answer.tokens_estimated

    def test_the_providers_own_count_when_it_has_one(self, project):
        scan_result, analysis = project(SCRIPTS)

        class Counting(Scripted):
            def complete(self, system, messages):
                self.last_usage = (1000, 7)
                return super().complete(system, messages)

        answer = ask(Counting("STRUCTURE APPopen.py", "ANSWER done"), scan_result, analysis, "q")
        assert (answer.tokens_in, answer.tokens_out) == (2000, 14)
        assert not answer.tokens_estimated


class TestTheSummary:
    def test_a_summary_is_a_low_effort_question(self, project):
        from aetron.ask import SUMMARY_QUESTION, summarize

        scan_result, analysis = project(SCRIPTS)
        model = Recording("ANSWER This project seems to be a set of desktop automation scripts.")
        answer = summarize(model, scan_result, analysis)
        assert answer.question == SUMMARY_QUESTION
        assert answer.effort == "low"
        assert answer.text.startswith("This project seems to be")


class TestAFileTheMapLedTo:
    def test_scores_like_one_a_search_returned(self, project):
        """Found in the browser: an answer walked perfectly from the map
        scored 75, because only a search could pass the first check - and
        the map is now the route a model is meant to take."""
        scan_result, analysis = project(SCRIPTS)
        model = Scripted("STRUCTURE APPopen.py", "SOURCE APPopen.py main", "ANSWER main in APPopen.py, line 5.")
        answer = ask(model, scan_result, analysis, "What starts the program?")
        assert answer.citation.confidence == 100
        ranked = next(c for c in answer.citation.checks if c.name == "ranked")
        assert ranked.detail == "The project map listed APPopen.py"


class TestALostModelIsStoppedEarly:
    def test_three_refusals_in_a_row_end_the_question(self, project):
        """The second real trail, replayed: once every repeat is refused, a
        model that keeps repeating is lost, and paying for more requests
        buys nothing."""
        scan_result, analysis = project(SCRIPTS)
        model = Scripted("SEARCH startup", *["SEARCH main"] * 11)
        answer = ask(model, scan_result, analysis, "What starts the program?", max_steps=12)
        assert len(answer.steps) == 5
        assert "refused 3 times in a row" in answer.incomplete

    def test_a_refusal_the_model_recovers_from_does_not_count(self, project):
        scan_result, analysis = project(crowded(PROJECT))
        model = Scripted(
            "STRUCTURE auth/login.py", "STRUCTURE auth/login.py",
            "SEARCH login", "STRUCTURE auth/login.py", "ANSWER auth/login.py line 2",
        )
        answer = ask(model, scan_result, analysis, "q")
        assert answer.text == "auth/login.py line 2"


REAL_SUMMARY = (
    "This project seems to be a Unity-based application for Minecraft, likely designed to "
    "enhance gameplay through automation and customization. It is built with C# and Python, "
    "with the main script starting at `myminecraft/Assets/Scripts/PlayerMovment.cs`."
)


class TestTheFirstRealSummary:
    """qwen2.5-coder 7B, the page's automatic summary, on the user's machine:
    it understood the project from the map and wrote a good summary - as
    plain text, then a bare ANSWER, then the plain text again, refused each
    time, until its budget ran out."""

    def test_plain_prose_is_the_summary(self, project):
        from aetron.ask import summarize

        scan_result, analysis = project(SCRIPTS)
        answer = summarize(Scripted(REAL_SUMMARY), scan_result, analysis)
        assert answer.text == REAL_SUMMARY
        assert len(answer.steps) == 1 and not answer.steps[0].refused
        assert answer.steps[0].thought == ""

    def test_the_questions_own_wording_asks_for_answer(self):
        from aetron.ask import SUMMARY_QUESTION

        assert "Reply as: ANSWER This project seems to be" in SUMMARY_QUESTION

    def test_a_bare_answer_after_prose_means_that_prose(self, project):
        """The real trail on an ordinary question: prose, then ANSWER alone."""
        scan_result, analysis = project(SCRIPTS)
        answer = ask(Scripted("APPopen.py starts it: main runs when the file is executed.", "ANSWER"),
                     scan_result, analysis, "What starts the program?")
        assert answer.text == "APPopen.py starts it: main runs when the file is executed."
        assert [s.refused for s in answer.steps] == [True, False]

    def test_prose_once_is_refused_with_the_line_that_would_work(self, project):
        scan_result, analysis = project(SCRIPTS)
        answer = ask(Scripted("APPopen.py starts it: main runs when the file is executed.", "ANSWER done"),
                     scan_result, analysis, "What starts the program?")
        assert "send it as: ANSWER APPopen.py starts it: main runs when the" in answer.steps[0].observation

    def test_prose_sent_twice_is_taken_as_the_answer(self, project):
        scan_result, analysis = project(SCRIPTS)
        prose = "APPopen.py starts it: main runs when the file is executed."
        answer = ask(Scripted(prose, prose), scan_result, analysis, "What starts the program?")
        assert answer.text == prose

    def test_a_plan_is_never_taken_as_an_answer(self, project):
        scan_result, analysis = project(SCRIPTS)
        plan = "Let me look at the structure of APPopen.py to find the entry point."
        answer = ask(Scripted(plan, plan, plan), scan_result, analysis, "What starts the program?")
        assert answer.text == ""
        assert "refused 3 times in a row" in answer.incomplete

    def test_three_refusals_of_any_kind_stop_the_question(self, project):
        """The first stop only counted refused commands; the real summary's
        refusals were for replies with no command and a bare ANSWER."""
        scan_result, analysis = project(SCRIPTS)
        answer = ask(Scripted("hmm", "ANSWER", "hmm", "ANSWER", "hmm"), scan_result, analysis, "q", max_steps=10)
        assert len(answer.steps) == 3
        assert "refused 3 times in a row" in answer.incomplete

    def test_the_rules_show_example_replies(self, project):
        scan_result, analysis = project(SCRIPTS)
        model = Recording("ANSWER x")
        ask(model, scan_result, analysis, "q")
        assert "ANSWER Movement is handled in src/player/Movement.cs" in model.calls[0][0]
