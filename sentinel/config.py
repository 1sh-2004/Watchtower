"""Configuration loading and validation.

Secrets are never stored in the config file. Where a credential is needed
(an API key for an authenticated health check, a webhook URL, an LLM key) the
config holds only the *name of an environment variable*.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


class ConfigError(ValueError):
    """Raised when the configuration file is invalid."""


@dataclass
class Thresholds:
    cpu: float = 80.0
    ram: float = 85.0
    disk: float = 90.0
    # An alert only fires after this many consecutive samples over threshold,
    # so a one-off spike does not page anyone.
    sustained_samples: int = 3
    # An alert resolves only after this many consecutive samples back under the
    # threshold, so a noisy metric does not flap open and closed.
    resolve_samples: int = 3
    # Disk I/O has no natural ceiling, so it alerts on a sustained jump above its
    # own recent baseline, and only if the jump is at least this many MB/s. 0 turns it off.
    disk_io_min_mbps: float = 20.0


@dataclass
class ApiKeyMeta:
    """Metadata about an API key. The key itself is read from the environment."""

    env: Optional[str] = None
    header: str = "Authorization"
    scheme: Optional[str] = "Bearer"
    expires_on: Optional[date] = None


@dataclass
class SloConfig:
    """Availability objective for one service, measured over a rolling window."""

    target: float = 99.9        # percent of checks that must succeed
    window_days: int = 7        # cannot exceed retention_days: the data must exist


@dataclass
class ServiceConfig:
    name: str
    url: str
    method: str = "GET"
    timeout: float = 5.0
    expected_status: List[int] = field(default_factory=lambda: [200])
    interval_seconds: int = 30
    latency_warn_ms: float = 1000.0
    failures_before_alert: int = 2
    api_key: Optional[ApiKeyMeta] = None
    slo: Optional[SloConfig] = None
    source: str = "config"  # "config" = sentinel.yaml, "ui" = added from the Settings tab


@dataclass
class AwsConfig:
    enabled: bool = False
    regions: List[str] = field(default_factory=list)
    profile: Optional[str] = None
    monthly_budget_usd: float = 0.0  # 0 disables budget alerts
    budget_warn_pct: float = 80.0
    idle_cpu_percent: float = 5.0
    idle_days: int = 7
    interval_minutes: int = 360  # Cost Explorer bills per request; keep this slow
    flag_unexpected_regions: bool = True
    max_s3_buckets: int = 50
    # Credentials entered in the Settings tab. repr=False keeps them out of logs and tracebacks.
    # When all are None, boto3's normal lookup applies (env vars, profile, instance role).
    access_key_id: Optional[str] = field(default=None, repr=False)
    secret_access_key: Optional[str] = field(default=None, repr=False)
    session_token: Optional[str] = field(default=None, repr=False)
    source: str = "config"


@dataclass
class AiConfig:
    llm_summary: bool = False  # opt-in; off by default
    model: str = "claude-sonnet-5-5"
    api_key_env: str = "ANTHROPIC_API_KEY"
    cache_seconds: int = 300


@dataclass
class AlertsConfig:
    webhook_env: Optional[str] = None  # env var holding a webhook URL
    expiry_warn_days: int = 14
    quota_warn_pct: float = 10.0  # alert when remaining quota <= this percent


@dataclass
class IncidentsConfig:
    # Alerts that open within this many seconds of an incident's latest activity
    # are grouped into it.
    window_seconds: int = 300


@dataclass
class Config:
    interval_seconds: int = 5
    retention_days: int = 7
    db_path: str = "sentinel.db"
    thresholds: Thresholds = field(default_factory=Thresholds)
    services: List[ServiceConfig] = field(default_factory=list)
    aws: AwsConfig = field(default_factory=AwsConfig)
    ai: AiConfig = field(default_factory=AiConfig)
    alerts: AlertsConfig = field(default_factory=AlertsConfig)
    incidents: IncidentsConfig = field(default_factory=IncidentsConfig)
    metrics_enabled: bool = True  # expose Prometheus /metrics


def _as_date(value: Any, where: str) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ConfigError(f"{where}: expires_on must be YYYY-MM-DD, got {value!r}") from exc


def _positive(value: Any, where: str, minimum: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{where}: expected a number, got {value!r}") from exc
    if number <= minimum:
        raise ConfigError(f"{where}: must be greater than {minimum}")
    return number


def _non_negative(value: Any, where: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{where}: expected a number, got {value!r}") from exc
    if number < 0:
        raise ConfigError(f"{where}: must not be negative")
    return number


def parse_service(raw: Dict[str, Any], index: int, retention_days: int = 7) -> ServiceConfig:
    where = f"services[{index}]"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: must be a mapping")
    for required in ("name", "url"):
        if not raw.get(required):
            raise ConfigError(f"{where}: '{required}' is required")
    url = str(raw["url"])
    if not url.startswith(("http://", "https://")):
        raise ConfigError(f"{where}: url must start with http:// or https://")

    key = None
    if raw.get("api_key"):
        k = raw["api_key"]
        if "value" in k or "key" in k:
            raise ConfigError(
                f"{where}.api_key: never put a key value in the config. "
                "Use 'env: NAME_OF_ENV_VAR' instead."
            )
        key = ApiKeyMeta(
            env=k.get("env"),
            header=k.get("header", "Authorization"),
            scheme=k.get("scheme", "Bearer"),
            expires_on=_as_date(k.get("expires_on"), f"{where}.api_key"),
        )

    slo = None
    if raw.get("slo") is not None:
        r = raw["slo"]
        if not isinstance(r, dict):
            raise ConfigError(f"{where}.slo: must be a mapping")
        try:
            target = float(r.get("target", 99.9))
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{where}.slo.target: expected a number, got {r.get('target')!r}") from exc
        if not 0 < target < 100:
            raise ConfigError(f"{where}.slo.target: must be between 0 and 100 (exclusive), e.g. 99.9")
        days = int(_positive(r.get("window_days", min(7, retention_days)), f"{where}.slo.window_days"))
        if days > retention_days:
            raise ConfigError(
                f"{where}.slo.window_days ({days}) cannot exceed retention_days ({retention_days}): "
                "Sentinel prunes check history older than that")
        slo = SloConfig(target=target, window_days=days)

    expected = raw.get("expected_status", [200])
    if isinstance(expected, int):
        expected = [expected]

    return ServiceConfig(
        name=str(raw["name"]),
        url=url,
        method=str(raw.get("method", "GET")).upper(),
        timeout=_positive(raw.get("timeout", 5), f"{where}.timeout"),
        expected_status=[int(s) for s in expected],
        interval_seconds=int(_positive(raw.get("interval_seconds", 30), f"{where}.interval_seconds")),
        latency_warn_ms=_positive(raw.get("latency_warn_ms", 1000), f"{where}.latency_warn_ms"),
        failures_before_alert=int(_positive(raw.get("failures_before_alert", 2), f"{where}.failures_before_alert")),
        api_key=key,
        slo=slo,
    )


def parse_aws(raw: Optional[Dict[str, Any]]) -> AwsConfig:
    a = raw or {}
    regions = a.get("regions", [])
    if isinstance(regions, str):
        regions = [r.strip() for r in regions.split(",") if r.strip()]
    cfg = AwsConfig(
        enabled=bool(a.get("enabled", False)),
        regions=[str(r) for r in regions],
        profile=a.get("profile") or None,
        monthly_budget_usd=_non_negative(a.get("monthly_budget_usd", 0), "aws.monthly_budget_usd"),
        budget_warn_pct=_positive(a.get("budget_warn_pct", 80), "aws.budget_warn_pct"),
        idle_cpu_percent=_non_negative(a.get("idle_cpu_percent", 5), "aws.idle_cpu_percent"),
        idle_days=int(_positive(a.get("idle_days", 7), "aws.idle_days")),
        interval_minutes=int(_positive(a.get("interval_minutes", 360), "aws.interval_minutes")),
        flag_unexpected_regions=bool(a.get("flag_unexpected_regions", True)),
        max_s3_buckets=int(a.get("max_s3_buckets", 50)),
    )
    for region in cfg.regions:
        if not re.fullmatch(r"[a-z]{2}(-[a-z]+)+-\d", region):
            raise ConfigError(f"aws.regions: {region!r} does not look like an AWS region (e.g. ap-south-1)")
    if cfg.enabled and not cfg.regions:
        raise ConfigError("aws.enabled is true but aws.regions is empty")
    return cfg


def parse_config(raw: Optional[Dict[str, Any]]) -> Config:
    raw = raw or {}
    cfg = Config(
        interval_seconds=int(_positive(raw.get("interval_seconds", 5), "interval_seconds")),
        retention_days=int(_positive(raw.get("retention_days", 7), "retention_days")),
        db_path=os.environ.get("SENTINEL_DB_PATH") or str(raw.get("db_path", "sentinel.db")),
    )

    t = raw.get("thresholds") or {}
    cfg.thresholds = Thresholds(
        cpu=_positive(t.get("cpu", 80), "thresholds.cpu"),
        ram=_positive(t.get("ram", 85), "thresholds.ram"),
        disk=_positive(t.get("disk", 90), "thresholds.disk"),
        sustained_samples=int(_positive(t.get("sustained_samples", 3), "thresholds.sustained_samples")),
        resolve_samples=int(_positive(t.get("resolve_samples", 3), "thresholds.resolve_samples")),
        disk_io_min_mbps=_non_negative(t.get("disk_io_min_mbps", 20), "thresholds.disk_io_min_mbps"),
    )

    names = set()
    for i, svc in enumerate(raw.get("services") or []):
        parsed = parse_service(svc, i, cfg.retention_days)
        if parsed.name in names:
            raise ConfigError(f"services[{i}]: duplicate name {parsed.name!r}")
        names.add(parsed.name)
        cfg.services.append(parsed)

    cfg.aws = parse_aws(raw.get("aws"))

    ai = raw.get("ai") or {}
    cfg.ai = AiConfig(
        llm_summary=bool(ai.get("llm_summary", False)),
        model=str(ai.get("model", "claude-sonnet-5-5")),
        api_key_env=str(ai.get("api_key_env", "ANTHROPIC_API_KEY")),
        cache_seconds=int(ai.get("cache_seconds", 300)),
    )

    al = raw.get("alerts") or {}
    cfg.alerts = AlertsConfig(
        webhook_env=al.get("webhook_env"),
        expiry_warn_days=int(al.get("expiry_warn_days", 14)),
        quota_warn_pct=float(al.get("quota_warn_pct", 10)),
    )

    inc = raw.get("incidents") or {}
    cfg.incidents = IncidentsConfig(
        window_seconds=int(_positive(inc.get("window_seconds", 300), "incidents.window_seconds")),
    )
    cfg.metrics_enabled = bool((raw.get("metrics") or {}).get("enabled", True))
    return cfg


def load_config(path: Optional[str] = None) -> Config:
    """Load config from `path`, $SENTINEL_CONFIG, or ./sentinel.yaml; else defaults."""
    candidate = path or os.environ.get("SENTINEL_CONFIG") or "sentinel.yaml"
    p = Path(candidate)
    if not p.exists():
        if path or os.environ.get("SENTINEL_CONFIG"):
            raise ConfigError(f"config file not found: {p}")
        return parse_config(None)
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{p}: invalid YAML: {exc}") from exc
    return parse_config(raw)
