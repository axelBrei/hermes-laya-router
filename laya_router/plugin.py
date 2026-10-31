"""Plugin de Hermes: hooks, tool home_action, comando /laya y sección de system prompt."""
import json
import logging
import math
import os
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, List, Mapping, Optional

from .config import CONFIG_SUBDIR, Config, Thresholds, load_config, missing_env, resolve_hermes_home
from .entities import EntityCache
from .executor import CONFIRMED, ERROR, FAILED, SENT, Executor
from .ha_client import HAClient
from .http import Transport, http_json
from .laya_client import LayaClient, LayaError
from .router import ACTIONS, AGGREGATES, build_input, build_questions, decide, prefilter
from .textnorm import extract_text

log = logging.getLogger(__name__)

PENDING_TTL_S = 120.0
DECISION_LOG_SIZE = 20
STATUS_DECISIONS = 5
MSG_LOG_CHARS = 80
FORCED_REFRESH_MIN_S = 60.0  # home_action con entity_id desconocido: como mucho un refresh por minuto
HISTORY_SCORE_CHARS = 300  # tope del mensaje previo del usuario que se suma al puntaje de candidatos
HVAC_MODES = ("cool", "heat", "fan_only", "auto")
TOOL_ACTIONS = tuple(a for a in ACTIONS if a != "none")
ENTITIES_FILENAME = "entities.json"

_ENTITY_ID_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")

_ACTION_ES = {
    "turn_on": "encender",
    "turn_off": "apagar",
    "all_off": "apagar todo",
    "lights_off": "apagar las luces",
    "climate_off": "apagar los aires",
    "set_climate": "configurar el aire",
}

TOOL_SCHEMA = {
    "name": "home_action",
    "description": (
        "Ejecuta una acción sobre luces, enchufes o aires acondicionados de la casa y verifica el resultado. "
        "Usalo cuando el mensaje traiga una intención [laya-router] o el usuario pida una acción simple de casa."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string",
                       "enum": ["turn_on", "turn_off", "all_off", "lights_off", "climate_off", "set_climate"]},
            "entity_id": {"type": "string",
                          "description": "entity_id de Home Assistant. Omitir para all_off, lights_off y climate_off."},
            "hvac_mode": {"type": "string", "enum": ["cool", "heat", "fan_only", "auto"]},
            "temperature": {"type": "integer", "minimum": 16, "maximum": 30},
        },
        "required": ["action"],
    },
}

SYSTEM_PROMPT_SECTION = (
    "Un plugin (laya-router) clasifica los pedidos de casa antes de que vos los veas. "
    "Si el mensaje del usuario termina con un bloque [laya-router]:\n"
    "- \"Acción ya ejecutada\": respondé con una sola frase corta confirmando lo que dice el bloque. "
    "No llames a ningún tool, no leas skills, no expliques.\n"
    "- Un bloque que termina en \"Informale al usuario. No digas que se hizo.\" (por ejemplo \"Intenté ... pero ...\"): "
    "informá el problema en una frase. No digas que se hizo.\n"
    "- \"Intención probable\": si es inequívoco, llamá a home_action con esos parámetros "
    "(entre corchetes van action y entity_id). Si hay dudas, hacé una pregunta corta.\n"
    "Los bloques [laya-router] de turnos anteriores son notas del sistema, no texto del usuario.\n"
    "Para acciones simples de luces, enchufes y aire acondicionado usá siempre home_action, "
    "no la API de Home Assistant directamente."
)


# -- saneamiento de configuración --------------------------------------------------

def _is_num(v) -> bool:
    return not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v)


def _as_int(v) -> Optional[int]:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and math.isfinite(v) and v.is_integer():
        return int(v)
    return None


def _str_list(v) -> Optional[List[str]]:
    if isinstance(v, (str, int)) and not isinstance(v, bool):
        v = [v]
    if not isinstance(v, list):
        return None
    out = []
    for item in v:
        if isinstance(item, bool) or not isinstance(item, (str, int)):
            continue
        s = str(item).strip()
        if s:
            out.append(s)
    return out


def _allowed_senders(v) -> Optional[List[str]]:
    """Lista estricta de remitentes; None si el valor es inválido (tipo o algún ítem)."""
    if not isinstance(v, list):
        return None
    out = []
    for item in v:
        if isinstance(item, bool) or not isinstance(item, (str, int)) or not str(item).strip():
            return None
        out.append(str(item).strip())
    return out


