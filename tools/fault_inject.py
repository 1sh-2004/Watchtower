#!/usr/bin/env python3
"""Fault injection for Sentinel: break things on purpose, measure detection time.

For every trial this script starts a fresh Sentinel (temporary config and
database) and a small target service it fully controls, waits for a healthy
baseline, injects one fault, and times how long Sentinel's own HTTP API takes to
show the expected alerts. It then removes the fault and times recovery.

    python tools/fault_inject.py                       # all scenarios, 3 trials each
    python tools/fault_inject.py --scenarios kill-service,cpu-spike --trials 1
    python tools/fault_inject.py --service-interval 30 # Sentinel's real default pace

Scenarios
    kill-service  SIGKILL the target process                       -> service down + SLO fast burn
    error-burst   target starts returning HTTP 500                 -> service down + SLO fast burn
    slow-api      target answers correctly but slowly              -> service slow
    cpu-spike     busy-loop processes on every core                -> CPU usage alert
    disk-io       a process writing and fsyncing large files       -> disk I/O anomaly alert
    full-cascade  slow API + CPU spike + disk I/O together; all
                  three must end up in ONE incident
    cascade       CPU spike and slow API together; checks that the
                  alerts are grouped into ONE incident             -> CPU + slow, one incident

Read the numbers correctly. Detection time is set mostly by how Sentinel is
configured (check interval x failures before alerting), so every result is
reported together with that configuration. The default here is a fast one so
a full run takes a few minutes; pass --service-interval 30 to measure the
production default. SLO alerts are timed for detection only: the burn-rate
short window is at least 5 minutes, so they clear long after a trial ends.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
SERVICE = "Target"
POLL = 0.1  # seconds between API polls; the resolution of every measurement


# --------------------------------------------------------------------------- target
class _TargetHandler(BaseHTTPRequestHandler):
    latency = 0.0
    status = 200

    def log_message(self, *_: Any) -> None:  # keep the console clean
        pass

    def _reply(self, code: int, body: bytes = b"ok") -> None:
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if urlparse(self.path).path == "/health":
            time.sleep(type(self).latency)
            self._reply(type(self).status)
        else:
            self._reply(404, b"not found")

    def do_POST(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        if u.path != "/_fault":
            return self._reply(404, b"not found")
        q = parse_qs(u.query)
        type(self).latency = float(q.get("latency_ms", ["0"])[0]) / 1000
        type(self).status = int(q.get("status", ["200"])[0])
        self._reply(200)


def serve_target(port: int) -> None:
    ThreadingHTTPServer(("127.0.0.1", port), _TargetHandler).serve_forever()


# ------------------------------------------------------------------- pure helpers
def build_config(target_port: int, a: argparse.Namespace) -> Dict[str, Any]:
    return {
        "interval_seconds": a.system_interval,
        "retention_days": 7,
        "thresholds": {"cpu": a.cpu_threshold, "ram": 99, "disk": 99.9, "sustained_samples": a.sustained,
                       "resolve_samples": a.resolve_samples, "disk_io_min_mbps": a.disk_io_min_mbps},
        "incidents": {"window_seconds": a.incident_window},
        "metrics": {"enabled": True},
        "services": [{
            "name": SERVICE, "url": f"http://127.0.0.1:{target_port}/health",
            "interval_seconds": a.service_interval, "timeout": 2.5, "latency_warn_ms": a.latency_warn_ms,
            "failures_before_alert": a.failures,
            "slo": {"target": 99.9, "window_days": 1},
        }],
    }


def summarize(values: List[Optional[float]]) -> Dict[str, Any]:
    """Median / min / max of the trials that produced a number; counts the misses."""
    got = [v for v in values if v is not None]
    return {
        "trials": len(values), "missed": len(values) - len(got),
        "median": round(statistics.median(got), 1) if got else None,
        "min": round(min(got), 1) if got else None,
        "max": round(max(got), 1) if got else None,
    }


def _fmt(s: Dict[str, Any]) -> str:
    if s["median"] is None and not s["trials"]:
        return "n/a"
    if s["median"] is None:
        return "MISSED" if s["trials"] else "n/a"
    base = f"{s['median']}s" if s["trials"] == 1 else f"{s['median']}s ({s['min']}-{s['max']}s)"
    return base + (f", {s['missed']} missed" if s["missed"] else "")


def render_markdown(report: Dict[str, Any]) -> str:
    c = report["config"]
    lines = [
        f"Measured with Sentinel checking every **{c['service_interval']}s** and alerting after "
        f"**{c['failures']}** consecutive failures (system sampled every {c['system_interval']}s, "
        f"CPU alert after {c['sustained']} samples over {c['cpu_threshold']}%, "
        f"alerts clear after {c.get('resolve_samples', 1)} calm samples). "
        f"{report['trials']} trial(s) per scenario, median with range, polled every {int(POLL * 1000)} ms.",
        "",
        "| Fault injected | Alert Sentinel raised | Detected after | Cleared after | Grouping |",
        "|---|---|---|---|---|",
    ]
    for r in report["results"]:
        for key, d in r["detection"].items():
            cleared = r["recovery"].get(key)
            grouping = r.get("incident_summary", "") if key == r["expect"][0] else ""
            lines.append(f"| {r['fault']} | `{key}` | {_fmt(d)} | {_fmt(cleared) if cleared else 'n/a'} | {grouping} |")
    flaps = sorted({k for r in report["results"] for k in r.get("flapped", [])})
    if flaps:
        lines += ["", f"Alerts that resolved on a quiet sample and re-fired during a trial: {', '.join(f'`{k}`' for k in flaps)}."]
    early = sorted({k for r in report["results"] for k in r.get("resolved_early", [])})
    if early:
        lines += ["", "Cleared while the fault was still running in at least one trial (noisy samples), so those "
                      f"trials are left out of the recovery time: {', '.join(f'`{k}`' for k in early)}."]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- harness
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Trial:
    """One fresh Sentinel + target. Use as a context manager."""

    def __init__(self, a: argparse.Namespace) -> None:
        import httpx
        self.a = a
        self.httpx = httpx
        self.tmp = tempfile.TemporaryDirectory(prefix="sentinel-fi-")
        self.target_port, self.sentinel_port = free_port(), free_port()
        self.target: Optional[subprocess.Popen] = None
        self.sentinel: Optional[subprocess.Popen] = None
        self.spinners: List[subprocess.Popen] = []
        self.writers: List[subprocess.Popen] = []
        self.api = httpx.Client(base_url=f"http://127.0.0.1:{self.sentinel_port}", timeout=5)
        self.ctl = httpx.Client(base_url=f"http://127.0.0.1:{self.target_port}", timeout=5)

    # -- lifecycle
    def __enter__(self) -> "Trial":
        import yaml
        cfg_path = Path(self.tmp.name) / "sentinel.yaml"
        cfg_path.write_text(yaml.safe_dump(build_config(self.target_port, self.a)))
        self.start_target()
        env = dict(os.environ, SENTINEL_CONFIG=str(cfg_path), SENTINEL_HOST="127.0.0.1",
                   SENTINEL_PORT=str(self.sentinel_port), SENTINEL_DB_PATH=str(Path(self.tmp.name) / "s.db"))
        self.sentinel = subprocess.Popen([sys.executable, str(ROOT / "main.py")], cwd=ROOT, env=env,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._wait(lambda: self.api.get("/api/health").status_code == 200, 20, "Sentinel did not start")
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop_spinners()
        self.stop_writer()
        for p in (self.sentinel, self.target):
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(5)
                except subprocess.TimeoutExpired:
                    p.kill()
        self.api.close(); self.ctl.close()
        self.tmp.cleanup()

    @staticmethod
    def _wait(fn: Callable[[], bool], timeout: float, msg: str) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                if fn():
                    return
            except Exception:  # noqa: BLE001 - server not up yet
                pass
            time.sleep(POLL)
        raise RuntimeError(msg)

    def start_target(self) -> None:
        self.target = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--serve-target",
                                        "--port", str(self.target_port)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._wait(lambda: self.ctl.get("/health").status_code == 200, 10, "target did not start")

    def warm_up(self, extra_baseline: bool = False) -> None:
        """Wait for a healthy baseline: several good checks and a system sample."""
        def ready() -> bool:
            svc = self.api.get("/api/services").json()["services"][0]
            return svc["stats_1h"]["checks"] >= self.a.warmup_checks and svc["stats_1h"]["failures"] == 0
        self._wait(ready, 60, "no healthy baseline")
        # The I/O anomaly alert needs ~20 samples of baseline before it can judge a jump.
        time.sleep(self.a.system_interval * (22 if extra_baseline else self.a.sustained + 1))

    # -- faults
    def set_fault(self, latency_ms: int = 0, status: int = 200) -> None:
        self.ctl.post("/_fault", params={"latency_ms": latency_ms, "status": status})

    def kill_target(self) -> None:
        assert self.target
        self.target.kill(); self.target.wait()

    def start_spinners(self) -> None:
        n = os.cpu_count() or 1
        self.spinners = [subprocess.Popen([sys.executable, "-c", "while True: pass"]) for _ in range(n)]

    def start_writer(self) -> None:
        """Write and fsync 16 MB chunks as fast as the disk allows, in the trial's temp dir."""
        code = ("import os,sys\nf=open(sys.argv[1],'wb')\nb=os.urandom(16<<20)\nn=0\n"
                "while True:\n f.write(b); f.flush(); os.fsync(f.fileno()); n+=1\n"
                " if n%16==0: f.seek(0); f.truncate()\n")
        self.writers = [subprocess.Popen([sys.executable, "-c", code, str(Path(self.tmp.name) / "io.bin")])]

    def stop_writer(self) -> None:
        for p in self.writers:
            p.kill()
        for p in self.writers:
            p.wait()
        self.writers = []

    def stop_spinners(self) -> None:
        for p in self.spinners:
            p.kill()
        for p in self.spinners:
            p.wait()
        self.spinners = []

    # -- observation
    def open_keys(self) -> set:
        return {a["key"] for a in self.api.get("/api/alerts", params={"status": "open", "limit": 200}).json()["alerts"]}

    def time_until(self, keys: List[str], present: bool, since: float, timeout: float) -> Dict[str, Optional[float]]:
        """Seconds (after `since`) until each key is open (present=True) or gone (False)."""
        found: Dict[str, Optional[float]] = {k: None for k in keys}
        while time.monotonic() - since < timeout and any(v is None for v in found.values()):
            now = time.monotonic()
            open_now = self.open_keys()
            for k in keys:
                if found[k] is None and ((k in open_now) == present):
                    found[k] = round(now - since, 2)
            time.sleep(POLL)
        return found


