from fastapi.testclient import TestClient

from app.api import app


def test_api_end_to_end_and_memory():
    with TestClient(app) as client:
        assert client.get("/healthz").json()["status"] == "ok"
        assert len(client.get("/alarms").json()) == 14

        inc = client.post("/incidents", json={"alarm_id": "ALM-2002"}).json()
        assert inc["status"] == "awaiting_approval"
        full = client.get(f"/incidents/{inc['incident_id']}").json()
        assert full["rag"]["best_runbook"] == "RB-UPF-002"
        assert any(s["span"].startswith("mcp.") for s in full["trace"])

        done = client.post(f"/incidents/{inc['incident_id']}/decision",
                           json={"approved": True, "by": "noc@op.com"}).json()
        assert done["status"] == "resolved"

        # the same fault comes back later: ingest a new alarm through the event path
        r = client.post("/alarms", json={"id": "ALM-9001", "node_id": "upf-01", "severity": "major",
                                         "code": "CPU_OVERLOAD", "text": "UPF CPU above 95%",
                                         "raised_at": "2026-10-21T12:30:00Z"}).json()
        assert r["incident_id"]
        again = client.get(f"/incidents/{r['incident_id']}").json()
        assert again["diagnosis"]["root_cause_node"] == "upf-01"
        timeline = " ".join(e["detail"] for e in again["timeline"])
        assert "1 similar past incident" in timeline

        assert client.post("/incidents", json={"alarm_id": "nope"}).status_code == 404
        assert "agent_span_seconds_bucket" in client.get("/metrics").text
        assert "approval_gate" in client.get("/graph").text
