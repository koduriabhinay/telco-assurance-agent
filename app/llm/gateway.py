"""Model gateway: one interface over several LLM back ends.

Providers
- anthropic: Claude through the Anthropic API (ANTHROPIC_API_KEY)
- vertex:    Claude on Google Vertex AI (VERTEX_PROJECT, VERTEX_REGION, Google ADC credentials)
- mock:      offline, deterministic reasoning used in tests, CI and demos without credentials

Every call asks for structured output that matches a Pydantic schema. For Claude this is done with a
forced tool call, so the reply is always a JSON object we can validate.
"""

from __future__ import annotations

import json
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from app.config import Settings, get_settings
from app.db import ACTION_CATALOG, RISK_ORDER
from app.observability import metrics, tracer

T = TypeVar("T", bound=BaseModel)


class LLMProvider(Protocol):
    name: str

    def generate(self, task: str, system: str, prompt: str, schema: type[T], context: dict) -> T: ...


class _ClaudeProvider:
    """Shared logic for Claude on the Anthropic API and on Vertex AI."""

    name = "claude"

    def __init__(self, client: Any, model: str) -> None:
        self.client = client
        self.model = model

    def generate(self, task: str, system: str, prompt: str, schema: type[T], context: dict) -> T:
        tool = {"name": f"submit_{task}", "description": f"Submit the {task} result.",
                "input_schema": schema.model_json_schema()}
        last_error = ""
        for _ in range(2):  # one retry with the validation error fed back
            msg = prompt if not last_error else f"{prompt}\n\nYour previous answer was invalid: {last_error}"
            resp = self.client.messages.create(
                model=self.model, max_tokens=2048, system=system,
                messages=[{"role": "user", "content": msg}],
                tools=[tool], tool_choice={"type": "tool", "name": tool["name"]},
            )
            usage = getattr(resp, "usage", None)
            if usage is not None:
                metrics.inc("llm_tokens", getattr(usage, "input_tokens", 0) or 0, direction="input")
                metrics.inc("llm_tokens", getattr(usage, "output_tokens", 0) or 0, direction="output")
            block = next((b for b in resp.content if getattr(b, "type", "") == "tool_use"), None)
            try:
                return schema.model_validate(block.input if block else {})
            except ValidationError as exc:
                last_error = str(exc)[:500]
        raise ValueError(f"model returned invalid {task}: {last_error}")


class AnthropicProvider(_ClaudeProvider):
    name = "anthropic"

    def __init__(self, settings: Settings) -> None:
        import anthropic

        super().__init__(anthropic.Anthropic(), settings.llm_model)


class VertexProvider(_ClaudeProvider):
    name = "vertex"

    def __init__(self, settings: Settings) -> None:
        from anthropic import AnthropicVertex

        super().__init__(AnthropicVertex(project_id=settings.vertex_project, region=settings.vertex_region),
                         settings.llm_model)


class MockProvider:
    """Deterministic stand-in for an LLM. It reasons only from the structured context the agent
    passes in, so the whole pipeline runs offline and gives repeatable results for tests."""

    name = "mock"

    def generate(self, task: str, system: str, prompt: str, schema: type[T], context: dict) -> T:
        if task == "diagnosis":
            cand = context["root_candidate"]
            rb = context.get("runbook") or {}
            degraded = [f"{n}.{k} {v['current']} (baseline {v['baseline']})"
                        for n, kp in context.get("kpis", {}).items() for k, v in kp.items() if v.get("degraded")]
            symptoms = [a for a in context["correlated_alarms"] if a["node_id"] != cand["node_id"]]
            data = {
                "root_cause_node": cand["node_id"],
                "fault_domain": cand.get("domain", "unknown"),
                "summary": f"{cand['code']} on {cand['node_id']} ({cand.get('type')}) is the earliest alarm in a "
                           f"correlated group of {len(context['correlated_alarms'])}; "
                           f"{len(symptoms)} alarms on connected nodes are downstream symptoms.",
                "evidence": [f"{a['raised_at']} {a['node_id']} {a['code']}" for a in context["correlated_alarms"]]
                            + degraded[:4],
                "runbook_id": rb.get("doc_id"),
                "confidence": 0.9 if rb.get("doc_id") and cand["code"] in rb.get("alarm_codes", []) else 0.6,
            }
        elif task == "plan":
            tried = set(context.get("tried_actions", []))
            allowed = [a for a in context.get("runbook_actions", []) if a not in tried and a in ACTION_CATALOG]
            # prefer the lowest-risk action that changes the network; tickets are the fallback
            allowed.sort(key=lambda a: (ACTION_CATALOG[a]["risk"] == "none",
                                        RISK_ORDER.index(ACTION_CATALOG[a]["risk"])))
            actions = allowed[:1]
            data = {"actions": [{"action": a, "target": context["root_cause_node"],
                                 "rationale": f"Runbook {context.get('runbook_id')} step: "
                                              f"{ACTION_CATALOG[a]['desc']}"} for a in actions]}
        else:
            raise ValueError(f"mock provider has no rule for task {task}")
        return schema.model_validate(data)


class LLMGateway:
    def __init__(self, provider: LLMProvider | None = None, settings: Settings | None = None) -> None:
        settings = settings or get_settings()
        if provider is None:
            provider = {"anthropic": AnthropicProvider, "vertex": VertexProvider}.get(
                settings.llm_provider, lambda s: MockProvider())(settings)
        self.provider = provider

    def generate(self, task: str, system: str, prompt: str, schema: type[T], context: dict) -> T:
        with tracer.span(f"llm.{task}", provider=self.provider.name):
            metrics.inc("llm_calls", provider=self.provider.name, task=task)
            return self.provider.generate(task, system, prompt, schema, context)


def render_context(context: dict) -> str:
    return json.dumps(context, indent=1, default=str)
