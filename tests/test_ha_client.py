import pytest

from laya_router.ha_client import HAAuthError, HAClient, HAError
from laya_router.http import HttpError


def test_get_state_404_returns_none(fake_transport):
    fake_transport.routes[("GET", "/api/states/light.x")] = (404, {"message": "not found"})
    assert HAClient("http://ha", "tok", 5, fake_transport).get_state("light.x") is None


def test_call_service_posts_entity_and_bearer(fake_transport):
    fake_transport.routes[("POST", "/api/services/light/turn_on")] = (200, [{"entity_id": "light.x", "state": "on"}])
    out = HAClient("http://ha", "tok", 5, fake_transport).call_service("light", "turn_on", {"entity_id": "light.x"})
    assert out[0]["state"] == "on"
    method, url, body = fake_transport.calls[0]
    assert body == {"entity_id": "light.x"} and fake_transport.last_headers["Authorization"] == "Bearer tok"
    assert fake_transport.last_headers["Content-Type"] == "application/json"


def test_401_raises_auth_error(fake_transport):
    fake_transport.routes[("GET", "/api/states")] = (401, {"message": "Unauthorized"})
    with pytest.raises(HAAuthError):
        HAClient("http://ha", "tok", 5, fake_transport).get_states()


def test_areas_parses_template_json(fake_transport):
    fake_transport.routes[("POST", "/api/template")] = (200, '[{"e": "light.living", "a": "Living"}, {"e": "switch.luz_oficina", "a": null}]')
    assert HAClient("http://ha", "tok", 5, fake_transport).areas(["light", "switch"]) == {"light.living": "Living", "switch.luz_oficina": None}


def test_500_raises_ha_error_without_token(fake_transport):
    fake_transport.routes[("GET", "/api/states")] = (500, {"message": "boom"})
    with pytest.raises(HAError) as ei:
        HAClient("http://ha", "secret-tok", 5, fake_transport).get_states()
    assert not isinstance(ei.value, HAAuthError) and "secret-tok" not in str(ei.value)


def test_network_error_raises_ha_error():
    def boom(*a):
        raise HttpError("down")
    with pytest.raises(HAError):
        HAClient("http://ha", "tok", 5, boom).get_states()


def test_areas_bad_output_raises_ha_error(fake_transport):
    fake_transport.routes[("POST", "/api/template")] = (200, "no json")
    with pytest.raises(HAError):
        HAClient("http://ha", "tok", 5, fake_transport).areas(["light"])


def test_unmatched_route_recorded(fake_transport):
    with pytest.raises(HAError):
        HAClient("http://ha", "tok", 5, fake_transport).get_states()
    assert ("GET", "/api/states") in fake_transport.called_routes
