"""Protect the local browser boundary as well as the retrieval sequence.

HTTP requests must not turn a local code viewer into a filesystem service for
other websites. Real pipeline fixtures also catch stale selections after a
project refresh, which ordinary command-line tests cannot exercise.

The question-answering tests use a scripted model for the same reason the
command-line ones do, with one addition: here it answers on a background
thread, so they also cover the part a caller gets wrong - a page that polls
forever because nothing ever set done.
"""

from http.client import HTTPConnection
import json
import re
from threading import Event, Thread
import time
from unittest.mock import Mock

import pytest

from aetron import web


@pytest.fixture
def workspace(tmp_path, make_project):
    root = make_project({"combat.py": "def combat():\n    return 123\n"})
    ws = web.Workspace(tmp_path / "history.json")
    ws.request("open", {"path": str(root)})
    return ws


def call(ws, action, **data):
    return ws.request(action, {"revision": ws.revision, **data})


def test_progressive_disclosure_and_cache(workspace, monkeypatch):
    monkeypatch.setattr(web, "scan", Mock(side_effect=AssertionError("unexpected scan")))
    with pytest.raises(ValueError, match="outline first"):
        call(workspace, "source", path="combat.py", name="combat")
    # The page is the owner's: any indexed file can be outlined, and nothing
    # outside the index can. The model's rationing lives in ask.py.
    with pytest.raises(ValueError, match="not in this project's index"):
        call(workspace, "structure", path="../secrets.py")
    found = call(workspace, "search", query="combat")
    assert found["candidates"][0]["path"] == "combat.py"
    assert "return 123" not in str(found)
    outline = call(workspace, "structure", path="combat.py")
    assert "return 123" not in str(outline)
    result = call(workspace, "source", path="combat.py", name="combat")
    assert "return 123" in result["text"]
    assert result["location"] == "combat.py:1"
    assert "Limitations" in call(workspace, "summary")["text"]


def test_refresh_invalidates_old_selections(workspace):
    call(workspace, "search", query="combat")
    call(workspace, "structure", path="combat.py")
    old = workspace.revision
    call(workspace, "refresh")
    with pytest.raises(ValueError, match="project changed"):
        workspace.request("source", {"revision": old, "path": "combat.py", "name": "combat"})
    with pytest.raises(ValueError, match="outline first"):
        call(workspace, "source", path="combat.py", name="combat")


def test_failed_open_preserves_project(workspace):
    root = workspace.scanned.root
    with pytest.raises(ValueError):
        call(workspace, "open", path=str(root / "missing"))
    assert workspace.scanned.root == root
    assert workspace.state()["recent"] == [str(root)]


def test_a_new_search_keeps_an_open_outline(workspace):
    """The explorer is browsed from a tree now; searching for something else
    must not close the file the person was reading."""
    call(workspace, "structure", path="combat.py")
    assert call(workspace, "search", query="nothinghere")["candidates"] == []
    assert "return 123" in call(workspace, "source", path="combat.py", name="combat")["text"]


@pytest.fixture
def http(workspace):
    server = web.make_server(workspace)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()

    def request(method, path, body=None, headers=None):
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read().decode(), dict(response.getheaders())
        finally:
            connection.close()

    yield request
    server.shutdown()
    server.server_close()
    worker.join()


def test_page_and_authenticated_api(http):
    status, page, headers = http("GET", "/")
    assert status == 200
    token = re.search(r'<script nonce="([^"]+)"', page)[1]
    assert token in headers["Content-Security-Policy"]
    status, body, _ = http("POST", "/api/state", "{}", {"X-Aetron-Token": token})
    assert status == 200
    assert json.loads(body)["files"] == 1


def test_cross_site_and_arbitrary_file_access_are_refused(http):
    assert http("POST", "/api/state", "{}")[0] == 403
    assert http("GET", "/", headers={"Host": "evil.example"})[0] == 403
    assert http("GET", "/", headers={"Origin": "https://evil.example"})[0] == 403
    assert http("GET", "/../../combat.py")[0] == 404


