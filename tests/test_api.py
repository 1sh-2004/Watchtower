import asyncio

import pytest
from fastapi.testclient import TestClient

from sentinel.app import create_app
from sentinel.config import parse_config
from sentinel.runtime import Runtime


@pytest.fixture
def cfg(tmp_path):
    return parse_config({
        "db_path": str(tmp_path / "api.db"),
        "services": [{"name": "svc", "url": "https://user:tok3n@api.example.com/health?token=abc",
                      "failures_before_alert": 2, "latency_warn_ms": 100,
                      "api_key": {"env": "K", "expires_on": "2000-01-01"}}],
    })


@pytest.fixture
def client(cfg):
    with TestClient(create_app(cfg, start_background=False)) as c:
        yield c


def test_health_and_security_headers(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["database"] is True
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"


def test_system_endpoints(client):
    cur = client.get("/api/system/current").json()
    for key in ("cpu", "ram", "disk", "network", "gpus", "uptime", "alerts"):
        assert key in cur
    assert "processes" in client.get("/api/system/processes").json()
    h = client.get("/api/system/history?hours=1").json()
    assert "timestamps" in h and "disk_read_bps" in h and "net_down_bps" in h


def test_input_validation(client):
    assert client.get("/api/system/history?hours=0").status_code == 422
    assert client.get("/api/system/history?hours=9999").status_code == 422
    assert client.get("/api/alerts?status=bogus").status_code == 422
    assert client.get("/api/services/nope/latency").status_code == 404


def test_services_endpoint_never_leaks_url_credentials(client):
    body = client.get("/api/services").text
    for leaked in ("tok3n", "token=abc", "user:"):
        assert leaked not in body
    assert "api.example.com" in body  # host only


def test_aws_disabled_by_default(client):
    assert client.get("/api/cloud/aws").json() == {"enabled": False}


def test_insights_rule_based_when_llm_off(client):
    data = client.get("/api/insights").json()
    assert data["summary_source"] == "rules" and data["llm_enabled"] is False
    assert "healthy" in data["summary"]


def test_service_down_alert_lifecycle_and_key_expiry(cfg):
    rt = Runtime(cfg, __import__("sentinel.db", fromlist=["Database"]).Database(cfg.db_path))
    svc = cfg.services[0]
    bad = {"name": "svc", "ok": False, "error": "timeout", "latency_ms": None, "key_days_left": -30,
           "rl_limit": 100, "rl_remaining": 5}
    good = {"name": "svc", "ok": True, "error": None, "latency_ms": 10.0, "key_days_left": -30,
            "rl_limit": 100, "rl_remaining": 90}

    ev1 = rt.evaluate_service(svc, bad)
    keys1 = {e["key"] for e in ev1}
    assert "svc:svc:down" not in keys1  # one failure is not enough (failures_before_alert=2)
    assert {"key:svc:expiry", "svc:svc:quota"} <= keys1

    ev2 = rt.evaluate_service(svc, bad)
    assert [e["key"] for e in ev2] == ["svc:svc:down"] and ev2[0]["severity"] == "critical"

    ev3 = rt.evaluate_service(svc, good)
    assert {(e["key"], e["event"]) for e in ev3} == {("svc:svc:down", "resolved"), ("svc:svc:quota", "resolved")}
    open_keys = {a["key"] for a in rt.db.list_alerts("open")}
    assert open_keys == {"key:svc:expiry"}  # expired key keeps alerting until rotated


def test_system_alert_needs_sustained_breach(cfg):
    from sentinel.db import Database
    rt = Runtime(cfg, Database(cfg.db_path))
    hot = {"cpu": {"percent": 95.0}, "ram": {"percent": 10.0}, "disk": {"percent": 10.0}}
    cool = {"cpu": {"percent": 10.0}, "ram": {"percent": 10.0}, "disk": {"percent": 10.0}}
    assert rt.evaluate_system(hot) == [] and rt.evaluate_system(hot) == []  # 2 samples: still quiet
    ev = rt.evaluate_system(hot)
    assert [e["key"] for e in ev] == ["sys:cpu"] and ev[0]["severity"] == "critical"
    # resolving needs resolve_samples (3) calm samples in a row, not just one
    assert rt.evaluate_system(cool) == [] and rt.evaluate_system(cool) == []
    assert [e["event"] for e in rt.evaluate_system(cool)] == ["resolved"]


def test_background_loops_collect_and_stop_cleanly(cfg):
    from sentinel.db import Database

    async def go():
        rt = Runtime(cfg, Database(cfg.db_path))
        await rt.system_tick()
        assert rt.latest_system and rt.latest_processes
        assert rt.db.system_history(1)["timestamps"]
        await rt.start()
        await asyncio.sleep(0.2)
        await rt.stop()
        return rt

    rt = asyncio.run(go())
    assert rt._tasks == []
