"""SLO + incident wiring in the runtime, and the Prometheus /metrics endpoint."""
import asyncio
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from sentinel.app import create_app
from sentinel.config import parse_config
from sentinel.db import Database
from sentinel.runtime import Runtime


@pytest.fixture
def cfg(tmp_path):
    return parse_config({
        "db_path": str(tmp_path / "o.db"),
        "incidents": {"window_seconds": 300},
        "services": [{"name": "api", "url": "https://user:s3cret@api.example.com/h?token=abc",
                      "interval_seconds": 30, "failures_before_alert": 2,
                      "slo": {"target": 99.9, "window_days": 7}}],
    })


def seed_ok(db, minutes_ago_start=120, minutes_ago_end=10):
    now = int(time.time())
    for ts in range(now - minutes_ago_start * 60, now - minutes_ago_end * 60, 30):
        db.insert_check({"ts": ts, "name": "api", "ok": True, "status_code": 200, "latency_ms": 20.0, "error": None})


def failing_client():
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(503)))


def healthy_client():
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200)))


def test_outage_opens_down_and_slo_alerts_in_one_incident_and_recovers(cfg):
    async def go():
        db = Database(cfg.db_path)
        seed_ok(db)
        rt = Runtime(cfg, db, http=failing_client())
        svc = cfg.services[0]
        await rt.service_tick(svc)
        assert db.list_alerts("open") == []                      # one failure pages nobody
        await rt.service_tick(svc)
        keys = {a["key"] for a in db.list_alerts("open")}
        assert {"svc:api:down", "slo:api:fast_burn"} <= keys

        incidents = rt.incidents.list("open")
        assert len(incidents) == 1
        assert {a["key"] for a in incidents[0]["alerts"]} >= {"svc:api:down", "slo:api:fast_burn"}
        assert incidents[0]["likely_cause"]["key"] == "svc:api:down"   # SLO burn is impact, not cause

        rt.http = healthy_client()
        for _ in range(12):                                       # healthy checks refill the short window
            await rt.service_tick(svc)
        return rt, db

    rt, db = asyncio.run(go())
    assert rt.check_totals[("api", "fail")] == 2 and rt.check_totals[("api", "ok")] == 12
    # the down alert clears at once; the SLO alert needs the short window to be clean,
    # which takes minutes of real time, so it is still open here
    keys = {a["key"] for a in db.list_alerts("open")}
    assert "svc:api:down" not in keys


def families(text):
    return {f.name: f for f in text_string_to_metric_families(text)}


def test_metrics_endpoint_is_valid_prometheus_and_leaks_nothing(cfg):
    async def prime():
        db = Database(cfg.db_path)
        seed_ok(db)
        rt = Runtime(cfg, db, http=failing_client())
        await rt.system_tick()
        for _ in range(2):
            await rt.service_tick(cfg.services[0])
        return rt

    rt = asyncio.run(prime())
    with TestClient(create_app(cfg, start_background=False, runtime=rt)) as c:
        r = c.get("/metrics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    for leaked in ("s3cret", "token=abc", "user:", "api.example.com"):
        assert leaked not in r.text
    fam = families(r.text)                                         # the official parser accepts it
    for name in ("sentinel_cpu_usage_ratio", "sentinel_service_up", "sentinel_slo_error_budget_remaining_ratio",
                 "sentinel_slo_burn_rate", "sentinel_alerts_open", "sentinel_incidents_open"):
        assert name in fam, name

    def val(name, **labels):
        for s in fam[name].samples:
            if all(s.labels.get(k) == v for k, v in labels.items()):
                return s.value
        raise AssertionError(f"{name} {labels} not found")

    assert val("sentinel_service_up", service="api") == 0
    assert val("sentinel_service_checks", service="api", result="fail") == 2
    assert val("sentinel_slo_burn_rate", service="api", window="1h") > 3.36
    assert val("sentinel_alerts_open", severity="critical", source="service") >= 1
    assert val("sentinel_alerts_open", severity="critical", source="slo") >= 1
    assert val("sentinel_incidents_open") == 1
    assert 0 < val("sentinel_slo_error_budget_remaining_ratio", service="api") < 1


def test_metrics_can_be_disabled(tmp_path):
    cfg = parse_config({"db_path": str(tmp_path / "m.db"), "metrics": {"enabled": False}})
    with TestClient(create_app(cfg, start_background=False)) as c:
        assert c.get("/metrics").status_code == 404


def test_registries_are_not_shared_between_apps(tmp_path):
    for i in range(2):                                             # would raise on duplicate registration
        cfg = parse_config({"db_path": str(tmp_path / f"{i}.db")})
        with TestClient(create_app(cfg, start_background=False)) as c:
            assert c.get("/metrics").status_code == 200


def test_slo_and_incident_endpoints(cfg):
    async def prime():
        db = Database(cfg.db_path)
        seed_ok(db)
        rt = Runtime(cfg, db, http=failing_client())
        for _ in range(2):
            await rt.service_tick(cfg.services[0])
        return rt

    rt = asyncio.run(prime())
    with TestClient(create_app(cfg, start_background=False, runtime=rt)) as c:
        slos = c.get("/api/slo").json()["slos"]
        assert slos[0]["service"] == "api" and slos[0]["status"] == "fast_burn"
        listing = c.get("/api/incidents?status=open").json()["incidents"]
        assert len(listing) == 1
        one = c.get(f"/api/incidents/{listing[0]['id']}").json()
        assert one["likely_cause"]["key"] == "svc:api:down" and one["timeline"]
        assert c.get("/api/incidents/9999").status_code == 404
        assert c.get("/api/incidents?status=bogus").status_code == 422
        assert c.get("/api/incidents?limit=0").status_code == 422
        insights = c.get("/api/insights").json()
        assert insights["open_incidents"] == 1 and "incident" in insights["summary"]
        body = c.get("/api/slo").text + c.get("/api/incidents").text
        assert "s3cret" not in body and "token=abc" not in body
