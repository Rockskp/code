"""Agentic RAG primitives: heading-aware chunking, a BM25 index, and an evidence loop.

The `Retriever` protocol is what the MCP context-layer server exposes. Locally it is backed by
BM25 (deterministic, zero-cost, good for policy manuals full of exact terms like "CPT 72148" or
"structuring"). In AWS it is backed by Bedrock Knowledge Bases / OpenSearch Serverless or
pgvector with hybrid (BM25 + dense) scoring and a reranker. See ADR-0003 in each project.

`gather_evidence` is the *agentic* part: it issues several queries (the original plus
facet queries the caller derives from the case), deduplicates, enforces metadata filters
(e.g. effective date, line of business, jurisdiction) and reports `sufficient=False` when
nothing clears the relevance floor, so the graph routes to a human instead of guessing.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

_TOKEN = re.compile(r"[a-z0-9][a-z0-9\-\.]*")
_STOP = frozenset("a an the and or of to in for on with is are be by as at from that this it".split())


def tokenize(text: str) -> list[str]:
    return [t.strip(".") for t in _TOKEN.findall(text.lower()) if t not in _STOP]


@dataclass(frozen=True)
class Chunk:
    id: str
    doc_id: str
    title: str
    text: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def citation(self) -> str:
        return f"{self.doc_id}#{self.id}"


def chunk_markdown(doc_id: str, text: str, metadata: Mapping[str, Any] | None = None,
                   max_chars: int = 1200) -> list[Chunk]:
    """Split on `##`/`###` headings, then by paragraph if a section is too long."""
    sections = re.split(r"(?m)^(?=#{2,3} )", text)
    chunks: list[Chunk] = []
    for sec in sections:
        sec = sec.strip()
        if not sec:
            continue
        title = sec.splitlines()[0].lstrip("# ").strip()
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:48] or "intro"
        buf, part = "", 0
        for para in sec.split("\n\n"):
            if buf and len(buf) + len(para) > max_chars:
                chunks.append(Chunk(f"{slug}-{part}", doc_id, title, buf.strip(), dict(metadata or {})))
                buf, part = "", part + 1
            buf += para + "\n\n"
        if buf.strip():
            chunks.append(Chunk(f"{slug}-{part}" if part else slug, doc_id, title, buf.strip(),
                                dict(metadata or {})))
    return chunks


@dataclass(frozen=True)
class Hit:
    chunk: Chunk
    score: float


class Retriever(Protocol):
    def search(self, query: str, k: int = 5, filters: Mapping[str, Any] | None = None) -> list[Hit]: ...


class BM25Index:
    def __init__(self, chunks: Iterable[Chunk], k1: float = 1.4, b: float = 0.75):
        self.chunks = list(chunks)
        self.k1, self.b = k1, b
        self._tf = [Counter(tokenize(c.title + " " + c.text)) for c in self.chunks]
        self._len = [sum(tf.values()) for tf in self._tf]
        self._avg = (sum(self._len) / len(self._len)) if self._len else 0.0
        df: Counter[str] = Counter()
        for tf in self._tf:
            df.update(tf.keys())
        n = len(self.chunks)
        self._idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}

    @staticmethod
    def _passes(meta: Mapping[str, Any], filters: Mapping[str, Any] | None) -> bool:
        if not filters:
            return True
        for key, want in filters.items():
            have = meta.get(key)
            if have is None:
                continue  # unscoped chunks apply everywhere
            if isinstance(have, list | tuple | set):
                if want not in have:
                    return False
            elif have != want:
                return False
        return True

    def search(self, query: str, k: int = 5, filters: Mapping[str, Any] | None = None) -> list[Hit]:
        q = tokenize(query)
        scored: list[Hit] = []
        for i, c in enumerate(self.chunks):
            if not self._passes(c.metadata, filters):
                continue
            tf, dl, s = self._tf[i], self._len[i], 0.0
            for t in q:
                if t in tf:
                    f = tf[t]
                    s += self._idf.get(t, 0.0) * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / self._avg))
            if s > 0:
                scored.append(Hit(c, round(s, 4)))
        scored.sort(key=lambda h: h.score, reverse=True)
        return scored[:k]


@dataclass(frozen=True)
class Evidence:
    hits: tuple[Hit, ...]
    queries: tuple[str, ...]
    sufficient: bool

    @property
    def citations(self) -> list[str]:
        return [h.chunk.citation() for h in self.hits]

    def as_context(self, max_chars: int = 6000) -> str:
        out, used = [], 0
        for h in self.hits:
            block = f"[{h.chunk.citation()}] {h.chunk.title}\n{h.chunk.text}\n"
            if used + len(block) > max_chars:
                break
            out.append(block)
            used += len(block)
        return "\n".join(out)


def gather_evidence(retriever: Retriever, queries: list[str], filters: Mapping[str, Any] | None = None,
                    k_per_query: int = 3, max_hits: int = 6, min_score: float = 1.0) -> Evidence:
    seen: dict[str, Hit] = {}
    for q in queries:
        for h in retriever.search(q, k=k_per_query, filters=filters):
            cur = seen.get(h.chunk.citation())
            if cur is None or h.score > cur.score:
                seen[h.chunk.citation()] = h
    ranked = sorted(seen.values(), key=lambda h: h.score, reverse=True)
    kept = tuple(h for h in ranked if h.score >= min_score)[:max_hits]
    return Evidence(kept, tuple(queries), sufficient=bool(kept))
