"""Tests against a real Ollama daemon, skipped unless one is named.

    ollama pull qwen2.5-coder
    AETRON_OLLAMA_MODEL=qwen2.5-coder python -m pytest tests/test_ollama_live.py -v -s

Every other test in this directory uses a scripted provider, which is what
makes the loop testable at all - and is also why the provider went unexamined
until a real daemon was put in front of it and the system prompt turned out
never to have been sent. The mock agreed with the code about what Ollama
wanted; only Ollama could disagree.

These are split by what they depend on. The transport tests hold for any model,
including a random-weight probe that cannot write a word: they check what
Ollama is sent and what comes back. The protocol test depends on the model
being able to follow the command language at all, which is the open question
the project has been carrying since the first session. A model that fails it is
a finding about the model or about SYSTEM_PROMPT, and ``-s`` prints the trail
so the reason is visible.
"""

import json
import os
import urllib.error
import urllib.request

import pytest

from aetron.ai_providers.base import Message
from aetron.ai_providers.ollama import DEFAULT_HOST, OllamaProvider
from aetron.analyzer import analyze
from aetron.ask import SYSTEM_PROMPT, ask, parse_command
from aetron.scanner import scan

MODEL = os.environ.get("AETRON_OLLAMA_MODEL", "")
HOST = os.environ.get("AETRON_OLLAMA_HOST", DEFAULT_HOST)

pytestmark = pytest.mark.skipif(
    not MODEL, reason="set AETRON_OLLAMA_MODEL to run against a real Ollama"
)

PROJECT = {
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


@pytest.fixture
def provider():
    return OllamaProvider(model=MODEL, host=HOST)


def _prompt_tokens(provider, system):
    """How many tokens Ollama built its prompt from, for one request."""
    payload = provider.payload(system, [Message("user", "where is movement?")])
    payload["options"]["num_predict"] = 1
    request = urllib.request.Request(
        f"{provider.host}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=provider.timeout) as response:
            body = json.loads(response.read())
    except urllib.error.URLError as exc:
        pytest.fail(f"Ollama at {provider.host} did not answer: {exc}")
    if "prompt_eval_count" not in body:
        # Seen from a random-weight probe whose output was not valid UTF-8:
        # Ollama abandoned the reply and returned an empty frame.
        pytest.fail(f"Ollama reported no prompt size: {str(body)[:200]}")
    return body["prompt_eval_count"]


class TestTransport:
    def test_the_system_prompt_reaches_the_model(self, provider):
        """The bug this file exists because of: a system prompt sent where
        /api/chat does not look is dropped without an error."""
        bare = _prompt_tokens(provider, "")
        full = _prompt_tokens(provider, SYSTEM_PROMPT)
        # Any tokenizer spends at least one token per eight characters of
        # English prose; a prompt that did not grow was never included.
        assert full - bare >= len(SYSTEM_PROMPT) // 8, (bare, full)

    def test_a_reply_comes_back_as_text(self, provider):
        reply = provider.complete(
            SYSTEM_PROMPT, [Message("user", "Question: where is movement?")]
        )
        assert isinstance(reply, str) and reply.strip()


class TestTheProtocol:
    def test_the_first_reply_is_a_command(self, provider):
        """The measurement nobody had made: does a local model write the
        command language when it is actually shown it?"""
        reply = provider.complete(
            SYSTEM_PROMPT, [Message("user", "Project: game\n\nQuestion: where is movement?")]
        )
        print(f"\n{MODEL} replied: {reply[:300]!r}")
        command, _ = parse_command(reply)
        assert command, f"no command in {reply[:300]!r}"

    def test_a_question_runs_to_an_end(self, provider, make_project):
        """Finishes one way or the other, never with an exception. Whether the
        answer is right is printed, not asserted: that is the model's half."""
        root = make_project(PROJECT)
        scan_result = scan(root)
        answer = ask(provider, scan_result, analyze(scan_result), "where is movement?")

        print(f"\n{MODEL}:")
        for step in answer.steps:
            marker = "x" if step.refused else "->"
            print(f"  {marker} {step.command or '(no command)'} {step.argument}".rstrip())
        print(f"  answer: {answer.text or answer.incomplete}")
        if answer.citation:
            print(f"  cited: {answer.citation.location} ({answer.citation.confidence}%)")

        assert answer.text or answer.incomplete
