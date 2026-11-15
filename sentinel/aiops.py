"""AIOps: statistical anomaly detection, capacity forecasting and incident summaries.

Design principles (responsible AI):
  * Statistics first. Anomalies and forecasts are plain, explainable maths
    (robust z-score, least-squares trend), not a black box.
  * The LLM summary is opt-in, off by default, advisory only, and receives
    minimised data: counts and numbers, never resource names, hostnames,
    alert text or secrets. If it fails, the rule-based summary is used.
"""
from __future__ import annotations

import os
import statistics
from typing import Any, Dict, List, Mapping, Optional, Sequence

import httpx

from .config import AiConfig
from .incidents import SEVERITY_RANK


# ---- anomaly detection -------------------------------------------------------
def robust_zscore(values: Sequence[Optional[float]], window: int = 120, min_samples: int = 20,
                  min_scale: float = 1.0) -> Optional[Dict[str, float]]:
    """Compare the latest value with the median of the preceding window.

    Uses median and MAD (median absolute deviation), which a handful of past
    spikes cannot distort the way mean/stdev can. `min_scale` stops a perfectly
    flat baseline from turning tiny wobbles into huge z-scores.
    """
    clean = [v for v in values if v is not None]
    if len(clean) < min_samples + 1:
        return None
    latest = clean[-1]
    baseline = clean[-(window + 1):-1]
    median = statistics.median(baseline)
    mad = statistics.median(abs(v - median) for v in baseline)
    scale = max(1.4826 * mad, min_scale)
    return {"latest": latest, "baseline": round(median, 2), "z": round((latest - median) / scale, 2)}


def detect_anomalies(history: Mapping[str, Sequence[Optional[float]]], z_threshold: float = 3.5,
                     min_delta: float = 15.0) -> List[Dict[str, Any]]:
    """Flag percent-based metrics whose latest value is statistically unusual
    AND at least `min_delta` points away from normal (so tiny shifts are ignored)."""
    out = []
    for metric in ("cpu", "ram", "gpu_util"):
        res = robust_zscore(history.get(metric, []))
        if not res:
            continue
        if abs(res["z"]) >= z_threshold and abs(res["latest"] - res["baseline"]) >= min_delta:
            out.append({
                "metric": metric, "latest": round(res["latest"], 1), "baseline": res["baseline"], "z": res["z"],
                "direction": "above" if res["z"] > 0 else "below",
                "message": (f"{metric.upper()} is {round(res['latest'], 1)}% vs a typical {res['baseline']}% "
                            f"(z={res['z']})"),
            })
    return out


# ---- capacity forecast -------------------------------------------------------
def forecast_full(timestamps: Sequence[float], values: Sequence[Optional[float]], limit: float = 100.0,
                  min_points: int = 12, max_days: float = 365.0) -> Optional[Dict[str, float]]:
    """Linear-trend estimate of when a metric reaches `limit`.

    Returns None when there is too little data, no upward trend, or the fit is
    poor (r^2 < 0.5), because a confident-sounding forecast from noise is worse
    than none.
    """
    pts = [(t, v) for t, v in zip(timestamps, values) if v is not None]
    if len(pts) < min_points:
        return None
    n = len(pts)
    mean_t = sum(t for t, _ in pts) / n
    mean_v = sum(v for _, v in pts) / n
    sxx = sum((t - mean_t) ** 2 for t, _ in pts)
    if sxx == 0:
        return None
    sxy = sum((t - mean_t) * (v - mean_v) for t, v in pts)
    slope = sxy / sxx  # units per second
    syy = sum((v - mean_v) ** 2 for _, v in pts)
    r2 = (sxy * sxy) / (sxx * syy) if syy > 0 else 0.0
    if slope <= 0 or r2 < 0.5:
        return None
    latest = pts[-1][1]
    if latest >= limit:
        return {"days_to_full": 0.0, "slope_per_day": round(slope * 86400, 3), "r2": round(r2, 2)}
    days = (limit - latest) / slope / 86400
    if days > max_days:
        return None
    return {"days_to_full": round(days, 1), "slope_per_day": round(slope * 86400, 3), "r2": round(r2, 2)}


