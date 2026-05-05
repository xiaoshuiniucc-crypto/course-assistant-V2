"""
知识库模块
ChromaDB 向量检索 + 中文分词 fallback
"""
from __future__ import annotations
import re
import uuid
import logging
from typing import List, Optional, Dict

from storage.db import DB

logger = logging.getLogger("knowledge")


class KnowledgeBase:
    """
    向量知识库
    - 优先使用 ChromaDB 进行语义检索
    - fallback: 字符级 2-gram + 单字 + 英文词的关键词匹配
    - 分块策略: 短文单块 / 章节标题分割 / 滑动窗口
    """

    def __init__(self, db: DB, collection_name: str = "course_knowledge"):
        self.db = db
        self.collection_name = collection_name
        self._chroma = None
        self._collection = None
        self._init_chroma()

    def _init_chroma(self):
        """初始化 ChromaDB"""
        try:
            import chromadb
            self._chroma = chromadb.Client()   # 内存模式
            self._collection = self._chroma.get_or_create_collection(
                name=self.collection_name,
                metadata={"hnsw:space": "cosine"}
            )
            logger.info("ChromaDB 向量检索已启用")
        except Exception as e:
            logger.warning(f"ChromaDB 初始化失败: {e}, 使用关键词 fallback")
            self._chroma = None
            self._collection = None

    # ── 文档入库 ────────────────────────────

    def add_courseware(self, courseware_id: str, text: str,
                       metadata: Optional[Dict] = None):
        """
        将课件文本加入知识库
        1. 分块
        2. 存入 ChromaDB (向量) + SQLite (原文)
        """
        chunks = self._chunk(text)
        metadata = metadata or {}

        if self._collection:
            # 向量入库
            ids = [f"{courseware_id}_{i}" for i in range(len(chunks))]
            metas = [{"courseware_id": courseware_id, "chunk_index": i, **metadata}
                     for i in range(len(chunks))]
            try:
                self._collection.add(
                    ids=ids,
                    documents=chunks,
                    metadatas=metas,
                )
                logger.info(f"ChromaDB: 已入库 {len(chunks)} 个分块 ({courseware_id})")
            except Exception as e:
                logger.error(f"ChromaDB 入库失败: {e}")

        # 同时存入 SQLite（fallback 用）
        self.db.save_chunks(courseware_id, chunks)

    # ── 检索 ────────────────────────────────

    def search(self, query: str, top_k: int = 3,
               courseware_id: Optional[str] = None) -> List[str]:
        """
        语义检索
        1. 优先 ChromaDB 向量检索
        2. Fallback: 关键词匹配
        """
        if self._collection:
            try:
                results = self._search_chroma(query, top_k, courseware_id)
                if results:
                    return results
            except Exception as e:
                logger.warning(f"ChromaDB 检索失败: {e}, fallback")

        return self._search_fallback(query, top_k, courseware_id)

    def _search_chroma(self, query: str, top_k: int,
                       courseware_id: Optional[str]) -> List[str]:
        """ChromaDB 向量检索"""
        where_filter = None
        if courseware_id:
            where_filter = {"courseware_id": courseware_id}

        results = self._collection.query(
            query_texts=[query],
            n_results=top_k,
            where=where_filter,
        )

        if results and results.get("documents"):
            return results["documents"][0]
        return []

    def _search_fallback(self, query: str, top_k: int,
                         courseware_id: Optional[str]) -> List[str]:
        """
        关键词 fallback 检索
        使用字符级 2-gram + 单字 + 英文词 tokenization
        """
        query_tokens = self._tokenize_chinese(query)
        if not query_tokens:
            return []

        all_rows = self.db._conn.execute(
            "SELECT courseware_id, text FROM knowledge_chunks"
        ).fetchall()

        scored = []
        for row in all_rows:
            if courseware_id and row["courseware_id"] != courseware_id:
                continue
            doc_tokens = self._tokenize_chinese(row["text"])
            # Jaccard 相似度
            intersection = query_tokens & doc_tokens
            union = query_tokens | doc_tokens
            if union:
                score = len(intersection) / len(union)
            else:
                score = 0
            if score > 0:
                scored.append((score, row["text"]))

        scored.sort(key=lambda x: -x[0])
        return [text for _, text in scored[:top_k]]

    # ── 中文分词 ────────────────────────────

    def _tokenize_chinese(self, text: str) -> set:
        """
        中文分词（无需 jieba）
        - 2-gram 字符组合
        - 单字
        - 英文单词提取
        """
        tokens = set()
        # 英文单词
        en_words = re.findall(r'[a-zA-Z]+', text.lower())
        tokens.update(en_words)

        # 中文字符
        cn_chars = re.findall(r'[\u4e00-\u9fff]', text)

        # 2-gram
        for i in range(len(cn_chars) - 1):
            tokens.add(cn_chars[i] + cn_chars[i + 1])

        # 单字
        tokens.update(cn_chars)

        return tokens

    # ── 分块策略 ────────────────────────────

    def _chunk(self, text: str, max_chunk: int = 800,
               overlap: int = 100) -> List[str]:
        """
        智能分块
        1. 短文本 (<2000字符): 整体作为一个分块
        2. 有章节标题: 按标题分割
        3. 长文本无标题: 滑动窗口
        """
        # 短文本快速路径
        if len(text) < 2000:
            return [text]

        # 尝试章节标题分割
        chunks = self._chunk_by_title(text)
        if chunks and len(chunks) > 1:
            # 过滤掉太短的分块（内容少于10字符的可能是纯标题）
            valid_chunks = [c for c in chunks if len(c.strip()) >= 10]
            if len(valid_chunks) >= 2:
                return valid_chunks
            # 如果大部分分块都被过滤了，说明分块策略不合适

        # 滑动窗口 fallback
        return self._chunk_sliding_window(text, max_chunk, overlap)

    def _chunk_by_title(self, text: str) -> List[str]:
        """按章节标题分割"""
        # 匹配: 第X章, X. 标题, X.X 标题, 一、标题 等
        title_pattern = re.compile(
            r'(?:^|\n)'
            r'(?:'
            r'第[一二三四五六七八九十\d]+[章章节]'  # 第三章
            r'|\d+\.\d*\s+'                          # 3.1 标题
            r'|[一二三四五六七八九十]+[、.]'           # 三、标题
            r'|#{1,3}\s+'                             # # Markdown 标题
            r')',
        )

        splits = title_pattern.split(text)
        # split 结果: [前面文本, 分隔符1, 内容1, 分隔符2, 内容2, ...]
        # 需要重新组合
        chunks = []
        current = ""

        parts = title_pattern.split(text)
        # parts[0] 是第一段内容, 之后交替为分隔符和内容
        if len(parts) <= 1:
            return [text]

        for i, part in enumerate(parts):
            if i % 2 == 0:  # 内容
                current += part
            else:           # 分隔符（标题）
                if current.strip() and len(current.strip()) >= 10:
                    chunks.append(current.strip())
                current = part  # 新分块以标题开头

        if current.strip() and len(current.strip()) >= 10:
            chunks.append(current.strip())

        return chunks

    def _chunk_sliding_window(self, text: str,
                              max_chunk: int = 800,
                              overlap: int = 100) -> List[str]:
        """滑动窗口分块"""
        chunks = []
        start = 0
        while start < len(text):
            end = start + max_chunk
            chunk = text[start:end]
            # 在句子边界切分
            if end < len(text):
                for sep in ["。", "！", "？", "\n", ".", " "]:
                    last = chunk.rfind(sep)
                    if last > max_chunk // 2:
                        chunk = text[start:start + last + 1]
                        end = start + last + 1
                        break
            chunks.append(chunk)
            start = end - overlap
        return chunks

    # ── 统计 ────────────────────────────────

    def get_chunk_count(self, courseware_id: Optional[str] = None) -> int:
        """获取分块数量"""
        if courseware_id:
            row = self.db._conn.execute(
                "SELECT COUNT(*) AS cnt FROM knowledge_chunks "
                "WHERE courseware_id=?",
                (courseware_id,)
            ).fetchone()
        else:
            row = self.db._conn.execute(
                "SELECT COUNT(*) AS cnt FROM knowledge_chunks"
            ).fetchone()
        return row["cnt"] if row else 0
