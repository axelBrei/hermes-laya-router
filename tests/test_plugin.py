import json
import logging
import threading
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

import laya_router.plugin as plugin_mod
from laya_router.config import DEFAULT_BIFROST_API_KEY_ENV, DEFAULT_BIFROST_URL_ENV, Config
from laya_router.entities import Entity
from laya_router.http import HttpError
from laya_router.plugin import TOOL_SCHEMA, LayaRouterPlugin, register

from conftest import FakeSleep

STATES = [
    {"entity_id": "switch.luz_oficina", "state": "off", "attributes": {"friendly_name": "Luz oficina"}},
    {"entity_id": "light.living", "state": "on", "attributes": {"friendly_name": "Luz living"}},
    {"entity_id": "climate.living", "state": "off", "attributes": {"friendly_name": "Aire living", "assumed_state": True}},
    {"entity_id": "climate.cocina", "state": "off", "attributes": {"friendly_name": "Aire cocina"}},
    {"entity_id": "switch.heladera", "state": "on", "attributes": {"friendly_name": "Heladera"}},
]
ENV = {"HASS_URL": "http://ha", "HASS_TOKEN": "tok-secreto", DEFAULT_BIFROST_URL_ENV: "http://bf",
       DEFAULT_BIFROST_API_KEY_ENV: "key-secreta"}


# --- helpers -----------------------------------------------------------------

class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class FakeCtx:
    """Contexto de Hermes falso: guarda todo lo registrado."""

    def __init__(self):
        self.hooks = {}
        self.tools = []
        self.commands = []
        self.sections = []

    def register_hook(self, name, cb):
        self.hooks[name] = cb

    def register_tool(self, name, toolset, schema, handler):
        self.tools.append({"name": name, "toolset": toolset, "schema": schema, "handler": handler})

    def register_command(self, name, handler, description=""):
        self.commands.append((name, handler, description))

    def register_system_prompt_section(self, section_id, text, position="after_memory", max_chars=4000):
        self.sections.append((section_id, text, position, max_chars))


class OldCtx(FakeCtx):
    """Hermes viejo: sin register_system_prompt_section."""

    register_system_prompt_section = property(lambda self: (_ for _ in ()).throw(AttributeError("x")))


def _entities():
    out = []
    for s in STATES:
        eid = s["entity_id"]
        attrs = s["attributes"]
        out.append(Entity(eid, eid.split(".")[0], attrs["friendly_name"], None, s["state"],
                          [], bool(attrs.get("assumed_state", False))))
    return out


def seed_cache(data_dir):
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "entities.json").write_text(json.dumps([asdict(e) for e in _entities()]), encoding="utf-8")


def ha_routes(ft):
    ft.routes[("GET", "/api/states")] = (200, STATES)
    ft.routes[("POST", "/api/template")] = (200, "[]")
    for dom in ("switch", "light", "climate"):
        for svc in ("turn_on", "turn_off"):
            ft.routes[("POST", "/api/services/%s/%s" % (dom, svc))] = (200, [])
    ft.routes[("POST", "/api/services/climate/set_hvac_mode")] = (200, [])
    ft.routes[("POST", "/api/services/climate/set_temperature")] = (200, [])
    ft.routes[("GET", "/api/states/switch.luz_oficina")] = (200, {"state": "on", "attributes": {}})


def laya_answers(ft, is_home, action, action_conf, target, target_conf):
    ft.routes[("POST", "/v1/decisions")] = (200, {"answers": [
        {"type": "predicate", "name": "is_home", "probability": is_home, "answer_confidence": is_home},
        {"type": "choice", "name": "action", "choice": action, "answer_confidence": action_conf},
        {"type": "choice", "name": "target", "choice": target, "answer_confidence": target_conf},
    ]})


def make_cfg(tmp_path, **over):
    return Config(data_dir=tmp_path / "laya-router", **over)


def make_plugin(ft, tmp_path, clock=None, **cfg_over):
    cfg = make_cfg(tmp_path, **cfg_over)
    seed_cache(cfg.data_dir)
    ha_routes(ft)
    p = LayaRouterPlugin(cfg, ENV, ft, clock or FakeClock())
    assert p.enabled
    p.executor.sleep = FakeSleep()
    ft.calls.clear()
    ft.called_routes.clear()
    return p


def service_posts(ft):
    return [u.split("/api/services/", 1)[1] for m, u, _ in ft.calls if m == "POST" and "/api/services/" in u]


def register_with(ctx, env):
    """Llama a register(ctx) con HERMES_HOME y entorno del fixture plugin_env, sin red."""
    created = []

    def factory(cfg, environ):
        p = LayaRouterPlugin(cfg, environ, env.transport, env.clock)
        created.append(p)
        return p

    env.monkeypatch.setattr(plugin_mod, "LayaRouterPlugin", factory)
    register(ctx)
    for p in created:
        if getattr(p, "stop_event", None) is not None:
            p.stop_event.set()
    return created[0] if created else None


# --- fixtures ----------------------------------------------------------------

@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def cfg(tmp_path):
    return make_cfg(tmp_path)


@pytest.fixture
def plugin(fake_transport, tmp_path, clock):
    return make_plugin(fake_transport, tmp_path, clock)


@pytest.fixture
def plugin_with_allowlist(fake_transport, tmp_path, clock):
    return make_plugin(fake_transport, tmp_path, clock, allowed_senders=["telegram:1"])


@pytest.fixture
def plugin_dry_run(fake_transport, tmp_path, clock):
    return make_plugin(fake_transport, tmp_path, clock, dry_run=True)


@pytest.fixture
def fake_ctx():
    return FakeCtx()


@pytest.fixture
def plugin_env(monkeypatch, tmp_path, fake_transport, clock):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    seed_cache(tmp_path / "laya-router")
    ha_routes(fake_transport)
    return SimpleNamespace(monkeypatch=monkeypatch, transport=fake_transport, clock=clock, home=tmp_path)


