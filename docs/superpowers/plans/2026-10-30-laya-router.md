# laya-router Implementation Plan

> Nota (2026-10-31): este documento es un registro histórico. La configuración se cambió después a solo-entorno: la URL de Bifrost ya no es una clave `bifrost_url` con default, sino que sale de la variable de entorno cuyo nombre indica `bifrost_url_env` (por defecto `LAYA_BIFROST_URL`), y la API key de `bifrost_api_key_env` (por defecto `LAYA_BIFROST_API_KEY`). Ver el README.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Un plugin de Hermes Agent que clasifica cada mensaje con Laya (vía Bifrost) y, con confianza alta, ejecuta luces, enchufes y aires en Home Assistant antes de que corra el LLM, dejándole solo la confirmación.

**Architecture:** Paquete Python `laya_router/` con módulos puros (config, normalización de texto, cache de entidades, clientes HTTP de Home Assistant y Bifrost, decisión y ejecución) y un módulo `plugin.py` que los cablea a los hooks de Hermes (`pre_llm_call`, `transform_llm_output`), al tool `home_action`, al slash command `/laya` y a una sección de system prompt. Toda la I/O HTTP pasa por una función `transport` inyectable para que los tests no toquen la red.

**Tech Stack:** Python 3.10+, solo stdlib (`urllib`, `json`, `threading`, `re`, `unicodedata`) más `pyyaml` (ya presente en el entorno de Hermes). Tests con `pytest`.

**Spec:** `docs/superpowers/specs/2026-10-30-laya-router-design.md`

## Global Constraints

- Python 3.10+ (sin `match`, sin `Self`). Sin dependencias externas salvo `pyyaml`. HTTP con `urllib.request`, nunca `requests`.
- El código vive en `laya_router/`; el `__init__.py` de la raíz solo expone `register`. Hermes instala el repo entero en `~/.hermes/plugins/hermes-laya-router/`.
- Nunca loguear ni devolver en texto `HASS_TOKEN` ni la API key de Bifrost. Log de decisiones: una línea por turno con mensaje truncado a 80 chars, acción, target, confianzas, camino y latencias.
- Textos al usuario y al LLM en español. Prefijo de contexto inyectado: `[laya-router]`.
- Defaults de configuración exactamente los del spec: `bifrost_url_env: LAYA_BIFROST_URL`, `bifrost_model: spanish`, `bifrost_api_key_env: LAYA_BIFROST_API_KEY`, `laya_timeout_s: 2.0`, `ha_timeout_s: 5.0`, `thresholds.is_home: 0.5`, `thresholds.execute: 0.85`, `domains: [light, switch, climate]`, `exclude_from_all_off: []`, `allowed_senders: []`, `aliases: {}`, `cache_refresh_minutes: 15`, `history_turns: 2`, `prefilter_lexicon: [prend, apag, enciend, luz, luces, aire, clima, temperatura, frío, frio, calor, grados, todo]`, `max_message_chars: 300`, `dry_run: false`, `verify_budget_s: 3.0`, `verify_poll_ms: 400`, `max_target_choices: 12`.
- Acciones: `turn_on`, `turn_off`, `all_off`, `lights_off`, `climate_off`, `set_climate`, `none`. Modos HVAC: `cool`, `heat`, `fan_only`, `auto`. Temperatura: entero 16–30.
- Ningún hook puede levantar una excepción hacia Hermes: cada callback envuelve su cuerpo en `try/except Exception`, loguea y devuelve `None`.
- En el gateway no usar `os.getcwd()`. La ruta de datos es `<HERMES_HOME>/laya-router/` con `HERMES_HOME` del entorno o `~/.hermes`.
- Commits pequeños, uno por tarea como mínimo, mensajes en español con prefijo `feat:`, `test:`, `docs:`.

## Review Focus

1. Un remitente fuera de `allowed_senders` escribe "apagá todo" por Telegram: tiene que ir al camino con pista, nunca ejecutar. Test en Task 8.
2. La luz ya estaba prendida y HA devuelve `200 []` al service call: la verificación por GET tiene que dar "confirmado", no fallo. Test en Task 6.
3. Bifrost devuelve una respuesta sin `answer_confidence` ni `probabilities`: la confianza cae a `probability`/`confidence` o a 0.0, nunca excepción, y 0.0 manda al camino con pista. Test en Task 4.
4. `user_message` llega como lista multimodal, vacío, o es un slash command: el hook devuelve `None` sin llamar a Laya. Tests en Task 2 y Task 8.
5. "Ponelo en 24" sin entidad en el mensaje: `set_climate` con `target == none` no ejecuta aunque la confianza de acción sea alta; va al camino con pista con el historial ya enviado a Laya. Test en Task 5.

---

## Estructura de archivos

```
hermes-laya-router/
├── plugin.yaml                  manifiesto Hermes
├── __init__.py                  expone register(ctx) importando laya_router.plugin
├── laya_router/
│   ├── __init__.py              vacío
│   ├── config.py                Config, load_config, resolve_hermes_home, missing_env
│   ├── textnorm.py              normalize, tokens, lexicon_match, extract_text, extract_temperature, extract_hvac_mode
│   ├── http.py                  http_json(transport por defecto) y HttpError
│   ├── ha_client.py             HAClient, HAError, HAAuthError
│   ├── laya_client.py           LayaClient, Question, Answer, LayaError
│   ├── entities.py              Entity, EntityCache (archivo JSON, refresh, candidatos)
│   ├── router.py                build_questions, prefilter, decide, Decision
│   ├── executor.py              Executor, ExecResult, textos de confirmación/contexto
│   └── plugin.py                LayaRouterPlugin: hooks, tool, comando, system prompt, register(ctx)
├── scripts/probe_bifrost.py     prueba manual contra Bifrost real
├── tests/
│   ├── conftest.py              agrega la raíz del repo al sys.path; fixtures de transport falso
│   ├── test_config.py
│   ├── test_textnorm.py
│   ├── test_ha_client.py
│   ├── test_laya_client.py
│   ├── test_entities.py
│   ├── test_router.py
│   ├── test_executor.py
│   └── test_plugin.py
├── pytest.ini
├── requirements-dev.txt         pytest
└── README.md
```

---

### Task 1: Esqueleto del repo y configuración

