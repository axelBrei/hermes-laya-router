"""Cache de entidades de Home Assistant y selección de candidatas por texto."""
import json
import logging
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .textnorm import lexicon_match, tokens

log = logging.getLogger(__name__)

_MIN_BACKOFF_S = 60

# (léxico, dominios sugeridos)
_DOMAIN_HINTS = (
    (["luz", "luces", "lampara"], ["light", "switch"]),
    (["aire", "clima", "calef", "temperatura"], ["climate"]),
)


@dataclass
class Entity:
    entity_id: str
    domain: str
    friendly_name: str
    area: Optional[str]
    state: str
    aliases: List[str] = field(default_factory=list)
    assumed_state: bool = False

    def description(self) -> str:
        out = self.friendly_name
        if self.area:
            out += ", área " + self.area
        if self.aliases:
            out += ", alias: " + ", ".join(self.aliases)
        return out

    def _tokens(self) -> set:
        return set(tokens(" ".join([self.friendly_name, self.area or ""] + list(self.aliases))))


class EntityCache:
    def __init__(self, path: Path, ha, domains: List[str], aliases_cfg: Dict[str, List[str]], refresh_minutes: int,
                 exclude_from_all_off: Optional[List[str]] = None):
        self.path = Path(path) if path is not None else None
        self.exclude_from_all_off = list(exclude_from_all_off or [])
        self.ha = ha
        self.domains = list(domains)
        self.aliases_cfg = aliases_cfg or {}
        self.refresh_minutes = refresh_minutes
        self.last_refresh: Optional[datetime] = None
        # Se reemplaza entero (nunca se muta) para que los lectores vean un snapshot consistente.
        self._entities: Dict[str, Entity] = {}
        self._write_lock = threading.Lock()

    # -- persistencia ---------------------------------------------------
    def load(self) -> int:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            entities = {}
            for item in raw:
                e = Entity(**item)
                entities[e.entity_id] = e
        except FileNotFoundError:
            return 0
        except (OSError, ValueError, TypeError, AttributeError):
            log.warning("laya-router: archivo de entidades corrupto o ilegible, se ignora")
            return 0
        self._entities = entities
        return len(entities)

    def _write(self, entities: List[Entity]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=self.path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump([asdict(e) for e in entities], f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def refresh(self) -> int:
        with self._write_lock:
            states = self.ha.get_states()  # las excepciones de HA se propagan sin tocar el cache
            try:
                areas = self.ha.areas(self.domains)
            except Exception as e:
                log.warning("laya-router: no se pudieron obtener las áreas (%s)", type(e).__name__)
                areas = {}
            entities: Dict[str, Entity] = {}
            for s in states:
                entity_id = s.get("entity_id", "")
                domain = entity_id.split(".", 1)[0]
                if domain not in self.domains:
                    continue
                attrs = s.get("attributes") or {}
                entities[entity_id] = Entity(
                    entity_id=entity_id,
                    domain=domain,
                    friendly_name=attrs.get("friendly_name") or entity_id,
                    area=areas.get(entity_id) or None,
                    state=str(s.get("state", "")),
                    aliases=list(self.aliases_cfg.get(entity_id, [])),
                    assumed_state=bool(attrs.get("assumed_state", False)),
                )
            self._write(list(entities.values()))
            self._entities = entities
            self.last_refresh = datetime.now()
            self._warn_missing_exclusions(entities)
            return len(entities)

    def _warn_missing_exclusions(self, entities: Dict[str, Entity]) -> None:
        """Un typo en exclude_from_all_off no debe pasar en silencio (all_off apagaría la heladera)."""
        missing = [eid for eid in self.exclude_from_all_off if eid not in entities]
        if missing:
            log.warning("laya-router: exclude_from_all_off tiene entity_ids que no están en Home Assistant "
                        "(¿typo?): %s", ", ".join(missing))

    # -- lectura --------------------------------------------------------
    def get(self, entity_id: str) -> Optional[Entity]:
        return self._entities.get(entity_id)

    def all(self) -> List[Entity]:
        return list(self._entities.values())

    def by_domain(self, domains: List[str]) -> List[Entity]:
        return [e for e in self._entities.values() if e.domain in domains]

    @staticmethod
    def _sorted(entities: List[Entity], max_n: int) -> List[Entity]:
        return sorted(entities, key=lambda e: e.friendly_name.lower())[:max_n]

    def candidates(self, text: str, max_n: int) -> List[Entity]:
        return self.candidates_info(text, max_n)[0]

    @staticmethod
    def _score(entities: List[Entity], text: str) -> List[Tuple[int, Entity]]:
        text_tokens = set(tokens(text))
        return [(len(text_tokens & e._tokens()), e) for e in entities]

    def candidates_info(self, text: str, max_n: int, history_text: str = "") -> Tuple[List[Entity], bool]:
        """Candidatas para el texto y si la lista es exacta.

        history_text (turno previo del usuario) solo se usa cuando el mensaje actual no puntúa
        ninguna entidad: nunca puede desplazar a un dispositivo nombrado en el mensaje actual.

        exact es True solo si al menos una entidad puntuó por tokens y no hubo corte por max_n;
        es False si se cayó al filtro por dominio, a todas las entidades o si se truncó.
        """
        entities = self.all()
        scored = self._score(entities, text)
        best = max((s for s, _ in scored), default=0)
        if best == 0 and history_text:
            scored = self._score(entities, history_text + " " + text)
            best = max((s for s, _ in scored), default=0)
        if best > 0:
            matched = [e for s, e in scored if s == best]
            return self._sorted(matched, max_n), len(matched) <= max_n
        wanted: List[str] = []
        for lexicon, doms in _DOMAIN_HINTS:
            if lexicon_match(text, lexicon):
                wanted.extend(d for d in doms if d not in wanted)
        if wanted:
            hinted = [e for e in entities if e.domain in wanted]
            if hinted:
                return self._sorted(hinted, max_n), False
        return self._sorted(entities, max_n), False

    # -- refresco en background ----------------------------------------
    def _loop(self, stop) -> None:
        interval = max(self.refresh_minutes, 1) * 60
        backoff = min(_MIN_BACKOFF_S, interval)
        wait_s = 0 if not self._entities else interval
        while not stop.is_set():
            if wait_s and stop.wait(wait_s):
                break
            try:
                self.refresh()
                backoff = min(_MIN_BACKOFF_S, interval)
                wait_s = interval
            except Exception as e:
                log.warning("laya-router: refresh de entidades falló (%s: %s)", type(e).__name__, e)
                wait_s = backoff
                backoff = min(backoff * 2, interval)

    def start_background(self, stop: threading.Event) -> threading.Thread:
        t = threading.Thread(target=self._loop, args=(stop,), name="laya-router-entities", daemon=True)
        t.start()
        return t
