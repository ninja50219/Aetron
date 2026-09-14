"""Give readers a visual workspace without adding a server dependency.

The server is bound to loopback and owns one project's cached index. Requests
carry a per-launch token and follow the same progressive disclosure contract
as the model: search authorizes a file's outline, then its definitions. No
generic filesystem endpoint is exposed, and project text is rendered as text.

Asking a model runs on a thread rather than inside the request. A local
seven-billion-parameter model answers in minutes, not milliseconds, and a
handler that waits for it would block the single-threaded server - the page
itself would stop loading while the question it asked was being answered. So
the request starts the work and returns, and the page asks how it is going.
That also gets the steps on screen as they happen, which is the honest way to
show a protocol whose whole argument is that the expensive path is visible.
"""

import argparse
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import secrets
import sys
import threading
import webbrowser

from aetron.ai_providers import DEFAULT_PROVIDER, PROVIDERS, ProviderError, get_provider
from aetron.analyzer import analyze
from aetron.ask import MAX_STEPS, ask
from aetron.cli.interactive import load_history, save_history
from aetron.context import build_structure, build_summary, get_source, search
from aetron.context.render import render_summary
from aetron.scanner import scan
from aetron.scanner.gitignore import AVAILABLE
from aetron.scanner.paths import normalize_path


class AskJob:
    """One question in flight.

    The thread writes steps and the request thread reads them, so both go
    through the lock. Nothing here cancels: a provider call sits in a socket
    read that cannot be interrupted politely, and a job left running is
    harmless because a new question replaces it.
    """

    def __init__(self, question: str, provider) -> None:
        self.question = question
        self.provider = f"{provider.name} ({provider.model})"
        self.steps: list[dict] = []
        self.answer = None
        self.error = ""
        self.done = False
        self._lock = threading.Lock()

    def record(self, step) -> None:
        with self._lock:
            self.steps.append(
                {
                    "command": step.command,
                    "argument": step.argument,
                    "refused": step.refused,
                    # The observation is the model's half of the protocol and
                    # can be a whole file skeleton. The page shows what was
                    # asked, not everything that came back.
                    "note": step.observation.split("\n")[0][:160]
                    if step.refused or step.command in ("", "ANSWER")
                    else "",
                }
            )

    def state(self) -> dict:
        with self._lock:
            result = {
                "question": self.question,
                "provider": self.provider,
                "steps": list(self.steps),
                "done": self.done,
                "error": self.error,
            }
            if self.answer is not None:
                result["answer"] = self.answer
            return result

    def finish(self, answer=None, error: str = "") -> None:
        with self._lock:
            self.answer = answer
            self.error = error
            self.done = True


def describe_answer(answer) -> dict:
    """An Answer as the page needs it: the sentence, and where to open it."""
    result = {
        "text": answer.text,
        "incomplete": answer.incomplete,
        "files_read": answer.files_read,
        "requests": len(
            [s for s in answer.steps if s.command and s.command != "ANSWER"]
        ),
        "citation": None,
    }
    if answer.citation is not None:
        citation = asdict(answer.citation)
        citation["location"] = answer.citation.location
        citation["checks_passed"] = answer.citation.checks_passed
        result["citation"] = citation
    return result


