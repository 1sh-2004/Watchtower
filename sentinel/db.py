"""SQLite persistence. One short-lived connection per call keeps this safe to
use from worker threads (collectors run via asyncio.to_thread)."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS system_metrics (
    ts INTEGER NOT NULL,
    cpu REAL, ram REAL, disk REAL,
    disk_read_bps REAL, disk_write_bps REAL,
    net_up_bps REAL, net_down_bps REAL,
    gpu_util REAL
);
CREATE INDEX IF NOT EXISTS idx_system_ts ON system_metrics(ts);

CREATE TABLE IF NOT EXISTS service_checks (
    ts INTEGER NOT NULL,
    name TEXT NOT NULL,
    ok INTEGER NOT NULL,
    status_code INTEGER,
    latency_ms REAL,
    error TEXT,
    rl_limit INTEGER, rl_remaining INTEGER, rl_reset INTEGER
);
CREATE INDEX IF NOT EXISTS idx_checks ON service_checks(name, ts);

CREATE TABLE IF NOT EXISTS cloud_snapshots (
    ts INTEGER NOT NULL,
    provider TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cloud ON cloud_snapshots(provider, ts);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    source TEXT NOT NULL,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    status TEXT NOT NULL,
    first_ts INTEGER NOT NULL,
    last_ts INTEGER NOT NULL,
    resolved_ts INTEGER,
    count INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_alerts_key ON alerts(key, status);

CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL,          -- open | resolved
    first_ts INTEGER NOT NULL,
    last_ts INTEGER NOT NULL,      -- latest member alert opened or resolved
    resolved_ts INTEGER
);
"""

# Columns added after v2. Applied to existing databases on startup.
MIGRATIONS = [
    ("alerts", "incident_id", "ALTER TABLE alerts ADD COLUMN incident_id INTEGER"),
]


