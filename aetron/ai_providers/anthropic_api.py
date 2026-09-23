"""Claude, through Anthropic's official SDK.

The SDK is imported inside the constructor rather than at the top of the file.
That is not a style choice: Aetron installs with no required dependencies, and
a module-level import would make every user of the scanner install an API
client they may never call. Someone who wants this provider installs it, and
someone who does not is never asked to.

The key is never Aetron's to hold. The SDK finds it itself - ANTHROPIC_API_KEY,
ANTHROPIC_AUTH_TOKEN, or a profile saved by "ant auth login" - so nothing about
a credential is read, stored or written by this file.
"""

from .base import Message, Provider, ProviderError

DEFAULT_MODEL = "claude-opus-5"

# Replies here are one command or one short answer, not an essay. The ceiling
# exists to stop a runaway, not to shape the response - but on Claude Opus 5
# and later, thinking is on by default and counts against it, so a ceiling
# sized for the visible reply alone can be spent before any text is written.
MAX_TOKENS = 16000

# Models whose safety classifiers can decline a request, and which accept
# "fallbacks": "default" - the API then re-runs a declined request on the model
# Anthropic recommends for that kind of refusal, in the same call. Without it,
# a false positive on a harmless question about someone's code is a dead end.
# Any other model is asked directly, since the parameter is not valid for all.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = frozenset(
    {"claude-opus-5", "claude-opus-5-5", "claude-fable-5", "claude-fable-5-1"}
)

# Models that take an effort level and adaptive thinking whose summary can be
# shown. Per the Claude API documentation Haiku 4.5 rejects effort, so a model
# not listed here is asked plainly rather than risking a 400.
EFFORT_MODELS = frozenset(
    {
        "claude-opus-5", "claude-opus-5-5", "claude-fable-5", "claude-fable-5-1",
        "claude-sonnet-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
        "claude-sonnet-4-6",
    }
)

NO_CREDENTIALS = (
    "Anthropic needs an API key, and none was found. Set ANTHROPIC_API_KEY in "
    "your shell before starting Aetron - Windows: setx ANTHROPIC_API_KEY "
    "\"your-key\" (then open a new window); macOS or Linux: export "
    "ANTHROPIC_API_KEY=\"your-key\" - or sign in with: ant auth login. "
    "Never put the key in a file inside your project."
)


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(
        self, model: str = DEFAULT_MODEL, api_key: str | None = None, effort: str = "medium"
    ) -> None:
        try:
            import anthropic
        except ImportError as exc:
            raise ProviderError(
                "The anthropic package is not installed. "
                "Install it with: pip install anthropic"
            ) from exc

        self.model = model
        self.effort = effort if effort in ("low", "medium", "high") else "medium"
        self._anthropic = anthropic
        # Read by ask() after each reply, as for every provider.
        self.last_thinking = ""
        self.last_usage: tuple[int | None, int | None] = (None, None)

        try:
            # With no key argument the SDK resolves ANTHROPIC_API_KEY, then
            # ANTHROPIC_AUTH_TOKEN, then a profile written by "ant auth login".
            # Passing None through would defeat all three.
            self._client = (
                anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
            )
        except Exception as exc:
            raise ProviderError(f"Could not create an Anthropic client: {exc}") from exc

    def request(self, system: str, messages: list[Message]) -> dict:
        """The arguments of one Messages API call."""
        arguments = {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "system": system,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            # The instructions and the project map open every request of a
            # question unchanged, which is what a prompt cache is for: each
            # request after the first reads them at a tenth of the price.
            "cache_control": {"type": "ephemeral"},
        }
        if self.model in EFFORT_MODELS:
            arguments["output_config"] = {"effort": self.effort}
            # "summarized" so the thinking can be read on the page; the
            # default on these models returns it empty.
            arguments["thinking"] = {"type": "adaptive", "display": "summarized"}
        if self.model in FALLBACK_MODELS:
            arguments["betas"] = [FALLBACK_BETA]
            arguments["fallbacks"] = "default"
        return arguments

    def complete(self, system: str, messages: list[Message]) -> str:
        arguments = self.request(system, messages)
        create = (
            self._client.beta.messages.create
            if "fallbacks" in arguments
            else self._client.messages.create
        )

        try:
            response = create(**arguments)
        except TypeError as exc:
            # Found by asking with no key set: the SDK builds a client without
            # complaint and raises a bare TypeError on the first request. It
            # escaped every handler above this one, and the page's worker
            # thread died with its question still marked as running.
            if "authentication" in str(exc):
                raise ProviderError(NO_CREDENTIALS) from exc
            raise
        except self._anthropic.AuthenticationError as exc:
            raise ProviderError(
                "Anthropic rejected the credentials. Check ANTHROPIC_API_KEY, "
                "or sign in with: ant auth login"
            ) from exc
        except self._anthropic.NotFoundError as exc:
            raise ProviderError(
                f"Anthropic has no model called {self.model!r}. "
                f"The default is {DEFAULT_MODEL}."
            ) from exc
        except self._anthropic.RateLimitError as exc:
            raise ProviderError("Anthropic rate limit reached; try again shortly.") from exc
        except self._anthropic.APIConnectionError as exc:
            raise ProviderError(f"Could not reach the Anthropic API: {exc}") from exc
        except self._anthropic.APIStatusError as exc:
            raise ProviderError(f"Anthropic returned {exc.status_code}: {exc.message}") from exc

        if response.stop_reason == "refusal":
            raise ProviderError("The model declined to answer this question.")

        usage = getattr(response, "usage", None)
        self.last_usage = (
            (getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None))
            if usage is not None else (None, None)
        )
        self.last_thinking = " ".join(
            block.thinking for block in response.content
            if getattr(block, "type", "") == "thinking" and getattr(block, "thinking", "")
        ).strip()

        # content is a list of blocks - thinking, text, and a fallback marker
        # when another model took over; only the text ones are the reply.
        text = "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        )

        if not text.strip():
            if response.stop_reason == "max_tokens":
                raise ProviderError(
                    f"{self.model} used its {MAX_TOKENS}-token limit before writing a reply."
                )
            raise ProviderError("Anthropic returned an empty message.")

        return text