def test_malformed_request_does_not_stop_server(http):
    _, page, _ = http("GET", "/")
    token = re.search(r'<script nonce="([^"]+)"', page)[1]
    headers = {"X-Aetron-Token": token}
    assert http("POST", "/api/state", "[", headers)[0] == 400
    assert http("POST", "/api/state", "[]", headers)[0] == 400
    assert http("POST", "/api/state", "{}", headers)[0] == 200


class Scripted:
    """A model that says exactly what it was told to, on its own thread."""

    name, model = "scripted", "test"

    def __init__(self, *replies):
        self.replies = list(replies)

    def complete(self, system, messages):
        if not self.replies:
            raise web.ProviderError("the model ran out of replies")
        return self.replies.pop(0)


def use_model(monkeypatch, *replies):
    monkeypatch.setattr(web, "get_provider", lambda *a, **k: Scripted(*replies))


def wait_for_answer(ws, timeout=10.0):
    """Poll the way the page does, and fail loudly rather than hanging."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = call(ws, "ask_status")
        if state["done"]:
            return state
        time.sleep(0.02)
    raise AssertionError("the question was never answered")


@pytest.fixture
def game(tmp_path, make_project):
    root = make_project(
        {
            "Player/PlayerMovement.cs": (
                "public class PlayerMovement\n"
                "{\n"
                "    void HandleWasdInput()\n"
                "    {\n"
                "        Translate(Input.GetAxis(\"Horizontal\"));\n"
                "    }\n"
                "}\n"
            )
        }
    )
    ws = web.Workspace(tmp_path / "history.json")
    ws.request("open", {"path": str(root)})
    return ws


def test_a_question_returns_a_definition_the_page_can_show(game, monkeypatch):
    use_model(
        monkeypatch,
        "SEARCH movement",
        "STRUCTURE Player/PlayerMovement.cs",
        "SOURCE Player/PlayerMovement.cs PlayerMovement.HandleWasdInput",
        "ANSWER Movement is in Player/PlayerMovement.cs, HandleWasdInput at line 3.",
    )
    started = call(game, "ask", question="where is movement?")
    assert started["provider"] == "scripted (test)"

    answer = wait_for_answer(game)["answer"]
    citation = answer["citation"]
    assert citation["location"] == "Player/PlayerMovement.cs:3"
    assert citation["qualified_name"] == "PlayerMovement.HandleWasdInput"
    assert citation["confidence"] == 100
    assert "GetAxis" in citation["text"]
    assert answer["files_read"] == ["Player/PlayerMovement.cs"]


def test_the_steps_are_visible_while_they_happen(game, monkeypatch):
    use_model(
        monkeypatch,
        "SEARCH movement",
        "ANSWER Movement is in Player/PlayerMovement.cs.",
    )
    call(game, "ask", question="where is movement?")
    steps = wait_for_answer(game)["steps"]
    assert [step["command"] for step in steps] == ["SEARCH", "ANSWER"]
    # What the model asked for, not what came back: a skeleton belongs on the
    # page as a skeleton, not as a line of someone else's search results.
    assert steps[0]["argument"] == "movement"
    assert steps[0]["note"] == ""


def test_a_model_that_is_not_running_says_so(game, monkeypatch):
    def refuse(*args, **kwargs):
        raise web.ProviderError("Could not reach Ollama. Is it running?")

    monkeypatch.setattr(web, "get_provider", refuse)
    with pytest.raises(ValueError, match="Could not reach Ollama"):
        call(game, "ask", question="where is movement?")


def test_a_model_that_dies_mid_question_does_not_leave_the_page_waiting(
    game, monkeypatch
):
    use_model(monkeypatch)  # no replies: the first request fails
    call(game, "ask", question="where is movement?")
    assert "ran out of replies" in wait_for_answer(game)["answer"]["incomplete"]


def test_rescanning_is_refused_while_a_question_is_in_flight(game, monkeypatch):
    # A real job is a thread holding the index it was started with. Blocking
    # the model rather than faking the flag is what makes this the actual race:
    # a rescan here would swap the project out from under a live question.
    released = Event()

    class Slow(Scripted):
        def complete(self, system, messages):
            released.wait(timeout=5)
            return super().complete(system, messages)

    monkeypatch.setattr(web, "get_provider", lambda *a, **k: Slow("ANSWER done"))
    call(game, "ask", question="where is movement?")
    try:
        with pytest.raises(ValueError, match="still being answered"):
            call(game, "refresh")
    finally:
        released.set()
    wait_for_answer(game)
    call(game, "refresh")


def test_an_empty_question_is_refused_before_a_model_is_asked(game, monkeypatch):
    use_model(monkeypatch, "ANSWER nothing")
    with pytest.raises(ValueError, match="Ask a question"):
        call(game, "ask", question="   ")


def test_a_cited_file_opens_in_the_explorer_without_searching_again(game, monkeypatch):
    use_model(
        monkeypatch,
        "SEARCH movement",
        "STRUCTURE Player/PlayerMovement.cs",
        "ANSWER Movement is in Player/PlayerMovement.cs, HandleWasdInput at line 3.",
    )
    call(game, "ask", question="where is movement?")
    wait_for_answer(game)

    outline = call(game, "structure", path="Player/PlayerMovement.cs")
    assert outline["rel_path"] == "Player/PlayerMovement.cs"


def test_an_unexpected_error_from_a_provider_still_finishes_the_question(
    game, monkeypatch
):
    """The Anthropic SDK raises a bare TypeError when it finds no key. It
    escaped the worker, the job never finished, and every rescan after it was
    refused as though the question were still running."""

    class NoKey(Scripted):
        def complete(self, system, messages):
            raise TypeError("Could not resolve authentication method.")

    monkeypatch.setattr(web, "get_provider", lambda *a, **k: NoKey())
    call(game, "ask", question="where is movement?")
    finished = wait_for_answer(game)
    assert "Could not resolve authentication method" in finished["error"]
    call(game, "refresh")


def test_the_page_is_told_which_providers_stay_on_this_machine(game):
    state = game.request("state", {})
    assert state["local_providers"] == ["ollama"]
    assert "openai" in state["providers"]


def test_a_function_named_after_its_file_opens_alone(tmp_path, make_project):
    """The file's own module symbol shares the name and used to win, so the
    explorer showed the whole file under the function's heading."""
    root = make_project(
        {"ask.py": '"""Asking."""\n\n\ndef ask():\n    return 1\n\n\ndef other():\n    return 2\n'}
    )
    ws = web.Workspace(tmp_path / "history.json")
    ws.request("open", {"path": str(root)})
    call(ws, "search", query="ask")
    call(ws, "structure", path="ask.py")
    source = call(ws, "source", path="ask.py", name="ask")
    assert source["location"] == "ask.py:4"
    assert "other" not in source["text"]
    assert source["problem"] == ""