class Workspace:
    def __init__(self, history_path: Path):
        self.history_path = history_path
        self.history = load_history(history_path)
        self.scanned = None
        self.analysis = None
        self.candidates = set()
        self.outlines = {}
        self.revision = 0
        self.job = None

    @property
    def asking(self) -> bool:
        return self.job is not None and not self.job.done

    def state(self):
        result = {"recent": self.history, "gitignore": AVAILABLE, "revision": self.revision,
                  "providers": list(PROVIDERS), "default_provider": DEFAULT_PROVIDER,
                  "max_steps": MAX_STEPS}
        if self.scanned is not None:
            result.update({"root": str(self.scanned.root), "name": self.scanned.root.name,
                           "files": len(self.scanned.files), "lines": self.scanned.total_lines,
                           "parsed": len(self.analysis.files),
                           "skipped": [asdict(s) for s in self.scanned.skipped],
                           "pruned": self.scanned.pruned_dirs})
        return result

    def request(self, action, data):
        if action == "state":
            return self.state()
        if action in ("open", "refresh"):
            if action == "refresh" and self.scanned is None:
                raise ValueError("Open a project first.")
            if self.asking:
                # The running job holds this index and reads from it between
                # steps. Swapping it underneath would answer the question
                # against half of one project and half of another.
                raise ValueError("A question is still being answered. Wait for it to finish.")
            root = normalize_path(data["path"] if action == "open" else str(self.scanned.root))
            scanned = scan(root, use_gitignore=True)
            analysis = analyze(scanned)
            self.scanned, self.analysis = scanned, analysis
            self.revision += 1
            self.candidates.clear()
            self.outlines.clear()
            self.job = None
            self.history = [str(root), *(p for p in self.history if p != str(root))]
            save_history(self.history_path, self.history)
            return self.state()
        if self.scanned is None:
            raise ValueError("Open a project first.")
        if data.get("revision") != self.revision:
            raise ValueError("The project changed. Search again to use the current index.")
        if action == "search":
            query = data["query"].strip()
            if not query:
                raise ValueError("Enter a word or a question.")
            found = search(self.scanned, self.analysis, query, limit=max(1, len(self.scanned.files)))
            self.candidates = {c.rel_path for c in found}
            self.outlines.clear()
            return {"candidates": [{"path": c.rel_path, "percent": c.percent,
                                    "reason": c.reason, "language": c.language,
                                    "parsed": c.parsed} for c in found]}
        if action == "structure":
            path = data["path"]
            if path not in self.candidates:
                raise ValueError("Choose a file from search results first.")
            outline = build_structure(self.scanned, self.analysis, path)
            self.outlines[path] = outline
            return asdict(outline)
        if action == "source":
            outline = self.outlines.get(data["path"])
            if outline is None:
                raise ValueError("Read the file outline first.")
            matches = [s for s in outline.symbols if s.qualified_name == data["name"]]
            if len(matches) != 1:
                raise ValueError("This definition cannot be selected uniquely by the source API.")
            source = get_source(self.scanned, self.analysis, data["path"], data["name"])
            return {**asdict(source), "location": source.location}
        if action == "summary":
            return {"text": render_summary(build_summary(self.scanned, self.analysis))}
        if action == "ask":
            return self.start_ask(data)
        if action == "ask_status":
            if self.job is None:
                raise ValueError("No question has been asked yet.")
            state = self.job.state()
            citation = (state.get("answer") or {}).get("citation")
            if citation:
                # The model justified this file by the same three levels the
                # explorer's own clicks go through, so opening it there next is
                # a continuation of the answer rather than a way around it.
                self.candidates.add(citation["rel_path"])
            return state
        raise ValueError("Unknown action.")

    def start_ask(self, data):
        """Hand the question to a model, and return before it has answered."""
        if self.asking:
            raise ValueError("A question is still being answered.")

        question = data.get("question", "").strip()
        if not question:
            raise ValueError("Ask a question, for example: where is movement?")

        name = data.get("provider") or DEFAULT_PROVIDER
        if name not in PROVIDERS:
            raise ValueError(f"Unknown provider {name!r}.")

        try:
            # Built here rather than on the worker thread so that a missing
            # package or a bad key is an error on the question, where the page
            # can show it, instead of a job that starts and dies.
            provider = get_provider(name, (data.get("model") or "").strip() or None)
        except ProviderError as exc:
            raise ValueError(str(exc)) from exc

        job = AskJob(question, provider)
        self.job = job
        scanned, analysis = self.scanned, self.analysis

        def run():
            try:
                answer = ask(provider, scanned, analysis, question, on_step=job.record)
            except ProviderError as exc:
                job.finish(error=str(exc))
            except (OSError, ValueError, RuntimeError) as exc:
                job.finish(error=f"The question could not be answered: {exc}")
            else:
                job.finish(answer=describe_answer(answer))

        threading.Thread(target=run, daemon=True, name="aetron-ask").start()
        return job.state()


def make_server(workspace, port=0):
    token = secrets.token_urlsafe(32)
    page = (Path(__file__).with_name("web_ui") / "index.html").read_text(encoding="utf-8")
    assets = {"/app.css": "text/css", "/app.js": "text/javascript"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, status, body, content_type="application/json"):
            content = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'nonce-" + token + "'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(content)

        def local_request(self):
            expected = f"127.0.0.1:{self.server.server_port}"
            return (self.headers.get("Host") == expected
                    and self.headers.get("Origin", f"http://{expected}") == f"http://{expected}")

        def do_GET(self):
            if not self.local_request():
                self.respond(403, '{}')
            elif self.path == "/":
                self.respond(200, page.replace("__TOKEN__", token), "text/html")
            elif self.path in assets:
                body = (Path(__file__).with_name("web_ui") / self.path[1:]).read_text(encoding="utf-8")
                self.respond(200, body, assets[self.path])
            else:
                self.respond(404, '{}')

        def do_POST(self):
            if not self.local_request() or self.headers.get("X-Aetron-Token") != token:
                self.respond(403, json.dumps({"error": "Invalid local session."}))
                return
            if not self.path.startswith("/api/"):
                self.respond(404, '{}')
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 65536:
                    raise ValueError("Invalid request size.")
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise ValueError("Expected a request object.")
                result = workspace.request(self.path[5:], data)
            except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError) as exc:
                self.respond(400, json.dumps({"error": str(exc)}))
                return
            self.respond(200, json.dumps(result))

    server = HTTPServer(("127.0.0.1", port), Handler)
    server.timeout = 0.5
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(prog="aetron ui", description="Open the local visual workspace.")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--port", type=int, default=0, help="local port (default: choose a free port)")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")
    if not AVAILABLE:
        print("Note: pathspec is not installed, .gitignore files are not applied.", file=sys.stderr)
    try:
        workspace = Workspace(Path.home() / ".aetron" / "recent-projects.json")
        with make_server(workspace, args.port) as server:
            url = f"http://127.0.0.1:{server.server_port}"
            print(f"Aetron is running at {url}\nKeep this window open. Press Ctrl+C to stop.", flush=True)
            if not args.no_browser:
                webbrowser.open(url)
            server.serve_forever()
    except KeyboardInterrupt:
        print("\nAetron stopped.")
    except OSError as exc:
        print(f"Cannot start Aetron: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
