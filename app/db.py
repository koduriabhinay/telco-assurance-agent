"""Operational store for alarms, topology, KPIs, incidents and the action audit log.

SQLite is the default (zero setup). Set DATABASE_URL=postgresql://... to use PostgreSQL;
the same SQL runs on both, only the parameter placeholder differs.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import DATA_DIR

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS nodes (
        id TEXT PRIMARY KEY, type TEXT, domain TEXT, site TEXT, vendor TEXT)""",
    """CREATE TABLE IF NOT EXISTS links (
        a TEXT, b TEXT, interface TEXT)""",
    """CREATE TABLE IF NOT EXISTS alarms (
        id TEXT PRIMARY KEY, node_id TEXT, severity TEXT, code TEXT, text TEXT,
        raised_at TEXT, cleared_at TEXT, scenario TEXT)""",
    """CREATE TABLE IF NOT EXISTS kpis (
        node_id TEXT, kpi TEXT, ts TEXT, value REAL)""",
    """CREATE TABLE IF NOT EXISTS incidents (
        id TEXT PRIMARY KEY, alarm_id TEXT, status TEXT, summary TEXT,
        root_cause_node TEXT, report TEXT, created_at TEXT, updated_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS action_log (
        incident_id TEXT, action TEXT, target TEXT, approved_by TEXT, result TEXT, ts TEXT)""",
]

# Catalog of remediation actions the agent may propose. Risk drives the approval policy.
ACTION_CATALOG: dict[str, dict[str, str]] = {
    "reroute_backhaul_path": {"risk": "low", "desc": "Switch CSR uplink to protection path"},
    "open_field_ticket": {"risk": "none", "desc": "Dispatch field operations"},
    "scale_out_upf": {"risk": "low", "desc": "Add a UPF data-plane replica"},
    "drain_upf_pod": {"risk": "medium", "desc": "Move sessions off a hot UPF pod"},
    "restart_upf_pod": {"risk": "high", "desc": "Restart a UPF pod (drops sessions)"},
    "failover_diameter_peer": {"risk": "low", "desc": "Fail over to secondary Diameter peer"},
    "restart_diameter_stack": {"risk": "medium", "desc": "Restart the Diameter stack"},
    "update_dra_route": {"risk": "medium", "desc": "Fix realm routing entry on the DRA"},
    "enable_overload_control": {"risk": "low", "desc": "Send NGAP Overload Start"},
    "scale_out_amf": {"risk": "low", "desc": "Add AMF set members"},
}
RISK_ORDER = ["none", "low", "medium", "high"]

# Simulator ground truth: which actions fix which scenario
_EFFECTIVE = {
    "n2-transport": {"reroute_backhaul_path"},
    "upf-overload": {"scale_out_upf", "drain_upf_pod", "restart_upf_pod"},
    "ims-cx": {"failover_diameter_peer", "restart_diameter_stack"},
    "epc-s6a": {"failover_diameter_peer", "update_dra_route"},
}

# (node, kpi, healthy, degraded, scenario)
_KPI_PROFILE = [
    ("amf-01", "registration_success_rate", 99.6, 61.0, "n2-transport"),
    ("gnb-blr-101", "cell_availability", 100.0, 0.0, "n2-transport"),
    ("gnb-blr-102", "cell_availability", 100.0, 0.0, "n2-transport"),
    ("csr-blr-01", "uplink_oper_status", 1.0, 0.0, "n2-transport"),
    ("upf-01", "cpu_util", 55.0, 97.0, "upf-overload"),
    ("upf-01", "throughput_gbps", 38.0, 39.5, "upf-overload"),
    ("smf-01", "pdu_session_success_rate", 99.3, 80.5, "upf-overload"),
    ("hss-ims-01", "cx_peer_up", 1.0, 0.0, "ims-cx"),
    ("scscf-01", "cx_latency_ms", 11.0, 3000.0, "ims-cx"),
    ("pcscf-01", "sip_503_rate", 0.3, 14.2, "ims-cx"),
    ("pcscf-01", "ims_registration_success_rate", 99.7, 84.0, "ims-cx"),
    ("dra-01", "s6a_route_success", 100.0, 38.0, "epc-s6a"),
    ("mme-01", "attach_success_rate", 99.4, 83.0, "epc-s6a"),
]