SCENARIOS: Dict[str, Dict[str, Any]] = {
    "kill-service": {
        "fault": "Kill the service process (SIGKILL)",
        "expect": [f"svc:{SERVICE}:down", f"slo:{SERVICE}:fast_burn"],
        "inject": lambda t: t.kill_target(),
        "recover": lambda t: t.start_target(),
    },
    "error-burst": {
        "fault": "Service starts returning HTTP 500",
        "expect": [f"svc:{SERVICE}:down", f"slo:{SERVICE}:fast_burn"],
        "inject": lambda t: t.set_fault(status=500),
        "recover": lambda t: t.set_fault(),
    },
    "slow-api": {
        "fault": "API responds correctly but 800 ms slower",
        "expect": [f"svc:{SERVICE}:slow"],
        "inject": lambda t: t.set_fault(latency_ms=800),
        "recover": lambda t: t.set_fault(),
    },
    "cpu-spike": {
        "fault": "CPU spike: busy loop on every core",
        "expect": ["sys:cpu"],
        "inject": lambda t: t.start_spinners(),
        "recover": lambda t: t.stop_spinners(),
    },
    "disk-io": {
        "fault": "Disk I/O spike: sustained heavy writes",
        "expect": ["sys:disk_io"],
        "inject": lambda t: t.start_writer(),
        "recover": lambda t: t.stop_writer(),
        "needs_baseline": True,
    },
    "full-cascade": {
        "fault": "Slow API + CPU spike + disk I/O spike together",
        "expect": ["sys:disk_io", "sys:cpu", f"svc:{SERVICE}:slow"],
        "inject": lambda t: (t.start_writer(), t.start_spinners(), t.set_fault(latency_ms=800)),
        "recover": lambda t: (t.stop_writer(), t.stop_spinners(), t.set_fault()),
        "single_incident": True,
        "needs_baseline": True,
    },
    "cascade": {
        "fault": "CPU spike and slow API together",
        "expect": ["sys:cpu", f"svc:{SERVICE}:slow"],
        "inject": lambda t: (t.start_spinners(), t.set_fault(latency_ms=800)),
        "recover": lambda t: (t.stop_spinners(), t.set_fault()),
        "single_incident": True,
    },
}


