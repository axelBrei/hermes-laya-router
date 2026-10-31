"""Carga de configuración de laya-router."""
import copy
import logging
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List, Mapping

import yaml

logger = logging.getLogger(__name__)

DEFAULT_BIFROST_MODEL = "spanish"
DEFAULT_BIFROST_URL_ENV = "LAYA_BIFROST_URL"
DEFAULT_BIFROST_API_KEY_ENV = "LAYA_BIFROST_API_KEY"
DEFAULT_LAYA_TIMEOUT_S = 2.0
DEFAULT_HA_TIMEOUT_S = 5.0
DEFAULT_THRESHOLD_IS_HOME = 0.5
DEFAULT_THRESHOLD_EXECUTE = 0.85
DEFAULT_DOMAINS = ["light", "switch", "climate"]
DEFAULT_CACHE_REFRESH_MINUTES = 15
DEFAULT_HISTORY_TURNS = 2
DEFAULT_PREFILTER_LEXICON = [
    "prend", "apag", "enciend", "luz", "luces", "aire", "clima",
    "temperatura", "frío", "frio", "calor", "grados", "todo",
]
DEFAULT_MAX_MESSAGE_CHARS = 300
DEFAULT_VERIFY_BUDGET_S = 3.0
DEFAULT_VERIFY_POLL_MS = 400
DEFAULT_MAX_TARGET_CHOICES = 12

CONFIG_SUBDIR = "laya-router"
CONFIG_FILENAME = "config.yaml"


@dataclass
class Thresholds:
    is_home: float = DEFAULT_THRESHOLD_IS_HOME
    execute: float = DEFAULT_THRESHOLD_EXECUTE


@dataclass
class Config:
    data_dir: Path
    bifrost_url_env: str = DEFAULT_BIFROST_URL_ENV
    bifrost_model: str = DEFAULT_BIFROST_MODEL
    bifrost_api_key_env: str = DEFAULT_BIFROST_API_KEY_ENV
    laya_timeout_s: float = DEFAULT_LAYA_TIMEOUT_S
    ha_timeout_s: float = DEFAULT_HA_TIMEOUT_S
    thresholds: Thresholds = field(default_factory=Thresholds)
    domains: List[str] = field(default_factory=lambda: list(DEFAULT_DOMAINS))
    exclude_from_all_off: List[str] = field(default_factory=list)
    allowed_senders: List[str] = field(default_factory=list)
    aliases: Dict[str, Any] = field(default_factory=dict)
    cache_refresh_minutes: int = DEFAULT_CACHE_REFRESH_MINUTES
    history_turns: int = DEFAULT_HISTORY_TURNS
    prefilter_lexicon: List[str] = field(default_factory=lambda: list(DEFAULT_PREFILTER_LEXICON))
    max_message_chars: int = DEFAULT_MAX_MESSAGE_CHARS
    dry_run: bool = False
    verify_budget_s: float = DEFAULT_VERIFY_BUDGET_S
    verify_poll_ms: int = DEFAULT_VERIFY_POLL_MS
    max_target_choices: int = DEFAULT_MAX_TARGET_CHOICES
    # True si config.yaml existe pero no se pudo leer/parsear o no es un mapeo.
    load_error: bool = False


def resolve_hermes_home() -> Path:
    env = os.environ.get("HERMES_HOME")
    return Path(env) if env else Path.home() / ".hermes"


def load_config(hermes_home: Path) -> Config:
    data_dir = Path(hermes_home) / CONFIG_SUBDIR
    cfg = Config(data_dir=data_dir)
    path = data_dir / CONFIG_FILENAME
    if not path.exists():
        return cfg
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("laya-router: no se pudo leer %s: %s", path, type(exc).__name__)
        cfg.load_error = True
        return cfg
    if raw is None:
        return cfg
    if not isinstance(raw, dict):
        logger.warning("laya-router: %s no es un mapeo; se ignora", path)
        cfg.load_error = True
        return cfg

    known = {f.name for f in fields(Config)} - {"data_dir", "load_error"}
    for key, value in raw.items():
        if key not in known:
            logger.warning("laya-router: clave desconocida en config.yaml: %s", key)
            continue
        if key == "thresholds":
            if not isinstance(value, dict):
                logger.warning("laya-router: 'thresholds' debe ser un mapeo; se ignora")
                continue
            known_th = {f.name for f in fields(Thresholds)}
            for tk, tv in value.items():
                if tk not in known_th:
                    logger.warning("laya-router: clave desconocida en thresholds: %s", tk)
                    continue
                setattr(cfg.thresholds, tk, tv)
            continue
        setattr(cfg, key, copy.deepcopy(value))
    return cfg


def missing_env(cfg: Config, env: Mapping[str, str]) -> List[str]:
    required = ["HASS_URL", "HASS_TOKEN", cfg.bifrost_url_env, cfg.bifrost_api_key_env]
    return [name for name in required if not env.get(name)]
