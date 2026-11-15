"""SLOs, error budgets and burn-rate alerting.

The SLI is the share of health checks that succeed. An SLO sets a target for
it (say 99.9%) over a rolling window, which implies an *error budget*: the
number of failed checks the service may have before it misses the target.

Alerts follow the multiwindow, multi-burn-rate approach from the Google SRE
Workbook instead of firing on single failures:

  burn rate = (observed failure ratio) / (allowed failure ratio)

A burn rate of 1 uses the budget exactly over the window; 10 empties it in a
tenth of the window. An alert fires only when the burn rate is high over a
long window AND still high over a short one. The long window shows the problem
is significant, the short one shows it is still happening, so the alert clears
soon after recovery.

  fast burn (critical): 2% of the budget gone within 1 hour
  slow burn (warning):  5% of the budget gone within 6 hours
  exhausted (critical): the budget for the window is spent

For a 30-day window these are the Workbook's 14.4x and 6x thresholds. They
are derived from the budget fraction, so they scale with `window_days`.

Limits worth knowing: checks are discrete (one every N seconds), so with a
tight target a single failed check is a large share of the budget. Alerts
therefore also need `MIN_BAD_CHECKS` failures in the long window. Time when
Sentinel itself was not running has no checks and is not counted against the
service; `coverage_percent` shows how full the window is.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from .config import ServiceConfig, SloConfig
from .db import Database

HOUR = 3600
FAST_LONG, SLOW_LONG = 1 * HOUR, 6 * HOUR
FAST_BUDGET_FRACTION, SLOW_BUDGET_FRACTION = 0.02, 0.05
MIN_BAD_CHECKS = 2


def fmt_window(seconds: int) -> str:
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


@dataclass(frozen=True)
class BurnPolicy:
    fast_long: int
    fast_short: int
    fast_threshold: float
    slow_long: int
    slow_short: int
    slow_threshold: float


def burn_policy(slo: SloConfig, interval_seconds: int) -> BurnPolicy:
    """Windows and thresholds for one service. The short window is a twelfth of
    the long one (as in the Workbook) but never shorter than two check intervals,
    so it always contains data."""
    window_hours = slo.window_days * 24
    floor = 2 * interval_seconds
    return BurnPolicy(
        fast_long=FAST_LONG, fast_short=max(FAST_LONG // 12, floor),
        fast_threshold=max(FAST_BUDGET_FRACTION * window_hours / (FAST_LONG / HOUR), 2.0),
        slow_long=SLOW_LONG, slow_short=max(SLOW_LONG // 12, floor),
        slow_threshold=max(SLOW_BUDGET_FRACTION * window_hours / (SLOW_LONG / HOUR), 1.0),
    )


def burn_rate(total: int, bad: int, target_percent: float) -> Optional[float]:
    if total <= 0:
        return None
    allowed_ratio = (100.0 - target_percent) / 100.0
    return (bad / total) / allowed_ratio


def evaluate(db: Database, svc: ServiceConfig, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Compute the SLO report for a service, or None when it has no SLO."""
    slo = svc.slo
    if slo is None:
        return None
    policy = burn_policy(slo, svc.interval_seconds)
    window = slo.window_days * 86400
    windows = {
        "window": window, "fast_long": policy.fast_long, "fast_short": policy.fast_short,
        "slow_long": policy.slow_long, "slow_short": policy.slow_short,
    }
    counts = db.check_counts(svc.name, windows, now)
    win = counts["window"]

    # The budget belongs to the whole window, not to the data collected so far:
    # judging it against a few checks would make one early failure look fatal.
    expected_checks = window / svc.interval_seconds
    basis = max(expected_checks, win["total"])
    allowed_ratio = (100.0 - slo.target) / 100.0
    budget_total = allowed_ratio * basis
    budget_remaining = budget_total - win["bad"]
    remaining_pct = 100.0 * budget_remaining / budget_total if budget_total else 0.0
    sli = round(100.0 * (win["total"] - win["bad"]) / win["total"], 3) if win["total"] else None

    rates = {k: burn_rate(counts[k]["total"], counts[k]["bad"], slo.target) for k in windows if k != "window"}

    def both(long_key: str, short_key: str, threshold: float) -> bool:
        lr, sr = rates[long_key], rates[short_key]
        return (lr is not None and sr is not None and lr >= threshold and sr >= threshold
                and counts[long_key]["bad"] >= MIN_BAD_CHECKS)

    fast = both("fast_long", "fast_short", policy.fast_threshold)
    slow = both("slow_long", "slow_short", policy.slow_threshold)
    exhausted = win["total"] > 0 and win["bad"] >= MIN_BAD_CHECKS and budget_remaining <= 0

    # If the last six hours' pace continued, when would the budget run out?
    exhausts_in_hours = None
    r6 = rates["slow_long"]
    if (r6 is not None and r6 > 1.0 and budget_remaining > 0 and budget_total > 0
            and counts["slow_long"]["bad"] >= MIN_BAD_CHECKS):
        fraction_per_hour = r6 / (slo.window_days * 24)
        exhausts_in_hours = round((budget_remaining / budget_total) / fraction_per_hour, 1)

    def r(v: Optional[float]) -> Optional[float]:
        return None if v is None else round(v, 2)

    return {
        "service": svc.name,
        "target_percent": slo.target,
        "window_days": slo.window_days,
        "sli_percent": sli,
        "checks": win["total"],
        "bad_checks": win["bad"],
        "coverage_percent": round(min(100.0, 100.0 * win["total"] / expected_checks), 1) if expected_checks else None,
        "budget_total_checks": round(budget_total, 2),
        "budget_remaining_checks": round(budget_remaining, 2),
        "budget_remaining_percent": round(remaining_pct, 1),
        "exhausts_in_hours": exhausts_in_hours,
        "burn": {
            "fast": {"long_window": fmt_window(policy.fast_long), "short_window": fmt_window(policy.fast_short),
                     "long_rate": r(rates["fast_long"]), "short_rate": r(rates["fast_short"]),
                     "threshold": round(policy.fast_threshold, 2), "firing": fast},
            "slow": {"long_window": fmt_window(policy.slow_long), "short_window": fmt_window(policy.slow_short),
                     "long_rate": r(rates["slow_long"]), "short_rate": r(rates["slow_short"]),
                     "threshold": round(policy.slow_threshold, 2), "firing": slow},
        },
        "exhausted": exhausted,
        "status": "exhausted" if exhausted else "fast_burn" if fast else "slow_burn" if slow else "ok",
    }


def alert_states(report: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Map an SLO report to the alert keys it controls: {suffix: {firing, severity, message}}."""
    name, b = report["service"], report["burn"]
    pct = report["target_percent"]
    return {
        "fast_burn": {
            "firing": b["fast"]["firing"], "severity": "critical",
            "message": (f"{name} is burning its {pct}% SLO error budget fast: {b['fast']['long_rate']}x over "
                        f"{b['fast']['long_window']} (alerts at {b['fast']['threshold']}x); "
                        f"{report['budget_remaining_percent']}% of budget left"),
        },
        "slow_burn": {
            "firing": b["slow"]["firing"], "severity": "warning",
            "message": (f"{name} is steadily burning its {pct}% SLO error budget: {b['slow']['long_rate']}x over "
                        f"{b['slow']['long_window']} (alerts at {b['slow']['threshold']}x)"),
        },
        "exhausted": {
            "firing": report["exhausted"], "severity": "critical",
            "message": (f"{name} has used up its error budget for the {report['window_days']}-day window "
                        f"(SLI {report['sli_percent']}% vs target {pct}%)"),
        },
    }
