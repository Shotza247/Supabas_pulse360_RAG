import hashlib
import re

from faq_agent.schemas import Chunk, SourceDocument

SECTION_RE = re.compile(r"^(#{1,3}\s+|[A-Z][A-Z0-9 &/.-]{3,}:?$)")


def chunk_document(
    document: SourceDocument,
    *,
    max_chars: int = 1200,
    overlap_chars: int = 180,
) -> list[Chunk]:
    if not 0 <= overlap_chars < max_chars:
        raise ValueError("Require 0 <= overlap_chars < max_chars")
    sections = _split_sections(document.text)
    chunks: list[Chunk] = []

    for section_index, (section_name, section_text) in enumerate(sections):
        for index, text in enumerate(_window_text(section_text, max_chars, overlap_chars)):
            chunk_key = f"{document.source_id}:{section_index}:{section_name}:{index}:{text}"
            chunk_id = hashlib.sha256(chunk_key.encode("utf-8")).hexdigest()[:24]
            chunks.append(
                Chunk(
                    chunk_id=chunk_id,
                    source_id=document.source_id,
                    text=text,
                    metadata={
                        **document.metadata,
                        "title": document.title,
                        "section": section_name,
                        "chunk_index": index,
                    },
                )
            )
    return chunks


def chunk_documents(documents: list[SourceDocument]) -> list[Chunk]:
    chunks: list[Chunk] = []
    for document in documents:
        chunks.extend(chunk_document(document))
    return chunks


def _split_sections(text: str) -> list[tuple[str, str]]:
    current_name = "FAQ"
    current_lines: list[str] = []
    sections: list[tuple[str, str]] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line and SECTION_RE.match(line) and len(line) <= 80:
            if current_lines:
                sections.append((current_name, "\n".join(current_lines).strip()))
                current_lines = []
            current_name = line.lstrip("#").strip().rstrip(":") or "FAQ"
        else:
            current_lines.append(raw_line)

    if current_lines:
        sections.append((current_name, "\n".join(current_lines).strip()))

    return [(name, body) for name, body in sections if body]


def _window_text(text: str, max_chars: int, overlap_chars: int) -> list[str]:
    # Prefer whitespace boundaries while enforcing a hard character limit.
    windows = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            boundary = max(
                text.rfind("\n", start + max_chars // 2, end),
                text.rfind(" ", start + max_chars // 2, end),
            )
            if boundary > start + overlap_chars:
                end = boundary
        chunk = text[start:end].strip()
        if chunk:
            windows.append(chunk)
        if end == len(text):
            break
        start = max(start + 1, end - overlap_chars)
    return windows
