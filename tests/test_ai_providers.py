"""Tests for the provider layer.

No network beyond loopback. What is worth testing here is the part that runs
when things are wrong: a provider that is not installed, a daemon that is not
running, a name that does not exist, a key that is missing or rejected. Those
are the paths a user actually meets. A real Ollama is exercised separately, in
test_ollama_live.py, when one is available.
"""

import json
import urllib.error

import pytest

from aetron.ai_providers import PROVIDERS, ProviderError, get_provider
from aetron.ai_providers.base import Message
from aetron.ai_providers.ollama import OllamaProvider


class TestTheRegistry:
    def test_every_listed_provider_can_be_named(self, monkeypatch):
        # Fake keys, built at runtime so that no key-shaped literal is ever
        # committed; see test_no_secrets.py.
        for variable in ("OPENAI_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY"):
            monkeypatch.setenv(variable, "test-" + "k" * 24)
        for name in PROVIDERS:
            try:
                provider = get_provider(name, "some-model")
            except ProviderError as exc:
                # Acceptable only when the optional dependency is absent.
                assert "not installed" in str(exc)
            else:
                assert provider.name == name
                assert provider.model == "some-model"

    def test_only_ollama_counts_as_local(self):
        """What the page uses to decide whether to warn that code leaves."""
        from aetron.ai_providers import LOCAL_PROVIDERS

        assert LOCAL_PROVIDERS == {"ollama"}

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


# --- hosted providers ------------------------------------------------------
#
# Tested against a real HTTP server on loopback rather than a patched urlopen:
# the thing worth checking is what actually crosses the socket - the header
# the key travels in, the body, the path - and a patch would only check what
# the code believes it sends.

import http.server
import threading

FAKE_KEY = "test-" + "x" * 40


@pytest.fixture
def server():
    """A loopback server that records each request and replies as scripted."""

    class Recorder:
        requests: list = []
        replies: list = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            size = int(self.headers.get("Content-Length", "0"))
            Recorder.requests.append(
                {"path": self.path,
                 "headers": {k.lower(): v for k, v in self.headers.items()},
                 "body": json.loads(self.rfile.read(size) or b"{}")}
            )
            status, body = Recorder.replies.pop(0) if Recorder.replies else (200, {})
            content = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    Recorder.requests, Recorder.replies = [], []
    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    Recorder.url = f"http://127.0.0.1:{httpd.server_port}"
    yield Recorder
    httpd.shutdown()
    httpd.server_close()


def chat_reply(content):
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


class TestKeysComeFromTheEnvironmentOnly:
    def test_a_missing_key_names_the_variable_and_how_to_set_it(self, monkeypatch):
        from aetron.ai_providers.base import key_from_environment

        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(ProviderError) as caught:
            key_from_environment("OPENAI_API_KEY", "OpenAI")
        message = str(caught.value)
        assert "OPENAI_API_KEY is not set" in message
        assert "setx OPENAI_API_KEY" in message
        assert "export OPENAI_API_KEY" in message
        assert "Never put the key in a file inside your project" in message

    def test_a_blank_key_counts_as_missing(self, monkeypatch):
        from aetron.ai_providers.base import key_from_environment

        monkeypatch.setenv("OPENAI_API_KEY", "   ")
        with pytest.raises(ProviderError):
            key_from_environment("OPENAI_API_KEY", "OpenAI")

    def test_a_key_is_scrubbed_from_text(self):
        from aetron.ai_providers.base import redact

        assert redact(f"Incorrect API key provided: {FAKE_KEY}.", FAKE_KEY) == (
            "Incorrect API key provided: [key hidden]."
        )

    def test_a_short_secret_does_not_blank_ordinary_words(self):
        from aetron.ai_providers.base import redact

        assert redact("the model", "the") == "the model"


