"""Tests for the provider layer.

No network. What is worth testing here is the part that runs when things are
wrong: a provider that is not installed, a daemon that is not running, a name
that does not exist. Those are the paths a user actually meets.
"""

import json
import urllib.error

import pytest

from aetron.ai_providers import PROVIDERS, ProviderError, get_provider
from aetron.ai_providers.base import Message
from aetron.ai_providers.ollama import OllamaProvider


class TestTheRegistry:
    def test_every_listed_provider_can_be_named(self):
        for name in PROVIDERS:
            try:
                get_provider(name)
            except ProviderError as exc:
                # Acceptable only when the optional dependency is absent.
                assert "not installed" in str(exc)

    def test_an_unknown_provider_lists_the_real_ones(self):
        with pytest.raises(ProviderError) as caught:
            get_provider("gpt9")
        assert "ollama" in str(caught.value)

    def test_a_model_can_be_chosen(self):
        assert get_provider("ollama", "qwen2.5-coder:7b").model == "qwen2.5-coder:7b"

    def test_the_default_provider_runs_locally(self):
        """A local model is the case this project exists for, and the only one
        that needs no account."""
        from aetron.ai_providers import DEFAULT_PROVIDER

        assert DEFAULT_PROVIDER == "ollama"


class TestOllamaFailures:
    def _provider(self, monkeypatch, error):
        provider = OllamaProvider()

        def explode(*args, **kwargs):
            raise error

        monkeypatch.setattr("urllib.request.urlopen", explode)
        return provider

    def test_a_daemon_that_is_not_running_says_how_to_start_it(self, monkeypatch):
        provider = self._provider(monkeypatch, urllib.error.URLError("refused"))
        with pytest.raises(ProviderError) as caught:
            provider.complete("sys", [Message("user", "hi")])
        assert "ollama serve" in str(caught.value)

    def test_a_missing_model_says_how_to_pull_it(self, monkeypatch):
        error = urllib.error.HTTPError("url", 404, "Not Found", {}, None)
        error.read = lambda: b"model not found"
        provider = self._provider(monkeypatch, error)

        with pytest.raises(ProviderError) as caught:
            provider.complete("sys", [Message("user", "hi")])
        assert "ollama pull" in str(caught.value)

    def test_a_successful_call_returns_the_message(self, monkeypatch):
        class Response:
            def read(self):
                return json.dumps({"message": {"content": "SEARCH login"}}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: Response())
        assert OllamaProvider().complete("sys", [Message("user", "hi")]) == "SEARCH login"

    def test_an_empty_reply_is_an_error_not_an_empty_string(self, monkeypatch):
        class Response:
            def read(self):
                return json.dumps({"message": {"content": ""}}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: Response())
        with pytest.raises(ProviderError):
            OllamaProvider().complete("sys", [Message("user", "hi")])


class TestOptionalDependencies:
    def test_importing_aetron_needs_no_provider_installed(self):
        """Aetron installs with nothing required. A provider's dependency is
        an error when that provider is chosen, never at import."""
        import importlib

        import aetron.ai_providers

        importlib.reload(aetron.ai_providers)
