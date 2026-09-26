""".docx -> plain text, preserving reading order.

Covers what resume templates actually use: body paragraphs, tables (incl.
merged and nested cells), headers/footers (contact details often live
there) and text boxes (python-docx ignores these by default; Word stores
them twice — DrawingML + VML fallback — so only one copy is taken).
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

import docx
from lxml import etree
from docx.document import Document as _Document
from docx.opc.exceptions import PackageNotFoundError
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

_NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
}


class DocxReadError(Exception):
    """File is not a readable .docx."""


def _clean(text: str) -> str:
    text = text.replace(" ", " ").replace("​", "")
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def _table_lines(table: Table) -> list[str]:
    lines: list[str] = []
    for row in table.rows:
        cells: list[str] = []
        seen: set[int] = set()
        for cell in row.cells:
            if id(cell._tc) in seen:  # horizontally merged cells repeat the same <w:tc>
                continue
            seen.add(id(cell._tc))
            parts: list[str] = []
            for block in cell.iter_inner_content():
                if isinstance(block, Paragraph):
                    t = _clean(block.text)
                    if t:
                        parts.append(t)
                else:
                    parts.extend(_table_lines(block))
            if parts:
                cells.append(" ".join(parts))
        if cells:
            lines.append(" | ".join(cells))
    return lines


def _textbox_lines(element) -> list[str]:
    lines: list[str] = []
    boxes = etree._Element.xpath(
        element,
        ".//w:txbxContent[not(ancestor::mc:Fallback)][not(ancestor::w:txbxContent)]", namespaces=_NS
    )
    for box in boxes:
        for p in box.iter(qn("w:p")):
            t = _clean("".join(x.text or "" for x in p.iter(qn("w:t"))))
            if t:
                lines.append(t)
    return lines


def _header_footer_lines(document: _Document) -> tuple[list[str], list[str]]:
    headers: list[str] = []
    footers: list[str] = []
    for section in document.sections:
        for part, out in ((section.header, headers), (section.footer, footers)):
            if part.is_linked_to_previous and out:
                continue
            for block in part.iter_inner_content():
                lines = [_clean(block.text)] if isinstance(block, Paragraph) else _table_lines(block)
                for line in lines:
                    if line and line not in out:
                        out.append(line)
            for line in _textbox_lines(part._element):
                if line not in out:
                    out.append(line)
    return headers, footers


def read_docx(path: Path) -> str:
    path = Path(path)
    if path.suffix.lower() != ".docx":
        raise DocxReadError(f"Unsupported file type '{path.suffix}' (only .docx is supported)")
    try:
        document = docx.Document(str(path))
    except (PackageNotFoundError, zipfile.BadZipFile, KeyError, ValueError) as e:
        raise DocxReadError(f"Not a readable .docx file: {e}") from e

    headers, footers = _header_footer_lines(document)
    body: list[str] = []
    for block in document.iter_inner_content():
        if isinstance(block, Paragraph):
            t = _clean(block.text)
            if t:
                body.append(t)
            body.extend(_textbox_lines(block._p))
        else:
            body.extend(_table_lines(block))
            body.extend(_textbox_lines(block._tbl))

    lines = headers + body + [f for f in footers if f not in headers]
    return "\n".join(lines).strip()
