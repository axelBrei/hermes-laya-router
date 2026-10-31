import pytest

from laya_router.config import Config
from laya_router.entities import EntityCache
from laya_router.executor import ExecResult, Executor
from laya_router.ha_client import HAClient

STATES = [
    {"entity_id": "switch.luz_oficina", "state": "off", "attributes": {"friendly_name": "Luz oficina"}},
    {"entity_id": "light.living", "state": "on", "attributes": {"friendly_name": "Luz living"}},
    {"entity_id": "climate.living", "state": "off", "attributes": {"friendly_name": "Aire living", "assumed_state": True}},
    {"entity_id": "climate.cocina", "state": "off", "attributes": {"friendly_name": "Aire cocina", "assumed_state": False}},
    {"entity_id": "switch.heladera", "state": "on", "attributes": {"friendly_name": "Heladera"}},
]
DOMAINS = ["light", "switch", "climate"]

_CACHE = {}


def entity(entity_id):
    return _CACHE["cache"].get(entity_id)


def _build(fake_transport, fake_sleep, tmp_path, exclude=()):
    fake_transport.routes[("GET", "/api/states")] = (200, STATES)
    fake_transport.routes[("POST", "/api/template")] = (200, "[]")
    ha = HAClient("http://ha", "tok", 5, fake_transport)
    cache = EntityCache(tmp_path / "entities.json", ha, DOMAINS, {}, 15)
    cache.refresh()
    fake_transport.calls.clear()
    fake_transport.called_routes.clear()
    _CACHE["cache"] = cache
    cfg = Config(data_dir=tmp_path, exclude_from_all_off=list(exclude))
    return Executor(ha, cache, cfg, sleep=fake_sleep)


@pytest.fixture
def executor(fake_transport, fake_sleep, tmp_path):
    return _build(fake_transport, fake_sleep, tmp_path)


@pytest.fixture
def executor_with_exclusion(fake_transport, fake_sleep, tmp_path):
    return _build(fake_transport, fake_sleep, tmp_path, exclude=["switch.heladera"])


def _posts(fake_transport):
    return [(u.split("/api/services/", 1)[1], b) for m, u, b in fake_transport.calls if m == "POST"]


# --- tests del brief -------------------------------------------------------

def test_turn_on_confirmed_even_when_service_returns_empty_list(executor, fake_transport):
    fake_transport.routes[("POST", "/api/services/switch/turn_on")] = (200, [])
    fake_transport.routes[("GET", "/api/states/switch.luz_oficina")] = (200, {"state": "on", "attributes": {}})
    r = executor.execute("turn_on", entity("switch.luz_oficina"))
    assert r.ok and r.status == "confirmed" and r.message == "Listo, Luz oficina encendida."


def test_verify_polls_until_state_changes(executor, fake_transport, fake_sleep):
    fake_transport.routes[("POST", "/api/services/light/turn_off")] = (200, [])
    fake_transport.sequence[("GET", "/api/states/light.living")] = [
        (200, {"state": "on", "attributes": {}}),
        (200, {"state": "on", "attributes": {}}),
        (200, {"state": "off", "attributes": {}}),
    ]
    r = executor.execute("turn_off", entity("light.living"))
    assert r.status == "confirmed" and fake_sleep.total == pytest.approx(0.8)


def test_verify_budget_exhausted_is_failed(executor, fake_transport, fake_sleep):
    fake_transport.routes[("POST", "/api/services/light/turn_off")] = (200, [])
    fake_transport.routes[("GET", "/api/states/light.living")] = (200, {"state": "on", "attributes": {}})
    r = executor.execute("turn_off", entity("light.living"))
    assert not r.ok and r.status == "failed" and "sigue" in r.message
    assert r.message == "Intenté apagar Luz living pero sigue encendida."
    assert r.failed == ["light.living"]
    # presupuesto 3.0 s / poll 400 ms -> 7 sleeps (2.8 s) y 8 GETs
    assert fake_sleep.calls == [pytest.approx(0.4)] * 7 and fake_sleep.total == pytest.approx(2.8)
    assert fake_transport.called_routes.count(("GET", "/api/states/light.living")) == 8


