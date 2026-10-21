"""Embeddings and vector stores for the runbook knowledge base.

- HashingEmbedder: offline, deterministic feature-hashing embedder (no model download), good enough
  for keyword-heavy telecom text like alarm codes and interface names.
- InMemoryVectorStore: numpy cosine similarity, used by default and in tests.
- PgVectorStore: PostgreSQL + pgvector, used when VECTOR_STORE=pgvector.
Swap the embedder for Vertex AI text embeddings in production by implementing `embed()`.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import numpy as np

# Telecom synonyms so "VoLTE" and "IMS registration" or "N2" and "NGAP" land near each other.
SYNONYMS = {
    "volte": "ims sip", "vonr": "ims sip", "ngap": "n2", "sctp": "n2 sctp", "pfcp": "n4 pfcp",
    "s1ap": "s1-mme", "diameter": "diameter cx s6a", "attach": "attach s6a mme", "los": "link_down",
    "fibre": "link_down backhaul", "fiber": "link_down backhaul", "503": "sip_5xx",
}


def _tokens(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9_\-]+", text.lower())
    out: list[str] = []
    for w in words:
        out.append(w)
        if w in SYNONYMS:
            out.extend(SYNONYMS[w].split())
    return out


class Embedder(Protocol):
    dim: int

    def embed(self, texts: list[str]) -> np.ndarray: ...


class HashingEmbedder:
    def __init__(self, dim: int = 512) -> None:
        self.dim = dim

    def _bucket(self, feature: str) -> int:
        return int.from_bytes(hashlib.md5(feature.encode()).digest()[:4], "little") % self.dim

    def embed(self, texts: list[str]) -> np.ndarray:
        mat = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            toks = _tokens(t)
            for tok in toks:
                mat[i, self._bucket(tok)] += 1.0
            for a, b in zip(toks, toks[1:]):
                mat[i, self._bucket(f"{a} {b}")] += 0.5
        mat = np.log1p(mat)
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        return mat / np.where(norms == 0, 1, norms)


@dataclass
class Chunk:
    id: str
    doc_id: str
    title: str
    section: str
    text: str
    metadata: dict = field(default_factory=dict)


@dataclass
class Hit:
    chunk: Chunk
    score: float


def load_runbook_chunks(folder: Path) -> list[Chunk]:
    """Split each markdown runbook into one chunk per '## ' section, keeping front-matter metadata."""
    chunks: list[Chunk] = []
    for path in sorted(folder.glob("*.md")):
        raw = path.read_text()
        meta: dict = {}
        body = raw
        if raw.startswith("---"):
            _, fm, body = raw.split("---", 2)
            for line in fm.strip().splitlines():
                k, _, v = line.partition(":")
                meta[k.strip()] = v.strip()
        meta["alarm_codes"] = [c.strip() for c in meta.get("alarm_codes", "").split(",") if c.strip()]
        meta["actions"] = [a.strip() for a in meta.get("actions", "").split(",") if a.strip()]
        doc_id = meta.get("id", path.stem)
        title = meta.get("title", path.stem)
        sections = re.split(r"\n(?=## )", body.strip())
        for j, sec in enumerate(sections):
            heading = sec.splitlines()[0].lstrip("# ").strip()
            chunks.append(Chunk(id=f"{doc_id}#{j}", doc_id=doc_id, title=title, section=heading,
                                text=f"{title}\n{sec}", metadata=meta))
    return chunks


class VectorStore(Protocol):
    def add(self, chunks: list[Chunk]) -> None: ...

    def search(self, query: str, k: int = 4) -> list[Hit]: ...


class InMemoryVectorStore:
    def __init__(self, embedder: Embedder | None = None) -> None:
        self.embedder = embedder or HashingEmbedder()
        self.chunks: list[Chunk] = []
        self._matrix: np.ndarray | None = None

    def add(self, chunks: list[Chunk]) -> None:
        self.chunks.extend(chunks)
        self._matrix = self.embedder.embed([c.text for c in self.chunks])

    def search(self, query: str, k: int = 4) -> list[Hit]:
        if self._matrix is None:
            return []
        q = self.embedder.embed([query])[0]
        scores = self._matrix @ q
        order = np.argsort(-scores)[:k]
        return [Hit(self.chunks[i], float(scores[i])) for i in order]


class PgVectorStore:
    """pgvector-backed store. Requires `CREATE EXTENSION vector` (the pgvector/pgvector image has it)."""

    def __init__(self, url: str, embedder: Embedder | None = None, table: str = "runbook_chunks") -> None:
        import psycopg

        self.embedder = embedder or HashingEmbedder()
        self.table = table
        self.conn = psycopg.connect(url, autocommit=True)
        self.conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        self.conn.execute(f"CREATE TABLE IF NOT EXISTS {table} (id TEXT PRIMARY KEY, doc_id TEXT, title TEXT, "
                          f"section TEXT, text TEXT, metadata JSONB, embedding vector({self.embedder.dim}))")

    def add(self, chunks: list[Chunk]) -> None:
        vecs = self.embedder.embed([c.text for c in chunks])
        for c, v in zip(chunks, vecs):
            self.conn.execute(
                f"INSERT INTO {self.table} VALUES (%s,%s,%s,%s,%s,%s,%s::vector) ON CONFLICT (id) DO UPDATE "
                f"SET text = EXCLUDED.text, embedding = EXCLUDED.embedding",
                (c.id, c.doc_id, c.title, c.section, c.text, json.dumps(c.metadata),
                 "[" + ",".join(f"{x:.6f}" for x in v) + "]"))

    def search(self, query: str, k: int = 4) -> list[Hit]:
        q = "[" + ",".join(f"{x:.6f}" for x in self.embedder.embed([query])[0]) + "]"
        rows = self.conn.execute(
            f"SELECT id, doc_id, title, section, text, metadata, 1 - (embedding <=> %s::vector) AS score "
            f"FROM {self.table} ORDER BY embedding <=> %s::vector LIMIT %s", (q, q, k)).fetchall()
        return [Hit(Chunk(r[0], r[1], r[2], r[3], r[4], r[5] if isinstance(r[5], dict) else json.loads(r[5])),
                    float(r[6])) for r in rows]
