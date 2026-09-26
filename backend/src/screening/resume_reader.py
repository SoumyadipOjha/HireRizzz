"""Resume file -> plain text. Supports .docx (docx_reader.py) and text-based .pdf.

Scanned / image-only PDFs have no text layer; they come out (nearly) empty and
fail Stage 1's `min_resume_chars` check with a clear message instead of being
scored on nothing.
"""

from __future__ import annotations

import re
from pathlib import Path

from .docx_reader import DocxReadError, read_docx

SUPPORTED = (".docx", ".pdf")


class ResumeReadError(DocxReadError):
    """File is not a readable resume (unsupported type, corrupt, encrypted...)."""


def read_pdf(path: Path) -> str:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                reader.decrypt("")  # many "protected" PDFs only restrict editing
            except Exception as e:
                raise ResumeReadError("PDF is password-protected") from e
        pages = [(p.extract_text() or "") for p in reader.pages]
    except ResumeReadError:
        raise
    except (PdfReadError, ValueError, KeyError, OSError, TypeError) as e:
        raise ResumeReadError(f"Not a readable .pdf file: {e}") from e
    lines = []
    for page in pages:
        for line in page.splitlines():
            line = re.sub(r"[ \t ]+", " ", line.replace("​", "")).strip()
            if line:
                lines.append(line)
    return "\n".join(lines).strip()


def read_resume(path: Path) -> str:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return read_docx(path)
    if suffix == ".pdf":
        return read_pdf(path)
    raise ResumeReadError(f"Unsupported file type '{path.suffix}' (supported: {', '.join(SUPPORTED)})")