**Files:**
- Create: `plugin.yaml`, `__init__.py`, `laya_router/__init__.py`, `laya_router/config.py`, `pytest.ini`, `requirements-dev.txt`, `tests/conftest.py`, `tests/test_config.py`

**Interfaces:**
- Produces:
  - `@dataclass Thresholds(is_home: float = 0.5, execute: float = 0.85)`
  - `@dataclass Config` con un campo por clave del spec (ver Global Constraints), `thresholds: Thresholds`, y `data_dir: Path`.
  - `resolve_hermes_home() -> Path`: `HERMES_HOME` del entorno o `Path.home() / ".hermes"`.
  - `load_config(hermes_home: Path) -> Config`: lee `<hermes_home>/laya-router/config.yaml` si existe (yaml.safe_load, mapping o vacío), hace merge superficial sobre los defaults, `thresholds` merge por campo, y fija `data_dir = hermes_home / "laya-router"`. Claves desconocidas se ignoran con warning.
  - `missing_env(cfg: Config, env: Mapping[str, str]) -> list[str]`: nombres faltantes entre `HASS_URL`, `HASS_TOKEN`, `cfg.bifrost_url_env`, `cfg.bifrost_api_key_env`.

- [ ] **Step 1: Crear `plugin.yaml`**

```yaml
name: laya-router
version: "0.1.0"
description: Clasifica pedidos de casa con Laya y los ejecuta en Home Assistant antes del LLM.
requires_env: [HASS_URL, HASS_TOKEN, LAYA_BIFROST_URL, LAYA_BIFROST_API_KEY]
```

- [ ] **Step 2: Crear `__init__.py` raíz, `laya_router/__init__.py`, `pytest.ini`, `requirements-dev.txt`, `tests/conftest.py`**

Raíz `__init__.py`: inserta `Path(__file__).resolve().parent` en `sys.path` si no está y hace `from laya_router.plugin import register` (import absoluto, para que funcione tanto cargado por Hermes como desde tests). `pytest.ini`: `testpaths = tests`. `requirements-dev.txt`: `pytest>=8`. `conftest.py`: inserta la raíz del repo en `sys.path`.

- [ ] **Step 3: Escribir tests en `tests/test_config.py`**

```python
def test_defaults_when_no_file(tmp_path):
    cfg = load_config(tmp_path)
    assert cfg.bifrost_model == "spanish"
    assert cfg.thresholds.execute == 0.85
    assert cfg.max_target_choices == 12
    assert cfg.data_dir == tmp_path / "laya-router"

def test_merge_partial_file(tmp_path):
    (tmp_path / "laya-router").mkdir()
    (tmp_path / "laya-router" / "config.yaml").write_text("dry_run: true\nthresholds:\n  execute: 0.9\nallowed_senders: ['telegram:1']\n")
    cfg = load_config(tmp_path)
    assert cfg.dry_run is True and cfg.thresholds.execute == 0.9 and cfg.thresholds.is_home == 0.5
    assert cfg.allowed_senders == ["telegram:1"]

def test_missing_env_lists_names():
    cfg = load_config(Path("/nonexistent"))
    assert missing_env(cfg, {"HASS_URL": "x"}) == ["HASS_TOKEN", "LAYA_BIFROST_URL", "LAYA_BIFROST_API_KEY"]

def test_resolve_hermes_home_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert resolve_hermes_home() == tmp_path
```

- [ ] **Step 4: Correr y verificar que fallan**

Run: `cd ~/Documents/hermes-laya-router && python -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt pyyaml && pytest tests/test_config.py -v`
Expected: FAIL con `ModuleNotFoundError` o `ImportError`.

- [ ] **Step 5: Implementar `laya_router/config.py`** con las firmas del bloque Interfaces. Defaults como constantes de módulo.

- [ ] **Step 6: Correr y verificar que pasan**

Run: `pytest tests/test_config.py -v` → PASS.

- [ ] **Step 7: Commit**

```bash
echo ".venv/\n__pycache__/\n.pytest_cache/" > .gitignore
git add -A && git commit -m "feat: esqueleto del plugin y carga de configuración"
```

---

### Task 2: Normalización de texto y extracción de parámetros

**Files:**
- Create: `laya_router/textnorm.py`, `tests/test_textnorm.py`

**Interfaces:**
- Produces:
  - `normalize(text: str) -> str`: minúsculas, sin acentos (NFD + quitar combining), espacios colapsados.
  - `tokens(text: str) -> list[str]`: palabras de `normalize(text)` con `len >= 3`, solo `[a-z0-9]`.
  - `lexicon_match(text: str, lexicon: list[str]) -> bool`: algún token empieza con alguna entrada normalizada del léxico.
  - `extract_text(user_message) -> str`: `str` tal cual; lista de partes → concatena `part["text"]` de las partes con `type == "text"`, separadas por `\n`; cualquier otra cosa → `""`.
  - `extract_temperature(text: str) -> int | None`: primer entero 16–30 como palabra completa, opcionalmente seguido de `°`, `grados` o `º`.
  - `extract_hvac_mode(text: str) -> str | None`: léxico sobre tokens normalizados: `frio|fresco|refriger` → `cool`; `calor|calef|calent` → `heat`; `ventil` → `fan_only`; `autom` → `auto`. Primer match en ese orden.

- [ ] **Step 1: Escribir `tests/test_textnorm.py`**

```python
def test_normalize_strips_accents():
    assert normalize("Prendé la LUZ de la Oficina") == "prende la luz de la oficina"

def test_lexicon_match_prefix():
    assert lexicon_match("apagame todo", ["apag"]) is True
    assert lexicon_match("como estas", ["apag", "luz"]) is False

def test_extract_text_multimodal_and_empty():
    assert extract_text([{"type": "text", "text": "hola"}, {"type": "image_url", "image_url": {}}]) == "hola"
    assert extract_text("") == "" and extract_text(None) == ""

def test_extract_temperature():
    assert extract_temperature("poné el aire en 24 grados") == 24
    assert extract_temperature("poné el aire a 35") is None
    assert extract_temperature("prendé la luz 2") is None

def test_extract_hvac_mode():
    assert extract_hvac_mode("poné el aire en frío") == "cool"
    assert extract_hvac_mode("calefacción en el living") == "heat"
    assert extract_hvac_mode("prendé el aire") is None
```

