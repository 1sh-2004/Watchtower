import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import fault_inject as fi  # noqa: E402

from sentinel.config import parse_config  # noqa: E402


def args(*extra):
    return fi.parse_args(list(extra))


def test_generated_config_is_valid_for_sentinel():
    a = args("--service-interval", "2", "--failures", "3")
    cfg = parse_config(fi.build_config(8123, a))
    svc = cfg.services[0]
    assert svc.url == "http://127.0.0.1:8123/health" and svc.interval_seconds == 2
    assert svc.failures_before_alert == 3 and svc.slo.target == 99.9
    assert cfg.incidents.window_seconds == a.incident_window


def test_summarize_counts_misses_and_ignores_them_in_the_median():
    s = fi.summarize([2.0, None, 4.0, 6.0])
    assert s == {"trials": 4, "missed": 1, "median": 4.0, "min": 2.0, "max": 6.0}
    assert fi.summarize([None])["median"] is None and fi.summarize([])["trials"] == 0


def test_markdown_states_its_configuration_and_marks_misses():
    report = {
        "trials": 3,
        "config": {"service_interval": 30, "failures": 2, "system_interval": 5, "sustained": 3, "cpu_threshold": 80},
        "results": [{
            "fault": "Kill it", "expect": ["svc:Target:down", "slo:Target:fast_burn"],
            "detection": {"svc:Target:down": fi.summarize([60.0, 61.5, 64.0]),
                          "slo:Target:fast_burn": fi.summarize([None, None, None])},
            "recovery": {"svc:Target:down": fi.summarize([30.0, 31.0, 32.0])},
            "incident_summary": "1 incident; likely cause: svc:Target:down", "flapped": [], "resolved_early": [],
        }],
    }
    md = fi.render_markdown(report)
    assert "every **30s**" in md and "**2** consecutive failures" in md      # numbers are never shown without their config
    assert "61.5s (60.0-64.0s)" in md and "MISSED" in md and "1 incident" in md


def test_unknown_scenario_is_rejected():
    import pytest
    with pytest.raises(SystemExit):
        args("--scenarios", "nope")
    assert args("--scenarios", "slow-api,cascade").scenario_list == ["slow-api", "cascade"]
    assert args().scenario_list == list(fi.SCENARIOS)
