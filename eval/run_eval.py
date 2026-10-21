"""Offline evaluation of the assurance agent against a golden set of alarms.

Each case starts from a fresh copy of the mock network, opens an incident from one alarm (root-cause
or symptom), approves the proposed action and checks the outcome.

Metrics
- rca_accuracy:       root-cause node matches the label
- runbook_hit_at_1:   the top retrieved runbook is the labelled one
- action_accuracy:    the first proposed action is the labelled fix
- resolution_rate:    the closed loop ends with KPIs healthy and alarms cleared
- unsafe_action_rate: proposed actions outside the runbook (should be 0; guardrails reject them)
- latency p50 / p95:  wall-clock per incident

Usage: python -m eval.run_eval [--out eval/report.json] [--min-rca 0.9]
Exit code 1 when a metric is below its threshold, so CI can gate a release on it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

from app.agent.service import IncidentService
from app.db import reset_store

GOLDEN = Path(__file__).parent / "golden.jsonl"


async def run_case(case: dict) -> dict:
    reset_store()
    svc = IncidentService()
    t0 = time.perf_counter()
    inc = await svc.open_incident(case["alarm_id"])
    s = inc["state"]
    proposed = (s.get("plan") or [{}])[0].get("action")
    if inc["status"] == "awaiting_approval":
        inc = await svc.decide(inc["incident_id"], True, "eval-bot")
    latency = time.perf_counter() - t0
    s = inc["state"]
    return {
        "alarm_id": case["alarm_id"],
        "rca_ok": s["diagnosis"]["root_cause_node"] == case["root_cause_node"],
        "runbook_ok": s["rag"]["best_runbook"] == case["runbook_id"],
        "action_ok": proposed == case["fix"],
        "resolved": inc["status"] == "resolved",
        "unsafe": len(s.get("guardrail_rejections", [])),
        "rag_attempts": len(s["rag"]["attempts"]),
        "latency_s": round(latency, 4),
        "predicted_root": s["diagnosis"]["root_cause_node"],
    }


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    lat = sorted(r["latency_s"] for r in rows)
    return {
        "cases": n,
        "rca_accuracy": sum(r["rca_ok"] for r in rows) / n,
        "runbook_hit_at_1": sum(r["runbook_ok"] for r in rows) / n,
        "action_accuracy": sum(r["action_ok"] for r in rows) / n,
        "resolution_rate": sum(r["resolved"] for r in rows) / n,
        "unsafe_action_rate": sum(r["unsafe"] > 0 for r in rows) / n,
        "latency_p50_s": statistics.median(lat),
        "latency_p95_s": lat[min(n - 1, int(round(0.95 * (n - 1))))],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="eval/report.json")
    ap.add_argument("--min-rca", type=float, default=0.9)
    ap.add_argument("--min-resolution", type=float, default=0.9)
    args = ap.parse_args()

    cases = [json.loads(line) for line in GOLDEN.read_text().splitlines() if line.strip()]
    rows = [asyncio.run(run_case(c)) for c in cases]
    summary = summarize(rows)
    Path(args.out).write_text(json.dumps({"summary": summary, "cases": rows}, indent=2))

    print(f"{'alarm':10} {'root ok':8} {'runbook':8} {'action':7} {'resolved':9} latency")
    for r in rows:
        print(f"{r['alarm_id']:10} {str(r['rca_ok']):8} {str(r['runbook_ok']):8} {str(r['action_ok']):7} "
              f"{str(r['resolved']):9} {r['latency_s']:.3f}s")
    print(json.dumps(summary, indent=2))

    failed = summary["rca_accuracy"] < args.min_rca or summary["resolution_rate"] < args.min_resolution \
        or summary["unsafe_action_rate"] > 0
    if failed:
        print("EVAL GATE FAILED", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
