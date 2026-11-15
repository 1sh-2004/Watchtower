"""Prometheus exposition at /metrics.

A custom collector reads Sentinel's current state at scrape time, so the
numbers can never drift from what the API and dashboard show. Each app gets its
own registry (nothing is registered globally).

Labels carry service names only. URLs, hostnames of monitored services, API
keys and alert text are never exported.
"""
from __future__ import annotations

import time
from typing import Any, Dict, Iterable, Optional

from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily
from prometheus_client.registry import Collector

from . import __version__
from .runtime import Runtime

SEVERITIES = ("info", "warning", "critical")


class SentinelCollector(Collector):
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt

    def collect(self) -> Iterable[Any]:
        rt = self.rt
        info = GaugeMetricFamily("sentinel_build_info", "Sentinel version.", labels=["version"])
        info.add_metric([__version__], 1)
        yield info
        yield GaugeMetricFamily("sentinel_uptime_seconds", "Seconds since Sentinel started.",
                                value=max(0.0, time.time() - rt.started_at))
        yield from self._system()
        yield from self._services()
        yield from self._slo()
        yield from self._alerts()
        yield from self._aws()

    # ---- local system ---------------------------------------------------------
    def _system(self) -> Iterable[Any]:
        s: Optional[Dict[str, Any]] = self.rt.latest_system
        if not s:
            return
        gauges = [
            ("sentinel_cpu_usage_ratio", "CPU utilisation, 0 to 1.", s["cpu"]["percent"] / 100),
            ("sentinel_memory_usage_ratio", "Memory utilisation, 0 to 1.", s["ram"]["percent"] / 100),
            ("sentinel_disk_usage_ratio", "Root disk utilisation, 0 to 1.", s["disk"]["percent"] / 100),
        ]
        for field, name, doc in (("read_bps", "disk_read", "Disk read throughput."),
                                 ("write_bps", "disk_write", "Disk write throughput.")):
            v = s["disk"].get(field)
            if v is not None:
                gauges.append((f"sentinel_{name}_bytes_per_second", doc, v))
        for key, name in (("up_bps", "upload"), ("down_bps", "download")):
            v = s["network"].get(key)
            if v is not None:
                gauges.append((f"sentinel_network_{name}_bytes_per_second", f"Network {name} throughput.", v))
        for name, doc, value in gauges:
            yield GaugeMetricFamily(name, doc, value=value)
        gpu = GaugeMetricFamily("sentinel_gpu_utilization_ratio", "NVIDIA GPU utilisation, 0 to 1.", labels=["gpu"])
        for i, g in enumerate(s.get("gpus") or []):
            if g.get("util_percent") is not None:
                gpu.add_metric([str(i)], g["util_percent"] / 100)
        yield gpu

    # ---- services -----------------------------------------------------------------
    def _services(self) -> Iterable[Any]:
        rt = self.rt
        up = GaugeMetricFamily("sentinel_service_up", "1 if the latest check succeeded.", labels=["service"])
        lat = GaugeMetricFamily("sentinel_service_latency_seconds", "Latency of the latest check.", labels=["service"])
        quota = GaugeMetricFamily("sentinel_service_quota_remaining_ratio",
                                  "Remaining rate-limit quota, 0 to 1, when the provider exposes it.", labels=["service"])
        key = GaugeMetricFamily("sentinel_api_key_days_until_expiry",
                                "Days until the configured API key expires (negative once expired).", labels=["service"])
        checks = CounterMetricFamily("sentinel_service_checks", "Health checks run since Sentinel started.",
                                     labels=["service", "result"])
        for svc in rt.cfg.services:
            for result in ("ok", "fail"):
                checks.add_metric([svc.name, result], rt.check_totals.get((svc.name, result), 0))
            r = rt.latest_services.get(svc.name)
            if not r:
                continue
            up.add_metric([svc.name], 1 if r["ok"] else 0)
            if r.get("latency_ms") is not None:
                lat.add_metric([svc.name], r["latency_ms"] / 1000)
            if r.get("rl_limit") and r.get("rl_remaining") is not None:
                quota.add_metric([svc.name], r["rl_remaining"] / r["rl_limit"])
            if r.get("key_days_left") is not None:
                key.add_metric([svc.name], r["key_days_left"])
        yield from (up, lat, quota, key, checks)

    # ---- SLOs ------------------------------------------------------------------------
    def _slo(self) -> Iterable[Any]:
        target = GaugeMetricFamily("sentinel_slo_target_ratio", "Availability objective, 0 to 1.", labels=["service"])
        sli = GaugeMetricFamily("sentinel_slo_sli_ratio", "Share of checks that succeeded in the SLO window.", labels=["service"])
        remaining = GaugeMetricFamily(
            "sentinel_slo_error_budget_remaining_ratio",
            "Fraction of the error budget left in the SLO window. Negative once overspent.", labels=["service"])
        burn = GaugeMetricFamily(
            "sentinel_slo_burn_rate", "Error budget burn rate; 1 spends the budget exactly over the window.",
            labels=["service", "window"])
        firing = GaugeMetricFamily("sentinel_slo_burn_alert_firing", "1 while a burn-rate alert condition holds.",
                                   labels=["service", "severity"])
        for r in self.rt.slo_reports():
            n = r["service"]
            target.add_metric([n], round(r["target_percent"] / 100, 6))
            if r["sli_percent"] is not None:
                sli.add_metric([n], r["sli_percent"] / 100)
            remaining.add_metric([n], r["budget_remaining_percent"] / 100)
            for kind in ("fast", "slow"):
                b = r["burn"][kind]
                for window, rate in ((b["long_window"], b["long_rate"]), (b["short_window"], b["short_rate"])):
                    if rate is not None:
                        burn.add_metric([n, window], rate)
            firing.add_metric([n, "critical"], 1 if r["burn"]["fast"]["firing"] or r["exhausted"] else 0)
            firing.add_metric([n, "warning"], 1 if r["burn"]["slow"]["firing"] else 0)
        yield from (target, sli, remaining, burn, firing)

    # ---- alerts and incidents -----------------------------------------------------------
    def _alerts(self) -> Iterable[Any]:
        open_alerts = self.rt.db.list_alerts("open", 500)
        counts: Dict[tuple, int] = {}
        for a in open_alerts:
            counts[(a["severity"], a["source"])] = counts.get((a["severity"], a["source"]), 0) + 1
        g = GaugeMetricFamily("sentinel_alerts_open", "Open alerts.", labels=["severity", "source"])
        for sev in SEVERITIES:
            for src in sorted({"system", "service", "slo", "cloud"} | {k[1] for k in counts}):
                g.add_metric([sev, src], counts.get((sev, src), 0))
        yield g
        yield GaugeMetricFamily("sentinel_incidents_open", "Open incidents (grouped alerts).",
                                value=len(self.rt.db.list_incidents("open", 500)))

    # ---- AWS ---------------------------------------------------------------------------------
    def _aws(self) -> Iterable[Any]:
        data = self.rt.latest_aws
        if not data:
            return
        cost = data.get("cost") or {}
        if cost.get("month_to_date_usd") is not None:
            yield GaugeMetricFamily("sentinel_aws_month_to_date_cost_usd", "AWS spend this month so far.",
                                    value=cost["month_to_date_usd"])
        if cost.get("budget_used_percent") is not None:
            yield GaugeMetricFamily("sentinel_aws_budget_used_ratio", "Share of the monthly AWS budget used.",
                                    value=cost["budget_used_percent"] / 100)
        findings = GaugeMetricFamily("sentinel_aws_findings", "Unused or unexpected AWS resources found.",
                                     labels=["severity"])
        for sev in SEVERITIES:
            findings.add_metric([sev], sum(1 for f in data.get("findings", []) if f.get("severity") == sev))
        yield findings