- [ ] **Step 2: Correr y verificar que fallan** → `pytest tests/test_textnorm.py -v`, FAIL por import.
- [ ] **Step 3: Implementar `laya_router/textnorm.py`** con las firmas de arriba.
- [ ] **Step 4: Correr y verificar que pasan.**
- [ ] **Step 5: Commit** → `git commit -am "feat: normalización de texto y extracción de temperatura y modo"` (con `git add` de los archivos nuevos).

---

### Task 3: Transport HTTP y cliente de Home Assistant

**Files:**
- Create: `laya_router/http.py`, `laya_router/ha_client.py`, `tests/test_ha_client.py`
- Modify: `tests/conftest.py` (fixture `fake_transport`)

**Interfaces:**
- Produces:
  - `Transport = Callable[[str, str, dict, bytes | None, float], tuple[int, bytes]]` (method, url, headers, body, timeout_s) → (status, body). `http_json(...)` es la implementación con `urllib.request`; convierte `HTTPError` en `(status, body)` y otros errores de red en `HttpError(Exception)`.
  - `class HAError(Exception)`, `class HAAuthError(HAError)`.
  - `class HAClient(base_url: str, token: str, timeout_s: float, transport: Transport = http_json)`:
    - `get_states() -> list[dict]` (GET `/api/states`)
    - `get_state(entity_id: str) -> dict | None` (GET `/api/states/<id>`; 404 → `None`)
    - `call_service(domain: str, service: str, data: dict) -> list[dict]` (POST `/api/services/<domain>/<service>`)
    - `areas(domains: list[str]) -> dict[str, str | None]` (POST `/api/template` con un template Jinja que devuelve JSON `[{"e": entity_id, "a": area_name}]` para las entidades de esos dominios; parsea el texto devuelto)
    - Cualquier 401/403 → `HAAuthError`; otro status ≥ 400 o `HttpError` → `HAError`. Header `Authorization: Bearer <token>`, `Content-Type: application/json`.
  - Fixture `fake_transport` en conftest: objeto con `.routes: dict[(method, path_sin_base), (status, obj)]` y `.calls: list[(method, url, body_dict)]`, invocable como Transport, que serializa `obj` a JSON; si `obj` es `str` lo devuelve como texto plano (para `/api/template`).

- [ ] **Step 1: Escribir `tests/test_ha_client.py`**

```python
def test_get_state_404_returns_none(fake_transport):
    fake_transport.routes[("GET", "/api/states/light.x")] = (404, {"message": "not found"})
    assert HAClient("http://ha", "tok", 5, fake_transport).get_state("light.x") is None

def test_call_service_posts_entity_and_bearer(fake_transport):
    fake_transport.routes[("POST", "/api/services/light/turn_on")] = (200, [{"entity_id": "light.x", "state": "on"}])
    out = HAClient("http://ha", "tok", 5, fake_transport).call_service("light", "turn_on", {"entity_id": "light.x"})
    assert out[0]["state"] == "on"
    method, url, body = fake_transport.calls[0]
    assert body == {"entity_id": "light.x"} and fake_transport.last_headers["Authorization"] == "Bearer tok"

def test_401_raises_auth_error(fake_transport):
    fake_transport.routes[("GET", "/api/states")] = (401, {"message": "Unauthorized"})
    with pytest.raises(HAAuthError):
        HAClient("http://ha", "tok", 5, fake_transport).get_states()

def test_areas_parses_template_json(fake_transport):
    fake_transport.routes[("POST", "/api/template")] = (200, '[{"e": "light.living", "a": "Living"}, {"e": "switch.luz_oficina", "a": null}]')
    assert HAClient("http://ha", "tok", 5, fake_transport).areas(["light", "switch"]) == {"light.living": "Living", "switch.luz_oficina": None}
```

- [ ] **Step 2: Correr y verificar que fallan.**
- [ ] **Step 3: Implementar `laya_router/http.py` y `laya_router/ha_client.py`, y la fixture en `tests/conftest.py`.**

Template Jinja sugerido para `areas` (el builder puede ajustarlo si HA lo rechaza):

```jinja
{% set ns = namespace(out=[]) %}{% for s in states if s.domain in {{ domains_json }} %}{% set ns.out = ns.out + [{"e": s.entity_id, "a": area_name(s.entity_id)}] %}{% endfor %}{{ ns.out | to_json }}
```

- [ ] **Step 4: Correr y verificar que pasan.**
- [ ] **Step 5: Commit** → `feat: transport HTTP y cliente de Home Assistant`.

---

### Task 4: Cliente de Laya vía Bifrost

**Files:**
- Create: `laya_router/laya_client.py`, `tests/test_laya_client.py`, `scripts/probe_bifrost.py`

**Interfaces:**
- Produces:
  - `@dataclass Question(type: str, name: str, instructions: str, choices: list[tuple[str, str]] | None = None)` con `to_payload() -> dict` (formato Bifrost: `type`, `name`, `instructions`, y `choices: [{"value", "description"}]` solo para `choice`).
  - `@dataclass Answer(name: str, type: str, value: str | bool | float | None, confidence: float, probabilities: dict[str, float])`.
  - `class LayaError(Exception)`.
  - `class LayaClient(base_url: str, model: str, api_key: str, timeout_s: float, transport: Transport = http_json)`:
    - `decide(input_: str | list[dict], questions: list[Question]) -> dict[str, Answer]`: POST `/v1/decisions` con header `x-api-key`, body `{"model", "input", "questions"}`. Mapea `answers` por `name` (si falta `name`, por posición). Status ≠ 200 o `HttpError` → `LayaError`.
    - `parse_answer(raw: dict, question: Question) -> Answer`: `value` = `probability` (predicate), `choice` (choice), `score` (score). `confidence` = `answer_confidence` si existe; si no, para predicate `probability`; para choice la probabilidad del valor elegido en `probabilities` (lista de `{value, probability}`); si no, `confidence`; si no, `0.0`.

- [ ] **Step 1: Escribir `tests/test_laya_client.py`**

