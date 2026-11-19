"""
shared/search — 统一搜索接口
=============================

更新：
- models.py: 信源评级域名库扩展至200+，评分→评级自动映射
- scorer.py: LLM动态打分 + 验证式QA（新增）

模块结构：
    adapters/    — 搜索源适配器（DeepSeek / GPTR / Meili）
    cache.py     — 搜索缓存（内存+文件双缓存，TTL 20天）
    models.py    — 统一数据结构（SearchResult / SearchQuery / SourceGrade）
    manager.py   — SearchManager 统一入口
    scorer.py    — SearchScorer LLM打分器

搜索源说明：
    - DeepSeek: 官方搜索（默认，复用 DeepSeek API Key）
    - GPTR:     GPT-Researcher微服务（深度研究）
    - Meili:    本地 Meilisearch 搜索索引（可选）

使用方式：
    from shared.search import SearchManager, SearchConfig, SearchScorer

    config = SearchConfig(deepseek_api_key="xxx")
    mgr = SearchManager(config)
    results = mgr.search("新能源汽车市场规模")

    # LLM打分
    scorer = SearchScorer(llm_call=your_llm_func)
    scored = scorer.score_batch(results, "新能源汽车市场规模")
"""

from shared.search.models import (
    SearchResult, SearchQuery, SourceGrade,
    score_to_grade, grade_to_score_range, sort_by_combined_score,
    filter_by_grade, grade_source, get_grade_reason,
)
from shared.search.manager import SearchManager, SearchConfig
from shared.search.scorer import SearchScorer, ScoreConfig, score_results, score_and_verify_results

__all__ = [
    # Models
    "SearchResult", "SearchQuery", "SourceGrade",
    "score_to_grade", "grade_to_score_range", "sort_by_combined_score",
    "filter_by_grade", "grade_source", "get_grade_reason",
    # Manager
    "SearchManager", "SearchConfig",
    # Scorer
    "SearchScorer", "ScoreConfig", "score_results", "score_and_verify_results",
]
