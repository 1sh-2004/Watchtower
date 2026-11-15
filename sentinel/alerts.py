"""Alert engine: open/resolve lifecycle with de-duplication and debouncing.

An alert is identified by a stable key. Firing the same key repeatedly updates
one open alert instead of creating a new one each time, and a notification is
sent only when an alert opens or resolves. This keeps noise (alert fatigue) low.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional

import httpx

from .db import Database

log = logging.getLogger("sentinel.alerts")

SEVERITIES = ("info", "warning", "critical")


class Debouncer:
    """Requires a condition to hold for N consecutive evaluations before it fires."""

    def __init__(self) -> None:
        self._streaks: Dict[str, int] = {}
        self._holds: Dict[str, Dict[str, Any]] = {}

    def check(self, key: str, condition: bool, needed: int) -> bool:
        if condition:
            self._streaks[key] = self._streaks.get(key, 0) + 1
        else:
            self._streaks[key] = 0
        return self._streaks[key] >= needed

    def streak(self, key: str) -> int:
        return self._streaks.get(key, 0)

    def hold(self, key: str, condition: bool, fire_after: int, clear_after: int,
             initially_firing: Optional[Callable[[], bool]] = None) -> bool:
        """Hysteresis: fire after `fire_after` consecutive bad samples and stay
        firing until `clear_after` consecutive good ones. `initially_firing` is
        asked once per key, so an alert that was already open when Sentinel
        restarted is not resolved by the first good sample."""
        st = self._holds.get(key)
        if st is None:
            st = self._holds[key] = {"bad": 0, "good": 0, "firing": bool(initially_firing and initially_firing())}
        if condition:
            st["bad"], st["good"] = st["bad"] + 1, 0
            if not st["firing"] and st["bad"] >= fire_after:
                st["firing"] = True
        else:
            st["good"], st["bad"] = st["good"] + 1, 0
            if st["firing"] and st["good"] >= clear_after:
                st["firing"] = False
        return st["firing"]

    def forget(self, prefix: str) -> None:
        """Drop all state for keys starting with `prefix` (a removed service)."""
        for table in (self._streaks, self._holds):
            for key in [k for k in table if k.startswith(prefix)]:
                del table[key]

    def good_streak(self, key: str) -> int:
        return self._holds.get(key, {}).get("good", 0)


class AlertEngine:
    def __init__(self, db: Database, clock: Callable[[], float] = time.time) -> None:
        self.db = db
        self.clock = clock

    def set_state(self, key: str, firing: bool, severity: str = "warning", source: str = "system",
                  message: str = "") -> Optional[Dict[str, Any]]:
        """Open, refresh or resolve the alert for `key`. Returns an event dict when
        a notification-worthy transition happened (opened / resolved), else None."""
        if severity not in SEVERITIES:
            raise ValueError(f"unknown severity {severity!r}")
        ts = int(self.clock())
        existing = self.db.get_open_alert(key)

        if firing and existing is None:
            self.db.create_alert(key, source, severity, message, ts)
            return {"event": "opened", "key": key, "severity": severity, "source": source, "message": message}
        if firing and existing is not None:
            self.db.touch_alert(existing["id"], ts, severity, message)
            return None
        if not firing and existing is not None:
            self.db.resolve_alert(existing["id"], ts)
            return {"event": "resolved", "key": key, "severity": existing["severity"],
                    "source": existing["source"], "message": existing["message"]}
        return None

    def sync_group(self, prefix: str, desired: Dict[str, Dict[str, str]], source: str) -> List[Dict[str, Any]]:
        """Make the set of open alerts under `prefix` match `desired`
        ({key: {severity, message}}). Alerts for things that disappeared are resolved."""
        events = []
        for key, d in desired.items():
            ev = self.set_state(key, True, d["severity"], source, d["message"])
            if ev:
                events.append(ev)
        for key in self.db.open_alert_keys(prefix):
            if key not in desired:
                ev = self.set_state(key, False, source=source)
                if ev:
                    events.append(ev)
        return events


async def notify(events: List[Dict[str, Any]], webhook_env: Optional[str],
                 client: Optional[httpx.AsyncClient] = None, env=None) -> None:
    """POST events to a webhook whose URL lives in an environment variable.
    Failures are logged (without the URL) and never raised."""
    if not events or not webhook_env:
        return
    url = (env if env is not None else os.environ).get(webhook_env)
    if not url:
        return
    payload = {"source": "sentinel", "events": events}
    try:
        if client is not None:
            await client.post(url, json=payload, timeout=5)
        else:
            async with httpx.AsyncClient() as c:
                await c.post(url, json=payload, timeout=5)
    except httpx.HTTPError as exc:
        log.warning("webhook delivery failed: %s", type(exc).__name__)