```python
def _resp(answers): return (200, {"model": "spanish", "answers": answers, "usage": {}})

def test_decide_builds_payload_and_maps_by_name(fake_transport):
    fake_transport.routes[("POST", "/v1/decisions")] = _resp([
        {"type": "predicate", "name": "is_home", "probability": 0.93, "answer_confidence": 0.93},
        {"type": "choice", "name": "action", "choice": "turn_on", "answer_confidence": 0.91,
         "probabilities": [{"value": "turn_on", "probability": 0.91}, {"value": "none", "probability": 0.09}]},
    ])
    qs = [Question("predicate", "is_home", "¿pide acción?"),
          Question("choice", "action", "¿qué acción?", [("turn_on", "encender"), ("none", "ninguna")])]
    out = LayaClient("http://bf", "spanish", "k", 2, fake_transport).decide("prendé la luz", qs)
    _, _, body = fake_transport.calls[0]
    assert body["model"] == "spanish" and body["questions"][1]["choices"][0] == {"value": "turn_on", "description": "encender"}
    assert fake_transport.last_headers["x-api-key"] == "k"
    assert out["action"].value == "turn_on" and out["action"].confidence == 0.91
    assert out["is_home"].value == 0.93

def test_confidence_fallbacks_without_answer_confidence():
    q = Question("choice", "action", "x", [("a", ""), ("b", "")])
    assert parse_answer({"type": "choice", "choice": "a", "probabilities": [{"value": "a", "probability": 0.6}]}, q).confidence == 0.6
    assert parse_answer({"type": "choice", "choice": "a", "confidence": 0.4}, q).confidence == 0.4
    assert parse_answer({"type": "choice", "choice": "a"}, q).confidence == 0.0

def test_non_200_raises(fake_transport):
    fake_transport.routes[("POST", "/v1/decisions")] = (500, {"error": {"message": "boom"}})
    with pytest.raises(LayaError):
        LayaClient("http://bf", "spanish", "k", 2, fake_transport).decide("x", [Question("predicate", "p", "i")])
```

- [ ] **Step 2: Correr y verificar que fallan.**
- [ ] **Step 3: Implementar `laya_router/laya_client.py`.**
- [ ] **Step 4: Escribir `scripts/probe_bifrost.py`**: lee `LAYA_BIFROST_URL` y `LAYA_BIFROST_API_KEY` del entorno, manda `"prendé la luz de la oficina"` con las tres preguntas de ejemplo del spec (dos entidades inventadas) y hace `print(json.dumps(raw, indent=2, ensure_ascii=False))` del cuerpo crudo. Es la verificación manual del formato real de `answers` que pide el spec; se corre en el home lab, no en CI.
- [ ] **Step 5: Correr tests y verificar que pasan.**
- [ ] **Step 6: Commit** → `feat: cliente de Laya vía Bifrost y script de prueba`.

---

### Task 5: Cache de entidades y candidatos

**Files:**
- Create: `laya_router/entities.py`, `tests/test_entities.py`

**Interfaces:**
- Consumes: `HAClient.get_states`, `HAClient.areas` (Task 3); `tokens`, `normalize`, `lexicon_match` (Task 2).
- Produces:
  - `@dataclass Entity(entity_id: str, domain: str, friendly_name: str, area: str | None, state: str, aliases: list[str], assumed_state: bool)` con `description() -> str` = `"<friendly_name>[, área <area>][, alias: a, b]"`.
  - `class EntityCache(path: Path, ha: HAClient, domains: list[str], aliases_cfg: dict[str, list[str]], refresh_minutes: int)`:
    - `load() -> int`: lee `path` si existe; devuelve cantidad. Archivo corrupto → 0 y warning.
    - `refresh() -> int`: `get_states()` filtrado por dominio, `areas()` (si falla, áreas `None` y warning), aplica `aliases_cfg[entity_id]`, `assumed_state = attributes.get("assumed_state", False)`, escribe JSON atómico (tmp + `os.replace`), actualiza `last_refresh: datetime | None`. Excepción de HA se propaga.
    - `get(entity_id) -> Entity | None`, `all() -> list[Entity]`, `by_domain(domains: list[str]) -> list[Entity]`.
    - `candidates(text: str, max_n: int) -> list[Entity]`: entidades con algún token compartido entre `tokens(text)` y los tokens de `friendly_name + area + aliases`; si no hay ninguna, entidades de los dominios que sugiere el léxico (`luz|luces|lampara` → `light`+`switch`, `aire|clima|calef|temperatura` → `climate`), si tampoco, `all()`. Orden alfabético por `friendly_name`, cortado a `max_n`.
    - `start_background(stop: threading.Event) -> threading.Thread`: thread daemon que hace `refresh()` cada `refresh_minutes`; ante excepción espera con backoff exponencial desde 1 min hasta el intervalo normal.

- [ ] **Step 1: Escribir `tests/test_entities.py`** con un `HAClient` sobre `fake_transport` y `/api/states` devolviendo cinco entidades: `light.living` ("Luz living", área Living), `switch.luz_oficina` ("Luz oficina", sin área), `climate.living` ("Aire living", `assumed_state: true`), `sensor.temp` (debe filtrarse), `light.cocina` ("Luz cocina").

```python
def test_refresh_filters_domains_and_writes_file(cache, tmp_path):
    assert cache.refresh() == 4
    assert cache.get("sensor.temp") is None
    assert cache.get("climate.living").assumed_state is True
    assert json.loads((tmp_path / "entities.json").read_text())[0]["entity_id"]

def test_load_reads_file_without_network(cache, fake_transport):
    cache.refresh(); n_calls = len(fake_transport.calls)
    cache2 = EntityCache(cache.path, cache.ha, ["light", "switch", "climate"], {}, 15)
    assert cache2.load() == 4 and len(fake_transport.calls) == n_calls

def test_candidates_by_token_then_domain(cache):
    cache.refresh()
    assert [e.entity_id for e in cache.candidates("prendé la luz de la oficina", 12)] == ["switch.luz_oficina"]
    assert [e.entity_id for e in cache.candidates("apagá el aire", 12)] == ["climate.living"]
    assert {e.domain for e in cache.candidates("prendé las luces", 12)} == {"light", "switch"}
    assert len(cache.candidates("hacé algo", 2)) == 2

def test_aliases_from_config_match(cache_with_alias):
    cache_with_alias.refresh()
    assert cache_with_alias.candidates("prendé la de la ofi", 12)[0].entity_id == "switch.luz_oficina"
```

