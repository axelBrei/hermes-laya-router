import logging
from pathlib import Path

from laya_router.config import load_config, missing_env, resolve_hermes_home


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


def test_missing_env_custom_names_and_empty_values():
    cfg = load_config(Path("/nonexistent"))
    cfg.bifrost_url_env, cfg.bifrost_api_key_env = "U", "K"
    env = {"HASS_URL": "", "HASS_TOKEN": "t", "U": "http://bf", "LAYA_BIFROST_URL": "http://otro"}
    assert missing_env(cfg, env) == ["HASS_URL", "K"]
    assert missing_env(cfg, {"HASS_URL": "a", "HASS_TOKEN": "b", "U": "c", "K": "d"}) == []


def test_old_bifrost_url_key_is_unknown_and_not_honored(tmp_path, caplog):
    (tmp_path / "laya-router").mkdir()
    (tmp_path / "laya-router" / "config.yaml").write_text("bifrost_url: http://viejo\n")
    with caplog.at_level(logging.WARNING):
        cfg = load_config(tmp_path)
    assert not hasattr(cfg, "bifrost_url")
    assert "bifrost_url" in caplog.text and "desconocida" in caplog.text


def test_resolve_hermes_home_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert resolve_hermes_home() == tmp_path


def test_all_defaults(tmp_path):
    cfg = load_config(tmp_path)
    assert cfg.bifrost_url_env == "LAYA_BIFROST_URL"
    assert cfg.bifrost_api_key_env == "LAYA_BIFROST_API_KEY"
    assert not hasattr(cfg, "bifrost_url")
    assert (cfg.laya_timeout_s, cfg.ha_timeout_s) == (6.0, 5.0)
    assert cfg.thresholds.is_home == 0.5
    assert cfg.domains == ["light", "switch", "climate"]
    assert cfg.exclude_from_all_off == [] and cfg.allowed_senders == [] and cfg.aliases == {}
    assert cfg.cache_refresh_minutes == 15 and cfg.history_turns == 2
    assert cfg.prefilter_lexicon == ["prend", "apag", "enciend", "luz", "luces", "aire", "clima",
                                     "temperatura", "frío", "frio", "calor", "grados", "todo"]
    assert cfg.max_message_chars == 300 and cfg.dry_run is False
    assert cfg.verify_budget_s == 3.0 and cfg.verify_poll_ms == 400


def test_unknown_keys_warn_and_ignored(tmp_path, caplog):
    (tmp_path / "laya-router").mkdir()
    (tmp_path / "laya-router" / "config.yaml").write_text("foo: 1\nthresholds:\n  bar: 2\n")
    with caplog.at_level(logging.WARNING):
        cfg = load_config(tmp_path)
    assert not hasattr(cfg, "foo")
    assert "foo" in caplog.text


def test_defaults_not_shared_between_loads(tmp_path):
    a = load_config(tmp_path)
    a.domains.append("fan")
    assert load_config(tmp_path).domains == ["light", "switch", "climate"]


def test_load_error_false_when_ok_or_missing(tmp_path):
    assert load_config(tmp_path).load_error is False
    (tmp_path / "laya-router").mkdir()
    (tmp_path / "laya-router" / "config.yaml").write_text("dry_run: true\n")
    assert load_config(tmp_path).load_error is False
    (tmp_path / "laya-router" / "config.yaml").write_text("")
    assert load_config(tmp_path).load_error is False


def test_load_error_on_invalid_yaml(tmp_path):
    (tmp_path / "laya-router").mkdir()
    (tmp_path / "laya-router" / "config.yaml").write_text("allowed_senders: [telegram:1\n  dry_run: : :\n")
    cfg = load_config(tmp_path)
    assert cfg.load_error is True and cfg.allowed_senders == []


def test_load_error_when_not_mapping(tmp_path):
    (tmp_path / "laya-router").mkdir()
    (tmp_path / "laya-router" / "config.yaml").write_text("- a\n- b\n")
    assert load_config(tmp_path).load_error is True


def test_load_error_when_unreadable(tmp_path):
    (tmp_path / "laya-router" / "config.yaml").mkdir(parents=True)  # existe pero no es legible como archivo
    cfg = load_config(tmp_path)
    assert cfg.load_error is True
