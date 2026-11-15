"""Incident grouping: related alerts become one incident with a timeline and a
ranked likely cause.

Grouping is by time. An alert that opens within `window_seconds` of an
incident's latest activity (an alert opening or resolving) joins it, and a
resolved incident reopens if a member flaps back inside that window. One
failure usually trips several alerts a few seconds apart (a CPU spike slows an
API, the checks start failing, the SLO starts burning), and this puts them in
one place.

Only operational alerts are grouped (`sys:`, `svc:`, `slo:`). API-key expiry
and AWS cost/waste findings are housekeeping, not part of an outage, so they
stay out of incidents.

The likely cause is a transparent heuristic, not a model:
  1. Alerts are placed in layers: host resources (0) sit below services (1),
     which sit below the SLO impact (2). A lower layer can explain a higher one.
  2. A resource alert is a candidate only if it opened no more than
     `RESOURCE_LEAD_TOLERANCE` seconds after the first service symptom, since
     debouncing delays alerts differently.
  3. The earliest candidate in the lowest layer wins.
It reports correlation. Confidence is never higher than "medium".
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from .db import Database

SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}
RESOURCE_LEAD_TOLERANCE = 120

_RESOURCE_NAMES = {"cpu": "CPU", "ram": "Memory", "disk": "Disk", "disk_io": "Disk I/O"}


def is_groupable(key: str) -> bool:
    return key.startswith(("sys:", "svc:", "slo:"))


def layer(key: str) -> int:
    if key.startswith("sys:"):
        return 0
    if key.startswith("slo:"):
        return 2
    return 1


def _label(alert: Dict[str, Any]) -> str:
    parts = alert["key"].split(":")
    if parts[0] == "sys":
        return f"{_RESOURCE_NAMES.get(parts[1], parts[1])} usage"
    if parts[0] == "svc":
        return f"{parts[1]} {parts[2]}"
    if parts[0] == "slo":
        return f"{parts[1]} SLO burn"
    return alert["key"]


def likely_cause(alerts: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not alerts:
        return None
    ordered = sorted(alerts, key=lambda a: (a["first_ts"], a["id"]))
    if len(ordered) == 1:
        a = ordered[0]
        return {"key": a["key"], "label": _label(a), "confidence": "low",
                "reason": "It is the only alert in this incident, so nothing else can be compared with it."}

    symptoms = [a for a in ordered if layer(a["key"]) == 1]
    resources = [a for a in ordered if layer(a["key"]) == 0]
    first_symptom = symptoms[0] if symptoms else None

    if resources and (first_symptom is None
                      or resources[0]["first_ts"] <= first_symptom["first_ts"] + RESOURCE_LEAD_TOLERANCE):
        cause = resources[0]
        if first_symptom is None:
            return {"key": cause["key"], "label": _label(cause), "confidence": "low",
                    "reason": "It is the lowest layer alert here (host resource) and it opened first."}
        lead = first_symptom["first_ts"] - cause["first_ts"]
        when = (f"{lead}s before" if lead > 0 else "at the same time as" if lead == 0 else f"{-lead}s after")
        return {"key": cause["key"], "label": _label(cause), "confidence": "medium",
                "reason": (f"{_label(cause)} opened {when} the first service symptom ({_label(first_symptom)}). "
                           "Pressure on the host commonly slows or fails the services running on it. "
                           "This is a correlation, not proof.")}

    if first_symptom is not None:
        extra = " The SLO alerts are the user-facing impact of it." if any(layer(a["key"]) == 2 for a in ordered) else ""
        later = [a for a in resources if a["first_ts"] > first_symptom["first_ts"]]
        note = " Host resource alerts opened later, so they look like a consequence rather than the cause." if later else ""
        return {"key": first_symptom["key"], "label": _label(first_symptom), "confidence": "low",
                "reason": f"{_label(first_symptom)} was the first operational symptom to open.{note}{extra}"}

    first = ordered[0]
    return {"key": first["key"], "label": _label(first), "confidence": "low",
            "reason": "Only SLO alerts are open; the symptom that triggered them has already cleared."}


def describe(incident: Dict[str, Any], alerts: List[Dict[str, Any]], now: Optional[float] = None) -> Dict[str, Any]:
    """Full incident view built from its member alerts, so it can never disagree with them."""
    cause = likely_cause(alerts)
    severity = max((a["severity"] for a in alerts), key=lambda s: SEVERITY_RANK[s], default="info")
    timeline: List[Dict[str, Any]] = []
    for a in alerts:
        base = {"key": a["key"], "severity": a["severity"], "message": a["message"]}
        timeline.append({"ts": a["first_ts"], "type": "alert_opened", **base})
        if a["status"] == "resolved" and a["resolved_ts"]:
            timeline.append({"ts": a["resolved_ts"], "type": "alert_resolved", **base})
    timeline.sort(key=lambda e: (e["ts"], 0 if e["type"] == "alert_opened" else 1))

    cause_alert = next((a for a in alerts if cause and a["key"] == cause["key"]), None)
    title = cause_alert["message"] if cause_alert else "Incident"
    if len(title) > 110:
        title = title[:107] + "..."
    if len(alerts) > 1:
        title += f" (+{len(alerts) - 1} related)"

    end = incident["resolved_ts"] if incident["status"] == "resolved" and incident["resolved_ts"] else int(now or time.time())
    return {
        "id": incident["id"], "status": incident["status"], "severity": severity, "title": title,
        "first_ts": incident["first_ts"], "last_ts": incident["last_ts"], "resolved_ts": incident["resolved_ts"],
        "duration_seconds": max(0, int(end - incident["first_ts"])),
        "likely_cause": cause,
        "alerts": [{"key": a["key"], "severity": a["severity"], "message": a["message"], "status": a["status"],
                    "first_ts": a["first_ts"], "resolved_ts": a["resolved_ts"]} for a in alerts],
        "timeline": timeline,
    }


class IncidentManager:
    def __init__(self, db: Database, window_seconds: int = 300, clock: Callable[[], float] = time.time) -> None:
        self.db = db
        self.window = window_seconds
        self.clock = clock

    def update(self) -> List[Dict[str, Any]]:
        """Group newly opened alerts, then open/resolve incidents to match their
        members. Returns incident open/resolve events."""
        events: List[Dict[str, Any]] = []
        now = int(self.clock())
        candidates = self.db.incident_candidates(now - self.window)

        for alert in self.db.ungrouped_open_alerts():
            if not is_groupable(alert["key"]):
                continue
            target = next((c for c in candidates if abs(alert["first_ts"] - c["last_ts"]) <= self.window), None)
            if target is None:
                iid = self.db.create_incident(alert["first_ts"])
                target = {"id": iid, "status": "open", "first_ts": alert["first_ts"],
                          "last_ts": alert["first_ts"], "resolved_ts": None}
                candidates.insert(0, target)
                events.append({"event": "incident_opened", "incident_id": iid})
            elif target["status"] == "resolved":
                self.db.update_incident(target["id"], status="open", resolved_ts=None)
                target.update(status="open", resolved_ts=None)
                events.append({"event": "incident_reopened", "incident_id": target["id"]})
            self.db.set_alert_incident(alert["id"], target["id"])
            new_last = max(target["last_ts"], alert["first_ts"])
            if new_last != target["last_ts"]:
                self.db.update_incident(target["id"], last_ts=new_last)
                target["last_ts"] = new_last

        for inc in self.db.list_incidents("open", 500):
            members = self.db.incident_alerts(inc["id"])
            if not members:
                continue
            stamps = [m["first_ts"] for m in members] + [m["resolved_ts"] for m in members if m["resolved_ts"]]
            last = max(stamps)
            if last != inc["last_ts"]:
                self.db.update_incident(inc["id"], last_ts=last)
            if all(m["status"] == "resolved" for m in members):
                self.db.update_incident(inc["id"], status="resolved",
                                        resolved_ts=max(m["resolved_ts"] for m in members))
                events.append({"event": "incident_resolved", "incident_id": inc["id"]})
        return events

    def view(self, incident_id: int) -> Optional[Dict[str, Any]]:
        inc = self.db.get_incident(incident_id)
        if not inc:
            return None
        return describe(inc, self.db.incident_alerts(incident_id), self.clock())

    def list(self, status: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        return [describe(i, self.db.incident_alerts(i["id"]), self.clock())
                for i in self.db.list_incidents(status, limit)]