(`cache_with_alias` usa `aliases_cfg={"switch.luz_oficina": ["la de la ofi"]}`.)

- [ ] **Step 2: Correr y verificar que fallan.**
- [ ] **Step 3: Implementar `laya_router/entities.py`.**
- [ ] **Step 4: Correr y verificar que pasan.**
- [ ] **Step 5: Commit** → `feat: cache de entidades con refresh y candidatos`.

---

### Task 6: Ejecutor contra Home Assistant con verificación

**Files:**
- Create: `laya_router/executor.py`, `tests/test_executor.py`

**Interfaces:**
- Consumes: `HAClient` (Task 3), `Entity`, `EntityCache` (Task 5), `Config` (Task 1).
- Produces:
  - `@dataclass ExecResult(ok: bool, status: str, entity_ids: list[str], message: str, failed: list[str])`. `status ∈ {"confirmed", "sent", "failed", "error"}`. `message` es el texto en español para el usuario.
  - `class Executor(ha: HAClient, cache: EntityCache, cfg: Config, sleep: Callable[[float], None] = time.sleep)`:
    - `execute(action: str, entity: Entity | None, hvac_mode: str | None = None, temperature: int | None = None) -> ExecResult`.
    - Mapeo: `turn_on`/`turn_off` → `<domain>/turn_on|turn_off` con `{"entity_id"}`. `set_climate` → si `hvac_mode`: `climate/set_hvac_mode {"entity_id", "hvac_mode"}`; si `temperature`: `climate/set_temperature {"entity_id", "temperature"}`; ambos en ese orden; ninguno → equivale a `turn_on`. `all_off` → `turn_off` sobre `cache.by_domain(cfg.domains)` menos `cfg.exclude_from_all_off`; `lights_off` → dominios `light` y `switch`; `climate_off` → `climate`.
    - `verify(entity: Entity, expected_state: str | None, expected_temp: int | None) -> str`: si `entity.assumed_state` → `"sent"` sin GET. Si no, GET cada `cfg.verify_poll_ms` ms hasta `cfg.verify_budget_s`; `state == expected_state` (y `attributes.temperature == expected_temp` cuando aplica) → `"confirmed"`; agotado → `"failed"`. Para `set_hvac_mode`, `expected_state = hvac_mode`.
    - `HAAuthError`/`HAError` durante el service call → `ExecResult(ok=False, status="error", ...)`, sin levantar.
    - Textos (`message`): confirmed → `"Listo, <friendly_name> <encendida|apagada|en <modo> a <t>°>."`; sent → `"Le mandé <acción> a <friendly_name>, pero no puedo confirmar que haya respondido."`; failed → `"Intenté <acción> <friendly_name> pero sigue <estado actual>."`; agregados → `"Listo, apagué N dispositivos."` o `"Apagué N, pero quedaron encendidos: a, b."`; error → `"No pude hablar con Home Assistant para <acción>."`. Género: `light`/`switch` femenino ("encendida"), `climate` masculino ("encendido").

- [ ] **Step 1: Escribir `tests/test_executor.py`** con `sleep` falso y un `fake_transport` cuyo `/api/states/<id>` se puede cambiar entre llamadas (la fixture admite una lista de respuestas sucesivas por ruta).

```python
def test_turn_on_confirmed_even_when_service_returns_empty_list(executor, fake_transport):
    fake_transport.routes[("POST", "/api/services/switch/turn_on")] = (200, [])
    fake_transport.routes[("GET", "/api/states/switch.luz_oficina")] = (200, {"state": "on", "attributes": {}})
    r = executor.execute("turn_on", entity("switch.luz_oficina"))
    assert r.ok and r.status == "confirmed" and r.message == "Listo, Luz oficina encendida."

def test_verify_polls_until_state_changes(executor, fake_transport, fake_sleep):
    fake_transport.routes[("POST", "/api/services/light/turn_off")] = (200, [])
    fake_transport.sequence[("GET", "/api/states/light.living")] = [(200, {"state": "on", "attributes": {}}), (200, {"state": "on", "attributes": {}}), (200, {"state": "off", "attributes": {}})]
    r = executor.execute("turn_off", entity("light.living"))
    assert r.status == "confirmed" and fake_sleep.total == pytest.approx(0.8)

def test_verify_budget_exhausted_is_failed(executor, fake_transport):
    fake_transport.routes[("POST", "/api/services/light/turn_off")] = (200, [])
    fake_transport.routes[("GET", "/api/states/light.living")] = (200, {"state": "on", "attributes": {}})
    r = executor.execute("turn_off", entity("light.living"))
    assert not r.ok and r.status == "failed" and "sigue" in r.message

def test_assumed_state_is_sent_without_get(executor, fake_transport):
    fake_transport.routes[("POST", "/api/services/climate/turn_off")] = (200, [])
    r = executor.execute("turn_off", entity("climate.living"))  # assumed_state True
    assert r.status == "sent" and not any(m == "GET" for m, _, _ in fake_transport.calls)

def test_set_climate_mode_and_temperature_order(executor, fake_transport):
    fake_transport.routes[("POST", "/api/services/climate/set_hvac_mode")] = (200, [])
    fake_transport.routes[("POST", "/api/services/climate/set_temperature")] = (200, [])
    r = executor.execute("set_climate", entity("climate.cocina"), hvac_mode="cool", temperature=24)  # assumed_state False, GET devuelve cool/24
    assert [u.rsplit("/", 1)[1] for m, u, _ in fake_transport.calls if m == "POST"] == ["set_hvac_mode", "set_temperature"]
    assert r.status == "confirmed"

def test_all_off_respects_exclusions_and_reports_failures(executor_with_exclusion, fake_transport):
    r = executor_with_exclusion.execute("all_off", None)  # excluye switch.heladera; light.living no apaga
    assert "switch.heladera" not in r.entity_ids and r.failed == ["light.living"] and not r.ok

def test_ha_error_is_error_status(executor, fake_transport):
    fake_transport.routes[("POST", "/api/services/light/turn_on")] = (500, {"message": "x"})
    assert executor.execute("turn_on", entity("light.living")).status == "error"
```

