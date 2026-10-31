#!/usr/bin/env python3
"""Prueba manual contra Bifrost real: imprime el cuerpo crudo de /v1/decisions.

Verifica el formato real de `answers` (campos probability/choice/score/etc.).
Uso (en el home lab, no en CI):
    LAYA_BIFROST_URL=... LAYA_BIFROST_API_KEY=... python scripts/probe_bifrost.py
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from laya_router.config import DEFAULT_BIFROST_MODEL  # noqa: E402
from laya_router.http import HttpError, http_json  # noqa: E402
from laya_router.laya_client import Question  # noqa: E402

URL_ENV = "LAYA_BIFROST_URL"
KEY_ENV = "LAYA_BIFROST_API_KEY"


def main() -> int:
    url = os.environ.get(URL_ENV)
    key = os.environ.get(KEY_ENV)
    missing = [name for name, value in ((URL_ENV, url), (KEY_ENV, key)) if not value]
    if missing:
        print("Faltan variables de entorno: %s (URL base y API key de Bifrost)." % ", ".join(missing), file=sys.stderr)
        return 1
    questions = [
        Question("predicate", "is_home", "¿El mensaje pide una acción sobre dispositivos de la casa (luces, aire, enchufes)?"),
        Question("choice", "action", "¿Qué acción pide el mensaje?", [
            ("turn_on", "encender un dispositivo"),
            ("turn_off", "apagar un dispositivo"),
            ("none", "ninguna acción"),
        ]),
        Question("choice", "target", "¿Sobre qué dispositivo?", [
            ("switch.luz_oficina", "luz de la oficina"),
            ("light.living", "luz del living"),
            ("none", "ninguna entidad concreta o no aplica"),
        ]),
    ]
    payload = {
        "model": DEFAULT_BIFROST_MODEL,
        "input": "prendé la luz de la oficina",
        "questions": [q.to_payload() for q in questions],
    }
    try:
        status, raw = http_json(
            "POST", url.rstrip("/") + "/v1/decisions",
            {"x-api-key": key, "Content-Type": "application/json"},
            json.dumps(payload).encode(), 10.0,
        )
    except HttpError as e:
        print("Error de red: %s" % e, file=sys.stderr)
        return 1
    print("status:", status, file=sys.stderr)
    try:
        print(json.dumps(json.loads(raw), indent=2, ensure_ascii=False))
    except ValueError:
        print("(cuerpo no JSON)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