_START = datetime(2026, 10, 21, 7, 0, tzinfo=timezone.utc)
_STEP = timedelta(minutes=5)
_SAMPLES = 60  # 07:00 to 11:55


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, url: str = "sqlite:///:memory:") -> None:
        self.url = url
        self._lock = threading.RLock()
        if url.startswith("postgresql://") or url.startswith("postgres://"):
            import psycopg  # optional dependency, installed in the Docker image

            self._conn = psycopg.connect(url, autocommit=True)
            self._ph = "%s"
        else:
            path = url.removeprefix("sqlite:///") or ":memory:"
            self._conn = sqlite3.connect(path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._ph = "?"

    # -- low-level helpers -------------------------------------------------
    def execute(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(sql.replace("?", self._ph), params)
            if isinstance(self._conn, sqlite3.Connection):
                self._conn.commit()

    def query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(sql.replace("?", self._ph), params)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    # -- seeding -----------------------------------------------------------
    def init(self, reset: bool = True) -> None:
        if reset:
            for t in ("nodes", "links", "alarms", "kpis", "incidents", "action_log"):
                self.execute(f"DROP TABLE IF EXISTS {t}")
        for stmt in SCHEMA:
            self.execute(stmt)
        if self.query("SELECT COUNT(*) AS n FROM nodes")[0]["n"]:
            return
        topo = json.loads((DATA_DIR / "topology.json").read_text())
        for n in topo["nodes"]:
            self.execute("INSERT INTO nodes VALUES (?,?,?,?,?)",
                         (n["id"], n["type"], n["domain"], n["site"], n["vendor"]))
        for link in topo["links"]:
            self.execute("INSERT INTO links VALUES (?,?,?)", (link["a"], link["b"], link["interface"]))
        alarms = json.loads((DATA_DIR / "alarms.json").read_text())
        onset = {}
        for a in alarms:
            self.execute("INSERT INTO alarms VALUES (?,?,?,?,?,?,?,?)",
                         (a["id"], a["node_id"], a["severity"], a["code"], a["text"],
                          a["raised_at"], None, a["scenario"]))
            onset.setdefault(a["scenario"], a["raised_at"])
        for node, kpi, good, bad, scenario in _KPI_PROFILE:
            start = datetime.strptime(onset[scenario], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            for i in range(_SAMPLES):
                ts = _START + i * _STEP
                jitter = ((i * 7919) % 13 - 6) / 100.0 * max(abs(good), 1) * 0.01
                value = bad if ts >= start - timedelta(minutes=1) else good + jitter
                self.execute("INSERT INTO kpis VALUES (?,?,?,?)", (node, kpi, _iso(ts), round(value, 3)))

    # -- domain queries ----------------------------------------------------
    def get_alarm(self, alarm_id: str) -> dict | None:
        rows = self.query("SELECT * FROM alarms WHERE id = ?", (alarm_id,))
        return rows[0] if rows else None

    def active_alarms(self, node_id: str | None = None) -> list[dict]:
        if node_id:
            return self.query("SELECT * FROM alarms WHERE cleared_at IS NULL AND node_id = ? ORDER BY raised_at",
                              (node_id,))
        return self.query("SELECT * FROM alarms WHERE cleared_at IS NULL ORDER BY raised_at")

    def alarms_in_window(self, center: str, window_s: int) -> list[dict]:
        c = datetime.strptime(center, "%Y-%m-%dT%H:%M:%SZ")
        lo, hi = _iso(c - timedelta(seconds=window_s)), _iso(c + timedelta(seconds=window_s))
        return self.query("SELECT * FROM alarms WHERE raised_at BETWEEN ? AND ? AND cleared_at IS NULL "
                          "ORDER BY raised_at", (lo, hi))

    def node(self, node_id: str) -> dict | None:
        rows = self.query("SELECT * FROM nodes WHERE id = ?", (node_id,))
        return rows[0] if rows else None

    def neighbors(self, node_id: str) -> list[dict]:
        rows = self.query("SELECT a, b, interface FROM links WHERE a = ? OR b = ?", (node_id, node_id))
        out = []
        for r in rows:
            other = r["b"] if r["a"] == node_id else r["a"]
            out.append({"node_id": other, "interface": r["interface"], **(self.node(other) or {})})
        return out

    def kpi_series(self, node_id: str, kpi: str | None = None, last_n: int = 12) -> list[dict]:
        if kpi:
            rows = self.query("SELECT * FROM kpis WHERE node_id = ? AND kpi = ? ORDER BY ts DESC LIMIT ?",
                              (node_id, kpi, last_n))
        else:
            rows = self.query("SELECT * FROM kpis WHERE node_id = ? ORDER BY ts DESC", (node_id,))
        return list(reversed(rows))

    def kpi_names(self, node_id: str) -> list[str]:
        return [r["kpi"] for r in self.query("SELECT DISTINCT kpi FROM kpis WHERE node_id = ?", (node_id,))]

    def insert_alarm(self, alarm: dict) -> dict:
        row = {"cleared_at": None, "scenario": None, **alarm}
        self.execute("INSERT INTO alarms VALUES (?,?,?,?,?,?,?,?)",
                     (row["id"], row["node_id"], row["severity"], row["code"], row["text"],
                      row["raised_at"], row["cleared_at"], row["scenario"]))
        return row

    # -- network simulator -------------------------------------------------
    def apply_action(self, action: str, target: str, scenario: str | None) -> dict:
        """Simulate executing a remediation action on the network."""
        effective = scenario is not None and action in _EFFECTIVE.get(scenario, set())
        if effective:
            last = self.query("SELECT MAX(ts) AS ts FROM kpis")[0]["ts"]
            ts = _iso(datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) + _STEP)
            for node, kpi, good, _bad, sc in _KPI_PROFILE:
                if sc == scenario:
                    self.execute("INSERT INTO kpis VALUES (?,?,?,?)", (node, kpi, ts, good))
            self.execute("UPDATE alarms SET cleared_at = ? WHERE scenario = ? AND cleared_at IS NULL",
                         (ts, scenario))
        return {"action": action, "target": target, "effective": effective}

    # -- incidents ---------------------------------------------------------
    def upsert_incident(self, inc: dict) -> None:
        now = _iso(datetime.now(timezone.utc))
        if self.query("SELECT id FROM incidents WHERE id = ?", (inc["id"],)):
            self.execute("UPDATE incidents SET status=?, summary=?, root_cause_node=?, report=?, updated_at=? "
                         "WHERE id=?", (inc["status"], inc.get("summary"), inc.get("root_cause_node"),
                                        json.dumps(inc.get("report")), now, inc["id"]))
        else:
            self.execute("INSERT INTO incidents VALUES (?,?,?,?,?,?,?,?)",
                         (inc["id"], inc["alarm_id"], inc["status"], inc.get("summary"),
                          inc.get("root_cause_node"), json.dumps(inc.get("report")), now, now))

    def resolved_incidents(self) -> list[dict]:
        rows = self.query("SELECT * FROM incidents WHERE status = 'resolved' ORDER BY updated_at DESC")
        for r in rows:
            r["report"] = json.loads(r["report"]) if r["report"] else None
        return rows

    def log_action(self, incident_id: str, action: str, target: str, approved_by: str, result: str) -> None:
        self.execute("INSERT INTO action_log VALUES (?,?,?,?,?,?)",
                     (incident_id, action, target, approved_by, result, _iso(datetime.now(timezone.utc))))

    def readonly_sql(self, sql: str, limit: int = 50) -> list[dict]:
        """Run an analyst query. Only single SELECT statements are allowed."""
        s = sql.strip().rstrip(";")
        banned = r"\b(insert|update|delete|drop|alter|create|attach|pragma|grant|truncate|copy)\b"
        if ";" in s or not s.lower().startswith("select") or re.search(banned, s, re.IGNORECASE):
            raise ValueError("Only single read-only SELECT statements are allowed")
        return self.query(f"SELECT * FROM ({s}) AS q LIMIT {int(limit)}")


_store: Store | None = None


def get_store() -> Store:
    global _store
    if _store is None:
        import os

        from app.config import get_settings

        _store = Store(get_settings().database_url)
        # RESET_DB=false lets several processes (API, MCP server) share one seeded PostgreSQL database
        _store.init(reset=os.getenv("RESET_DB", "true").lower() == "true")
    return _store


def reset_store() -> Store:
    global _store
    _store = None
    return get_store()
