"""LangGraph workflow for alarm triage, root-cause analysis and closed-loop remediation.

triage -> gather_context -> retrieve_knowledge -> diagnose -> plan_remediation -> approval_gate
       -> execute -> verify -> (resolved: report | not healthy: plan_remediation again | give up: report)

The approval gate calls `interrupt()`, which pauses the graph and saves its state in the checkpointer.
The API resumes it with `Command(resume={...})` when an engineer approves or rejects.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.agent.state import Diagnosis, IncidentState, RemediationPlan
from app.config import get_settings
from app.db import ACTION_CATALOG, RISK_ORDER, get_store
from app.llm.gateway import LLMGateway, render_context
from app.mcp_client import TelecomToolClient
from app.observability import metrics, tracer
from app.rag.retriever import get_retriever

MAX_REMEDIATION_ATTEMPTS = 2
DOMAIN_RANK = {"Transport": 0, "EPC": 1, "5GC": 1, "IMS": 2, "RAN": 3}  # upstream first

SYSTEM_PROMPT = (
    "You are a senior NOC engineer for a 4G/5G mobile operator (5G Core, EPC, IMS, transport). "
    "You reason from alarms, KPIs, topology and the operator's runbooks. The loudest alarm is often a "
    "symptom: find the earliest, most upstream fault. Only use node ids and actions that appear in the "
    "context. Never propose an action that is not in the runbook's action list."
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _event(state: IncidentState, step: str, detail: str) -> list[dict]:
    return [*state.get("timeline", []), {"ts": _now(), "step": step, "detail": detail}]


def _degraded(name: str, cur: float, base: float) -> bool:
    # lower is better for these, higher is better for the rest
    lower_better = name in {"cpu_util", "cx_latency_ms", "sip_503_rate"}
    if lower_better:
        return cur > base * 1.3 + 1
    return cur < base * 0.95


class AssuranceAgent:
    def __init__(self, tools: TelecomToolClient | None = None, llm: LLMGateway | None = None,
                 checkpointer: Any | None = None) -> None:
        self.tools = tools or TelecomToolClient()
        self.llm = llm or LLMGateway()
        self.settings = get_settings()
        self.graph = self._build().compile(checkpointer=checkpointer or InMemorySaver())

    # ------------------------------------------------------------------ nodes
    async def triage(self, state: IncidentState) -> dict:
        with tracer.span("node.triage"):
            trigger = await self.tools.call("get_alarm", alarm_id=state["alarm_id"])
            window = await self.tools.call("get_alarms_in_window", timestamp=trigger["raised_at"],
                                           window_s=self.settings.correlation_window_s)
            # grow a cluster of alarmed nodes connected (within 2 hops) to the trigger's node
            alarmed = {a["node_id"] for a in window}
            cluster, frontier = {trigger["node_id"]}, [trigger["node_id"]]
            topology: dict[str, list[dict]] = {}
            while frontier:
                node = frontier.pop()
                hop1 = await self.tools.call("get_topology_neighbors", node_id=node)
                topology[node] = hop1
                reach = {n["node_id"] for n in hop1}
                for mid in list(reach):
                    if mid not in alarmed:
                        reach |= {n["node_id"] for n in await self.tools.call("get_topology_neighbors", node_id=mid)}
                for n in reach & alarmed - cluster:
                    cluster.add(n)
                    frontier.append(n)
            correlated = [a for a in window if a["node_id"] in cluster]
            nodes = {n: (await self.tools.call("get_alarm", alarm_id=next(
                a["id"] for a in correlated if a["node_id"] == n)))["node"] for n in cluster}
            root = min(correlated, key=lambda a: (a["raised_at"], DOMAIN_RANK.get(nodes[a["node_id"]]["domain"], 9)))
            root = {**root, "type": nodes[root["node_id"]]["type"], "domain": nodes[root["node_id"]]["domain"]}
            metrics.inc("alarms_correlated", len(correlated))
            return {
                "trigger_alarm": trigger, "correlated_alarms": correlated, "root_candidate": root,
                "affected_nodes": sorted(cluster), "topology": topology, "status": "triaged",
                "timeline": _event(state, "triage", f"{len(correlated)} correlated alarms on {len(cluster)} nodes; "
                                                    f"earliest is {root['code']} on {root['node_id']}"),
            }

    async def gather_context(self, state: IncidentState) -> dict:
        with tracer.span("node.gather_context"):
            kpis: dict[str, dict] = {}
            for node in state["affected_nodes"]:
                res = await self.tools.call("query_kpi", node_id=node, last_n=6)
                kpis[node] = {k: {"current": v["current"], "baseline": v["baseline"],
                                  "degraded": _degraded(k, v["current"], v["baseline"])}
                              for k, v in res["kpis"].items() if "current" in v}
            n_bad = sum(v["degraded"] for kp in kpis.values() for v in kp.values())
            return {"kpis": kpis, "timeline": _event(state, "gather_context", f"{n_bad} degraded KPIs")}

    async def retrieve_knowledge(self, state: IncidentState) -> dict:
        with tracer.span("node.retrieve_knowledge"):
            node_types = [state["root_candidate"]["type"]] + [
                t["type"] for ns in state["topology"].values() for t in ns if t.get("node_id") in
                state["affected_nodes"] and t.get("type")]
            result = get_retriever().retrieve(state["correlated_alarms"], node_types)
            codes = {a["code"] for a in state["correlated_alarms"]}
            similar = []
            for inc in get_store().resolved_incidents():
                rep = inc.get("report") or {}
                overlap = codes & set(rep.get("alarm_codes", []))
                if overlap:
                    similar.append({"incident_id": inc["id"], "root_cause_node": inc["root_cause_node"],
                                    "fix": rep.get("fix"), "shared_codes": sorted(overlap)})
            rag = {
                "best_runbook": result.best_runbook,
                "attempts": result.attempts,
                "chunks": [{"id": h.chunk.id, "doc_id": h.chunk.doc_id, "section": h.chunk.section,
                            "score": h.score, "text": h.chunk.text,
                            "alarm_codes": h.chunk.metadata.get("alarm_codes", []),
                            "actions": h.chunk.metadata.get("actions", [])} for h in result.hits],
            }
            metrics.inc("rag_attempts", len(result.attempts))
            return {"rag": rag, "similar_incidents": similar[:3],
                    "timeline": _event(state, "retrieve_knowledge",
                                       f"runbook {result.best_runbook} after {len(result.attempts)} attempt(s); "
                                       f"{len(similar)} similar past incident(s)")}

    def _runbook(self, state: IncidentState, runbook_id: str | None) -> dict:
        for c in state["rag"]["chunks"]:
            if c["doc_id"] == runbook_id:
                return c
        return state["rag"]["chunks"][0] if state["rag"]["chunks"] else {}

    async def diagnose(self, state: IncidentState) -> dict:
        with tracer.span("node.diagnose"):
            best = self._runbook(state, state["rag"]["best_runbook"])
            context = {
                "trigger_alarm": state["trigger_alarm"], "correlated_alarms": state["correlated_alarms"],
                "root_candidate": state["root_candidate"], "kpis": state["kpis"],
                "topology": state["topology"], "similar_incidents": state.get("similar_incidents", []),
                "runbook": {"doc_id": best.get("doc_id"), "alarm_codes": best.get("alarm_codes", [])},
                "runbook_text": "\n\n".join(c["text"] for c in state["rag"]["chunks"]),
            }
            prompt = ("Diagnose this incident. Return the root-cause network function, the fault domain, a short "
                      "summary, the evidence, the runbook id that applies and your confidence.\n\n"
                      + render_context(context))
            diag = self.llm.generate("diagnosis", SYSTEM_PROMPT, prompt, Diagnosis, context)
            if diag.root_cause_node not in state["affected_nodes"]:
                # guardrail: the model may only blame a node that is part of the incident
                diag.root_cause_node = state["root_candidate"]["node_id"]
                diag.confidence = min(diag.confidence, 0.5)
            return {"diagnosis": diag.model_dump(), "status": "diagnosed",
                    "timeline": _event(state, "diagnose", f"root cause {diag.root_cause_node} "
                                                          f"(confidence {diag.confidence:.2f})")}

    async def plan_remediation(self, state: IncidentState) -> dict:
        with tracer.span("node.plan_remediation"):
            diag = state["diagnosis"]
            runbook = self._runbook(state, diag.get("runbook_id"))
            allowed = [a for a in runbook.get("actions", []) if a in ACTION_CATALOG]
            context = {"diagnosis": diag, "root_cause_node": diag["root_cause_node"],
                       "runbook_id": runbook.get("doc_id"), "runbook_actions": allowed,
                       "action_risk": {a: ACTION_CATALOG[a]["risk"] for a in allowed},
                       "tried_actions": state.get("tried_actions", []),
                       "runbook_text": runbook.get("text", "")}
            prompt = ("Propose the next remediation action (one is best). Prefer the lowest-risk action that fixes "
                      "the root cause. Do not repeat tried actions.\n\n" + render_context(context))
            plan = self.llm.generate("plan", SYSTEM_PROMPT, prompt, RemediationPlan, context)
            accepted, rejected = [], list(state.get("guardrail_rejections", []))
            for a in plan.actions:
                if a.action in allowed and a.action not in context["tried_actions"] and get_store().node(a.target):
                    accepted.append({**a.model_dump(), "risk": ACTION_CATALOG[a.action]["risk"]})
                else:
                    rejected.append({**a.model_dump(), "reason": "not in runbook/catalog, already tried, "
                                                                  "or unknown target"})
                    metrics.inc("guardrail_rejections")
            detail = ", ".join(f"{a['action']}@{a['target']} ({a['risk']} risk)" for a in accepted) or "no safe action"
            return {"plan": accepted[:1], "guardrail_rejections": rejected, "status": "planned",
                    "timeline": _event(state, "plan_remediation", detail)}

    async def approval_gate(self, state: IncidentState) -> dict:
        if not state.get("plan"):
            return {"approval": {"approved": False, "by": "system", "comment": "no safe action to run"}}
        action = state["plan"][0]
        threshold = self.settings.auto_approve_max_risk
        if RISK_ORDER.index(action["risk"]) <= RISK_ORDER.index(threshold):
            return {"approval": {"approved": True, "by": "policy", "comment": f"risk {action['risk']} <= {threshold}"},
                    "timeline": _event(state, "approval_gate", "auto-approved by policy")}
        # note: on resume LangGraph re-runs this node from the top, so nothing above interrupt() has side effects
        decision = interrupt({
            "type": "approval_request", "incident_id": state["incident_id"], "action": action,
            "diagnosis": state["diagnosis"]["summary"], "confidence": state["diagnosis"]["confidence"],
        })
        approved = bool(decision.get("approved"))
        metrics.inc("approvals", outcome="approved" if approved else "rejected")
        return {"approval": {"approved": approved, "by": decision.get("by", "unknown"),
                             "comment": decision.get("comment", "")},
                "timeline": _event(state, "approval_gate",
                                   f"{'approved' if approved else 'rejected'} by {decision.get('by', 'unknown')}")}

    async def execute(self, state: IncidentState) -> dict:
        with tracer.span("node.execute"):
            action = state["plan"][0]
            res = await self.tools.call("execute_remediation", incident_id=state["incident_id"],
                                        action=action["action"], target=action["target"],
                                        approval_token=f"APPROVED-{state['incident_id']}",
                                        approved_by=state["approval"]["by"])
            metrics.inc("remediations", action=action["action"])
            return {"executions": [*state.get("executions", []), res],
                    "tried_actions": [*state.get("tried_actions", []), action["action"]],
                    "status": "executed",
                    "timeline": _event(state, "execute", f"ran {action['action']} on {action['target']}")}

    async def verify(self, state: IncidentState) -> dict:
        with tracer.span("node.verify"):
            still_bad = []
            for node in state["affected_nodes"]:
                res = await self.tools.call("query_kpi", node_id=node, last_n=1)
                for k, v in res["kpis"].items():
                    base = state["kpis"].get(node, {}).get(k, {}).get("baseline", v.get("baseline"))
                    if "current" in v and _degraded(k, v["current"], base):
                        still_bad.append(f"{node}.{k}={v['current']}")
            active = await self.tools.call("get_active_alarms")
            open_ids = {a["id"] for a in active} & {a["id"] for a in state["correlated_alarms"]}
            healthy = not still_bad and not open_ids
            return {"verification": {"healthy": healthy, "degraded_kpis": still_bad, "open_alarms": sorted(open_ids)},
                    "timeline": _event(state, "verify", "KPIs healthy, alarms cleared" if healthy
                                       else f"still degraded: {', '.join(still_bad[:3]) or 'alarms open'}")}

    async def report(self, state: IncidentState) -> dict:
        with tracer.span("node.report"):
            approval = state.get("approval", {})
            healthy = state.get("verification", {}).get("healthy", False)
            # anything not verified healthy (rejected, no safe action, fix did not work) goes to a human
            status = "resolved" if healthy else "escalated"
            diag = state.get("diagnosis", {})
            report = {
                "status": status, "root_cause_node": diag.get("root_cause_node"), "summary": diag.get("summary"),
                "runbook_id": diag.get("runbook_id"),
                "alarm_codes": sorted({a["code"] for a in state["correlated_alarms"]}),
                "fix": state["tried_actions"][-1] if healthy and state.get("tried_actions") else None,
                "actions_tried": state.get("tried_actions", []), "approval": approval,
            }
            get_store().upsert_incident({"id": state["incident_id"], "alarm_id": state["alarm_id"], "status": status,
                                         "summary": diag.get("summary"), "root_cause_node": diag.get("root_cause_node"),
                                         "report": report})
            metrics.inc("incidents", status=status)
            return {"report": report, "status": status, "timeline": _event(state, "report", status)}

    # ------------------------------------------------------------------ routing
    @staticmethod
    def _after_approval(state: IncidentState) -> str:
        return "execute" if state.get("approval", {}).get("approved") else "report"

    @staticmethod
    def _after_verify(state: IncidentState) -> str:
        if state["verification"]["healthy"]:
            return "report"
        return "plan_remediation" if len(state.get("tried_actions", [])) < MAX_REMEDIATION_ATTEMPTS else "report"

    def _build(self) -> StateGraph:
        g = StateGraph(IncidentState)
        for name in ("triage", "gather_context", "retrieve_knowledge", "diagnose", "plan_remediation",
                     "approval_gate", "execute", "verify", "report"):
            g.add_node(name, getattr(self, name))
        g.add_edge(START, "triage")
        g.add_edge("triage", "gather_context")
        g.add_edge("gather_context", "retrieve_knowledge")
        g.add_edge("retrieve_knowledge", "diagnose")
        g.add_edge("diagnose", "plan_remediation")
        g.add_edge("plan_remediation", "approval_gate")
        g.add_conditional_edges("approval_gate", self._after_approval, ["execute", "report"])
        g.add_edge("execute", "verify")
        g.add_conditional_edges("verify", self._after_verify, ["report", "plan_remediation"])
        g.add_edge("report", END)
        return g

    def mermaid(self) -> str:
        return self.graph.get_graph().draw_mermaid()


def to_json(obj: Any) -> str:
    return json.dumps(obj, default=str, indent=2)
