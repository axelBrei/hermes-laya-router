"""Transport HTTP mínimo sobre urllib (inyectable en tests)."""
import urllib.error
import urllib.request
from typing import Callable, Optional, Tuple

# (method, url, headers, body, timeout_s) -> (status, body)
Transport = Callable[[str, str, dict, Optional[bytes], float], Tuple[int, bytes]]


class HttpError(Exception):
    """Error de red (no de status HTTP)."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """No sigue redirecciones: urllib reenviaría Authorization / x-api-key al destino.

    Devolver None hace que el 3xx llegue como HTTPError y se devuelva como (status, body).
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)

# urllib se presenta como "Python-urllib/3.x", que Cloudflare y otros WAF rechazan con 403.
USER_AGENT = "hermes-laya-router/0.1"


def http_json(method: str, url: str, headers: dict, body: Optional[bytes], timeout_s: float) -> Tuple[int, bytes]:
    headers = dict(headers or {})
    if not any(k.lower() == "user-agent" for k in headers):
        headers["User-Agent"] = USER_AGENT
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with _OPENER.open(req, timeout=timeout_s) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        try:
            try:
                data = e.read()
            except Exception:
                data = b""
            return e.code, data
        finally:
            try:
                e.close()
            except Exception:
                pass
    except Exception as e:  # URLError, timeout, ssl, etc.
        # Solo el tipo de error: la URL/headers no deben filtrarse.
        raise HttpError(type(e).__name__) from None