# ---- summaries ----------------------------------------------------------------
def summarize_rules(ctx: Mapping[str, Any]) -> str:
    """Deterministic plain-English status summary."""
    parts: List[str] = []
    alerts = ctx.get("open_alerts", [])
    crit = [a for a in alerts if a["severity"] == "critical"]
    if not alerts:
        parts.append("All monitored systems are healthy: no open alerts.")
    else:
        parts.append(f"{len(alerts)} open alert(s), {len(crit)} critical.")
        for a in sorted(alerts, key=lambda x: ("critical", "warning", "info").index(x["severity"]))[:3]:
            parts.append(f"[{a['severity']}] {a['message'].rstrip('.')}.")

    incidents = ctx.get("incidents", [])
    if incidents:
        top = max(incidents, key=lambda i: SEVERITY_RANK[i["severity"]])
        parts.append(f"{len(incidents)} open incident(s). Most severe: {top['title']}.")
        cause = top.get("likely_cause")
        if cause and len(top["alerts"]) > 1:
            parts.append(f"Likely cause: {cause['label']} ({cause['confidence']} confidence).")

    svcs = ctx.get("services", [])
    if svcs:
        down = [s["name"] for s in svcs if not s.get("ok")]
        parts.append(f"{len(svcs) - len(down)}/{len(svcs)} services up" + (f" (down: {', '.join(down)})." if down else "."))

    for a in ctx.get("anomalies", []):
        parts.append(f"Unusual behaviour: {a['message']}.")
    for f in ctx.get("forecasts", []):
        if f["days_to_full"] <= 30:
            parts.append(f"Capacity: {f['metric']} is projected to fill in about {f['days_to_full']} day(s).")

    cost = (ctx.get("cloud") or {}).get("cost") if ctx.get("cloud") else None
    if cost and cost.get("budget_usd"):
        parts.append(f"AWS spend is ${cost['month_to_date_usd']} of a ${cost['budget_usd']} monthly budget "
                     f"({cost['budget_used_percent']}%).")
    return " ".join(parts)


def minimized_facts(ctx: Mapping[str, Any]) -> Dict[str, Any]:
    """The only data an LLM ever sees: counts and numbers. No names, hosts,
    alert messages or identifiers."""
    alerts = ctx.get("open_alerts", [])
    by_sev: Dict[str, int] = {}
    by_source: Dict[str, int] = {}
    for a in alerts:
        by_sev[a["severity"]] = by_sev.get(a["severity"], 0) + 1
        by_source[a["source"]] = by_source.get(a["source"], 0) + 1
    svcs = ctx.get("services", [])
    sysm = ctx.get("system") or {}
    cost = ((ctx.get("cloud") or {}).get("cost")) or {}
    return {
        "open_alerts_by_severity": by_sev,
        "open_alerts_by_source": by_source,
        "open_incidents": len(ctx.get("incidents", [])),
        "services_total": len(svcs),
        "services_down": sum(1 for s in svcs if not s.get("ok")),
        "cpu_percent": (sysm.get("cpu") or {}).get("percent"),
        "ram_percent": (sysm.get("ram") or {}).get("percent"),
        "disk_percent": (sysm.get("disk") or {}).get("percent"),
        "anomalies": [{"metric": a["metric"], "z": a["z"], "direction": a["direction"]} for a in ctx.get("anomalies", [])],
        "days_until_full": {f["metric"]: f["days_to_full"] for f in ctx.get("forecasts", [])},
        "aws_budget_used_percent": cost.get("budget_used_percent"),
    }


_SYSTEM_PROMPT = (
    "You are an SRE assistant. Summarise the monitoring facts below in at most 4 short sentences for an on-call "
    "engineer. Use only the facts provided; do not invent causes, hostnames or numbers. If everything is healthy, "
    "say so briefly. End with at most two concrete things to check, only if something is wrong."
)


async def llm_summary(ctx: Mapping[str, Any], ai: AiConfig, client: httpx.AsyncClient,
                      env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Ask Claude for a plain-language summary of *minimised* facts. Returns None
    (so the caller falls back to rules) when disabled, unconfigured or on any error."""
    if not ai.llm_summary:
        return None
    key = (os.environ if env is None else env).get(ai.api_key_env)
    if not key:
        return None
    import json

    try:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={
                "model": ai.model, "max_tokens": 300, "system": _SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": json.dumps(minimized_facts(ctx))}],
            },
            timeout=20,
        )
        resp.raise_for_status()
        blocks = resp.json().get("content", [])
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
        return text or None
    except (httpx.HTTPError, ValueError, KeyError):
        return None
