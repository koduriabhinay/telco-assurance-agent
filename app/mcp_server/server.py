"""Telecom MCP server.

Exposes the network's operational systems as MCP tools so any MCP client (this project's agent,
Claude Desktop, an IDE, another agent) can use them through one protocol.

Run standalone:
    python -m app.mcp_server.server              # stdio
    python -m app.mcp_server.server --http 8001  # streamable HTTP at /mcp
"""

from __future__ import annotations

import argparse
import statistics

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from app.db import ACTION_CATALOG, get_store
from app.rag.retriever import get_retriever

mcp = MCPServer(
    name="telco-noc-tools",
    instructions="Network operations tools for a 4G/5G mobile network: alarms, KPIs, topology, "
                 "runbooks, read-only SQL and guarded remediation.",
)


def _kpi_status(series: list[dict]) -> dict:
    values = [r["value"] for r in series]
    if not values:
        return {}
    baseline = statistics.median(values[:6])  # first samples of the day are the baseline window
    current = values[-1]
    return {"current": current, "baseline": round(baseline, 3), "latest_ts": series[-1]["ts"]}


@mcp.tool(description="List active (uncleared) alarms, optionally for one network function.")
def get_active_alarms(node_id: str | None = None) -> list[dict]:
    return get_store().active_alarms(node_id)


@mcp.tool(description="Get one alarm by id, with the network function it was raised on.")
def get_alarm(alarm_id: str) -> dict:
    store = get_store()
    alarm = store.get_alarm(alarm_id)
    if not alarm:
        raise ToolError(f"alarm {alarm_id} not found")
    return {**alarm, "node": store.node(alarm["node_id"])}


@mcp.tool(description="Get alarms raised within +/- window_s seconds of a timestamp (for correlation).")
def get_alarms_in_window(timestamp: str, window_s: int = 600) -> list[dict]:
    return get_store().alarms_in_window(timestamp, window_s)


@mcp.tool(description="Get the neighbours of a network function and the 3GPP interface to each.")
def get_topology_neighbors(node_id: str) -> list[dict]:
    return get_store().neighbors(node_id)


@mcp.tool(description="Query KPIs for a network function. Returns recent samples plus current vs baseline.")
def query_kpi(node_id: str, kpi: str | None = None, last_n: int = 12) -> dict:
    store = get_store()
    names = [kpi] if kpi else store.kpi_names(node_id)
    out = {}
    for name in names:
        series = store.kpi_series(node_id, name, last_n=10_000)
        out[name] = {**_kpi_status(series), "samples": [r["value"] for r in series[-last_n:]]}
    return {"node_id": node_id, "kpis": out}


@mcp.tool(description="Semantic search over operational runbooks and 3GPP reference notes.")
def search_runbooks(query: str, k: int = 3) -> list[dict]:
    hits = get_retriever().store.search(query, k=k)
    return [{"doc_id": h.chunk.doc_id, "section": h.chunk.section, "score": round(h.score, 4),
             "text": h.chunk.text, "actions": h.chunk.metadata.get("actions", [])} for h in hits]


@mcp.tool(description="Run a read-only SQL SELECT against the operations database "
                      "(tables: nodes, links, alarms, kpis, incidents, action_log).")
def run_sql(query: str, limit: int = 50) -> list[dict]:
    try:
        return get_store().readonly_sql(query, limit)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(description="List remediation actions the agent may use, with their risk level.")
def list_actions() -> dict:
    return ACTION_CATALOG


@mcp.tool(description="Execute an approved remediation action on a network function. "
                      "Requires approval_token issued by the human approval step.")
def execute_remediation(incident_id: str, action: str, target: str, approval_token: str,
                        approved_by: str = "unknown") -> dict:
    if action not in ACTION_CATALOG:
        raise ToolError(f"unknown action {action}")
    if not approval_token.startswith("APPROVED-"):
        raise ToolError("missing or invalid approval token")
    store = get_store()
    # which fault scenario the target is part of (the simulator uses it to decide if the fix works)
    rows = store.query("SELECT scenario FROM alarms WHERE node_id = ? AND cleared_at IS NULL", (target,))
    scenario = rows[0]["scenario"] if rows else None
    result = store.apply_action(action, target, scenario)
    store.log_action(incident_id, action, target, approved_by, "effective" if result["effective"] else "no_effect")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--http", type=int, help="serve streamable HTTP on this port instead of stdio")
    args = parser.parse_args()
    if args.http:
        mcp.run(transport="streamable-http", host="0.0.0.0", port=args.http)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
