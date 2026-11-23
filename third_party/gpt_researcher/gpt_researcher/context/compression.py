"""Context compression utilities for GPT Researcher.

This module provides classes for compressing and retrieving relevant
context from documents using embeddings and similarity filtering.

The compression pipeline:
1. Splits documents into chunks
2. Filters chunks by embedding similarity to the query
3. Returns the most relevant chunks as context

Classes:
    VectorstoreCompressor: Retrieves context from a vector store.
    ContextCompressor: Compresses raw documents using embedding similarity.
    WrittenContentCompressor: Compresses previously written content sections.

本地补丁（P3-8 / P3-10，非上游代码，升级后需重新应用）：
    1. 候选块 embedding 改为分批提交：每批 8 块，查询向量只算一次；
       批内失败按 4/2/1 降级，仍失败的块跳过，全部失败才回退词面筛选。
    2. 连续 2 次 embedding 超时后熔断 300 秒，冷却期内直接走词面筛选。
    3. 尽力给支持 client_kwargs 的 embedding 客户端注入传输层 timeout
       （当前覆盖 OllamaEmbeddings），失败时保留 wait_for + 熔断兜底。
    4. 修正 chunk_overlap 可能 >= chunk_size 导致分块丢字的问题。
"""

import asyncio
import logging
import math
import os
import time
from typing import Optional

from langchain_classic.retrievers import ContextualCompressionRetriever
from langchain_classic.retrievers.document_compressors import (
    DocumentCompressorPipeline,
    EmbeddingsFilter,
)
from langchain_text_splitters import RecursiveCharacterTextSplitter

from ..memory.embeddings import OPENAI_EMBEDDING_MODEL
from ..prompts import PromptFamily
from ..utils.costs import estimate_embedding_cost
from ..vector_store import VectorStoreWrapper
from .retriever import SectionRetriever

logger = logging.getLogger(__name__)

# 本地 embedding 模型（Ollama）的单条输入上限远小于 GPTR 默认的 1000 字分块，
# 分块过大 → 整批请求返回 413；分块过多 → 单次请求要跑数分钟。
# 这里按模型安全上限切块、限量，并给 embedding 调用加上超时与词面兜底。
EMBED_CHUNK_SIZE = int(os.environ.get("EMBED_CHUNK_SIZE", "600"))
EMBED_MAX_CHUNKS = int(os.environ.get("EMBED_MAX_CHUNKS", "30"))
EMBED_TIMEOUT = float(os.environ.get("EMBED_TIMEOUT", "90"))
# 分批与熔断参数（P3-8）：批越小单次请求越短，但请求数会上升。
EMBED_BATCH_SIZE = int(os.environ.get("EMBED_BATCH_SIZE", "8"))
EMBED_TIMEOUT_STREAK = 2
EMBED_COOLDOWN = float(os.environ.get("EMBED_COOLDOWN", "300"))

# 进程级熔断状态：连续超时达到阈值后进入冷却期，期间不再发起 embedding。
_timeout_streak = 0
_cooldown_until = 0.0


def _is_timeout(exc: BaseException) -> bool:
    """判断异常是否属于超时（wait_for 超时或 httpx 传输层超时）。"""
    return isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or "Timeout" in type(exc).__name__


def _embedding_cooling_down() -> bool:
    """熔断冷却期内返回 True；冷却结束后复位计数并重新允许 embedding。"""
    global _timeout_streak, _cooldown_until
    if not _cooldown_until:
        return False
    if time.monotonic() < _cooldown_until:
        return True
    _cooldown_until = 0.0
    _timeout_streak = 0
    logger.info("[Compression] embedding 熔断冷却结束，恢复 embedding")
    return False


def _note_timeout() -> None:
    """记录一次超时；连续达到阈值则进入冷却期。"""
    global _timeout_streak, _cooldown_until
    _timeout_streak += 1
    if _timeout_streak >= EMBED_TIMEOUT_STREAK:
        _cooldown_until = time.monotonic() + EMBED_COOLDOWN
        logger.warning(
            f"[Compression] embedding 连续 {_timeout_streak} 次超时，"
            f"冷却 {EMBED_COOLDOWN:.0f}s，期间直接走词面筛选"
        )


def _note_success() -> None:
    """一次成功的 embedding 调用会打断“连续超时”计数。"""
    global _timeout_streak
    _timeout_streak = 0


