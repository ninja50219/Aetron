"""What Aetron remembers about a project between runs: its summary.

A summary is the one answer worth keeping. Every question about a project
starts better knowing what the project is - "this seems to be a Unity game
with its movement in PlayerMovment.cs" is sixty tokens that save a model
from rediscovering it - and writing one costs a model several requests. So it
is written once, stored beside the recent-projects list in the user's home
directory (never inside the project, where it could be committed), and
reused until the project changes.

"Changes" is measured, not guessed: a fingerprint of every indexed file's
path, size and modification time. A summary whose fingerprint no longer
matches is still returned, marked stale, because a slightly old account of a
project is far more useful than none - and the page offers to rewrite it.
"""

import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from aetron.scanner.scanner import ScanResult


@dataclass
class Note:
    text: str
    model: str
    written: float
    # False when the project has changed since the summary was written.
    fresh: bool


def fingerprint(scan_result: ScanResult) -> str:
    """A digest of what the index was built from, for spotting a change."""
    digest = hashlib.sha256()
    for file_info in sorted(scan_result.files, key=lambda f: f.rel_path):
        try:
            modified = int(os.stat(file_info.path).st_mtime)
        except OSError:
            modified = 0
        digest.update(f"{file_info.rel_path}\0{file_info.size}\0{modified}\n".encode())
    return digest.hexdigest()


def _path_for(directory: Path, root: Path) -> Path:
    key = hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:24]
    return directory / f"{key}.json"


def load_summary(directory: Path, scan_result: ScanResult) -> Note | None:
    """The stored summary of this project, or None when there is none."""
    try:
        data = json.loads(_path_for(directory, scan_result.root).read_text(encoding="utf-8"))
        text = str(data["text"]).strip()
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not text:
        return None
    return Note(
        text=text,
        model=str(data.get("model", "")),
        written=float(data.get("written", 0)),
        fresh=data.get("fingerprint") == fingerprint(scan_result),
    )


def save_summary(directory: Path, scan_result: ScanResult, text: str, model: str) -> Note | None:
    """Store a summary; an unwritable directory costs the cache, not the answer."""
    note = Note(text=text.strip(), model=model, written=time.time(), fresh=True)
    target = _path_for(directory, scan_result.root)
    temporary = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=target.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(
                {
                    "root": str(scan_result.root),
                    "fingerprint": fingerprint(scan_result),
                    "text": note.text,
                    "model": model,
                    "written": note.written,
                },
                handle,
                ensure_ascii=False,
                indent=2,
            )
        os.replace(temporary, target)
    except OSError:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        return None
    return note
