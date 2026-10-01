"""Evaluate the active /search endpoint against reviewed chunk-ID labels."""

import argparse
import json
from pathlib import Path

import requests


def score_results(expected, retrieved):
    expected = set(expected)
    if not expected:
        raise ValueError("Expected chunk IDs must not be empty")
    recall = len(expected.intersection(retrieved)) / len(expected)
    reciprocal_rank = next(
        (1 / rank for rank, item in enumerate(retrieved, 1) if item in expected), 0
    )
    return recall, reciprocal_rank


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("labels", type=Path, help="JSON array with question and expected_chunk_ids")
    parser.add_argument("--api-url", default="http://127.0.0.1:8767")
    parser.add_argument("--collection", help="UI collection ID; omit for the configured CLI corpus")
    args = parser.parse_args()
    rows = json.loads(args.labels.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not rows:
        parser.error("Labels must be a nonempty JSON array")
    for row in rows:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("question"), str)
            or not isinstance(row.get("expected_chunk_ids"), list)
            or not row["expected_chunk_ids"]
            or any(not isinstance(i, str) for i in row["expected_chunk_ids"])
        ):
            parser.error("Each label needs a question and nonempty string expected_chunk_ids")
    scores = []
    for row in rows:
        body = {"question": row["question"]}
        if args.collection:
            body["collection_id"] = args.collection
        response = requests.post(args.api_url.rstrip("/") + "/search", json=body, timeout=90)
        if response.status_code != 200:
            parser.error(f"Search failed with HTTP {response.status_code}; inspect API logs")
        matches = response.json()["matches"]
        scores.append(score_results(row["expected_chunk_ids"], [m["chunk_id"] for m in matches]))
    print(
        json.dumps(
            {
                "questions": len(scores),
                "recall_at_k": sum(s[0] for s in scores) / len(scores),
                "mrr": sum(s[1] for s in scores) / len(scores),
            }
        )
    )


if __name__ == "__main__":
    main()
