"""Settings tab: keys entered in the dashboard, localhost-only guard, AWS hot-swap, persistence."""
import json
import os
import stat
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from sentinel.app import create_app
from sentinel.collectors.aws import describe_error
from sentinel.config import ConfigError, parse_config
from sentinel.db import Database
from sentinel.runtime import Runtime
from sentinel.settings import SettingsStore, build_ui_service

SECRET = "sk-live-SUPERSECRET-123"
AWS_ID = "AKIAIOSFODNN7EXAMPLE"
AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


def as_client(app, ip):
    """Make requests appear to come from `ip`. Wraps the ASGI app, because older Starlette
    versions have no TestClient(client=...) option."""
    async def wrapper(scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            scope = {**scope, "client": (ip, 5000)}
        await app(scope, receive, send)
    return wrapper


def make_client(app, ip="127.0.0.1", host="localhost"):
    return TestClient(as_client(app, ip), base_url=f"http://{host}")


def key_protected_api(request: httpx.Request) -> httpx.Response:
    """A fake API: 200 with the right bearer token (and rate-limit headers), 401 without."""
    if request.headers.get("authorization") == f"Bearer {SECRET}":
        return httpx.Response(200, headers={"x-ratelimit-limit": "100", "x-ratelimit-remaining": "42"}, json={"ok": 1})
    return httpx.Response(401)


class FakeClient:
    """Minimal boto3-style client; `fail` maps a method name to an exception to raise."""
    def __init__(self, service, region, fail):
        self.service, self.region, self.fail = service, region, fail

    def _maybe(self, name):
        if name in self.fail:
            raise self.fail[name]

    def get_caller_identity(self):
        self._maybe("get_caller_identity")
        return {"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/sentinel"}

    def get_paginator(self, name):
        self._maybe("describe_instances")
        insts = [{"InstanceId": "i-1", "State": {"Name": "running"}, "InstanceType": "t3.micro",
                  "Tags": [{"Key": "Name", "Value": "web"}]}]

        class P:
            def paginate(self_inner):
                return iter([{"Reservations": [{"Instances": insts}]}])
        return P()

    def describe_instances(self, **_):
        self._maybe("describe_instances")
        return {"Reservations": []}

    def describe_regions(self):
        self._maybe("describe_regions")
        return {"Regions": [{"RegionName": self.region}]}

    def describe_volumes(self, **_):
        return {"Volumes": []}

    def describe_addresses(self):
        return {"Addresses": []}

    def list_buckets(self):
        self._maybe("list_buckets")
        return {"Buckets": []}

    def get_metric_statistics(self, **_):
        self._maybe("get_metric_statistics")
        return {"Datapoints": [{"Average": 40.0}]}

    def get_cost_and_usage(self, **_):
        self._maybe("get_cost_and_usage")
        return {"ResultsByTime": [{"Groups": [{"Keys": ["Amazon EC2"], "Metrics": {"UnblendedCost": {"Amount": "12.34"}}}]}]}

    def get_cost_forecast(self, **_):
        return {"Total": {"Amount": "20"}}


def make_factory(fail=None, seen=None):
    def factory(service, region=None):
        if seen is not None:
            seen.append(service)
        return FakeClient(service, region, fail or {})
    return factory


@pytest.fixture
def parts(tmp_path):
    cfg = parse_config({"db_path": str(tmp_path / "s.db"),
                        "services": [{"name": "yaml-svc", "url": "https://example.com"}]})
    db = Database(cfg.db_path)
    http = httpx.AsyncClient(transport=httpx.MockTransport(key_protected_api))
    store = SettingsStore(str(tmp_path / "settings.json"))
    rt = Runtime(cfg, db, http=http, settings=store, aws_client_factory=make_factory())
    return cfg, rt, store


@pytest.fixture
def client(parts):
    cfg, rt, _ = parts
    with make_client(create_app(cfg, runtime=rt, start_background=True)) as c:
        yield c


def wait_for(fn, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        val = fn()
        if val:
            return val
        time.sleep(0.05)
    raise AssertionError("timed out waiting for background work")


SERVICE = {"name": "Payments", "url": "https://pay.example.com/health", "interval_seconds": 1,
           "api_key": {"value": SECRET, "header": "Authorization", "scheme": "Bearer"}}


# ---- validation ---------------------------------------------------------------------
def test_build_ui_service_splits_secret_off_the_config():
    svc, secret = build_ui_service(SERVICE, 7)
    assert secret == SECRET and svc.source == "ui"
    assert SECRET not in repr(svc)
    assert svc.api_key.env == "ui:Payments"


@pytest.mark.parametrize("patch,msg", [
    ({"name": "a:b"}, "cannot contain"),
    ({"name": ""}, "name is required"),
    ({"url": "ftp://x"}, "http"),
    ({"api_key": {"value": "x", "header": "Bad Header"}}, "header"),
    ({"api_key": {"value": "abc\ndef"}}, "control characters"),
])
def test_build_ui_service_rejects_bad_input(patch, msg):
    with pytest.raises(ConfigError, match=msg):
        build_ui_service({**SERVICE, **patch}, 7)


def test_scheme_can_be_blank_for_raw_key_headers():
    svc, _ = build_ui_service({**SERVICE, "api_key": {"value": SECRET, "header": "X-Api-Key", "scheme": ""}}, 7)
    assert svc.api_key.scheme is None


# ---- the settings file ---------------------------------------------------------------
def test_store_writes_owner_only_and_survives_corruption(tmp_path):
    store = SettingsStore(str(tmp_path / "x" / "settings.json"))
    store.save({"services": [{"name": "a"}], "aws": None})
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    assert store.load()["services"] == [{"name": "a"}]
    store.path.write_text("{ not json")
    assert store.load()["services"] == []


# ---- localhost-only guard -------------------------------------------------------------
def test_settings_refused_from_other_machines(parts):
    cfg, rt, _ = parts
    app = create_app(cfg, runtime=rt, start_background=False)
    for kwargs, label in (
        (dict(ip="203.0.113.9"), "remote client"),
        (dict(host="evil.example.com"), "dns rebinding host"),
    ):
        with make_client(app, **kwargs) as c:
            assert c.get("/api/settings").status_code == 403, label
            assert c.put("/api/settings/services", json=SERVICE).status_code == 403, label
    with make_client(app) as c:
        assert c.get("/api/settings", headers={"Origin": "https://evil.example.com"}).status_code == 403
        assert c.get("/api/settings", headers={"Origin": "http://localhost:8000"}).status_code == 200


def test_docker_trust_flag_allows_bridge_address(parts, monkeypatch):
    cfg, rt, _ = parts
    app = create_app(cfg, runtime=rt, start_background=False)
    with make_client(app, ip="172.17.0.1") as c:
        assert c.get("/api/settings").status_code == 403
    monkeypatch.setenv("SENTINEL_SETTINGS_TRUST_NETWORK", "1")
    with make_client(app, ip="172.17.0.1") as c:
        assert c.get("/api/settings").status_code == 200


def test_rest_of_dashboard_still_works_from_anywhere(parts):
    cfg, rt, _ = parts
    with make_client(create_app(cfg, runtime=rt, start_background=False), ip="10.0.0.5", host="anything.lan") as c:
        assert c.get("/api/health").status_code == 200


# ---- adding an API with a key ------------------------------------------------------------
def test_key_entered_in_dashboard_is_sent_and_gives_readings(client, parts):
    _, _, store = parts
    # Without the key the API says 401: the test endpoint reports that.
    r = client.post("/api/settings/test/service", json={**SERVICE, "api_key": {**SERVICE["api_key"], "value": "wrong"}})
    assert r.status_code == 200 and r.json()["ok"] is False and r.json()["status_code"] == 401
    # With the right key the same test passes and surfaces the quota headers.
    good = client.post("/api/settings/test/service", json=SERVICE).json()
    assert good["ok"] is True and good["rl_remaining"] == 42 and good["rl_limit"] == 100

    assert client.put("/api/settings/services", json=SERVICE).status_code == 200
    row = wait_for(lambda: next((s for s in client.get("/api/services").json()["services"]
                                 if s["name"] == "Payments" and s["latest"]), None))
    assert row["latest"]["ok"] is True and row["latest"]["rl_remaining"] == 42
    assert row["key_set"] is True and row["source"] == "ui"
    assert row["stats_24h"]["checks"] >= 1
    # The unauthenticated YAML service is untouched by all of this.
    assert any(s["name"] == "yaml-svc" for s in client.get("/api/services").json()["services"])


def test_key_never_appears_in_any_api_response(client):
    client.put("/api/settings/services", json=SERVICE)
    client.put("/api/settings/aws", json={"regions": ["ap-south-1"], "access_key_id": AWS_ID,
                                          "secret_access_key": AWS_SECRET})
    wait_for(lambda: client.get("/api/cloud/aws").json().get("instances"))
    bodies = [client.get(u).text for u in ("/api/settings", "/api/services", "/api/cloud/aws", "/api/alerts",
                                           "/api/insights", "/api/health", "/metrics")]
    bodies.append(client.post("/api/settings/test/service", json=SERVICE).text)
    bodies.append(client.post("/api/settings/test/aws", json={"regions": ["ap-south-1"], "access_key_id": AWS_ID,
                                                              "secret_access_key": AWS_SECRET}).text)
    # A failed validation must not echo the secret back either.
    bodies.append(client.put("/api/settings/services", json={**SERVICE, "name": "a:b"}).text)
    for body in bodies:
        assert SECRET not in body and AWS_SECRET not in body and AWS_ID not in body


def test_service_saved_with_key_survives_restart(client, parts, tmp_path):
    cfg, rt, store = parts
    client.put("/api/settings/services", json=SERVICE)
    mode = stat.S_IMODE(os.stat(store.path).st_mode)
    assert mode == 0o600
    # a brand-new runtime on the same settings file picks the service and key back up
    cfg2 = parse_config({"db_path": str(tmp_path / "s2.db")})
    rt2 = Runtime(cfg2, Database(cfg2.db_path), settings=store)
    rt2.load_saved()
    svc = next(s for s in cfg2.services if s.name == "Payments")
    assert rt2.key_state(svc) is True and rt2.env().get("ui:Payments") == SECRET


def test_editing_keeps_key_and_url_when_left_blank(client, parts):
    _, rt, _ = parts
    client.put("/api/settings/services", json=SERVICE)
    r = client.put("/api/settings/services", json={"name": "Payments", "url": "", "interval_seconds": 5,
                                                   "api_key": {"value": None, "header": "Authorization"}})
    assert r.status_code == 200 and r.json()["interval_seconds"] == 5 and r.json()["key_set"] is True
    svc = next(s for s in rt.cfg.services if s.name == "Payments")
    assert svc.url == SERVICE["url"] and rt.secrets["ui:Payments"] == SECRET


def test_removing_the_key_checkbox_deletes_the_stored_key(client, parts):
    _, rt, _ = parts
    client.put("/api/settings/services", json=SERVICE)
    client.put("/api/settings/services", json={"name": "Payments", "url": "", "interval_seconds": 5})
    assert "ui:Payments" not in rt.secrets


def test_yaml_services_cannot_be_changed_from_the_ui(client):
    r = client.put("/api/settings/services", json={"name": "yaml-svc", "url": "https://other.example.com"})
    assert r.status_code == 400 and "sentinel.yaml" in r.json()["detail"]
    assert client.delete("/api/settings/services/yaml-svc").status_code == 400
    assert client.delete("/api/settings/services/nope").status_code == 404


def test_removing_a_service_closes_its_alerts_and_forgets_the_key(client, parts):
    _, rt, store = parts
    client.put("/api/settings/services", json={"name": "Gone", "url": "https://gone.example.com",
                                               "failures_before_alert": 1, "interval_seconds": 1})
    wait_for(lambda: rt.db.get_open_alert("svc:Gone:down"))      # unauthenticated -> 401 -> down alert
    assert client.delete("/api/settings/services/Gone").status_code == 200
    assert rt.db.get_open_alert("svc:Gone:down") is None
    assert all(s.name != "Gone" for s in rt.cfg.services)
    assert "Gone" not in json.dumps(store.load())


# ---- AWS from the dashboard ---------------------------------------------------------------------
def test_aws_off_by_default_makes_no_calls_and_says_so(parts):
    cfg, rt, _ = parts
    seen = []
    rt._aws_client_factory = make_factory(seen=seen)
    with make_client(create_app(cfg, runtime=rt, start_background=True)) as c:
        time.sleep(0.3)
        assert c.get("/api/cloud/aws").json() == {"enabled": False}
        assert seen == [] and rt.aws is None


def test_aws_enabled_from_dashboard_collects_and_can_be_turned_off(client, parts):
    _, rt, store = parts
    assert client.get("/api/cloud/aws").json() == {"enabled": False}
    r = client.put("/api/settings/aws", json={"regions": ["ap-south-1"], "monthly_budget_usd": 10,
                                              "access_key_id": AWS_ID, "secret_access_key": AWS_SECRET})
    assert r.status_code == 200 and r.json()["key_id_hint"] == "…MPLE"
    data = wait_for(lambda: (d := client.get("/api/cloud/aws").json()).get("instances") and d)
    assert data["instances"][0]["name"] == "web" and data["instances"][0]["avg_cpu_percent"] == 40.0
    assert data["cost"]["month_to_date_usd"] == 12.34
    assert rt.db.get_open_alert("aws:budget")                     # 12.34 > $10 budget -> critical finding
    assert store.load()["aws"]["secret_access_key"] == AWS_SECRET  # persisted for restarts

    # blank keys on a later save keep the stored ones
    client.put("/api/settings/aws", json={"regions": ["ap-south-1"], "monthly_budget_usd": 500})
    assert rt.cfg.aws.access_key_id == AWS_ID and rt.cfg.aws.monthly_budget_usd == 500

    # turning it off stops collection and closes AWS alerts
    client.put("/api/settings/aws", json={"enabled": False})
    assert client.get("/api/cloud/aws").json() == {"enabled": False}
    assert rt.db.get_open_alert("aws:budget") is None and rt.aws is None


def test_aws_reset_forgets_keys_and_returns_to_yaml(client, parts):
    _, rt, store = parts
    client.put("/api/settings/aws", json={"regions": ["ap-south-1"], "access_key_id": AWS_ID,
                                          "secret_access_key": AWS_SECRET})
    client.delete("/api/settings/aws")
    assert rt.cfg.aws.enabled is False and rt.cfg.aws.access_key_id is None
    assert store.load()["aws"] is None


def test_aws_input_validation(client):
    bad = [{"regions": []}, {"regions": ["mars-1"]}, {"regions": ["ap-south-1"], "access_key_id": AWS_ID},
           {"regions": ["ap-south-1"], "access_key_id": "short", "secret_access_key": "x"}]
    for body in bad:
        r = client.put("/api/settings/aws", json=body)
        assert r.status_code == 400, body


def test_aws_connection_test_reports_each_permission(client, parts):
    _, rt, _ = parts
    body = {"regions": ["ap-south-1"], "access_key_id": AWS_ID, "secret_access_key": AWS_SECRET}
    ok = client.post("/api/settings/test/aws", json=body).json()
    assert ok["ok"] is True and ok["account_hint"] == "…9012" and len(ok["checks"]) == 5

    class Denied(Exception):
        response = {"Error": {"Code": "UnauthorizedOperation", "Message": "arn:aws:iam::123456789012:user/x secret"}}

    rt._aws_client_factory = make_factory(fail={"list_buckets": Denied()})
    res = client.post("/api/settings/test/aws", json=body).json()
    assert res["ok"] is False
    s3 = next(c for c in res["checks"] if c["name"] == "S3 buckets")
    assert s3["error"] == "Denied: UnauthorizedOperation" and "arn:" not in json.dumps(res)

    rt._aws_client_factory = make_factory(fail={"get_caller_identity": Denied()})
    res = client.post("/api/settings/test/aws", json=body).json()
    assert res["ok"] is False and res["account_hint"] is None and len(res["checks"]) == 1


def test_describe_error_never_includes_messages():
    class E(Exception):
        response = {"Error": {"Code": "AccessDenied", "Message": "User arn:aws:iam::1234:user/bob"}}

    assert describe_error(E("User arn:aws:iam::1234:user/bob")) == "E: AccessDenied"
    assert describe_error(ValueError("secret")) == "ValueError"
    E.response = {"Error": {"Code": "bad code <script>"}}
    assert describe_error(E()) == "E"


def test_aws_collection_errors_become_a_readable_alert(parts):
    cfg, rt, _ = parts

    class Denied(Exception):
        response = {"Error": {"Code": "AccessDenied"}}

    rt._aws_client_factory = make_factory(fail={"get_cost_and_usage": Denied()})
    with make_client(create_app(cfg, runtime=rt, start_background=True)) as c:
        c.put("/api/settings/aws", json={"regions": ["ap-south-1"]})
        data = wait_for(lambda: (d := c.get("/api/cloud/aws").json()).get("errors") and d)
        assert data["errors"] == ["cost: Denied: AccessDenied"] and data["instances"]  # EC2 still reported