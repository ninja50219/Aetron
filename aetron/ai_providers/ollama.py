"""Models running on the machine Aetron is running on, through Ollama.

Ollama serves an HTTP endpoint on localhost, so this needs no client library -
the standard library is enough, and Aetron keeps its promise that nothing is
required to install it.

This is the provider the project is really for. A codebase is the most private
thing a developer has, and the reason Aetron reduces a repository to an index
before anything sees it is so that the thing which does see it can be a model
on your own hardware. Llama, Qwen and DeepSeek all serve through here.

Most of what follows was learned from a real daemon rather than from the
documentation. Until 2026-09-23 this file had only ever met a mocked
``urlopen``, and the first real Ollama it was pointed at showed what the mock
could not: the system prompt was being dropped, a long conversation lost its
own question, a looping reply ran for tens of thousands of tokens, and a slow
model crashed the terminal instead of reporting that it was slow.
"""

import json
import urllib.error
import urllib.request

from .base import Message, Provider, ProviderError

DEFAULT_HOST = "http://localhost:11434"
DEFAULT_MODEL = "qwen2.5-coder"

# Local models are slower than hosted ones and a first call may load several
# gigabytes of weights from disk. Timing out mid-load and reporting a failure
# would be wrong about what happened.
TIMEOUT_SECONDS = 300

# The context window to ask for. The Ollama 0.34.3 this was measured against
# gave a model 4096 tokens unless asked, and when a conversation outgrew that
# it silently dropped the oldest messages while keeping the system prompt. The
# oldest message is the one that carries the question, so the model went on
# searching for something it could no longer see. Measured on this repository, an eight-request walk peaks near 5600 tokens and twelve
# requests reach roughly twice that; 16384 covers MAX_STEPS with room to spare.
# The cost is memory: about 0.9 GB of cache for a 7B model, allocated when the
# model loads.
NUM_CTX = 16384

# A ceiling on one reply, for the same reason the Anthropic provider has one:
# to stop a runaway, not to shape the answer. A reply is one command or a short
# answer. Without a ceiling, a probe model that looped on two tokens generated
# 40960 of them before Ollama stopped it - two hours on a laptop CPU at 7B
# speeds, long past the timeout above. A cut-off reply still works, because
# the command is looked for on every line.
MAX_TOKENS = 1024

# TODO: reasoning models (qwen3, deepseek-r1) write their thinking before the
# command, and 1024 tokens may cut them off mid-thought, which comes back as an
# empty message. None could be pulled in the environment this was written in.
# Recommendation: if one returns empty replies here, send "think": false in the
# payload rather than raising MAX_TOKENS - the protocol asks for one command per
# turn, and minutes of reasoning per SEARCH on a CPU defeats the point.


class OllamaProvider(Provider):
    name = "ollama"

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        host: str = DEFAULT_HOST,
        num_ctx: int = NUM_CTX,
        timeout: float = TIMEOUT_SECONDS,
    ) -> None:
        self.model = model
        self.host = host.rstrip("/")
        self.num_ctx = num_ctx
        self.timeout = timeout

    def payload(self, system: str, messages: list[Message]) -> dict:
        """The body of one /api/chat request.

        The system prompt travels as the first message. /api/chat has no
        top-level ``system`` field - that belongs to /api/generate - and it
        ignores keys it does not know rather than rejecting them. Sent the old
        way, a 500-byte system prompt and a 20-byte question produced a
        39-token prompt; as a message, the same request produced 547. Every
        local model Aetron had driven until then had never seen the command
        language it was being asked to write.
        """
        return {
            "model": self.model,
            "messages": [{"role": "system", "content": system}]
            + [{"role": m.role, "content": m.content} for m in messages],
            "stream": False,
            "options": {
                # The protocol wants one command per turn, chosen deliberately.
                # Sampling that wanders produces commands that do not parse.
                "temperature": 0.1,
                "num_ctx": self.num_ctx,
                "num_predict": MAX_TOKENS,
            },
        }

    def complete(self, system: str, messages: list[Message]) -> str:
        request = urllib.request.Request(
            f"{self.host}/api/chat",
            data=json.dumps(self.payload(system, messages)).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = _error_text(exc)
            if exc.code == 404:
                raise ProviderError(
                    f"Ollama has no model called {self.model!r}. "
                    f"Pull it first: ollama pull {self.model}"
                ) from exc
            if "repeat limit" in detail:
                raise ProviderError(
                    f"{self.model} got stuck repeating itself and Ollama stopped "
                    "it. Ask again, or try a larger model."
                ) from exc
            raise ProviderError(f"Ollama returned {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise ProviderError(
                f"Could not reach Ollama at {self.host}. Is it running? "
                "Start it with: ollama serve"
            ) from exc
        except TimeoutError as exc:
            # Not a URLError: urllib wraps a timeout while connecting, but a
            # timeout waiting for the reply escapes as the bare socket error.
            # A non-streaming request waits for the whole reply, so this is
            # the error a slow model actually produces - and it reached the
            # terminal as a traceback.
            raise ProviderError(
                f"{self.model} did not answer within {self.timeout:.0f} seconds. "
                "A smaller model is faster on a CPU, for example: "
                "ollama pull qwen2.5-coder:3b"
            ) from exc
        except OSError as exc:
            # The connection dropped mid-reply: Ollama restarted, or its runner
            # was killed for memory.
            raise ProviderError(f"Lost the connection to Ollama: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise ProviderError("Ollama returned something that was not JSON.") from exc

        content = body.get("message", {}).get("content")
        if not content:
            raise ProviderError("Ollama returned an empty message.")

        return content


def _error_text(exc: urllib.error.HTTPError) -> str:
    """What Ollama said went wrong, without the JSON around it.

    Ollama reports failures as ``{"error": "..."}``. Printing that verbatim
    puts braces and escaped quotes in front of someone who only needs the
    sentence inside.
    """
    raw = exc.read().decode("utf-8", errors="replace")
    try:
        message = json.loads(raw).get("error")
    except (json.JSONDecodeError, AttributeError):
        message = None
    return (message or raw)[:200]