# --- tests del brief ---------------------------------------------------------

def test_register_wires_everything(fake_ctx, plugin_env):
    register_with(fake_ctx, plugin_env)
    assert set(fake_ctx.hooks) == {"pre_llm_call", "transform_llm_output"}
    assert fake_ctx.tools[0]["name"] == "home_action" and fake_ctx.commands[0][0] == "laya"
    assert fake_ctx.sections[0][0] == "laya-router.protocol"


def test_disabled_without_env_returns_none_and_logs_once(cfg, fake_transport, caplog):
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
    assert out["context"].startswith("[laya-router] Intención probable")
    assert not any(m == "POST" and "/api/services/" in u for m, u, _ in fake_transport.calls)


def test_dry_run_logs_and_hints(plugin_dry_run, fake_transport, caplog):
    caplog.set_level(logging.INFO)
    laya_answers(fake_transport, 0.95, "turn_off", 0.95, "switch.luz_oficina", 0.95)
    out = plugin_dry_run.on_pre_llm_call(session_id="s4", user_message="apagá la luz de la oficina", platform="telegram", sender_id="1")
    assert "Intención probable" in out["context"] and any("[dry-run]" in r.message for r in caplog.records)
    assert service_posts(fake_transport) == []


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


# --- textos de contexto ------------------------------------------------------

def test_failed_context_text_and_pending(plugin, fake_transport):
    fake_transport.routes[("GET", "/api/states/switch.luz_oficina")] = (200, {"state": "off", "attributes": {}})
    laya_answers(fake_transport, 0.95, "turn_on", 0.92, "switch.luz_oficina", 0.9)
    out = plugin.on_pre_llm_call(session_id="f1", user_message="prendé la luz de la oficina", platform="telegram", sender_id="1")
    msg = "Intenté encender Luz oficina pero sigue apagada."
    assert out["context"] == "[laya-router] %s Informale al usuario. No digas que se hizo." % msg
    assert plugin.on_transform_llm_output(response_text="x", session_id="f1", platform="telegram") == msg


def test_confirmed_context_exact_text(plugin, fake_transport):
    laya_answers(fake_transport, 0.95, "turn_on", 0.92, "switch.luz_oficina", 0.9)
    out = plugin.on_pre_llm_call(session_id="c1", user_message="prendé la luz de la oficina", platform="telegram", sender_id="1")
    assert out["context"] == ("[laya-router] Acción ya ejecutada: Listo, Luz oficina encendida. Respondé con una sola "
                              "frase confirmando exactamente eso. No llames a ningún tool.")


def test_error_context_falls_back_to_hint_without_pending(plugin, fake_transport):
    fake_transport.routes[("POST", "/api/services/switch/turn_on")] = (500, {})
    laya_answers(fake_transport, 0.95, "turn_on", 0.92, "switch.luz_oficina", 0.9)
    out = plugin.on_pre_llm_call(session_id="e1", user_message="prendé la luz de la oficina", platform="telegram", sender_id="1")
    assert out["context"].startswith("[laya-router] Intención probable: encender")
    assert out["context"].endswith(" Home Assistant no respondió al intento directo.")
    assert plugin.on_transform_llm_output(response_text="x", session_id="e1", platform="telegram") is None


def test_hint_text_is_readable_spanish(plugin, fake_transport):
    laya_answers(fake_transport, 0.95, "set_climate", 0.6, "climate.living", 0.9)
    out = plugin.on_pre_llm_call(session_id="h1", user_message="poné el aire del living a 24 en frío",
                                 platform="telegram", sender_id="1")
    ctx = out["context"]
    assert ctx.startswith("[laya-router] Intención probable: configurar el aire")
    assert "Aire living" in ctx and "climate.living" in ctx
    assert "(confianza acción 0.60, entidad 0.90)" in ctx
    assert ctx.endswith("Si es inequívoco llamá al tool home_action con esos parámetros; si hay dudas, hacé una pregunta corta.")
    assert service_posts(fake_transport) == []


def test_hint_without_target_says_sin_entidad_clara(plugin, fake_transport):
    laya_answers(fake_transport, 0.95, "turn_off", 0.95, "none", 0.9)
    out = plugin.on_pre_llm_call(session_id="h2", user_message="apagá la luz", platform="telegram", sender_id="1")
    assert "Intención probable: apagar" in out["context"] and "sin entidad clara" in out["context"]


@pytest.mark.parametrize("action,legible", [
    ("all_off", "apagar todo"), ("lights_off", "apagar las luces"), ("climate_off", "apagar los aires"),
])
def test_hint_aggregate_legible(plugin_with_allowlist, fake_transport, action, legible):
    laya_answers(fake_transport, 0.95, action, 0.97, "none", 0.9)
    out = plugin_with_allowlist.on_pre_llm_call(session_id="h3", user_message="apagá todo", platform="telegram", sender_id="7")
    assert out["context"].startswith("[laya-router] Intención probable: %s" % legible)


def test_skip_when_not_home(plugin, fake_transport):
    laya_answers(fake_transport, 0.1, "none", 0.9, "none", 0.9)
    assert plugin.on_pre_llm_call(session_id="k1", user_message="tengo calor hoy", platform="telegram", sender_id="1") is None


# --- respuestas faltantes / errores de Laya -----------------------------------

