import sys
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent.parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


import json
from urllib.parse import urlsplit

import pytest


class FakeTransport:
    """Transport falso. Rutas indexadas por (method, path sin base ni query)."""

    def __init__(self):
        self.routes = {}  # (method, path) -> (status, obj)
        # (method, path) -> [(status, obj), ...]: respuestas sucesivas; el último se repite.
        # Tiene prioridad sobre .routes.
        self.sequence = {}
        self.calls = []  # (method, url, body_dict)
        self.called_routes = []  # (method, path)
        self.last_headers = {}

    def __call__(self, method, url, headers, body, timeout_s):
        path = urlsplit(url).path
        self.called_routes.append((method, path))
        self.last_headers = dict(headers)
        self.calls.append((method, url, json.loads(body) if body else None))
        seq = self.sequence.get((method, path))
        if seq:
            route = seq.pop(0) if len(seq) > 1 else seq[0]
        else:
            route = self.routes.get((method, path))
        if route is None:
            return 404, json.dumps({"message": "fake_transport: ruta no configurada"}).encode()
        status, obj = route
        if isinstance(obj, str):
            return status, obj.encode()
        return status, json.dumps(obj).encode()


@pytest.fixture
def fake_transport():
    return FakeTransport()


class FakeSleep:
    """Reemplazo de time.sleep: registra los segundos pedidos sin dormir."""

    def __init__(self):
        self.calls = []

    def __call__(self, seconds):
        self.calls.append(seconds)

    @property
    def total(self):
        return sum(self.calls)


@pytest.fixture
def fake_sleep():
    return FakeSleep()
