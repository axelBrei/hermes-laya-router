import json
import threading

import pytest

from laya_router.entities import EntityCache
from laya_router.ha_client import HAClient, HAError

STATES = [
    {"entity_id": "light.living", "state": "on", "attributes": {"friendly_name": "Luz living"}},
    {"entity_id": "switch.luz_oficina", "state": "off", "attributes": {"friendly_name": "Luz oficina"}},
    {"entity_id": "climate.living", "state": "off", "attributes": {"friendly_name": "Aire living", "assumed_state": True}},
    {"entity_id": "sensor.temp", "state": "21", "attributes": {"friendly_name": "Temp"}},
    {"entity_id": "light.cocina", "state": "off", "attributes": {"friendly_name": "Luz cocina"}},
]
AREAS = '[{"e": "light.living", "a": "Living"}, {"e": "switch.luz_oficina", "a": null}, {"e": "climate.living", "a": "Living"}, {"e": "light.cocina", "a": null}]'
DOMAINS = ["light", "switch", "climate"]


def _ha(fake_transport):
    fake_transport.routes[("GET", "/api/states")] = (200, STATES)
    fake_transport.routes[("POST", "/api/template")] = (200, AREAS)
    return HAClient("http://ha", "tok", 5, fake_transport)


@pytest.fixture
def cache(fake_transport, tmp_path):
    return EntityCache(tmp_path / "entities.json", _ha(fake_transport), DOMAINS, {}, 15)


@pytest.fixture
def cache_with_alias(fake_transport, tmp_path):
    aliases = {"switch.luz_oficina": ["la de la ofi"]}
    return EntityCache(tmp_path / "entities.json", _ha(fake_transport), DOMAINS, aliases, 15)


def test_refresh_filters_domains_and_writes_file(cache, tmp_path):
    assert cache.refresh() == 4
    assert cache.get("sensor.temp") is None
    assert cache.get("climate.living").assumed_state is True
    assert cache.get("light.living").area == "Living"
    assert cache.last_refresh is not None
    assert json.loads((tmp_path / "entities.json").read_text())[0]["entity_id"]
    assert [p.name for p in tmp_path.iterdir()] == ["entities.json"]


def test_refresh_creates_parent_dir(fake_transport, tmp_path):
    c = EntityCache(tmp_path / "sub" / "entities.json", _ha(fake_transport), DOMAINS, {}, 15)
    assert c.refresh() == 4 and (tmp_path / "sub" / "entities.json").exists()


def test_load_reads_file_without_network(cache, fake_transport):
    cache.refresh()
    n_calls = len(fake_transport.calls)
    cache2 = EntityCache(cache.path, cache.ha, DOMAINS, {}, 15)
    assert cache2.load() == 4 and len(fake_transport.calls) == n_calls


def test_load_missing_or_corrupt(cache, tmp_path):
    assert cache.load() == 0
    cache.path.write_text("{not json")
    assert cache.load() == 0 and cache.all() == []


def test_areas_failure_degrades_to_none(cache, fake_transport):
    fake_transport.routes[("POST", "/api/template")] = (500, {"message": "boom"})
    assert cache.refresh() == 4
    assert cache.get("light.living").area is None


def test_refresh_failure_keeps_previous_entities(cache, fake_transport):
    cache.refresh()
    before = cache.path.read_text()
    fake_transport.routes[("GET", "/api/states")] = (500, {"message": "boom"})
    with pytest.raises(HAError):
        cache.refresh()
    assert len(cache.all()) == 4 and cache.path.read_text() == before


def test_description(cache_with_alias):
    cache_with_alias.refresh()
    assert cache_with_alias.get("light.living").description() == "Luz living, área Living"
    assert cache_with_alias.get("switch.luz_oficina").description() == "Luz oficina, alias: la de la ofi"
    assert cache_with_alias.get("light.cocina").description() == "Luz cocina"


def test_by_domain(cache):
    cache.refresh()
    assert {e.entity_id for e in cache.by_domain(["light"])} == {"light.living", "light.cocina"}


def test_candidates_by_token_then_domain(cache):
    cache.refresh()
    assert [e.entity_id for e in cache.candidates("prendé la luz de la oficina", 12)] == ["switch.luz_oficina"]
    assert [e.entity_id for e in cache.candidates("apagá el aire", 12)] == ["climate.living"]
    assert {e.domain for e in cache.candidates("prendé las luces", 12)} == {"light", "switch"}
    assert len(cache.candidates("hacé algo", 2)) == 2


def test_candidates_sorted_and_cut(cache):
    cache.refresh()
    names = [e.friendly_name for e in cache.candidates("prendé las luces", 12)]
    assert names == sorted(names, key=str.lower)
    assert len(cache.candidates("prendé las luces", 2)) == 2


def test_aliases_from_config_match(cache_with_alias):
    cache_with_alias.refresh()
    assert cache_with_alias.candidates("prendé la de la ofi", 12)[0].entity_id == "switch.luz_oficina"


