import pytest

from laya_router.laya_client import Answer, LayaClient, LayaError, Question, parse_answer


def _resp(answers):
    return (200, {"model": "spanish", "answers": answers, "usage": {}})


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
    assert "choices" not in body["questions"][0]
    assert body["input"] == "prendé la luz"
    assert fake_transport.last_headers["x-api-key"] == "k"
    assert out["action"].value == "turn_on" and out["action"].confidence == 0.91
    assert out["is_home"].value == 0.93


def test_confidence_fallbacks_without_answer_confidence():
    q = Question("choice", "action", "x", [("a", ""), ("b", "")])
    assert parse_answer({"type": "choice", "choice": "a", "probabilities": [{"value": "a", "probability": 0.6}]}, q).confidence == 0.6
    assert parse_answer({"type": "choice", "choice": "a", "confidence": 0.4}, q).confidence == 0.4
    assert parse_answer({"type": "choice", "choice": "a"}, q).confidence == 0.0


def test_predicate_and_score_parsing():
    p = Question("predicate", "p", "i")
    a = parse_answer({"probability": 0.7}, p)
    assert a.value == 0.7 and a.confidence == 0.7
    s = Question("score", "s", "i")
    assert parse_answer({"score": 0.25, "confidence": 0.5}, s).value == 0.25


def test_parse_is_tolerant_to_odd_fields():
    q = Question("choice", "action", "x", [("a", "")])
    a = parse_answer({"choice": "a", "probabilities": "garbage", "answer_confidence": "nope"}, q)
    assert a.confidence == 0.0 and a.probabilities == {}
    a = parse_answer({}, q)
    assert a.value is None and a.confidence == 0.0
    a = parse_answer({"probabilities": [{"value": "a", "probability": 0.2}, 5, {"value": "b"}]}, q)
    assert a.probabilities == {"a": 0.2}


def test_maps_by_position_when_name_missing(fake_transport):
    fake_transport.routes[("POST", "/v1/decisions")] = _resp([{"probability": 0.8}, {"choice": "x"}])
    qs = [Question("predicate", "p1", "i"), Question("choice", "p2", "i", [("x", "")])]
    out = LayaClient("http://bf", "m", "k", 2, fake_transport).decide([{"role": "user", "content": "hi"}], qs)
    assert out["p1"].value == 0.8 and out["p2"].value == "x"


def test_non_200_raises(fake_transport):
    fake_transport.routes[("POST", "/v1/decisions")] = (500, {"error": {"message": "boom"}})
    with pytest.raises(LayaError) as ei:
        LayaClient("http://bf", "spanish", "k", 2, fake_transport).decide("x", [Question("predicate", "p", "i")])
    assert "boom" not in str(ei.value) and "500" in str(ei.value)


@pytest.mark.parametrize("body", ["not json", {"answers": "x"}, {"no": "answers"}, [1]])
def test_malformed_200_raises(fake_transport, body):
    fake_transport.routes[("POST", "/v1/decisions")] = (200, body)
    with pytest.raises(LayaError):
        LayaClient("http://bf", "m", "SECRETKEY", 2, fake_transport).decide("x", [Question("predicate", "p", "i")])


def test_network_error_does_not_leak_key():
    from laya_router.http import HttpError

    def boom(*a, **k):
        raise HttpError("URLError")

    with pytest.raises(LayaError) as ei:
        LayaClient("http://bf", "m", "SECRETKEY", 2, boom).decide("x", [Question("predicate", "p", "i")])
    assert "SECRETKEY" not in str(ei.value) and "http://bf" not in str(ei.value)
