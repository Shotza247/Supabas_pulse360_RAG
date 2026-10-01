from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SourceDocument:
    source_id: str
    path: str
    title: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    source_id: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: str
    text: str
    score: float | None
    metadata: dict[str, Any] = field(default_factory=dict)
    rerank_score: float | None = None

    @property
    def vector_score(self):
        return self.score
