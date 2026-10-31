"""Cliente mínimo de la API REST de Home Assistant."""
import json
from typing import Dict, List, Optional

from .http import HttpError, Transport, http_json


class HAError(Exception):
    pass


class HAAuthError(HAError):
    pass


class HAUnreachable(HAError):
    """Error de red: HA no respondió (timeout, conexión rechazada, DNS...), no un status HTTP."""


_AREAS_TEMPLATE = (
    "{% set ns = namespace(out=[]) %}"
    "{% for s in states if s.domain in " + "{domains}" + " %}"
    '{% set ns.out = ns.out + [{"e": s.entity_id, "a": area_name(s.entity_id)}] %}'
    "{% endfor %}{{ ns.out | to_json }}"
)


class HAClient:
    def __init__(self, base_url: str, token: str, timeout_s: float, transport: Transport = http_json):
        self._base = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout_s
        self._transport = transport

    def _request(self, method: str, path: str, payload=None):
        headers = {"Authorization": "Bearer " + self._token, "Content-Type": "application/json"}
        body = json.dumps(payload).encode() if payload is not None else None
        try:
            status, raw = self._transport(method, self._base + path, headers, body, self._timeout)
        except HttpError as e:
            raise HAUnreachable("HA no accesible: %s" % e) from None
        return status, raw

    @staticmethod
    def _check(status: int, path: str) -> None:
        if status in (401, 403):
            raise HAAuthError("HA rechazó la autenticación (%d) en %s" % (status, path))
        if status >= 400:
            raise HAError("HA devolvió %d en %s" % (status, path))

    def _json(self, method: str, path: str, payload=None):
        status, raw = self._request(method, path, payload)
        self._check(status, path)
        try:
            return json.loads(raw)
        except ValueError:
            raise HAError("Respuesta de HA no es JSON en %s" % path) from None

    def get_states(self) -> List[dict]:
        return self._json("GET", "/api/states")

    def get_state(self, entity_id: str) -> Optional[dict]:
        path = "/api/states/" + entity_id
        status, raw = self._request("GET", path)
        if status == 404:
            return None
        self._check(status, path)
        try:
            return json.loads(raw)
        except ValueError:
            raise HAError("Respuesta de HA no es JSON en %s" % path) from None

    def call_service(self, domain: str, service: str, data: dict) -> List[dict]:
        return self._json("POST", "/api/services/%s/%s" % (domain, service), data)

    def areas(self, domains: List[str]) -> Dict[str, Optional[str]]:
        template = _AREAS_TEMPLATE.replace("{domains}", json.dumps(list(domains)))
        path = "/api/template"
        status, raw = self._request("POST", path, {"template": template})
        self._check(status, path)
        try:
            items = json.loads(raw.decode("utf-8"))
            return {i["e"]: i.get("a") or None for i in items}
        except (ValueError, TypeError, KeyError, AttributeError):
            raise HAError("Salida de template de áreas inválida") from None
