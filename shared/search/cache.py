"""
shared/search/cache — 搜索缓存
================================

迁移自：调研工具 search_cache.py
功能：内存+文件双缓存，TTL自动过期，查询归一化
定位：服务工具流同步（洞察→调研→方案），非长期知识存储
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import asdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class CacheEntry:
    """缓存条目"""
    query: str
    normalized_query: str
    results: List[Dict]
    cached_at: str
    hit_count: int = 0


class SearchCache:
    """
    轻量级搜索缓存

    特点：
    - 内存+文件双缓存
    - 查询归一化（去时间词、空格）
    - TTL自动过期（默认7天）
    - 统计信息
    """

    def __init__(
        self,
        enabled: bool = True,
        ttl_days: int = 7,
        max_items: int = 1000,
        cache_dir: str = None,
    ):
        self.enabled = enabled
        self.ttl = timedelta(days=ttl_days)
        self.max_items = max_items
        self._cache: Dict[str, CacheEntry] = {}
        self._lock = threading.Lock()
        self._cache_dir = None
        if cache_dir:
            self._cache_dir = Path(cache_dir)
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            self._load_from_disk()
        self._session_hits = 0
        self._session_misses = 0
        logger.info(f"[SearchCache] 初始化: enabled={enabled}, ttl={ttl_days}天, max={max_items}")

    def get(self, query: str) -> Optional[List[Dict]]:
        if not self.enabled:
            return None
        normalized = self._normalize(query)
        with self._lock:
            entry = self._cache.get(normalized)
            if entry:
                cached_at = datetime.fromisoformat(entry.cached_at)
                if datetime.now() - cached_at < self.ttl:
                    entry.hit_count += 1
                    self._session_hits += 1
                    return entry.results
                else:
                    del self._cache[normalized]
            self._session_misses += 1
        return None

    def set(self, query: str, results: List[Dict]):
        if not self.enabled or not results:
            return
        normalized = self._normalize(query)
        with self._lock:
            if len(self._cache) >= self.max_items:
                self._evict_oldest()
            self._cache[normalized] = CacheEntry(
                query=query,
                normalized_query=normalized,
                results=results,
                cached_at=datetime.now().isoformat(),
            )
        if self._cache_dir:
            self._save_to_disk()

    def get_stats(self) -> Dict:
        total = self._session_hits + self._session_misses
        hit_rate = self._session_hits / total if total > 0 else 0
        return {
            "enabled": self.enabled,
            "session_hits": self._session_hits,
            "session_misses": self._session_misses,
            "hit_rate": f"{hit_rate:.1%}",
            "cached_items": len(self._cache),
            "estimated_savings": f"¥{self._session_hits * 0.01:.2f}",
            "ttl_days": self.ttl.days,
        }

    def clear(self):
        with self._lock:
            self._cache.clear()
            self._session_hits = 0
            self._session_misses = 0
        if self._cache_dir:
            for f in self._cache_dir.glob("*.json"):
                f.unlink()

    def _normalize(self, query: str) -> str:
        """
        查询归一化（用于缓存键匹配）
        
        目标：让语义相同的query命中同一个缓存
        "2024年中国奶茶市场规模分析" ≈ "中国奶茶市场规模 2024"
        """
        q = query.lower().strip()
        
        # 1. 去掉年份（2020-2099，含前面的"年"）
        q = re.sub(r"20[2-9]\d年?", "", q)
        
        # 2. 去掉常见时间修饰词
        q = re.sub(r"(今年|去年|近期|最近|当前|目前|最新|上半年|下半年|一季度|二季度|三季度|四季度)", "", q)
        
        # 3. 去掉常见分析修饰词（单独出现时去掉，但"趋势""格局"等有语义价值的保留）
        q = re.sub(r"(?<![趋势格局生态])分析|(?<![趋势格局生态])报告|(?<![趋势格局生态])研究|调研|洞察|预测|前景|现状|概况|概述|总结|整理", "", q)
        
        # 4. 去掉常见程度修饰词
        q = re.sub(r"(深度|全面|详细|详细|完整|系统|专业|快速|简要)", "", q)
        
        # 5. 去掉标点符号（保留中文、英文、数字）
        q = re.sub(r"[^\u4e00-\u9fa5a-z0-9\s]", "", q)
        
        # 6. 合并多余空格
        q = re.sub(r"\s+", " ", q)
        
        return q.strip()

    def _evict_oldest(self):
        if not self._cache:
            return
        oldest = min(self._cache, key=lambda k: datetime.fromisoformat(self._cache[k].cached_at))
        del self._cache[oldest]

    def _save_to_disk(self):
        if not self._cache_dir:
            return
        try:
            data = {"saved_at": datetime.now().isoformat(), "entries": [asdict(e) for e in self._cache.values()]}
            (self._cache_dir / "search_cache.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"[SearchCache] 保存失败: {e}")

    def _load_from_disk(self):
        if not self._cache_dir:
            return
        try:
            f = self._cache_dir / "search_cache.json"
            if not f.exists():
                return
            data = json.loads(f.read_text(encoding="utf-8"))
            for e_data in data.get("entries", []):
                entry = CacheEntry(**e_data)
                if datetime.now() - datetime.fromisoformat(entry.cached_at) < self.ttl:
                    self._cache[entry.normalized_query] = entry
        except Exception as e:
            logger.warning(f"[SearchCache] 加载失败: {e}")
