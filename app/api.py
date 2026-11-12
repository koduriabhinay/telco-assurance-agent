"""FastAPI service for the network assurance agent.

POST /incidents                     open an incident for an alarm (runs the agent until done or approval)
GET  /incidents                     list incidents
GET  /incidents/{id}                full state, timeline and trace
POST /incidents/{id}/decision       approve or reject the pending remediation
POST /alarms                        ingest a new alarm (the same path Kafka / Pub/Sub messages take)
GET  /alarms                        active alarms
GET  /graph                         the agent workflow as a Mermaid diagram
GET  /metrics                       Prometheus metrics
GET  /healthz                       liveness
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from app.agent.graph import AssuranceAgent
from app.agent.service import IncidentService
from app.config import get_settings
from app.db import get_store
from app.events import InMemoryBus, alarm_handler, make_bus
from app.observability import metrics


class OpenIncident(BaseModel):
    alarm_id: str = Field(examples=["ALM-1002"])


class Decision(BaseModel):
    approved: bool
    by: str = Field(examples=["noc.engineer@operator.com"])
    comment: str = ""


class AlarmIn(BaseModel):
    id: str
    node_id: str
    severity: Literal["critical", "major", "minor", "warning"]
    code: str
    text: str
    raised_at: str = Field(examples=["2026-10-21T12:00:00Z"])


service: IncidentService | None = None
bus = None


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    global service, bus
    get_store()
    async with contextlib.AsyncExitStack() as stack:
        checkpointer = None
        url = get_settings().database_url
        if url.startswith(("postgresql://", "postgres://")):
            # paused incidents survive restarts and can be approved on any replica
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

            checkpointer = await stack.enter_async_context(AsyncPostgresSaver.from_conn_string(url))
            await checkpointer.setup()
        service = IncidentService(AssuranceAgent(checkpointer=checkpointer))
        bus = make_bus()
        consumer = asyncio.create_task(bus.consume(alarm_handler(service, get_store())))
        yield
        consumer.cancel()


app = FastAPI(title="Telco Assurance Agent", version="1.0.0", lifespan=lifespan,
              description="Agentic RCA and closed-loop remediation for 5G Core, EPC and IMS.")


def _svc() -> IncidentService:
    assert service is not None
    return service


def _public(inc: dict, full: bool = True) -> dict:
    s = inc["state"]
    body = {
        "incident_id": inc["incident_id"], "status": inc["status"], "alarm_id": s.get("alarm_id"),
        "diagnosis": s.get("diagnosis"), "plan": s.get("plan"), "pending_approval": inc["pending_approval"],
        "verification": s.get("verification"), "report": s.get("report"), "timeline": s.get("timeline", []),
    }
    if full:
        body |= {"correlated_alarms": s.get("correlated_alarms"), "kpis": s.get("kpis"),
                 "rag": {k: v for k, v in (s.get("rag") or {}).items() if k != "chunks"} | {
                     "chunks": [{"id": c["id"], "score": c["score"]} for c in (s.get("rag") or {}).get("chunks", [])]},
                 "guardrail_rejections": s.get("guardrail_rejections", []), "trace": inc["trace"]}
    return body


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "llm_provider": get_settings().llm_provider}


@app.get("/alarms")
def alarms() -> list[dict]:
    return get_store().active_alarms()


@app.post("/alarms", status_code=202)
async def ingest_alarm(alarm: AlarmIn) -> dict:
    await bus.publish(alarm.model_dump())
    if isinstance(bus, InMemoryBus):
        await bus.queue.join()  # in-memory mode: wait so the caller sees the incident straight away
    inc_id = _svc().by_alarm.get(alarm.id)
    return {"accepted": True, "incident_id": inc_id}


@app.post("/incidents")
async def open_incident(req: OpenIncident) -> dict:
    if not get_store().get_alarm(req.alarm_id):
        raise HTTPException(404, f"alarm {req.alarm_id} not found")
    return _public(await _svc().open_incident(req.alarm_id), full=False)


@app.get("/incidents")
async def list_incidents() -> list[dict]:
    return await _svc().list()


@app.get("/incidents/{incident_id}")
async def get_incident(incident_id: str) -> dict:
    try:
        return _public(await _svc().get(incident_id))
    except KeyError:
        raise HTTPException(404, "incident not found") from None


@app.post("/incidents/{incident_id}/decision")
async def decide(incident_id: str, d: Decision) -> dict:
    try:
        return _public(await _svc().decide(incident_id, d.approved, d.by, d.comment), full=False)
    except KeyError:
        raise HTTPException(404, "incident not found") from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@app.get("/graph", response_class=PlainTextResponse)
def graph() -> str:
    return _svc().agent.mermaid()


@app.get("/metrics", response_class=PlainTextResponse)
def prometheus() -> str:
    return metrics.render()
