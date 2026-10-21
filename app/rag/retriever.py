"""Agentic RAG: plan a query, retrieve, grade the hits, and rewrite and retry when they are weak."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from app.config import DATA_DIR
from app.rag.vectorstore import Hit, InMemoryVectorStore, VectorStore, load_runbook_chunks

MIN_SCORE = 0.25  # cosine score below which a hit is treated as not relevant


@dataclass
class RetrievalResult:
    hits: list[Hit]
    attempts: list[dict] = field(default_factory=list)

    @property
    def best_runbook(self) -> str | None:
        return self.hits[0].chunk.doc_id if self.hits else None


class AgenticRetriever:
    def __init__(self, store: VectorStore) -> None:
        self.store = store

    @staticmethod
    def grade(hit: Hit, alarm_codes: set[str]) -> float:
        """Relevance grade: vector score, boosted when the runbook lists the same alarm codes."""
        overlap = len(alarm_codes & set(hit.chunk.metadata.get("alarm_codes", [])))
        return hit.score + 0.15 * overlap

    def retrieve(self, alarms: list[dict], node_types: list[str], k: int = 4,
                 max_attempts: int = 3) -> RetrievalResult:
        codes = {a["code"] for a in alarms}
        queries = [
            # 1. precise: alarm codes and their text
            " ".join(sorted(codes)) + " " + " ".join(a["text"] for a in alarms),
            # 2. rewrite: node types and generic symptom words
            " ".join(sorted(set(node_types))) + " failure root cause remediation " + " ".join(sorted(codes)),
            # 3. broadest: just the domain vocabulary
            " ".join(sorted(set(node_types))) + " alarm troubleshooting runbook",
        ]
        result = RetrievalResult(hits=[])
        for attempt, q in enumerate(queries[:max_attempts], start=1):
            hits = self.store.search(q, k=k * 2)
            graded = sorted(((self.grade(h, codes), h) for h in hits), key=lambda x: -x[0])
            relevant = [Hit(h.chunk, round(g, 4)) for g, h in graded if g >= MIN_SCORE]
            result.attempts.append({"attempt": attempt, "query": q[:160], "top_score": round(graded[0][0], 4)
                                    if graded else 0.0, "relevant": len(relevant)})
            if relevant:
                # de-duplicate to one chunk per section, keep the best runbook's chunks first
                seen, out = set(), []
                for h in relevant:
                    if h.chunk.id not in seen:
                        seen.add(h.chunk.id)
                        out.append(h)
                result.hits = out[:k]
                return result
        return result


_retriever: AgenticRetriever | None = None


def get_retriever() -> AgenticRetriever:
    global _retriever
    if _retriever is None:
        if os.getenv("VECTOR_STORE") == "pgvector":
            from app.rag.vectorstore import PgVectorStore

            store: VectorStore = PgVectorStore(os.environ["DATABASE_URL"])
        else:
            store = InMemoryVectorStore()
        store.add(load_runbook_chunks(DATA_DIR / "runbooks"))
        _retriever = AgenticRetriever(store)
    return _retriever
