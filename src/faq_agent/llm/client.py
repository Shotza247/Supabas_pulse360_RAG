"""Basic vector RAG with direct hosted chat, without agent orchestration."""

import json
import logging
import time

import requests

from faq_agent.prompts.templates import ABSTENTION, SYSTEM_PROMPT

logger = logging.getLogger("faq_agent")

# Retry configuration — mirrors embedder.py.  Three attempts with exponential
# back-off so a single transient 429 or 502 from HuggingFace does not fail
# the user's request permanently.
_MAX_RETRIES = 3
_RETRY_STATUSES = {429, 500, 502, 503, 504}


def _post_with_retry(url: str, headers: dict, payload: dict, timeout: int) -> requests.Response:
    """POST with exponential back-off on 429 / 5xx responses.

    Attempts: 1 (immediate) → 2 (1 s delay) → 3 (2 s delay).
    Non-retryable statuses are raised immediately via raise_for_status().
    """
    delay = 1.0
    for attempt in range(1, _MAX_RETRIES + 1):
        response = requests.post(url, headers=headers, json=payload, timeout=timeout)
        if response.status_code not in _RETRY_STATUSES:
            response.raise_for_status()
            return response
        if attempt == _MAX_RETRIES:
            logger.error(
                "llm_request_failed url=%s status=%d attempt=%d/%d",
                url,
                response.status_code,
                attempt,
                _MAX_RETRIES,
            )
            response.raise_for_status()
        logger.warning(
            "llm_request_retrying url=%s status=%d attempt=%d/%d delay=%.1fs",
            url,
            response.status_code,
            attempt,
            _MAX_RETRIES,
            delay,
        )
        time.sleep(delay)
        delay *= 2
    raise RuntimeError("Retry loop exited without returning")  # pragma: no cover


def validate_answer(raw, matches):
    abstain = {"answer": ABSTENTION, "sources": [], "status": "insufficient_evidence"}
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return abstain
    if not isinstance(data, dict):
        return abstain
    answer, ids = data.get("answer"), data.get("citation_ids")
    evidence = {hit["chunk_id"]: hit for hit in matches}
    if (
        not isinstance(answer, str)
        or not answer.strip()
        or len(answer) > 6000
        or not isinstance(ids, list)
        or not ids
        or any(not isinstance(i, str) or i not in evidence for i in ids)
    ):
        return abstain
    return {
        "answer": answer.strip(),
        "status": "answered",
        "sources": [
            {
                k: evidence[i].get(k)
                for k in (
                    "chunk_id",
                    "title",
                    "section",
                    "vector_score",
                    "rerank_score",
                    "filename",
                    "page",
                    "document_id",
                )
                if k in evidence[i]
            }
            for i in dict.fromkeys(ids)
        ],
    }


# Fields sent to the LLM as evidence.  Restricted to what the model needs
# to answer and cite — internal IDs, scores and filenames are excluded to
# reduce token usage and avoid leaking storage details into the prompt.
_LLM_EVIDENCE_FIELDS = ("chunk_id", "text", "title", "section")


def synthesize(question, matches, settings):
    if not matches:
        return {"answer": ABSTENTION, "sources": [], "status": "insufficient_evidence"}
    if settings.llm_provider != "huggingface":
        raise ValueError("Basic hosted synthesis currently supports huggingface only")
    if not settings.hf_token or not settings.llm_model:
        raise ValueError("Configure HF_TOKEN and LLM_MODEL")
    # Strip each match to only the fields the LLM needs.  The full matches
    # list is kept separately so validate_answer() can still build complete
    # citation objects (including vector_score, filename, page, etc.) for
    # the API response — those never leave the server boundary.
    evidence = [
        {k: m[k] for k in _LLM_EVIDENCE_FIELDS if k in m}
        for m in matches
    ]
    response = _post_with_retry(
        settings.llm_base_url.rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {settings.hf_token}"},
        payload={
            "model": settings.llm_model,
            "temperature": 0,
            "max_tokens": 700,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps({"question": question, "evidence": evidence}),
                },
            ],
        },
        timeout=settings.request_timeout,
    )
    raw = response.json()["choices"][0]["message"]["content"]
    # validate_answer uses the original full matches so citations in the
    # response carry scores, filenames and page numbers for the caller.
    return validate_answer(raw, matches)
