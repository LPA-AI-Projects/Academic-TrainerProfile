from pathlib import Path

from docx import Document
from pypdf import PdfReader

TEXT_EXTENSIONS = {".txt", ".md", ".rtf"}


def _read_pdf(path: Path) -> str:
    reader = PdfReader(str(path))
    pages = []
    for page in reader.pages:
        text = page.extract_text() or ""
        pages.append(text.strip())
    return "\n\n".join(pages).strip()


def _read_docx(path: Path) -> str:
    document = Document(str(path))
    return "\n".join(p.text for p in document.paragraphs if p.text).strip()


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore").strip()


def read_text_from_path(file_path: str) -> str:
    path = Path(file_path)
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"File path not found: {file_path}")

    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _read_pdf(path)
    if suffix == ".docx":
        return _read_docx(path)
    if suffix in TEXT_EXTENSIONS:
        return _read_text(path)

    raise ValueError(
        f"Unsupported file type '{suffix}'. Use one of: .pdf, .docx, .txt, .md, .rtf"
    )


def truncate_inputs(cv_text: str, outline_texts: list[str]) -> tuple[str, list[str]]:
    # Intentionally pass through full extracted text without clipping.
    return cv_text, outline_texts
