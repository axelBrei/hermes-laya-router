"""Cliente de Laya (decisiones) vía Bifrost."""
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union

from .http import HttpError, Transport, http_json


class LayaError(Exception):
    """Fallo al consultar Laya. Mensajes fijos: nunca incluyen cuerpo, URL ni headers."""


@dataclass
class Question:
    type: str  # predicate | choice | score
    name: str
    instructions: str
    choices: Optional[List[Tuple[str, str]]] = None

    def to_payload(self) -> dict:
        payload = {"type": self.type, "name": self.name, "instructions": self.instructions}
        if self.type == "choice":
            payload["choices"] = [{"value": v, "description": d} for v, d in (self.choices or [])]
        return payload


@dataclass
class Answer:
    name: str
    type: str
    value: Union[str, bool, float, None]
    confidence: float
    probabilities: Dict[str, float] = field(default_factory=dict)


def _num(x: Any) -> Optional[float]:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    return float(x)


def _probabilities(raw: dict) -> Dict[str, float]:
    items = raw.get("probabilities")
    out: Dict[str, float] = {}
    if not isinstance(items, list):
        return out
    for it in items:
        if not isinstance(it, dict):
            continue
        p = _num(it.get("probability"))
        v = it.get("value")
        if p is not None and isinstance(v, (str, bool, int, float)):
            out[str(v)] = p
    return out


def parse_answer(raw: dict, question: Question) -> Answer:
    """Convierte una respuesta cruda de Bifrost en Answer; nunca lanza por campos faltantes o raros."""
    if not isinstance(raw, dict):
        raw = {}
    qtype = question.type
    probs = _probabilities(raw)

    if qtype == "predicate":
        value: Union[str, bool, float, None] = _num(raw.get("probability"))
    elif qtype == "choice":
        value = raw.get("choice")
        if value is not None and not isinstance(value, str):
            value = None
    else:
        value = _num(raw.get("score"))

    conf = _num(raw.get("answer_confidence"))
    if conf is None and qtype == "predicate":
        conf = _num(raw.get("probability"))
    if conf is None and qtype == "choice" and isinstance(value, str):
        conf = probs.get(value)
    if conf is None:
        conf = _num(raw.get("confidence"))
    if conf is None:
        conf = 0.0
    return Answer(name=question.name, type=qtype, value=value, confidence=conf, probabilities=probs)


class LayaClient:
    def __init__(self, base_url: str, model: str, api_key: str, timeout_s: float, transport: Transport = http_json):
        self._base = base_url.rstrip("/")
        self._model = model
        self._key = api_key
        self._timeout = timeout_s
        self._transport = transport

    def decide(self, input_: Union[str, List[dict]], questions: List[Question]) -> Dict[str, Answer]:
        headers = {"x-api-key": self._key, "Content-Type": "application/json"}
        payload = {
            "model": self._model,
            "input": input_,
            "questions": [q.to_payload() for q in questions],
        }
        body = json.dumps(payload).encode()
        try:
            status, raw = self._transport("POST", self._base + "/v1/decisions", headers, body, self._timeout)
        except HttpError as e:
            raise LayaError("Laya no accesible (%s)" % e) from None
        if status != 200:
            raise LayaError("Laya devolvió status %d" % status)
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            raise LayaError("Respuesta de Laya no es JSON") from None
        answers = data.get("answers") if isinstance(data, dict) else None
        if not isinstance(answers, list):
            raise LayaError("Respuesta de Laya sin lista 'answers'")

        by_name = {q.name: q for q in questions}
        out: Dict[str, Answer] = {}
        for i, item in enumerate(answers):
            name = item.get("name") if isinstance(item, dict) else None
            if isinstance(name, str) and name in by_name:
                q = by_name[name]
            elif i < len(questions) and not isinstance(name, str):
                q = questions[i]
            else:
                continue
            out[q.name] = parse_answer(item, q)
        return out
