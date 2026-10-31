"""Tests del transport HTTP sobre urllib (servidores locales en 127.0.0.1, sin red externa)."""
import io
import socket
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import laya_router.http as http_mod
from laya_router.http import HttpError, http_json

SECRET = "Bearer tok-secreto"


class _Server:
    """HTTPServer local en un thread; registra los headers de cada request."""

    def __init__(self, respond):
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self):
                outer.requests.append({"path": self.path, "headers": dict(self.headers)})
                status, headers, body = respond(self)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = _handle

            def log_message(self, *a):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)


@pytest.fixture
def servers():
    created = []

    def make(respond):
        s = _Server(respond)
        created.append(s)
        return s

    yield make
    for s in created:
        s.close()


def test_ok_returns_status_and_body(servers):
    s = servers(lambda h: (200, {}, b'{"ok": true}'))
    assert http_json("GET", s.url + "/api/", {"Authorization": SECRET}, None, 2) == (200, b'{"ok": true}')


def test_default_user_agent_is_not_python_urllib(servers):
    # Cloudflare y otros WAF bloquean "Python-urllib" con 403 (error 1010).
    s = servers(lambda h: (200, {}, b"{}"))
    http_json("GET", s.url + "/api/", {}, None, 2)
    ua = s.requests[0]["headers"]["User-Agent"]
    assert ua.startswith("hermes-laya-router/")
    assert "urllib" not in ua.lower()


def test_explicit_user_agent_is_respected(servers):
    s = servers(lambda h: (200, {}, b"{}"))
    http_json("GET", s.url + "/api/", {"User-Agent": "otro/1"}, None, 2)
    assert s.requests[0]["headers"]["User-Agent"] == "otro/1"


def test_redirect_is_not_followed_and_auth_never_reaches_target(servers):
    target = servers(lambda h: (200, {}, b"robado"))
    origin = servers(lambda h: (302, {"Location": target.url + "/captura"}, b"movido"))
    status, body = http_json("GET", origin.url + "/api/states",
                             {"Authorization": SECRET, "x-api-key": "key-secreta"}, None, 2)
    assert status == 302 and body == b"movido"
    assert target.requests == []
    assert origin.requests[0]["headers"].get("Authorization") == SECRET


def test_redirect_on_post_is_not_followed(servers):
    target = servers(lambda h: (200, {}, b"robado"))
    origin = servers(lambda h: (307, {"Location": target.url + "/x"}, b""))
    status, _ = http_json("POST", origin.url + "/v1/decisions", {"x-api-key": "key-secreta"}, b"{}", 2)
    assert status == 307 and target.requests == []


def test_http_error_returns_code_and_body(servers):
    s = servers(lambda h: (500, {}, b'{"message": "boom"}'))
    assert http_json("GET", s.url + "/x", {}, None, 2) == (500, b'{"message": "boom"}')


def test_connection_refused_is_http_error_without_url_or_token():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()  # puerto libre: nadie escucha
    url = "http://127.0.0.1:%d/api/states?token=tok-secreto" % port
    with pytest.raises(HttpError) as ei:
        http_json("GET", url, {"Authorization": SECRET}, None, 1)
    msg = str(ei.value)
    assert "127.0.0.1" not in msg and "tok-secreto" not in msg and str(port) not in msg
    assert ei.value.__cause__ is None and ei.value.__suppress_context__


class _FakeOpener:
    def __init__(self, exc):
        self.exc = exc

    def open(self, req, timeout=None):
        raise self.exc


@pytest.mark.parametrize("exc", [
    urllib.error.URLError("no se pudo conectar a http://ha.local con Bearer tok-secreto"),
    socket.timeout("timed out http://ha.local tok-secreto"),
    TimeoutError("tok-secreto"),
])
def test_network_errors_become_http_error_without_secrets(monkeypatch, exc):
    monkeypatch.setattr(http_mod, "_OPENER", _FakeOpener(exc))
    with pytest.raises(HttpError) as ei:
        http_json("GET", "http://ha.local/api/states", {"Authorization": SECRET}, None, 1)
    assert "tok-secreto" not in str(ei.value) and "ha.local" not in str(ei.value)


class _BrokenBody(io.BytesIO):
    def __init__(self):
        super().__init__(b"")
        self.closed_called = False

    def read(self, *a):
        raise OSError("lectura rota")

    def close(self):
        self.closed_called = True
        super().close()


def test_http_error_with_unreadable_body_returns_empty_and_closes(monkeypatch):
    fp = _BrokenBody()
    err = urllib.error.HTTPError("http://ha.local/x", 503, "Service Unavailable", {}, fp)
    monkeypatch.setattr(http_mod, "_OPENER", _FakeOpener(err))
    assert http_json("GET", "http://ha.local/x", {}, None, 1) == (503, b"")
    assert fp.closed_called


def test_http_error_is_closed_after_reading(monkeypatch):
    fp = io.BytesIO(b"cuerpo")
    err = urllib.error.HTTPError("http://ha.local/x", 404, "Not Found", {}, fp)
    monkeypatch.setattr(http_mod, "_OPENER", _FakeOpener(err))
    assert http_json("GET", "http://ha.local/x", {}, None, 1) == (404, b"cuerpo")
    assert fp.closed