def _apply_transport_timeout(embeddings, timeout: float) -> bool:
    """尽力给 embedding 客户端注入传输层超时。

    OllamaEmbeddings 的 client_kwargs 只在构造时生效，而构造点在 vendor 之外，
    因此这里直接改已创建实例：先补 client_kwargs（便于后续重建），
    再给底层 httpx 客户端设置未被显式指定的 timeout。
    结构不认识或设置失败时保持 wait_for + 熔断兜底。
    """
    applied = False
    try:
        client_kwargs = getattr(embeddings, "client_kwargs", None)
        if isinstance(client_kwargs, dict):
            client_kwargs.setdefault("timeout", timeout)
        for attr in ("_client", "_async_client"):
            httpx_client = getattr(getattr(embeddings, attr, None), "_client", None)
            current = getattr(httpx_client, "timeout", None)
            # httpx 的“未设置”是 Timeout(connect=None, read=None, ...)，不是 None
            if httpx_client is not None and all(
                getattr(current, name, None) is None
                for name in ("connect", "read", "write", "pool")
            ):
                httpx_client.timeout = timeout
                applied = True
    except Exception as e:
        logger.debug(f"[Compression] 注入传输层超时失败，保留 wait_for 兜底：{type(e).__name__}: {e}")
    if applied:
        logger.info(f"[Compression] 已为 embedding 客户端设置传输层超时 {timeout:.0f}s")
    return applied


async def _embed_documents(embeddings, texts: list, timeout: float) -> list:
    """带超时地同步 embed_documents，并校验返回向量与输入一一对齐。"""
    vectors = await asyncio.wait_for(
        asyncio.to_thread(embeddings.embed_documents, texts),
        timeout=timeout,
    )
    if len(vectors) != len(texts):
        raise ValueError(f"embedding 返回数量不一致：{len(vectors)} != {len(texts)}")
    return vectors


async def _embed_batch_adaptive(embeddings, texts: list, timeout: float) -> list:
    """整批 embedding，失败则对半降级重试（8→4→2→1），仍失败的块用 None 占位。

    超时不降级重试：wait_for 取消后线程仍在跑，降级会叠加线程；直接跳过该批。
    """
    try:
        vectors = await _embed_documents(embeddings, texts, timeout)
        _note_success()
        return vectors
    except Exception as e:
        if _is_timeout(e):
            _note_timeout()
            logger.warning(
                f"[Compression] embedding 批次（{len(texts)} 块）超时，跳过该批：{type(e).__name__}"
            )
            return [None] * len(texts)
        if len(texts) == 1:
            logger.warning(f"[Compression] embedding 单块失败，跳过：{type(e).__name__}: {e}")
            return [None]

        if _embedding_cooling_down():
            # 已进入冷却期，不再递归发起 embedding
            return [None] * len(texts)

        mid = len(texts) // 2
        logger.warning(
            f"[Compression] embedding {len(texts)} 块批次失败，降级为 {mid}/{len(texts) - mid} 重试："
            f"{type(e).__name__}: {e}"
        )
        left = await _embed_batch_adaptive(embeddings, texts[:mid], timeout)
        right = await _embed_batch_adaptive(embeddings, texts[mid:], timeout)
        return left + right


async def _embed_in_batches(embeddings, texts: list, timeout: float) -> list:
    """按 EMBED_BATCH_SIZE 分批 embedding，冷却期内批次直接跳过。"""
    vectors = []
    for start in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = texts[start:start + EMBED_BATCH_SIZE]
        if _embedding_cooling_down():
            vectors.extend([None] * len(batch))
            continue
        vectors.extend(await _embed_batch_adaptive(embeddings, batch, timeout))
    return vectors


def _as_documents(pages: list) -> list:
    """把抓取结果（dict 或 Document）统一成 Document，过滤空正文。"""
    from langchain_core.documents import Document

    docs = []
    for page in pages:
        if isinstance(page, Document):
            if page.page_content:
                docs.append(page)
        elif isinstance(page, dict):
            content = page.get("raw_content") or page.get("content") or ""
            if content:
                docs.append(Document(
                    page_content=content,
                    metadata={"title": page.get("title", ""), "source": page.get("url", "")},
                ))
    return docs


def _lexical_score(query: str, text: str) -> float:
    """按中英文二元组覆盖率粗排文本与查询的相关性（不依赖 embedding）。"""
    grams = {query[i:i + 2] for i in range(len(query) - 1) if query[i:i + 2].strip()}
    if not grams:
        return 0.0
    return sum(1 for g in grams if g in text) / len(grams)


def _rank_by_lexical(query: str, docs: list) -> list:
    """按词面相关性降序排列文档块。"""
    return sorted(docs, key=lambda d: _lexical_score(query, d.page_content), reverse=True)


