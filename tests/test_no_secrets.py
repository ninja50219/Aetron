"""Nothing that looks like a credential is in the repository, or about to be.

Aetron talks to hosted models with API keys, and the people using it paste
those keys into shells, .env files and scratch scripts inside the very
projects they point it at - this one included. A key that reaches a public
repository is compromised the moment it is pushed; scanners find new ones in
minutes. So this runs with the rest of the suite, which CONTRIBUTING.md says
must pass before every push.

What it reads is what git would publish: tracked files, plus untracked files
that .gitignore does not exclude, since those are one ``git add .`` away from
being tracked. Ignored files are left alone - ignoring them is the defence.

The patterns live in aetron/credentials.py, which the ask loop also uses to
keep keys out of what a model is shown. They are for key formats that are
unambiguous by shape, so the test fires on real keys and not on fixtures.
Test code that needs a fake key builds it at runtime (see
test_ai_providers.py) so that no key-shaped literal is ever committed.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from aetron.credentials import KEY_PATTERNS, find_credentials

ROOT = Path(__file__).resolve().parent.parent


# File names that are secrets by convention, whatever they contain.
SECRET_NAMES = re.compile(
    r"(^|/)(\.env(\.[^/]*)?|[^/]*\.(pem|key|p12|pfx)|id_(rsa|ed25519|ecdsa)[^/]*"
    r"|credentials\.json|secrets\.(json|toml|ya?ml))$"
)
# The one conventional exception: a template listing variable names, no values.
ALLOWED_NAMES = {".env.example"}


def _publishable_files() -> list[str]:
    """Paths git would push: tracked, plus untracked and not ignored."""
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout, so there is nothing about to be pushed")
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT, capture_output=True, check=True,
    )
    return sorted(
        {p for p in result.stdout.decode("utf-8", "replace").split("\0") if p}
    )


def test_no_file_contains_something_shaped_like_a_key():
    found = []
    for rel_path in _publishable_files():
        path = ROOT / rel_path
        if not path.is_file():
            continue  # deleted in the working tree but still in the index
        text = path.read_bytes().decode("utf-8", errors="ignore")
        for name, match in find_credentials(text):
            line = text.count("\n", 0, match.start()) + 1
            # Only a prefix is shown, so a failure does not print the key.
            found.append(f"{rel_path}:{line}: {name} ({match.group()[:8]}...)")
    assert not found, (
        "Something that looks like a credential would be pushed. Remove it, "
        "revoke the key with its provider, and read it from an environment "
        "variable instead:\n  " + "\n  ".join(found)
    )


def test_no_file_is_named_like_a_secret():
    found = [
        p for p in _publishable_files()
        if SECRET_NAMES.search(p) and Path(p).name not in ALLOWED_NAMES
    ]
    assert not found, (
        "These files are secrets by name and would be pushed. Delete them or "
        "add them to .gitignore:\n  " + "\n  ".join(found)
    )


class TestThePatternsThemselves:
    """A scanner that never fires looks exactly like a clean repository."""

    @pytest.mark.parametrize(
        "name, sample",
        [
            ("Anthropic API key", "sk-ant-api03-" + "A1b2" * 10),
            ("OpenAI API key", "sk-proj-" + "Z9y8" * 10),
            ("Google API key", "AIza" + "S" * 35),
            ("GitHub token", "ghp_" + "a" * 36),
            ("AWS access key", "AKIA" + "ABCDEFGHIJKLMNOP"),
            ("private key", "-----BEGIN OPENSSH " + "PRIVATE KEY-----"),
        ],
    )
    def test_a_real_shaped_key_is_caught(self, name, sample):
        assert re.search(KEY_PATTERNS[name], f"key = '{sample}'")

    @pytest.mark.parametrize(
        "text",
        [
            'os.environ.get("OPENAI_API_KEY")',
            "setx ANTHROPIC_API_KEY \"your-key\"",
            "token = secrets.token_urlsafe(32)",
            "sk-" + "short",
            "task-budgets-2026-03-13",
        ],
    )
    def test_ordinary_code_about_keys_is_not(self, text):
        assert not any(re.search(p, text) for p in KEY_PATTERNS.values())

    @pytest.mark.parametrize(
        "path", [".env", "app/.env.local", "certs/server.pem", "id_rsa", "deploy/credentials.json"]
    )
    def test_a_secret_file_name_is_caught(self, path):
        assert SECRET_NAMES.search(path)

    @pytest.mark.parametrize("path", ["aetron/scanner/gitignore.py", "keys.md", "tests/test_env.py"])
    def test_an_ordinary_file_name_is_not(self, path):
        assert not SECRET_NAMES.search(path)


def test_gitignore_keeps_env_files_out():
    """The second line of defence: git itself refuses to see them."""
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    probes = [".env", ".env.local", "config/.env.production", "server.pem", "api.key",
              "secrets.toml", "credentials.json"]
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", *probes], cwd=ROOT, capture_output=True, text=True
    )
    ignored = set(result.stdout.split())
    assert ignored == set(probes), f"not ignored: {sorted(set(probes) - ignored)}"
