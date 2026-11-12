"""Runs incidents through the agent graph and handles pause / resume for human approval."""

from __future__ import annotations

import time
import uuid
from typing import Any

from langgraph.types import Command

from app.agent.graph import AssuranceAgent
from app.observability import metrics, tracer


class IncidentService:
    def __init__(self, agent: AssuranceAgent | None = None) -> None:
        self.agent = agent or AssuranceAgent()
        self.traces: dict[str, list[dict]] = {}
        self.by_alarm: dict[str, str] = {}

    @staticmethod
    def _config(incident_id: str) -> dict:
        return {"configurable": {"thread_id": incident_id}}

    async def _run(self, incident_id: str, payload: Any) -> dict:
        start = time.perf_counter()
        with tracer.trace() as spans:
            await self.agent.graph.ainvoke(payload, self._config(incident_id))
        self.traces.setdefault(incident_id, []).extend(spans)
        metrics.observe("incident_run_seconds", time.perf_counter() - start)
        return await self.get(incident_id)

    async def open_incident(self, alarm_id: str) -> dict:
        """Start a new incident for an alarm. Alarms already part of an open incident are deduplicated."""
        if alarm_id in self.by_alarm:
            return await self.get(self.by_alarm[alarm_id])
        incident_id = f"INC-{uuid.uuid4().hex[:8]}"
        self.by_alarm[alarm_id] = incident_id
        result = await self._run(incident_id, {"incident_id": incident_id, "alarm_id": alarm_id, "timeline": []})
        if result["status"] == "awaiting_approval":
            metrics.inc("approvals_requested", risk=result["pending_approval"]["action"]["risk"])
        for a in result["state"].get("correlated_alarms", []):
            self.by_alarm.setdefault(a["id"], incident_id)
        return result

    async def decide(self, incident_id: str, approved: bool, by: str, comment: str = "") -> dict:
        current = await self.get(incident_id)
        if current["status"] != "awaiting_approval":
            raise ValueError(f"incident {incident_id} is not waiting for approval (status {current['status']})")
        return await self._run(incident_id, Command(resume={"approved": approved, "by": by, "comment": comment}))

    async def get(self, incident_id: str) -> dict:
        snap = await self.agent.graph.aget_state(self._config(incident_id))
        if not snap.values:
            raise KeyError(incident_id)
        state = dict(snap.values)
        pending = [i.value for t in snap.tasks for i in getattr(t, "interrupts", ())]
        status = "awaiting_approval" if pending else state.get("status", "running")
        return {"incident_id": incident_id, "status": status, "pending_approval": pending[0] if pending else None,
                "state": state, "trace": self.traces.get(incident_id, [])}

    async def list(self) -> list[dict]:
        out = []
        for incident_id in dict.fromkeys(self.by_alarm.values()):
            inc = await self.get(incident_id)
            s = inc["state"]
            out.append({"incident_id": incident_id, "status": inc["status"], "alarm_id": s.get("alarm_id"),
                        "root_cause_node": s.get("diagnosis", {}).get("root_cause_node")})
        return out