def test_assumed_state_is_sent_without_get(executor, fake_transport):
    fake_transport.routes[("POST", "/api/services/climate/turn_off")] = (200, [])
    r = executor.execute("turn_off", entity("climate.living"))  # assumed_state True
    assert r.status == "sent" and not any(m == "GET" for m, _, _ in fake_transport.calls)
    assert r.ok
    assert r.message == "Le mandé apagar a Aire living, pero no puedo confirmar que haya respondido."


def test_set_climate_mode_and_temperature_order(executor, fake_transport):
    fake_transport.routes[("POST", "/api/services/climate/set_hvac_mode")] = (200, [])
    fake_transport.routes[("POST", "/api/services/climate/set_temperature")] = (200, [])
    fake_transport.routes[("GET", "/api/states/climate.cocina")] = (200, {"state": "cool", "attributes": {"temperature": 24.0}})
    r = executor.execute("set_climate", entity("climate.cocina"), hvac_mode="cool", temperature=24)
    assert [u.rsplit("/", 1)[1] for m, u, _ in fake_transport.calls if m == "POST"] == ["set_hvac_mode", "set_temperature"]
    assert r.status == "confirmed"
    assert _posts(fake_transport) == [
        ("climate/set_hvac_mode", {"entity_id": "climate.cocina", "hvac_mode": "cool"}),
        ("climate/set_temperature", {"entity_id": "climate.cocina", "temperature": 24}),
    ]
    assert r.message == "Listo, Aire cocina en frío a 24°."


def _all_off_routes(fake_transport):
    for d in ("switch", "light", "climate"):
        fake_transport.routes[("POST", "/api/services/%s/turn_off" % d)] = (200, [])
    fake_transport.routes[("GET", "/api/states/switch.luz_oficina")] = (200, {"state": "off", "attributes": {}})
    fake_transport.routes[("GET", "/api/states/light.living")] = (200, {"state": "on", "attributes": {}})
    fake_transport.routes[("GET", "/api/states/climate.cocina")] = (200, {"state": "off", "attributes": {}})
    fake_transport.routes[("GET", "/api/states/switch.heladera")] = (200, {"state": "off", "attributes": {}})


def test_all_off_respects_exclusions_and_reports_failures(executor_with_exclusion, fake_transport):
    _all_off_routes(fake_transport)
    r = executor_with_exclusion.execute("all_off", None)  # excluye switch.heladera; light.living no apaga
    assert "switch.heladera" not in r.entity_ids and r.failed == ["light.living"] and not r.ok
    assert r.status == "failed"
    # climate.living (assumed_state) solo se envió: no cuenta en "Apagué N"
    assert r.message == "Apagué 2, pero quedaron encendidos: Luz living. A Aire living le mandé la orden sin poder confirmar."
    assert all(b["entity_id"] != "switch.heladera" for _, b in _posts(fake_transport))


def test_ha_error_is_error_status(executor, fake_transport):
    fake_transport.routes[("POST", "/api/services/light/turn_on")] = (500, {"message": "x"})
    assert executor.execute("turn_on", entity("light.living")).status == "error"


# --- tests adicionales -----------------------------------------------------

def test_ha_error_message_and_no_secrets(executor, fake_transport):
    fake_transport.routes[("POST", "/api/services/light/turn_on")] = (401, {"message": "x"})
    r = executor.execute("turn_on", entity("light.living"))
    assert isinstance(r, ExecResult) and not r.ok and r.status == "error"
    assert r.message == "No pude hablar con Home Assistant para encender Luz living."
    assert "tok" not in r.message and "http" not in r.message
    assert r.failed == ["light.living"]


def test_all_off_success_message(executor_with_exclusion, fake_transport):
    _all_off_routes(fake_transport)
    fake_transport.routes[("GET", "/api/states/light.living")] = (200, {"state": "off", "attributes": {}})
    r = executor_with_exclusion.execute("all_off", None)
    assert r.ok and r.failed == [] and r.message == "Listo, apagué 3 dispositivos. A Aire living le mandé la orden sin poder confirmar."
    assert r.status == "sent"
    assert sorted(r.entity_ids) == ["climate.cocina", "climate.living", "light.living", "switch.luz_oficina"]
    # assumed_state no se verifica por GET
    assert ("GET", "/api/states/climate.living") not in fake_transport.called_routes


def test_all_off_without_exclusion_includes_heladera(executor, fake_transport):
    _all_off_routes(fake_transport)
    fake_transport.routes[("GET", "/api/states/light.living")] = (200, {"state": "off", "attributes": {}})
    r = executor.execute("all_off", None)
    assert "switch.heladera" in r.entity_ids and r.ok


