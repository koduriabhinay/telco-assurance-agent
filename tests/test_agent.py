import asyncio

import pytest

from app.agent.graph import AssuranceAgent
from app.agent.service import IncidentService
from app.llm.gateway import LLMGateway, MockProvider

CASES = [
    ("ALM-1002", "csr-blr-01", "reroute_backhaul_path"),  # AMF alarm, transport root cause
    ("ALM-2002", "upf-01", "scale_out_upf"),
    ("ALM-3003", "hss-ims-01", "failover_diameter_peer"),
    ("ALM-4003", "dra-01", "failover_diameter_peer"),
]


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("alarm,root,fix", CASES)
def test_full_loop_with_human_approval(alarm, root, fix):
    svc = IncidentService()
    inc = run(svc.open_incident(alarm))
    assert inc["status"] == "awaiting_approval"
    assert inc["state"]["diagnosis"]["root_cause_node"] == root
    assert inc["pending_approval"]["action"]["action"] == fix

    done = run(svc.decide(inc["incident_id"], True, "engineer@op.com"))
    assert done["status"] == "resolved"
    assert done["state"]["verification"]["healthy"]
    assert done["state"]["report"]["fix"] == fix


def test_rejection_skips_execution_and_escalates():
    svc = IncidentService()
    inc = run(svc.open_incident("ALM-1002"))
    done = run(svc.decide(inc["incident_id"], False, "engineer@op.com", "change freeze"))
    assert done["status"] == "escalated"
    assert not done["state"].get("executions")


def test_low_risk_auto_approval_policy(monkeypatch):
    monkeypatch.setenv("AUTO_APPROVE_MAX_RISK", "low")
    svc = IncidentService(AssuranceAgent())
    inc = run(svc.open_incident("ALM-2002"))
    assert inc["status"] == "resolved"
    assert inc["state"]["approval"]["by"] == "policy"


def test_decision_on_finished_incident_is_refused():
    svc = IncidentService()
    inc = run(svc.open_incident("ALM-3003"))
    run(svc.decide(inc["incident_id"], True, "a"))
    with pytest.raises(ValueError):
        run(svc.decide(inc["incident_id"], True, "a"))


class HallucinatingProvider(MockProvider):
    """Blames a node outside the incident and proposes an action that is not in the runbook."""

    def generate(self, task, system, prompt, schema, context):
        if task == "diagnosis":
            out = super().generate(task, system, prompt, schema, context)
            out.root_cause_node = "amf-01-does-not-exist"
            return out
        return schema.model_validate({"actions": [
            {"action": "reboot_entire_core", "target": "amf-01", "rationale": "why not"}]})


def test_guardrails_block_hallucinated_root_cause_and_action():
    svc = IncidentService(AssuranceAgent(llm=LLMGateway(HallucinatingProvider())))
    inc = run(svc.open_incident("ALM-1002"))
    s = inc["state"]
    assert s["diagnosis"]["root_cause_node"] == "csr-blr-01"  # fell back to the correlated candidate
    assert s["diagnosis"]["confidence"] <= 0.5
    assert s["guardrail_rejections"][0]["action"] == "reboot_entire_core"
    assert inc["status"] == "escalated" and not s.get("executions")


def test_alarm_dedup_reuses_open_incident():
    svc = IncidentService()
    first = run(svc.open_incident("ALM-1002"))
    again = run(svc.open_incident("ALM-1004"))  # gNB symptom of the same fault
    assert again["incident_id"] == first["incident_id"]
