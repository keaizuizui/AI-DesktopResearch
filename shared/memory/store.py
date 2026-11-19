"""
Hybrid Memory Store Module
混合记忆存储模块 - 向量 + BM25 + SQLite 持久化

来源：调研工具 memory.py

设计原则：
- 通用接口，不依赖特定工具的类
- 可选依赖：Ollama向量、BM25、SQLite
- 支持事实卡片压缩
- 线程安全
"""
import os
import sqlite3
import json
import re
import threading
import logging
from typing import List, Dict, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class MemoryItem:
    """记忆项"""
    id: Optional[int] = None
    query: str = ""
    title: str = ""
    url: str = ""
    content: str = ""
    source: str = ""
    score: float = 0.0
    reliability: str = ""
    facts: List[str] = field(default_factory=list)
    confidence: float = 0.0
    fact_card: str = ""
    tags: List[str] = field(default_factory=list)
    created_at: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "query": self.query,
            "title": self.title,
            "url": self.url,
            "content": self.content,
            "source": self.source,
            "score": self.score,
            "reliability": self.reliability,
            "facts": self.facts,
            "confidence": self.confidence,
            "fact_card": self.fact_card,
            "tags": self.tags,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MemoryItem":
        return cls(
            id=data.get("id"),
            query=data.get("query", ""),
            title=data.get("title", ""),
            url=data.get("url", ""),
            content=data.get("content", ""),
            source=data.get("source", ""),
            score=data.get("score", 0.0),
            reliability=data.get("reliability", ""),
            facts=data.get("facts", []),
            confidence=data.get("confidence", 0.0),
            fact_card=data.get("fact_card", ""),
            tags=data.get("tags", []),
            created_at=data.get("created_at", ""),
        )


# ============ 向量存储（可选 Ollama） ============

class VectorStore:
    """向量存储基类"""

    def add(self, texts: List[str], metas: List[Dict]):
        raise NotImplementedError

    def search(self, query: str, top_k: int = 10) -> List[Dict]:
        raise NotImplementedError

    def clear(self):
        raise NotImplementedError


class OllamaVectorStore(VectorStore):
    """Ollama 向量存储"""

    def __init__(self, host: str, model: str, embedding_dim: int = 768):
        self.host = host.rstrip("/")
        self.model = model
        self.embedding_dim = embedding_dim
        self.texts: List[str] = []
        self.metas: List[Dict] = []
        self.vectors: Optional[Any] = None
        self._lock = threading.Lock()

    def embed(self, texts: List[str]) -> Any:
        import requests
        import numpy as np
        if not texts:
            return np.array([], dtype=np.float32)
        truncated = [t[:800] for t in texts]
        # 优先使用 /api/embed 批量端点（Ollama >=0.1.14），失败回退逐条
        try:
            r = requests.post(
                f"{self.host}/api/embed",
                json={"model": self.model, "input": truncated},
                timeout=120,
            )
            r.raise_for_status()
            embeddings = r.json().get("embeddings", [])
            vectors = []
            for i, emb in enumerate(embeddings):
                vectors.append(emb if emb else [0.0] * self.embedding_dim)
            return np.array(vectors, dtype=np.float32)
        except Exception:
            pass
        # 回退：逐条调用 /api/embeddings
        vectors = []
        for t in truncated:
            try:
                r = requests.post(
                    f"{self.host}/api/embeddings",
                    json={"model": self.model, "prompt": t},
                    timeout=60,
                )
                r.raise_for_status()
                vec = r.json().get("embedding")
                vectors.append(vec if vec else [0.0] * self.embedding_dim)
            except Exception:
                vectors.append([0.0] * self.embedding_dim)
        return np.array(vectors, dtype=np.float32)

    def add(self, texts: List[str], metas: List[Dict]):
        if not texts:
            return
        import numpy as np
        processed_texts = []
        processed_metas = []
        for t, m in zip(texts, metas):
            if len(t) > 1200:
                chunks = self._split_by_boundary(t, max_chunk=800, max_chunks=3)
                for i, chunk in enumerate(chunks):
                    processed_texts.append(chunk)
                    cm = m.copy()
                    cm["_chunk_idx"] = i
                    processed_metas.append(cm)
            else:
                processed_texts.append(t)
                processed_metas.append(m)

        vecs = self.embed(processed_texts)
        with self._lock:
            if self.vectors is None:
                self.vectors = vecs
                self.texts = processed_texts
                self.metas = processed_metas
            else:
                import numpy as np
                self.vectors = np.vstack([self.vectors, vecs])
                self.texts.extend(processed_texts)
                self.metas.extend(processed_metas)

    def search(self, query: str, top_k: int = 10) -> List[Dict]:
        if self.vectors is None or len(self.texts) == 0:
            return []
        import numpy as np
        q_vec = self.embed([query])[0]
        norms = np.linalg.norm(self.vectors, axis=1) * np.linalg.norm(q_vec)
        norms[norms == 0] = 1e-10
        sims = np.dot(self.vectors, q_vec) / norms
        top_idx = np.argsort(sims)[::-1][:top_k]
        return [
            {"score": float(sims[i]), "text": self.texts[i], "meta": self.metas[i]}
            for i in top_idx
        ]

    def clear(self):
        with self._lock:
            self.texts.clear()
            self.metas.clear()
            self.vectors = None

    @staticmethod
    def _split_by_boundary(text: str, max_chunk: int = 800, max_chunks: int = 3) -> list:
        """按语义边界分块（段落 > 句子 > 硬截断）

        优先级：段落边界(\n\n) > 句子边界(。！？.!) > 硬截断
        """
        if len(text) <= max_chunk:
            return [text]

        # 第一轮：按段落边界切分
        paragraphs = text.split("\n\n")
        chunks = []
        current = ""
        for para in paragraphs:
            if len(current) + len(para) + 2 <= max_chunk:
                current += para + "\n\n"
            else:
                if current:
                    chunks.append(current.strip())
                # 单段超长，按句子切分
                if len(para) > max_chunk:
                    sentences = _split_by_sentence(para, max_chunk)
                    chunks.extend(sentences[:-1]) if len(sentences) > 1 else None
                    current = sentences[-1] if sentences else ""
                else:
                    current = para + "\n\n"
        if current:
            chunks.append(current.strip())

        return chunks[:max_chunks]


