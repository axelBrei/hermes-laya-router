"""Ejecución de acciones en Home Assistant con verificación del estado resultante."""
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .config import Config
from .entities import Entity, EntityCache
from .ha_client import HAClient, HAError, HAUnreachable

log = logging.getLogger(__name__)

CONFIRMED = "confirmed"
SENT = "sent"
FAILED = "failed"
ERROR = "error"

_VERBS = {"turn_on": "encender", "turn_off": "apagar", "set_climate": "configurar"}

# Agregados: acción -> (dominios, texto para el mensaje de error)
_AGGREGATES = {
    "all_off": (None, "apagar todo"),  # None = cfg.domains
    "lights_off": (["light", "switch"], "apagar las luces"),
    "climate_off": (["climate"], "apagar los aires"),
}

_MODES_ES = {
    "cool": "frío",
    "heat": "calor",
    "fan_only": "ventilación",
    "auto": "automático",
    "heat_cool": "frío/calor",
    "dry": "seco",
}

# Plazo total (reloj de pared) de un execute(): service calls + verificación. Los hooks de
# Hermes tienen timeout de 30 s; con HA caído no hay que acercarse. No es clave de config.
EXECUTE_DEADLINE_S = 8.0

# Estados internos de verificación (nunca salen en ExecResult.status).
_UNRESPONSIVE = "unresponsive"
_TIMEOUT = "timeout"
_UNRESPONSIVE_STATES = ("unavailable", "unknown")

# Estados de climate que NO cuentan como "encendido".
_CLIMATE_NOT_ON = ("off", "unavailable", "unknown")


@dataclass
class ExecResult:
    ok: bool
    status: str  # confirmed | sent | failed | error
    entity_ids: List[str]
    message: str
    failed: List[str] = field(default_factory=list)


def _fmt_temp(value) -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(f)) if f == int(f) else ("%g" % f)


def _temp_equal(actual, expected) -> bool:
    try:
        return abs(float(actual) - float(expected)) < 1e-6
    except (TypeError, ValueError):
        return False


def _on_off_word(entity: Entity, on: bool) -> str:
    if entity.domain == "climate":
        return "encendido" if on else "apagado"
    return "encendida" if on else "apagada"


def _describe_state(entity: Entity, obs: dict, with_temp: bool) -> str:
    """Estado observado, en español, para el mensaje de falla."""
    state = str(obs.get("state", ""))
    if state == "on":
        out = _on_off_word(entity, True)
    elif state == "off":
        out = _on_off_word(entity, False)
    elif state == "unavailable":
        out = "no disponible"
    elif state in ("unknown", ""):
        out = "en estado desconocido"
    elif entity.domain == "climate":
        out = "en " + _MODES_ES.get(state, state)
    else:
        out = state
    temp = (obs.get("attributes") or {}).get("temperature")
    if with_temp and temp is not None:
        out += " a %s°" % _fmt_temp(temp)
    return out


