"""Settings entered in the dashboard (Settings tab), persisted to a local file.

Sentinel is a local tool, so this is a plain JSON file next to the database,
written atomically with owner-only permissions (0600). It holds the services
and AWS settings added from the UI, including any API keys or AWS keys typed
there. Keys are never returned by the API and never logged; the dashboard only
learns whether a key is set.

Anything in sentinel.yaml stays in sentinel.yaml: the UI never edits that file.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import ConfigError, ServiceConfig, parse_service

log = logging.getLogger("sentinel.settings")

VERSION = 1
_HEADER_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")  # RFC 7230 token
_NAME_BAD = re.compile(r"[:\x00-\x1f\x7f]")


class SettingsStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def load(self) -> Dict[str, Any]:
        empty: Dict[str, Any] = {"version": VERSION, "services": [], "aws": None}
        if not self.path.exists():
            return empty
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except (OSError, ValueError) as exc:
            # Do not crash the monitor over a damaged settings file, and do not echo its contents.
            log.warning("could not read %s (%s); ignoring it", self.path.name, type(exc).__name__)
            return empty
        data.setdefault("services", [])
        data.setdefault("aws", None)
        return data

    def save(self, data: Dict[str, Any]) -> None:
        """Atomic write with owner-only permissions."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".sentinel-settings-")
        try:
            # mkstemp creates the file 0600 already, so the keys are never briefly world-readable.
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({**data, "version": VERSION}, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def secret_env_name(service_name: str) -> str:
    """Synthetic 'environment variable' name under which a UI-entered key is held in memory."""
    return f"ui:{service_name}"


def build_ui_service(raw: Dict[str, Any], retention_days: int) -> Tuple[ServiceConfig, Optional[str]]:
    """Validate a service submitted from the UI. Returns (config, secret value or None).

    The secret is split off here so the ServiceConfig never carries it.
    """
    if not isinstance(raw, dict):
        raise ConfigError("service must be an object")
    name = str(raw.get("name") or "").strip()
    if not name:
        raise ConfigError("name is required")
    if len(name) > 80 or _NAME_BAD.search(name):
        raise ConfigError("name must be at most 80 characters and cannot contain ':' or control characters")

    shaped = {k: v for k, v in raw.items() if k not in ("api_key", "source")}
    shaped["name"] = name
    secret: Optional[str] = None
    key = raw.get("api_key")
    if key:
        if not isinstance(key, dict):
            raise ConfigError("api_key must be an object")
        value = key.get("value")
        secret = str(value).strip() if value else None
        header = str(key.get("header") or "Authorization").strip()
        if not _HEADER_RE.match(header):
            raise ConfigError(f"api_key.header {header!r} is not a valid HTTP header name")
        if secret and re.search(r"[\x00-\x1f\x7f]", secret):
            raise ConfigError("the API key contains control characters (a stray newline from copy-paste?)")
        shaped["api_key"] = {
            "env": secret_env_name(name), "header": header,
            "scheme": (str(key["scheme"]).strip() if key.get("scheme") is not None else "Bearer") or None,
            "expires_on": key.get("expires_on") or None,
        }
    svc = parse_service(shaped, 0, retention_days)
    svc.source = "ui"
    return svc, secret


def service_to_record(svc: ServiceConfig, secret: Optional[str]) -> Dict[str, Any]:
    """Serialise a UI service for the settings file (this is the only place a key is written)."""
    rec: Dict[str, Any] = {
        "name": svc.name, "url": svc.url, "method": svc.method, "timeout": svc.timeout,
        "expected_status": svc.expected_status, "interval_seconds": svc.interval_seconds,
        "latency_warn_ms": svc.latency_warn_ms, "failures_before_alert": svc.failures_before_alert,
        "api_key": None, "slo": None,
    }
    if svc.api_key:
        rec["api_key"] = {
            "value": secret, "header": svc.api_key.header, "scheme": svc.api_key.scheme,
            "expires_on": svc.api_key.expires_on.isoformat() if svc.api_key.expires_on else None,
        }
    if svc.slo:
        rec["slo"] = {"target": svc.slo.target, "window_days": svc.slo.window_days}
    return rec


def mask_key_id(key_id: Optional[str]) -> Optional[str]:
    """Show only the last 4 characters of an access key ID."""
    if not key_id:
        return None
    return "…" + key_id[-4:] if len(key_id) > 4 else "…"


def clean_records(records: List[Any]) -> List[Dict[str, Any]]:
    return [r for r in records if isinstance(r, dict)]