class Thinking(Scripted):
    """A model that thinks aloud and reports what it cost."""

    def complete(self, system, messages):
        self.last_thinking = "The map says the movement script is PlayerMovement.cs."
        self.last_usage = (500, 10)
        return super().complete(system, messages)


def test_the_tree_lists_every_file_and_marks_what_matters(game):
    files = call(game, "tree")["files"]
    assert [f["path"] for f in files] == ["Player/PlayerMovement.cs"]
    entry = files[0]
    assert entry["important"] and entry["parsed"]
    assert entry["definitions"] == 2
    assert "class PlayerMovement" in entry["describe"]


def test_a_question_carries_its_effort_thinking_and_cost(game, monkeypatch):
    seen = {}

    def provider(name, model=None, **kwargs):
        seen.update(kwargs)
        return Thinking("STRUCTURE Player/PlayerMovement.cs", "ANSWER In PlayerMovement.HandleWasdInput, line 3.")

    monkeypatch.setattr(web, "get_provider", provider)
    call(game, "ask", question="where is movement?", effort="high")
    state = wait_for_answer(game)
    assert seen["effort"] == "high"
    assert state["effort"] == "high"
    assert state["steps"][0]["thought"].startswith("The map says")
    answer = state["answer"]
    assert (answer["tokens_in"], answer["tokens_out"]) == (1000, 20)
    assert not answer["tokens_estimated"]


