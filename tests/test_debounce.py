import pytest

from sentinel.alerts import Debouncer
from sentinel.config import ConfigError, parse_config
from sentinel.db import Database
from sentinel.runtime import Runtime

MB = 1024 * 1024


def test_hold_needs_n_bad_to_fire_and_n_good_to_clear():
    d = Debouncer()
    seq = [True, True, False, True, True, True, False, False, True, False, False, False]
    out = [d.hold("k", c, fire_after=3, clear_after=3) for c in seq]
    #        b     b     g      b     b     b     g      g      b     g      g      g
    assert out == [False, False, False, False, False, True, True, True, True, True, True, False]


def test_hold_starts_firing_when_an_alert_was_already_open():
    d = Debouncer()
    assert d.hold("k", False, 3, 3, initially_firing=lambda: True) is True   # restart: first calm sample must not resolve
    assert d.hold("k", False, 3, 3, initially_firing=lambda: True) is True
    assert d.hold("k", False, 3, 3) is False
    calls = []
    d2 = Debouncer()
    for _ in range(3):
        d2.hold("x", False, 1, 1, initially_firing=lambda: calls.append(1) or False)
    assert len(calls) == 1                                                   # asked once per key


def sample(cpu=10.0, read=0.0, write=0.0):
    return {"cpu": {"percent": cpu}, "ram": {"percent": 10.0}, "disk": {"percent": 10.0, "read_bps": read, "write_bps": write}}


@pytest.fixture
def rt(tmp_path):
    cfg = parse_config({"db_path": str(tmp_path / "d.db")})
    return Runtime(cfg, Database(cfg.db_path))


def test_noisy_cpu_does_not_flap(rt):
    opened = resolved = 0
    for cpu in [95, 95, 95, 40, 95, 40, 95, 95, 40, 40, 95, 40, 40, 40]:   # keeps bouncing across the threshold
        for e in rt.evaluate_system(sample(cpu)):
            opened += e["event"] == "opened"
            resolved += e["event"] == "resolved"
    assert opened == 1 and resolved == 1                                      # one alert, closed once, at the end
    assert len(rt.db.list_alerts()) == 1


def test_restart_does_not_resolve_an_open_alert_on_the_first_calm_sample(rt):
    for _ in range(3):
        rt.evaluate_system(sample(95))
    restarted = Runtime(rt.cfg, rt.db)                                        # fresh debouncer, same database
    assert restarted.evaluate_system(sample(10)) == []
    assert rt.db.get_open_alert("sys:cpu") is not None


def test_slow_alert_needs_consecutive_fast_checks_to_clear(rt):
    cfg = parse_config({"db_path": rt.cfg.db_path, "services": [{"name": "a", "url": "https://x.io", "latency_warn_ms": 100,
                                                               "failures_before_alert": 2}]})
    r2 = Runtime(cfg, rt.db)
    svc = cfg.services[0]
    slow = {"name": "a", "ok": True, "error": None, "latency_ms": 500.0}
    fast = {"name": "a", "ok": True, "error": None, "latency_ms": 10.0}
    keys = lambda evs: [(e["key"], e["event"]) for e in evs]  # noqa: E731
    assert r2.evaluate_service(svc, slow) == []
    assert keys(r2.evaluate_service(svc, slow)) == [("svc:a:slow", "opened")]
    assert r2.evaluate_service(svc, fast) == []                               # one fast check is not recovery
    assert r2.evaluate_service(svc, slow) == []                               # and a relapse does not re-open anything
    assert r2.evaluate_service(svc, fast) == []
    assert keys(r2.evaluate_service(svc, fast)) == [("svc:a:slow", "resolved")]


def test_disk_io_alert_fires_on_a_sustained_jump_and_clears(rt):
    for _ in range(25):                                                       # quiet baseline around 1 MB/s
        assert rt.evaluate_system(sample(read=0.5 * MB, write=0.5 * MB)) == []
    assert rt.evaluate_system(sample(read=100 * MB, write=100 * MB)) == []    # needs 3 samples in a row
    assert rt.evaluate_system(sample(read=100 * MB, write=100 * MB)) == []
    ev = rt.evaluate_system(sample(read=100 * MB, write=100 * MB))
    assert [(e["key"], e["event"]) for e in ev] == [("sys:disk_io", "opened")]
    assert "200.0 MB/s" in ev[0]["message"]
    for _ in range(2):
        assert rt.evaluate_system(sample(read=0.5 * MB, write=0.5 * MB)) == []
    ev = rt.evaluate_system(sample(read=0.5 * MB, write=0.5 * MB))
    assert [(e["key"], e["event"]) for e in ev] == [("sys:disk_io", "resolved")]


def test_disk_io_ignores_small_jumps_a_single_spike_and_unsupported_hosts(rt):
    for _ in range(25):
        rt.evaluate_system(sample(read=0.1 * MB, write=0.1 * MB))
    for _ in range(5):                                                        # 10x the baseline, but only 8 MB/s: not worth paging
        assert rt.evaluate_system(sample(read=4 * MB, write=4 * MB)) == []
    for _ in range(5):
        rt.evaluate_system(sample(read=0.1 * MB, write=0.1 * MB))
    assert rt.evaluate_system(sample(read=500 * MB, write=0)) == []           # one spike is debounced away
    assert rt.evaluate_system(sample(read=0.1 * MB, write=0.1 * MB)) == []
    s = sample(); s["disk"].pop("read_bps"); s["disk"].pop("write_bps")
    assert rt.evaluate_system(s) == [] and rt.db.get_open_alert("sys:disk_io") is None


def test_disk_io_can_be_disabled_and_config_is_validated(tmp_path):
    cfg = parse_config({"db_path": str(tmp_path / "x.db"), "thresholds": {"disk_io_min_mbps": 0}})
    rt = Runtime(cfg, Database(cfg.db_path))
    for _ in range(25):
        rt.evaluate_system(sample(read=0.1 * MB))
    for _ in range(5):
        assert rt.evaluate_system(sample(read=900 * MB)) == []
    with pytest.raises(ConfigError):
        parse_config({"thresholds": {"disk_io_min_mbps": -1}})
    with pytest.raises(ConfigError):
        parse_config({"thresholds": {"resolve_samples": 0}})


def test_disk_io_groups_with_cpu_and_a_slow_api_into_one_incident(rt):
    from sentinel.alerts import AlertEngine
    from sentinel.incidents import IncidentManager
    clock = [1_000_000]
    eng = AlertEngine(rt.db, lambda: clock[0])
    mgr = IncidentManager(rt.db, 300, lambda: clock[0])
    eng.set_state("sys:disk_io", True, "warning", "system", "Disk I/O spike"); mgr.update()
    clock[0] += 5
    eng.set_state("sys:cpu", True, "warning", "system", "CPU high"); mgr.update()
    clock[0] += 20
    eng.set_state("svc:api:slow", True, "warning", "service", "api slow"); mgr.update()
    incs = mgr.list()
    assert len(incs) == 1 and len(incs[0]["alerts"]) == 3
    assert incs[0]["likely_cause"]["key"] == "sys:disk_io" and "Disk I/O" in incs[0]["likely_cause"]["label"]
