"""Hosted models that speak OpenAI's chat completions API: GPT, and Gemini.

One class, because the API became a lingua franca. OpenAI serves it, Google
serves Gemini through a compatible endpoint, and so do most other hosts and
local servers, so a provider here is a base URL and the name of an environment
variable - not a client library. Like the Ollama provider it uses only the
standard library, which keeps Aetron installable with nothing.

A key is the difference between this file and the local one, and most of the
care here is about the key: it is read from the environment only, it is sent
only over HTTPS (or to this machine), and it is scrubbed from any error text
before that text can reach a screen or a log.

There is no default model. A hosted model is billed per token, the prices
between one model and the next differ by an order of magnitude, and names are
retired on the provider's schedule, not this project's. Choosing one silently
would be spending someone's money on a guess - so the person asking names it.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .base import Message, Provider, ProviderError, key_from_environment, redact

TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class Endpoint:
    """Where one hosted provider lives, and where its key is found."""

    label: str
    base_url: str
    key_variable: str
    # A variable that may point the provider somewhere else, e.g. a proxy or a
    # compatible local server. Empty when the provider has no such convention.
    base_url_variable: str = ""


ENDPOINTS = {
    "openai": Endpoint(
        label="OpenAI",
        base_url="https://api.openai.com/v1",
        key_variable="OPENAI_API_KEY",
        # The variable OpenAI's own SDK reads, so an existing setup carries over.
        base_url_variable="OPENAI_BASE_URL",
    ),
    "gemini": Endpoint(
        label="Gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        key_variable="GEMINI_API_KEY",
    ),
}


class OpenAICompatibleProvider(Provider):
    def __init__(
        self,
        name: str,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = TIMEOUT_SECONDS,
    ) -> None:
        if name not in ENDPOINTS:
            raise ProviderError(f"No endpoint is defined for {name!r}.")
        endpoint = ENDPOINTS[name]

        if not model:
            raise ProviderError(
                f"Name the {endpoint.label} model to use, for example: "
                f'aetron ask . "where is login?" --provider {name} --model <model>. '
                "Aetron does not choose a hosted model for you, because the "
                "choice decides what you are billed."
            )

        self.name = name
        self.model = model
        self.label = endpoint.label
        self.timeout = timeout
        self.base_url = _checked_url(
            base_url
            or (os.environ.get(endpoint.base_url_variable, "").strip()
                if endpoint.base_url_variable else "")
            or endpoint.base_url
        )
        self.key_variable = endpoint.key_variable
        self._key = api_key or key_from_environment(endpoint.key_variable, endpoint.label)

    def payload(self, system: str, messages: list[Message]) -> dict:
        """The request body. Deliberately minimal: every optional field is one
        that some compatible server rejects - reasoning models refuse any
        temperature but the default, and the token ceiling is spelled two
        different ways depending on whose server it is."""
        return {
            "model": self.model,
            "messages": [{"role": "system", "content": system}]
            + [{"role": m.role, "content": m.content} for m in messages],
        }

    def complete(self, system: str, messages: list[Message]) -> str:
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(self.payload(system, messages)).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._key}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc) from None
        except urllib.error.URLError as exc:
            raise ProviderError(
                f"Could not reach {self.label} at {self.base_url}: {self._clean(exc.reason)}"
            ) from None
        except TimeoutError:
            # See the Ollama provider: urllib does not wrap a timeout that
            # happens while waiting for the reply.
            raise ProviderError(
                f"{self.label} did not answer within {self.timeout:.0f} seconds."
            ) from None
        except OSError as exc:
            raise ProviderError(
                f"Lost the connection to {self.label}: {self._clean(exc)}"
            ) from None
        except json.JSONDecodeError:
            raise ProviderError(f"{self.label} returned something that was not JSON.") from None

        try:
            message = body["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise ProviderError(f"{self.label} returned a reply with no message in it.") from None

        content = message.get("content")
        if not content and message.get("refusal"):
            raise ProviderError(f"The model declined to answer: {message['refusal']}")
        if not content or not str(content).strip():
            raise ProviderError(f"{self.label} returned an empty message.")
        return content

    def _http_error(self, exc: urllib.error.HTTPError) -> ProviderError:
        # Every raise in complete() is "from None". The message built here has
        # been through redact(); a chained original has not, and would print
        # beneath it in any traceback.
        detail = self._clean(_error_text(exc))
        if exc.code in (401, 403):
            return ProviderError(
                f"{self.label} rejected the key in {self.key_variable}: {detail}"
            )
        if exc.code == 404:
            return ProviderError(
                f"{self.label} has no model called {self.model!r}, or this key "
                f"cannot use it: {detail}"
            )
        if exc.code == 429:
            return ProviderError(
                f"{self.label} refused for rate limit or quota: {detail}"
            )
        return ProviderError(f"{self.label} returned {exc.code}: {detail}")

    def _clean(self, value) -> str:
        return redact(str(value), self._key)


def _checked_url(url: str) -> str:
    """The base URL, refused if it would send a key where it could be read.

    Plain HTTP is allowed only to this machine, where a compatible local
    server (LM Studio, llama.cpp, vLLM) usually listens. Anywhere else, the
    key would cross the network in the clear.
    """
    url = url.rstrip("/")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "https":
        return url
    if parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1"):
        return url
    raise ProviderError(
        f"Refusing to send an API key to {url!r}: use https://, "
        "or http:// only for a server on this machine."
    )


def _error_text(exc: urllib.error.HTTPError) -> str:
    """The sentence inside an error body, in either shape these APIs use.

    OpenAI answers ``{"error": {"message": ...}}``; Google's compatible
    endpoint wraps the same thing in a one-element list.
    """
    raw = exc.read().decode("utf-8", errors="replace")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw[:300]
    if isinstance(parsed, list) and parsed:
        parsed = parsed[0]
    error = parsed.get("error") if isinstance(parsed, dict) else None
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])[:300]
    if isinstance(error, str):
        return error[:300]
    return raw[:300]
