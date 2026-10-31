"""Prefiltro, preguntas a Laya y decisión de ruta (skip / execute / hint)."""
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .config import Config
from .entities import Entity
from .laya_client import Answer, Question
from .textnorm import extract_hvac_mode, extract_temperature, lexicon_match, tokens

ACTIONS = ("turn_on", "turn_off", "all_off", "lights_off", "climate_off", "set_climate", "none")
AGGREGATES = {"all_off", "lights_off", "climate_off"}

# Verificación determinista de la acción contra el texto: el clasificador puede confundir
# "prende la luz" con lights_off (apagar TODAS las luces), y eso no se ejecuta sin una palabra de apagado.
_OFF_STEMS = ("apag", "desactiv", "off")
_ON_STEMS = ("prend", "encend", "activ", "pone", "ponga", "ponel")
# Un agregado afecta a TODA la casa: además de apagar, el texto tiene que nombrar el alcance
# ("todo", o el plural de lo que se apaga). "apagá la luz del cuarto" nunca es lights_off.
_ALL_STEMS = ("tod",)
_SCOPE_STEMS = {
    "all_off": ("casa",),
    "lights_off": ("luces", "lampar", "foco"),
    "climate_off": ("aires", "acondicionad", "split", "clima"),
}

_NONE_TARGET = ("none", "ninguno o no aplica")

# Descripciones paralelas: todas nombran el mismo objeto y solo cambian verbo y alcance. Con "prender un
# dispositivo" vs "apagar todas las luces", Laya elegía lights_off por compartir "luz" con el mensaje.
_ACTION_CHOICES = [
    ("turn_on", "prender o encender una luz, un enchufe o un aire"),
    ("turn_off", "apagar una luz, un enchufe o un aire"),
    ("all_off", "apagar todo lo de la casa"),
    ("lights_off", "apagar todas las luces de la casa"),
    ("climate_off", "apagar todos los aires de la casa"),
    ("set_climate", "cambiar la temperatura de un aire"),
    ("none", "no es una orden para la casa"),
]
assert tuple(v for v, _ in _ACTION_CHOICES) == ACTIONS


def _has_stem(text: str, stems) -> bool:
    return any(t.startswith(p) for t in tokens(text) for p in stems)


def _intent_mismatch(action: str, text: str) -> bool:
    """True si el texto no respalda la acción: apagar exige una palabra de apagado; prender
    exige una de encendido y ninguna de apagado."""
    off = _has_stem(text, _OFF_STEMS)
    if action in AGGREGATES:
        return not off or not _has_stem(text, _ALL_STEMS + _SCOPE_STEMS[action])
    if action == "turn_off":
        return not off
    if action == "turn_on":
        return off or not _has_stem(text, _ON_STEMS)
    return False


def prefilter(text: str, cfg: Config) -> bool:
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped or stripped.startswith("/"):
        return False
    if len(text) > cfg.max_message_chars:
        return False
    return lexicon_match(stripped, cfg.prefilter_lexicon)


def build_questions(candidates: List[Entity]) -> List[Question]:
    targets = [(e.entity_id, e.description()) for e in candidates] + [_NONE_TARGET]
    return [
        Question(
            type="predicate",
            name="is_home",
            instructions="¿Pide controlar luces, aires o dispositivos de la casa?",
            choices=None,
        ),
        Question(
            type="choice",
            name="action",
            instructions="¿Qué acción pide sobre la casa?",
            choices=list(_ACTION_CHOICES),
        ),
        Question(
            type="choice",
            name="target",
            instructions="¿Sobre qué dispositivo?",
            choices=targets,
        ),
    ]


def build_input(text: str, history: List[dict], history_turns: int) -> List[dict]:
    out: List[dict] = []
    n = 2 * max(int(history_turns), 0)
    if n and history:
        for m in list(history)[-n:]:
            if (
                isinstance(m, dict)
                and m.get("role") in ("user", "assistant")
                and isinstance(m.get("content"), str)
            ):
                out.append({"role": m["role"], "content": m["content"]})
    # Hermes ya incluye el mensaje actual en el historial: no mandarlo dos veces.
    if out and out[-1] == {"role": "user", "content": text}:
        out.pop()
    out.append({"role": "user", "content": text})
    return out


@dataclass
class Decision:
    path: str  # skip | execute | hint
    action: str
    target: Optional[Entity]
    hvac_mode: Optional[str]
    temperature: Optional[int]
    confidences: Dict[str, float] = field(default_factory=dict)
    reason: str = ""


def _conf(a: Optional[Answer]) -> float:
    c = getattr(a, "confidence", None)
    if isinstance(c, bool) or not isinstance(c, (int, float)) or not math.isfinite(c):
        return 0.0
    return float(c)


def _is_home_value(a: Answer) -> float:
    v = a.value
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return 0.0
    return float(v)


def decide(
    text: str,
    answers: Dict[str, Answer],
    candidates: List[Entity],
    cfg: Config,
    fast_allowed: bool,
    *,
    targets_exact: bool = True,
) -> Decision:
    """targets_exact=False: la lista de candidatos salió de un fallback o se truncó, así que el
    target elegido puede no ser el correcto; las acciones sobre un dispositivo van a hint."""
    answers = answers or {}
    is_home = answers.get("is_home")
    action_ans = answers.get("action")
    target_ans = answers.get("target")
    confs = {
        "is_home": _conf(is_home),
        "action": _conf(action_ans),
        "target": _conf(target_ans),
    }

    def skip(reason: str, action: str = "none") -> Decision:
        return Decision("skip", action, None, None, None, confs, reason)

    if is_home is None:
        return skip("sin respuesta is_home")
    if _is_home_value(is_home) < cfg.thresholds.is_home:
        return skip("is_home bajo el umbral")
    if action_ans is None or not isinstance(action_ans.value, str):
        return skip("sin respuesta action")
    action = action_ans.value
    if action not in ACTIONS:
        return skip("action desconocida")
    if action == "none":
        return skip("action none")

    target: Optional[Entity] = None
    if target_ans is not None and isinstance(target_ans.value, str) and target_ans.value != "none":
        for e in candidates:
            if e.entity_id == target_ans.value:
                target = e
                break

    hvac_mode = temperature = None
    if action == "set_climate":
        hvac_mode = extract_hvac_mode(text)
        temperature = extract_temperature(text)

    thr = cfg.thresholds.execute
    reason = ""
    if not fast_allowed:
        reason = "ejecución directa no permitida"
    elif _intent_mismatch(action, text):
        reason = "la acción no coincide con el texto"
    elif confs["action"] < thr:
        reason = "action bajo el umbral execute"
    elif action not in AGGREGATES and not targets_exact:
        reason = "lista de candidatos truncada o ambigua"
    elif action not in AGGREGATES and target is None:
        reason = "target ausente o desconocido"
    elif action not in AGGREGATES and confs["target"] < thr:
        reason = "target bajo el umbral execute"
    elif action == "set_climate" and (target is None or target.domain != "climate"):
        reason = "set_climate requiere un target climate"

    if reason:
        return Decision("hint", action, target, hvac_mode, temperature, confs, reason)
    return Decision("execute", action, target, hvac_mode, temperature, confs, "umbrales cumplidos")
