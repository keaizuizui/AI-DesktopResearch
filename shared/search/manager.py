"""
shared/search/manager — SearchManager 统一入口
=================================================

统一管理所有搜索适配器，提供一站式搜索接口。

使用方式：
    from shared.search import SearchManager, SearchConfig

    config = SearchConfig(deepseek_api_key="xxx")
    mgr = SearchManager(config)

    # 同步搜索（DeepSeek）
    results = mgr.search("新能源汽车市场规模")

    # 深度搜索（GPTR）
    results = mgr.search("行业趋势", sources=["gptr"])

    # 带缓存和信源过滤
    results = mgr.search("行业趋势", use_cache=True, min_grade="B")

    # 异步搜索
    results = await mgr.search_async("行业趋势")
"""

from __future__ import annotations

import logging
import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from shared.search.models import SearchResult, SourceGrade, filter_by_grade
from shared.search.adapters import (
    BaseSearchAdapter, DeepSeekSearchAdapter, GPTRAdapter, MeiliAdapter,
)
from shared.search.cache import SearchCache

logger = logging.getLogger(__name__)


@dataclass
class SearchConfig:
    """搜索配置"""
    # DeepSeek 官方搜索（默认搜索源，复用 DeepSeek API Key）
    deepseek_api_key: str = field(default_factory=lambda: os.environ.get("DEEPSEEK_API_KEY", ""))
    deepseek_endpoint: str = "https://api.deepseek.com/v1/tools/search"

    # GPT-Researcher 微服务
    gptr_base_url: str = "http://localhost:8000"
    gptr_timeout: int = 600

    # Meilisearch 本地搜索索引
    meili_enabled: bool = False
    meili_endpoint: str = "http://localhost:7700"
    meili_api_key: str = ""
    meili_index_name: str = "research_knowledge"

    # 缓存配置
    enable_cache: bool = True
    cache_ttl_days: int = 7
    cache_max_items: int = 1000
    cache_dir: str = ""

    # 默认搜索源（DeepSeek 官方搜索）
    default_sources: List[str] = field(default_factory=lambda: ["deepseek"])

    # 信源过滤
    default_min_grade: SourceGrade = SourceGrade.C

    # 搜索预算
    max_results_per_source: int = 10

    # 线程池大小（用于并行搜索）
    max_workers: int = 5


class SearchManager:
    """
    统一搜索管理器

    职责：
    - 管理所有搜索适配器的注册和生命周期
    - 提供统一的搜索入口（同步+异步）
    - 集成搜索缓存（20天TTL）
    - 集成信源分级过滤
    - 支持多源并行搜索
    """

    def __init__(self, config: SearchConfig):
        self.config = config
        self._adapters: Dict[str, BaseSearchAdapter] = {}
        self._cache: Optional[SearchCache] = None
        self._executor = ThreadPoolExecutor(max_workers=config.max_workers)

        # 注册适配器
        self._register_adapters()

        # 初始化缓存
        if config.enable_cache:
            self._cache = SearchCache(
                enabled=True,
                ttl_days=config.cache_ttl_days,
                max_items=config.cache_max_items,
                cache_dir=config.cache_dir or None,
            )

        logger.info(
            f"[SearchManager] 初始化完成，可用搜索源: {list(self._adapters.keys())}"
        )

    def _register_adapters(self):
        """根据配置注册搜索适配器"""
        cfg = self.config

        # DeepSeek（官方搜索，默认）
        if cfg.deepseek_api_key:
            self._adapters["deepseek"] = DeepSeekSearchAdapter(
                api_key=cfg.deepseek_api_key,
                endpoint=cfg.deepseek_endpoint,
            )
            logger.info("[SearchManager] DeepSeek Search 已启用")

        # 搜索统一走 DeepSeek
        # GPTR（深度研究）
        self._adapters["gptr"] = GPTRAdapter(
            base_url=cfg.gptr_base_url,
            timeout=cfg.gptr_timeout,
        )

        # Meilisearch（本地搜索索引，需显式启用）
        if cfg.meili_enabled:
            meili = MeiliAdapter(
                endpoint=cfg.meili_endpoint,
                api_key=cfg.meili_api_key,
                index_name=cfg.meili_index_name,
            )
            if meili.available:
                meili.ensure_index()
                self._adapters["meili"] = meili
                logger.info(f"[SearchManager] Meilisearch 已连接: {cfg.meili_endpoint}")
            else:
                logger.warning(f"[SearchManager] Meilisearch 不可用: {cfg.meili_endpoint}")

    def search(
        self,
        query: str,
        sources: Optional[List[str]] = None,
        max_results: int = 0,
        use_cache: bool = True,
        min_grade: Optional[SourceGrade] = None,
        deduplicate: bool = True,
    ) -> List[SearchResult]:
        """
        统一搜索入口（同步）

        Args:
            query: 搜索查询
            sources: 使用的搜索源列表，None则使用默认源
            max_results: 每个源的最大结果数，0则使用配置默认值
            use_cache: 是否使用缓存
            min_grade: 最低信源等级，None则使用配置默认值
            deduplicate: 是否按URL去重

        Returns:
            统一的 SearchResult 列表
        """
        if not query or not query.strip():
            return []

        sources = sources or self.config.default_sources
        max_results = max_results or self.config.max_results_per_source
        min_grade = min_grade or self.config.default_min_grade

        # 检查缓存
        if use_cache and self._cache:
            cached = self._cache.get(query)
            if cached:
                results = [SearchResult.from_dict(d) for d in cached]
                return filter_by_grade(results, min_grade)

        # 执行搜索（按 sources 顺序）
        all_results: List[SearchResult] = []
        for source_name in sources:
            adapter = self._adapters.get(source_name)
            if adapter is None:
                logger.debug(f"[SearchManager] 搜索源 '{source_name}' 未注册，跳过")
                continue

            try:
                results = adapter.search(query, max_results=max_results)
                all_results.extend(results)
                logger.debug(f"[SearchManager] {source_name}: 返回{len(results)}条结果")
            except Exception as e:
                logger.warning(f"[SearchManager] {source_name} 搜索异常: {e}")

        # 按URL去重
        if deduplicate:
            seen_urls = set()
            unique_results = []
            for r in all_results:
                if r.url and r.url in seen_urls:
                    continue
                if r.url:
                    seen_urls.add(r.url)
                unique_results.append(r)
            all_results = unique_results

        # 信源过滤
        filtered = filter_by_grade(all_results, min_grade)

        # 写入缓存
        if use_cache and self._cache and filtered:
            self._cache.set(query, [r.to_dict() for r in filtered])

        return filtered

    async def search_async(
        self,
        query: str,
        sources: Optional[List[str]] = None,
        max_results: int = 0,
        use_cache: bool = True,
        min_grade: Optional[SourceGrade] = None,
        deduplicate: bool = True,
    ) -> List[SearchResult]:
        """
        异步搜索入口

        在线程池中执行同步搜索，避免阻塞事件循环。
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._executor,
            lambda: self.search(query, sources, max_results, use_cache, min_grade, deduplicate)
        )

    def get_stats(self) -> Dict[str, Any]:
        """获取搜索统计信息"""
        stats = {
            "available_sources": list(self._adapters.keys()),
            "default_sources": self.config.default_sources,
        }
        if self._cache:
            stats["cache"] = self._cache.get_stats()
        return stats

    @property
    def available_sources(self) -> List[str]:
        """获取可用的搜索源列表"""
        return list(self._adapters.keys())

    def close(self):
        """关闭管理器，释放资源"""
        self._executor.shutdown(wait=False)
