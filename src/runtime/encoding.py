"""UTF-8 helpers for console IO and text decoding."""
from __future__ import annotations

import codecs
import os
import sys
from pathlib import Path
from typing import Iterable

_DEFAULT_FALLBACK_ENCODINGS = ("gb18030",)


def configure_utf8_stdio() -> None:
    """Best-effort UTF-8 setup for Windows terminals and child Python processes."""
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    for stream_name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            continue

    if os.name != "nt":
        return

    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleCP(65001)
        kernel32.SetConsoleOutputCP(65001)
    except Exception:
        # Some embedded environments do not expose a real Windows console.
        pass


def decode_text_bytes(
    data: bytes,
    fallback_encodings: Iterable[str] | None = None,
) -> str:
    """Decode text bytes with UTF-8 first, then a small compatibility fallback set."""
    encodings = ["utf-8-sig", "utf-8"]
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encodings.append("utf-16")
    encodings.extend(fallback_encodings or _DEFAULT_FALLBACK_ENCODINGS)

    seen = set()
    for encoding in encodings:
        if encoding in seen:
            continue
        seen.add(encoding)
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue

    return data.decode("utf-8", errors="replace")


def read_text_file(
    path: str | Path,
    fallback_encodings: Iterable[str] | None = None,
) -> str:
    """Read a text file using the shared UTF-8-first decoding policy."""
    return decode_text_bytes(Path(path).read_bytes(), fallback_encodings=fallback_encodings)