- [ ] **Step 2: Correr y verificar que fallan.**
- [ ] **Step 3: Implementar `laya_router/executor.py`** (y extender la fixture con `.sequence` y `fake_sleep` en conftest).
- [ ] **Step 4: Correr y verificar que pasan.**
- [ ] **Step 5: Commit** → `feat: ejecutor de acciones con verificación de estado`.

---

### Task 7: Router: preguntas a Laya, prefiltro y decisión

**Files:**
- Create: `laya_router/router.py`, `tests/test_router.py`

**Interfaces:**
- Consumes: `Question`, `Answer` (Task 4); `Entity` (Task 5); `Config` (Task 1); `lexicon_match`, `extract_temperature`, `extract_hvac_mode` (Task 2).
- Produces:
  - `ACTIONS = ("turn_on", "turn_off", "all_off", "lights_off", "climate_off", "set_climate", "none")`, `AGGREGATES = {"all_off", "lights_off", "climate_off"}`.
  - `prefilter(text: str, cfg: Config) -> bool`: `False` si vacío, si empieza con `/`, si `len(text) > cfg.max_message_chars`, o si no hay `lexicon_match` con `cfg.prefilter_lexicon`.
  - `build_questions(candidates: list[Entity]) -> list[Question]`: tres preguntas con nombres `is_home`, `action`, `target`, instrucciones y descripciones en español (redacción libre del builder, respetando que `all_off` se distinga de `lights_off`/`climate_off`, y que `target` incluya `("none", "ninguna entidad concreta o no aplica")` al final).
  - `build_input(text: str, history: list[dict], history_turns: int) -> list[dict]`: últimos `2 * history_turns` mensajes de `history` con `role in {"user", "assistant"}` y `content` str, más `{"role": "user", "content": text}`.
  - `@dataclass Decision(path: str, action: str, target: Entity | None, hvac_mode: str | None, temperature: int | None, confidences: dict[str, float], reason: str)`; `path ∈ {"skip", "execute", "hint"}`.
  - `decide(text: str, answers: dict[str, Answer], candidates: list[Entity], cfg: Config, fast_allowed: bool) -> Decision`:
    1. `is_home.value < cfg.thresholds.is_home` → `skip`.
    2. `action.value == "none"` → `skip`.
    3. Resolver `target`: `Entity` de `candidates` con ese `entity_id`, o `None` si `"none"`/desconocido.
    4. Parámetros: `hvac_mode = extract_hvac_mode(text)`, `temperature = extract_temperature(text)` solo cuando `action == "set_climate"`.
    5. `execute` si `fast_allowed` y `action.confidence >= cfg.thresholds.execute` y (acción agregada, o `target` no es `None` con `target.confidence >= cfg.thresholds.execute`) y (`action != "set_climate"` o `target.domain == "climate"`). Si no, `hint`.
    6. `reason` explica en una frase corta qué umbral no se cumplió.

- [ ] **Step 1: Escribir `tests/test_router.py`** con helpers `ans(name, value, conf)` y dos candidatos (`switch.luz_oficina`, `climate.living`).

```python
def test_prefilter(cfg):
    assert prefilter("prendé la luz", cfg) and not prefilter("/laya status", cfg)
    assert not prefilter("x" * 301, cfg) and not prefilter("qué hora es", cfg) and not prefilter("", cfg)

def test_build_questions_shapes(cands):
    qs = build_questions(cands)
    assert [q.name for q in qs] == ["is_home", "action", "target"]
    assert [v for v, _ in qs[1].choices] == list(ACTIONS)
    assert qs[2].choices[-1][0] == "none" and qs[2].choices[0][0] == "switch.luz_oficina"

def test_build_input_keeps_last_turns():
    hist = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}, {"role": "user", "content": "c"}, {"role": "assistant", "content": [{"type": "text"}]}]
    out = build_input("apagala", hist, 1)
    assert out == [{"role": "user", "content": "c"}, {"role": "user", "content": "apagala"}] or out[-1]["content"] == "apagala"

def test_decide_execute_when_confident(cfg, cands):
    d = decide("prendé la luz de la oficina", {"is_home": ans("is_home", 0.95, 0.95), "action": ans("action", "turn_on", 0.9), "target": ans("target", "switch.luz_oficina", 0.88)}, cands, cfg, True)
    assert d.path == "execute" and d.target.entity_id == "switch.luz_oficina"

def test_decide_hint_when_target_low(cfg, cands):
    d = decide("prendé la luz", {...is_home 0.9, action turn_on 0.9, target switch.luz_oficina 0.6}, cands, cfg, True)
    assert d.path == "hint" and "target" in d.reason

def test_decide_skip_when_not_home(cfg, cands):
    assert decide("hola", {...is_home 0.1, action none 0.9, target none 0.9}, cands, cfg, True).path == "skip"

def test_set_climate_without_target_is_hint_but_extracts_params(cfg, cands):
    d = decide("ponelo en 24 frío", {...is_home 0.9, action set_climate 0.95, target none 0.9}, cands, cfg, True)
    assert d.path == "hint" and d.temperature == 24 and d.hvac_mode == "cool"

def test_aggregate_executes_without_target(cfg, cands):
    assert decide("apagá las luces", {...is_home 0.9, action lights_off 0.9, target none 0.9}, cands, cfg, True).path == "execute"

def test_fast_not_allowed_forces_hint(cfg, cands):
    assert decide("apagá todo", {...is_home 0.9, action all_off 0.95, target none 0.9}, cands, cfg, False).path == "hint"
```

- [ ] **Step 2: Correr y verificar que fallan.**
- [ ] **Step 3: Implementar `laya_router/router.py`.**
- [ ] **Step 4: Correr y verificar que pasan.**
- [ ] **Step 5: Commit** → `feat: router de decisión y preguntas a Laya`.

---

### Task 8: Plugin: hooks, tool, comando y system prompt

**Files:**
- Create: `laya_router/plugin.py`, `tests/test_plugin.py`

