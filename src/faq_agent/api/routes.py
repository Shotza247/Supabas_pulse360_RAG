import logging
import time
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from faq_agent.api.documents import router as library_router
from faq_agent.config import get_settings
from faq_agent.retrieval.retriever import answer_question, search_question

logger = logging.getLogger("faq_agent")


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    question: str = Field(min_length=2, max_length=800)
    collection_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{2,47}$")
    document_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_scope(self):
        if self.document_id and not self.collection_id:
            raise ValueError("document_id requires collection_id")
        return self

    @field_validator("question")
    @classmethod
    def clean_question(cls, value):
        value = value.strip()
        if len(value) < 2:
            raise ValueError("Question must contain at least two characters")
        return value


class Citation(BaseModel):
    chunk_id: str
    title: str
    section: str
    filename: str | None = None
    page: int | None = None
    document_id: str | None = None


class ServiceErrorDetail(BaseModel):
    message: str
    request_id: str


class ServiceErrorResponse(BaseModel):
    detail: ServiceErrorDetail


class ScoredCitation(Citation):
    vector_score: float = Field(allow_inf_nan=False)
    rerank_score: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)


class AskResponse(BaseModel):
    answer: str
    sources: list[ScoredCitation]
    status: str
    request_id: str


class SearchHit(ScoredCitation):
    text: str
    score: float | None


class SearchResponse(BaseModel):
    matches: list[SearchHit]
    request_id: str


def create_app(answerer=None, searcher=None):
    app = FastAPI(title="FAQ Document RAG", version="0.2.0")
    app.include_router(library_router)
    settings = get_settings()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.origins,
        allow_methods=["POST", "GET"],
        allow_headers=["Content-Type"],
    )

    @app.get("/health")
    def health():
        return {"status": "ok", "kind": "liveness"}

    @app.post(
        "/search",
        response_model=SearchResponse,
        response_model_exclude_unset=True,
        responses={
            503: {
                "model": ServiceErrorResponse,
                "description": "Embedding or vector retrieval is unavailable.",
            }
        },
    )
    def search(body: AskRequest):
        """Vector-only retrieval: source text and vector scores, with no reranking or LLM call."""
        request_id = str(uuid4())
        try:
            return {
                "matches": (searcher or search_question)(
                    body.question, body.collection_id, body.document_id
                )
                if body.collection_id
                else (searcher or search_question)(body.question),
                "request_id": request_id,
            }
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 - redact provider errors
            logger.warning("search_failed id=%s error_type=%s", request_id, type(exc).__name__)
            raise HTTPException(
                503,
                detail={"message": "Search is temporarily unavailable.", "request_id": request_id},
            ) from None

    @app.post(
        "/ask",
        response_model=AskResponse,
        response_model_exclude_unset=True,
        responses={
            503: {
                "model": ServiceErrorResponse,
                "description": "Retrieval or hosted answer generation is unavailable.",
            }
        },
    )
    def ask(body: AskRequest):
        """Run the same vector search as /search, then synthesize a cited answer. No reranking."""
        # Blocking provider calls run in FastAPI's thread pool.
        request_id, started = str(uuid4()), time.monotonic()
        try:
            result = (
                (answerer or answer_question)(body.question, body.collection_id, body.document_id)
                if body.collection_id
                else (answerer or answer_question)(body.question)
            )
            return {**result, "request_id": request_id}
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 - public boundary redacts upstream failures
            logger.warning("request_failed id=%s error_type=%s", request_id, type(exc).__name__)
            raise HTTPException(
                503,
                detail={
                    "message": "The assistant is temporarily unavailable.",
                    "request_id": request_id,
                },
            ) from None
        finally:
            logger.info(
                "request_finished id=%s duration_ms=%d",
                request_id,
                (time.monotonic() - started) * 1000,
            )

    return app


app = create_app()
