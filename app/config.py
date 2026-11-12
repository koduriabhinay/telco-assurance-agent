"""Runtime settings, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"


@dataclass(frozen=True)
class Settings:
    # LLM gateway: "mock" (offline, deterministic), "anthropic" (Claude API) or "vertex" (Claude on Vertex AI)
    llm_provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "mock"))
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "claude-sonnet-5-5"))
    vertex_project: str | None = field(default_factory=lambda: os.getenv("VERTEX_PROJECT"))
    vertex_region: str = field(default_factory=lambda: os.getenv("VERTEX_REGION", "us-east5"))

    # Operational store: sqlite file/memory by default, PostgreSQL when DATABASE_URL starts with postgresql://
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///:memory:"))

    # Event bus: "memory" by default, "kafka" for a real broker
    event_bus: str = field(default_factory=lambda: os.getenv("EVENT_BUS", "memory"))
    kafka_bootstrap: str = field(default_factory=lambda: os.getenv("KAFKA_BOOTSTRAP", "localhost:9092"))
    alarm_topic: str = field(default_factory=lambda: os.getenv("ALARM_TOPIC", "noc.alarms"))

    # MCP: "inproc" connects the agent to the telecom MCP server in the same process,
    # or set an http(s) URL of a running streamable-HTTP MCP server.
    mcp_server: str = field(default_factory=lambda: os.getenv("MCP_SERVER", "inproc"))

    # Remediation actions above this risk level need a human approval
    auto_approve_max_risk: str = field(default_factory=lambda: os.getenv("AUTO_APPROVE_MAX_RISK", "none"))

    correlation_window_s: int = 600


def get_settings() -> Settings:
    return Settings()