def _cosine(a: list, b: list) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class VectorstoreCompressor:
    """Retrieves and compresses context from a vector store.

    Uses similarity search on an existing vector store to find
    relevant documents for a given query.

    Attributes:
        vector_store: The vector store wrapper to search.
        max_results: Maximum number of results to return.
        filter: Optional filter for vector store queries.
    """

    def __init__(
        self,
        vector_store: VectorStoreWrapper,
        max_results: int = 7,
        filter: Optional[dict] = None,
        prompt_family: type[PromptFamily] | PromptFamily = PromptFamily,
        **kwargs,
    ):
        """Initialize the VectorstoreCompressor.

        Args:
            vector_store: The vector store to search.
            max_results: Maximum number of results to return.
            filter: Optional filter dictionary for queries.
            prompt_family: Prompt family for formatting output.
            **kwargs: Additional keyword arguments.
        """
        self.vector_store = vector_store
        self.max_results = max_results
        self.filter = filter
        self.kwargs = kwargs
        self.prompt_family = prompt_family

    async def async_get_context(self, query: str, max_results: int = 5) -> str:
        """Get relevant context from the vector store.

        Args:
            query: The search query.
            max_results: Maximum number of results to return.

        Returns:
            Formatted string of relevant document content.
        """
        results = await self.vector_store.asimilarity_search(query=query, k=max_results, filter=self.filter)
        return self.prompt_family.pretty_print_docs(results)


