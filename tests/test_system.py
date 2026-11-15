from sentinel.collectors.system import SystemCollector, parse_nvidia_smi, rate


def test_rate_basic_and_edge_cases():
    assert rate(100, 300, 2) == 100
    assert rate(None, 300, 2) is None
    assert rate(100, 300, 0) is None
    assert rate(500, 100, 2) == 0.0  # counter reset must not produce a negative rate


def test_parse_nvidia_smi():
    out = "NVIDIA GeForce RTX 4060, 37, 1024, 8188, 52\nTesla T4, [N/A], 10, 15360, 40\n"
    gpus = parse_nvidia_smi(out)
    assert gpus[0] == {"name": "NVIDIA GeForce RTX 4060", "util_percent": 37.0, "mem_used_mb": 1024.0,
                       "mem_total_mb": 8188.0, "temp_c": 52.0}
    assert gpus[1]["util_percent"] is None  # unsupported field becomes None, not a crash
    assert parse_nvidia_smi("") == []
    assert parse_nvidia_smi("garbage line") == []


def test_sample_shape_and_rates_after_second_call():
    c = SystemCollector()
    first = c.sample()
    assert first["network"]["up_bps"] is None  # no previous counter yet
    second = c.sample()
    for key in ("cpu", "ram", "disk", "network", "gpus", "uptime", "ts"):
        assert key in second
    assert 0 <= second["cpu"]["percent"] <= 100
    assert 0 <= second["ram"]["percent"] <= 100
    assert second["network"]["up_bps"] is None or second["network"]["up_bps"] >= 0


def test_top_processes_shape():
    res = SystemCollector().top_processes(5)
    assert res["total_count"] > 0
    assert len(res["processes"]) <= 5
    for p in res["processes"]:
        assert set(p) == {"pid", "name", "cpu", "memory"}
        assert p["cpu"] <= 100
