import asyncio
import json
import random

import httpx

from sentinel.aiops import (detect_anomalies, forecast_full, llm_summary, minimized_facts, robust_zscore,
                            summarize_rules)
from sentinel.config import AiConfig


def noisy(base, n, jitter=2.0, seed=1):
    rnd = random.Random(seed)
    return [base + rnd.uniform(-jitter, jitter) for _ in range(n)]


def test_zscore_flags_spike_but_not_noise():
    steady = noisy(20, 100)
    assert abs(robust_zscore(steady)["z"]) < 3.5
    spike = steady + [95.0]
    assert robust_zscore(spike)["z"] > 10


def test_zscore_needs_enough_data_and_handles_flat_baseline():
    assert robust_zscore([10] * 5) is None
    flat = [10.0] * 60 + [10.5]
    assert abs(robust_zscore(flat)["z"]) < 1  # min_scale stops a flat baseline exploding


def test_detect_anomalies_requires_material_change():
    hist = {"cpu": noisy(20, 100) + [95.0], "ram": [50.0] * 60 + [53.0]}
    found = detect_anomalies(hist)
    assert [a["metric"] for a in found] == ["cpu"]  # RAM shifted only 3 points: ignored
    assert found[0]["direction"] == "above"


def test_forecast_linear_growth():
    ts = [i * 3600 for i in range(48)]
    vals = [50 + i * 0.5 for i in range(48)]  # +0.5 pts/hour, ends at 73.5
    f = forecast_full(ts, vals)
    assert f["r2"] > 0.99
    assert abs(f["days_to_full"] - (100 - 73.5) / 0.5 / 24) < 0.2


def test_forecast_declines_noise_flat_and_decreasing():
    rnd = random.Random(7)
    ts = [i * 3600 for i in range(48)]
    assert forecast_full(ts, [rnd.uniform(40, 60) for _ in ts]) is None  # poor fit
    assert forecast_full(ts, [50.0] * 48) is None
    assert forecast_full(ts, [90 - i for i in range(48)]) is None
    assert forecast_full(ts[:5], [1, 2, 3, 4, 5]) is None  # too little data


CTX = {
    "open_alerts": [
        {"severity": "critical", "source": "service", "message": "payments-prod is failing (timeout)"},
        {"severity": "warning", "source": "cloud", "message": "EBS volume vol-0secret is unattached"},
    ],
    "services": [{"name": "payments-prod", "ok": False}, {"name": "docs", "ok": True}],
    "system": {"cpu": {"percent": 41.0}, "ram": {"percent": 60.0}, "disk": {"percent": 71.0}},
    "anomalies": [{"metric": "cpu", "z": 6.1, "direction": "above", "message": "CPU is 95% vs typical 20%"}],
    "forecasts": [{"metric": "disk", "days_to_full": 12.5}],
    "cloud": {"cost": {"month_to_date_usd": 36.0, "budget_usd": 50.0, "budget_used_percent": 72.0}},
}


def test_rule_summary_covers_key_facts():
    s = summarize_rules(CTX)
    assert "2 open alert(s), 1 critical" in s and "1/2 services up" in s and "12.5 day" in s and "72.0%" in s
    assert "healthy" in summarize_rules({"open_alerts": []})


def test_minimized_facts_contain_no_names_or_messages():
    blob = json.dumps(minimized_facts(CTX))
    for leaked in ("payments-prod", "docs", "vol-0secret", "unattached", "failing"):
        assert leaked not in blob
    assert minimized_facts(CTX)["services_down"] == 1


def run_llm(ai, env, handler):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await llm_summary(CTX, ai, c, env=env)
    return asyncio.run(go())


def test_llm_disabled_or_unconfigured_makes_no_request():
    def must_not_call(req):
        raise AssertionError("no request expected")

    assert run_llm(AiConfig(llm_summary=False), {"ANTHROPIC_API_KEY": "k"}, must_not_call) is None
    assert run_llm(AiConfig(llm_summary=True), {}, must_not_call) is None


def test_llm_request_is_minimised_and_response_parsed():
    seen = {}

    def handler(req):
        seen["body"] = req.content.decode()
        seen["key"] = req.headers["x-api-key"]
        return httpx.Response(200, json={"content": [{"type": "text", "text": "One service is down."}]})

    out = run_llm(AiConfig(llm_summary=True), {"ANTHROPIC_API_KEY": "test-key"}, handler)
    assert out == "One service is down." and seen["key"] == "test-key"
    for leaked in ("payments-prod", "vol-0secret", "unattached"):
        assert leaked not in seen["body"]


def test_llm_failure_falls_back_to_none():
    assert run_llm(AiConfig(llm_summary=True), {"ANTHROPIC_API_KEY": "k"}, lambda r: httpx.Response(500)) is None
    assert run_llm(AiConfig(llm_summary=True), {"ANTHROPIC_API_KEY": "k"},
                   lambda r: httpx.Response(200, text="not json")) is None
