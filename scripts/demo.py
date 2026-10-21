"""Command-line demo: run one alarm (or all four fault scenarios) through the agent.

    python -m scripts.demo                 # all scenarios, asks you to approve each action
    python -m scripts.demo ALM-1002 --yes  # one alarm, approve automatically
"""

from __future__ import annotations

import argparse
import asyncio

from app.agent.service import IncidentService

SCENARIOS = ["ALM-1002", "ALM-2002", "ALM-3003", "ALM-4003"]


async def handle(svc: IncidentService, alarm_id: str, auto_yes: bool) -> None:
    print(f"\n=== Alarm {alarm_id} ===")
    inc = await svc.open_incident(alarm_id)
    s = inc["state"]
    t = s["trigger_alarm"]
    print(f"Trigger : {t['code']} on {t['node_id']} - {t['text']}")
    print(f"Diagnosis: {s['diagnosis']['summary']}")
    print(f"Runbook : {s['diagnosis']['runbook_id']}  confidence {s['diagnosis']['confidence']:.2f}")
    if inc["status"] == "awaiting_approval":
        a = inc["pending_approval"]["action"]
        print(f"Proposed: {a['action']} on {a['target']} ({a['risk']} risk) - {a['rationale']}")
        ok = auto_yes or input("Approve? [y/N] ").strip().lower() == "y"
        inc = await svc.decide(inc["incident_id"], ok, "demo-user", "" if ok else "rejected in demo")
    for e in inc["state"]["timeline"]:
        print(f"  - {e['step']:<18} {e['detail']}")
    print(f"Result  : {inc['status'].upper()}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("alarms", nargs="*", default=SCENARIOS)
    ap.add_argument("--yes", action="store_true", help="approve every proposed action")
    args = ap.parse_args()
    svc = IncidentService()
    for alarm in args.alarms:
        await handle(svc, alarm, args.yes)


if __name__ == "__main__":
    asyncio.run(main())