def test_lights_off_only_lights_and_switches(executor_with_exclusion, fake_transport):
    _all_off_routes(fake_transport)
    fake_transport.routes[("GET", "/api/states/light.living")] = (200, {"state": "off", "attributes": {}})
    r = executor_with_exclusion.execute("lights_off", None)
    assert sorted(r.entity_ids) == ["light.living", "switch.luz_oficina"] and r.ok
    assert all(not s.startswith("climate/") for s, _ in _posts(fake_transport))


def test_climate_off_only_climate(executor, fake_transport):
    _all_off_routes(fake_transport)
    r = executor.execute("climate_off", None)
    assert sorted(r.entity_ids) == ["climate.cocina", "climate.living"] and r.ok
    assert r.message == "Listo, apagué 1 dispositivo. A Aire living le mandé la orden sin poder confirmar."
    assert r.status == "sent"


def test_aggregate_with_no_entities_fails(fake_transport, fake_sleep, tmp_path):
    ex = _build(fake_transport, fake_sleep, tmp_path, exclude=[s["entity_id"] for s in STATES])
    r = ex.execute("all_off", None)
    assert not r.ok and r.status == "failed" and r.message == "No encontré dispositivos para apagar."
    assert fake_transport.calls == []


def test_aggregate_partial_service_error_counts_as_failed(executor_with_exclusion, fake_transport):
    _all_off_routes(fake_transport)
    fake_transport.routes[("GET", "/api/states/light.living")] = (200, {"state": "off", "attributes": {}})
    fake_transport.routes[("POST", "/api/services/light/turn_off")] = (500, {"message": "x"})
    r = executor_with_exclusion.execute("all_off", None)
    assert not r.ok and r.status == "failed" and r.failed == ["light.living"]
    assert r.message == "Apagué 2, pero no pude apagar: Luz living. A Aire living le mandé la orden sin poder confirmar."
    assert ("GET", "/api/states/light.living") not in fake_transport.called_routes


def test_aggregate_all_service_errors_is_error(executor_with_exclusion, fake_transport):
    for d in ("switch", "light", "climate"):
        fake_transport.routes[("POST", "/api/services/%s/turn_off" % d)] = (500, {"message": "x"})
    r = executor_with_exclusion.execute("all_off", None)
    assert not r.ok and r.status == "error"
    assert r.message == "No pude hablar con Home Assistant para apagar todo."


def test_climate_turn_on_confirms_with_hvac_mode_state(executor, fake_transport):
    fake_transport.routes[("POST", "/api/services/climate/turn_on")] = (200, [])
    fake_transport.routes[("GET", "/api/states/climate.cocina")] = (200, {"state": "cool", "attributes": {}})
    r = executor.execute("turn_on", entity("climate.cocina"))
    assert r.ok and r.status == "confirmed" and r.message == "Listo, Aire cocina encendido."


def test_climate_turn_on_unavailable_is_failed(executor, fake_transport):
    fake_transport.routes[("POST", "/api/services/climate/turn_on")] = (200, [])
    fake_transport.routes[("GET", "/api/states/climate.cocina")] = (200, {"state": "unavailable", "attributes": {}})
    r = executor.execute("turn_on", entity("climate.cocina"))
    assert not r.ok and r.status == "failed"
    assert r.message == "Intenté encender Aire cocina pero sigue no disponible."


def test_climate_turn_off_requires_off(executor, fake_transport):
    fake_transport.routes[("POST", "/api/services/climate/turn_off")] = (200, [])
    fake_transport.routes[("GET", "/api/states/climate.cocina")] = (200, {"state": "cool", "attributes": {}})
    r = executor.execute("turn_off", entity("climate.cocina"))
    assert r.status == "failed" and r.message == "Intenté apagar Aire cocina pero sigue en frío."


def test_set_climate_temperature_mismatch_fails(executor, fake_transport):
    fake_transport.routes[("POST", "/api/services/climate/set_temperature")] = (200, [])
    fake_transport.routes[("GET", "/api/states/climate.cocina")] = (200, {"state": "cool", "attributes": {"temperature": 22}})
    r = executor.execute("set_climate", entity("climate.cocina"), temperature=24)
    assert r.status == "failed" and r.message == "Intenté configurar Aire cocina pero sigue en frío a 22°."
    assert _posts(fake_transport) == [("climate/set_temperature", {"entity_id": "climate.cocina", "temperature": 24})]


