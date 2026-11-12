"""The Claude provider path, exercised with a fake client (no network, no API key)."""

from types import SimpleNamespace

from app.agent.state import Diagnosis
from app.llm.gateway import LLMGateway, _ClaudeProvider


class FakeMessages:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        block = SimpleNamespace(type="tool_use", input=self.replies.pop(0))
        return SimpleNamespace(content=[block], usage=SimpleNamespace(input_tokens=100, output_tokens=50))


def test_claude_provider_forces_structured_tool_output_and_retries_invalid():
    good = {"root_cause_node": "upf-01", "fault_domain": "5GC", "summary": "UPF overload",
            "evidence": [], "runbook_id": "RB-UPF-002", "confidence": 0.8}
    msgs = FakeMessages([{"confidence": 7}, good])  # first reply fails validation
    provider = _ClaudeProvider(SimpleNamespace(messages=msgs), "claude-sonnet-5-5")
    out = LLMGateway(provider).generate("diagnosis", "sys", "prompt", Diagnosis, {})
    assert out.root_cause_node == "upf-01"
    assert len(msgs.calls) == 2
    first = msgs.calls[0]
    assert first["tool_choice"] == {"type": "tool", "name": "submit_diagnosis"}
    assert first["tools"][0]["input_schema"]["properties"]["root_cause_node"]
    assert "previous answer was invalid" in msgs.calls[1]["messages"][0]["content"]