**Interfaces:**
- Consumes: todo lo anterior.
- Produces:
  - `class LayaRouterPlugin(cfg: Config, env: Mapping[str, str], transport: Transport = http_json, clock: Callable[[], float] = time.monotonic)`:
    - `enabled: bool`; en `__init__`, si `missing_env(cfg, env)` no es vacío → `enabled = False` y un warning con los nombres (nunca valores). Si está habilitado construye `HAClient`, `LayaClient`, `EntityCache`, `Executor`; `cache.load()`; si devuelve 0, lanza un refresh en thread (no bloquea).
    - `fast_allowed(platform: str, sender_id: str) -> bool`: `platform == "cli"` o `not cfg.allowed_senders` o `f"{platform}:{sender_id}" in cfg.allowed_senders` o `sender_id in cfg.allowed_senders`.
    - `on_pre_llm_call(session_id: str = "", user_message=None, conversation_history=None, platform: str = "", sender_id: str = "", **kw) -> dict | None`: flujo del spec. Si `enabled` es `False` → `None`. `extract_text` → `prefilter` → `candidates` → `LayaClient.decide` (excepción → log y `None`) → `decide` → si `execute` y `cfg.dry_run` → log `[dry-run]` y tratar como `hint`; si `execute` → `Executor.execute`, guardar `pending[session_id] = (result.message, now)` solo cuando `status != "error"`, devolver `{"context": context_text}`; si `hint` → `{"context": hint_text}`; `skip` → `None`.
    - Textos de contexto: ejecución OK (`confirmed`/`sent`): `"[laya-router] Acción ya ejecutada: <message>. Respondé con una sola frase confirmando exactamente eso. No llames a ningún tool."`; `failed`: `"[laya-router] <message> Informale al usuario. No digas que se hizo."`; `error`: cae a hint con sufijo `" Home Assistant no respondió al intento directo."`; hint: `"[laya-router] Intención probable: <acción legible> sobre <friendly_name o 'sin entidad clara'> (confianza acción X.XX, entidad Y.YY). Si es inequívoco llamá al tool home_action con esos parámetros; si hay dudas, hacé una pregunta corta."`
    - `on_transform_llm_output(response_text: str = "", session_id: str = "", platform: str = "", **kw) -> str | None`: si `platform == "cli"` → `None` (y descarta `pending[session_id]`); si hay `pending[session_id]` con antigüedad < 120 s → lo borra y devuelve su texto; si no → `None`.
    - `handle_home_action(params: dict, **kw) -> str`: valida `action ∈ ACTIONS - {"none"}`, `entity_id` requerido salvo agregados y existente en cache (si no, `refresh()` y reintento), `temperature` 16–30, `hvac_mode` válido. Llama a `Executor.execute` (respeta `dry_run` devolviendo `{"ok": false, "dry_run": true, ...}`). Devuelve `json.dumps({"ok", "status", "entity_ids", "message"}, ensure_ascii=False)`. Errores de validación → `{"ok": false, "error": "..."}`.
    - `handle_command(args: str = "", **kw) -> str`: `"refresh"` → `cache.refresh()` y `"Entidades: N"`; `"status"` (default) → habilitado, umbrales, N entidades, último refresh, `dry_run`, últimas 5 decisiones del log en memoria.
    - `system_prompt_section() -> str`: el texto del spec ("Sección de system prompt"), < 1500 chars.
    - `TOOL_SCHEMA: dict` exactamente el del spec.
  - `register(ctx)`: `cfg = load_config(resolve_hermes_home())`, `plugin = LayaRouterPlugin(cfg, os.environ)`, `ctx.register_hook("pre_llm_call", plugin.on_pre_llm_call)`, `ctx.register_hook("transform_llm_output", plugin.on_transform_llm_output)`, `ctx.register_tool(name="home_action", toolset="laya_router", schema=TOOL_SCHEMA, handler=plugin.handle_home_action)`, `ctx.register_command("laya", plugin.handle_command, "laya-router: status | refresh")`, y `ctx.register_system_prompt_section("laya-router.protocol", plugin.system_prompt_section(), position="after_memory", max_chars=4000)` dentro de `try/except AttributeError` con warning. Si `plugin.enabled` es `False` igual registra todo (los hooks devuelven `None`), para que `/laya status` explique qué falta. Si está habilitado, `plugin.cache.start_background(threading.Event())`.

- [ ] **Step 1: Escribir `tests/test_plugin.py`** con un `FakeCtx` que guarda lo registrado, `fake_transport` con rutas para `/api/states`, `/api/template`, `/v1/decisions`, services y states, y un helper `laya_answers(is_home, action, action_conf, target, target_conf)` que fija la respuesta de Bifrost.

