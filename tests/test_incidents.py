from sentinel.alerts import AlertEngine
from sentinel.incidents import IncidentManager, likely_cause, describe


class Clock:
    def __init__(self, t=1_000_000):
        self.t = t

    def __call__(self):
        return self.t


def setup(db, window=300):
    clock = Clock()
    return clock, AlertEngine(db, clock), IncidentManager(db, window, clock)


def fire(engine, key, sev="warning", msg=None, source="service"):
    return engine.set_state(key, True, sev, source, msg or key)


def test_related_alerts_become_one_incident_with_a_timeline(db):
    clock, eng, mgr = setup(db)
    fire(eng, "sys:cpu", "critical", "CPU usage 97%", "system"); mgr.update()
    clock.t += 20
    fire(eng, "svc:api:slow", "warning", "api latency 900ms"); mgr.update()
    clock.t += 15
    fire(eng, "svc:api:down", "critical", "api is failing"); mgr.update()
    clock.t += 10
    fire(eng, "slo:api:fast_burn", "critical", "api burning budget", "slo"); mgr.update()

    incidents = mgr.list()
    assert len(incidents) == 1
    inc = incidents[0]
    assert inc["status"] == "open" and inc["severity"] == "critical"
    assert [a["key"] for a in inc["alerts"]] == ["sys:cpu", "svc:api:slow", "svc:api:down", "slo:api:fast_burn"]
    assert [e["type"] for e in inc["timeline"]] == ["alert_opened"] * 4
    assert inc["likely_cause"]["key"] == "sys:cpu" and inc["likely_cause"]["confidence"] == "medium"
    assert "correlation" in inc["likely_cause"]["reason"]
    assert "(+3 related)" in inc["title"]


def test_alerts_far_apart_are_separate_incidents(db):
    clock, eng, mgr = setup(db, window=300)
    fire(eng, "svc:a:down", "critical"); mgr.update()
    clock.t += 900
    fire(eng, "svc:b:down", "critical"); mgr.update()
    assert len(mgr.list()) == 2


def test_incident_resolves_only_when_every_member_resolves(db):
    clock, eng, mgr = setup(db)
    fire(eng, "sys:cpu"); fire(eng, "svc:api:slow"); mgr.update()
    clock.t += 30
    eng.set_state("sys:cpu", False); mgr.update()
    assert mgr.list()[0]["status"] == "open"
    clock.t += 30
    eng.set_state("svc:api:slow", False); events = mgr.update()
    inc = mgr.list()[0]
    assert inc["status"] == "resolved" and inc["resolved_ts"] == clock.t
    assert [e["event"] for e in events] == ["incident_resolved"]
    assert [e["type"] for e in inc["timeline"]].count("alert_resolved") == 2
    assert inc["duration_seconds"] == 60


def test_flapping_alert_reopens_the_incident_instead_of_creating_a_new_one(db):
    clock, eng, mgr = setup(db, window=300)
    fire(eng, "svc:api:down", "critical"); mgr.update()
    clock.t += 60
    eng.set_state("svc:api:down", False); mgr.update()
    assert mgr.list()[0]["status"] == "resolved"
    clock.t += 90
    fire(eng, "svc:api:down", "critical"); events = mgr.update()
    incidents = mgr.list()
    assert len(incidents) == 1 and incidents[0]["status"] == "open"
    assert [e["event"] for e in events] == ["incident_reopened"]
    assert len(incidents[0]["alerts"]) == 2          # the first and second firing of the same alert
    # once the window passes, a new failure is a new incident
    eng.set_state("svc:api:down", False); mgr.update()
    clock.t += 1000
    fire(eng, "svc:api:down", "critical"); mgr.update()
    assert len(mgr.list()) == 2


def test_housekeeping_alerts_are_never_grouped(db):
    clock, eng, mgr = setup(db)
    fire(eng, "key:api:expiry", "warning", source="service")
    fire(eng, "aws:idle-ec2", "warning", source="cloud")
    fire(eng, "svc:api:down", "critical")
    mgr.update()
    inc = mgr.list()
    assert len(inc) == 1 and [a["key"] for a in inc[0]["alerts"]] == ["svc:api:down"]


def test_update_is_idempotent(db):
    clock, eng, mgr = setup(db)
    fire(eng, "sys:cpu"); fire(eng, "svc:api:slow")
    first = mgr.update()
    assert [e["event"] for e in first] == ["incident_opened"]
    assert mgr.update() == [] and mgr.update() == []
    assert len(mgr.list()) == 1


def A(id_, key, ts, status="open"):
    return {"id": id_, "key": key, "first_ts": ts, "status": status, "resolved_ts": None,
            "severity": "warning", "message": key}


def test_cause_ranking_rules():
    # resource first -> resource
    assert likely_cause([A(1, "sys:cpu", 100), A(2, "svc:api:slow", 130)])["key"] == "sys:cpu"
    # resource opens shortly after the symptom (debounce skew) -> still candidate
    assert likely_cause([A(1, "svc:api:slow", 100), A(2, "sys:cpu", 150)])["key"] == "sys:cpu"
    # resource opens long after -> looks like a consequence, the service symptom wins
    c = likely_cause([A(1, "svc:api:down", 100), A(2, "sys:cpu", 400)])
    assert c["key"] == "svc:api:down" and "consequence" in c["reason"]
    # SLO alerts are impact, not cause
    assert likely_cause([A(1, "slo:api:fast_burn", 90), A(2, "svc:api:down", 100)])["key"] == "svc:api:down"
    # only SLO alerts left -> says so, low confidence
    c = likely_cause([A(1, "slo:api:exhausted", 90)])
    assert c["confidence"] == "low"
    # confidence is never 'high'
    assert likely_cause([A(1, "sys:cpu", 100), A(2, "svc:a:slow", 110), A(3, "slo:a:fast_burn", 120)])["confidence"] == "medium"
    assert likely_cause([]) is None


def test_describe_matches_members(db):
    clock, eng, mgr = setup(db)
    fire(eng, "svc:api:down", "critical", "api is failing")
    mgr.update()
    inc = mgr.view(1)
    assert inc["title"] == "api is failing" and inc["alerts"][0]["status"] == "open"
    assert mgr.view(999) is None


def test_old_database_is_migrated(tmp_path):
    import sqlite3
    from sentinel.db import Database
    path = str(tmp_path / "v2.db")
    conn = sqlite3.connect(path)
    conn.executescript("""CREATE TABLE alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL,
        source TEXT NOT NULL, severity TEXT NOT NULL, message TEXT NOT NULL, status TEXT NOT NULL,
        first_ts INTEGER NOT NULL, last_ts INTEGER NOT NULL, resolved_ts INTEGER, count INTEGER NOT NULL DEFAULT 1);
        INSERT INTO alerts (key, source, severity, message, status, first_ts, last_ts)
        VALUES ('svc:x:down','service','critical','old','resolved',1,1);""")
    conn.commit(); conn.close()
    db = Database(path)                      # must not raise, and must keep old rows
    assert db.list_alerts()[0]["message"] == "old"
    Database(path)                           # running the migration twice is harmless
    assert db.ungrouped_open_alerts() == []
