"""Physical line counts must agree for documentation and source."""

import pytest

from aetron.scanner import scan


@pytest.mark.parametrize("text,count", [
    ("", 0), ("x=1", 1), ("x=1\n", 1), ("\n", 1),
    ("x=1\n\n", 2), ("x=1\r\ny=2\r\n", 2),
])
def test_line_counts(make_project, text, count):
    root = make_project({})
    for name in ("app.py", "README.md"):
        (root / name).write_bytes(text.encode("utf-8"))
    result = scan(root)
    assert result.files[0].lines == count
    assert result.docs[0].lines == count
    assert result.total_lines == count
