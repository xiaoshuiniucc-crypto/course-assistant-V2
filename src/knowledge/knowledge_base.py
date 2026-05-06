"""
Knowledge base module.

Features:
- persistent ChromaDB retrieval when available
- keyword fallback retrieval
- course-aware storage and search
- structured search results for evidence/report generation
"""
from __future__ import annotations

import logging
import os
import re
from typing import Dict, List, Optional

from contracts.models import SearchResult
from storage.db import DB

logger = logging.getLogger("knowledge")

DEFAULT_CHROMA_PATH = os.path.join(
    os.environ.get("COURSE_ASSISTANT_DATA", ".data"),
    "chromadb",
)


class KnowledgeBase:
    def __init__(
        self,
        db: DB,
        collection_name: str = "course_knowledge",
        chroma_path: Optional[str] = None,
    ):
        self.db = db
        self.collection_name = collection_name
        self._chroma = None
        self._collection = None
        self._chroma_path = chroma_path or DEFAULT_CHROMA_PATH
        self._init_chroma()

    def _init_chroma(self):
        if os.name == "nt" and os.environ.get("COURSE_ASSISTANT_FORCE_CHROMA", "0") != "1":
            logger.info("ChromaDB disabled on Windows by default; using keyword fallback")
            return

        try:
            import chromadb

            os.makedirs(self._chroma_path, exist_ok=True)
            self._chroma = chromadb.PersistentClient(path=self._chroma_path)
            self._collection = self._chroma.get_or_create_collection(
                name=self.collection_name,
                metadata={"hnsw:space": "cosine"},
            )
            logger.info(
                "ChromaDB enabled at %s with %s records",
                self._chroma_path,
                self._collection.count(),
            )
        except Exception as exc:
            logger.warning("ChromaDB init failed: %s, using keyword fallback", exc)
            self._chroma = None
            self._collection = None

    def add_courseware(
        self,
        courseware_id: str,
        text: str,
        metadata: Optional[Dict] = None,
        course_id: str = "default",
    ):
        chunks = self._chunk(text)
        metadata = metadata or {}

        if self._collection:
            ids = [f"{course_id}_{courseware_id}_{i}" for i in range(len(chunks))]
            metadatas = [
                {
                    "courseware_id": courseware_id,
                    "course_id": course_id,
                    "chunk_index": i,
                    **metadata,
                }
                for i in range(len(chunks))
            ]
            try:
                self._collection.add(ids=ids, documents=chunks, metadatas=metadatas)
            except Exception as exc:
                logger.error("ChromaDB add failed: %s", exc)

        self.db.save_chunks(courseware_id, chunks, course_id=course_id)

    def search(
        self,
        query: str,
        top_k: int = 3,
        courseware_id: Optional[str] = None,
        course_id: Optional[str] = None,
    ) -> List[str]:
        return [
            item.text
            for item in self.search_results(
                query=query,
                top_k=top_k,
                courseware_id=courseware_id,
                course_id=course_id,
            )
        ]

    def search_results(
        self,
        query: str,
        top_k: int = 3,
        courseware_id: Optional[str] = None,
        course_id: Optional[str] = None,
    ) -> List[SearchResult]:
        if self._collection:
            try:
                results = self._search_chroma(query, top_k, courseware_id, course_id)
                if results:
                    return results
            except Exception as exc:
                logger.warning("ChromaDB search failed: %s, fallback to keyword search", exc)

        return self._search_fallback(query, top_k, courseware_id, course_id)

    def _search_chroma(
        self,
        query: str,
        top_k: int,
        courseware_id: Optional[str],
        course_id: Optional[str],
    ) -> List[SearchResult]:
        where_filter = None
        if courseware_id and course_id:
            where_filter = {"$and": [{"courseware_id": courseware_id}, {"course_id": course_id}]}
        elif courseware_id:
            where_filter = {"courseware_id": courseware_id}
        elif course_id:
            where_filter = {"course_id": course_id}

        results = self._collection.query(
            query_texts=[query],
            n_results=top_k,
            where=where_filter,
        )

        if not results or not results.get("documents"):
            return []

        documents = results.get("documents", [[]])[0]
        metadatas = results.get("metadatas", [[]])[0]
        distances = results.get("distances", [[]])[0] if results.get("distances") else []

        normalized: List[SearchResult] = []
        for index, text in enumerate(documents):
            metadata = metadatas[index] if index < len(metadatas) else {}
            distance = distances[index] if index < len(distances) else 0.0
            score = max(0.0, 1.0 - float(distance)) if isinstance(distance, (int, float)) else 0.0
            normalized.append(
                SearchResult(
                    text=text,
                    score=round(score, 4),
                    courseware_id=str(metadata.get("courseware_id", "")),
                    chunk_index=int(metadata.get("chunk_index", index) or index),
                    course_id=str(metadata.get("course_id", course_id or "default")),
                    title=str(metadata.get("title", "")),
                )
            )
        return normalized

    def _search_fallback(
        self,
        query: str,
        top_k: int,
        courseware_id: Optional[str],
        course_id: Optional[str],
    ) -> List[SearchResult]:
        query_tokens = self._tokenize_chinese(query)
        if not query_tokens:
            return []

        rows = self.db._conn.execute(
            "SELECT kc.courseware_id, kc.chunk_index, kc.text, cw.course_id, cw.title "
            "FROM knowledge_chunks kc "
            "LEFT JOIN courseware cw ON cw.id = kc.courseware_id"
        ).fetchall()

        scored: List[SearchResult] = []
        for row in rows:
            if courseware_id and row["courseware_id"] != courseware_id:
                continue
            if course_id and row["course_id"] and row["course_id"] != course_id:
                continue

            doc_tokens = self._tokenize_chinese(row["text"])
            union = query_tokens | doc_tokens
            if not union:
                continue

            score = len(query_tokens & doc_tokens) / len(union)
            if score <= 0:
                continue

            scored.append(
                SearchResult(
                    text=row["text"],
                    score=round(score, 4),
                    courseware_id=row["courseware_id"],
                    chunk_index=int(row["chunk_index"] or 0),
                    course_id=row["course_id"] or "default",
                    title=row["title"] or "",
                )
            )

        scored.sort(key=lambda item: -item.score)
        return scored[:top_k]

    def _tokenize_chinese(self, text: str) -> set:
        tokens = set()
        en_words = re.findall(r"[a-zA-Z]+", text.lower())
        tokens.update(en_words)

        cn_chars = re.findall(r"[\u4e00-\u9fff]", text)
        for index in range(len(cn_chars) - 1):
            tokens.add(cn_chars[index] + cn_chars[index + 1])
        tokens.update(cn_chars)
        return tokens

    def _chunk(self, text: str, max_chunk: int = 800, overlap: int = 100) -> List[str]:
        if len(text) < 2000:
            return [text]

        chunks = self._chunk_by_title(text)
        if chunks and len(chunks) > 1:
            valid_chunks = [chunk for chunk in chunks if len(chunk.strip()) >= 10]
            if len(valid_chunks) >= 2:
                return valid_chunks

        return self._chunk_sliding_window(text, max_chunk, overlap)

    def _chunk_by_title(self, text: str) -> List[str]:
        title_pattern = re.compile(
            r"(?:^|\n)(?:"
            r"第[一二三四五六七八九十\d]+[章节节]"
            r"|\d+\.\d*\s+"
            r"|[一二三四五六七八九十]+[、.]"
            r"|#{1,3}\s+"
            r")"
        )

        parts = title_pattern.split(text)
        if len(parts) <= 1:
            return [text]

        chunks = []
        current = ""
        for index, part in enumerate(parts):
            if index % 2 == 0:
                current += part
            else:
                if current.strip() and len(current.strip()) >= 10:
                    chunks.append(current.strip())
                current = part

        if current.strip() and len(current.strip()) >= 10:
            chunks.append(current.strip())
        return chunks

    def _chunk_sliding_window(
        self,
        text: str,
        max_chunk: int = 800,
        overlap: int = 100,
    ) -> List[str]:
        chunks = []
        start = 0
        while start < len(text):
            end = start + max_chunk
            chunk = text[start:end]
            if end < len(text):
                for sep in ["。", "；", "，", "\n", ".", " "]:
                    last = chunk.rfind(sep)
                    if last > max_chunk // 2:
                        chunk = text[start:start + last + 1]
                        end = start + last + 1
                        break
            chunks.append(chunk)
            start = end - overlap
        return chunks

    def get_chunk_count(self, courseware_id: Optional[str] = None) -> int:
        if courseware_id:
            row = self.db._conn.execute(
                "SELECT COUNT(*) AS cnt FROM knowledge_chunks WHERE courseware_id=?",
                (courseware_id,),
            ).fetchone()
        else:
            row = self.db._conn.execute(
                "SELECT COUNT(*) AS cnt FROM knowledge_chunks"
            ).fetchone()
        return row["cnt"] if row else 0
