from pathlib import Path

import pytest

from laya_router.config import Config
from laya_router.entities import Entity
from laya_router.laya_client import Answer
from laya_router.router import (
    ACTIONS,
    AGGREGATES,
    build_input,
    build_questions,
    decide,
    prefilter,
)


def ans(name, value, conf):
    return Answer(name=name, type="x", value=value, confidence=conf)


@pytest.fixture
def cfg():
    return Config(data_dir=Path("/tmp/laya-router-test"))


@pytest.fixture
def cands():
    return [
        Entity("switch.luz_oficina", "switch", "Luz oficina", "Oficina", "off"),
        Entity("climate.living", "climate", "Aire living", "Living", "off", aliases=["split"]),
    ]


def answers(is_home=0.9, action=("turn_on", 0.9), target=("switch.luz_oficina", 0.9)):
    out = {"is_home": ans("is_home", is_home, is_home)}
    if action is not None:
        out["action"] = ans("action", *action)
    if target is not None:
        out["target"] = ans("target", *target)
    return out


def test_prefilter(cfg):
    assert prefilter("prendé la luz", cfg)
    assert not prefilter("/laya status", cfg)
    assert not prefilter("x" * 301, cfg)
    assert not prefilter("qué hora es", cfg)
    assert not prefilter("", cfg)


def test_prefilter_normalizes_accents(cfg):
    assert prefilter("PRENDÉ eso", cfg)
    assert prefilter("hace FRÍO", cfg)


def test_prefilter_slash_after_strip(cfg):
    assert not prefilter("   /luz", cfg)


def test_build_questions_shapes(cands):
    qs = build_questions(cands)
    assert [q.name for q in qs] == ["is_home", "action", "target"]
    assert [q.type for q in qs] == ["predicate", "choice", "choice"]
    assert [v for v, _ in qs[1].choices] == list(ACTIONS)
    assert qs[2].choices[-1] == ("none", "ninguna entidad concreta o no aplica")
    assert qs[2].choices[0] == ("switch.luz_oficina", cands[0].description())
    assert [v for v, _ in qs[2].choices] == ["switch.luz_oficina", "climate.living", "none"]
    assert qs[2].choices[1][1] == cands[1].description()


def test_build_questions_all_off_distinct(cands):
    desc = dict(build_questions(cands)[1].choices)
    assert "todo" in desc["all_off"].lower()
    assert desc["all_off"] != desc["lights_off"] != desc["climate_off"]
    assert "luces" in desc["lights_off"].lower()
    assert "aire" in desc["climate_off"].lower()
    assert AGGREGATES == {"all_off", "lights_off", "climate_off"}


def test_build_input_keeps_last_turns():
    hist = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "c"},
        {"role": "assistant", "content": [{"type": "text"}]},
    ]
    out = build_input("apagala", hist, 1)
    assert out == [{"role": "user", "content": "c"}, {"role": "user", "content": "apagala"}]


def test_build_input_no_history():
    assert build_input("hola", [], 2) == [{"role": "user", "content": "hola"}]
    assert build_input("hola", [{"role": "user", "content": "x"}], 0) == [
        {"role": "user", "content": "hola"}
    ]


def test_build_input_filters_roles():
    hist = [{"role": "system", "content": "s"}, {"role": "tool", "content": "t"}, "basura"]
    assert build_input("hola", hist, 2) == [{"role": "user", "content": "hola"}]


def test_decide_execute_when_confident(cfg, cands):
    d = decide(
        "prendé la luz de la oficina",
        answers(action=("turn_on", 0.9), target=("switch.luz_oficina", 0.88)),
        cands, cfg, True,
    )
    assert d.path == "execute" and d.action == "turn_on"
    assert d.target.entity_id == "switch.luz_oficina"
    assert d.confidences["target"] == 0.88


def test_decide_hint_when_target_low(cfg, cands):
    d = decide("prendé la luz", answers(target=("switch.luz_oficina", 0.6)), cands, cfg, True)
    assert d.path == "hint" and "target" in d.reason


def test_decide_hint_when_action_low(cfg, cands):
    d = decide("prendé la luz", answers(action=("turn_on", 0.5)), cands, cfg, True)
    assert d.path == "hint" and "action" in d.reason


def test_decide_skip_when_not_home(cfg, cands):
    d = decide("hola", answers(is_home=0.1, action=("none", 0.9), target=("none", 0.9)), cands, cfg, True)
    assert d.path == "skip"


def test_decide_skip_when_action_none(cfg, cands):
    d = decide("hola luz", answers(action=("none", 0.9), target=("none", 0.9)), cands, cfg, True)
    assert d.path == "skip"


