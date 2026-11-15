import asyncio

import httpx
import pytest

from sentinel.alerts import AlertEngine, Debouncer, notify


def test_lifecycle_open_refresh_resolve(db):
    t = {"now": 1000}
    eng = AlertEngine(db, clock=lambda: t["now"])

    assert eng.set_state("k", False) is None  # nothing to resolve
    opened = eng.set_state("k", True, "warning", "system", "high")
    assert opened["event"] == "opened"

    t["now"] = 1010
    assert eng.set_state("k", True, "warning", "system", "still high") is None  # no duplicate notification
    rows = db.list_alerts("open")
    assert len(rows) == 1 and rows[0]["count"] == 2 and rows[0]["message"] == "still high"

    t["now"] = 1020
    resolved = eng.set_state("k", False)
    assert resolved["event"] == "resolved"
    assert db.list_alerts("open") == [] and db.list_alerts("resolved")[0]["resolved_ts"] == 1020


def test_invalid_severity_rejected(db):
    with pytest.raises(ValueError):
        AlertEngine(db).set_state("k", True, "panic")


def test_debouncer_requires_consecutive_hits():
    d = Debouncer()
    assert [d.check("x", c, 3) for c in (True, True, False, True, True, True)] == [False, False, False, False, False, True]


def test_sync_group_opens_and_resolves_disappeared(db):
    eng = AlertEngine(db)
    ev = eng.sync_group("aws:", {"aws:a": {"severity": "warning", "message": "A"},
                                 "aws:b": {"severity": "info", "message": "B"}}, "cloud")
    assert {e["key"] for e in ev} == {"aws:a", "aws:b"}
    ev = eng.sync_group("aws:", {"aws:a": {"severity": "warning", "message": "A"}}, "cloud")
    assert [(e["event"], e["key"]) for e in ev] == [("resolved", "aws:b")]
    assert db.open_alert_keys("aws:") == ["aws:a"]


def test_notify_posts_to_webhook_from_env_and_swallows_errors():
    sent = []

    def handler(req):
        sent.append(req)
        return httpx.Response(200)

    async def go(env):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            await notify([{"event": "opened"}], "HOOK", c, env=env)

    asyncio.run(go({"HOOK": "https://hooks.example.com/abc"}))
    assert len(sent) == 1 and str(sent[0].url) == "https://hooks.example.com/abc"

    asyncio.run(go({}))  # env var not set: silently does nothing
    assert len(sent) == 1

    def failing(req):
        raise httpx.ConnectError("down", request=req)

    async def go_fail():
        async with httpx.AsyncClient(transport=httpx.MockTransport(failing)) as c:
            await notify([{"event": "opened"}], "HOOK", c, env={"HOOK": "https://h.example.com"})

    asyncio.run(go_fail())  # must not raise
