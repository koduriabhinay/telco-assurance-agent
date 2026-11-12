"""Lightweight tracing, metrics and structured logs.

- Spans: every graph node, LLM call and MCP tool call is timed and attached to the incident's trace.
- Metrics: counters and latency histograms rendered in Prometheus text format at GET /metrics.
- Logs: one JSON line per span, ready for Cloud Logging / Loki.
If OpenTelemetry is installed and OTEL_EXPORTER_OTLP_ENDPOINT is set, spans are also exported there.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from typing import Any, Iterator

log = logging.getLogger("telco.agent")
if not log.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(h)
    log.setLevel(os.getenv("LOG_LEVEL", "INFO"))

_current_trace: contextvars.ContextVar[list | None] = contextvars.ContextVar("trace", default=None)

_BUCKETS = (0.005, 0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10, 30)


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.counters: dict[tuple[str, tuple], float] = defaultdict(float)
        self.hist: dict[tuple[str, tuple], list[float]] = defaultdict(list)

    def inc(self, name: str, value: float = 1, **labels: str) -> None:
        with self._lock:
            self.counters[(name, tuple(sorted(labels.items())))] += value

    def observe(self, name: str, value: float, **labels: str) -> None:
        with self._lock:
            self.hist[(name, tuple(sorted(labels.items())))].append(value)

    def render(self) -> str:
        def lbl(items: tuple, extra: dict | None = None) -> str:
            d = dict(items) | (extra or {})
            return "{" + ",".join(f'{k}="{v}"' for k, v in d.items()) + "}" if d else ""

        lines = []
        with self._lock:
            for (name, labels), v in sorted(self.counters.items()):
                lines.append(f"{name}_total{lbl(labels)} {v}")
            for (name, labels), values in sorted(self.hist.items()):
                for b in _BUCKETS:
                    lines.append(f"{name}_bucket{lbl(labels, {'le': b})} {sum(1 for x in values if x <= b)}")
                lines.append(f"{name}_bucket{lbl(labels, {'le': '+Inf'})} {len(values)}")
                lines.append(f"{name}_sum{lbl(labels)} {sum(values):.6f}")
                lines.append(f"{name}_count{lbl(labels)} {len(values)}")
        return "\n".join(lines) + "\n"


metrics = Metrics()


class Tracer:
    def __init__(self) -> None:
        self._otel = None
        if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
            try:
                from opentelemetry import trace

                self._otel = trace.get_tracer("telco-assurance-agent")
            except ImportError:
                pass

    @contextmanager
    def trace(self) -> Iterator[list]:
        spans: list = []
        token = _current_trace.set(spans)
        try:
            yield spans
        finally:
            _current_trace.reset(token)

    @contextmanager
    def span(self, name: str, **attrs: Any) -> Iterator[dict]:
        rec: dict[str, Any] = {"span": name, **attrs}
        start = time.perf_counter()
        otel_cm = self._otel.start_as_current_span(name) if self._otel else None
        if otel_cm:
            otel_cm.__enter__()
        try:
            yield rec
            rec["status"] = "ok"
        except Exception as exc:
            rec["status"] = "error"
            rec["error"] = str(exc)[:200]
            raise
        finally:
            rec["ms"] = round((time.perf_counter() - start) * 1000, 2)
            if otel_cm:
                otel_cm.__exit__(None, None, None)
            kind = name.split(".")[0]
            metrics.observe("agent_span_seconds", rec["ms"] / 1000, kind=kind, op=name)
            if rec["status"] == "error":
                metrics.inc("agent_span_errors", kind=kind, op=name)
            spans = _current_trace.get()
            if spans is not None:
                spans.append(rec)
            log.debug(json.dumps(rec, default=str))


tracer = Tracer()


def current_spans() -> list:
    return list(_current_trace.get() or [])
