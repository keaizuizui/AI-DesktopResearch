"""
shared/search/adapters — 搜索源适配器
========================================

统一Bocha/DDG/GPTR/Meili搜索适配器，消除重复实现。

适配器：
- BochaAdapter: 博查搜索（需要API Key）
- DDGAdapter: DuckDuckGo搜索（免费，无需Key）
- GPTRAdapter: GPT-Researcher微服务（深度研究）
- MeiliAdapter: Meilisearch本地知识库索引（MIT许可证）
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from typing import List

import requests

from shared.search.models import SearchResult, SourceGrade, grade_source

logger = logging.getLogger(__name__)


class BaseSearchAdapter(ABC):
    """搜索适配器基类"""

    name: str = "base"

    @abstractmethod
    def search(self, query: str, max_results: int = 10) -> List[SearchResult]:
        """执行搜索，返回统一 SearchResult 列表"""
        ...


# ═══════════════════════════════════════════════════════════════
# DeepSeek 官方搜索
# ═══════════════════════════════════════════════════════════════

class DeepSeekSearchAdapter(BaseSearchAdapter):
    """
    DeepSeek 官方网页搜索适配器

    Endpoint: POST https://api.deepseek.com/v1/tools/search
    鉴权与 DeepSeek LLM 相同；计费 5 元/千次成功请求。
    """

    name = "deepseek"

    def __init__(
        self,
        api_key: str = "",
        endpoint: str = "https://api.deepseek.com/v1/tools/search",
    ):
        self.api_key = api_key or ""
        self.endpoint = endpoint or "https://api.deepseek.com/v1/tools/search"
        self._last_request_time = 0.0
        self._min_interval = 0.05  # 官方 QPS 100，留出余量

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    @staticmethod
    def _authority_to_grade(authority):
        """DeepSeek authority(0-4) 映射到站内信源等级"""
        if authority is None or authority < 0:
            return None
        if authority >= 3:
            return SourceGrade.A
        if authority == 2:
            return SourceGrade.B
        return SourceGrade.C

    def search(self, query: str, max_results: int = 10) -> List[SearchResult]:
        if not self.api_key or not query or not query.strip():
            return []

        # DeepSeek 限制 query 1-128 字符、count 1-50
        query = query.strip()[:128]
        count = max(1, min(int(max_results or 10), 50))

        elapsed = time.time() - self._last_request_time
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_time = time.time()

        for attempt in range(3):
            try:
                headers = {
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                }
                payload = {"query": query, "count": count, "need_content": False}
                r = requests.post(self.endpoint, json=payload, headers=headers, timeout=30)
                if r.status_code == 429:
                    wait = 2 ** attempt
                    logger.warning(f"[DeepSeek] 429 频率限制，等待 {wait}s 后重试")
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                items = r.json().get("results", []) or []

                results = []
                for item in items:
                    url = item.get("url", "")
                    summary = item.get("summary") or item.get("snippet") or ""
                    grade = self._authority_to_grade(item.get("authority")) or grade_source(url)
                    results.append(
                        SearchResult(
                            title=item.get("title", ""),
                            url=url,
                            content=summary[:2000],
                            snippet=(item.get("snippet") or summary)[:500],
                            source="deepseek",
                            source_type="web",
                            grade=grade,
                            published_date=item.get("date") or "",
                            relevance_score=min(float(item.get("rerank_score", 0.0)) / 3.0, 1.0),
                        )
                    )
                return results
            except Exception as e:
                logger.warning(f"[DeepSeek] 搜索失败: {e}")
                return []
        return []

    async def search_async(self, query: str, max_results: int = 10) -> List[SearchResult]:
        """异步搜索（requests 为同步实现，放入线程池）"""
        import asyncio
        return await asyncio.to_thread(self.search, query, max_results)


# ═══════════════════════════════════════════════════════════════
# Bocha 博查搜索
# ═══════════════════════════════════════════════════════════════

class BochaAdapter(BaseSearchAdapter):
    """
    博查搜索适配器 — 同步+异步双模式

    添加原生aiohttp异步支持，而非线程池包装。
    """

    name = "bocha"

    def __init__(self, api_key: str = "", endpoint: str = "https://api.bochaai.com/v1/web-search"):
        self.api_key = api_key or ""
        self.endpoint = endpoint
        self._last_request_time = 0.0
        self._min_interval = 1.0  # 两次请求之间最小间隔（秒），避免429

    # ─── 同步 ───

    def search(self, query: str, max_results: int = 10) -> List[SearchResult]:
        if not self.api_key:
            return []
        # 频率限制：两次请求之间至少间隔 _min_interval 秒
        elapsed = time.time() - self._last_request_time
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_time = time.time()

        max_retries = 3
        for attempt in range(max_retries):
            try:
                headers = {"Authorization": f"Bearer {self.api_key}"}
                payload = {"query": query, "count": max_results}
                r = requests.post(self.endpoint, json=payload, headers=headers, timeout=30)
                if r.status_code == 429:
                    wait = 2 ** attempt  # 1s, 2s, 4s
                    logger.warning(f"[Bocha] 429 频率限制，等待 {wait}s 后重试 (第{attempt+1}次)")
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                data = r.json()

                items = data.get("data", {}).get("webPages", {}).get("value", [])
                if not items:
                    items = data.get("results", [])

                return [
                    SearchResult(
                        title=item.get("name", item.get("title", "")),
                        url=item.get("url", ""),
                        content=item.get("snippet", item.get("body", ""))[:2000],
                        source="bocha",
                        source_type="web",
                        grade=grade_source(item.get("url", "")),
                    )
                    for item in items
                ]
            except Exception as e:
                logger.warning(f"[Bocha] 搜索失败: {e}")
                return []

    # ─── 异步（原生aiohttp） ───

    async def search_async(self, query: str, max_results: int = 10) -> List[SearchResult]:
        """异步搜索（原生aiohttp实现，含429重试和频率限制）"""
        if not self.api_key:
            return []
        import asyncio
        # 频率限制
        elapsed = time.time() - self._last_request_time
        if elapsed < self._min_interval:
            await asyncio.sleep(self._min_interval - elapsed)
        self._last_request_time = time.time()

        max_retries = 3
        for attempt in range(max_retries):
            try:
                import aiohttp
                headers = {"Authorization": f"Bearer {self.api_key}"}
                payload = {"query": query, "count": max_results}
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        self.endpoint, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=30)
                    ) as resp:
                        if resp.status == 429:
                            wait = 2 ** attempt
                            logger.warning(f"[Bocha] 异步429 频率限制，等待 {wait}s 后重试 (第{attempt+1}次)")
                            await asyncio.sleep(wait)
                            continue
                        if resp.status != 200:
                            return []
                        data = await resp.json()

                items = data.get("data", {}).get("webPages", {}).get("value", [])
                if not items:
                    items = data.get("results", [])

                return [
                    SearchResult(
                        title=item.get("name", item.get("title", "")),
                        url=item.get("url", ""),
                        content=item.get("snippet", item.get("body", ""))[:2000],
                        source="bocha",
                        source_type="web",
                        grade=grade_source(item.get("url", "")),
                    )
                    for item in items
                ]
            except Exception as e:
                logger.warning(f"[Bocha] 异步搜索失败: {e}")
                return []


# ═══════════════════════════════════════════════════════════════
# DDG DuckDuckGo搜索（免费）
# ═══════════════════════════════════════════════════════════════

class DDGAdapter(BaseSearchAdapter):
    """
    DuckDuckGo搜索适配器（免费，无需API Key）

    迁移自：调研工具 search.py
    """

    name = "ddg"
    _ddgs_cls = None  # 类级别缓存，避免重复导入

    def __init__(self):
        self._ensure_ddgs()

    def _ensure_ddgs(self):
        """延迟导入DDG，避免硬依赖"""
        if DDGAdapter._ddgs_cls is not None:
            return
        try:
            from ddgs import DDGS
            DDGAdapter._ddgs_cls = DDGS
        except ImportError:
            try:
                from duckduckgo_search import DDGS
                DDGAdapter._ddgs_cls = DDGS
            except ImportError:
                logger.warning("[DDG] ddgs / duckduckgo_search 均未安装，DDG搜索不可用")
                DDGAdapter._ddgs_cls = None

    @property
    def available(self) -> bool:
        return DDGAdapter._ddgs_cls is not None

    def search(self, query: str, max_results: int = 10) -> List[SearchResult]:
        if not self.available:
            return []
        try:
            results = []
            with DDGAdapter._ddgs_cls() as ddgs:
                for r in ddgs.text(query, max_results=max_results):
                    results.append(
                        SearchResult(
                            title=r.get("title", ""),
                            url=r.get("href", ""),
                            content=r.get("body", "")[:2000],
                            source="ddg",
                            source_type="web",
                            grade=grade_source(r.get("href", "")),
                        )
                    )
            return results
        except Exception as e:
            logger.warning(f"[DDG] 搜索失败: {e}")
            return []

    async def search_async(self, query: str, max_results: int = 10) -> List[SearchResult]:
        """异步搜索（在线程池执行）"""
        import asyncio
        return await asyncio.to_thread(self.search, query, max_results)


# ═══════════════════════════════════════════════════════════════
# ZhihuAdapter 知乎搜索
# ═══════════════════════════════════════════════════════════════

class ZhihuAdapter(BaseSearchAdapter):
    """
    知乎搜索适配器 新增。

    定位：补搜中发现有价值的知乎网址后，调用知乎 API 进行知乎内搜索。
    知乎 API 仅返回摘要，不含原文（需 Playwright 抓取全文）。

    配置：
    - api_key: 知乎 API Key（配置项 search.zhihu.api_key）
    - endpoint: 知乎 API 端点（默认 https://api.zhihu.com）
    """

    name = "zhihu"

    def __init__(self, api_key: str = "", endpoint: str = "https://api.zhihu.com"):
        self.api_key = api_key
        self.endpoint = endpoint.rstrip("/")

    @property
    def available(self) -> bool:
        """检查知乎 API 是否可用"""
        return bool(self.api_key)

    def search(self, query: str, max_results: int = 5) -> List[SearchResult]:
        """
        搜索知乎内容。

        注意：知乎 API 仅返回摘要。如需全文，请使用 Playwright 抓取。
        """
        if not self.available:
            return []

        try:
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
            # 知乎搜索 API 端点
            payload = {
                "query": query,
                "limit": min(max_results, 20),
            }
            r = requests.post(
                f"{self.endpoint}/search",
                json=payload,
                headers=headers,
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()

            results = []
            items = data.get("data", data.get("results", []))
            for item in items:
                results.append(
                    SearchResult(
                        title=item.get("title", item.get("question", {}).get("title", "")),
                        url=item.get("url", f"https://www.zhihu.com/question/{item.get('id', '')}"),
                        content=item.get("excerpt", item.get("summary", ""))[:2000],
                        snippet=item.get("excerpt", item.get("summary", ""))[:500],
                        source="zhihu",
                        source_type="zhihu",
                        grade=SourceGrade.B,
                        data_type="unknown",
                    )
                )
            logger.info(f"[Zhihu] 搜索 '{query[:30]}' 返回 {len(results)} 条结果")
            return results

        except requests.exceptions.Timeout:
            logger.warning(f"[Zhihu] 搜索超时: {query[:30]}")
            return []
        except Exception as e:
            logger.warning(f"[Zhihu] 搜索失败: {e}")
            return []

    async def search_async(self, query: str, max_results: int = 5) -> List[SearchResult]:
        """异步搜索（在线程池执行）"""
        import asyncio
        return await asyncio.to_thread(self.search, query, max_results)

    def search_by_url(self, zhihu_url: str, max_results: int = 3) -> List[SearchResult]:
        """
        根据知乎 URL 进行知乎内搜索（补搜场景）。

        当补搜中发现有价值的知乎链接时，提取关键信息后
        调用知乎 API 搜索相关内容。

        Args:
            zhihu_url: 知乎链接
            max_results: 最大返回数

        Returns:
            SearchResult 列表
        """
        # 从 URL 提取关键信息作为搜索词
        import re
        query = zhihu_url
        # 尝试提取标题关键词
        match = re.search(r'/question/(\d+)', zhihu_url)
        if match:
            query = f"zhihu question {match.group(1)}"
        return self.search(query, max_results)


# ═══════════════════════════════════════════════════════════════
# XNGAdapter SearXNG 元搜索引擎
# ═══════════════════════════════════════════════════════════════

class XNGAdapter(BaseSearchAdapter):
    """
    SearXNG 元搜索引擎适配器

    定位：自建 SearXNG 实例，聚合 70+ 搜索引擎（含百度/Google/Brave），
    零费用、不限速。作为博查 API 的低成本替代方案。

    部署：docker run -d -p 8080:8080 searxng/searxng
    配置：settings.yml 中启用 JSON API
    """

    name = "xng"

    def __init__(self, endpoint: str = ""):
        # 优先级：构造参数 > 环境变量 SEARX_URL
        if endpoint:
            self.endpoint = endpoint.rstrip("/")
        else:
            self.endpoint = (os.environ.get("SEARX_URL") or "http://localhost:8080").rstrip("/")

    @property
    def available(self) -> bool:
        """检查 XNG 服务是否可用"""
        try:
            r = requests.get(f"{self.endpoint}/healthz", timeout=3)
            return r.status_code == 200
        except Exception:
            return False

    def search(self, query: str, max_results: int = 10) -> List[SearchResult]:
        """
        调用 SearXNG JSON API。

        SearXNG 原生支持 ?format=json 参数，
        返回 Google/Bing/百度等引擎的聚合结果。
        """
        if not self.available:
            return []

        try:
            params = {
                "q": query,
                "format": "json",
                "categories": "general",
                "pageno": 1,
            }
            r = requests.get(
                f"{self.endpoint}/search",
                params=params,
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()

            results = []
            for item in data.get("results", [])[:max_results]:
                results.append(
                    SearchResult(
                        title=item.get("title", ""),
                        url=item.get("url", ""),
                        content=item.get("content", "")[:2000],
                        snippet=item.get("content", "")[:500],
                        source="xng",
                        source_type="web",
                        grade=grade_source(item.get("url", "")),
                        data_type="unknown",
                    )
                )
            logger.info(f"[XNG] 搜索 '{query[:30]}' 返回 {len(results)} 条结果")
            return results

        except Exception as e:
            logger.warning(f"[XNG] 搜索失败: {e}")
            return []

    async def search_async(self, query: str, max_results: int = 10) -> List[SearchResult]:
        """异步搜索（在线程池执行）"""
        import asyncio
        return await asyncio.to_thread(self.search, query, max_results)


# ═══════════════════════════════════════════════════════════════
# MeiliAdapter Meilisearch 本地知识库索引
# ═══════════════════════════════════════════════════════════════

class MeiliAdapter(BaseSearchAdapter):
    """
    Meilisearch 本地知识库适配器

    定位：将调研/方案/洞察工具产出的结构化知识建立可检索索引，
    支持跨项目知识复用、混合搜索（关键词+语义向量）、分面过滤。

    与 Bocha/DDG 的区别：
    - Bocha/DDG 搜索互联网公开网页
    - MeiliAdapter 搜索本地已积累的知识库

    部署：docker run -p 7700:7700 getmeili/meilisearch:latest
    许可证：MIT（社区版）
    """

    name = "meili"

    def __init__(
        self,
        endpoint: str = "http://localhost:7700",
        api_key: str = "",
        index_name: str = "research_knowledge",
    ):
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.index_name = index_name
        self._client = None

    @classmethod
    def research_sources(cls, endpoint: str = "http://localhost:7700", api_key: str = "") -> "MeiliAdapter":
        """双shard: Shard A — 调研原始信源池
        - 来源: GPTR / Bocha / DDG / SearXNG 搜索抓取
        - 用途: 原始信源检索、验证、溯源
        """
        return cls(endpoint=endpoint, api_key=api_key, index_name="research_sources")

    @classmethod
    def tool_knowledge(cls, endpoint: str = "http://localhost:7700", api_key: str = "") -> "MeiliAdapter":
        """双shard: Shard B — 工具知识库
        - 来源: 洞察工具 / 品牌大师 / 方案工具 产出的结构化切片
        - 用途: 策略节点按需查询、跨工具知识复用
        """
        return cls(endpoint=endpoint, api_key=api_key, index_name="tool_knowledge")

    def _get_client(self):
        """延迟导入 + 客户端单例"""
        if self._client is not None:
            return self._client
        try:
            import meilisearch
            key = self.api_key if self.api_key else None
            self._client = meilisearch.Client(self.endpoint, key)
            return self._client
        except ImportError:
            logger.warning("[Meili] meilisearch 包未安装，pip install meilisearch")
            return None

    @property
    def available(self) -> bool:
        """检查 Meilisearch 服务是否可用"""
        client = self._get_client()
        if client is None:
            return False
        try:
            client.health()
            return True
        except Exception:
            return False

    def ensure_index(self, filterable_attrs: list = None, sortable_attrs: list = None):
        """
        确保索引存在并配置好过滤/排序属性

        Args:
            filterable_attrs: 可过滤属性列表
            sortable_attrs: 可排序属性列表
        """
        client = self._get_client()
        if client is None:
            return

        filterable_attrs = filterable_attrs or [
            "source", "source_type", "grade", "tier",
            "published_date", "searched_at",
        ]
        sortable_attrs = sortable_attrs or ["searched_at", "llm_score", "score"]

        try:
            index = client.index(self.index_name)
            index.update_filterable_attributes(filterable_attrs)
            index.update_sortable_attributes(sortable_attrs)
            logger.debug(f"[Meili] 索引 {self.index_name} 配置已更新")
        except Exception as e:
            logger.warning(f"[Meili] 索引配置失败: {e}")

    def index_documents(self, results: List[SearchResult], batch_size: int = 200):
        """
        将 SearchResult 列表写入 Meilisearch 索引

        Args:
            results: SearchResult 列表
            batch_size: 每批写入数量
        """
        client = self._get_client()
        if client is None:
            return

        if not results:
            return

        # 转换为 Meilisearch 文档格式（必须有唯一 id）
        docs = []
        for i, r in enumerate(results):
            doc = r.to_dict() if hasattr(r, "to_dict") else dict(r)
            # 生成唯一 ID（URL + title 的哈希，无 URL 时用序号）
            if doc.get("url"):
                doc_id = str(abs(hash(doc["url"] + doc.get("title", ""))))[:32]
            else:
                doc_id = f"local_{i}_{int(time.time())}"
            doc["id"] = doc_id
            docs.append(doc)

        self._write_batch(client, docs, batch_size)

    def index_documents_any(self, docs: List[Dict], batch_size: int = 200):
        """将任意 dict 文档写入 Meilisearch 索引

        与 index_documents 的区别：不要求 SearchResult 类型，
        适用于工具知识切片等非搜索素材的文档。

        Args:
            docs: 文档列表，每条必须包含 "id" 字段
            batch_size: 每批写入数量
        """
        client = self._get_client()
        if client is None:
            return
        if not docs:
            return

        # 确保每条都有 id
        import hashlib
        for i, doc in enumerate(docs):
            if "id" not in doc:
                key = f"{doc.get('tool_source','')}_{doc.get('category','')}_{doc.get('field','')}_{doc.get('source_url','')}"
                doc["id"] = hashlib.md5(key.encode()).hexdigest()[:32]

        self._write_batch(client, docs, batch_size)

    def _write_batch(self, client, docs: List[Dict], batch_size: int = 200):
        """内部分批写入"""
        for start in range(0, len(docs), batch_size):
            batch = docs[start:start + batch_size]
            try:
                index = client.index(self.index_name)
                task = index.add_documents(batch)
                logger.info(f"[Meili] 写入 {len(batch)} 条文档到 {self.index_name}，task_uid={task.task_uid}")
            except Exception as e:
                logger.warning(f"[Meili] 文档写入 {self.index_name} 失败: {e}")

    def search(self, query: str, max_results: int = 10) -> List[SearchResult]:
        """从本地知识库搜索"""
        client = self._get_client()
        if client is None:
            return []
        try:
            index = client.index(self.index_name)
            resp = index.search(
                query,
                {
                    "limit": max_results,
                    "attributesToHighlight": ["title", "content"],
                    "attributesToRetrieve": [
                        "title", "url", "content", "snippet", "source",
                        "source_type", "grade", "tier", "published_date",
                        "searched_at", "relevance_score", "data_density",
                        "llm_score", "score", "facts", "reliability",
                        "score_reason", "fact_card",
                    ],
                },
            )

            hits = resp.get("hits", [])
            results = []
            for hit in hits:
                # 移除 Meilisearch 内部字段
                hit.pop("_formatted", None)
                hit.pop("id", None)
                hit.pop("_rankingScore", None)
                results.append(SearchResult.from_dict(hit))

            logger.debug(f"[Meili] 搜索 '{query[:30]}' 返回 {len(results)} 条结果")
            return results
        except Exception as e:
            logger.warning(f"[Meili] 搜索失败: {e}")
            return []

    async def search_async(self, query: str, max_results: int = 10) -> List[SearchResult]:
        """异步搜索（在线程池执行）"""
        import asyncio
        return await asyncio.to_thread(self.search, query, max_results)

    def get_stats(self) -> dict:
        """获取索引统计信息"""
        client = self._get_client()
        if client is None:
            return {"available": False}
        try:
            index = client.index(self.index_name)
            stats = index.get_stats()
            return {
                "available": True,
                "index_name": self.index_name,
                "documents_count": stats.get("numberOfDocuments", 0),
                "is_indexing": stats.get("isIndexing", False),
            }
        except Exception as e:
            return {"available": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════════
# GPTR GPT-Researcher微服务
# ═══════════════════════════════════════════════════════════════

class GPTRAdapter(BaseSearchAdapter):
    """
    GPT-Researcher微服务适配器

    通过HTTP调用 gptr_service 微服务执行深度研究。
    """

    name = "gptr"

    def __init__(self, base_url: str = "http://localhost:8000", timeout: int = 600):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def search(self, query: str, max_results: int = 10) -> List[SearchResult]:
        """提交深度研究任务（同步等待结果）"""
        try:
            r = requests.post(
                f"{self.base_url}/api/v1/research",
                json={"query": query[:400], "report_type": "research_report"},
                timeout=self.timeout,
            )
            r.raise_for_status()
            data = r.json()
            # GPTR返回的是研究报告文本，包装为单条SearchResult
            return [
                SearchResult(
                    title=f"深度研究: {query[:50]}",
                    url="",
                    content=data.get("report", data.get("result", ""))[:5000],
                    source="gptr",
                    source_type="research",
                    grade=SourceGrade.A,
                    relevance_score=0.9,
                )
            ]
        except Exception as e:
            logger.warning(f"[GPTR] 深度研究失败: {e}")
            return []

    async def submit_async(self, query: str) -> str:
        """异步提交任务，返回task_id"""
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/api/v1/research",
                json={"query": query[:400], "report_type": "research_report"},
            ) as r:
                data = await r.json()
                return data.get("task_id", "")

    async def query_status(self, task_id: str) -> dict:
        """查询任务状态"""
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{self.base_url}/api/v1/research/{task_id}") as r:
                return await r.json()

    async def search_async(self, query: str, max_results: int = 10) -> List[SearchResult]:
        """异步搜索（在线程池执行）"""
        import asyncio
        return await asyncio.to_thread(self.search, query, max_results)
