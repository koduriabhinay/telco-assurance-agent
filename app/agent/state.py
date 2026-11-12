"""Agent state and the structured outputs the LLM must return."""

from __future__ import annotations

from typing import Any, TypedDict

from pydantic import BaseModel, Field


class Diagnosis(BaseModel):
    root_cause_node: str = Field(description="id of the network function that is the root cause")
    fault_domain: str = Field(description="RAN, Transport, 5GC, EPC or IMS")
    summary: str = Field(description="2-3 sentence explanation for a NOC engineer")
    evidence: list[str] = Field(default_factory=list, description="alarms and KPI readings that support it")
    runbook_id: str | None = Field(default=None, description="id of the runbook that applies")
    confidence: float = Field(ge=0, le=1)


class ProposedAction(BaseModel):
    action: str = Field(description="action name from the runbook's action list")
    target: str = Field(description="network function id to run it on")
    rationale: str


class RemediationPlan(BaseModel):
    actions: list[ProposedAction] = Field(default_factory=list, max_length=3)


class IncidentState(TypedDict, total=False):
    incident_id: str
    alarm_id: str
    trigger_alarm: dict
    correlated_alarms: list[dict]
    root_candidate: dict
    affected_nodes: list[str]
    kpis: dict[str, dict[str, Any]]
    topology: dict[str, list[dict]]
    rag: dict
    similar_incidents: list[dict]
    diagnosis: dict
    plan: list[dict]
    guardrail_rejections: list[dict]
    approval: dict
    executions: list[dict]
    tried_actions: list[str]
    verification: dict
    status: str
    report: dict
    timeline: list[dict]
