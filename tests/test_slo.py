import os
import tempfile

import pytest

from sentinel.config import ConfigError, parse_config
from sentinel.db import Database
from sentinel import slo

NOW = 1_000_000_000


def make_cfg(target=99.9, window_days=7, interval=30, retention=7):
    return parse_config({
        "retention_days": retention,
        "services": [{"name": "api", "url": "https://x.io", "interval_seconds": interval,
                      "slo": {"target": target, "window_days": window_days}}],
    })


def seed(db, start, end, interval=30, bad_ranges=()):
    """One check every `interval` seconds; failed inside any (from, to) in bad_ranges."""
    for ts in range(start, end, interval):
        bad = any(a <= ts < b for a, b in bad_ranges)
        db.insert_check({"ts": ts, "name": "api", "ok": not bad, "status_code": 500 if bad else 200,
                         "latency_ms": 10.0, "error": None})


def report(db, cfg):
    return slo.evaluate(db, cfg.services[0], now=NOW)


def test_config_validation():
    assert make_cfg().services[0].slo.target == 99.9
    for bad in (100, 0, -1, 150):
        with pytest.raises(ConfigError):
            make_cfg(target=bad)
    with pytest.raises(ConfigError, match="retention_days"):
        make_cfg(window_days=10, retention=7)
    assert parse_config({"services": [{"name": "a", "url": "https://x.io"}]}).services[0].slo is None


def test_no_slo_means_no_report(db):
    cfg = parse_config({"services": [{"name": "api", "url": "https://x.io"}]})
    assert slo.evaluate(db, cfg.services[0]) is None


def test_healthy_service_keeps_full_budget(db):
    cfg = make_cfg()
    seed(db, NOW - 7200, NOW)
    r = report(db, cfg)
    assert r["status"] == "ok" and r["bad_checks"] == 0
    assert r["budget_remaining_percent"] == 100.0 and r["sli_percent"] == 100.0
    assert all(s["firing"] is False for s in (r["burn"]["fast"], r["burn"]["slow"]))


def test_budget_belongs_to_the_window_not_to_the_data_so_far(db):
    # 7 days at 30s = 20160 checks; 99.9% allows ~20.16 failures. Two hours of data
    # with 2 failures must not read as a blown budget.
    cfg = make_cfg()
    seed(db, NOW - 7200, NOW, bad_ranges=[(NOW - 100, NOW - 40)])
    r = report(db, cfg)
    assert r["bad_checks"] == 2
    assert r["budget_total_checks"] == pytest.approx(20.16)
    assert r["budget_remaining_percent"] == pytest.approx(100 * (20.16 - 2) / 20.16, abs=0.1)
    assert r["coverage_percent"] < 5


def test_fast_burn_needs_long_and_short_window_and_min_failures(db):
    cfg = make_cfg()
    # a lone failed check never pages, however sharp the short window looks
    seed(db, NOW - 7200, NOW, bad_ranges=[(NOW - 60, NOW - 30)])
    r = report(db, cfg)
    assert r["bad_checks"] == 1 and r["burn"]["fast"]["firing"] is False


def test_outage_fires_fast_burn_then_clears_after_recovery(db):
    cfg = make_cfg()
    seed(db, NOW - 7200, NOW, bad_ranges=[(NOW - 600, NOW)])
    r = report(db, cfg)
    assert r["burn"]["fast"]["firing"] and r["status"] == "fast_burn"
    states = slo.alert_states(r)
    assert states["fast_burn"]["firing"] and states["fast_burn"]["severity"] == "critical"

    # same outage, but it ended 20 minutes ago and checks have passed since: the short
    # window is clean, so the alert resolves even though the 1h window still remembers it
    d2 = Database(os.path.join(tempfile.mkdtemp(), "x.db"))
    seed(d2, NOW - 7200, NOW, bad_ranges=[(NOW - 2400, NOW - 1200)])
    r2 = slo.evaluate(d2, cfg.services[0], now=NOW)
    assert r2["burn"]["fast"]["long_rate"] > r2["burn"]["fast"]["threshold"]
    assert r2["burn"]["fast"]["short_rate"] == 0
    assert r2["burn"]["fast"]["firing"] is False


def test_exhausted_budget(db):
    cfg = make_cfg(target=99.0, window_days=1, interval=60)   # 1440 checks, 14.4 allowed failures
    seed(db, NOW - 86400, NOW, interval=60, bad_ranges=[(NOW - 86400, NOW - 86400 + 60 * 30)])
    r = report(db, cfg)
    assert r["bad_checks"] == 29 and r["budget_remaining_checks"] < 0   # window start is exclusive
    assert r["exhausted"] and r["status"] == "exhausted"
    assert r["budget_remaining_percent"] < 0


def test_burn_thresholds_follow_the_workbook_for_30_days():
    cfg = make_cfg(window_days=30, retention=30)
    p = slo.burn_policy(cfg.services[0].slo, 30)
    assert p.fast_threshold == pytest.approx(14.4) and p.slow_threshold == pytest.approx(6.0)
    short = slo.burn_policy(make_cfg(window_days=1).services[0].slo, 30)
    assert short.fast_threshold >= 2.0 and short.slow_threshold >= 1.0   # clamps for tiny windows


def test_short_window_never_smaller_than_two_intervals():
    p = slo.burn_policy(make_cfg().services[0].slo, 900)
    assert p.fast_short == 1800 and p.slow_short == 1800
    p = slo.burn_policy(make_cfg().services[0].slo, 30)
    assert p.fast_short == 300 and p.slow_short == 1800


def test_exhaustion_forecast_only_when_burning_faster_than_sustainable(db):
    cfg = make_cfg()
    seed(db, NOW - 7200, NOW)
    assert report(db, cfg)["exhausts_in_hours"] is None
    d2 = Database(os.path.join(tempfile.mkdtemp(), "y.db"))
    seed(d2, NOW - 7200, NOW, bad_ranges=[(NOW - 150, NOW)])
    r = slo.evaluate(d2, cfg.services[0], now=NOW)
    assert r["exhausts_in_hours"] is not None and r["exhausts_in_hours"] > 0


def test_check_counts_counts_each_window_in_one_query(db):
    seed(db, NOW - 3600, NOW, bad_ranges=[(NOW - 300, NOW - 150)])
    c = db.check_counts("api", {"1h": 3600, "10m": 600}, now=NOW)
    assert c["1h"]["total"] == 119 and c["1h"]["bad"] == 5      # the window start is exclusive
    assert c["10m"]["total"] == 19 and c["10m"]["bad"] == 5
