"""
文件解析器
支持 PDF / DOCX / TXT / Markdown 提取纯文本
"""
from __future__ import annotations
import os
from typing import Optional
from pathlib import Path


class FileParser:
    """多格式文件解析 → 纯文本"""

    SUPPORTED_EXTS = {".pdf", ".docx", ".doc", ".txt", ".md", ".csv"}

    def parse(self, file_path: str) -> str:
        """根据文件扩展名选择解析方法"""
        ext = Path(file_path).suffix.lower()
        if ext not in self.SUPPORTED_EXTS:
            raise ValueError(
                f"不支持的文件格式: {ext}，"
                f"支持: {', '.join(self.SUPPORTED_EXTS)}"
            )

        if ext == ".pdf":
            return self._parse_pdf(file_path)
        elif ext in (".docx", ".doc"):
            return self._parse_docx(file_path)
        else:
            return self._parse_text(file_path)

    def _parse_pdf(self, file_path: str) -> str:
        """解析 PDF 文件"""
        try:
            import PyPDF2
            text_parts = []
            with open(file_path, "rb") as f:
                reader = PyPDF2.PdfReader(f)
                for page in reader.pages:
                    t = page.extract_text()
                    if t:
                        text_parts.append(t)
            return "\n".join(text_parts)
        except ImportError:
            # fallback: 用 pdfminer
            try:
                from pdfminer.high_level import extract_text
                return extract_text(file_path)
            except ImportError:
                return f"[PDF解析不可用: {file_path}，请安装 PyPDF2 或 pdfminer]"

    def _parse_docx(self, file_path: str) -> str:
        """解析 DOCX 文件"""
        try:
            from docx import Document
            doc = Document(file_path)
            return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        except ImportError:
            return f"[DOCX解析不可用: {file_path}，请安装 python-docx]"

    def _parse_text(self, file_path: str) -> str:
        """解析纯文本/Markdown/CSV"""
        encodings = ["utf-8", "gbk", "gb2312", "latin-1"]
        for enc in encodings:
            try:
                with open(file_path, "r", encoding=enc) as f:
                    return f.read()
            except (UnicodeDecodeError, UnicodeError):
                continue
        return f"[无法解码文件: {file_path}]"

    def detect_title(self, file_path: str, content: str) -> str:
        """从文件名或内容推断标题"""
        name = Path(file_path).stem
        # 尝试从内容第一行提取
        first_line = content.strip().split("\n")[0].strip()
        if first_line and len(first_line) < 100 and not first_line.startswith("#"):
            return first_line
        return name
