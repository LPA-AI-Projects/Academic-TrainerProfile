from pathlib import Path

from docx import Document
from pypdf import PdfReader

from ..config import get_settings


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
    settings = get_settings()
    cv_trimmed = cv_text[: settings.max_cv_chars]
    outlines_trimmed = [txt[: settings.max_outline_chars] for txt in outline_texts]
    total = len(cv_trimmed) + sum(len(x) for x in outlines_trimmed)
    if total <= settings.max_total_input_chars:
        return cv_trimmed, outlines_trimmed

    overflow = total - settings.max_total_input_chars
    cv_safe_floor = min(len(cv_trimmed), max(8000, settings.max_cv_chars // 3))
    cv_new_len = max(cv_safe_floor, len(cv_trimmed) - overflow)
    cv_trimmed = cv_trimmed[:cv_new_len]
    return cv_trimmed, outlines_trimmed