def allowlist_invalid(cfg) -> bool:
    return _allowed_senders(getattr(cfg, "allowed_senders", None)) is None


def _norm_platform(platform) -> str:
    if hasattr(platform, "value"):
        return str(platform.value)
    return str(platform or "")


def sanitize_config(cfg: Config) -> Config:
    """Devuelve un Config con tipos válidos; los valores inválidos vuelven al default con warning."""
    data_dir = getattr(cfg, "data_dir", None)
    if not isinstance(data_dir, Path):
        data_dir = resolve_hermes_home() / CONFIG_SUBDIR
    d = Config(data_dir=data_dir)
    out = Config(data_dir=data_dir)

    def bad(name):
        log.warning("laya-router: valor inválido para '%s' en config.yaml; se usa el default", name)

    for name in ("bifrost_url_env", "bifrost_model", "bifrost_api_key_env"):
        v = getattr(cfg, name, None)
        if isinstance(v, str) and v.strip():
            setattr(out, name, v.strip())
        else:
            bad(name)

    # (nombre, admite 0)
    for name, zero_ok in (("laya_timeout_s", False), ("ha_timeout_s", False), ("verify_budget_s", True)):
        v = getattr(cfg, name, None)
        if _is_num(v) and (v >= 0.0 if zero_ok else v > 0.0):
            setattr(out, name, float(v))
        else:
            bad(name)

    th = getattr(cfg, "thresholds", None)
    out.thresholds = Thresholds()
    for name in ("is_home", "execute"):
        v = getattr(th, name, None)
        if _is_num(v) and 0.0 <= v <= 1.0:
            setattr(out.thresholds, name, float(v))
        else:
            bad("thresholds." + name)

    out.load_error = bool(getattr(cfg, "load_error", False))
    allowed = _allowed_senders(getattr(cfg, "allowed_senders", None))
    out.allowed_senders = allowed if allowed is not None else []

    for name, allow_empty in (("domains", False), ("exclude_from_all_off", True), ("prefilter_lexicon", False)):
        v = _str_list(getattr(cfg, name, None))
        if v is None or (not v and not allow_empty):
            bad(name)
            setattr(out, name, list(getattr(d, name)))
        else:
            setattr(out, name, v)

    aliases = getattr(cfg, "aliases", None)
    if isinstance(aliases, dict):
        out.aliases = {}
        for k, v in aliases.items():
            vs = _str_list(v)
            if isinstance(k, str) and vs:
                out.aliases[k] = vs
    else:
        bad("aliases")

    for name, minimum in (("cache_refresh_minutes", 1), ("history_turns", 0), ("max_message_chars", 1),
                          ("verify_poll_ms", 1), ("max_target_choices", 1)):
        v = _as_int(getattr(cfg, name, None))
        if v is not None and v >= minimum:
            setattr(out, name, v)
        else:
            bad(name)

    dry_run = getattr(cfg, "dry_run", None)
    if isinstance(dry_run, bool):
        out.dry_run = dry_run
    else:
        log.warning("laya-router: 'dry_run' inválido en config.yaml; se activa dry_run por seguridad")
        out.dry_run = True
    return out


def _last_user_text(history, current: str) -> str:
    """Último mensaje de usuario (str) del historial distinto del actual, recortado; "" si no hay."""
    if not isinstance(history, list):
        return ""
    for m in reversed(history):
        if isinstance(m, dict) and m.get("role") == "user" and isinstance(m.get("content"), str):
            content = m["content"]
            if content == current:
                continue
            return content[:HISTORY_SCORE_CHARS]
    return ""


def _fmt_conf(confs: dict, key: str) -> str:
    v = confs.get(key, 0.0) if isinstance(confs, dict) else 0.0
    return "%.2f" % v if _is_num(v) else "0.00"


# -- plugin ----------------------------------------------------------------------------

