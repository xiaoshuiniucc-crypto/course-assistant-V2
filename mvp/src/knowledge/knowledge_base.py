"""
M05 — 知识库管理
管理课件的向量化存储与检索，按 course_id 隔离。

MVP 策略：
- 使用 Chroma 作为向量数据库（轻量，pip install chromadb）
- Embedding 模型：默认 all-MiniLM-L6-v2（本地，无需 API Key）
- 如果 chromadb 不可用，回退到简单的关键词匹配
- 切片策略：按章节标题分块（优先），回退按 512 token 滑动窗口
"""

import re
import hashlib
from typing import Optional
from pathlib import Path

from contracts.models import ParsedFile, SearchResult


class KnowledgeBase:
    """
    知识库管理器。
    - add_courseware(): 添加课件
    - search(): 语义检索
    - list_courseware(): 列出已有课件
    - delete_courseware(): 删除课件
    """

    def __init__(self, persist_dir: Optional[str] = None):
        self._persist_dir = persist_dir
        self._chroma_client = None
        self._use_fallback = False
        self._fallback_store: dict[str, list[dict]] = {}  # {course_id: [chunks]}
        self._courseware_meta: dict[str, list[dict]] = {}  # {course_id: [metadata]}
        self._init_chroma()

    def _init_chroma(self):
        """初始化 Chroma 客户端。"""
        try:
            import chromadb
            if self._persist_dir:
                self._chroma_client = chromadb.PersistentClient(path=self._persist_dir)
            else:
                self._chroma_client = chromadb.Client()
            # 测试能否创建 collection
            self._chroma_client.get_or_create_collection("test")
            self._chroma_client.delete_collection("test")
        except ImportError:
            print("  ⚠️ chromadb 未安装，使用关键词匹配回退模式")
            self._use_fallback = True
        except Exception as e:
            print(f"  ⚠️ Chroma 初始化失败 ({e})，使用关键词匹配回退模式")
            self._use_fallback = True

    # ── 公开接口 ──────────────────────────────────────────

    def add_courseware(self, course_id: str, parsed_file: ParsedFile,
                       metadata: Optional[dict] = None) -> int:
        """
        增量添加课件到知识库。
        返回添加的文本块数量。
        """
        chunks = self._split_into_chunks(parsed_file, metadata or {})
        file_id = metadata.get("file_name", "unknown") if metadata else "unknown"

        if self._use_fallback:
            return self._add_fallback(course_id, chunks, file_id)
        else:
            return self._add_chroma(course_id, chunks, file_id)

    def search(self, course_id: str, query: str, top_k: int = 5) -> list[SearchResult]:
        """
        检索与 query 最相关的 top_k 个文本段落。
        """
        if self._use_fallback:
            return self._search_fallback(course_id, query, top_k)
        else:
            return self._search_chroma(course_id, query, top_k)

    def list_courseware(self, course_id: str) -> list[dict]:
        """列出已有课件元数据。"""
        return self._courseware_meta.get(course_id, [])

    def delete_courseware(self, course_id: str, file_id: str) -> bool:
        """删除指定课件。"""
        if self._use_fallback:
            if course_id in self._fallback_store:
                self._fallback_store[course_id] = [
                    c for c in self._fallback_store[course_id]
                    if c.get("file_id") != file_id
                ]
            self._courseware_meta[course_id] = [
                m for m in self._courseware_meta.get(course_id, [])
                if m.get("file_id") != file_id
            ]
            return True
        else:
            try:
                collection = self._chroma_client.get_or_create_collection(
                    name=f"course_{course_id}"
                )
                collection.delete(where={"file_id": file_id})
                self._courseware_meta[course_id] = [
                    m for m in self._courseware_meta.get(course_id, [])
                    if m.get("file_id") != file_id
                ]
                return True
            except Exception:
                return False

    # ── 切片策略 ──────────────────────────────────────────

    def _split_into_chunks(self, parsed_file: ParsedFile, metadata: dict) -> list[dict]:
        """
        将课件文本切片。
        策略：按章节标题分块（优先），回退滑动窗口。
        """
        text = parsed_file.text
        if not text.strip():
            return []

        # 如果文本较短（< 2000字），直接作为单个chunk
        if len(text.strip()) < 2000:
            return [{
                "text": text.strip(),
                "chapter": metadata.get("file_name", ""),
                "source": metadata.get("file_name", "unknown"),
                "page": None,
            }]

        # 尝试按章节标题分块
        chapter_pattern = r'(?:^|\n)((?:第\d+章|第[一二三四五六七八九十]+章|\d+\.\d+)\s*.+?)(?=\n(?:第\d+章|第[一二三四五六七八九十]+章|\d+\.\d+)|$)'
        sections = re.split(chapter_pattern, text, flags=re.MULTILINE)

        chunks = []
        # 如果正则分割出了多个章节
        if len(sections) > 1:
            current_title = ""
            for i, section in enumerate(sections):
                section = section.strip()
                if not section:
                    continue
                # 判断是否是标题（以章节号开头）
                if re.match(r'(?:第\d+章|第[一二三四五六七八九十]+章|\d+\.\d+)', section):
                    # 如果之前有内容，先保存
                    current_title = section
                    continue
                # 是内容块
                if len(section) > 10:  # 降低阈值
                    chunks.append({
                        "text": (current_title + "\n" + section) if current_title else section,
                        "chapter": current_title,
                        "source": metadata.get("file_name", "unknown"),
                        "page": None,
                    })

        # 如果章节分块没有产出结果，回退到滑动窗口
        if not chunks:
            chunks = self._sliding_window_split(text, metadata)

        return chunks

    def _sliding_window_split(self, text: str, metadata: dict,
                               chunk_size: int = 500, overlap: int = 100) -> list[dict]:
        """滑动窗口分块。"""
        chunks = []
        start = 0
        while start < len(text):
            end = start + chunk_size
            chunk_text = text[start:end].strip()
            if chunk_text:
                chunks.append({
                    "text": chunk_text,
                    "chapter": None,
                    "source": metadata.get("file_name", "unknown"),
                    "page": None,
                })
            start += chunk_size - overlap
        return chunks

    # ── Chroma 模式 ───────────────────────────────────────

    def _add_chroma(self, course_id: str, chunks: list[dict], file_id: str) -> int:
        """用 Chroma 添加向量。"""
        collection = self._chroma_client.get_or_create_collection(
            name=f"course_{course_id}"
        )

        ids = []
        documents = []
        metas = []
        for i, chunk in enumerate(chunks):
            chunk_id = hashlib.md5(f"{course_id}:{file_id}:{i}".encode()).hexdigest()
            ids.append(chunk_id)
            documents.append(chunk["text"])
            metas.append({
                "file_id": file_id,
                "source": chunk["source"],
                "chapter": chunk["chapter"] or "",
                "page": chunk["page"] or "",
            })

        if ids:
            collection.upsert(ids=ids, documents=documents, metadatas=metas)

        # 记录课件元数据
        if course_id not in self._courseware_meta:
            self._courseware_meta[course_id] = []
        self._courseware_meta[course_id].append({
            "file_id": file_id, "chunk_count": len(chunks),
        })

        return len(chunks)

    def _search_chroma(self, course_id: str, query: str, top_k: int) -> list[SearchResult]:
        """用 Chroma 检索。"""
        try:
            collection = self._chroma_client.get_or_create_collection(
                name=f"course_{course_id}"
            )
            results = collection.query(query_texts=[query], n_results=min(top_k, 10))
            search_results = []
            if results and results["documents"]:
                for i, doc in enumerate(results["documents"][0]):
                    meta = results["metadatas"][0][i] if results["metadatas"] else {}
                    dist = results["distances"][0][i] if results["distances"] else 0
                    search_results.append(SearchResult(
                        text=doc,
                        source=meta.get("source", ""),
                        page=int(meta["page"]) if meta.get("page") else None,
                        chapter=meta.get("chapter") or None,
                        score=1.0 - dist,  # 距离转相似度
                    ))
            return search_results
        except Exception as e:
            print(f"  ⚠️ Chroma 检索失败 ({e})，回退到关键词匹配")
            return self._search_fallback(course_id, query, top_k)

    # ── 回退模式（关键词匹配）──────────────────────────────

    def _add_fallback(self, course_id: str, chunks: list[dict], file_id: str) -> int:
        """关键词匹配回退：添加。"""
        if course_id not in self._fallback_store:
            self._fallback_store[course_id] = []
        for chunk in chunks:
            chunk["file_id"] = file_id
            self._fallback_store[course_id].append(chunk)

        if course_id not in self._courseware_meta:
            self._courseware_meta[course_id] = []
        self._courseware_meta[course_id].append({
            "file_id": file_id, "chunk_count": len(chunks),
        })

        return len(chunks)

    def _search_fallback(self, course_id: str, query: str, top_k: int) -> list[SearchResult]:
        """关键词匹配回退：检索。"""
        chunks = self._fallback_store.get(course_id, [])
        if not chunks:
            return []

        # 中文分词：按字符级 + 词级混合
        # 先尝试按2-gram分词，再按单字分词
        def tokenize(text):
            # 提取中文字符的2-gram和单字
            chars = re.findall(r'[\u4e00-\u9fff]', text)
            bigrams = set(chars[i] + chars[i+1] for i in range(len(chars)-1))
            singles = set(chars)
            # 加上英文词
            eng_words = set(re.findall(r'[a-zA-Z]+', text.lower()))
            return bigrams | singles | eng_words

        query_tokens = tokenize(query)
        if not query_tokens:
            return []

        scored = []
        for chunk in chunks:
            chunk_tokens = tokenize(chunk["text"])
            overlap = len(query_tokens & chunk_tokens)
            if overlap > 0:
                score = overlap / max(len(query_tokens), 1)
                scored.append((score, chunk))

        scored.sort(key=lambda x: x[0], reverse=True)
        results = []
        for score, chunk in scored[:top_k]:
            results.append(SearchResult(
                text=chunk["text"],
                source=chunk.get("source", ""),
                page=chunk.get("page"),
                chapter=chunk.get("chapter"),
                score=score,
            ))
        return results