def run_trial(name: str, a: argparse.Namespace) -> Dict[str, Any]:
    sc = SCENARIOS[name]
    keys: List[str] = sc["expect"]
    with Trial(a) as t:
        t.warm_up(sc.get("needs_baseline", False))
        sc["inject"](t)
        t0 = time.monotonic()
        detected = t.time_until(keys, True, t0, a.timeout)
        time.sleep(a.settle)  # let grouping finish after the last alert opens

        # Every trial starts from an empty database, so every incident was caused by this fault.
        incs = t.api.get("/api/incidents").json()["incidents"]
        rows = t.api.get("/api/alerts", params={"limit": 200}).json()["alerts"]
        seen = {x["key"] for i in incs for x in i["alerts"]}
        main_inc = max(incs, key=lambda i: len(i["alerts"]), default=None)
        cause = main_inc["likely_cause"] if main_inc else None
        incident: Dict[str, Any] = {
            "incidents": len(incs), "grouped": set(keys) <= seen and len(incs) == 1,
            "cause": cause["key"] if cause else None, "confidence": cause["confidence"] if cause else None,
            # an alert that opened more than once flapped: it resolved on a quiet sample and re-fired
            "flapped": sorted({k for k in keys if sum(1 for r in rows if r["key"] == k) > 1}),
        }

        # SLO alerts clear only after the 5 minute short window is clean, so they are not timed here.
        clearable = [k for k in keys if not k.startswith("slo:")]
        open_now = t.open_keys()
        still_open = [k for k in clearable if k in open_now]
        # An alert that already cleared while the fault was still running (noisy samples) says
        # nothing about recovery time, so it is reported as n/a rather than as 0 seconds.
        incident["resolved_early"] = [k for k in clearable if k not in still_open]
        sc["recover"](t)
        t1 = time.monotonic()
        cleared: Dict[str, Optional[float]] = {k: None for k in clearable}
        cleared.update(t.time_until(still_open, False, t1, a.timeout))
    return {"detected": detected, "cleared": cleared, "incident": incident}