def test_set_climate_only_mode(executor, fake_transport):
    fake_transport.routes[("POST", "/api/services/climate/set_hvac_mode")] = (200, [])
    fake_transport.routes[("GET", "/api/states/climate.cocina")] = (200, {"state": "heat", "attributes": {"temperature": 20}})
    r = executor.execute("set_climate", entity("climate.cocina"), hvac_mode="heat")
    assert r.ok and r.message == "Listo, Aire cocina en calor."
    assert [s for s, _ in _posts(fake_transport)] == ["climate/set_hvac_mode"]


def test_set_climate_without_params_is_turn_on(executor, fake_transport):
    fake_transport.routes[("POST", "/api/services/climate/turn_on")] = (200, [])
    fake_transport.routes[("GET", "/api/states/climate.cocina")] = (200, {"state": "auto", "attributes": {}})
    r = executor.execute("set_climate", entity("climate.cocina"))
    assert r.ok and [s for s, _ in _posts(fake_transport)] == ["climate/turn_on"]


def test_set_climate_assumed_state_sent(executor, fake_transport):
    fake_transport.routes[("POST", "/api/services/climate/set_hvac_mode")] = (200, [])
    fake_transport.routes[("POST", "/api/services/climate/set_temperature")] = (200, [])
    r = executor.execute("set_climate", entity("climate.living"), hvac_mode="cool", temperature=23)
    assert r.ok and r.status == "sent"
    assert r.message == "Le mandé configurar a Aire living, pero no puedo confirmar que haya respondido."


def test_entity_none_for_single_action_is_error(executor, fake_transport):
    r = executor.execute("turn_on", None)
    assert not r.ok and r.status == "error" and r.message and fake_transport.calls == []


def test_unknown_action_is_error(executor, fake_transport):
    r = executor.execute("bailar", entity("light.living"))
    assert not r.ok and r.status == "error" and fake_transport.calls == []


