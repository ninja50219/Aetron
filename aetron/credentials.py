"""What a credential looks like, so that one is never passed along by accident.

Two places need to know. The test suite refuses to let one be pushed to the
repository, and the ask loop refuses to let one reach a model: the project a
question is about is somebody's working tree, working trees contain keys that
were only ever meant to be temporary, and a hosted model is a third party.
Level 3 exists to send a single definition rather than a whole file, and a
single definition can still be the one holding a key.

The patterns cover formats that are unambiguous by shape - a prefix a
provider stamps on every key it issues. A generic "password = ..." rule would
hide ordinary code from the model and fire on every test fixture, and a check
that fires on everything is a check people learn to ignore.
"""

import re

KEY_PATTERNS = {
    "Anthropic API key": r"sk-ant-[A-Za-z0-9_\-]{20,}",
    # The lookahead keeps an Anthropic key from being reported twice.
    "OpenAI API key": r"sk-(?!ant-)(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{32,}",
    "Google API key": r"AIza[0-9A-Za-z_\-]{35}",
    "GitHub token": r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{60,}",
    "AWS access key": r"(?:AKIA|ASIA)[0-9A-Z]{16}",
    "Slack token": r"xox[abprs]-[A-Za-z0-9-]{10,}",
    "Hugging Face token": r"hf_[A-Za-z0-9]{34,}",
    "private key": r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY",
}

_COMPILED = {name: re.compile(pattern) for name, pattern in KEY_PATTERNS.items()}


def find_credentials(text: str) -> list[tuple[str, re.Match]]:
    """Every credential-shaped string in ``text``, with what it looks like."""
    return [
        (name, match)
        for name, pattern in _COMPILED.items()
        for match in pattern.finditer(text)
    ]


def hide_credentials(text: str) -> tuple[str, int]:
    """``text`` with each credential replaced by a note saying what it was.

    The note names the kind of key so the model can still reason about the
    code - "this reads an OpenAI key from a literal" is a finding worth
    reporting - without ever holding the key itself.
    """
    hidden = 0
    for name, pattern in _COMPILED.items():
        text, count = pattern.subn(f"[hidden by Aetron: {name}]", text)
        hidden += count
    return text, hidden