def run(a: argparse.Namespace) -> Dict[str, Any]:
    results = []
    for name in a.scenario_list:
        sc = SCENARIOS[name]
        print(f"\n== {name}: {sc['fault']}", flush=True)
        trials = []
        for i in range(a.trials):
            r = run_trial(name, a)
            trials.append(r)
            print(f"   trial {i + 1}/{a.trials}: detected {r['detected']}  cleared {r['cleared']}  "
                  f"incident {r['incident']}", flush=True)
        detection = {k: summarize([tr["detected"][k] for tr in trials]) for k in sc["expect"]}
        recovery = {k: summarize([tr["cleared"][k] for tr in trials if k not in tr["incident"]["resolved_early"]])
                    for k in sc["expect"] if not k.startswith("slo:")}
        entry: Dict[str, Any] = {"scenario": name, "fault": sc["fault"], "expect": sc["expect"],
                                 "detection": detection, "recovery": recovery,
                                 "trials_raw": trials}
        if sc.get("single_incident"):
            ok = [tr["incident"]["grouped"] for tr in trials]
            entry["incident_ok"] = all(ok)
            causes = sorted({tr["incident"]["cause"] or "none" for tr in trials})
            entry["incident_summary"] = (("1 incident" if all(ok) else "NOT grouped") + f"; likely cause: {', '.join(causes)}")
        else:
            entry["incident_summary"] = ""
        entry["flapped"] = sorted({k for tr in trials for k in tr["incident"]["flapped"]})
        entry["resolved_early"] = sorted({k for tr in trials for k in tr["incident"]["resolved_early"]})
        results.append(entry)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "environment": {"python": platform.python_version(), "os": f"{platform.system()} {platform.release()}",
                        "cpus": os.cpu_count()},
        "trials": a.trials,
        "config": {"service_interval": a.service_interval, "failures": a.failures,
                   "system_interval": a.system_interval, "sustained": a.sustained,
                   "resolve_samples": a.resolve_samples, "disk_io_min_mbps": a.disk_io_min_mbps,
                   "cpu_threshold": a.cpu_threshold, "latency_warn_ms": a.latency_warn_ms,
                   "incident_window": a.incident_window},
        "results": results,
    }


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--scenarios", default="all", help=f"comma list of: {', '.join(SCENARIOS)} (default: all)")
    p.add_argument("--trials", type=int, default=3)
    p.add_argument("--system-interval", type=float, default=1.0, help="seconds between system samples")
    p.add_argument("--service-interval", type=int, default=1, help="seconds between service checks")
    p.add_argument("--failures", type=int, default=2, help="failed checks before a service alert")
    p.add_argument("--sustained", type=int, default=3, help="samples over threshold before a system alert")
    p.add_argument("--resolve-samples", type=int, default=3, help="calm samples before a system alert resolves")
    p.add_argument("--disk-io-min-mbps", type=float, default=15.0)
    p.add_argument("--cpu-threshold", type=float, default=60.0)
    p.add_argument("--latency-warn-ms", type=float, default=300.0)
    p.add_argument("--incident-window", type=int, default=15)
    p.add_argument("--warmup-checks", type=int, default=6)
    p.add_argument("--settle", type=float, default=2.0, help="seconds to wait before reading incidents")
    p.add_argument("--timeout", type=float, default=90.0, help="give up on a detection after this many seconds")
    p.add_argument("--json", help="write full results to this file")
    p.add_argument("--markdown", help="write a README-ready table to this file")
    p.add_argument("--serve-target", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--port", type=int, help=argparse.SUPPRESS)
    a = p.parse_args(argv)
    names = list(SCENARIOS) if a.scenarios == "all" else [s.strip() for s in a.scenarios.split(",") if s.strip()]
    unknown = [n for n in names if n not in SCENARIOS]
    if unknown:
        p.error(f"unknown scenario(s): {', '.join(unknown)}")
    a.scenario_list = names
    return a


def main(argv: Optional[List[str]] = None) -> int:
    a = parse_args(argv)
    if a.serve_target:
        serve_target(a.port)
        return 0
    report = run(a)
    md = render_markdown(report)
    print("\n" + md)
    if a.json:
        Path(a.json).write_text(json.dumps(report, indent=2))
    if a.markdown:
        Path(a.markdown).write_text(md)
    missed = any(d["missed"] for r in report["results"] for d in r["detection"].values())
    ungrouped = any(r.get("incident_ok") is False for r in report["results"])
    return 1 if (missed or ungrouped) else 0


if __name__ == "__main__":
    sys.exit(main())