def test_background_refreshes_when_empty_and_stops_promptly(cache):
    stop = threading.Event()
    done = threading.Event()
    orig = cache.refresh

    def refresh():
        n = orig()
        done.set()
        return n

    cache.refresh = refresh
    t = cache.start_background(stop)
    assert t.daemon and done.wait(5)
    stop.set()
    t.join(5)
    assert not t.is_alive() and len(cache.all()) == 4


def test_background_skips_initial_refresh_if_loaded(cache):
    cache.refresh()
    calls = []
    cache.refresh = lambda: calls.append(1) or 4
    stop = threading.Event()
    t = cache.start_background(stop)
    stop.set()
    t.join(5)
    assert calls == []


def test_background_backoff_survives_exceptions():
    class FakeStop:
        def __init__(self):
            self.waits = []

        def is_set(self):
            return len(self.waits) >= 5

        def wait(self, timeout):
            self.waits.append(timeout)
            return self.is_set()

    c = EntityCache.__new__(EntityCache)
    EntityCache.__init__(c, None, None, DOMAINS, {}, 4)  # 240 s
    c.refresh = lambda: (_ for _ in ()).throw(HAError("down"))
    stop = FakeStop()
    c._loop(stop)
    assert stop.waits == [60, 120, 240, 240, 240]


# --- final fix 5: candidates_info con bandera exact ---------------------------------

def test_candidates_info_exact_when_scored_and_not_cut(cache):
    cache.refresh()
    ents, exact = cache.candidates_info("prendé la luz de la oficina", 12)
    assert [e.entity_id for e in ents] == ["switch.luz_oficina"] and exact is True


def test_candidates_info_truncation_sets_exact_false(cache):
    cache.refresh()
    ents, exact = cache.candidates_info("apagá la luz", 12)
    assert len(ents) == 3 and exact is True  # Luz cocina, Luz living, Luz oficina: todos puntúan, sin corte
    ents, exact = cache.candidates_info("apagá la luz", 2)
    assert len(ents) == 2 and exact is False


def test_candidates_info_domain_fallback_is_not_exact(cache):
    cache.refresh()
    ents, exact = cache.candidates_info("apagá el clima", 12)  # "clima" no está en ningún nombre
    assert [e.entity_id for e in ents] == ["climate.living"] and exact is False


def test_candidates_info_all_fallback_is_not_exact(cache):
    cache.refresh()
    ents, exact = cache.candidates_info("apagala", 12)
    assert len(ents) == 4 and exact is False


def test_candidates_info_empty_cache(cache):
    assert cache.candidates_info("prendé la luz", 12) == ([], False)


def test_candidates_is_thin_wrapper(cache):
    cache.refresh()
    for text, n in (("prendé la luz de la oficina", 12), ("apagá la luz", 2), ("apagala", 3), ("apagá el aire", 12)):
        assert cache.candidates(text, n) == cache.candidates_info(text, n)[0]


# --- final fix 6: aviso de exclusiones inexistentes ----------------------------------

def test_refresh_warns_for_excluded_ids_not_in_cache(fake_transport, tmp_path, caplog):
    c = EntityCache(tmp_path / "entities.json", _ha(fake_transport), DOMAINS, {}, 15,
                    exclude_from_all_off=["light.living", "switch.heladra", "climate.fantasma"])
    caplog.set_level("WARNING", logger="laya_router")
    c.refresh()
    warns = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warns) == 1
    assert "switch.heladra" in warns[0] and "climate.fantasma" in warns[0] and "light.living" not in warns[0]
    assert "tok" not in warns[0]
    c.refresh()  # se repite en cada refresh
    assert sum("switch.heladra" in r.getMessage() for r in caplog.records) == 2


def test_refresh_no_warning_when_exclusions_exist(fake_transport, tmp_path, caplog):
    c = EntityCache(tmp_path / "entities.json", _ha(fake_transport), DOMAINS, {}, 15,
                    exclude_from_all_off=["light.living"])
    caplog.set_level("WARNING", logger="laya_router")
    c.refresh()
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


# --- historial: nunca desplaza a lo nombrado en el mensaje actual ---------------------

def test_history_does_not_displace_device_named_now(cache):
    cache.refresh()
    ents, exact = cache.candidates_info("prendé el aire", 12, "apagá la luz de la cocina")
    assert [e.entity_id for e in ents] == ["climate.living"] and exact


def test_history_used_only_when_current_text_names_nothing(cache):
    cache.refresh()
    ents, exact = cache.candidates_info("apagala", 12, "prendé la luz de la oficina")
    assert [e.entity_id for e in ents] == ["switch.luz_oficina"] and exact


def test_history_without_match_still_falls_back_inexact(cache):
    cache.refresh()
    ents, exact = cache.candidates_info("apagala", 12, "hola, cómo andás")
    assert len(ents) == 4 and not exact
