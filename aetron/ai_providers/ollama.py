"""Models running on the machine Aetron is running on, through Ollama.

Ollama serves an HTTP endpoint on localhost, so this needs no client library -
the standard library is enough, and Aetron keeps its promise that nothing is
required to install it.

This is the provider the project is really for. A codebase is the most private
thing a developer has, and the reason Aetron reduces a repository to an index
before anything sees it is so that the thing which does see it can be a model
on your own hardware. Llama, Qwen and DeepSeek all serve through here.
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


class OllamaProvider(Provider):
    name = "ollama"

    def __init__(self, model: str = DEFAULT_MODEL, host: str = DEFAULT_HOST) -> None:
        self.model = model
        self.host = host.rstrip("/")

    def complete(self, system: str, messages: list[Message]) -> str:
        payload = {
            "model": self.model,
            "system": system,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": False,
            "options": {
                # The protocol wants one command per turn, chosen deliberately.
                # Sampling that wanders produces commands that do not parse.
                "temperature": 0.1,
            },
        }

        request = urllib.request.Request(
            f"{self.host}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
            if exc.code == 404:
                raise ProviderError(
                    f"Ollama has no model called {self.model!r}. "
                    f"Pull it first: ollama pull {self.model}"
                ) from exc
            raise ProviderError(f"Ollama returned {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise ProviderError(
                f"Could not reach Ollama at {self.host}. Is it running? "
                "Start it with: ollama serve"
            ) from exc
        except json.JSONDecodeError as exc:
            raise ProviderError("Ollama returned something that was not JSON.") from exc

        content = body.get("message", {}).get("content")
        if not content:
            raise ProviderError("Ollama returned an empty message.")

        return content
