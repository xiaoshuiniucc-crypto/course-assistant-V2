"""
MVP file parsing engine.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

from contracts.models import ParsedFile


def parse(file_path: str) -> ParsedFile:
    ext = Path(file_path).suffix.lower()
    if ext == ".docx":
        return _parse_docx(file_path)
    if ext == ".pdf":
        return _parse_pdf(file_path)
    if ext == ".zip":
        return _parse_zip(file_path)
    return _parse_code(file_path)


def _parse_docx(path: str) -> ParsedFile:
    try:
        from docx import Document
    except ImportError:
        raise RuntimeError("Please install python-docx: pip install python-docx")

    doc = Document(path)
    paragraphs = [para.text.strip() for para in doc.paragraphs if para.text.strip()]
    full_text = "\n".join(paragraphs)
    return ParsedFile(
        text=full_text,
        pages=paragraphs if paragraphs else [full_text],
        code_files={},
        metadata={"format": "docx", "path": path},
    )


def _parse_pdf(path: str) -> ParsedFile:
    try:
        import pdfplumber
    except ImportError:
        raise RuntimeError("Please install pdfplumber: pip install pdfplumber")

    pages = []
    full_parts = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text and text.strip():
                clean = text.strip()
                pages.append(clean)
                full_parts.append(clean)

    full_text = "\n\n".join(full_parts)
    if not full_text.strip():
        return ParsedFile(
            text="",
            pages=[],
            code_files={},
            metadata={"format": "pdf", "path": path, "ocr_needed": True},
        )

    return ParsedFile(
        text=full_text,
        pages=pages,
        code_files={},
        metadata={"format": "pdf", "path": path, "page_count": len(pages)},
    )


def _parse_zip(path: str) -> ParsedFile:
    code_files = {}
    full_parts = []

    with zipfile.ZipFile(path, "r") as zf:
        file_list = [f for f in zf.namelist() if not f.endswith("/")]
        if len(file_list) > 50:
            file_list = file_list[:50]

        for file_name in file_list:
            try:
                data = zf.read(file_name)
                try:
                    text = data.decode("utf-8", errors="replace")
                except Exception:
                    text = data.decode("gbk", errors="replace")

                ext = Path(file_name).suffix.lower()
                if ext in (
                    ".py", ".java", ".cpp", ".c", ".js", ".ts",
                    ".go", ".rs", ".md", ".txt", ".json", ".yaml", ".yml",
                ):
                    code_files[file_name] = text
                    full_parts.append(f"# === {file_name} ===\n{text}")
            except Exception:
                continue

    return ParsedFile(
        text="\n\n".join(full_parts),
        pages=full_parts,
        code_files=code_files,
        metadata={"format": "zip", "path": path, "file_count": len(code_files)},
    )


def _parse_code(path: str) -> ParsedFile:
    ext = Path(path).suffix.lower()
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except UnicodeDecodeError:
        with open(path, encoding="gbk", errors="replace") as f:
            text = f.read()

    lang_map = {
        ".py": "Python",
        ".java": "Java",
        ".cpp": "C++",
        ".c": "C",
        ".js": "JavaScript",
        ".ts": "TypeScript",
        ".go": "Go",
        ".rs": "Rust",
    }
    lang = lang_map.get(ext, "Unknown")

    return ParsedFile(
        text=text,
        pages=[text],
        code_files={Path(path).name: text},
        metadata={"format": "code", "language": lang, "path": path},
    )