```python
def test_register_wires_everything(fake_ctx, plugin_env):
    register_with(fake_ctx, plugin_env)  # helper que parchea load_config/os.environ
    assert set(fake_ctx.hooks) == {"pre_llm_call", "transform_llm_output"}
    assert fake_ctx.tools[0]["name"] == "home_action" and fake_ctx.commands[0][0] == "laya"
    assert fake_ctx.sections[0][0] == "laya-router.protocol"

def test_disabled_without_env_returns_none_and_logs_once(caplog):
    p = LayaRouterPlugin(cfg, {}, fake_transport)
    assert p.enabled is False
    assert p.on_pre_llm_call(session_id="s", user_message="prendé la luz") is None
    assert sum("HASS_TOKEN" in r.message for r in caplog.records) == 1

def test_fast_path_executes_and_sets_pending(plugin, fake_transport):
    laya_answers(fake_transport, 0.95, "turn_on", 0.92, "switch.luz_oficina", 0.9)
    out = plugin.on_pre_llm_call(session_id="s1", user_message="prendé la luz de la oficina", platform="telegram", sender_id="1")
    assert out["context"].startswith("[laya-router] Acción ya ejecutada: Listo, Luz oficina encendida.")
    assert ("POST", "/api/services/switch/turn_on") in fake_transport.called_routes
    assert plugin.on_transform_llm_output(response_text="bla", session_id="s1", platform="telegram") == "Listo, Luz oficina encendida."
    assert plugin.on_transform_llm_output(response_text="bla", session_id="s1", platform="telegram") is None

def test_cli_does_not_replace_output(plugin, fake_transport):
    laya_answers(fake_transport, 0.95, "turn_on", 0.92, "switch.luz_oficina", 0.9)
    plugin.on_pre_llm_call(session_id="s2", user_message="prendé la luz de la oficina", platform="cli")
    assert plugin.on_transform_llm_output(response_text="ok", session_id="s2", platform="cli") is None

def test_unlisted_sender_gets_hint_not_execution(plugin_with_allowlist, fake_transport):
    laya_answers(fake_transport, 0.95, "all_off", 0.97, "none", 0.9)
    out = plugin_with_allowlist.on_pre_llm_call(session_id="s3", user_message="apagá todo", platform="telegram", sender_id="999")
    assert out["context"].startswith("[laya-router] Intención probable") and not any(m == "POST" and "/api/services/" in u for m, u, _ in fake_transport.calls)

def test_dry_run_logs_and_hints(plugin_dry_run, fake_transport, caplog):
    laya_answers(fake_transport, 0.95, "turn_off", 0.95, "switch.luz_oficina", 0.95)
    out = plugin_dry_run.on_pre_llm_call(session_id="s4", user_message="apagá la luz de la oficina", platform="telegram", sender_id="1")
    assert "Intención probable" in out["context"] and any("[dry-run]" in r.message for r in caplog.records)

def test_prefilter_skips_laya(plugin, fake_transport):
    assert plugin.on_pre_llm_call(session_id="s5", user_message="qué hora es") is None
    assert plugin.on_pre_llm_call(session_id="s5", user_message=[{"type": "image_url"}]) is None
    assert plugin.on_pre_llm_call(session_id="s5", user_message="/laya status") is None
    assert ("POST", "/v1/decisions") not in fake_transport.called_routes

def test_laya_error_falls_back_to_none(plugin, fake_transport):
    fake_transport.routes[("POST", "/v1/decisions")] = (500, {})
    assert plugin.on_pre_llm_call(session_id="s6", user_message="prendé la luz") is None

def test_home_action_tool_validates_and_executes(plugin, fake_transport):
    out = json.loads(plugin.handle_home_action({"action": "turn_on", "entity_id": "switch.luz_oficina"}))
    assert out["ok"] and out["status"] == "confirmed"
    assert json.loads(plugin.handle_home_action({"action": "set_climate", "entity_id": "climate.living", "temperature": 40}))["ok"] is False
    assert json.loads(plugin.handle_home_action({"action": "turn_on", "entity_id": "light.nope"}))["ok"] is False

def test_command_status_and_refresh(plugin):
    assert "Entidades:" in plugin.handle_command("refresh")
    assert "umbrales" in plugin.handle_command("status").lower()

def test_hooks_never_raise(plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_run_turn", lambda *a, **k: 1 / 0)
    assert plugin.on_pre_llm_call(session_id="s7", user_message="prendé la luz") is None
```

- [ ] **Step 2: Correr y verificar que fallan.**
- [ ] **Step 3: Implementar `laya_router/plugin.py`.** `_run_turn` es el método interno que hace el flujo completo; `on_pre_llm_call` solo lo envuelve en `try/except`. Lista circular de últimas 20 decisiones para `/laya status`.
- [ ] **Step 4: Correr toda la suite** → `pytest -v`, todo PASS.
- [ ] **Step 5: Commit** → `feat: plugin de Hermes con hooks, tool home_action y comando /laya`.

---

### Task 9: README, instalación y prueba manual en el home lab

**Files:**
- Create: `README.md`
- Modify: `docs/superpowers/specs/2026-10-30-laya-router-design.md` (solo si la prueba real contradice algo; anotar fecha)

**Interfaces:** ninguna nueva.

- [ ] **Step 1: Escribir `README.md`** en español con: qué hace (tres párrafos), requisitos (Hermes con plugins, Laya detrás de Bifrost, las tres variables de entorno), instalación (`hermes plugins install axelBrei/hermes-laya-router`, `hermes plugins enable laya-router`, o copiar a `~/.hermes/plugins/laya-router/`), configuración (el YAML completo de defaults con una línea de comentario por clave), `allowed_senders` y cómo descubrir el `sender_id` (`/laya status` muestra el último `platform:sender_id` visto), modo `dry_run` recomendado los primeros días, comandos `/laya status` y `/laya refresh`, cómo correr tests, y una sección "Cómo decide" con el diagrama de flujo en texto (skip / execute / hint).

- [ ] **Step 2: Prueba manual en el home lab** (la hace el usuario o un agente con acceso; dejar el checklist en el README bajo "Verificación"):
  1. `python scripts/probe_bifrost.py` y confirmar que `answers` trae `answer_confidence`; si trae otro nombre, ajustar `parse_answer` y su test.
  2. Instalar el plugin, habilitar, `dry_run: true`, reiniciar Hermes, mandar "prendé la luz de la oficina" y mirar en el log la línea de decisión con confianzas.
  3. Probar cinco frases: encender, apagar, "apagá las luces", "poné el aire en 24", "qué hora es". Ajustar umbrales si hace falta.
  4. Poner `dry_run: false`, repetir "prendé la luz de la oficina" y verificar que la respuesta es exactamente "Listo, Luz oficina encendida." y que la luz prendió.

- [ ] **Step 3: Commit** → `docs: README con instalación, configuración y verificación`.

---

## Self-review

- **Cobertura del spec:** flujo por turno (Tasks 7 y 8), tool `home_action` (8), sección de system prompt (8), cache y candidatos (5), preguntas a Laya e historial (7), extracción de clima (2), config y entorno incompleto (1 y 8), verificación con espera y `assumed_state` (6), `dry_run` (8), CLI sin reemplazo (8), allowlist (8), agregados (6 y 7), README e instalación (9). El refresh forzado ante entidad desconocida está en el tool (8) y el 404 en verificación queda cubierto por `failed` tras el presupuesto; el reintento con refresh en el camino rápido lo decide el builder dentro de `Executor` si lo ve necesario.
- **Tipos:** `Transport` definido en Task 3 y consumido en 4, 5, 8. `Entity` de Task 5 usado en 6 y 7. `Answer.confidence` y `.value` de Task 4 usados en 7. `ExecResult.message` de Task 6 usado en 8 como texto de `pending`.
- **Review Focus:** los cinco puntos tienen test en las tareas indicadas.
- **Proporción:** el plan define firmas y tests, no cuerpos; los únicos bloques de código son tests y el template Jinja.
