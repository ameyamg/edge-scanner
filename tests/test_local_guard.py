"""The local-only guard: foreign pages cannot write, rebound hostnames cannot talk
to the API at all, and the static route never resolves a UNC path (audit 3, S1).

Every request here is one a web page in the user's browser could make while the
scanner runs: a simple cross-origin POST, a DNS-rebinding page whose Host and
Origin are both the attacker's name, a cross-origin WebSocket, an <img> GET."""
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from scanner import local_guard

LOCAL = "http://127.0.0.1:7777"
EVIL = "https://evil.example"


@pytest.fixture(autouse=True)
def _real_allowlist(monkeypatch):
    monkeypatch.delenv("SCANNER_ALLOWED_HOSTS", raising=False)
    monkeypatch.setattr(local_guard, "_extra_hosts", set())


@pytest.fixture
def client(tmp_path, monkeypatch):
    """The real app, with settings.reset recorded instead of run."""
    monkeypatch.chdir(tmp_path)
    from scanner.api import AppState, create_app
    from scanner.settings import settings
    calls = []
    monkeypatch.setattr(type(settings), "reset", lambda self, **kw: calls.append(kw) or {"reset": True})
    c = TestClient(create_app(AppState(SimpleNamespace(_states={}), None)), base_url=LOCAL)
    c.reset_calls = calls
    return c


# ── simple cross-origin POSTs ────────────────────────────────────────────────

def test_foreign_origin_cannot_post_even_without_a_body(client):
    r = client.post("/api/v2/settings/reset", headers={"Origin": EVIL})
    assert r.status_code == 403 and client.reset_calls == []
    r = client.post("/api/v2/settings/reset", json={}, headers={"Origin": EVIL})
    assert r.status_code == 403 and client.reset_calls == []
    r = client.post("/api/v2/settings/reset", json={}, headers={"Origin": "null"})
    assert r.status_code == 403 and client.reset_calls == []


@pytest.mark.parametrize("ctype", [None, "text/plain", "application/x-www-form-urlencoded", "multipart/form-data"])
def test_post_must_be_json(client, ctype):
    """What a no-cors fetch can send without a preflight. No Content-Type at all
    used to be parsed as JSON and applied."""
    headers = {"Content-Type": ctype} if ctype else {}
    r = client.post("/api/v2/settings/reset", content=b"{}", headers=headers)
    assert r.status_code == 415 and client.reset_calls == []


def test_dashboard_and_scripts_still_write(client):
    for origin in ("http://localhost:5174", "http://127.0.0.1:7777", "http://[::1]:7777", None):
        headers = {"Origin": origin} if origin else {}
        r = client.post("/api/v2/settings/reset", json={}, headers=headers)
        assert r.status_code == 200, origin
    assert len(client.reset_calls) == 4


def test_preflight_from_the_vite_dev_server_still_passes(client):
    r = client.options("/api/v2/settings", headers={
        "Origin": "http://localhost:5174", "Access-Control-Request-Method": "PUT"})
    assert r.status_code == 200


# ── DNS rebinding: the attacker's name arrives as Host ───────────────────────

@pytest.mark.parametrize("method,path", [("get", "/api/v2/version"), ("get", "/api/v2/settings"),
                                         ("put", "/api/v2/settings"), ("delete", "/api/v2/layouts/x")])
def test_rebound_hostname_is_refused(client, method, path):
    headers = {"Host": "rebind.evil.example:7777", "Origin": "http://rebind.evil.example:7777"}
    kw = {"json": {}} if method == "put" else {}
    assert getattr(client, method)(path, headers=headers, **kw).status_code == 400


def test_loopback_names_are_accepted(client):
    for host in ("localhost:7777", "127.0.0.1:7777", "[::1]:7777", "LOCALHOST:5174"):
        assert client.get("/api/v2/version", headers={"Host": host}).status_code == 200, host


def test_lan_name_only_when_allowed_on_purpose(client, monkeypatch):
    headers = {"Host": "192.168.1.5:7777", "Origin": "http://192.168.1.5:7777"}
    assert client.get("/api/v2/version", headers=headers).status_code == 400
    local_guard.allow_hosts("192.168.1.5")                  # what --host 192.168.1.5 does
    assert client.get("/api/v2/version", headers=headers).status_code == 200
    assert client.post("/api/v2/settings/reset", json={}, headers=headers).status_code == 200
    local_guard.allow_hosts("0.0.0.0")                      # a wildcard names nothing
    assert "0.0.0.0" not in local_guard.allowed_hosts()
    monkeypatch.setenv("SCANNER_ALLOWED_HOSTS", "trading-pc, trading-pc.lan")
    assert local_guard.host_allowed("trading-pc.lan:7777")


# ── WebSockets ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("headers", [
    {"Origin": EVIL},
    {"Host": "rebind.evil.example:7777", "Origin": "http://rebind.evil.example:7777"},
])
def test_foreign_websocket_is_closed(client, headers):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws/alerts", headers=headers) as ws:
            ws.receive_json()
    assert exc.value.code == 1008


# ── static route: no UNC paths ───────────────────────────────────────────────

@pytest.mark.parametrize("path", ["//evil.example/share/a.js", "\\\\evil.example\\share\\a.js",
                                  "C:/Windows/win.ini", "assets/../../secret", "a\x00b"])
def test_unsafe_static_paths_rejected(path):
    from scanner.api_v2 import _safe_static_path
    assert not _safe_static_path(path)


def test_static_route_never_resolves_a_unc_path(tmp_path, monkeypatch):
    from scanner.api_v2 import mount_v2_static
    (tmp_path / "index.html").write_text("<html>index</html>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("js")
    app = FastAPI()
    mount_v2_static(app, dist=tmp_path)
    resolved = []
    real = Path.resolve
    monkeypatch.setattr(Path, "resolve", lambda self, *a, **k: resolved.append(str(self)) or real(self, *a, **k))
    c = TestClient(app, base_url=LOCAL)
    for url in ("/v2///evil.example/share/a.js", "/v2/%5C%5Cevil.example%5Cshare%5Ca.js"):
        r = c.get(url)
        assert r.status_code == 200 and "index" in r.text              # SPA fallback
    assert not any("evil.example" in p for p in resolved)
    assert c.get("/v2/assets/app.js").text == "js"                      # real assets still served
