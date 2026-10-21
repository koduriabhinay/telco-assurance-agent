import asyncio

import pytest

from app.mcp_client import TelecomToolClient, ToolError


def run(coro):
    return asyncio.run(coro)


def test_server_exposes_telecom_tools():
    names = set(run(TelecomToolClient().list_tools()))
    assert {"get_active_alarms", "get_alarm", "query_kpi", "get_topology_neighbors",
            "search_runbooks", "run_sql", "execute_remediation"} <= names


def test_kpi_tool_flags_degradation():
    res = run(TelecomToolClient().call("query_kpi", node_id="amf-01", kpi="registration_success_rate"))
    kpi = res["kpis"]["registration_success_rate"]
    assert kpi["baseline"] > 99 and kpi["current"] < 70


def test_sql_tool_is_read_only():
    client = TelecomToolClient()
    rows = run(client.call("run_sql", query="SELECT code, COUNT(*) AS n FROM alarms GROUP BY code"))
    assert any(r["code"] == "LINK_DOWN" for r in rows)
    for bad in ["DELETE FROM alarms", "SELECT 1; DROP TABLE alarms", "UPDATE alarms SET code='x'"]:
        with pytest.raises(ToolError):
            run(client.call("run_sql", query=bad))


def test_remediation_needs_approval_token():
    with pytest.raises(ToolError):
        run(TelecomToolClient().call("execute_remediation", incident_id="INC-1", action="scale_out_upf",
                                     target="upf-01", approval_token="let-me-in"))
