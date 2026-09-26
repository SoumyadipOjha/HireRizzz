"""PDF resumes: text PDFs go through Stage 1 like .docx; broken / image-only ones fail visibly."""

from __future__ import annotations

import pytest
from conftest import SAMPLES, FakeLLM

from screening.docx_reader import read_docx
from screening.resume_reader import ResumeReadError, read_pdf, read_resume
from screening.stage0_ingest import ingest
from screening.stage1_extract import run_stage1


def make_pdf(path, lines: list[str]) -> None:
    """A minimal one-page PDF with a real text layer (Helvetica), no extra dependencies."""
    esc = lambda s: s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")  # noqa: E731
    text = "BT /F1 10 Tf 40 800 Td 13 TL " + " ".join(f"({esc(l)}) Tj T*" for l in lines) + " ET"
    stream = text.encode("latin-1", "replace")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 5 0 R /Resources << /Font << /F1 4 0 R >> >> >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
    ]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    path.write_bytes(bytes(out))


def aarav_lines() -> list[str]:
    return [ln for ln in read_docx(SAMPLES / "resumes" / "aarav_sharma.docx").splitlines() if ln.strip()][:60]


def test_text_pdf_is_read(tmp_path):
    f = tmp_path / "aarav.pdf"
    make_pdf(f, aarav_lines())
    text = read_resume(f)
    assert "Aarav Sharma" in text and len(text) > 200


def test_pdf_resume_through_stage1(ctx, data_dir):
    resumes = data_dir / "input" / "resumes"
    for f in resumes.iterdir():
        f.unlink()
    make_pdf(resumes / "aarav_sharma.pdf", aarav_lines())
    ingest(ctx)
    s1 = run_stage1(ctx, FakeLLM())
    assert len(s1.succeeded) == 1 and s1.failed == []
    assert ctx.index.all()[0].display_name == "Aarav Sharma"


def test_bad_pdfs_fail_clearly(tmp_path, ctx, data_dir):
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4 this is not really a pdf")
    with pytest.raises(ResumeReadError):
        read_pdf(broken)
    with pytest.raises(ResumeReadError, match="Unsupported file type '.txt'"):
        read_resume(tmp_path / "cv.txt")

    # an image-only / scanned PDF (no text layer) fails Stage 1 with a readable reason
    resumes = data_dir / "input" / "resumes"
    make_pdf(resumes / "scanned.pdf", [])
    ingest(ctx)
    run_stage1(ctx, FakeLLM())
    entry = next(e for e in ctx.index.all() if e.source_file.endswith("scanned.pdf"))
    assert entry.stages["stage1_extraction"].status == "failed"
    assert "scanned or image-only" in entry.stages["stage1_extraction"].error
