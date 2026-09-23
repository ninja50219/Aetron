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


class _Reply:
    """A urlopen result carrying one Ollama reply."""

    def __init__(self, content="SEARCH login"):
        self.body = json.dumps({"message": {"content": content}}).encode()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TestWhatOllamaIsSent:
    """Each of these was found by pointing the provider at a real daemon, which
    until then it had never met."""

    def _sent(self, monkeypatch, provider=None):
        sent = {}

        def capture(request, timeout):
            sent["body"] = json.loads(request.data)
            sent["timeout"] = timeout
            return _Reply()

        monkeypatch.setattr("urllib.request.urlopen", capture)
        (provider or OllamaProvider()).complete(
            "the rules", [Message("user", "where is login?")]
        )
        return sent

    def test_the_system_prompt_is_the_first_message(self, monkeypatch):
        """/api/chat ignores a top-level "system" key. Sent that way, the real
        daemon built its prompt from the question alone."""
        body = self._sent(monkeypatch)["body"]
        assert "system" not in body
        assert body["messages"][0] == {"role": "system", "content": "the rules"}
        assert body["messages"][1] == {"role": "user", "content": "where is login?"}

    def test_the_context_is_wide_enough_to_keep_the_question(self, monkeypatch):
        """At Ollama's 4096 default, a long walk lost its oldest message - the
        one that holds the question."""
        assert self._sent(monkeypatch)["body"]["options"]["num_ctx"] >= 16384

    def test_a_reply_has_a_ceiling(self, monkeypatch):
        """Unbounded, a looping probe model produced 40960 tokens."""
        from aetron.ai_providers.ollama import MAX_TOKENS

        assert self._sent(monkeypatch)["body"]["options"]["num_predict"] == MAX_TOKENS

    def test_the_context_and_timeout_can_be_chosen(self, monkeypatch):
        sent = self._sent(monkeypatch, OllamaProvider(num_ctx=4096, timeout=30))
        assert sent["body"]["options"]["num_ctx"] == 4096
        assert sent["timeout"] == 30


class TestOllamaFailuresFoundOnARealDaemon:
    def _provider(self, monkeypatch, error):
        def explode(*args, **kwargs):
            raise error

        monkeypatch.setattr("urllib.request.urlopen", explode)
        return OllamaProvider()

    def test_a_model_slower_than_the_timeout_is_a_provider_error(self, monkeypatch):
        """urllib does not wrap a timeout that happens while waiting for the
        reply, and ``aetron ask`` printed a traceback after five minutes."""
        provider = self._provider(monkeypatch, TimeoutError("timed out"))
        with pytest.raises(ProviderError) as caught:
            provider.complete("sys", [Message("user", "hi")])
        assert "did not answer within" in str(caught.value)
        assert "smaller model" in str(caught.value)

    def test_a_dropped_connection_is_a_provider_error(self, monkeypatch):
        provider = self._provider(monkeypatch, ConnectionResetError("reset by peer"))
        with pytest.raises(ProviderError) as caught:
            provider.complete("sys", [Message("user", "hi")])
        assert "Lost the connection" in str(caught.value)

    def _http_error(self, code, body):
        error = urllib.error.HTTPError("url", code, "Error", {}, None)
        error.read = lambda: body
        return error

    def test_ollamas_error_sentence_is_shown_without_its_json(self, monkeypatch):
        body = b'{"error":"model requires more system memory (9.1 GiB) than is available (7.6 GiB)"}'
        provider = self._provider(monkeypatch, self._http_error(500, body))
        with pytest.raises(ProviderError) as caught:
            provider.complete("sys", [Message("user", "hi")])
        assert str(caught.value) == (
            "Ollama returned 500: model requires more system memory "
            "(9.1 GiB) than is available (7.6 GiB)"
        )

    def test_a_model_stuck_repeating_itself_is_named_as_such(self, monkeypatch):
        """The daemon's own words when a probe model emitted one token forever."""
        body = b'{"error":"prediction aborted, token repeat limit reached"}'
        provider = self._provider(monkeypatch, self._http_error(500, body))
        with pytest.raises(ProviderError) as caught:
            provider.complete("sys", [Message("user", "hi")])
        assert "stuck repeating itself" in str(caught.value)

    def test_an_error_that_is_not_json_is_shown_as_it_came(self, monkeypatch):
        provider = self._provider(monkeypatch, self._http_error(502, b"Bad Gateway"))
        with pytest.raises(ProviderError) as caught:
            provider.complete("sys", [Message("user", "hi")])
        assert str(caught.value) == "Ollama returned 502: Bad Gateway"


class TestOptionalDependencies:
    def test_importing_aetron_needs_no_provider_installed(self):
        """Aetron installs with nothing required. A provider's dependency is
        an error when that provider is chosen, never at import."""
        import importlib

        import aetron.ai_providers

        importlib.reload(aetron.ai_providers)

