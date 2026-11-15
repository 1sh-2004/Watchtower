from datetime import date

import pytest

from sentinel.config import ConfigError, load_config, parse_config


def test_defaults():
    cfg = parse_config(None)
    assert cfg.thresholds.cpu == 80 and cfg.thresholds.ram == 85 and cfg.thresholds.disk == 90
    assert cfg.services == [] and cfg.aws.enabled is False
    assert cfg.ai.llm_summary is False  # AI features are opt-in


def test_service_and_key_metadata_parse():
    cfg = parse_config({"services": [{
        "name": "api", "url": "https://example.com/health", "expected_status": 204,
        "api_key": {"env": "MY_KEY", "expires_on": "2027-03-01"},
    }]})
    svc = cfg.services[0]
    assert svc.expected_status == [204]
    assert svc.api_key.env == "MY_KEY" and svc.api_key.expires_on == date(2027, 3, 1)


def test_literal_key_in_config_is_rejected():
    with pytest.raises(ConfigError, match="never put a key"):
        parse_config({"services": [{"name": "a", "url": "https://x.io", "api_key": {"value": "sk-123"}}]})


def test_duplicate_names_and_bad_urls_rejected():
    with pytest.raises(ConfigError, match="duplicate"):
        parse_config({"services": [{"name": "a", "url": "https://x.io"}, {"name": "a", "url": "https://y.io"}]})
    with pytest.raises(ConfigError, match="http"):
        parse_config({"services": [{"name": "a", "url": "ftp://x.io"}]})


def test_aws_enabled_requires_regions():
    with pytest.raises(ConfigError, match="regions"):
        parse_config({"aws": {"enabled": True}})


def test_bad_expiry_date_and_numbers():
    with pytest.raises(ConfigError, match="YYYY-MM-DD"):
        parse_config({"services": [{"name": "a", "url": "https://x.io", "api_key": {"expires_on": "soon"}}]})
    with pytest.raises(ConfigError):
        parse_config({"thresholds": {"cpu": -5}})


def test_load_config_missing_explicit_path(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(str(tmp_path / "nope.yaml"))


def test_db_path_env_override(monkeypatch):
    monkeypatch.setenv("SENTINEL_DB_PATH", "/data/x.db")
    assert parse_config({"db_path": "ignored.db"}).db_path == "/data/x.db"