def test_missing_answers_from_laya_does_not_crash(plugin, fake_transport):
    fake_transport.routes[("POST", "/v1/decisions")] = (200, {"answers": [
        {"type": "predicate", "name": "is_home", "probability": 0.9}]})
    assert plugin.on_pre_llm_call(session_id="m1", user_message="prendé la luz", platform="telegram", sender_id="1") is None
    fake_transport.routes[("POST", "/v1/decisions")] = (200, {"answers": [
        {"type": "predicate", "name": "is_home", "probability": 0.9},
        {"type": "choice", "name": "action", "choice": "turn_on", "answer_confidence": 0.95}]})
    out = plugin.on_pre_llm_call(session_id="m2", user_message="prendé la luz", platform="telegram", sender_id="1")
    assert "Intención probable" in out["context"] and "sin entidad clara" in out["context"]
    assert service_posts(fake_transport) == []
    fake_transport.routes[("POST", "/v1/decisions")] = (200, {"answers": []})
    assert plugin.on_pre_llm_call(session_id="m3", user_message="prendé la luz", platform="telegram", sender_id="1") is None


def test_laya_network_error_returns_none(plugin, fake_transport):
    def boom(method, url, headers, body, timeout_s):
        if url.endswith("/v1/decisions"):
            raise HttpError("TimeoutError")
        return fake_transport(method, url, headers, body, timeout_s)

    plugin.laya._transport = boom
    assert plugin.on_pre_llm_call(session_id="n1", user_message="prendé la luz", platform="telegram", sender_id="1") is None


def test_laya_receives_history_and_candidate_questions(plugin, fake_transport):
    laya_answers(fake_transport, 0.1, "none", 0.9, "none", 0.9)
    history = [{"role": "user", "content": "hola"}, {"role": "assistant", "content": "qué tal"},
               {"role": "tool", "content": "x"}]
    plugin.on_pre_llm_call(session_id="q1", user_message="prendé la luz de la oficina",
                           conversation_history=history, platform="telegram", sender_id="1")
    body = [b for m, u, b in fake_transport.calls if u.endswith("/v1/decisions")][0]
    assert body["input"] == [{"role": "user", "content": "hola"}, {"role": "assistant", "content": "qué tal"},
                             {"role": "user", "content": "prendé la luz de la oficina"}]
    names = [q["name"] for q in body["questions"]]
    assert names == ["is_home", "action", "target"]
    targets = [c["value"] for c in body["questions"][2]["choices"]]
    assert targets == ["switch.luz_oficina", "none"]


# --- fast_allowed ---------------------------------------------------------------

def test_fast_allowed_matrix(fake_transport, tmp_path):
    p = make_plugin(fake_transport, tmp_path)
    assert p.fast_allowed("telegram", "999")  # allowlist vacía
    p = make_plugin(fake_transport, tmp_path, allowed_senders=["telegram:123", "456"])
    assert p.fast_allowed("cli", "")
    assert p.fast_allowed("telegram", "123")
    assert not p.fast_allowed("discord", "123")
    assert p.fast_allowed("discord", "456") and p.fast_allowed("telegram", "456")
    assert not p.fast_allowed("telegram", "999")
    assert not p.fast_allowed("telegram", "")


# --- pending -----------------------------------------------------------------------

def test_pending_expires_after_ttl(plugin, fake_transport, clock):
    laya_answers(fake_transport, 0.95, "turn_on", 0.92, "switch.luz_oficina", 0.9)
    plugin.on_pre_llm_call(session_id="t1", user_message="prendé la luz de la oficina", platform="telegram", sender_id="1")
    clock.now += 121
    assert plugin.on_transform_llm_output(response_text="x", session_id="t1", platform="telegram") is None
    assert "t1" not in plugin.pending


def test_pending_within_ttl_and_other_session(plugin, fake_transport, clock):
    laya_answers(fake_transport, 0.95, "turn_on", 0.92, "switch.luz_oficina", 0.9)
    plugin.on_pre_llm_call(session_id="t2", user_message="prendé la luz de la oficina", platform="telegram", sender_id="1")
    clock.now += 119
    assert plugin.on_transform_llm_output(response_text="x", session_id="otra", platform="telegram") is None
    assert plugin.on_transform_llm_output(response_text="x", session_id="t2", platform="telegram") == "Listo, Luz oficina encendida."


def test_expired_pending_pruned_opportunistically(plugin, fake_transport, clock):
    laya_answers(fake_transport, 0.95, "turn_on", 0.92, "switch.luz_oficina", 0.9)
    plugin.on_pre_llm_call(session_id="old", user_message="prendé la luz de la oficina", platform="telegram", sender_id="1")
    clock.now += 500
    plugin.on_pre_llm_call(session_id="new", user_message="prendé la luz de la oficina", platform="telegram", sender_id="1")
    assert set(plugin.pending) == {"new"}


def test_cli_discards_pending(plugin, fake_transport):
    laya_answers(fake_transport, 0.95, "turn_on", 0.92, "switch.luz_oficina", 0.9)
    plugin.on_pre_llm_call(session_id="c2", user_message="prendé la luz de la oficina", platform="cli")
    assert plugin.on_transform_llm_output(response_text="ok", session_id="c2", platform="cli") is None
    assert plugin.on_transform_llm_output(response_text="ok", session_id="c2", platform="telegram") is None


# --- tool home_action ---------------------------------------------------------------

def _tool(p, params):
    return json.loads(p.handle_home_action(params))


def test_home_action_aggregate(plugin, fake_transport):
    for s in STATES:
        fake_transport.routes[("GET", "/api/states/" + s["entity_id"])] = (200, {"state": "off", "attributes": {}})
    out = _tool(plugin, {"action": "lights_off"})
    assert out["ok"] is True and out["status"] == "confirmed"
    assert set(out["entity_ids"]) == {"switch.luz_oficina", "light.living", "switch.heladera"}
    assert sorted(service_posts(fake_transport)) == ["light/turn_off", "switch/turn_off", "switch/turn_off"]
    assert set(out) == {"ok", "status", "entity_ids", "message"}


def test_home_action_dry_run(plugin_dry_run, fake_transport, caplog):
    caplog.set_level(logging.INFO)
    out = _tool(plugin_dry_run, {"action": "all_off"})
    assert out["ok"] is False and out["dry_run"] is True
    out = _tool(plugin_dry_run, {"action": "turn_on", "entity_id": "switch.luz_oficina"})
    assert out["ok"] is False and out["dry_run"] is True
    assert service_posts(fake_transport) == []
    assert any("[dry-run]" in r.message for r in caplog.records)


