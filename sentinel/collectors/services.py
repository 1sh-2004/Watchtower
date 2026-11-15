"""API and service health checks.

Measures availability, latency and HTTP errors, and captures rate-limit /
quota headers when a provider exposes them. API keys are read from the
environment only to authenticate the request; they are never stored,
logged or returned by the API.
"""
from __future__ import annotations

import os
import time
from datetime import date
from typing import Any, Dict, Mapping, Optional

import httpx

from ..config import ServiceConfig

# Header names providers commonly use, lower-cased. First match wins.
_LIMIT = ("x-ratelimit-limit", "ratelimit-limit", "x-rate-limit-limit", "x-ratelimit-limit-requests")
_REMAINING = ("x-ratelimit-remaining", "ratelimit-remaining", "x-rate-limit-remaining",
              "x-ratelimit-remaining-requests")
_RESET = ("x-ratelimit-reset", "ratelimit-reset", "x-rate-limit-reset", "retry-after")


def _first_int(headers: Mapping[str, str], names) -> Optional[int]:
    for name in names:
        value = headers.get(name)
        if value is None:
            continue
        try:
            return int(float(value))
        except ValueError:
            continue
    return None


def parse_rate_limit(headers: Mapping[str, str]) -> Dict[str, Optional[int]]:
    lowered = {k.lower(): v for k, v in headers.items()}
    return {
        "rl_limit": _first_int(lowered, _LIMIT),
        "rl_remaining": _first_int(lowered, _REMAINING),
        "rl_reset": _first_int(lowered, _RESET),
    }


def key_days_left(expires_on: Optional[date], today: Optional[date] = None) -> Optional[int]:
    """Days until a key expires (negative once expired). Metadata only."""
    if expires_on is None:
        return None
    return (expires_on - (today or date.today())).days


async def run_check(
    client: httpx.AsyncClient,
    svc: ServiceConfig,
    env: Optional[Mapping[str, str]] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Run one health check and return a result dict. Never raises."""
    env = os.environ if env is None else env
    headers: Dict[str, str] = {"User-Agent": "Sentinel-Monitor/2.0"}
    note = None

    if svc.api_key and svc.api_key.env:
        secret = env.get(svc.api_key.env)
        if secret:
            headers[svc.api_key.header] = f"{svc.api_key.scheme} {secret}" if svc.api_key.scheme else secret
        else:
            note = ("no API key has been entered for this service; checked without credentials"
                    if svc.api_key.env.startswith("ui:") else
                    f"environment variable {svc.api_key.env} is not set; checked without credentials")

    result: Dict[str, Any] = {
        "name": svc.name,
        "ts": int(now or time.time()),
        "ok": False,
        "status_code": None,
        "latency_ms": None,
        "error": None,
        "note": note,
        "rl_limit": None,
        "rl_remaining": None,
        "rl_reset": None,
        "key_days_left": key_days_left(svc.api_key.expires_on) if svc.api_key else None,
    }

    start = time.perf_counter()
    try:
        resp = await client.request(svc.method, svc.url, headers=headers, timeout=svc.timeout)
    except httpx.TimeoutException:
        result["error"] = "timeout"
        result["latency_ms"] = round((time.perf_counter() - start) * 1000, 1)
        return result
    except httpx.ConnectError:
        result["error"] = "connection failed"
        return result
    except httpx.HTTPError as exc:
        # Use the exception class only: str(exc) can echo request details.
        result["error"] = type(exc).__name__
        return result

    result["latency_ms"] = round((time.perf_counter() - start) * 1000, 1)
    result["status_code"] = resp.status_code
    result.update(parse_rate_limit(resp.headers))
    result["ok"] = resp.status_code in svc.expected_status
    if not result["ok"]:
        result["error"] = f"unexpected status {resp.status_code}"
    return result