def test_unexpected_exception_is_error(executor, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secreto")

    monkeypatch.setattr(executor.ha, "call_service", boom)
    r = executor.execute("turn_on", entity("light.living"))
    assert not r.ok and r.status == "error" and "secreto" not in r.message


def test_verify_tolerates_transient_get_errors(executor, fake_transport):
    fake_transport.routes[("POST", "/api/services/light/turn_off")] = (200, [])
    fake_transport.sequence[("GET", "/api/states/light.living")] = [
        (500, {"message": "x"}),
        (200, {"state": "off", "attributes": {}}),
    ]
    r = executor.execute("turn_off", entity("light.living"))
    assert r.status == "confirmed"


def test_verify_public_assumed_returns_sent(executor, fake_transport):
    assert executor.verify(entity("climate.living"), "off", None) == "sent"
    assert fake_transport.calls == []


def test_fake_transport_sequence_repeats_last(fake_transport):
    fake_transport.routes[("GET", "/x")] = (200, {"r": 1})
    fake_transport.sequence[("GET", "/x")] = [(200, {"s": 1}), (200, {"s": 2})]
    out = [fake_transport("GET", "http://h/x", {}, None, 1)[1] for _ in range(3)]
    assert out == [b'{"s": 1}', b'{"s": 2}', b'{"s": 2}']


def test_set_climate_on_non_climate_is_error(executor, fake_transport):
    r = executor.execute("set_climate", entity("light.living"), hvac_mode="cool")
    assert not r.ok and r.status == "error" and fake_transport.calls == []


# --- fix round 1: exclusiones en todos los agregados y entidades que no responden ---

def test_lights_off_respects_exclusions(executor_with_exclusion, fake_transport):
    _all_off_routes(fake_transport)
    fake_transport.routes[("GET", "/api/states/light.living")] = (200, {"state": "off", "attributes": {}})
    r = executor_with_exclusion.execute("lights_off", None)
    assert "switch.heladera" not in r.entity_ids and r.ok
    assert all(b["entity_id"] != "switch.heladera" for _, b in _posts(fake_transport))


def test_climate_off_respects_exclusions(fake_transport, fake_sleep, tmp_path):
    ex = _build(fake_transport, fake_sleep, tmp_path, exclude=["climate.cocina"])
    _all_off_routes(fake_transport)
    r = ex.execute("climate_off", None)
    assert r.entity_ids == ["climate.living"] and r.ok
    assert all(b["entity_id"] != "climate.cocina" for _, b in _posts(fake_transport))


def test_all_off_unavailable_entity_is_reported_not_failed(executor_with_exclusion, fake_transport, fake_sleep):
    _all_off_routes(fake_transport)
    fake_transport.routes[("GET", "/api/states/light.living")] = (200, {"state": "unavailable", "attributes": {}})
    r = executor_with_exclusion.execute("all_off", None)
    assert r.ok and r.failed == [] and r.status == "sent"
    assert r.message == "Listo, apagué 2 dispositivos. A Aire living le mandé la orden sin poder confirmar. No responden: Luz living."
    # no se quema el presupuesto: un solo GET y ningún sleep
    assert fake_transport.called_routes.count(("GET", "/api/states/light.living")) == 1
    assert fake_sleep.calls == []


def test_all_off_unknown_state_is_unresponsive(executor_with_exclusion, fake_transport):
    _all_off_routes(fake_transport)
    fake_transport.routes[("GET", "/api/states/light.living")] = (200, {"state": "unknown", "attributes": {}})
    r = executor_with_exclusion.execute("all_off", None)
    assert r.ok and r.message == "Listo, apagué 2 dispositivos. A Aire living le mandé la orden sin poder confirmar. No responden: Luz living."


def test_all_off_never_readable_is_unresponsive(executor_with_exclusion, fake_transport):
    _all_off_routes(fake_transport)
    del fake_transport.routes[("GET", "/api/states/light.living")]  # 404 en cada GET
    r = executor_with_exclusion.execute("all_off", None)
    assert r.ok and r.failed == [] and "No responden: Luz living." in r.message


def test_all_off_errored_call_message(executor_with_exclusion, fake_transport):
    _all_off_routes(fake_transport)
    fake_transport.routes[("POST", "/api/services/switch/turn_off")] = (500, {"message": "x"})
    fake_transport.routes[("GET", "/api/states/light.living")] = (200, {"state": "off", "attributes": {}})
    r = executor_with_exclusion.execute("all_off", None)
    assert not r.ok and r.status == "failed" and r.failed == ["switch.luz_oficina"]
    assert r.message == "Apagué 2, pero no pude apagar: Luz oficina. A Aire living le mandé la orden sin poder confirmar."


def test_all_off_mixed_wrong_errored_unresponsive(fake_transport, fake_sleep, tmp_path):
    ex = _build(fake_transport, fake_sleep, tmp_path)  # sin exclusión: incluye switch.heladera
    _all_off_routes(fake_transport)
    fake_transport.routes[("POST", "/api/services/climate/turn_off")] = (500, {"message": "x"})
    fake_transport.routes[("GET", "/api/states/switch.heladera")] = (200, {"state": "unavailable", "attributes": {}})
    r = ex.execute("all_off", None)  # light.living sigue on; climate.* falla el call
    assert not r.ok and r.status == "failed"
    assert r.failed == ["light.living", "climate.living", "climate.cocina"]
    assert r.message == ("Apagué 1, pero quedaron encendidos: Luz living; no pude apagar: Aire living, Aire cocina."
                         " No responden: Heladera.")


def test_single_unavailable_keeps_failed_behavior(executor, fake_transport):
    fake_transport.routes[("POST", "/api/services/light/turn_off")] = (200, [])
    fake_transport.routes[("GET", "/api/states/light.living")] = (200, {"state": "unavailable", "attributes": {}})
    r = executor.execute("turn_off", entity("light.living"))
    assert not r.ok and r.status == "failed" and r.message == "Intenté apagar Luz living pero sigue no disponible."


# --- fix round 2: agregado sin ninguna entidad confirmada/enviada ---

def test_aggregate_all_unresponsive_is_failed(executor_with_exclusion, fake_transport):
    _all_off_routes(fake_transport)
    fake_transport.routes[("GET", "/api/states/switch.luz_oficina")] = (200, {"state": "unknown", "attributes": {}})
    fake_transport.routes[("GET", "/api/states/light.living")] = (200, {"state": "unavailable", "attributes": {}})
    r = executor_with_exclusion.execute("lights_off", None)
    assert not r.ok and r.status == "failed" and r.failed == []
    assert r.message == "No pude confirmar que se apagara nada: no responden: Luz oficina, Luz living."


def test_aggregate_errored_and_unresponsive_none_done(executor_with_exclusion, fake_transport):
    _all_off_routes(fake_transport)
    fake_transport.routes[("POST", "/api/services/switch/turn_off")] = (500, {"message": "x"})
    fake_transport.routes[("GET", "/api/states/light.living")] = (200, {"state": "unavailable", "attributes": {}})
    r = executor_with_exclusion.execute("lights_off", None)
    assert not r.ok and r.status == "failed" and r.failed == ["switch.luz_oficina"]
    assert r.message == "No pude confirmar que se apagara nada: no pude apagar: Luz oficina; no responden: Luz living."


def test_aggregate_wrong_and_unresponsive_none_done(executor_with_exclusion, fake_transport):
    _all_off_routes(fake_transport)
    fake_transport.routes[("GET", "/api/states/switch.luz_oficina")] = (200, {"state": "unavailable", "attributes": {}})
    r = executor_with_exclusion.execute("lights_off", None)  # light.living sigue on
    assert not r.ok and r.status == "failed" and r.failed == ["light.living"]
    assert r.message == ("No pude confirmar que se apagara nada: quedaron encendidos: Luz living;"
                         " no responden: Luz oficina.")


def test_aggregate_one_done_keeps_partial_message(executor_with_exclusion, fake_transport):
    _all_off_routes(fake_transport)
    fake_transport.routes[("GET", "/api/states/light.living")] = (200, {"state": "unavailable", "attributes": {}})
    r = executor_with_exclusion.execute("lights_off", None)  # switch.luz_oficina confirma off
    assert r.ok and r.message == "Listo, apagué 1 dispositivo. No responden: Luz living."


# --- final fix 3: deadline de ejecución y HA caído --------------------------------

from laya_router.executor import EXECUTE_DEADLINE_S  # noqa: E402
from laya_router.ha_client import HAUnreachable  # noqa: E402
from laya_router.http import HttpError  # noqa: E402


class StepClock:
    """Reloj falso manual (los tests lo avanzan desde el transport)."""

    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


def _many_lights(n):
    return [{"entity_id": "light.l%02d" % i, "state": "on", "attributes": {"friendly_name": "Luz %02d" % i}}
            for i in range(n)]


def _build_many(fake_transport, fake_sleep, tmp_path, n, clock):
    fake_transport.routes[("GET", "/api/states")] = (200, _many_lights(n))
    fake_transport.routes[("POST", "/api/template")] = (200, "[]")
    ha = HAClient("http://ha", "tok", 5, fake_transport)
    cache = EntityCache(tmp_path / "entities.json", ha, DOMAINS, {}, 15)
    cache.refresh()
    fake_transport.calls.clear()
    fake_transport.called_routes.clear()
    return Executor(ha, cache, Config(data_dir=tmp_path), sleep=fake_sleep, clock=clock)


def test_deadline_constant_is_8_seconds():
    assert EXECUTE_DEADLINE_S == 8.0


def test_executor_default_constructor_still_works(fake_transport, fake_sleep, tmp_path):
    ex = _build(fake_transport, fake_sleep, tmp_path)
    assert callable(ex.clock)


def test_ha_client_network_error_is_unreachable(fake_transport):
    def down(*a):
        raise HttpError("URLError")

    with pytest.raises(HAUnreachable):
        HAClient("http://ha", "tok", 5, down).call_service("light", "turn_off", {"entity_id": "light.x"})


def test_aggregate_ha_down_aborts_after_first_attempt(fake_transport, fake_sleep, tmp_path):
    ex = _build_many(fake_transport, fake_sleep, tmp_path, 25, StepClock())
    attempts = []

    def down(method, url, headers, body, timeout_s):
        attempts.append((method, url))
        raise HttpError("TimeoutError")

    ex.ha._transport = down
    r = ex.execute("lights_off", None)
    assert len(attempts) <= 1
    assert not r.ok and r.status == "error"
    assert "Home Assistant no respondió" in r.message
    assert len(r.failed) == 25 and len(r.entity_ids) == 25
    assert "tok" not in r.message and "http" not in r.message


def test_aggregate_network_error_midway_marks_rest_errored(fake_transport, fake_sleep, tmp_path):
    ex = _build_many(fake_transport, fake_sleep, tmp_path, 25, StepClock())
    for i in range(25):
        fake_transport.routes[("GET", "/api/states/light.l%02d" % i)] = (200, {"state": "off", "attributes": {}})
    fake_transport.routes[("POST", "/api/services/light/turn_off")] = (200, [])
    posts = []

    def flaky(method, url, headers, body, timeout_s):
        if method == "POST":
            posts.append(url)
            if len(posts) >= 3:
                raise HttpError("URLError")
        return fake_transport(method, url, headers, body, timeout_s)

    ex.ha._transport = flaky
    r = ex.execute("lights_off", None)
    assert len(posts) == 3
    assert not r.ok and r.status == "failed"
    assert r.message.startswith("Apagué 2, pero Home Assistant no respondió, no pude apagar: Luz 02, Luz 03")
    assert len(r.failed) == 23 and "light.l00" not in r.failed


def test_aggregate_http_status_error_does_not_abort(fake_transport, fake_sleep, tmp_path):
    ex = _build_many(fake_transport, fake_sleep, tmp_path, 3, StepClock())
    for i in range(3):
        fake_transport.routes[("GET", "/api/states/light.l%02d" % i)] = (200, {"state": "off", "attributes": {}})
    fake_transport.sequence[("POST", "/api/services/light/turn_off")] = [(400, {"message": "x"}), (200, [])]
    r = ex.execute("lights_off", None)
    assert service_count(fake_transport) == 3
    assert r.failed == ["light.l00"] and r.message == "Apagué 2, pero no pude apagar: Luz 00."


def service_count(ft):
    return sum(1 for m, u, _ in ft.calls if m == "POST" and "/api/services/" in u)


def test_aggregate_deadline_during_service_calls_stops_calling(fake_transport, fake_sleep, tmp_path):
    clock = StepClock()
    ex = _build_many(fake_transport, fake_sleep, tmp_path, 25, clock)
    for i in range(25):
        fake_transport.routes[("GET", "/api/states/light.l%02d" % i)] = (200, {"state": "off", "attributes": {}})
    fake_transport.routes[("POST", "/api/services/light/turn_off")] = (200, [])

    def slow(method, url, headers, body, timeout_s):
        clock.now += 2.0  # cada request tarda 2 s
        return fake_transport(method, url, headers, body, timeout_s)

    ex.ha._transport = slow
    r = ex.execute("lights_off", None)
    assert service_count(fake_transport) == 4  # 0, 2, 4, 6 s; a los 8 s se corta
    gets = [c for c in fake_transport.called_routes if c[0] == "GET"]
    assert gets == []  # sin tiempo para verificar
    assert not r.ok and r.status == "failed"
    assert "no llegué a tiempo a apagar: Luz 04" in r.message
    assert "no pude confirmar a tiempo: Luz 00, Luz 01, Luz 02, Luz 03." in r.message
    assert len(r.failed) == 21


def test_single_deadline_hit_during_verification(fake_transport, fake_sleep, tmp_path):
    clock = StepClock()
    ex = _build(fake_transport, fake_sleep, tmp_path)
    ex.clock = clock
    fake_transport.routes[("POST", "/api/services/light/turn_off")] = (200, [])
    fake_transport.routes[("GET", "/api/states/light.living")] = (200, {"state": "on", "attributes": {}})
    gets = []

    def slow_get(method, url, headers, body, timeout_s):
        if method == "GET":
            gets.append(url)
            clock.now += 5.0  # GET lento (timeout de HA)
        return fake_transport(method, url, headers, body, timeout_s)

    ex.ha._transport = slow_get
    r = ex.execute("turn_off", entity("light.living"))
    assert len(gets) == 2  # 0 s y 5 s; a los 10 s ya pasó el deadline
    assert not r.ok and r.status == "failed"
    assert r.message == "Intenté apagar Luz living pero no pude confirmar a tiempo."
    assert r.failed == ["light.living"]


def test_single_deadline_between_set_climate_calls(fake_transport, fake_sleep, tmp_path):
    clock = StepClock()
    ex = _build(fake_transport, fake_sleep, tmp_path)
    ex.clock = clock
    fake_transport.routes[("POST", "/api/services/climate/set_hvac_mode")] = (200, [])
    fake_transport.routes[("POST", "/api/services/climate/set_temperature")] = (200, [])

    def slow(method, url, headers, body, timeout_s):
        clock.now += 9.0
        return fake_transport(method, url, headers, body, timeout_s)

    ex.ha._transport = slow
    r = ex.execute("set_climate", entity("climate.cocina"), hvac_mode="cool", temperature=24)
    assert [s for s, _ in _posts(fake_transport)] == ["climate/set_hvac_mode"]
    assert not r.ok and r.status == "failed" and "no pude confirmar a tiempo" in r.message


def test_aggregate_deadline_during_verification(fake_transport, fake_sleep, tmp_path):
    clock = StepClock()
    ex = _build_many(fake_transport, fake_sleep, tmp_path, 2, clock)
    fake_transport.routes[("POST", "/api/services/light/turn_off")] = (200, [])
    fake_transport.routes[("GET", "/api/states/light.l00")] = (200, {"state": "off", "attributes": {}})
    fake_transport.routes[("GET", "/api/states/light.l01")] = (200, {"state": "on", "attributes": {}})

    def slow_get(method, url, headers, body, timeout_s):
        if method == "GET":
            clock.now += 3.0
        return fake_transport(method, url, headers, body, timeout_s)

    ex.ha._transport = slow_get
    r = ex.execute("lights_off", None)
    assert r.ok and r.status == "sent"
    assert r.message == "Listo, apagué 1 dispositivo. No pude confirmar a tiempo: Luz 01."


# --- final fix 4: "Listo, apagué N" cuenta solo confirmados ------------------------

def test_aggregate_only_sent_is_sent_not_failed(fake_transport, fake_sleep, tmp_path):
    ex = _build(fake_transport, fake_sleep, tmp_path, exclude=["climate.cocina"])
    _all_off_routes(fake_transport)
    r = ex.execute("climate_off", None)
    assert r.ok and r.status == "sent" and r.failed == []
    assert r.message == "A Aire living le mandé la orden sin poder confirmar."
    assert "apagué" not in r.message.lower()


def test_aggregate_all_confirmed_is_confirmed(executor_with_exclusion, fake_transport):
    _all_off_routes(fake_transport)
    fake_transport.routes[("GET", "/api/states/light.living")] = (200, {"state": "off", "attributes": {}})
    r = executor_with_exclusion.execute("lights_off", None)
    assert r.ok and r.status == "confirmed" and r.message == "Listo, apagué 2 dispositivos."


def test_aggregate_several_sent_plural(fake_transport, fake_sleep, tmp_path):
    states = [
        {"entity_id": "climate.a", "state": "cool", "attributes": {"friendly_name": "Aire A", "assumed_state": True}},
        {"entity_id": "climate.b", "state": "cool", "attributes": {"friendly_name": "Aire B", "assumed_state": True}},
        {"entity_id": "climate.c", "state": "cool", "attributes": {"friendly_name": "Aire C"}},
    ]
    fake_transport.routes[("GET", "/api/states")] = (200, states)
    fake_transport.routes[("POST", "/api/template")] = (200, "[]")
    ha = HAClient("http://ha", "tok", 5, fake_transport)
    cache = EntityCache(tmp_path / "entities.json", ha, DOMAINS, {}, 15)
    cache.refresh()
    fake_transport.routes[("POST", "/api/services/climate/turn_off")] = (200, [])
    fake_transport.routes[("GET", "/api/states/climate.c")] = (200, {"state": "off", "attributes": {}})
    r = Executor(ha, cache, Config(data_dir=tmp_path), sleep=fake_sleep).execute("climate_off", None)
    assert r.ok and r.status == "sent"
    assert r.message == "Listo, apagué 1 dispositivo. A Aire A, Aire B les mandé la orden sin poder confirmar."


def test_aggregate_sent_and_wrong_without_confirmed_is_failed(fake_transport, fake_sleep, tmp_path):
    ex = _build(fake_transport, fake_sleep, tmp_path,
                exclude=["switch.luz_oficina", "climate.cocina", "switch.heladera"])
    _all_off_routes(fake_transport)  # light.living sigue on; climate.living assumed
    r = ex.execute("all_off", None)
    assert not r.ok and r.status == "failed" and r.failed == ["light.living"]
    assert r.message == "Quedaron encendidos: Luz living. A Aire living le mandé la orden sin poder confirmar."
