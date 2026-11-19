#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小醉桌面研究 v1.0 / v16_1_utils.py
基础设施层：智能截断、搜索成本追踪、SearchResult 全文扩展
本文件被 所有功能模块依赖，请勿随意修改接口。

依赖：config.py（仅使用 SearchResult、logger）
"""

import re
import logging
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import List, Dict, Optional, Any

from config import SearchResult, logger


# ============================================================
# 新增默认配置项
# ============================================================

V16_1_DEFAULT_CONFIG = {
    # 功能2: DeepScraper（深度抓取）
    "enable_deep_scrape": True,
    "deep_scrape_provider": "crawl4ai",          # crawl4ai
    "crawl4ai_api_url": "",                   # 本地 Crawl4AI 服务地址
    "deep_scrape_top_n": 15,
    "deep_scrape_timeout": 30,


    # 功能4: 域名分级
    "domain_scraping_priority": {},           # 用户自定义域名优先级 {"example.com": 10, ...}

    # 功能5: 缺口自动补搜
    "auto_fill_final_gaps": True,
    "auto_fill_max_gaps": 3,
    "auto_fill_rounds": 1,
    "auto_fill_sources_per_round": 20,

    # 功能8: 搜索成本追踪
    "search_cost_tracking": True,
    "crawl4ai_cost_per_url": 0.0,            # Crawl4AI 本地免费
    "gptr_cost_per_session": 0.8,            # GPT Researcher 单次成本 (CNY)
}


def merge_v16_1_config(cfg: dict) -> dict:
    """将 默认配置合并到用户配置中（仅填充缺失的键，不覆盖已有值）。

    Args:
        cfg: 用户 research_config.json 加载后的完整配置字典

    Returns:
        合并后的配置字典（原地修改并返回）
    """
    for key, default_val in V16_1_DEFAULT_CONFIG.items():
        if key not in cfg:
            cfg[key] = default_val
    return cfg


# ============================================================
# 1. 智能截断函数
# ============================================================

# 中英文句子结束标记（优先在句号/问号/感叹号处截断）
_SENTENCE_ENDINGS = re.compile(r'[。！？\.\!\?\n]')

# 段落结束标记（换行符、连续空行）
_PARAGRAPH_ENDINGS = re.compile(r'\n\s*\n')


def smart_truncate(text: str, max_chars: int, min_ratio: float = 0.7) -> str:
    """智能截断函数，在句子/段落边界截断，替代所有 [:N] 硬截断。

    规则：
    - 如果文本本身 <= max_chars，直接返回原文
    - 优先在段落边界（空行）截断
    - 其次在句子边界（。！？.!?换行）截断
    - 至少保留 max_chars * min_ratio 的内容
    - 支持中英文混合文本

    Args:
        text: 待截断的文本
        max_chars: 最大字符数
        min_ratio: 最小保留比例（默认 0.7，即至少保留 70%）

    Returns:
        截断后的文本
    """
    if not text or len(text) <= max_chars:
        return text

    min_chars = int(max_chars * min_ratio)
    # 搜索区域：从 max_chars 往前到 min_chars
    search_start = min(max_chars, len(text) - 1)
    search_end = min_chars

    # 策略1: 在段落边界截断（双换行）
    segment = text[search_end:search_start]
    # 找最后一个段落结束位置（相对于 search_end 的偏移）
    paragraph_matches = list(_PARAGRAPH_ENDINGS.finditer(segment))
    if paragraph_matches:
        last_match = paragraph_matches[-1]
        cut_pos = search_end + last_match.end()
        return text[:cut_pos].rstrip()

    # 策略2: 在句子边界截断
    segment = text[search_end:search_start]
    sentence_matches = list(_SENTENCE_ENDINGS.finditer(segment))
    if sentence_matches:
        last_match = sentence_matches[-1]
        cut_pos = search_end + last_match.end()
        return text[:cut_pos].rstrip()

    # 策略3: 在逗号/分号处截断（退而求其次）
    comma_pattern = re.compile(r'[,，；;、]')
    segment = text[search_end:search_start]
    comma_matches = list(comma_pattern.finditer(segment))
    if comma_matches:
        last_match = comma_matches[-1]
        cut_pos = search_end + last_match.end()
        return text[:cut_pos].rstrip()

    # 策略4: 在空格处截断（英文场景）
    space_segment = text[search_end:search_start]
    last_space = space_segment.rfind(' ')
    if last_space >= 0:
        cut_pos = search_end + last_space
        return text[:cut_pos].rstrip()

    # 兜底: 硬截断到 max_chars
    return text[:max_chars].rstrip()


# ============================================================
# 2. 搜索成本实时追踪器
# ============================================================

@dataclass
class _CostRecord:
    """单次搜索成本记录"""
    source: str           # 来源标识（bocha/tavily/ddg/gptr/crawl4ai）
    query: str            # 查询摘要（截断到100字）
    num_results: int      # 返回结果数量 / URL数量
    cost: float           # 估算成本 (CNY)
    timestamp: str        # ISO 格式时间戳


class SearchCostTracker:
    """搜索成本实时追踪器（线程安全）。

    追踪各搜索来源的调用次数和估算成本，支持导出为 dict 供 Web 端展示。
    成本估算参数从 research_config.json 读取。

    用法:
        tracker = SearchCostTracker(cfg)
        tracker.record_search("bocha", "市场规模查询", 10, 0.01)
        summary = tracker.get_summary()
        total = tracker.get_total()
    """

    # 已知搜索来源列表
    KNOWN_SOURCES = ["deepseek", "bocha", "tavily", "ddg", "gptr", "crawl4ai"]

    def __init__(self, cfg: Optional[dict] = None):
        """初始化追踪器。

        Args:
            cfg: research_config.json 配置字典，用于读取成本估算参数。
                 如果为 None，则使用 V16_1_DEFAULT_CONFIG 中的默认值。
        """
        self._lock = threading.RLock()
        self._records: List[_CostRecord] = []
        self._source_stats: Dict[str, Dict[str, Any]] = {}

        # 从配置读取成本参数
        if cfg is None:
            cfg = {}
        self._cost_params = {
            "crawl4ai_cost_per_url": float(cfg.get("crawl4ai_cost_per_url", V16_1_DEFAULT_CONFIG["crawl4ai_cost_per_url"])),
            "gptr_cost_per_session": float(cfg.get("gptr_cost_per_session", V16_1_DEFAULT_CONFIG["gptr_cost_per_session"])),
        }

        # 初始化各来源统计
        for src in self.KNOWN_SOURCES:
            self._source_stats[src] = {
                "count": 0,
                "total_cost": 0.0,
                "total_results": 0,
            }

    def record_search(self, source: str, query: str, num_results: int = 0, cost: float = 0.0) -> None:
        """记录一次搜索调用。

        Args:
            source: 搜索来源标识（bocha/tavily/ddg/gptr/crawl4ai）
            query: 查询摘要
            num_results: 返回结果数量 / URL数量
            cost: 估算成本 (CNY)，传 0.0 时自动根据来源和 num_results 估算
        """
        # 自动估算成本（如果调用方未传 cost）
        if cost == 0.0 and num_results > 0:
            cost = self._estimate_cost(source, num_results)

        record = _CostRecord(
            source=source,
            query=query[:100],  # 截断查询摘要，避免内存膨胀
            num_results=num_results,
            cost=cost,
            timestamp=datetime.now().isoformat(timespec="seconds"),
        )

        with self._lock:
            self._records.append(record)
            if source not in self._source_stats:
                self._source_stats[source] = {
                    "count": 0,
                    "total_cost": 0.0,
                    "total_results": 0,
                }
            stats = self._source_stats[source]
            stats["count"] += 1
            stats["total_cost"] = round(stats["total_cost"] + cost, 4)
            stats["total_results"] += num_results

        logger.debug(f"[成本追踪] {source}: +{cost:.4f}元 (查询: {query[:50]}..., 结果数: {num_results})")

    def _estimate_cost(self, source: str, num_results: int) -> float:
        """根据来源和结果数量自动估算成本。

        Args:
            source: 搜索来源标识
            num_results: 结果/URL数量

        Returns:
            估算成本 (CNY)
        """
        if source == "crawl4ai":
            return self._cost_params["crawl4ai_cost_per_url"] * num_results
        elif source == "gptr":
            return self._cost_params["gptr_cost_per_session"]
        # bocha/tavily/ddg 等搜索API的成本由外部传入，此处返回0
        return 0.0

    def get_total(self) -> float:
        """获取总估算成本。

        Returns:
            所有来源的总成本 (CNY)
        """
        with self._lock:
            return round(sum(s["total_cost"] for s in self._source_stats.values()), 4)

    def get_summary(self) -> dict:
        """获取成本汇总，供 Web 端展示。

        Returns:
            包含 total_cost、source_breakdown、recent_records 的字典
        """
        with self._lock:
            source_breakdown = {}
            for src, stats in self._source_stats.items():
                if stats["count"] > 0:
                    source_breakdown[src] = {
                        "count": stats["count"],
                        "total_cost": round(stats["total_cost"], 4),
                        "total_results": stats["total_results"],
                        "avg_cost_per_call": round(stats["total_cost"] / stats["count"], 4) if stats["count"] > 0 else 0.0,
                    }

            # 最近 20 条记录
            recent = []
            for r in self._records[-20:]:
                recent.append({
                    "source": r.source,
                    "query": r.query,
                    "num_results": r.num_results,
                    "cost": round(r.cost, 4),
                    "timestamp": r.timestamp,
                })

            return {
                "total_cost": self.get_total(),
                "total_calls": sum(s["count"] for s in self._source_stats.values()),
                "source_breakdown": source_breakdown,
                "recent_records": recent,
            }

    def get_source_cost(self, source: str) -> float:
        """获取指定来源的总成本。

        Args:
            source: 搜索来源标识

        Returns:
            该来源的总成本 (CNY)
        """
        with self._lock:
            stats = self._source_stats.get(source)
            if stats:
                return round(stats["total_cost"], 4)
            return 0.0

    def reset(self) -> None:
        """重置所有追踪数据。"""
        with self._lock:
            self._records.clear()
            for src in self._source_stats:
                self._source_stats[src] = {
                    "count": 0,
                    "total_cost": 0.0,
                    "total_results": 0,
                }
        logger.info("[成本追踪] 已重置")


# ============================================================
# 3. SearchResult 全文扩展
# ============================================================

@dataclass
class MaterialWithFullText:
    """SearchResult 的全文扩展包装器。

    由于 SearchResult 是 dataclass 不能动态加字段，
    本类包装 SearchResult 并增加 full_text 字段，
    用于存储深度抓取（DeepScraper）获取的网页全文。

    用法:
        from config import SearchResult
        result = SearchResult(title="...", url="...", content="摘要...")
        material = MaterialWithFullText.from_search_result(result)
        material.full_text = "这里是抓取到的完整网页内容..."
        # 还原为普通 SearchResult（用 full_text 替代 content）
        sr = material.to_search_result()
        # 导出完整数据（含 full_text）
        d = material.to_dict()
    """

    # ---- 与 SearchResult 对齐的字段 ----
    title: str = ""
    url: str = ""
    content: str = ""
    source: str = ""
    source_type: str = "web"
    score: float = 0.0
    llm_score: float = 0.0
    facts: List[str] = field(default_factory=list)
    reliability: str = "medium"
    raw_html: str = ""
    confidence: float = 0.0
    fact_card: str = ""
    tier: str = "B"

    # --扩展字段 ----
    full_text: str = ""  # 深度抓取获取的网页全文

    @classmethod
    def from_search_result(cls, result: SearchResult, full_text: str = "") -> "MaterialWithFullText":
        """从 SearchResult 创建 MaterialWithFullText 实例。

        Args:
            result: 原始 SearchResult 对象
            full_text: 可选的全文内容

        Returns:
            MaterialWithFullText 实例
        """
        return cls(
            title=result.title,
            url=result.url,
            content=result.content,
            source=result.source,
            source_type=result.source_type,
            score=result.score,
            llm_score=result.llm_score,
            facts=list(result.facts),
            reliability=result.reliability,
            raw_html=result.raw_html,
            confidence=result.confidence,
            fact_card=result.fact_card,
            tier=result.tier,
            full_text=full_text,
        )

    def to_search_result(self) -> SearchResult:
        """转换为普通 SearchResult。

        如果 full_text 非空，则用 full_text 替代 content（截断到 8000 字），
        保留原始 content 到 raw_html 字段以供回溯。

        Returns:
            SearchResult 实例
        """
        # 如果有全文，用全文替代 content
        if self.full_text:
            # 保留原始 content 到 raw_html（如果 raw_html 为空）
            final_raw = self.raw_html or self.content
            final_content = self.full_text[:8000]
        else:
            final_raw = self.raw_html
            final_content = self.content

        return SearchResult(
            title=self.title,
            url=self.url,
            content=final_content,
            source=self.source,
            source_type=self.source_type,
            score=self.score,
            llm_score=self.llm_score,
            facts=list(self.facts),
            reliability=self.reliability,
            raw_html=final_raw,
            confidence=self.confidence,
            fact_card=self.fact_card,
            tier=self.tier,
        )

    def to_dict(self) -> dict:
        """导出为完整字典（含 full_text），供序列化和 Web 展示。

        Returns:
            包含所有字段（含 full_text）的字典
        """
        return asdict(self)


def enrich_with_full_text(result: SearchResult, full_text: str) -> dict:
    """将全文存储到 result 的扩展字段中，返回扩展后的 dict。

    这是一个便捷函数，等价于：
        material = MaterialWithFullText.from_search_result(result, full_text)
        return material.to_dict()

    Args:
        result: 原始 SearchResult 对象
        full_text: 深度抓取获取的网页全文

    Returns:
        包含 full_text 字段的完整字典
    """
    material = MaterialWithFullText.from_search_result(result, full_text)
    return material.to_dict()