def test_an_unknown_effort_is_refused(game, monkeypatch):
    use_model(monkeypatch, "ANSWER x")
    with pytest.raises(ValueError, match="Unknown effort"):
        call(game, "ask", question="q", effort="ludicrous")


def test_a_follow_up_is_asked_with_the_earlier_answer(game, monkeypatch):
    calls = []

    class Recorder(Scripted):
        def complete(self, system, messages):
            calls.append(messages[0].content)
            return super().complete(system, messages)

    monkeypatch.setattr(web, "get_provider", lambda *a, **k: Recorder("ANSWER It is in PlayerMovement.cs."))
    call(game, "ask", question="where is movement?")
    wait_for_answer(game)
    monkeypatch.setattr(web, "get_provider", lambda *a, **k: Recorder("ANSWER Line 3."))
    call(game, "ask", question="which line?")
    wait_for_answer(game)
    assert "Earlier question: where is movement?" in calls[-1]
    assert game.request("state", {})["conversation"] == 2
    call(game, "clear_conversation")
    assert game.request("state", {})["conversation"] == 0


def test_a_summary_is_written_once_and_remembered(game, monkeypatch, tmp_path):
    use_model(monkeypatch, "ANSWER This project seems to be a small Unity movement demo.")
    call(game, "summarize")
    wait_for_answer(game)
    summary = game.request("state", {})["summary"]
    assert summary["text"].startswith("This project seems to be")
    assert summary["fresh"]

    # A new page on the same machine finds it without asking a model again.
    again = web.Workspace(tmp_path / "history.json")
    again.request("open", {"path": str(game.scanned.root)})
    assert again.request("state", {})["summary"]["text"] == summary["text"]
    assert not (game.scanned.root / "summaries").exists()


def test_the_summary_is_given_to_later_questions(game, monkeypatch):
    use_model(monkeypatch, "ANSWER This project seems to be a movement demo.")
    call(game, "summarize")
    wait_for_answer(game)
    systems = []

    class Recorder(Scripted):
        def complete(self, system, messages):
            systems.append(system)
            return super().complete(system, messages)

    monkeypatch.setattr(web, "get_provider", lambda *a, **k: Recorder("ANSWER ok"))
    call(game, "ask", question="where is movement?")
    wait_for_answer(game)
    assert "Summary from an earlier look: This project seems to be a movement demo." in systems[0]


def test_installed_models_are_offered(workspace, monkeypatch):
    from aetron.ai_providers import ollama

    monkeypatch.setattr(ollama, "list_models", lambda host=ollama.DEFAULT_HOST: [
        {"name": "qwen2.5-coder:latest", "size": 1, "parameters": "7.6B", "family": "qwen2", "capabilities": ["completion"]}
    ])
    offered = workspace.request("models", {"provider": "ollama"})
    assert offered["reachable"]
    assert offered["models"][0]["name"] == "qwen2.5-coder:latest"
    monkeypatch.setattr(ollama, "list_models", lambda host=ollama.DEFAULT_HOST: None)
    assert not workspace.request("models", {"provider": "ollama"})["reachable"]
    assert workspace.request("models", {"provider": "openai"})["default"] == ""