def _split_by_sentence(text: str, max_len: int) -> list:
    """按句子边界切分，保证不在句子中间截断"""
    import re
    # 按中英文句号、感叹号、问号分割，保留分隔符
    parts = re.split(r'(?<=[。！？.!?])', text)
    chunks = []
    current = ""
    for part in parts:
        if len(current) + len(part) <= max_len:
            current += part
        else:
            if current:
                chunks.append(current)
            # 单句超长，硬截断
            if len(part) > max_len:
                chunks.append(part[:max_len])
                current = ""
            else:
                current = part
    if current:
        chunks.append(current)
    return chunks if chunks else [text[:max_len]]


class SimpleVectorStore(VectorStore):
    """简单向量存储（无外部依赖，使用哈希模拟）"""

    def __init__(self):
        self.texts: List[str] = []
        self.metas: List[Dict] = []
        self._lock = threading.Lock()

    def _hash_vector(self, text: str) -> List[float]:
        """用哈希生成伪向量"""
        import hashlib
        h = hashlib.sha256(text.encode()).hexdigest()
        return [int(h[i:i+2], 16) / 255.0 for i in range(0, 32, 2)]

    def add(self, texts: List[str], metas: List[Dict]):
        with self._lock:
            self.texts.extend(texts)
            self.metas.extend(metas)

    def search(self, query: str, top_k: int = 10) -> List[Dict]:
        """基于关键词匹配的降级搜索"""
        query_words = set(re.findall(r'[\u4e00-\u9fa5]|[a-zA-Z]+|\d+', query.lower()))
        scores = []
        for i, text in enumerate(self.texts):
            text_words = set(re.findall(r'[\u4e00-\u9fa5]|[a-zA-Z]+|\d+', text.lower()))
            overlap = len(query_words & text_words)
            score = overlap / max(len(query_words), 1)
            scores.append((score, i))

        scores.sort(reverse=True)
        return [
            {"score": s, "text": self.texts[i], "meta": self.metas[i]}
            for s, i in scores[:top_k]
        ]

    def clear(self):
        with self._lock:
            self.texts.clear()
            self.metas.clear()


# ============ BM25 存储（可选） ============

class BM25Store:
    """BM25 关键词检索存储"""

    def __init__(self):
        self.texts: List[str] = []
        self.metas: List[Dict] = []
        self.bm25 = None
        self.tokenized: List[List[str]] = []
        self._available = self._check_bm25()

    def _check_bm25(self) -> bool:
        try:
            from rank_bm25 import BM25Okapi
            return True
        except ImportError:
            logger.warning("[BM25] rank_bm25 未安装，混合检索降级。pip install rank-bm25")
            return False

    def _tokenize(self, text: str) -> List[str]:
        return re.findall(r'[\u4e00-\u9fa5]|[a-zA-Z]+|\d+', text.lower())

    def add(self, texts: List[str], metas: List[Dict]):
        if not texts:
            return
        self.texts.extend(texts)
        self.metas.extend(metas)
        self.tokenized = [self._tokenize(t) for t in self.texts]
        if self._available and self.tokenized:
            from rank_bm25 import BM25Okapi
            self.bm25 = BM25Okapi(self.tokenized)

    def search(self, query: str, top_k: int = 10) -> List[Dict]:
        if not self._available or not self.bm25:
            return []
        q_tokens = self._tokenize(query)
        scores = self.bm25.get_scores(q_tokens)
        import numpy as np
        top_idx = np.argsort(scores)[::-1][:top_k]
        return [
            {"score": float(scores[i]), "text": self.texts[i], "meta": self.metas[i]}
            for i in top_idx
        ]

    def clear(self):
        self.texts.clear()
        self.metas.clear()
        self.tokenized.clear()
        self.bm25 = None


