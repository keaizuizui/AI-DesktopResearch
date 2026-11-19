"""
Memory Module
混合记忆存储模块

提供：
- HybridMemoryStore: 混合检索（向量 + BM25 + SQLite）
- MemoryItem: 统一记忆项数据模型
- VectorStore: 向量存储基类及实现
- BM25Store: BM25关键词检索
- SQLiteArchive: SQLite持久化

使用示例：
    from shared.memory import HybridMemoryStore, MemoryItem

    store = HybridMemoryStore()
    store.add([MemoryItem(title="示例", content="内容")])
    results = store.search("查询词")
"""
from shared.memory.store import (
    MemoryItem,
    VectorStore,
    OllamaVectorStore,
    SimpleVectorStore,
    BM25Store,
    SQLiteArchive,
    HybridMemoryStore,
)

__all__ = [
    "MemoryItem",
    "VectorStore",
    "OllamaVectorStore",
    "SimpleVectorStore",
    "BM25Store",
    "SQLiteArchive",
    "HybridMemoryStore",
]