class LayaRouterPlugin:
    def __init__(self, cfg: Config, env: Mapping[str, str], transport: Transport = http_json,
                 clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self._lock = threading.Lock()
        self.pending = {}  # session_id -> (texto, timestamp de clock)
        self.decisions = deque(maxlen=DECISION_LOG_SIZE)
        self.last_seen: Optional[str] = None
        self.enabled = False
        self.disabled_reason = ""
        self.ha = self.laya = self.cache = self.executor = None
        self.stop_event: Optional[threading.Event] = None
        self._init_refresh_thread: Optional[threading.Thread] = None
        self.fast_locked = False
        self._last_forced_refresh: Optional[float] = None

        try:
            self.cfg = sanitize_config(cfg)
        except Exception as e:
            log.warning("laya-router: configuración inválida (%s); plugin deshabilitado", type(e).__name__)
            self.cfg = Config(data_dir=resolve_hermes_home() / CONFIG_SUBDIR)
            self.disabled_reason = "configuración inválida"
            return

        # Allowlist inválida o config.yaml ilegible: solo cli puede ejecutar directo (fail closed).
        try:
            if allowlist_invalid(cfg):
                log.warning("laya-router: 'allowed_senders' inválido en config.yaml; "
                            "ejecución directa deshabilitada salvo cli (solo pista)")
                self.fast_locked = True
            elif self.cfg.load_error:
                log.warning("laya-router: config.yaml ilegible; ejecución directa deshabilitada salvo cli (solo pista)")
                self.fast_locked = True
        except Exception:
            self.fast_locked = True

        try:
            missing = missing_env(self.cfg, env)
        except Exception as e:
            log.warning("laya-router: no se pudo leer el entorno (%s); plugin deshabilitado", type(e).__name__)
            self.disabled_reason = "no se pudo leer el entorno"
            return
        if missing:
            # Solo nombres de variables, nunca valores.
            log.warning("laya-router: deshabilitado, faltan variables de entorno: %s", ", ".join(missing))
            self.disabled_reason = "faltan variables de entorno: " + ", ".join(missing)
            return

        try:
            cfg = self.cfg
            self.ha = HAClient(env["HASS_URL"], env["HASS_TOKEN"], cfg.ha_timeout_s, transport)
            self.laya = LayaClient(env[cfg.bifrost_url_env], cfg.bifrost_model, env[cfg.bifrost_api_key_env],
                                   cfg.laya_timeout_s, transport)
            self.cache = EntityCache(cfg.data_dir / ENTITIES_FILENAME, self.ha, cfg.domains, cfg.aliases,
                                     cfg.cache_refresh_minutes, exclude_from_all_off=cfg.exclude_from_all_off)
            self.executor = Executor(self.ha, self.cache, cfg)
            loaded = self.cache.load()
        except Exception as e:
            log.warning("laya-router: error al inicializar (%s); plugin deshabilitado", type(e).__name__)
            self.ha = self.laya = self.cache = self.executor = None
            self.disabled_reason = "error al inicializar (%s)" % type(e).__name__
            return

        self.enabled = True
        if loaded == 0:
            t = threading.Thread(target=self._initial_refresh, name="laya-router-initial-refresh", daemon=True)
            self._init_refresh_thread = t
            t.start()

    def _initial_refresh(self) -> None:
        try:
            n = self.cache.refresh()
            log.info("laya-router: cache de entidades inicial: %d", n)
        except Exception as e:
            log.warning("laya-router: refresh inicial de entidades falló (%s)", type(e).__name__)

    # -- allowlist ---------------------------------------------------------------
    def fast_allowed(self, platform: str, sender_id: str) -> bool:
        platform = _norm_platform(platform)
        sender_id = "" if sender_id is None else str(sender_id)
        if platform == "cli":
            return True
        if self.fast_locked:
            return False
        allowed = self.cfg.allowed_senders
        return (not allowed or "%s:%s" % (platform, sender_id) in allowed
                or sender_id in allowed)

    # -- pending -------------------------------------------------------------------
    def _prune_locked(self, now: float) -> None:
        for sid in [s for s, (_, ts) in self.pending.items() if now - ts >= PENDING_TTL_S]:
            del self.pending[sid]

    def _set_pending(self, session_id: str, text: str) -> None:
        with self._lock:
            now = self.clock()
            self._prune_locked(now)
            self.pending[session_id] = (text, now)

    # -- textos ---------------------------------------------------------------------
    @staticmethod
    def _hint_text(action: str, target, hvac_mode, temperature, confs: dict) -> str:
        legible = "%s [%s]" % (_ACTION_ES.get(action, action), action)
        if action == "set_climate":
            extras = []
            if hvac_mode:
                extras.append("modo " + hvac_mode)
            if temperature is not None:
                extras.append("%s°" % temperature)
            if extras:
                legible += " (" + ", ".join(extras) + ")"
        where = "%s [%s]" % (target.friendly_name, target.entity_id) if target is not None else "sin entidad clara"
        return ("[laya-router] Intención probable: %s sobre %s (confianza acción %s, entidad %s). "
                "Si es inequívoco llamá al tool home_action con esos parámetros; si hay dudas, hacé una pregunta corta."
                % (legible, where, _fmt_conf(confs, "action"), _fmt_conf(confs, "target")))

    @staticmethod
    def _done_text(message: str) -> str:
        return ("[laya-router] Acción ya ejecutada: %s. Respondé con una sola frase confirmando exactamente eso. "
                "No llames a ningún tool." % message.rstrip().rstrip("."))

    @staticmethod
    def _failed_text(message: str) -> str:
        return "[laya-router] %s Informale al usuario. No digas que se hizo." % message.strip()

    # -- log de decisiones --------------------------------------------------------------
    def _log_decision(self, text: str, action, target, confs, path: str, reason: str,
                      laya_ms: Optional[float], exec_ms: Optional[float], status: str = "-") -> None:
        line = ("laya-router: msg=%r action=%s target=%s conf(action=%s target=%s is_home=%s) path=%s "
                "status=%s reason=%s laya_ms=%s exec_ms=%s" % (
                    text[:MSG_LOG_CHARS], action or "-", target or "-",
                    _fmt_conf(confs, "action"), _fmt_conf(confs, "target"), _fmt_conf(confs, "is_home"),
                    path, status, reason or "-",
                    "%d" % laya_ms if laya_ms is not None else "-",
                    "%d" % exec_ms if exec_ms is not None else "-"))
        log.info("%s", line)
        self.decisions.append(line)

    # -- hooks ------------------------------------------------------------------------------
    def on_pre_llm_call(self, session_id: str = "", user_message=None, conversation_history=None,
                        platform: str = "", sender_id: str = "", **kw):
        try:
            platform = _norm_platform(platform)
            sender_id = "" if sender_id is None else str(sender_id)
            # Un turno nuevo invalida cualquier resultado pendiente del turno anterior.
            with self._lock:
                self.pending.pop(session_id, None)
            self.last_seen = "%s:%s" % (platform, sender_id)
            if not self.enabled:
                return None
            return self._run_turn(session_id, user_message, conversation_history, platform, sender_id)
        except Exception as e:
            log.warning("laya-router: error en pre_llm_call (%s)", type(e).__name__)
            return None

    def _run_turn(self, session_id, user_message, conversation_history, platform: str, sender_id: str):
        cfg = self.cfg
        text = extract_text(user_message)
        if not prefilter(text, cfg):
            log.debug("laya-router: prefiltro descarta el turno")
            return None

        history = conversation_history if isinstance(conversation_history, list) else []
        # El turno anterior del usuario ayuda con "apagala", pero solo si el mensaje actual no nombra nada.
        candidates, targets_exact = self.cache.candidates_info(
            text, cfg.max_target_choices, _last_user_text(history, text))
        questions = build_questions(candidates)
        t0 = self.clock()
        try:
            answers = self.laya.decide(build_input(text, history, cfg.history_turns), questions)
        except Exception as e:
            laya_ms = (self.clock() - t0) * 1000
            detail = str(e) if isinstance(e, LayaError) else type(e).__name__
            log.warning("laya-router: Laya falló (%s)", detail)
            self._log_decision(text, None, None, {}, "laya_error", detail, laya_ms, None)
            return None
        laya_ms = (self.clock() - t0) * 1000

        d = decide(text, answers, candidates, cfg, self.fast_allowed(platform, sender_id),
                   targets_exact=targets_exact)
        target_id = d.target.entity_id if d.target is not None else None
        if d.path == "skip":
            self._log_decision(text, d.action, target_id, d.confidences, "skip", d.reason, laya_ms, None)
            return None

        path = d.path
        if path == "execute" and cfg.dry_run:
            log.info("laya-router: [dry-run] would execute %s on %s (hvac_mode=%s, temperature=%s)",
                     d.action, target_id or "-", d.hvac_mode, d.temperature)
            path = "dry_run"

        if path == "execute":
            t1 = self.clock()
            res = self.executor.execute(d.action, d.target, d.hvac_mode, d.temperature)
            exec_ms = (self.clock() - t1) * 1000
            if res.status in (CONFIRMED, SENT):
                context = self._done_text(res.message)
            elif res.status == FAILED:
                context = self._failed_text(res.message)
            else:
                context = (self._hint_text(d.action, d.target, d.hvac_mode, d.temperature, d.confidences)
                           + " Home Assistant no respondió al intento directo.")
            if res.status != ERROR and isinstance(session_id, str) and session_id:
                self._set_pending(session_id, res.message)
            self._log_decision(text, d.action, target_id, d.confidences, "execute", d.reason, laya_ms, exec_ms,
                               res.status)
            return {"context": context}

        self._log_decision(text, d.action, target_id, d.confidences, path, d.reason, laya_ms, None)
        return {"context": self._hint_text(d.action, d.target, d.hvac_mode, d.temperature, d.confidences)}

    def on_transform_llm_output(self, response_text: str = "", session_id: str = "", platform: str = "", **kw):
        try:
            with self._lock:
                now = self.clock()
                self._prune_locked(now)
                entry = self.pending.pop(session_id, None)
            if _norm_platform(platform) == "cli" or entry is None:
                return None
            text, ts = entry
            if now - ts >= PENDING_TTL_S:
                return None
            return text
        except Exception as e:
            log.warning("laya-router: error en transform_llm_output (%s)", type(e).__name__)
            return None

    # -- tool --------------------------------------------------------------------------------
    def handle_home_action(self, params: dict, **kw) -> str:
        try:
            return json.dumps(self._home_action(params), ensure_ascii=False)
        except Exception as e:
            log.warning("laya-router: error en home_action (%s)", type(e).__name__)
            return json.dumps({"ok": False, "error": "Error interno de laya-router."}, ensure_ascii=False)

    def _home_action(self, params) -> dict:
        def err(msg):
            return {"ok": False, "error": msg}

        if not self.enabled:
            return err("laya-router está deshabilitado: %s." % (self.disabled_reason or "configuración incompleta"))
        if not isinstance(params, dict):
            return err("Parámetros inválidos.")
        action = params.get("action")
        if not isinstance(action, str) or action not in TOOL_ACTIONS:
            return err("action inválida; usá una de: %s." % ", ".join(TOOL_ACTIONS))

        hvac_mode = params.get("hvac_mode")
        if hvac_mode is not None and (not isinstance(hvac_mode, str) or hvac_mode not in HVAC_MODES):
            return err("hvac_mode inválido; usá una de: %s." % ", ".join(HVAC_MODES))
        temperature = params.get("temperature")
        if temperature is not None:
            temperature = _as_int(temperature)
            if temperature is None or not 16 <= temperature <= 30:
                return err("temperature debe ser un entero entre 16 y 30.")

        entity = None
        if action not in AGGREGATES:
            eid = params.get("entity_id")
            if not isinstance(eid, str) or not _ENTITY_ID_RE.match(eid):
                return err("entity_id requerido y válido para %s." % action)
            if eid.split(".", 1)[0] not in self.cfg.domains:
                return err("El dominio de %s no está habilitado en laya-router." % eid)
            entity = self.cache.get(eid)
            if entity is None and self._take_forced_refresh():
                try:
                    self.cache.refresh()
                except Exception as e:
                    log.warning("laya-router: refresh desde home_action falló (%s)", type(e).__name__)
                    return err("No pude actualizar la lista de dispositivos de Home Assistant.")
                entity = self.cache.get(eid)
            if entity is None or entity.domain not in self.cfg.domains:
                return err("No conozco el dispositivo %s." % eid)

        target_id = entity.entity_id if entity is not None else None
        if self.cfg.dry_run:
            log.info("laya-router: [dry-run] would execute home_action %s on %s (hvac_mode=%s, temperature=%s)",
                     action, target_id or "-", hvac_mode, temperature)
            return {"ok": False, "dry_run": True, "status": "dry_run",
                    "entity_ids": [target_id] if target_id else [],
                    "message": "Modo dry_run: no se ejecutó %s." % _ACTION_ES[action]}

        t0 = self.clock()
        res = self.executor.execute(action, entity, hvac_mode, temperature)
        exec_ms = (self.clock() - t0) * 1000
        log.info("laya-router: tool home_action action=%s target=%s status=%s exec_ms=%d",
                 action, target_id or "-", res.status, exec_ms)
        return {"ok": res.ok, "status": res.status, "entity_ids": list(res.entity_ids), "message": res.message}

    def _take_forced_refresh(self) -> bool:
        """True si se puede forzar un refresh ahora (y lo registra); a lo sumo uno por FORCED_REFRESH_MIN_S."""
        with self._lock:
            now = self.clock()
            last = self._last_forced_refresh
            if last is not None and now - last < FORCED_REFRESH_MIN_S:
                return False
            self._last_forced_refresh = now
            return True

    # -- comando /laya ------------------------------------------------------------------------
    def handle_command(self, args: str = "", **kw) -> str:
        try:
            words = (args if isinstance(args, str) else "").strip().lower().split()
            sub = words[0] if words else "status"
            if sub == "status":
                return self._status()
            if sub == "refresh":
                return self._refresh_command()
            return "Uso: /laya status | /laya refresh"
        except Exception as e:
            log.warning("laya-router: error en /laya (%s)", type(e).__name__)
            return "laya-router: error interno (%s)." % type(e).__name__

    def _refresh_command(self) -> str:
        if not self.enabled:
            return "laya-router deshabilitado: %s." % self.disabled_reason
        try:
            n = self.cache.refresh()
        except Exception as e:
            log.warning("laya-router: refresh manual falló (%s)", type(e).__name__)
            return "No se pudo refrescar la lista de entidades (%s)." % type(e).__name__
        return "Entidades: %d" % n

    def _status(self) -> str:
        cfg = self.cfg
        state = "habilitado" if self.enabled else "deshabilitado (%s)" % self.disabled_reason
        n = len(self.cache.all()) if self.cache is not None else 0
        last = getattr(self.cache, "last_refresh", None)
        lines = [
            "laya-router: " + state,
            "Umbrales: is_home %.2f, execute %.2f" % (cfg.thresholds.is_home, cfg.thresholds.execute),
            "Entidades: %d" % n,
            "Último refresh: %s" % (last.isoformat(sep=" ", timespec="seconds") if last else "nunca"),
            "dry_run: %s" % ("sí" if cfg.dry_run else "no"),
            "Allowlist: %s" % ("inválida o config ilegible: solo pista (salvo cli)" if self.fast_locked
                               else ", ".join(cfg.allowed_senders) or "vacía (todos)"),
            "Último remitente visto: %s" % (self.last_seen or "ninguno"),
            "Últimas decisiones:",
        ]
        recent = list(self.decisions)[-STATUS_DECISIONS:]
        if recent:
            lines.extend("- " + d for d in recent)
        else:
            lines.append("- (ninguna)")
        return "\n".join(lines)

    # -- system prompt ----------------------------------------------------------------------------
    def system_prompt_section(self) -> str:
        return SYSTEM_PROMPT_SECTION


def register(ctx) -> None:
    """Punto de entrada de Hermes. Nunca levanta."""
    try:
        try:
            cfg = load_config(resolve_hermes_home())
        except Exception as e:
            log.warning("laya-router: no se pudo cargar la configuración (%s); se usan defaults", type(e).__name__)
            cfg = Config(data_dir=resolve_hermes_home() / CONFIG_SUBDIR)
        plugin = LayaRouterPlugin(cfg, os.environ)
    except Exception as e:
        log.warning("laya-router: no se pudo crear el plugin (%s)", type(e).__name__)
        return None

    steps = (
        ("hook pre_llm_call", lambda: ctx.register_hook("pre_llm_call", plugin.on_pre_llm_call)),
        ("hook transform_llm_output",
         lambda: ctx.register_hook("transform_llm_output", plugin.on_transform_llm_output)),
        ("tool home_action", lambda: ctx.register_tool(name="home_action", toolset="laya_router",
                                                       schema=TOOL_SCHEMA, handler=plugin.handle_home_action)),
        ("comando /laya", lambda: ctx.register_command("laya", plugin.handle_command,
                                                       "laya-router: status | refresh")),
    )
    for what, fn in steps:
        try:
            fn()
        except Exception as e:
            log.warning("laya-router: no se pudo registrar %s (%s)", what, type(e).__name__)

    try:
        ctx.register_system_prompt_section("laya-router.protocol", plugin.system_prompt_section(),
                                           position="after_memory", max_chars=4000)
    except AttributeError:
        log.warning("laya-router: esta versión de Hermes no soporta secciones de system prompt; "
                    "se omite laya-router.protocol")
    except Exception as e:
        log.warning("laya-router: no se pudo registrar la sección de system prompt (%s)", type(e).__name__)

    if plugin.enabled:
        try:
            plugin.stop_event = threading.Event()
            plugin.cache.start_background(plugin.stop_event)
        except Exception as e:
            log.warning("laya-router: no se pudo iniciar el refresco en background (%s)", type(e).__name__)
    return None