class TestOpenAICompatible:
    def _provider(self, server, name="openai", **kwargs):
        from aetron.ai_providers.openai_compatible import OpenAICompatibleProvider

        return OpenAICompatibleProvider(
            name, model=kwargs.pop("model", "some-model"), api_key=FAKE_KEY,
            base_url=server.url, **kwargs
        )

    def test_the_request_is_the_chat_completions_shape(self, server):
        server.replies.append((200, chat_reply("SEARCH login")))
        reply = self._provider(server).complete(
            "the rules", [Message("user", "where is login?")]
        )
        assert reply == "SEARCH login"

        sent = server.requests[0]
        assert sent["path"] == "/chat/completions"
        assert sent["headers"]["authorization"] == f"Bearer {FAKE_KEY}"
        assert sent["body"] == {
            "model": "some-model",
            "messages": [
                {"role": "system", "content": "the rules"},
                {"role": "user", "content": "where is login?"},
            ],
        }

    def test_the_key_is_read_from_the_environment(self, server, monkeypatch):
        from aetron.ai_providers.openai_compatible import OpenAICompatibleProvider

        monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)
        server.replies.append((200, chat_reply("ANSWER none")))
        provider = OpenAICompatibleProvider("gemini", model="m", base_url=server.url)
        provider.complete("s", [Message("user", "q")])
        assert server.requests[0]["headers"]["authorization"] == f"Bearer {FAKE_KEY}"

    def test_openai_honours_its_own_base_url_variable(self, server, monkeypatch):
        from aetron.ai_providers.openai_compatible import OpenAICompatibleProvider

        monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
        monkeypatch.setenv("OPENAI_BASE_URL", server.url + "/v1/")
        server.replies.append((200, chat_reply("ANSWER none")))
        OpenAICompatibleProvider("openai", model="m").complete("s", [Message("user", "q")])
        assert server.requests[0]["path"] == "/v1/chat/completions"

    def test_a_hosted_model_must_be_named(self, monkeypatch):
        """No default: the model decides the bill, so the person paying picks."""
        monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
        with pytest.raises(ProviderError, match="Name the OpenAI model"):
            get_provider("openai")

    def test_a_missing_key_is_an_error_before_anything_is_sent(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        with pytest.raises(ProviderError, match="GEMINI_API_KEY is not set"):
            get_provider("gemini", "some-model")

    @pytest.mark.parametrize(
        "url", ["http://api.example.com/v1", "ftp://127.0.0.1", "api.example.com"]
    )
    def test_a_key_is_never_sent_in_the_clear(self, url):
        from aetron.ai_providers.openai_compatible import OpenAICompatibleProvider

        with pytest.raises(ProviderError, match="Refusing to send an API key"):
            OpenAICompatibleProvider("openai", model="m", api_key=FAKE_KEY, base_url=url)

    @pytest.mark.parametrize("url", ["https://api.example.com/v1", "http://localhost:1234/v1"])
    def test_https_and_this_machine_are_allowed(self, url):
        from aetron.ai_providers.openai_compatible import OpenAICompatibleProvider

        provider = OpenAICompatibleProvider("openai", model="m", api_key=FAKE_KEY, base_url=url)
        assert provider.base_url == url

    def test_a_rejected_key_names_its_variable_and_never_shows_it(self, server):
        # Shaped like OpenAI's real reply, which quotes the key it rejected.
        server.replies.append(
            (401, {"error": {"message": f"Incorrect API key provided: {FAKE_KEY}.",
                             "type": "invalid_request_error"}})
        )
        with pytest.raises(ProviderError) as caught:
            self._provider(server).complete("s", [Message("user", "q")])
        message = str(caught.value)
        assert "rejected the key in OPENAI_API_KEY" in message
        assert FAKE_KEY not in message
        assert "[key hidden]" in message
        assert caught.value.__cause__ is None and caught.value.__suppress_context__

    def test_an_unknown_model_says_so(self, server):
        server.replies.append(
            (404, {"error": {"message": "The model `nope` does not exist."}})
        )
        with pytest.raises(ProviderError) as caught:
            self._provider(server, model="nope").complete("s", [Message("user", "q")])
        assert "has no model called 'nope'" in str(caught.value)
        assert "does not exist" in str(caught.value)

    def test_googles_list_shaped_error_is_read_too(self, server):
        server.replies.append(
            (400, [{"error": {"code": 400, "message": "API key not valid.",
                              "status": "INVALID_ARGUMENT"}}])
        )
        with pytest.raises(ProviderError) as caught:
            self._provider(server, name="gemini").complete("s", [Message("user", "q")])
        assert str(caught.value) == "Gemini returned 400: API key not valid."

    def test_a_quota_error_is_named(self, server):
        server.replies.append((429, {"error": {"message": "You exceeded your quota."}}))
        with pytest.raises(ProviderError, match="rate limit or quota"):
            self._provider(server).complete("s", [Message("user", "q")])

    def test_a_refusal_is_an_error_not_an_answer(self, server):
        server.replies.append(
            (200, {"choices": [{"message": {"content": None, "refusal": "I can't help."}}]})
        )
        with pytest.raises(ProviderError, match="declined to answer: I can't help"):
            self._provider(server).complete("s", [Message("user", "q")])

    def test_an_empty_reply_is_an_error(self, server):
        server.replies.append((200, chat_reply("  ")))
        with pytest.raises(ProviderError, match="empty message"):
            self._provider(server).complete("s", [Message("user", "q")])

    def test_a_server_that_is_not_there_says_where_it_looked(self):
        from aetron.ai_providers.openai_compatible import OpenAICompatibleProvider

        provider = OpenAICompatibleProvider(
            "openai", model="m", api_key=FAKE_KEY, base_url="http://127.0.0.1:9"
        )
        with pytest.raises(ProviderError, match="Could not reach OpenAI at http://127.0.0.1:9"):
            provider.complete("s", [Message("user", "q")])


class TestAnthropic:
    """Through the real SDK, pointed at the loopback server, so the wire
    format is the SDK's own rather than a guess at it."""

    @pytest.fixture
    def anthropic_at(self, server, monkeypatch):
        pytest.importorskip("anthropic")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", server.url)
        return server

    def _reply(self, text="SEARCH login", stop="end_turn", model="claude-opus-5"):
        content = [{"type": "thinking", "thinking": "", "signature": "sig"}]
        if text:
            content.append({"type": "text", "text": text})
        return {
            "id": "msg_test", "type": "message", "role": "assistant", "model": model,
            "content": content, "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }

    def _provider(self, model="claude-opus-5"):
        from aetron.ai_providers.anthropic_api import AnthropicProvider

        return AnthropicProvider(model=model, api_key=FAKE_KEY)

    def test_the_default_model_asks_for_refusal_fallbacks(self, anthropic_at):
        anthropic_at.replies.append((200, self._reply()))
        assert self._provider().complete("the rules", [Message("user", "q")]) == "SEARCH login"

        sent = anthropic_at.requests[0]
        assert sent["path"].startswith("/v1/messages")
        assert "server-side-fallback-2026-07-01" in sent["headers"]["anthropic-beta"]
        assert sent["body"]["fallbacks"] == "default"
        assert sent["body"]["system"] == "the rules"
        assert sent["body"]["max_tokens"] == 16000
        assert sent["headers"]["x-api-key"] == FAKE_KEY

    def test_another_model_is_asked_without_them(self, anthropic_at):
        anthropic_at.replies.append((200, self._reply(model="claude-haiku-4-5")))
        self._provider("claude-haiku-4-5").complete("s", [Message("user", "q")])
        sent = anthropic_at.requests[0]
        assert "fallbacks" not in sent["body"]
        assert "server-side-fallback" not in sent["headers"].get("anthropic-beta", "")

    def test_thinking_blocks_are_not_part_of_the_reply(self, anthropic_at):
        anthropic_at.replies.append((200, self._reply("ANSWER in login.py line 2")))
        assert self._provider().complete("s", [Message("user", "q")]) == "ANSWER in login.py line 2"

    def test_a_reply_spent_on_thinking_says_so(self, anthropic_at):
        anthropic_at.replies.append((200, self._reply(text="", stop="max_tokens")))
        with pytest.raises(ProviderError, match="16000-token limit"):
            self._provider().complete("s", [Message("user", "q")])

    def test_an_unknown_model_says_so(self, anthropic_at):
        anthropic_at.replies.append(
            (404, {"type": "error", "error": {"type": "not_found_error", "message": "model: nope"}})
        )
        with pytest.raises(ProviderError, match="no model called 'nope'"):
            self._provider("nope").complete("s", [Message("user", "q")])

    def test_no_credentials_at_all_is_a_provider_error(self, monkeypatch, tmp_path):
        """The SDK builds a client with no key and raises a bare TypeError on
        the first request, which escaped every handler in Aetron."""
        pytest.importorskip("anthropic")
        from aetron.ai_providers.anthropic_api import AnthropicProvider

        for variable in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
            monkeypatch.delenv(variable, raising=False)
        # No "ant auth login" profile to fall back on.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")

        with pytest.raises(ProviderError) as caught:
            AnthropicProvider().complete("s", [Message("user", "q")])
        assert "ANTHROPIC_API_KEY" in str(caught.value)
        assert "ant auth login" in str(caught.value)


class _Daemon:
    """A urlopen stand-in that answers each Ollama endpoint differently."""

    def __init__(self, capabilities=(), family="qwen2", chat=None, tags=None):
        self.capabilities = list(capabilities)
        self.family = family
        self.chat = chat or {"message": {"content": "SEARCH login"}, "prompt_eval_count": 120, "eval_count": 4}
        self.tags = tags
        self.sent = []

    def __call__(self, request, timeout=None):
        url = request if isinstance(request, str) else request.full_url
        body = json.loads(request.data) if not isinstance(request, str) and request.data else None
        self.sent.append((url, body))
        if url.endswith("/api/show"):
            reply = {"capabilities": self.capabilities, "details": {"family": self.family}}
        elif url.endswith("/api/tags"):
            reply = self.tags
        else:
            reply = self.chat
        return _Raw(reply)


class _Raw:
    def __init__(self, reply):
        self.body = json.dumps(reply).encode()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TestThinkingAndEffort:
    """Measured on Ollama 0.34.3 with probe models: "think" sent to a model
    without the thinking capability is an HTTP 400; "think": false is accepted
    by any model; a thinking model that never stops thinking returns an empty
    content with all its output under message.thinking."""

    def _chat_body(self, daemon):
        return next(body for url, body in daemon.sent if url.endswith("/api/chat"))

    def test_think_is_never_sent_to_a_model_that_cannot(self, monkeypatch):
        daemon = _Daemon(capabilities=["completion"])
        monkeypatch.setattr("urllib.request.urlopen", daemon)
        OllamaProvider(effort="high").complete("s", [Message("user", "q")])
        assert "think" not in self._chat_body(daemon)

    def test_a_thinking_model_thinks_except_at_low_effort(self, monkeypatch):
        for effort, expected, budget in (("low", False, 1024), ("medium", True, 4096), ("high", True, 8192)):
            daemon = _Daemon(capabilities=["completion", "thinking"])
            monkeypatch.setattr("urllib.request.urlopen", daemon)
            OllamaProvider(model="qwen3", effort=effort).complete("s", [Message("user", "q")])
            body = self._chat_body(daemon)
            assert body["think"] is expected
            assert body["options"]["num_predict"] == budget

    def test_gpt_oss_gets_a_level_not_a_switch(self, monkeypatch):
        daemon = _Daemon(capabilities=["completion", "thinking"], family="gptoss")
        monkeypatch.setattr("urllib.request.urlopen", daemon)
        OllamaProvider(model="gpt-oss:20b", effort="high").complete("s", [Message("user", "q")])
        assert self._chat_body(daemon)["think"] == "high"

    def test_the_capabilities_are_asked_once(self, monkeypatch):
        daemon = _Daemon(capabilities=["completion", "thinking"])
        monkeypatch.setattr("urllib.request.urlopen", daemon)
        provider = OllamaProvider()
        for _ in range(3):
            provider.complete("s", [Message("user", "q")])
        assert sum(url.endswith("/api/show") for url, _ in daemon.sent) == 1

    def test_the_model_stays_loaded_between_questions(self, monkeypatch):
        daemon = _Daemon()
        monkeypatch.setattr("urllib.request.urlopen", daemon)
        OllamaProvider().complete("s", [Message("user", "q")])
        assert self._chat_body(daemon)["keep_alive"] == "30m"

    def test_thinking_and_real_token_counts_are_kept(self, monkeypatch):
        daemon = _Daemon(
            capabilities=["completion", "thinking"],
            chat={"message": {"content": "SEARCH x", "thinking": "The map shows a script."},
                  "prompt_eval_count": 812, "eval_count": 37},
        )
        monkeypatch.setattr("urllib.request.urlopen", daemon)
        provider = OllamaProvider()
        assert provider.complete("s", [Message("user", "q")]) == "SEARCH x"
        assert provider.last_thinking == "The map shows a script."
        assert provider.last_usage == (812, 37)

    def test_a_model_that_only_thought_says_so(self, monkeypatch):
        daemon = _Daemon(
            capabilities=["completion", "thinking"],
            chat={"message": {"content": "", "thinking": "hmm " * 1000}, "done_reason": "length"},
        )
        monkeypatch.setattr("urllib.request.urlopen", daemon)
        with pytest.raises(ProviderError, match="spent its whole reply thinking"):
            OllamaProvider().complete("s", [Message("user", "q")])

    def test_an_unreachable_daemon_means_no_thinking_not_a_crash(self, monkeypatch):
        calls = []

        def only_chat(request, timeout=None):
            calls.append(request.full_url)
            if request.full_url.endswith("/api/show"):
                raise urllib.error.URLError("refused")
            return _Raw({"message": {"content": "SEARCH x"}})

        monkeypatch.setattr("urllib.request.urlopen", only_chat)
        provider = OllamaProvider()
        assert provider.complete("s", [Message("user", "q")]) == "SEARCH x"
        assert provider.capabilities() == set()


class TestInstalledModels:
    def test_the_pulled_models_are_listed_with_what_they_can_do(self, monkeypatch):
        from aetron.ai_providers.ollama import list_models

        tags = {"models": [
            {"name": "qwen3:8b", "size": 5_200_000_000, "details": {"parameter_size": "8.2B", "family": "qwen3"},
             "capabilities": ["completion", "tools", "thinking"]},
            {"name": "qwen2.5-coder:latest", "size": 4_700_000_000, "details": {"parameter_size": "7.6B"},
             "capabilities": ["completion", "tools"]},
        ]}
        monkeypatch.setattr("urllib.request.urlopen", _Daemon(tags=tags))
        models = list_models()
        assert [m["name"] for m in models] == ["qwen2.5-coder:latest", "qwen3:8b"]
        assert "thinking" in models[1]["capabilities"]
        assert models[1]["parameters"] == "8.2B"

    def test_no_daemon_is_none_not_an_empty_list(self, monkeypatch):
        """None means "could not ask"; [] means "asked, and nothing is pulled"."""
        from aetron.ai_providers.ollama import list_models

        def refuse(*args, **kwargs):
            raise urllib.error.URLError("refused")

        monkeypatch.setattr("urllib.request.urlopen", refuse)
        assert list_models() is None

    def test_preloading_loads_without_generating(self, monkeypatch):
        daemon = _Daemon(chat={"model": "m", "message": {"content": ""}, "done": True, "done_reason": "load"})
        monkeypatch.setattr("urllib.request.urlopen", daemon)
        assert OllamaProvider().preload()
        assert _chat_body_of(daemon)["messages"] == []


def _chat_body_of(daemon):
    return next(body for url, body in daemon.sent if url.endswith("/api/chat"))


class TestAnthropicEffortAndThinking:
    """Through the real SDK against the loopback server, like TestAnthropic."""

    @pytest.fixture
    def anthropic_at(self, server, monkeypatch):
        pytest.importorskip("anthropic")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", server.url)
        return server

    def _reply(self, thinking="", model="claude-opus-5"):
        return {
            "id": "msg_test", "type": "message", "role": "assistant", "model": model,
            "content": [
                {"type": "thinking", "thinking": thinking, "signature": "sig"},
                {"type": "text", "text": "SEARCH login"},
            ],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 1520, "output_tokens": 42},
        }

    def test_effort_and_readable_thinking_are_asked_for(self, anthropic_at):
        from aetron.ai_providers.anthropic_api import AnthropicProvider

        anthropic_at.replies.append((200, self._reply()))
        AnthropicProvider(api_key=FAKE_KEY, effort="high").complete("s", [Message("user", "q")])
        body = anthropic_at.requests[0]["body"]
        assert body["output_config"] == {"effort": "high"}
        assert body["thinking"] == {"type": "adaptive", "display": "summarized"}

    def test_the_stable_prefix_is_marked_for_caching(self, anthropic_at):
        from aetron.ai_providers.anthropic_api import AnthropicProvider

        anthropic_at.replies.append((200, self._reply()))
        AnthropicProvider(api_key=FAKE_KEY).complete("s", [Message("user", "q")])
        assert anthropic_at.requests[0]["body"]["cache_control"] == {"type": "ephemeral"}

    def test_a_model_without_effort_is_asked_plainly(self, anthropic_at):
        from aetron.ai_providers.anthropic_api import AnthropicProvider

        anthropic_at.replies.append((200, self._reply(model="claude-haiku-4-5")))
        AnthropicProvider(model="claude-haiku-4-5", api_key=FAKE_KEY).complete("s", [Message("user", "q")])
        body = anthropic_at.requests[0]["body"]
        assert "output_config" not in body and "thinking" not in body

    def test_the_thinking_summary_and_real_counts_are_kept(self, anthropic_at):
        from aetron.ai_providers.anthropic_api import AnthropicProvider

        anthropic_at.replies.append((200, self._reply(thinking="The map names LoginController.")))
        provider = AnthropicProvider(api_key=FAKE_KEY)
        provider.complete("s", [Message("user", "q")])
        assert provider.last_thinking == "The map names LoginController."
        assert provider.last_usage == (1520, 42)


class TestOpenAICompatibleCounts:
    def test_usage_and_a_servers_reasoning_are_kept(self, server):
        from aetron.ai_providers.openai_compatible import OpenAICompatibleProvider

        server.replies.append((200, {
            "choices": [{"message": {"content": "SEARCH x", "reasoning_content": "Check the map first."}}],
            "usage": {"prompt_tokens": 900, "completion_tokens": 12},
        }))
        provider = OpenAICompatibleProvider("openai", model="m", api_key=FAKE_KEY, base_url=server.url)
        provider.complete("s", [Message("user", "q")])
        assert provider.last_usage == (900, 12)
        assert provider.last_thinking == "Check the map first."