def test_set_climate_without_target_is_hint_but_extracts_params(cfg, cands):
    d = decide("ponelo en 24 frío", answers(action=("set_climate", 0.95), target=("none", 0.9)), cands, cfg, True)
    assert d.path == "hint" and d.temperature == 24 and d.hvac_mode == "cool"
    assert d.target is None


def test_set_climate_with_climate_target_executes(cfg, cands):
    d = decide("ponelo en 24 frío", answers(action=("set_climate", 0.95), target=("climate.living", 0.9)), cands, cfg, True)
    assert d.path == "execute" and d.temperature == 24 and d.target.entity_id == "climate.living"


def test_set_climate_with_non_climate_target_is_hint(cfg, cands):
    d = decide("ponelo en 24", answers(action=("set_climate", 0.95), target=("switch.luz_oficina", 0.95)), cands, cfg, True)
    assert d.path == "hint"


def test_params_only_for_set_climate(cfg, cands):
    d = decide("prendé la luz 24 frío", answers(), cands, cfg, True)
    assert d.temperature is None and d.hvac_mode is None


def test_aggregate_executes_without_target(cfg, cands):
    d = decide("apagá las luces", answers(action=("lights_off", 0.9), target=("none", 0.9)), cands, cfg, True)
    assert d.path == "execute" and d.target is None


def test_aggregate_executes_with_missing_target_answer(cfg, cands):
    d = decide("apagá todo", answers(action=("all_off", 0.9), target=None), cands, cfg, True)
    assert d.path == "execute"


def test_fast_not_allowed_forces_hint(cfg, cands):
    d = decide("apagá todo", answers(action=("all_off", 0.95), target=("none", 0.9)), cands, cfg, False)
    assert d.path == "hint"


def test_missing_is_home_or_action_skips(cfg, cands):
    full = answers()
    no_home = {k: v for k, v in full.items() if k != "is_home"}
    no_action = {k: v for k, v in full.items() if k != "action"}
    assert decide("luz", no_home, cands, cfg, True).path == "skip"
    assert decide("luz", no_action, cands, cfg, True).path == "skip"
    assert decide("luz", {}, cands, cfg, True).path == "skip"


def test_missing_target_answer_non_aggregate_is_hint(cfg, cands):
    d = decide("prendé la luz", answers(target=None), cands, cfg, True)
    assert d.path == "hint" and d.target is None


def test_unknown_action_skips(cfg, cands):
    d = decide("luz", answers(action=("explotar", 0.99)), cands, cfg, True)
    assert d.path == "skip" and d.reason


def test_unknown_target_becomes_none(cfg, cands):
    d = decide("prendé la luz", answers(target=("light.fantasma", 0.99)), cands, cfg, True)
    assert d.path == "hint" and d.target is None


def test_non_finite_and_non_numeric_values_never_raise(cfg, cands):
    nan, inf = float("nan"), float("inf")
    a = answers()
    a["action"] = Answer("action", "choice", "turn_on", nan)
    a["target"] = Answer("target", "choice", "switch.luz_oficina", inf)
    d = decide("prendé la luz", a, cands, cfg, True)
    assert d.path == "hint" and d.confidences["action"] == 0.0 and d.confidences["target"] == 0.0
    a2 = answers()
    a2["is_home"] = Answer("is_home", "predicate", "mucho", 0.9)
    assert decide("prendé la luz", a2, cands, cfg, True).path == "skip"
    a3 = answers()
    a3["is_home"] = Answer("is_home", "predicate", inf, 0.9)
    assert decide("prendé la luz", a3, cands, cfg, True).path == "skip"
    a4 = answers()
    a4["action"] = Answer("action", "choice", None, "x")
    assert decide("prendé la luz", a4, cands, cfg, True).path == "skip"


# --- final fix 5: lista de candidatos no exacta -> hint ------------------------------

def test_inexact_candidates_force_hint_for_single_action(cfg, cands):
    d = decide("apagá la luz", answers(action=("turn_off", 0.95), target=("switch.luz_oficina", 0.95)),
               cands, cfg, True, targets_exact=False)
    assert d.path == "hint" and "candidatos" in d.reason
    assert d.target.entity_id == "switch.luz_oficina"


def test_inexact_candidates_do_not_affect_aggregates(cfg, cands):
    d = decide("apagá todo", answers(action=("all_off", 0.95), target=("none", 0.9)), cands, cfg, True,
               targets_exact=False)
    assert d.path == "execute"


def test_targets_exact_defaults_true(cfg, cands):
    d = decide("prendé la luz de la oficina", answers(action=("turn_on", 0.95), target=("switch.luz_oficina", 0.95)),
               cands, cfg, True)
    assert d.path == "execute"
