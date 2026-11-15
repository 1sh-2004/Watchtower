import asyncio
from datetime import date

import httpx

from sentinel.collectors.services import key_days_left, parse_rate_limit, run_check
from sentinel.config import ApiKeyMeta, ServiceConfig

SECRET = "sk-super-secret-value"


def check(handler, svc=None, env=None):
    svc = svc or ServiceConfig(name="api", url="https://api.example.com/health")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await run_check(client, svc, env=env or {})

    return asyncio.run(go())


def test_success_records_latency_and_status():
    r = check(lambda req: httpx.Response(200, json={"ok": True}))
    assert r["ok"] and r["status_code"] == 200 and r["latency_ms"] is not None and r["error"] is None


def test_http_error_is_a_failure_with_status():
    r = check(lambda req: httpx.Response(503))
    assert not r["ok"] and r["status_code"] == 503 and "503" in r["error"]


def test_timeout_and_connection_errors_never_raise():
    def boom_timeout(req):
        raise httpx.ReadTimeout("slow", request=req)

    def boom_connect(req):
        raise httpx.ConnectError("refused", request=req)

    assert check(boom_timeout)["error"] == "timeout"
    assert check(boom_connect)["error"] == "connection failed"


def test_rate_limit_headers_parsed():
    r = check(lambda req: httpx.Response(200, headers={"X-RateLimit-Limit": "1000", "X-RateLimit-Remaining": "37",
                                                       "X-RateLimit-Reset": "3600"}))
    assert (r["rl_limit"], r["rl_remaining"], r["rl_reset"]) == (1000, 37, 3600)
    assert parse_rate_limit({"RateLimit-Remaining": "5"})["rl_remaining"] == 5
    assert parse_rate_limit({"x-ratelimit-remaining": "abc"})["rl_remaining"] is None


def test_api_key_sent_but_never_returned():
    seen = {}

    def handler(req):
        seen["auth"] = req.headers.get("authorization")
        return httpx.Response(200)

    svc = ServiceConfig(name="api", url="https://api.example.com/h", api_key=ApiKeyMeta(env="K"))
    r = check(handler, svc, env={"K": SECRET})
    assert seen["auth"] == f"Bearer {SECRET}"
    assert SECRET not in repr(r)  # the secret must not appear anywhere in the result


def test_missing_key_env_is_noted_not_fatal():
    svc = ServiceConfig(name="api", url="https://api.example.com/h", api_key=ApiKeyMeta(env="MISSING"))
    r = check(lambda req: httpx.Response(200), svc, env={})
    assert r["ok"] and "MISSING" in r["note"]


def test_key_days_left():
    today = date(2027, 1, 1)
    assert key_days_left(date(2027, 1, 11), today) == 10
    assert key_days_left(date(2026, 12, 30), today) == -2
    assert key_days_left(None, today) is None