# ============ SQLite 持久化 ============

class SQLiteArchive:
    """SQLite 持久化存储"""

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            db_path = os.path.join(os.path.expanduser("~"), ".xiaozui", "memory.db")
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS materials (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query TEXT,
                    title TEXT,
                    url TEXT,
                    content TEXT,
                    source TEXT,
                    score REAL,
                    reliability TEXT,
                    facts TEXT,
                    confidence REAL DEFAULT 0.0,
                    fact_card TEXT,
                    tags TEXT,
                    created_at TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS queries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query TEXT UNIQUE,
                    norm_query TEXT,
                    hit_count INTEGER DEFAULT 0,
                    last_searched TEXT
                )
            """)
            conn.commit()

    def save(self, item: MemoryItem):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT OR IGNORE INTO materials
                (query, title, url, content, source, score, reliability, facts, confidence, fact_card, tags, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (item.query, item.title, item.url, item.content, item.source,
                 item.score, item.reliability, json.dumps(item.facts, ensure_ascii=False),
                 item.confidence, item.fact_card, json.dumps(item.tags, ensure_ascii=False),
                 item.created_at or datetime.now().isoformat())
            )
            conn.commit()

    def load(self, query: str, limit: int = 10) -> List[MemoryItem]:
        norm = re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9]', '', query.lower())
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM materials WHERE query LIKE ? OR ? LIKE query ORDER BY confidence DESC, score DESC LIMIT ?",
                (f"%{query}%", f"%{norm}%", limit)
            ).fetchall()

        results = []
        for row in rows:
            results.append(MemoryItem(
                id=row["id"],
                query=row["query"],
                title=row["title"],
                url=row["url"],
                content=row["content"],
                source=row["source"],
                score=row["score"] or 0.0,
                reliability=row["reliability"],
                facts=json.loads(row["facts"]) if row["facts"] else [],
                confidence=row["confidence"] or 0.0,
                fact_card=row["fact_card"] or "",
                tags=json.loads(row["tags"]) if row["tags"] else [],
                created_at=row["created_at"],
            ))
        return results

    def record_query(self, query: str):
        norm = re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9]', '', query.lower())
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO queries (query, norm_query, hit_count, last_searched)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(query) DO UPDATE SET
                hit_count = hit_count + 1,
                last_searched = excluded.last_searched""",
                (query, norm, datetime.now().isoformat())
            )
            conn.commit()

    def is_frequent(self, query: str, threshold: int = 3) -> bool:
        norm = re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9]', '', query.lower())
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT hit_count FROM queries WHERE norm_query = ?", (norm,)
            ).fetchone()
        return row is not None and row[0] >= threshold


# ============ 混合记忆存储 ============

class HybridMemoryStore:
    """
    混合记忆存储

    整合：向量检索 + BM25关键词检索 + SQLite持久化
    """

    def __init__(
        self,
        vector_store: Optional[VectorStore] = None,
        bm25_store: Optional[BM25Store] = None,
        archive: Optional[SQLiteArchive] = None,
        enable_compression: bool = True,
    ):
        self.vector_store = vector_store or SimpleVectorStore()
        self.bm25_store = bm25_store or BM25Store()
        self.archive = archive or SQLiteArchive()
        self.enable_compression = enable_compression

    def add(self, items: List[MemoryItem]):
        """添加记忆项"""
        texts = []
        metas = []
        for item in items:
            text = item.fact_card or item.content[:200]
            texts.append(text)
            metas.append(item.to_dict())
            self.archive.save(item)

        self.vector_store.add(texts, metas)
        self.bm25_store.add(texts, metas)

    def search(self, query: str, top_k: int = 10) -> List[Dict]:
        """混合检索"""
        # 向量检索
        vec_results = self.vector_store.search(query, top_k=top_k)
        # BM25检索
        bm25_results = self.bm25_store.search(query, top_k=top_k)
        # 合并去重
        seen = set()
        merged = []
        for r in vec_results + bm25_results:
            key = r.get("text", "")[:50]
            if key not in seen:
                seen.add(key)
                merged.append(r)
        return merged[:top_k]

    def load_from_archive(self, query: str, limit: int = 10) -> List[MemoryItem]:
        """从持久化加载"""
        return self.archive.load(query, limit=limit)

    def record_query(self, query: str):
        """记录查询"""
        self.archive.record_query(query)

    def is_frequent_topic(self, query: str, threshold: int = 3) -> bool:
        """判断是否为高频查询"""
        return self.archive.is_frequent(query, threshold)

    def clear(self):
        """清空所有存储"""
        self.vector_store.clear()
        self.bm25_store.clear()
