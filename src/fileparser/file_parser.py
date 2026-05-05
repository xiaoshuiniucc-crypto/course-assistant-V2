"""
File parsing helpers.
"""
from __future__ import annotations

from pathlib import Path


class FileParser:
    """Parse supported files into plain text."""

    SUPPORTED_EXTS = {".pdf", ".docx", ".doc", ".txt", ".md", ".csv"}

    def parse(self, file_path: str) -> str:
        ext = Path(file_path).suffix.lower()
        if ext not in self.SUPPORTED_EXTS:
            raise ValueError(
                f"Unsupported file type: {ext}. "
                f"Supported: {', '.join(sorted(self.SUPPORTED_EXTS))}"
            )

        if ext == ".pdf":
            return self._parse_pdf(file_path)
        if ext in (".docx", ".doc"):
            return self._parse_docx(file_path)
        return self._parse_text(file_path)

    def _parse_pdf(self, file_path: str) -> str:
        try:
            import PyPDF2

            text_parts = []
            with open(file_path, "rb") as f:
                reader = PyPDF2.PdfReader(f)
                for page in reader.pages:
                    text = page.extract_text()
                    if text:
                        text_parts.append(text)
            return "\n".join(text_parts)
        except ImportError:
            try:
                from pdfminer.high_level import extract_text

                return extract_text(file_path)
            except ImportError:
                return f"[PDF parser unavailable for {file_path}; install PyPDF2 or pdfminer]"

    def _parse_docx(self, file_path: str) -> str:
        try:
            from docx import Document

            doc = Document(file_path)
            return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        except ImportError:
            return f"[DOCX parser unavailable for {file_path}; install python-docx]"

    def _parse_text(self, file_path: str) -> str:
        encodings = ["utf-8", "gbk", "gb2312", "latin-1"]
        for encoding in encodings:
            try:
                with open(file_path, "r", encoding=encoding) as f:
                    return f.read()
            except (UnicodeDecodeError, UnicodeError):
                continue
        return f"[Unable to decode file: {file_path}]"

    def detect_title(self, file_path: str, content: str) -> str:
        name = Path(file_path).stem
        first_line = content.strip().split("\n")[0].strip()
        if first_line and len(first_line) < 100 and not first_line.startswith("#"):
            return first_line
        return name