class Database:
    def __init__(self, path: str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._memory_conn: Optional[sqlite3.Connection] = None
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            for table, column, ddl in MIGRATIONS:
                cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
                if column not in cols:
                    conn.execute(ddl)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        # In-memory databases vanish when the connection closes, so share one.
        if self.path == ":memory:":
            if self._memory_conn is None:
                self._memory_conn = sqlite3.connect(":memory:", check_same_thread=False)
                self._memory_conn.row_factory = sqlite3.Row
            yield self._memory_conn
            self._memory_conn.commit()
            return
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---- system metrics -------------------------------------------------
    def insert_system(self, s: Dict[str, Any]) -> None:
        gpus = s.get("gpus") or []
        gpu_util = max((g["util_percent"] for g in gpus if g.get("util_percent") is not None), default=None)
        with self._conn() as c:
            c.execute(
                "INSERT INTO system_metrics VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    int(s["ts"]),
                    s["cpu"]["percent"],
                    s["ram"]["percent"],
                    s["disk"]["percent"],
                    s["disk"].get("read_bps"),
                    s["disk"].get("write_bps"),
                    s["network"].get("up_bps"),
                    s["network"].get("down_bps"),
                    gpu_util,
                ),
            )

    def system_history(self, hours: int, now: Optional[float] = None, max_points: int = 600) -> Dict[str, List[Any]]:
        """History averaged into at most ~max_points buckets, so a 7-day window
        does not ship hundreds of thousands of rows to the browser."""
        since = int((now or time.time()) - hours * 3600)
        bucket = max(1, (hours * 3600) // max_points)
        cols = ["cpu", "ram", "disk", "disk_read_bps", "disk_write_bps", "net_up_bps", "net_down_bps", "gpu_util"]
        avgs = ", ".join(f"AVG({c}) AS {c}" for c in cols)
        with self._conn() as c:
            rows = c.execute(
                f"SELECT (ts / ?) * ? AS t, {avgs} FROM system_metrics WHERE ts > ? GROUP BY ts / ? ORDER BY t",
                (bucket, bucket, since, bucket),
            ).fetchall()

        def clean(v: Optional[float]) -> Optional[float]:
            return None if v is None else round(v, 2)

        out: Dict[str, List[Any]] = {col: [clean(r[col]) for r in rows] for col in cols}
        out["timestamps"] = [r["t"] for r in rows]
        return out

    # ---- service checks -------------------------------------------------
    def insert_check(self, r: Dict[str, Any]) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO service_checks VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    int(r["ts"]), r["name"], 1 if r["ok"] else 0, r.get("status_code"),
                    r.get("latency_ms"), r.get("error"),
                    r.get("rl_limit"), r.get("rl_remaining"), r.get("rl_reset"),
                ),
            )

    def service_stats(self, name: str, hours: int, now: Optional[float] = None) -> Dict[str, Any]:
        since = int((now or time.time()) - hours * 3600)
        with self._conn() as c:
            rows = c.execute(
                "SELECT ok, status_code, latency_ms FROM service_checks WHERE name=? AND ts>? ORDER BY ts",
                (name, since),
            ).fetchall()
        total = len(rows)
        failures = sum(1 for r in rows if not r["ok"])
        http_errors = sum(1 for r in rows if r["status_code"] is not None and r["status_code"] >= 400)
        rate_limited = sum(1 for r in rows if r["status_code"] == 429)
        lat = sorted(r["latency_ms"] for r in rows if r["latency_ms"] is not None and r["ok"])
        p95 = lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else None
        return {
            "window_hours": hours,
            "checks": total,
            "failures": failures,
            "http_errors": http_errors,
            "rate_limited": rate_limited,
            "uptime_percent": round(100 * (total - failures) / total, 2) if total else None,
            "avg_latency_ms": round(sum(lat) / len(lat), 1) if lat else None,
            "p95_latency_ms": round(p95, 1) if p95 is not None else None,
        }

    def check_counts(self, name: str, windows: Dict[str, int], now: Optional[float] = None) -> Dict[str, Dict[str, int]]:
        """Total and failed check counts for several trailing windows (label -> seconds),
        in a single query."""
        now_i = int(now or time.time())
        labels = list(windows)
        sums = ", ".join(
            f"SUM(CASE WHEN ts > ? THEN 1 ELSE 0 END), SUM(CASE WHEN ts > ? AND ok = 0 THEN 1 ELSE 0 END)"
            for _ in labels)
        args: List[Any] = []
        for label in labels:
            since = now_i - windows[label]
            args += [since, since]
        args += [name, now_i - max(windows.values())]
        with self._conn() as c:
            row = c.execute(f"SELECT {sums} FROM service_checks WHERE name=? AND ts>?", args).fetchone()
        out = {}
        for i, label in enumerate(labels):
            out[label] = {"total": int(row[2 * i] or 0), "bad": int(row[2 * i + 1] or 0)}
        return out

    def service_latency_series(self, name: str, hours: int, now: Optional[float] = None) -> Dict[str, List[Any]]:
        since = int((now or time.time()) - hours * 3600)
        with self._conn() as c:
            rows = c.execute(
                "SELECT ts, latency_ms, ok FROM service_checks WHERE name=? AND ts>? ORDER BY ts", (name, since)
            ).fetchall()
        return {"timestamps": [r["ts"] for r in rows], "latency_ms": [r["latency_ms"] for r in rows],
                "ok": [bool(r["ok"]) for r in rows]}

    # ---- cloud ----------------------------------------------------------
    def save_cloud(self, provider: str, data: Dict[str, Any], ts: Optional[int] = None) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO cloud_snapshots VALUES (?,?,?)",
                (int(ts or time.time()), provider, json.dumps(data)),
            )

    def latest_cloud(self, provider: str) -> Optional[Dict[str, Any]]:
        with self._conn() as c:
            row = c.execute(
                "SELECT ts, data FROM cloud_snapshots WHERE provider=? ORDER BY ts DESC LIMIT 1", (provider,)
            ).fetchone()
        if not row:
            return None
        data = json.loads(row["data"])
        data["collected_at"] = row["ts"]
        return data

    # ---- alerts ---------------------------------------------------------
    def get_open_alert(self, key: str) -> Optional[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM alerts WHERE key=? AND status='open' ORDER BY id DESC LIMIT 1", (key,)
            ).fetchone()

    def open_alert_keys(self, prefix: str) -> List[str]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT key FROM alerts WHERE status='open' AND key LIKE ?", (prefix + "%",)
            ).fetchall()
        return [r["key"] for r in rows]

    def create_alert(self, key: str, source: str, severity: str, message: str, ts: int) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO alerts (key, source, severity, message, status, first_ts, last_ts) "
                "VALUES (?,?,?,?, 'open', ?, ?)",
                (key, source, severity, message, ts, ts),
            )
            return int(cur.lastrowid)

    def touch_alert(self, alert_id: int, ts: int, severity: str, message: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE alerts SET last_ts=?, severity=?, message=?, count=count+1 WHERE id=?",
                (ts, severity, message, alert_id),
            )

    def resolve_alert(self, alert_id: int, ts: int) -> None:
        with self._conn() as c:
            c.execute("UPDATE alerts SET status='resolved', resolved_ts=? WHERE id=?", (ts, alert_id))

    def list_alerts(self, status: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        q = "SELECT * FROM alerts"
        args: List[Any] = []
        if status:
            q += " WHERE status=?"
            args.append(status)
        q += " ORDER BY last_ts DESC LIMIT ?"
        args.append(limit)
        with self._conn() as c:
            return [dict(r) for r in c.execute(q, args).fetchall()]

    # ---- incidents ------------------------------------------------------
    def ungrouped_open_alerts(self) -> List[Dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM alerts WHERE status='open' AND incident_id IS NULL ORDER BY first_ts, id").fetchall()
        return [dict(r) for r in rows]

    def set_alert_incident(self, alert_id: int, incident_id: int) -> None:
        with self._conn() as c:
            c.execute("UPDATE alerts SET incident_id=? WHERE id=?", (incident_id, alert_id))

    def create_incident(self, ts: int) -> int:
        with self._conn() as c:
            cur = c.execute("INSERT INTO incidents (status, first_ts, last_ts) VALUES ('open', ?, ?)", (ts, ts))
            return int(cur.lastrowid)

    def update_incident(self, incident_id: int, **fields: Any) -> None:
        allowed = {"status", "last_ts", "resolved_ts"}
        if not fields or not set(fields) <= allowed:
            raise ValueError(f"can only update {sorted(allowed)}")
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._conn() as c:
            c.execute(f"UPDATE incidents SET {sets} WHERE id=?", (*fields.values(), incident_id))

    def incident_candidates(self, since_ts: int) -> List[Dict[str, Any]]:
        """Open incidents, plus ones resolved recently enough that a flapping alert may reopen them."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM incidents WHERE status='open' OR (resolved_ts IS NOT NULL AND resolved_ts >= ?) "
                "ORDER BY last_ts DESC", (since_ts,)).fetchall()
        return [dict(r) for r in rows]

    def list_incidents(self, status: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        q, args = "SELECT * FROM incidents", []
        if status:
            q += " WHERE status=?"
            args.append(status)
        q += " ORDER BY first_ts DESC LIMIT ?"
        args.append(limit)
        with self._conn() as c:
            return [dict(r) for r in c.execute(q, args).fetchall()]

    def get_incident(self, incident_id: int) -> Optional[Dict[str, Any]]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
        return dict(row) if row else None

    def incident_alerts(self, incident_id: int) -> List[Dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM alerts WHERE incident_id=? ORDER BY first_ts, id", (incident_id,)).fetchall()
        return [dict(r) for r in rows]

    # ---- housekeeping ---------------------------------------------------
    def prune(self, retention_days: int, now: Optional[float] = None) -> None:
        cutoff = int((now or time.time()) - retention_days * 86400)
        with self._conn() as c:
            c.execute("DELETE FROM system_metrics WHERE ts < ?", (cutoff,))
            c.execute("DELETE FROM service_checks WHERE ts < ?", (cutoff,))
            c.execute("DELETE FROM cloud_snapshots WHERE ts < ?", (cutoff,))
            c.execute("DELETE FROM alerts WHERE status='resolved' AND resolved_ts < ?", (cutoff,))
            c.execute("DELETE FROM incidents WHERE status='resolved' AND resolved_ts < ?", (cutoff,))

    def ping(self) -> bool:
        try:
            with self._conn() as c:
                c.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