class ContextCompressor:
    """Compresses raw documents to extract relevant context.

    Uses embedding similarity to filter document chunks and return
    only the most relevant content for a given query.

    Attributes:
        documents: List of documents to compress.
        embeddings: Embedding model for similarity calculation.
        max_results: Maximum number of results to return.
        similarity_threshold: Minimum similarity score for inclusion.
    """

    def __init__(
        self,
        documents,
        embeddings,
        max_results: int = 5,
        prompt_family: type[PromptFamily] | PromptFamily = PromptFamily,
        **kwargs,
    ):
        """Initialize the ContextCompressor.

        Args:
            documents: List of documents to compress.
            embeddings: Embedding model instance.
            max_results: Maximum number of results to return.
            prompt_family: Prompt family for formatting output.
            **kwargs: Additional keyword arguments.
        """
        self.max_results = max_results
        self.documents = documents
        self.kwargs = kwargs
        self.embeddings = embeddings
        self.similarity_threshold = os.environ.get("SIMILARITY_THRESHOLD", 0.35)
        self.prompt_family = prompt_family
        # 传输层超时只能尽力注入（构造点不在本文件），失败由 wait_for + 熔断兜底
        _apply_transport_timeout(embeddings, float(os.environ.get("EMBED_TIMEOUT", EMBED_TIMEOUT)))

    async def async_get_context(self, query: str, max_results: Optional[int] = None, cost_callback=None) -> str:
        """Get relevant context from documents asynchronously.

        先按词面相关性裁剪候选块，再把候选块分批做 embedding 精排；
        单批失败按 4/2/1 降级，连续超时进入冷却期，最终回退词面排序，
        避免单个子查询整条失败。

        Args:
            query: The search query.
            max_results: Maximum number of results to return.
            cost_callback: Optional callback for tracking embedding costs.

        Returns:
            Formatted string of relevant document content.
        """
        if not self.documents:
            return ""

        # 未显式传入时使用构造时的 max_results，避免固定 5 覆盖配置
        effective_max = max(1, int(max_results if max_results is not None else self.max_results))

        documents = _as_documents(self.documents)
        if not documents:
            return ""

        # 运行时读取：启动自检会按实际模型能力写入这些变量
        chunk_size = max(1, int(os.environ.get("EMBED_CHUNK_SIZE", EMBED_CHUNK_SIZE)))
        max_chunks = max(0, int(os.environ.get("EMBED_MAX_CHUNKS", EMBED_MAX_CHUNKS)))
        timeout = float(os.environ.get("EMBED_TIMEOUT", EMBED_TIMEOUT))
        disabled = os.environ.get("GPTR_DISABLE_EMBEDDING") == "1"

        # chunk_overlap 必须严格小于 chunk_size，否则分块会丢字（P3-10）
        chunk_overlap = min(max(20, chunk_size // 8), chunk_size - 1)
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size, chunk_overlap=chunk_overlap
        )
        chunks = splitter.split_documents(documents)
        ranked = _rank_by_lexical(query, chunks)

        if disabled:
            logger.info(f"[Compression] embedding 已关闭，词面筛选 {min(len(ranked), effective_max)} 块")
            return self.prompt_family.pretty_print_docs(ranked[:effective_max], effective_max)

        if max_chunks == 0:
            logger.info("[Compression] EMBED_MAX_CHUNKS=0，跳过 embedding，直接词面筛选")
            return self.prompt_family.pretty_print_docs(ranked[:effective_max], effective_max)

        candidates = ranked[:max_chunks]

        if cost_callback:
            cost_callback(estimate_embedding_cost(model=OPENAI_EMBEDDING_MODEL, docs=candidates))

        selected = None
        if candidates:
            if _embedding_cooling_down():
                logger.warning("[Compression] embedding 熔断冷却中，直接走词面筛选")
            else:
                selected = await self._select_by_embedding(query, candidates, chunk_size, timeout, effective_max)

        if selected is None:
            selected = ranked[:effective_max]

        return self.prompt_family.pretty_print_docs(selected, effective_max)

    async def _select_by_embedding(
        self, query: str, candidates: list, chunk_size: int, timeout: float, max_results: int
    ) -> Optional[list]:
        """查询向量只算一次，候选块分批精排；返回 None 表示需要词面兜底。"""
        try:
            query_vec = (await _embed_documents(self.embeddings, [query[:chunk_size]], timeout))[0]
            _note_success()
        except Exception as e:
            if _is_timeout(e):
                _note_timeout()
            logger.warning(
                f"[Compression] 查询向量计算失败，回退词面筛选：{type(e).__name__}: {e}"
            )
            return None

        vectors = await _embed_in_batches(
            self.embeddings, [d.page_content for d in candidates], timeout
        )
        scored = [
            (doc, _cosine(query_vec, vec))
            for doc, vec in zip(candidates, vectors)
            if vec is not None
        ]
        if not scored:
            logger.warning("[Compression] 候选块 embedding 全部失败，回退词面筛选")
            return None

        threshold = float(self.similarity_threshold)
        scored.sort(key=lambda item: item[1], reverse=True)
        selected = [doc for doc, score in scored if score >= threshold][:max_results]
        return selected or [doc for doc, _ in scored[:max_results]]


class WrittenContentCompressor:
    """Compresses previously written content sections.

    Specialized compressor for finding relevant sections from
    previously written report content, preserving section titles
    and structure.

    Attributes:
        documents: List of written content sections.
        embeddings: Embedding model for similarity calculation.
        similarity_threshold: Minimum similarity score for inclusion.
    """

    def __init__(self, documents, embeddings, similarity_threshold: float, **kwargs):
        """Initialize the WrittenContentCompressor.

        Args:
            documents: List of written content sections.
            embeddings: Embedding model instance.
            similarity_threshold: Minimum similarity score for inclusion.
            **kwargs: Additional keyword arguments.
        """
        self.documents = documents
        self.kwargs = kwargs
        self.embeddings = embeddings
        self.similarity_threshold = similarity_threshold

    def __get_contextual_retriever(self):
        """Build the contextual compression retriever for sections.

        Returns:
            A ContextualCompressionRetriever configured for section retrieval.
        """
        splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=100)
        relevance_filter = EmbeddingsFilter(embeddings=self.embeddings,
                                            similarity_threshold=self.similarity_threshold)
        pipeline_compressor = DocumentCompressorPipeline(
            transformers=[splitter, relevance_filter]
        )
        base_retriever = SectionRetriever(
            sections=self.documents
        )
        contextual_retriever = ContextualCompressionRetriever(
            base_compressor=pipeline_compressor, base_retriever=base_retriever
        )
        return contextual_retriever

    def __pretty_docs_list(self, docs, top_n: int) -> list[str]:
        """Format documents as a list of title/content strings.

        Args:
            docs: List of documents to format.
            top_n: Maximum number of documents to include.

        Returns:
            List of formatted document strings.
        """
        return [f"Title: {d.metadata.get('section_title')}\nContent: {d.page_content}\n" for i, d in enumerate(docs) if i < top_n]

    async def async_get_context(self, query: str, max_results: int = 5, cost_callback=None) -> list[str]:
        """Get relevant written content sections asynchronously.

        Args:
            query: The search query.
            max_results: Maximum number of results to return.
            cost_callback: Optional callback for tracking embedding costs.

        Returns:
            List of formatted section strings.
        """
        compressed_docs = self.__get_contextual_retriever()
        if cost_callback:
            cost_callback(estimate_embedding_cost(model=OPENAI_EMBEDDING_MODEL, docs=self.documents))
        relevant_docs = await asyncio.to_thread(compressed_docs.invoke, query, **self.kwargs)
        return self.__pretty_docs_list(relevant_docs, max_results)
