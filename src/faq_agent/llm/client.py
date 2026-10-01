from faq_agent.prompts.templates import ABSTENTION, SYSTEM_PROMPT

"""Basic vector RAG with direct hosted chat, without agent orchestration."""

import json

import requests


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


def synthesize(question, matches, settings):
    if not matches:
        return {"answer": ABSTENTION, "sources": [], "status": "insufficient_evidence"}
    if settings.llm_provider != "huggingface":
        raise ValueError("Basic hosted synthesis currently supports huggingface only")
    if not settings.hf_token or not settings.llm_model:
        raise ValueError("Configure HF_TOKEN and LLM_MODEL")
    response = requests.post(
        settings.llm_base_url.rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {settings.hf_token}"},
        json={
            "model": settings.llm_model,
            "temperature": 0,
            "max_tokens": 700,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps({"question": question, "evidence": matches}),
                },
            ],
        },
        timeout=settings.request_timeout,
    )
    response.raise_for_status()
    raw = response.json()["choices"][0]["message"]["content"]
    return validate_answer(raw, matches)
