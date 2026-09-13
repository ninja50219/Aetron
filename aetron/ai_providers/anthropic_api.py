"""Claude, through Anthropic's official SDK.

The SDK is imported inside the constructor rather than at the top of the file.
That is not a style choice: Aetron installs with no required dependencies, and
a module-level import would make every user of the scanner install an API
client they may never call. Someone who wants this provider installs it, and
someone who does not is never asked to.
"""

from .base import Message, Provider, ProviderError

DEFAULT_MODEL = "claude-opus-5"

# Replies here are one command or one short answer, not an essay. The ceiling
# exists to stop a runaway, not to shape the response.
MAX_TOKENS = 4096


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, model: str = DEFAULT_MODEL, api_key: str | None = None) -> None:
        try:
            import anthropic
        except ImportError as exc:
            raise ProviderError(
                "The anthropic package is not installed. "
                "Install it with: pip install anthropic"
            ) from exc

        self.model = model
        self._anthropic = anthropic

        try:
            # With no key argument the SDK resolves ANTHROPIC_API_KEY, then
            # ANTHROPIC_AUTH_TOKEN, then a profile written by "ant auth login".
            # Passing None through would defeat all three.
            self._client = (
                anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
            )
        except Exception as exc:
            raise ProviderError(f"Could not create an Anthropic client: {exc}") from exc

    def complete(self, system: str, messages: list[Message]) -> str:
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=[{"role": m.role, "content": m.content} for m in messages],
            )
        except self._anthropic.AuthenticationError as exc:
            raise ProviderError(
                "Anthropic rejected the credentials. Set ANTHROPIC_API_KEY, "
                "or sign in with: ant auth login"
            ) from exc
        except self._anthropic.RateLimitError as exc:
            raise ProviderError("Anthropic rate limit reached; try again shortly.") from exc
        except self._anthropic.APIConnectionError as exc:
            raise ProviderError(f"Could not reach the Anthropic API: {exc}") from exc
        except self._anthropic.APIStatusError as exc:
            raise ProviderError(f"Anthropic returned {exc.status_code}: {exc.message}") from exc

        if response.stop_reason == "refusal":
            raise ProviderError("The model declined to answer this question.")

        # content is a list of blocks; only the text ones are the reply.
        text = "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        )

        if not text.strip():
            raise ProviderError("Anthropic returned an empty message.")

        return text
