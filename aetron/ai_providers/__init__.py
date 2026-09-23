"""Language models Aetron can ask, local or hosted.

Nothing in this package knows what a symbol or an import graph is, and nothing
in the retrieval levels knows what a model is. They meet one layer up, in
``aetron.ask``, which is the only place that needs to understand both.
"""

from .base import Message, Provider, ProviderError

# Imported lazily by name so that a missing optional dependency is an error
# when that provider is chosen, not when Aetron is imported.
PROVIDERS = ("ollama", "anthropic", "openai", "gemini")

# The ones that run on this machine. Everything else receives the question and
# whatever the model asks to see, which callers are expected to say out loud.
LOCAL_PROVIDERS = frozenset({"ollama"})

DEFAULT_PROVIDER = "ollama"


def get_provider(name: str, model: str | None = None, **kwargs) -> Provider:
    """Build a provider by name.

    Defaults to Ollama, because a local model is the case this project was
    built for and the only one that needs no account.
    """
    if name == "ollama":
        from .ollama import DEFAULT_MODEL, OllamaProvider

        return OllamaProvider(model=model or DEFAULT_MODEL, **kwargs)

    if name == "anthropic":
        from .anthropic_api import DEFAULT_MODEL, AnthropicProvider

        return AnthropicProvider(model=model or DEFAULT_MODEL, **kwargs)

    if name in ("openai", "gemini"):
        from .openai_compatible import OpenAICompatibleProvider

        return OpenAICompatibleProvider(name, model=model, **kwargs)

    raise ProviderError(
        f"Unknown provider {name!r}. Available: {', '.join(PROVIDERS)}"
    )


__all__ = [
    "DEFAULT_PROVIDER",
    "LOCAL_PROVIDERS",
    "PROVIDERS",
    "Message",
    "Provider",
    "ProviderError",
    "get_provider",
]
