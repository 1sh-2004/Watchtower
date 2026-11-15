"""Background runtime: schedules collectors, persists results, drives alerts.

This is the key fix over the original app: metrics are collected continuously
on a schedule, not only while a browser tab happens to be polling.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import os
import re
import time
from collections import ChainMap, deque
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

import httpx

from . import aiops, slo
from .alerts import AlertEngine, Debouncer, notify
from .collectors.aws import AwsCollector, default_client_factory, describe_error
from .collectors.services import run_check
from .collectors.system import SystemCollector
from .config import AwsConfig, Config, ConfigError, ServiceConfig, parse_aws
from .db import Database
from .incidents import IncidentManager
from .settings import (SettingsStore, build_ui_service, clean_records, mask_key_id, secret_env_name,
                       service_to_record)

log = logging.getLogger("sentinel.runtime")


class Runtime:
    def __init__(self, cfg: Config, db: Database, system: Optional[SystemCollector] = None,
                 aws: Optional[AwsCollector] = None, http: Optional[httpx.AsyncClient] = None,
                 settings: Optional[SettingsStore] = None,
                 aws_client_factory: Optional[Callable[..., Any]] = None) -> None:
        self.cfg = cfg
        self.db = db
        self.system = system or SystemCollector()
        self.settings = settings
        # Test seam: a fake boto3-style client factory used for every AWS collector this runtime builds.
        self._aws_client_factory = aws_client_factory
        self.aws = aws if aws is not None else (
            AwsCollector(cfg.aws, client_factory=aws_client_factory) if cfg.aws.enabled else None)
        # Keys typed into the Settings tab, held in memory only (and in the 0600 settings file).
        self.secrets: Dict[str, str] = {}
        self._yaml_aws = copy.copy(cfg.aws)   # what sentinel.yaml said, so the UI override can be undone
        self._named_tasks: Dict[str, asyncio.Task] = {}
        self._running = False
        self._config_lock = asyncio.Lock()
        self.engine = AlertEngine(db)
        self.incidents = IncidentManager(db, cfg.incidents.window_seconds)
        self.debounce = Debouncer()
        self.http = http
        self._owns_http = http is None
        self.started_at = time.time()

        self.latest_system: Optional[Dict[str, Any]] = None
        self.latest_processes: Optional[Dict[str, Any]] = None
        self.latest_services: Dict[str, Dict[str, Any]] = {}
        self.latest_aws: Optional[Dict[str, Any]] = db.latest_cloud("aws") if self.aws else None
        self.errors: Dict[str, str] = {}
        # In-memory totals since start, exported as Prometheus counters (they reset on restart).
        self.check_totals: Dict[tuple, int] = {}
        # Recent combined disk read+write rate (bytes/s): the baseline for the I/O anomaly alert.
        self._io_history: deque = deque(maxlen=121)
        self._fail_streak: Dict[str, int] = {}
        self._tasks: List[asyncio.Task] = []
        self._summary_cache: Optional[Dict[str, Any]] = None

    # ---- alert evaluation -------------------------------------------------------
    def evaluate_system(self, s: Dict[str, Any]) -> List[Dict[str, Any]]:
        t = self.cfg.thresholds
        events = []
        for metric, label, thr in (("cpu", "CPU", t.cpu), ("ram", "Memory", t.ram), ("disk", "Disk", t.disk)):
            value = s[metric]["percent"]
            key = f"sys:{metric}"
            firing = self.debounce.hold(
                key, value > thr, t.sustained_samples, t.resolve_samples,
                lambda k=key: self.db.get_open_alert(k) is not None)
            if value > thr:
                msg = f"{label} usage {value}% is above the {thr:.0f}% threshold"
            else:
                msg = (f"{label} usage {value}% is back under the {thr:.0f}% threshold; resolving after "
                       f"{t.resolve_samples} calm samples ({self.debounce.good_streak(key)} so far)")
            ev = self.engine.set_state(key, firing, "critical" if value > thr + 10 else "warning", "system", msg)
            if ev:
                events.append(ev)
        ev = self.evaluate_disk_io(s)
        if ev:
            events.append(ev)
        return events

    def evaluate_disk_io(self, s: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Alert on a sustained jump in disk throughput compared with its recent baseline."""
        t = self.cfg.thresholds
        disk = s.get("disk") or {}
        read, write = disk.get("read_bps"), disk.get("write_bps")
        if t.disk_io_min_mbps <= 0 or read is None or write is None:
            return None
        total = read + write
        min_bps = t.disk_io_min_mbps * 1024 * 1024
        res = aiops.robust_zscore(list(self._io_history) + [total], window=120, min_samples=20,
                                  min_scale=min_bps / 4)
        self._io_history.append(total)
        anomalous = bool(res and res["z"] >= 3.5 and total - res["baseline"] >= min_bps)
        key = "sys:disk_io"
        firing = self.debounce.hold(key, anomalous, t.sustained_samples, t.resolve_samples,
                                    lambda: self.db.get_open_alert(key) is not None)
        mb = lambda b: round(b / 1024 / 1024, 1)  # noqa: E731
        baseline = mb(res["baseline"]) if res else None
        return self.engine.set_state(
            key, firing, "warning", "system",
            f"Disk I/O {mb(total)} MB/s is far above its recent typical {baseline} MB/s" if anomalous
            else f"Disk I/O {mb(total)} MB/s (typical {baseline} MB/s); resolving after "
                 f"{t.resolve_samples} calm samples")

    def evaluate_service(self, svc: ServiceConfig, r: Dict[str, Any]) -> List[Dict[str, Any]]:
        a = self.cfg.alerts
        name = svc.name
        events: List[Optional[Dict[str, Any]]] = []

        streak = 0 if r["ok"] else self._fail_streak.get(name, 0) + 1
        self._fail_streak[name] = streak
        events.append(self.engine.set_state(
            f"svc:{name}:down", streak >= svc.failures_before_alert, "critical", "service",
            f"{name} is failing ({r['error']}); {streak} consecutive failed check(s)",
        ))

        slow = bool(r["ok"] and r["latency_ms"] is not None and r["latency_ms"] > svc.latency_warn_ms)
        slow_key = f"svc:{name}:slow"
        slow_firing = self.debounce.hold(
            slow_key, slow, svc.failures_before_alert, svc.failures_before_alert,
            lambda: self.db.get_open_alert(slow_key) is not None)
        events.append(self.engine.set_state(
            slow_key, slow_firing, "warning", "service",
            f"{name} latency {r['latency_ms']}ms exceeds {svc.latency_warn_ms:.0f}ms"
            if slow else f"{name} latency {r['latency_ms']}ms is back under {svc.latency_warn_ms:.0f}ms; confirming recovery",
        ))

        limit, remaining = r.get("rl_limit"), r.get("rl_remaining")
        if limit and remaining is not None:
            pct = 100 * remaining / limit
            events.append(self.engine.set_state(
                f"svc:{name}:quota", pct <= a.quota_warn_pct, "warning", "service",
                f"{name} has {remaining} of {limit} requests remaining ({pct:.0f}%)",
            ))

        days = r.get("key_days_left")
        if days is not None:
            msg = (f"API key for {name} expired {-days} day(s) ago" if days < 0
                   else f"API key for {name} expires in {days} day(s)")
            events.append(self.engine.set_state(
                f"key:{name}:expiry", days <= a.expiry_warn_days, "critical" if days <= 3 else "warning",
                "service", msg,
            ))
        return [e for e in events if e]

    def evaluate_slo(self, svc: ServiceConfig) -> List[Dict[str, Any]]:
        """Open, refresh or resolve the SLO burn-rate alerts for one service."""
        report = slo.evaluate(self.db, svc)
        if report is None:
            return []
        events = []
        for suffix, st in slo.alert_states(report).items():
            ev = self.engine.set_state(f"slo:{svc.name}:{suffix}", st["firing"], st["severity"], "slo", st["message"])
            if ev:
                events.append(ev)
        return events

    def slo_reports(self) -> List[Dict[str, Any]]:
        """Current SLO report for every service that has an SLO. Synchronous (DB reads)."""
        return [r for svc in self.cfg.services if (r := slo.evaluate(self.db, svc))]

    async def _publish(self, events: List[Dict[str, Any]]) -> None:
        """After any alert change: group alerts into incidents, then send the webhook."""
        if not events:
            return
        await asyncio.to_thread(self.incidents.update)
        await notify(events, self.cfg.alerts.webhook_env, self.http)

    def evaluate_aws(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        desired = {f"aws:{f['id']}": {"severity": f["severity"], "message": f["message"]}
                   for f in data.get("findings", [])}
        if data.get("errors"):
            # Partial data: raise what we found but do not resolve existing alerts,
            # because the missing part might have contained the resolution.
            desired["aws:collector"] = {"severity": "warning",
                                        "message": "AWS collection had errors: " + "; ".join(data["errors"])}
            return [e for k, d in desired.items()
                    if (e := self.engine.set_state(k, True, d["severity"], "cloud", d["message"]))]
        events = self.engine.sync_group("aws:", desired, "cloud")
        return events

    # ---- ticks ----------------------------------------------------------------
    async def system_tick(self) -> None:
        sample = await asyncio.to_thread(self.system.sample)
        procs = await asyncio.to_thread(self.system.top_processes)
        await asyncio.to_thread(self.db.insert_system, sample)
        self.latest_system, self.latest_processes = sample, procs
        events = await asyncio.to_thread(self.evaluate_system, sample)
        await self._publish(events)

    async def service_tick(self, svc: ServiceConfig) -> None:
        assert self.http is not None
        result = await run_check(self.http, svc, env=self.env())
        await asyncio.to_thread(self.db.insert_check, result)
        self.latest_services[svc.name] = result
        outcome = (svc.name, "ok" if result["ok"] else "fail")
        self.check_totals[outcome] = self.check_totals.get(outcome, 0) + 1
        events = await asyncio.to_thread(self.evaluate_service, svc, result)
        events += await asyncio.to_thread(self.evaluate_slo, svc)
        await self._publish(events)

    async def aws_tick(self) -> None:
        if not self.aws:
            return
        data = await asyncio.to_thread(self.aws.collect)
        await asyncio.to_thread(self.db.save_cloud, "aws", data)
        data["collected_at"] = int(time.time())
        self.latest_aws = data
        events = await asyncio.to_thread(self.evaluate_aws, data)
        await self._publish(events)

    async def housekeeping_tick(self) -> None:
        await asyncio.to_thread(self.db.prune, self.cfg.retention_days)

    # ---- insights ---------------------------------------------------------------
    def build_context(self) -> Dict[str, Any]:
        """Gather everything the summary needs. Synchronous (DB reads): call via to_thread."""
        recent = self.db.system_history(1)
        week = self.db.system_history(24 * self.cfg.retention_days, max_points=400)
        forecasts = []
        disk = aiops.forecast_full(week["timestamps"], week["disk"], 100.0)
        if disk:
            forecasts.append({"metric": "disk", **disk})
        return {
            "open_alerts": self.db.list_alerts("open", 200),
            "services": list(self.latest_services.values()),
            "system": self.latest_system,
            "cloud": self.latest_aws,
            "incidents": self.incidents.list("open", 50),
            "anomalies": aiops.detect_anomalies(recent),
            "forecasts": forecasts,
        }

    async def insights(self) -> Dict[str, Any]:
        ctx = await asyncio.to_thread(self.build_context)
        ai = self.cfg.ai
        now = time.time()
        cached = self._summary_cache
        if cached and ai.llm_summary and now - cached["at"] < ai.cache_seconds:
            summary, source = cached["summary"], "llm"
        else:
            llm = await aiops.llm_summary(ctx, ai, self.http) if (ai.llm_summary and self.http) else None
            summary, source = (llm, "llm") if llm else (aiops.summarize_rules(ctx), "rules")
            if llm:
                self._summary_cache = {"at": now, "summary": llm}
        return {
            "summary": summary, "summary_source": source,
            "anomalies": ctx["anomalies"], "forecasts": ctx["forecasts"],
            "open_alerts": len(ctx["open_alerts"]),
            "open_incidents": len(ctx["incidents"]),
            "llm_enabled": ai.llm_summary,
        }

    # ---- lifecycle ------------------------------------------------------------------
    async def _forever(self, name: str, fn, interval: float, *args) -> None:
        while True:
            try:
                await fn(*args)
                self.errors.pop(name, None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a collector bug must not kill the loop
                self.errors[name] = type(exc).__name__
                log.exception("task %s failed", name)
            await asyncio.sleep(interval)

    def env(self) -> ChainMap:
        """Environment used to resolve API keys: UI-entered keys first, then real env vars."""
        return ChainMap(self.secrets, os.environ)

    def _spawn(self, name: str, fn, interval: float, *args) -> None:
        task = asyncio.create_task(self._forever(name, fn, interval, *args), name=name)
        self._tasks.append(task)
        self._named_tasks[name] = task

    async def _cancel(self, name: str) -> None:
        task = self._named_tasks.pop(name, None)
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if task in self._tasks:
            self._tasks.remove(task)
        self.errors.pop(name, None)

    async def start(self) -> None:
        if self.http is None:
            self.http = httpx.AsyncClient()
        self._running = True
        self._spawn("system", self.system_tick, self.cfg.interval_seconds)
        for svc in self.cfg.services:
            self._spawn(f"service:{svc.name}", self.service_tick, svc.interval_seconds, svc)
        if self.aws:
            self._spawn("aws", self.aws_tick, self.cfg.aws.interval_minutes * 60)
        self._spawn("housekeeping", self.housekeeping_tick, 3600)

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._named_tasks.clear()
        if self.http is not None and self._owns_http:
            await self.http.aclose()

    # ---- settings made in the dashboard ---------------------------------------------------
    def load_saved(self) -> None:
        """Merge services and AWS settings saved from the Settings tab. Call before start().

        sentinel.yaml wins on a name clash, and a bad saved record is skipped (and logged by
        name only) instead of stopping the monitor from starting."""
        if not self.settings:
            return
        data = self.settings.load()
        taken = {s.name for s in self.cfg.services}
        services = list(self.cfg.services)
        for raw in clean_records(data.get("services", [])):
            try:
                svc, secret = build_ui_service(raw, self.cfg.retention_days)
            except ConfigError as exc:
                log.warning("skipping saved service %r: %s", raw.get("name"), exc)
                continue
            if svc.name in taken:
                log.warning("saved service %r clashes with sentinel.yaml; keeping the yaml one", svc.name)
                continue
            taken.add(svc.name)
            services.append(svc)
            if secret:
                self.secrets[secret_env_name(svc.name)] = secret
        self.cfg.services = services

        raw_aws = data.get("aws")
        if isinstance(raw_aws, dict):
            try:
                self.cfg.aws = self._aws_from_payload(raw_aws, use_current_keys=False)
            except ConfigError as exc:
                log.warning("ignoring saved AWS settings: %s", exc)
            else:
                self.aws = AwsCollector(self.cfg.aws, client_factory=self._aws_client_factory) \
                    if self.cfg.aws.enabled else None
                self.latest_aws = self.db.latest_cloud("aws") if self.aws else None

    def _save(self) -> None:
        """Write every UI-sourced setting (including its keys) to the 0600 settings file."""
        if not self.settings:
            return
        records = [service_to_record(svc, self.secrets.get(secret_env_name(svc.name)))
                   for svc in self.cfg.services if svc.source == "ui"]
        aws = self.cfg.aws
        aws_rec = None
        if aws.source == "ui":
            aws_rec = {
                "enabled": aws.enabled, "regions": aws.regions, "profile": aws.profile,
                "monthly_budget_usd": aws.monthly_budget_usd, "budget_warn_pct": aws.budget_warn_pct,
                "idle_cpu_percent": aws.idle_cpu_percent, "idle_days": aws.idle_days,
                "interval_minutes": aws.interval_minutes,
                "flag_unexpected_regions": aws.flag_unexpected_regions,
                "access_key_id": aws.access_key_id, "secret_access_key": aws.secret_access_key,
                "session_token": aws.session_token,
            }
        self.settings.save({"services": records, "aws": aws_rec})

    def build_service(self, raw: Dict[str, Any]):
        """Validate a UI submission. If it has an api_key block but no value, keep the stored key."""
        if not str(raw.get("url") or "").strip():
            # Editing: the API never sends full URLs back (they can carry tokens), so blank = keep.
            current = next((s for s in self.cfg.services
                            if s.name == str(raw.get("name") or "").strip() and s.source == "ui"), None)
            if current:
                raw = {**raw, "url": current.url}
        svc, secret = build_ui_service(raw, self.cfg.retention_days)
        if secret is None and svc.api_key:
            secret = self.secrets.get(secret_env_name(svc.name))
        return svc, secret

    async def upsert_service(self, svc: ServiceConfig, secret: Optional[str]) -> None:
        async with self._config_lock:
            existing = next((s for s in self.cfg.services if s.name == svc.name), None)
            if existing is not None and existing.source != "ui":
                raise ConfigError(f"{svc.name!r} is defined in sentinel.yaml; edit it there")
            env = secret_env_name(svc.name)
            if svc.api_key and secret:
                self.secrets[env] = secret
            elif not svc.api_key:
                self.secrets.pop(env, None)
            await self._cancel(f"service:{svc.name}")
            self.cfg.services = [s for s in self.cfg.services if s.name != svc.name] + [svc]
            self._fail_streak.pop(svc.name, None)
            self._save()
            if self._running:
                self._spawn(f"service:{svc.name}", self.service_tick, svc.interval_seconds, svc)

    async def remove_service(self, name: str) -> bool:
        async with self._config_lock:
            svc = next((s for s in self.cfg.services if s.name == name), None)
            if svc is None:
                return False
            if svc.source != "ui":
                raise ConfigError(f"{name!r} is defined in sentinel.yaml; remove it there")
            await self._cancel(f"service:{name}")
            self.cfg.services = [s for s in self.cfg.services if s.name != name]
            self.secrets.pop(secret_env_name(name), None)
            self.latest_services.pop(name, None)
            self._fail_streak.pop(name, None)
            self.debounce.forget(f"svc:{name}:")
            events: List[Dict[str, Any]] = []
            for prefix, source in ((f"svc:{name}:", "service"), (f"key:{name}:", "service"), (f"slo:{name}:", "slo")):
                events += await asyncio.to_thread(self.engine.sync_group, prefix, {}, source)
            if events:
                await asyncio.to_thread(self.incidents.update)
            self._save()
            return True

    def _aws_from_payload(self, payload: Dict[str, Any], use_current_keys: bool = True) -> AwsConfig:
        cfg = parse_aws({**payload, "enabled": payload.get("enabled", True)})
        key_id = (payload.get("access_key_id") or "").strip() or None
        secret = (payload.get("secret_access_key") or "").strip() or None
        token = (payload.get("session_token") or "").strip() or None
        if bool(key_id) != bool(secret):
            raise ConfigError("enter both the access key ID and the secret access key, or neither "
                              "(to use an AWS profile, environment variables or an instance role)")
        cur = self.cfg.aws
        if (not key_id and use_current_keys and not payload.get("use_environment")
                and cur.source == "ui" and cur.access_key_id):
            key_id, secret, token = cur.access_key_id, cur.secret_access_key, cur.session_token
        if key_id and not re.fullmatch(r"[A-Za-z0-9]{16,128}", key_id):
            raise ConfigError("access key ID should be 16-128 letters and digits (it usually starts with AKIA or ASIA)")
        if secret and re.search(r"\s", secret + (token or "")):
            raise ConfigError("the secret key or session token contains whitespace; check the copy-paste")
        cfg.access_key_id, cfg.secret_access_key, cfg.session_token = key_id, secret, token
        cfg.source = "ui"
        return cfg

    def build_aws(self, payload: Dict[str, Any]) -> AwsConfig:
        return self._aws_from_payload(payload)

    async def set_aws(self, new: AwsConfig) -> None:
        """Swap AWS settings at runtime: stop the old loop, start a new one (first collection runs at once)."""
        async with self._config_lock:
            await self._cancel("aws")
            self.cfg.aws = new
            self.latest_aws = None
            if new.enabled:
                self.aws = AwsCollector(new, client_factory=self._aws_client_factory)
                if self._running:
                    self._spawn("aws", self.aws_tick, new.interval_minutes * 60)
            else:
                self.aws = None
                await self._drop_aws_alerts()
            self._save()

    async def reset_aws(self) -> None:
        """Forget the AWS settings and keys saved from the UI and go back to sentinel.yaml."""
        await self.set_aws(copy.copy(self._yaml_aws))

    async def _drop_aws_alerts(self) -> None:
        events = await asyncio.to_thread(self.engine.sync_group, "aws:", {}, "cloud")
        if events:
            await asyncio.to_thread(self.incidents.update)

    # ---- connection tests (nothing is saved, and nothing touches uptime stats) ----------------
    async def test_service(self, svc: ServiceConfig, secret: Optional[str]) -> Dict[str, Any]:
        env = ChainMap({secret_env_name(svc.name): secret} if secret else {}, self.secrets, os.environ)
        if self.http is not None:
            result = await run_check(self.http, svc, env=env)
        else:
            async with httpx.AsyncClient() as client:
                result = await run_check(client, svc, env=env)
        keep = ("ok", "status_code", "latency_ms", "error", "note", "rl_limit", "rl_remaining", "key_days_left")
        return {k: result.get(k) for k in keep}

    def test_aws(self, cfg: AwsConfig) -> Dict[str, Any]:
        """Free, read-only probes of each permission Sentinel needs. Blocking: call via to_thread.
        Cost Explorer is not probed because each call is billed (~$0.01); it is checked on the first collection."""
        factory = self._aws_client_factory or default_client_factory(
            cfg.profile, cfg.access_key_id, cfg.secret_access_key, cfg.session_token)
        out: Dict[str, Any] = {"ok": False, "account_hint": None, "warning": None, "checks": []}
        region = cfg.regions[0] if cfg.regions else "us-east-1"

        def probe(label: str, fn) -> bool:
            try:
                fn()
                out["checks"].append({"name": label, "ok": True, "error": None})
                return True
            except Exception as exc:  # noqa: BLE001 - report class/code only: messages can hold account details
                out["checks"].append({"name": label, "ok": False, "error": describe_error(exc)})
                return False

        def identity():
            ident = factory("sts", region).get_caller_identity()
            out["account_hint"] = "…" + str(ident.get("Account", ""))[-4:]
            if str(ident.get("Arn", "")).endswith(":root"):
                out["warning"] = ("These are ROOT credentials. Create an IAM user with the read-only policy "
                                  "in infra/aws-readonly-policy.json and use its keys instead.")

        if not probe("Sign in (STS)", identity):
            return out
        end = datetime.now(timezone.utc)
        probes = [
            (f"EC2 instances ({region})", lambda: factory("ec2", region).describe_instances(MaxResults=5)),
            ("EC2 regions list", lambda: factory("ec2", region).describe_regions()),
            ("S3 buckets", lambda: factory("s3", None).list_buckets()),
            (f"CloudWatch metrics ({region})", lambda: factory("cloudwatch", region).get_metric_statistics(
                Namespace="AWS/EC2", MetricName="CPUUtilization", StartTime=end - timedelta(hours=1), EndTime=end,
                Period=3600, Statistics=["Average"])),
        ]
        for label, fn in probes:
            probe(label, fn)
        out["ok"] = all(c["ok"] for c in out["checks"])
        return out

    def key_state(self, svc: ServiceConfig) -> Optional[bool]:
        """None if the service uses no key; else whether a key value is actually available. Never the value."""
        if svc.api_key is None or not svc.api_key.env:
            return None
        return bool(self.env().get(svc.api_key.env))

    def aws_view(self) -> Dict[str, Any]:
        a = self.cfg.aws
        return {
            "enabled": a.enabled, "regions": a.regions, "source": a.source,
            "profile": a.profile, "monthly_budget_usd": a.monthly_budget_usd,
            "budget_warn_pct": a.budget_warn_pct, "idle_cpu_percent": a.idle_cpu_percent,
            "idle_days": a.idle_days, "interval_minutes": a.interval_minutes,
            "flag_unexpected_regions": a.flag_unexpected_regions,
            "credentials": ("entered in dashboard" if a.access_key_id
                            else f"AWS profile {a.profile}" if a.profile else "environment / default AWS lookup"),
            "key_id_hint": mask_key_id(a.access_key_id),
            "has_session_token": bool(a.session_token),
        }
