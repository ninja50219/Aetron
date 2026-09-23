"""What Aetron needs from a language model, and nothing more.

One method: given a system prompt and a conversation, return text. No streaming,
no tool-calling API, no provider-specific message shapes above this line.

That is a deliberate floor rather than a first draft. Aetron has to work with
models running on someone's own machine - Llama, Qwen, DeepSeek through Ollama -
and the features those models reliably share is a short list. Tool-calling is
not on it: support ranges from good to absent to confidently wrong, and a
protocol built on it would work on the API models and quietly fail on the local
ones, which are the ones this project exists to serve.

So the retrieval protocol is carried in text the model writes, and every
provider below this file only has to return a string.
"""

import os
from dataclasses import dataclass
from typing import Protocol


class ProviderError(RuntimeError):
    """A provider could not answer.

    Always phrased for someone at a terminal: which provider, what went wrong,
    and what to do about it. A stack trace from inside an HTTP client tells the
    user nothing they can act on.
    """


def key_from_environment(variable: str, provider: str) -> str:
    """An API key, read from the environment and from nowhere else.

    Not from a file in the project, not from a config Aetron writes, not from
    the page. A key that lives in the working tree is one ``git add .`` away
    from a public repository, and Aetron is a tool people point at their
    working trees. The environment is the one place that is never committed.
    """
    key = os.environ.get(variable, "").strip()
    if not key:
        raise ProviderError(
            f"{provider} needs an API key, and {variable} is not set. "
            f"Set it in your shell before starting Aetron - "
            f"Windows: setx {variable} \"your-key\" (then open a new window); "
            f"macOS or Linux: export {variable}=\"your-key\". "
            "Never put the key in a file inside your project."
        )
    return key


def redact(text: str, secret: str) -> str:
    """``text`` with ``secret`` removed, for error messages that echo a request.

    Some APIs quote the key they rejected back in the error body, and an error
    is shown on screen, pasted into issues and written to logs. Short secrets
    are left alone rather than blanking every common substring they share.
    """
    if secret and len(secret) >= 8:
        text = text.replace(secret, "[key hidden]")
    return text


@dataclass
class Message:
    # "user" or "assistant". System prompts are passed separately, because
    # providers disagree about whether a system prompt is a message at all.
    role: str
    content: str


class Provider(Protocol):
    """Anything that can answer a question about a codebase."""

    name: str
    model: str

    def complete(self, system: str, messages: list[Message]) -> str:
        """Return the model's next message as plain text."""
        ...