def test_home_action_set_climate(plugin, fake_transport):
    fake_transport.routes[("GET", "/api/states/climate.cocina")] = (200, {"state": "cool", "attributes": {"temperature": 24}})
    out = _tool(plugin, {"action": "set_climate", "entity_id": "climate.cocina", "hvac_mode": "cool", "temperature": 24.0})
    assert out["ok"] and out["status"] == "confirmed"
    bodies = [b for m, u, b in fake_transport.calls if u.endswith("/set_temperature")]
    assert bodies == [{"entity_id": "climate.cocina", "temperature": 24}]
    assert isinstance(bodies[0]["temperature"], int)


@pytest.mark.parametrize("params", [
    "no-es-un-dict",
    {},
    {"action": "none"},
    {"action": "explotar"},
    {"action": ["turn_on"]},
    {"action": "turn_on"},
    {"action": "turn_on", "entity_id": 5},
    {"action": "turn_on", "entity_id": ""},
    {"action": "turn_on", "entity_id": "lock.puerta"},
    {"action": "turn_on", "entity_id": "switch.luz_oficina/../../config"},
    {"action": "set_climate", "entity_id": "climate.cocina", "temperature": True},
    {"action": "set_climate", "entity_id": "climate.cocina", "temperature": 24.5},
    {"action": "set_climate", "entity_id": "climate.cocina", "temperature": "24"},
    {"action": "set_climate", "entity_id": "climate.cocina", "temperature": 15},
    {"action": "set_climate", "entity_id": "climate.cocina", "temperature": 31},
    {"action": "set_climate", "entity_id": "climate.cocina", "hvac_mode": "dry"},
    {"action": "set_climate", "entity_id": "climate.cocina", "hvac_mode": 3},
])
def test_home_action_rejects_invalid(plugin, fake_transport, params):
    out = _tool(plugin, params)
    assert out["ok"] is False and isinstance(out["error"], str) and out["error"]
    assert service_posts(fake_transport) == []


def test_home_action_domain_not_configured_never_reaches_ha(fake_transport, tmp_path):
    p = make_plugin(fake_transport, tmp_path, domains=["light", "switch"])
    out = _tool(p, {"action": "turn_on", "entity_id": "climate.living"})
    assert out["ok"] is False
    assert service_posts(fake_transport) == []


def test_home_action_unknown_entity_refreshes_once_then_executes(plugin, fake_transport):
    new = {"entity_id": "light.nueva", "state": "off", "attributes": {"friendly_name": "Luz nueva"}}
    fake_transport.routes[("GET", "/api/states")] = (200, STATES + [new])
    fake_transport.routes[("GET", "/api/states/light.nueva")] = (200, {"state": "on", "attributes": {}})
    out = _tool(plugin, {"action": "turn_on", "entity_id": "light.nueva"})
    assert out["ok"] and out["entity_ids"] == ["light.nueva"]
    assert fake_transport.called_routes.count(("GET", "/api/states")) == 1


def test_home_action_unknown_entity_refreshes_only_once(plugin, fake_transport):
    out = _tool(plugin, {"action": "turn_on", "entity_id": "light.nope"})
    assert out["ok"] is False
    assert fake_transport.called_routes.count(("GET", "/api/states")) == 1


def test_home_action_refresh_failure_is_error_not_exception(plugin, fake_transport):
    fake_transport.routes[("GET", "/api/states")] = (500, {})
    out = _tool(plugin, {"action": "turn_on", "entity_id": "light.nope"})
    assert out["ok"] is False and out["error"]


def test_home_action_disabled(cfg, fake_transport):
    p = LayaRouterPlugin(cfg, {}, fake_transport)
    out = _tool(p, {"action": "turn_on", "entity_id": "switch.luz_oficina"})
    assert out["ok"] is False and out["error"]
    assert fake_transport.calls == []


def test_home_action_never_raises(plugin, monkeypatch):
    monkeypatch.setattr(plugin.executor, "execute", lambda *a, **k: 1 / 0)
    out = _tool(plugin, {"action": "turn_on", "entity_id": "switch.luz_oficina"})
    assert out["ok"] is False and out["error"]


# --- comando /laya --------------------------------------------------------------------

def test_status_content(plugin, fake_transport):
    laya_answers(fake_transport, 0.95, "turn_on", 0.92, "switch.luz_oficina", 0.9)
    for i in range(7):
        plugin.on_pre_llm_call(session_id="st", user_message="prendé la luz de la oficina %d" % i,
                               platform="telegram", sender_id="1")
    plugin.on_pre_llm_call(session_id="st", user_message="qué hora es", platform="discord", sender_id="42")
    plugin.handle_command("refresh")
    out = plugin.handle_command("status")
    assert "discord:42" in out
    assert "0.50" in out and "0.85" in out
    assert "Entidades: 5" in out
    assert "Último refresh:" in out and "nunca" not in out.split("Último refresh:")[1].splitlines()[0]
    assert "dry_run: no" in out
    assert "oficina 6" in out and "oficina 2" in out and "oficina 1" not in out
    assert "tok-secreto" not in out and "key-secreta" not in out
    assert plugin.handle_command("") == plugin.handle_command("status")


def test_status_when_disabled_explains_missing(cfg, fake_transport):
    p = LayaRouterPlugin(cfg, {"HASS_URL": "http://ha"}, fake_transport)
    out = p.handle_command("status")
    assert "deshabilitado" in out.lower() and "HASS_TOKEN" in out and DEFAULT_BIFROST_API_KEY_ENV in out
    assert DEFAULT_BIFROST_URL_ENV in out
    assert "http://ha" not in out
    assert "deshabilitado" in p.handle_command("refresh").lower()
    assert fake_transport.calls == []


