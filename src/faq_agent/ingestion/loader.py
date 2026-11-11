import hashlib
import io
import zipfile
from dataclasses import replace
from pathlib import Path

from faq_agent.schemas import SourceDocument

SUPPORTED_EXTENSIONS = {".txt", ".md", ".pdf", ".docx"}

# A .docx is a zip file; the upload size cap applies to the compressed bytes only.
# Refuse archives that would expand far beyond any real document before parsing.
MAX_DOCX_UNCOMPRESSED = 50 * 1024 * 1024


def load_document(path: Path) -> SourceDocument:
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported file type: {path.suffix}")

    if suffix in {".txt", ".md"}:
        text = path.read_text(encoding="utf-8")
    elif suffix == ".pdf":
        text = _read_pdf(path)
    elif suffix == ".docx":
        text = _read_docx(path)
    else:
        raise ValueError(f"Unsupported file type: {path.suffix}")

    clean_text = "\n".join(line.rstrip() for line in text.splitlines()).strip()
    if not clean_text:
        raise ValueError(f"No readable text found in {path}")

    # Use a hash of the resolved absolute path so that two files with the same
    # stem in different directories always get distinct source_ids (finding #15).
    source_id = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:24]
    return SourceDocument(
        source_id=source_id,
        path=str(path),
        title=path.stem,
        text=clean_text,
        metadata={"extension": suffix, "filename": path.name},
    )


def load_documents(folder: Path) -> list[SourceDocument]:
    if not folder.is_dir():
        raise ValueError("Source directory does not exist")
    docs: list[SourceDocument] = []
    for path in sorted(folder.glob("**/*")):
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS:
            if path.is_symlink() or not path.resolve().is_relative_to(folder.resolve()):
                raise ValueError("Source links outside the corpus are not allowed")
            relative = path.relative_to(folder).as_posix()
            source_id = hashlib.sha256(relative.encode()).hexdigest()[:24]
            docs.append(replace(load_document(path), source_id=source_id))
    return docs


def _read_pdf(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def docx_text(data: bytes) -> str:
    """Extract text from .docx bytes in document order, including table rows."""
    from docx import Document
    from docx.table import Table

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if sum(item.file_size for item in archive.infolist()) > MAX_DOCX_UNCOMPRESSED:
            raise ValueError("DOCX expands beyond the allowed size")
    lines: list[str] = []
    for block in Document(io.BytesIO(data)).iter_inner_content():
        if isinstance(block, Table):
            for row in block.rows:
                cells = [cell.text.strip() for cell in row.cells]
                if any(cells):
                    lines.append(" | ".join(cells))
        else:
            lines.append(block.text)
    return "\n".join(lines)


def _read_docx(path: Path) -> str:
    return docx_text(path.read_bytes())