class Executor:
    def __init__(self, ha: HAClient, cache: EntityCache, cfg: Config, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self.ha = ha
        self.cache = cache
        self.cfg = cfg
        self.sleep = sleep
        self.clock = clock

    def _expired(self, deadline: float) -> bool:
        return self.clock() >= deadline

    # -- API pública ----------------------------------------------------
    def execute(self, action: str, entity: Optional[Entity], hvac_mode: Optional[str] = None,
                temperature: Optional[int] = None) -> ExecResult:
        """Nunca levanta: cualquier problema se devuelve como ExecResult.

        Un único plazo de EXECUTE_DEADLINE_S (reloj de pared) cubre service calls y verificación.
        """
        try:
            deadline = self.clock() + EXECUTE_DEADLINE_S
            if action in _AGGREGATES:
                return self._aggregate(action, deadline)
            if action not in _VERBS:
                return ExecResult(False, ERROR, [], "No sé cómo hacer esa acción.", [])
            if entity is None:
                return ExecResult(False, ERROR, [], "No encontré el dispositivo para %s." % _VERBS[action], [])
            if action == "set_climate" and hvac_mode is None and temperature is None:
                action = "turn_on"
            if action == "set_climate" and entity.domain != "climate":
                return ExecResult(False, ERROR, [entity.entity_id],
                                  "%s no es un aire, no lo puedo configurar." % entity.friendly_name,
                                  [entity.entity_id])
            return self._single(action, entity, hvac_mode, temperature, deadline)
        except Exception as e:
            log.warning("laya-router: error inesperado en executor (%s)", type(e).__name__)
            target = entity.entity_id if entity is not None else None
            ids = [target] if target else []
            return ExecResult(False, ERROR, ids, "Tuve un problema inesperado al intentar esa acción.", ids)

    def verify(self, entity: Entity, expected_state: Optional[str], expected_temp: Optional[int]) -> str:
        deadline = self.clock() + EXECUTE_DEADLINE_S
        status, _ = self._verify_many([(entity, expected_state, expected_temp)], deadline)[entity.entity_id]
        return FAILED if status == _TIMEOUT else status

    # -- acciones -------------------------------------------------------
    def _single(self, action: str, entity: Entity, hvac_mode: Optional[str],
                temperature: Optional[int], deadline: float) -> ExecResult:
        eid = entity.entity_id
        verb = _VERBS[action]
        late = ExecResult(False, FAILED, [eid], "Intenté %s %s pero no pude confirmar a tiempo."
                          % (verb, entity.friendly_name), [eid])
        try:
            if action == "set_climate":
                if hvac_mode is not None:
                    self.ha.call_service("climate", "set_hvac_mode", {"entity_id": eid, "hvac_mode": hvac_mode})
                if temperature is not None:
                    if hvac_mode is not None and self._expired(deadline):
                        return late
                    self.ha.call_service("climate", "set_temperature", {"entity_id": eid, "temperature": temperature})
                expected_state, expected_temp = hvac_mode, temperature
            else:
                self.ha.call_service(entity.domain, action, {"entity_id": eid})
                expected_state, expected_temp = ("on" if action == "turn_on" else "off"), None
        except HAError as e:
            log.warning("laya-router: service call falló para %s (%s)", eid, type(e).__name__)
            return ExecResult(False, ERROR, [eid], "No pude hablar con Home Assistant para %s %s."
                              % (verb, entity.friendly_name), [eid])

        status, obs = self._verify_many([(entity, expected_state, expected_temp)], deadline)[eid]
        if status == _TIMEOUT:
            return late
        name = entity.friendly_name
        if status == CONFIRMED:
            if action == "set_climate":
                parts = []
                if hvac_mode is not None:
                    parts.append("en " + _MODES_ES.get(hvac_mode, hvac_mode))
                if temperature is not None:
                    parts.append("a %s°" % _fmt_temp(temperature))
                done = " ".join(parts)
            else:
                done = _on_off_word(entity, action == "turn_on")
            return ExecResult(True, CONFIRMED, [eid], "Listo, %s %s." % (name, done), [])
        if status == SENT:
            return ExecResult(True, SENT, [eid], "Le mandé %s a %s, pero no puedo confirmar que haya respondido."
                              % (verb, name), [])
        if obs is None:
            msg = "Intenté %s %s pero no pude confirmar su estado." % (verb, name)
        else:
            msg = "Intenté %s %s pero sigue %s." % (verb, name, _describe_state(entity, obs, expected_temp is not None))
        return ExecResult(False, FAILED, [eid], msg, [eid])

    def _aggregate(self, action: str, deadline: float) -> ExecResult:
        """Un turn_off por entidad (para atribuir fallas por entidad); luego se verifican juntas.

        Ante el primer error de red (HA no responde) se deja de llamar: el resto queda sin intentar.
        Vencido el plazo, tampoco se hacen más llamadas.
        """
        domains, what = _AGGREGATES[action]
        excluded = set(self.cfg.exclude_from_all_off or [])
        targets = [e for e in self.cache.by_domain(domains if domains is not None else self.cfg.domains)
                   if e.entity_id not in excluded]
        ids = [e.entity_id for e in targets]
        if not targets:
            return ExecResult(False, FAILED, [], "No encontré dispositivos para apagar.", [])

        errored = set()      # el service call devolvió un error HTTP
        unreached = set()    # HA no respondió (error de red) o se cortó tras ese error
        late = set()         # no se llamó porque venció el plazo
        for i, e in enumerate(targets):
            if self._expired(deadline):
                late.update(t.entity_id for t in targets[i:])
                break
            try:
                self.ha.call_service(e.domain, "turn_off", {"entity_id": e.entity_id})
            except HAUnreachable as exc:
                log.warning("laya-router: HA no respondió al apagar %s (%s); se cancela el resto",
                            e.entity_id, type(exc).__name__)
                unreached.update(t.entity_id for t in targets[i:])
                break
            except HAError as exc:
                log.warning("laya-router: turn_off falló para %s (%s)", e.entity_id, type(exc).__name__)
                errored.add(e.entity_id)
        not_called = errored | unreached | late
        if len(not_called) == len(targets):
            if unreached:
                msg = "Home Assistant no respondió, no pude %s." % what
            else:
                msg = "No pude hablar con Home Assistant para %s." % what
            return ExecResult(False, ERROR, ids, msg, list(ids))

        results = self._verify_many([(e, "off", None) for e in targets if e.entity_id not in not_called],
                                    deadline, stop_on_unresponsive=True)

        def pick(status):
            return [e for e in targets if e.entity_id not in not_called and results[e.entity_id][0] == status]

        # wrong: observado en el estado incorrecto; unresponsive: unavailable/unknown o nunca se pudo
        # leer (no cuenta como falla); timed_out: se venció el plazo antes de confirmar (tampoco).
        wrong = pick(FAILED)
        unresponsive = pick(_UNRESPONSIVE)
        timed_out = pick(_TIMEOUT)
        sent = pick(SENT)
        confirmed = pick(CONFIRMED)
        bad_call = [e for e in targets if e.entity_id in errored]
        no_answer = [e for e in targets if e.entity_id in unreached]
        too_late = [e for e in targets if e.entity_id in late]
        hard_ids = {e.entity_id for e in wrong} | not_called
        failed_ids = [e.entity_id for e in targets if e.entity_id in hard_ids]

        def names(es):
            return ", ".join(e.friendly_name for e in es)

        hard = []
        if wrong:
            hard.append("quedaron encendidos: " + names(wrong))
        if bad_call:
            hard.append("no pude apagar: " + names(bad_call))
        if no_answer:
            hard.append("Home Assistant no respondió, no pude apagar: " + names(no_answer))
        if too_late:
            hard.append("no llegué a tiempo a apagar: " + names(too_late))

        if not confirmed and not sent:
            # Ninguna entidad confirmada ni enviada: no se puede decir que se apagó algo.
            clauses = list(hard)
            if unresponsive:
                clauses.append("no responden: " + names(unresponsive))
            if timed_out:
                clauses.append("no pude confirmar a tiempo: " + names(timed_out))
            msg = "No pude confirmar que se apagara nada: %s." % "; ".join(clauses)
            return ExecResult(False, FAILED, ids, msg, failed_ids)

        # "Apagué N" cuenta solo lo confirmado; lo enviado sin confirmar (assumed_state) va aparte.
        sentences = []
        n = len(confirmed)
        if n and hard:
            sentences.append("Apagué %d, pero %s." % (n, "; ".join(hard)))
        elif n:
            sentences.append("Listo, apagué %d %s." % (n, "dispositivo" if n == 1 else "dispositivos"))
        elif hard:
            text = "; ".join(hard)
            sentences.append(text[:1].upper() + text[1:] + ".")
        if sent:
            sentences.append("A %s %s mandé la orden sin poder confirmar."
                             % (names(sent), "le" if len(sent) == 1 else "les"))
        if unresponsive:
            sentences.append("No responden: %s." % names(unresponsive))
        if timed_out:
            sentences.append("No pude confirmar a tiempo: %s." % names(timed_out))
        msg = " ".join(sentences)
        if hard:
            return ExecResult(False, FAILED, ids, msg, failed_ids)
        all_confirmed = len(confirmed) == len(targets)
        return ExecResult(True, CONFIRMED if all_confirmed else SENT, ids, msg, [])

    # -- verificación ---------------------------------------------------
    @staticmethod
    def _matches(entity: Entity, obs: dict, expected_state: Optional[str], expected_temp) -> bool:
        state = str(obs.get("state", ""))
        if expected_state is not None:
            if entity.domain == "climate" and expected_state == "on":
                # Muchos climate reportan el modo (p.ej. "cool") en vez de "on" cuando están prendidos.
                if state in _CLIMATE_NOT_ON:
                    return False
            elif state != expected_state:
                return False
        if expected_temp is not None:
            if not _temp_equal((obs.get("attributes") or {}).get("temperature"), expected_temp):
                return False
        return True

    def _verify_many(self, checks: List[Tuple[Entity, Optional[str], Optional[int]]], deadline: float,
                     stop_on_unresponsive: bool = False) -> Dict[str, Tuple[str, Optional[dict]]]:
        """Devuelve entity_id -> (status, último estado observado o None).

        Con stop_on_unresponsive (agregados), una entidad leída como unavailable/unknown deja
        de sondearse y queda como _UNRESPONSIVE; también la que nunca se pudo leer.

        Los reintentos se cuentan como suma de los sleeps pedidos: hasta
        verify_budget_s / verify_poll_ms reintentos tras el primer GET inmediato. Aparte, el
        plazo de pared `deadline` (self.clock) corta todo: lo pendiente queda como _TIMEOUT.
        """
        results: Dict[str, Tuple[str, Optional[dict]]] = {}
        pending = []
        for entity, exp_state, exp_temp in checks:
            if entity.assumed_state:
                results[entity.entity_id] = (SENT, None)
            else:
                pending.append((entity, exp_state, exp_temp))
        last: Dict[str, Optional[dict]] = {e.entity_id: None for e, _, _ in pending}

        poll_ms = int(self.cfg.verify_poll_ms)
        max_sleeps = int(float(self.cfg.verify_budget_s) * 1000 // poll_ms) if poll_ms > 0 else 0
        attempt = 0
        timed_out = False
        while pending and not timed_out:
            if attempt > 0:
                if attempt > max_sleeps:
                    break
                if self._expired(deadline):
                    timed_out = True
                    break
                self.sleep(poll_ms / 1000.0)
            attempt += 1
            still = []
            for idx, (entity, exp_state, exp_temp) in enumerate(pending):
                if self._expired(deadline):
                    still.extend(pending[idx:])
                    timed_out = True
                    break
                try:
                    obs = self.ha.get_state(entity.entity_id)
                except Exception as e:
                    log.warning("laya-router: GET de estado falló para %s (%s)", entity.entity_id, type(e).__name__)
                    obs = None
                if isinstance(obs, dict):
                    last[entity.entity_id] = obs
                    if self._matches(entity, obs, exp_state, exp_temp):
                        results[entity.entity_id] = (CONFIRMED, obs)
                        continue
                    if stop_on_unresponsive and str(obs.get("state", "")) in _UNRESPONSIVE_STATES:
                        results[entity.entity_id] = (_UNRESPONSIVE, obs)
                        continue
                still.append((entity, exp_state, exp_temp))
            pending = still
        for entity, _, _ in pending:
            obs = last[entity.entity_id]
            if timed_out:
                results[entity.entity_id] = (_TIMEOUT, obs)
            elif stop_on_unresponsive and obs is None:
                results[entity.entity_id] = (_UNRESPONSIVE, None)
            else:
                results[entity.entity_id] = (FAILED, obs)
        return results