def test_missing_bifrost_url_alone_disables_with_warning(cfg, fake_transport, caplog):
    env = {k: v for k, v in ENV.items() if k != DEFAULT_BIFROST_URL_ENV}
    with caplog.at_level(logging.WARNING):
        p = LayaRouterPlugin(cfg, env, fake_transport)
    assert not p.enabled
    assert DEFAULT_BIFROST_URL_ENV in caplog.text
    assert DEFAULT_BIFROST_URL_ENV in p.disabled_reason
    assert "HASS_TOKEN" not in p.disabled_reason and DEFAULT_BIFROST_API_KEY_ENV not in p.disabled_reason
    assert "key-secreta" not in caplog.text and "tok-secreto" not in caplog.text


def test_empty_bifrost_url_counts_as_missing(cfg, fake_transport):
    p = LayaRouterPlugin(cfg, dict(ENV, **{DEFAULT_BIFROST_URL_ENV: ""}), fake_transport)
    assert not p.enabled and DEFAULT_BIFROST_URL_ENV in p.disabled_reason


def test_laya_request_uses_url_from_env(tmp_path, fake_transport):
    laya_answers(fake_transport, 0.95, "turn_on", 0.92, "switch.luz_oficina", 0.9)
    cfg = make_cfg(tmp_path)
    seed_cache(cfg.data_dir)
    ha_routes(fake_transport)
    p = LayaRouterPlugin(cfg, dict(ENV, **{DEFAULT_BIFROST_URL_ENV: "http://bf-env:8080/"}), fake_transport, FakeClock())
    assert p.enabled
    fake_transport.calls.clear()
    p.on_pre_llm_call(session_id="u", user_message="prendé la luz de la oficina", platform="cli", sender_id="1")
    urls = [u for m, u, b in fake_transport.calls if u.endswith("/v1/decisions")]
    assert urls == ["http://bf-env:8080/v1/decisions"]


def test_custom_bifrost_url_env_is_honored(tmp_path, fake_transport):
    laya_answers(fake_transport, 0.95, "turn_on", 0.92, "switch.luz_oficina", 0.9)
    cfg = make_cfg(tmp_path, bifrost_url_env="MI_URL_BF", bifrost_api_key_env="MI_KEY_BF")
    seed_cache(cfg.data_dir)
    ha_routes(fake_transport)
    env = {"HASS_URL": "http://ha", "HASS_TOKEN": "tok-secreto", "MI_URL_BF": "http://bf-custom", "MI_KEY_BF": "k2"}
    p = LayaRouterPlugin(cfg, env, fake_transport, FakeClock())
    assert p.enabled
    fake_transport.calls.clear()
    p.on_pre_llm_call(session_id="c", user_message="prendé la luz de la oficina", platform="cli", sender_id="1")
    urls = [u for m, u, b in fake_transport.calls if u.endswith("/v1/decisions")]
    assert urls == ["http://bf-custom/v1/decisions"]
    # Con el entorno por defecto, las variables personalizadas faltan y se nombran.
    p2 = LayaRouterPlugin(cfg, ENV, fake_transport, FakeClock())
    assert not p2.enabled and "MI_URL_BF" in p2.disabled_reason and "MI_KEY_BF" in p2.disabled_reason


def test_sanitize_invalid_bifrost_url_env_falls_back(caplog):
    cfg = Config(data_dir=Path("/tmp/x"), bifrost_url_env=123)
    with caplog.at_level(logging.WARNING):
        out = plugin_mod.sanitize_config(cfg)
    assert out.bifrost_url_env == DEFAULT_BIFROST_URL_ENV
    assert "bifrost_url_env" in caplog.text


def test_refresh_failure_message(plugin, fake_transport):
    fake_transport.routes[("GET", "/api/states")] = (500, {})
    out = plugin.handle_command("refresh")
    assert "no" in out.lower() and "tok-secreto" not in out


def test_unknown_command_shows_usage(plugin):
    assert "status" in plugin.handle_command("bailar") and "refresh" in plugin.handle_command("bailar")


def test_command_never_raises(plugin, monkeypatch):
    monkeypatch.setattr(plugin.cache, "refresh", lambda: 1 / 0)
    assert isinstance(plugin.handle_command("refresh"), str)


# --- log de decisiones -----------------------------------------------------------------

def test_decision_log_line(plugin, fake_transport, caplog):
    caplog.set_level(logging.INFO, logger="laya_router")
    laya_answers(fake_transport, 0.95, "turn_on", 0.92, "switch.luz_oficina", 0.9)
    msg = "prendé la luz de la oficina " + "x" * 200
    plugin.cfg.max_message_chars = 1000
    plugin.on_pre_llm_call(session_id="l1", user_message=msg, platform="telegram", sender_id="1")
    lines = [r.message for r in caplog.records if "path=" in r.message]
    assert len(lines) == 1
    line = lines[0]
    assert msg[:80] in line and msg[:81] not in line
    for part in ("action=turn_on", "target=switch.luz_oficina", "path=execute", "laya_ms=", "exec_ms=", "0.92", "0.90", "0.95"):
        assert part in line
    for r in caplog.records:
        assert "tok-secreto" not in r.message and "key-secreta" not in r.message


def test_prefilter_skip_does_not_log_body_but_records_sender(plugin, caplog):
    caplog.set_level(logging.DEBUG, logger="laya_router")
    plugin.on_pre_llm_call(session_id="p", user_message="mi secreto personal", platform="telegram", sender_id="77")
    assert not any("secreto" in r.message for r in caplog.records)
    assert "telegram:77" in plugin.handle_command("status")


# --- register / robustez -----------------------------------------------------------------

