"""Protect the local browser boundary as well as the retrieval sequence.

HTTP requests must not turn a local code viewer into a filesystem service for
other websites. Real pipeline fixtures also catch stale selections after a
project refresh, which ordinary command-line tests cannot exercise.
"""

from http.client import HTTPConnection
import json
import re
from threading import Thread
from unittest.mock import Mock

import pytest

from aetron import web


@pytest.fixture
def workspace(tmp_path, make_project):
    root = make_project({"combat.py": "def combat():\n    return 123\n"})
    ws = web.Workspace(tmp_path / "history.json")
    ws.request("open", {"path": str(root)})
    return ws


def call(ws, action, **data):
    return ws.request(action, {"revision": ws.revision, **data})


def test_progressive_disclosure_and_cache(workspace, monkeypatch):
    monkeypatch.setattr(web, "scan", Mock(side_effect=AssertionError("unexpected scan")))
    with pytest.raises(ValueError, match="outline first"):
        call(workspace, "source", path="combat.py", name="combat")
    with pytest.raises(ValueError, match="search results"):
        call(workspace, "structure", path="combat.py")
    found = call(workspace, "search", query="combat")
    assert found["candidates"][0]["path"] == "combat.py"
    assert "return 123" not in str(found)
    outline = call(workspace, "structure", path="combat.py")
    assert "return 123" not in str(outline)
    result = call(workspace, "source", path="combat.py", name="combat")
    assert "return 123" in result["text"]
    assert result["location"] == "combat.py:1"
    assert "Limitations" in call(workspace, "summary")["text"]


def test_refresh_invalidates_old_selections(workspace):
    call(workspace, "search", query="combat")
    call(workspace, "structure", path="combat.py")
    old = workspace.revision
    call(workspace, "refresh")
    with pytest.raises(ValueError, match="project changed"):
        workspace.request("source", {"revision": old, "path": "combat.py", "name": "combat"})
    with pytest.raises(ValueError, match="outline first"):
        call(workspace, "source", path="combat.py", name="combat")


def test_failed_open_preserves_project(workspace):
    root = workspace.scanned.root
    with pytest.raises(ValueError):
        call(workspace, "open", path=str(root / "missing"))
    assert workspace.scanned.root == root
    assert workspace.state()["recent"] == [str(root)]


def test_new_search_revokes_previous_outline(workspace):
    call(workspace, "search", query="combat")
    call(workspace, "structure", path="combat.py")
    assert call(workspace, "search", query="nothinghere")["candidates"] == []
    with pytest.raises(ValueError, match="outline first"):
        call(workspace, "source", path="combat.py", name="combat")


@pytest.fixture
def http(workspace):
    server = web.make_server(workspace)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()

    def request(method, path, body=None, headers=None):
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read().decode(), dict(response.getheaders())
        finally:
            connection.close()

    yield request
    server.shutdown()
    server.server_close()
    worker.join()


def test_page_and_authenticated_api(http):
    status, page, headers = http("GET", "/")
    assert status == 200
    token = re.search(r'<script nonce="([^"]+)"', page)[1]
    assert token in headers["Content-Security-Policy"]
    status, body, _ = http("POST", "/api/state", "{}", {"X-Aetron-Token": token})
    assert status == 200
    assert json.loads(body)["files"] == 1


def test_cross_site_and_arbitrary_file_access_are_refused(http):
    assert http("POST", "/api/state", "{}")[0] == 403
    assert http("GET", "/", headers={"Host": "evil.example"})[0] == 403
    assert http("GET", "/", headers={"Origin": "https://evil.example"})[0] == 403
    assert http("GET", "/../../combat.py")[0] == 404


def test_malformed_request_does_not_stop_server(http):
    _, page, _ = http("GET", "/")
    token = re.search(r'<script nonce="([^"]+)"', page)[1]
    headers = {"X-Aetron-Token": token}
    assert http("POST", "/api/state", "[", headers)[0] == 400
    assert http("POST", "/api/state", "[]", headers)[0] == 400
    assert http("POST", "/api/state", "{}", headers)[0] == 200
