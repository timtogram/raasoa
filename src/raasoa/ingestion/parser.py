from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ParsedSection:
    text: str
    title: str | None = None
    section_type: str = "text"  # 'text', 'table', 'code', 'heading'


@dataclass
class ParsedDocument:
    title: str | None
    full_text: str
    sections: list[ParsedSection] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


def parse_text(content: str, filename: str) -> ParsedDocument:
    lines = content.strip().split("\n")
    title = lines[0].strip("# ").strip() if lines else filename
    return ParsedDocument(
        title=title,
        full_text=content,
        sections=[ParsedSection(text=content, title=title)],
        metadata={"format": "text", "filename": filename},
    )


def parse_pdf(data: bytes, filename: str) -> ParsedDocument:
    try:
        import io

        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        pages: list[str] = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                pages.append(text)

        full_text = "\n\n".join(pages)
        title = reader.metadata.title if reader.metadata and reader.metadata.title else filename

        sections = [
            ParsedSection(text=page_text, title=f"Page {i + 1}")
            for i, page_text in enumerate(pages)
        ]

        return ParsedDocument(
            title=title,
            full_text=full_text,
            sections=sections,
            metadata={"format": "pdf", "filename": filename, "pages": len(pages)},
        )
    except ImportError as err:
        msg = "pypdf is required for PDF parsing. Install with: uv sync --extra parsing"
        raise ImportError(msg) from err


def parse_docx(data: bytes, filename: str) -> ParsedDocument:
    try:
        import io

        from docx import Document as DocxDocument

        doc = DocxDocument(io.BytesIO(data))
        paragraphs: list[str] = []
        sections: list[ParsedSection] = []
        current_title: str | None = None

        for para in doc.paragraphs:
            text = para.text.strip()
            if not text:
                continue

            if para.style and para.style.name and para.style.name.startswith("Heading"):
                current_title = text
                sections.append(ParsedSection(text=text, title=text, section_type="heading"))
            else:
                sections.append(ParsedSection(text=text, title=current_title))

            paragraphs.append(text)

        full_text = "\n\n".join(paragraphs)
        title = paragraphs[0] if paragraphs else filename

        return ParsedDocument(
            title=title,
            full_text=full_text,
            sections=sections,
            metadata={"format": "docx", "filename": filename},
        )
    except ImportError as err:
        msg = "python-docx is required for DOCX parsing. Install with: uv sync --extra parsing"
        raise ImportError(msg) from err


def parse_file(data: bytes, filename: str) -> ParsedDocument:
    suffix = Path(filename).suffix.lower()

    if suffix == ".pdf":
        return parse_pdf(data, filename)
    elif suffix in (".docx", ".doc"):
        return parse_docx(data, filename)
    elif suffix in (".txt", ".md", ".csv", ".log", ".json", ".xml", ".html"):
        return parse_text(data.decode("utf-8", errors="replace"), filename)
    else:
        return parse_text(data.decode("utf-8", errors="replace"), filename)