def test_register_details(fake_ctx, plugin_env):
    p = register_with(fake_ctx, plugin_env)
    assert fake_ctx.hooks["pre_llm_call"] == p.on_pre_llm_call
    assert fake_ctx.hooks["transform_llm_output"] == p.on_transform_llm_output
    tool = fake_ctx.tools[0]
    assert tool["toolset"] == "laya_router" and tool["schema"] is TOOL_SCHEMA and tool["handler"] == p.handle_home_action
    assert fake_ctx.commands[0][1] == p.handle_command and fake_ctx.commands[0][2] == "laya-router: status | refresh"
    sid, text, position, max_chars = fake_ctx.sections[0]
    assert text == p.system_prompt_section() and position == "after_memory" and max_chars == 4000
    assert p.enabled and p.cfg.data_dir == plugin_env.home / "laya-router"


def test_register_without_section_support_warns(plugin_env, caplog):
    ctx = OldCtx()
    register_with(ctx, plugin_env)
    assert set(ctx.hooks) == {"pre_llm_call", "transform_llm_output"} and ctx.tools and ctx.commands
    assert any("system prompt" in r.message.lower() for r in caplog.records)


def test_register_disabled_still_registers(fake_ctx, plugin_env):
    plugin_env.monkeypatch.delenv("HASS_TOKEN")
    p = register_with(fake_ctx, plugin_env)
    assert p.enabled is False and p.stop_event is None
    assert set(fake_ctx.hooks) == {"pre_llm_call", "transform_llm_output"} and fake_ctx.tools and fake_ctx.sections


def test_register_never_raises(plugin_env):
    class BrokenCtx(FakeCtx):
        def register_hook(self, name, cb):
            raise RuntimeError("boom")

    ctx = BrokenCtx()
    register_with(ctx, plugin_env)  # no levanta
    assert ctx.tools and ctx.commands and ctx.sections


def test_register_bad_config_does_not_raise(fake_ctx, plugin_env):
    (plugin_env.home / "laya-router" / "config.yaml").write_text(
        "domains: null\nthresholds: {execute: alto}\nallowed_senders: 123\ndry_run: 'no'\n"
        "max_target_choices: muchos\nhistory_turns: null\nprefilter_lexicon: luz\n", encoding="utf-8")
    p = register_with(fake_ctx, plugin_env)
    assert fake_ctx.hooks and fake_ctx.tools
    assert p.cfg.domains == ["light", "switch", "climate"]
    assert p.cfg.thresholds.execute == 0.85
    assert p.cfg.allowed_senders == [] and p.fast_locked  # escalar: tipo inválido -> solo pista
    assert p.fast_allowed("cli", "") and not p.fast_allowed("telegram", "123")
    assert p.cfg.dry_run is True  # valor inválido -> lo seguro
    assert p.cfg.max_target_choices == 12 and p.cfg.history_turns == 2


def test_construction_failure_yields_disabled(cfg, fake_transport, monkeypatch, caplog):
    def broken(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(plugin_mod, "HAClient", broken)
    p = LayaRouterPlugin(cfg, ENV, fake_transport)
    assert p.enabled is False
    assert p.on_pre_llm_call(session_id="x", user_message="prendé la luz") is None
    assert "deshabilitado" in p.handle_command("status").lower()
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_empty_cache_refreshes_in_background(cfg, fake_transport):
    ha_routes(fake_transport)
    p = LayaRouterPlugin(cfg, ENV, fake_transport)
    p._init_refresh_thread.join(timeout=5)
    assert len(p.cache.all()) == 5


def test_transform_never_raises(plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_lock", None)
    assert plugin.on_transform_llm_output(response_text="x", session_id="s", platform="telegram") is None


def test_on_pre_llm_call_accepts_unknown_kwargs(plugin):
    assert plugin.on_pre_llm_call(session_id="s", user_message="hola", is_first_turn=True, model="m") is None


# --- system prompt / schema -----------------------------------------------------------

def test_system_prompt_section():
    text = LayaRouterPlugin.system_prompt_section(None)
    assert len(text) < 1500
    for frag in ("[laya-router]", "Acción ya ejecutada", "Intención probable", "home_action", "No digas que se hizo"):
        assert frag in text


def test_tool_schema_matches_spec():
    assert TOOL_SCHEMA["name"] == "home_action"
    props = TOOL_SCHEMA["parameters"]["properties"]
    assert props["action"]["enum"] == ["turn_on", "turn_off", "all_off", "lights_off", "climate_off", "set_climate"]
    assert props["hvac_mode"]["enum"] == ["cool", "heat", "fan_only", "auto"]
    assert props["temperature"] == {"type": "integer", "minimum": 16, "maximum": 30}
    assert TOOL_SCHEMA["parameters"]["required"] == ["action"]
    assert TOOL_SCHEMA["description"].startswith("Ejecuta una acción sobre luces, enchufes o aires")


# --- fix round 1 --------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [[{"telegram": 123}], [None], {"telegram": "123"}, ["telegram:1", None], [True], 3.5])
def test_invalid_allowlist_denies_fast_path_except_cli(fake_transport, tmp_path, caplog, bad):
    p = make_plugin(fake_transport, tmp_path, allowed_senders=bad)
    assert sum("allowed_senders" in r.message for r in caplog.records if r.levelno == logging.WARNING) == 1
    assert p.fast_allowed("cli", "")
    assert not p.fast_allowed("telegram", "1") and not p.fast_allowed("telegram", "123")
    laya_answers(fake_transport, 0.95, "turn_on", 0.92, "switch.luz_oficina", 0.9)
    out = p.on_pre_llm_call(session_id="a1", user_message="prendé la luz de la oficina", platform="telegram", sender_id="1")
    assert out["context"].startswith("[laya-router] Intención probable") and service_posts(fake_transport) == []
    out = p.on_pre_llm_call(session_id="a2", user_message="prendé la luz de la oficina", platform="cli")
    assert out["context"].startswith("[laya-router] Acción ya ejecutada")
    assert "solo pista" in p.handle_command("status")


def test_valid_allowlist_still_allows(fake_transport, tmp_path):
    p = make_plugin(fake_transport, tmp_path, allowed_senders=["telegram:1", 456])
    assert p.fast_allowed("telegram", "1") and p.fast_allowed("discord", "456")
    assert not p.fast_allowed("telegram", "2")
    assert "solo pista" not in p.handle_command("status")


def test_load_error_denies_fast_path_except_cli(fake_transport, tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.load_error = True
    seed_cache(cfg.data_dir)
    ha_routes(fake_transport)
    p = LayaRouterPlugin(cfg, ENV, fake_transport, FakeClock())
    assert p.enabled and p.fast_allowed("cli", "") and not p.fast_allowed("telegram", "1")


def test_unparseable_config_file_registers_hint_only(fake_ctx, plugin_env):
    (plugin_env.home / "laya-router" / "config.yaml").write_text("allowed_senders: [telegram:1\n", encoding="utf-8")
    p = register_with(fake_ctx, plugin_env)
    assert p.enabled and fake_ctx.hooks
    assert p.fast_allowed("cli", "") and not p.fast_allowed("telegram", "1")


def test_new_turn_clears_stale_pending(plugin, fake_transport):
    laya_answers(fake_transport, 0.95, "turn_on", 0.92, "switch.luz_oficina", 0.9)
    plugin.on_pre_llm_call(session_id="p1", user_message="prendé la luz de la oficina", platform="telegram", sender_id="1")
    # transform no corrió (p.ej. el turno falló); llega un turno no domótico
    assert plugin.on_pre_llm_call(session_id="p1", user_message="contame un chiste", platform="telegram", sender_id="1") is None
    assert plugin.on_transform_llm_output(response_text="chiste", session_id="p1", platform="telegram") is None


def test_new_turn_clears_pending_even_when_disabled_path(plugin):
    plugin.pending["p2"] = ("viejo", plugin.clock())
    plugin.enabled = False
    plugin.on_pre_llm_call(session_id="p2", user_message="hola")
    assert "p2" not in plugin.pending


class FakePlatform:
    """Enum-like (p.ej. Platform.TELEGRAM) cuyo str() no es el valor."""

    def __init__(self, value):
        self.value = value

    def __str__(self):
        return "Platform." + self.value.upper()


def test_enum_like_platform_normalized(plugin_with_allowlist, fake_transport):
    p = plugin_with_allowlist
    assert p.fast_allowed(FakePlatform("telegram"), "1") and p.fast_allowed(FakePlatform("cli"), "")
    laya_answers(fake_transport, 0.95, "turn_on", 0.92, "switch.luz_oficina", 0.9)
    out = p.on_pre_llm_call(session_id="e1", user_message="prendé la luz de la oficina",
                            platform=FakePlatform("telegram"), sender_id=1)
    assert out["context"].startswith("[laya-router] Acción ya ejecutada")
    assert "telegram:1" in p.handle_command("status")
    assert p.on_transform_llm_output(response_text="x", session_id="e1",
                                     platform=FakePlatform("telegram")) == "Listo, Luz oficina encendida."
    p.on_pre_llm_call(session_id="e2", user_message="prendé la luz de la oficina", platform=FakePlatform("cli"))
    assert p.on_transform_llm_output(response_text="x", session_id="e2", platform=FakePlatform("cli")) is None


def test_sender_none_with_allowlist_gets_hint(plugin_with_allowlist, fake_transport):
    laya_answers(fake_transport, 0.95, "turn_on", 0.92, "switch.luz_oficina", 0.9)
    out = plugin_with_allowlist.on_pre_llm_call(session_id="n", user_message="prendé la luz de la oficina",
                                                platform="telegram", sender_id=None)
    assert out["context"].startswith("[laya-router] Intención probable") and service_posts(fake_transport) == []


def test_verify_budget_zero_is_valid(tmp_path):
    from laya_router.plugin import sanitize_config
    assert sanitize_config(make_cfg(tmp_path, verify_budget_s=0.0)).verify_budget_s == 0.0
    assert sanitize_config(make_cfg(tmp_path, verify_budget_s=-1)).verify_budget_s == 3.0


# --- final fix 5: candidatos con historial y bandera exact ------------------------------

LIGHTS_NO_WORD = [
    {"entity_id": "light.lampara_living", "state": "on", "attributes": {"friendly_name": "Lámpara living"}},
    {"entity_id": "light.velador", "state": "on", "attributes": {"friendly_name": "Velador"}},
    {"entity_id": "light.techo", "state": "on", "attributes": {"friendly_name": "Techo"}},
]


def _laya_targets(ft):
    body = [b for m, u, b in ft.calls if u.endswith("/v1/decisions")][-1]
    return [c["value"] for c in body["questions"][2]["choices"]]


def test_ambiguous_light_with_domain_fallback_is_hint(plugin, fake_transport):
    fake_transport.routes[("GET", "/api/states")] = (200, LIGHTS_NO_WORD)
    plugin.cache.refresh()
    laya_answers(fake_transport, 0.95, "turn_off", 0.95, "light.velador", 0.95)
    out = plugin.on_pre_llm_call(session_id="x1", user_message="apagá la luz", platform="telegram", sender_id="1")
    assert out["context"].startswith("[laya-router] Intención probable: apagar")
    assert service_posts(fake_transport) == []
    assert "candidatos" in plugin.decisions[-1]


def test_ambiguous_light_truncated_is_hint(fake_transport, tmp_path):
    p = make_plugin(fake_transport, tmp_path, max_target_choices=1)
    laya_answers(fake_transport, 0.95, "turn_off", 0.95, "light.living", 0.95)
    out = p.on_pre_llm_call(session_id="x2", user_message="apagá la luz", platform="telegram", sender_id="1")
    assert out["context"].startswith("[laya-router] Intención probable") and service_posts(fake_transport) == []


def test_apagala_uses_previous_user_turn_for_candidates(fake_transport, tmp_path):
    p = make_plugin(fake_transport, tmp_path, max_target_choices=2)
    fake_transport.routes[("GET", "/api/states/switch.luz_oficina")] = (200, {"state": "off", "attributes": {}})
    laya_answers(fake_transport, 0.95, "turn_off", 0.95, "switch.luz_oficina", 0.95)
    history = [{"role": "user", "content": "prendé la luz de la oficina"},
               {"role": "assistant", "content": "Listo, Luz oficina encendida."}]
    out = p.on_pre_llm_call(session_id="x3", user_message="apagala", conversation_history=history,
                            platform="telegram", sender_id="1")
    assert "switch.luz_oficina" in _laya_targets(fake_transport)
    assert out["context"].startswith("[laya-router] Acción ya ejecutada")
    assert service_posts(fake_transport) == ["switch/turn_off"]


def test_apagala_without_history_is_not_exact(fake_transport, tmp_path):
    p = make_plugin(fake_transport, tmp_path, max_target_choices=2)
    laya_answers(fake_transport, 0.95, "turn_off", 0.95, "climate.cocina", 0.95)
    out = p.on_pre_llm_call(session_id="x4", user_message="apagala", platform="telegram", sender_id="1")
    assert "switch.luz_oficina" not in _laya_targets(fake_transport)
    assert out["context"].startswith("[laya-router] Intención probable") and service_posts(fake_transport) == []


def test_history_non_string_content_is_ignored_for_scoring(fake_transport, tmp_path):
    p = make_plugin(fake_transport, tmp_path, max_target_choices=2)
    laya_answers(fake_transport, 0.1, "none", 0.9, "none", 0.9)
    history = [{"role": "user", "content": [{"type": "text", "text": "luz oficina"}]}, "basura", None]
    p.on_pre_llm_call(session_id="x5", user_message="apagala", conversation_history=history,
                      platform="telegram", sender_id="1")
    assert "switch.luz_oficina" not in _laya_targets(fake_transport)


def test_history_scoring_text_is_capped(fake_transport, tmp_path):
    p = make_plugin(fake_transport, tmp_path, max_target_choices=2)
    laya_answers(fake_transport, 0.1, "none", 0.9, "none", 0.9)
    history = [{"role": "user", "content": "x " * 400 + "luz oficina"}]
    p.on_pre_llm_call(session_id="x6", user_message="apagala", conversation_history=history,
                      platform="telegram", sender_id="1")
    assert "switch.luz_oficina" not in _laya_targets(fake_transport)


def test_exact_match_still_executes(plugin, fake_transport):
    laya_answers(fake_transport, 0.95, "turn_on", 0.92, "switch.luz_oficina", 0.9)
    out = plugin.on_pre_llm_call(session_id="x7", user_message="prendé la luz de la oficina",
                                 platform="telegram", sender_id="1")
    assert out["context"].startswith("[laya-router] Acción ya ejecutada")


# --- final fix 6: exclusiones con typo y refresh forzado limitado ---------------------

def test_plugin_warns_for_unknown_exclusion_after_refresh(fake_transport, tmp_path, caplog):
    p = make_plugin(fake_transport, tmp_path, exclude_from_all_off=["switch.heladra"])
    caplog.set_level(logging.WARNING, logger="laya_router")
    p.handle_command("refresh")
    assert any("switch.heladra" in r.getMessage() and "exclude_from_all_off" in r.getMessage()
               for r in caplog.records)


def test_home_action_unknown_entity_refresh_is_rate_limited(plugin, fake_transport, clock):
    for _ in range(3):
        out = _tool(plugin, {"action": "turn_on", "entity_id": "light.nope"})
        assert out["ok"] is False and "light.nope" in out["error"]
    assert fake_transport.called_routes.count(("GET", "/api/states")) == 1
    clock.now += 59
    _tool(plugin, {"action": "turn_on", "entity_id": "light.otra"})
    assert fake_transport.called_routes.count(("GET", "/api/states")) == 1
    clock.now += 2  # 61 s desde el refresh forzado
    _tool(plugin, {"action": "turn_on", "entity_id": "light.nope"})
    assert fake_transport.called_routes.count(("GET", "/api/states")) == 2
    assert service_posts(fake_transport) == []


def test_home_action_failed_refresh_also_counts_for_rate_limit(plugin, fake_transport):
    fake_transport.routes[("GET", "/api/states")] = (500, {})
    assert "No pude actualizar" in _tool(plugin, {"action": "turn_on", "entity_id": "light.nope"})["error"]
    out = _tool(plugin, {"action": "turn_on", "entity_id": "light.nope"})
    assert out["ok"] is False and "light.nope" in out["error"]
    assert fake_transport.called_routes.count(("GET", "/api/states")) == 1


def test_home_action_known_entity_does_not_refresh(plugin, fake_transport):
    _tool(plugin, {"action": "turn_on", "entity_id": "switch.luz_oficina"})
    assert ("GET", "/api/states") not in fake_transport.called_routes


def test_history_does_not_displace_device_named_in_current_message(fake_transport, tmp_path):
    # El turno previo nombró la cocina; el mensaje actual nombra el aire: Laya debe ver solo el aire.
    p = make_plugin(fake_transport, tmp_path)
    laya_answers(fake_transport, 0.95, "turn_on", 0.95, "climate.living", 0.95)
    history = [{"role": "user", "content": "apagá la luz de la cocina"},
               {"role": "assistant", "content": "Listo."}]
    p.on_pre_llm_call(session_id="x7", user_message="prendé el aire", conversation_history=history,
                      platform="telegram", sender_id="1")
    targets = _laya_targets(fake_transport)
    assert "climate.living" in targets and "light.cocina" not in targets
