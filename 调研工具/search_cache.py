#!/usr/bin/env python3
"""
轻量级搜索缓存模块
- Web端：无感知，静默省钱
- 本地端：透明展示，方便测试
- TTL: 20天自动过期
"""

import re
import json
import os
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict
from pathlib import Path

from config import logger


@dataclass
class CacheEntry:
    """缓存条目"""
    query: str                    # 原始查询
    normalized_query: str         # 归一化查询（缓存键）
    results: List[Dict]           # 搜索结果（序列化为dict）
    cached_at: str                # 缓存时间 ISO格式
    hit_count: int = 0            # 命中次数


class SearchCache:
    """
    轻量级搜索缓存
    
    特点：
    - 内存+文件双缓存（本地持久化，Web重启清空）
    - 查询归一化（去时间词、空格）
    - TTL自动过期（默认20天）
    - 统计信息（本地调试用）
    """
    
    def __init__(self, 
                 enabled: bool = True, 
                 ttl_days: int = 20,
                 max_items: int = 1000,
                 cache_dir: str = None):
        self.enabled = enabled
        self.ttl = timedelta(days=ttl_days)
        self.max_items = max_items
        
        # 内存缓存
        self._cache: Dict[str, CacheEntry] = {}
        
        # 本地持久化（仅本地端使用）
        self._cache_dir = None
        if cache_dir:
            self._cache_dir = Path(cache_dir)
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            self._load_from_disk()
        
        # 统计
        self._session_hits = 0
        self._session_misses = 0
        
        logger.info(f"[SearchCache] 初始化: enabled={enabled}, ttl={ttl_days}天, max={max_items}")
    
    def get(self, query: str) -> Optional[List[Dict]]:
        """
        获取缓存结果
        
        Returns:
            缓存的搜索结果列表，未命中返回None
        """
        if not self.enabled:
            return None
        
        normalized = self._normalize(query)
        entry = self._cache.get(normalized)
        
        if entry:
            # 检查是否过期
            cached_at = datetime.fromisoformat(entry.cached_at)
            if datetime.now() - cached_at < self.ttl:
                entry.hit_count += 1
                self._session_hits += 1
                logger.debug(f"[SearchCache] 命中: {query[:50]}... (已命中{entry.hit_count}次)")
                return entry.results
            else:
                # 过期删除
                del self._cache[normalized]
                logger.debug(f"[SearchCache] 过期删除: {query[:50]}...")
        
        self._session_misses += 1
        return None
    
    def set(self, query: str, results: List[Dict]):
        """
        保存到缓存
        
        Args:
            query: 原始查询
            results: 搜索结果列表
        """
        if not self.enabled or not results:
            return
        
        # 限制缓存大小
        if len(self._cache) >= self.max_items:
            self._evict_oldest()
        
        normalized = self._normalize(query)
        entry = CacheEntry(
            query=query,
            normalized_query=normalized,
            results=results,
            cached_at=datetime.now().isoformat(),
            hit_count=0
        )
        
        self._cache[normalized] = entry
        logger.debug(f"[SearchCache] 缓存: {query[:50]}... ({len(results)}条结果)")
        
        # 本地端持久化
        if self._cache_dir:
            self._save_to_disk()
    
    def get_stats(self) -> Dict:
        """
        获取缓存统计（本地调试用）
        
        Returns:
            统计信息字典
        """
        total_session = self._session_hits + self._session_misses
        hit_rate = self._session_hits / total_session if total_session > 0 else 0
        
        # 估算节省费用（假设每次搜索¥0.01）
        cost_per_search = 0.01
        estimated_savings = self._session_hits * cost_per_search
        
        return {
            "enabled": self.enabled,
            "session_hits": self._session_hits,
            "session_misses": self._session_misses,
            "session_total": total_session,
            "hit_rate": f"{hit_rate:.1%}",
            "cached_items": len(self._cache),
            "estimated_savings": f"¥{estimated_savings:.2f}",
            "ttl_days": self.ttl.days,
        }
    
    def clear(self):
        """清空缓存"""
        self._cache.clear()
        self._session_hits = 0
        self._session_misses = 0
        if self._cache_dir:
            for f in self._cache_dir.glob("*.json"):
                f.unlink()
        logger.info("[SearchCache] 缓存已清空")
    
    def _normalize(self, query: str) -> str:
        """
        查询归一化
        
        处理：
        - 转小写
        - 去多余空格
        - 年份泛化（2024/2025等 → {{YEAR}}）
        - 去标点
        """
        # 转小写，去首尾空格
        q = query.lower().strip()
        
        # 统一空格
        q = re.sub(r'\s+', ' ', q)
        
        # 年份泛化（2020-2029）
        q = re.sub(r'20[2-9]\d', '{{YEAR}}', q)
        
        # 去标点（保留中英文核心内容）
        q = re.sub(r'[^\u4e00-\u9fa5a-z0-9\s]', '', q)
        
        return q.strip()
    
    def _evict_oldest(self):
        """淘汰最旧的缓存条目"""
        if not self._cache:
            return
        
        # 找最早缓存的
        oldest_key = min(
            self._cache.keys(),
            key=lambda k: datetime.fromisoformat(self._cache[k].cached_at)
        )
        del self._cache[oldest_key]
        logger.debug(f"[SearchCache] 淘汰旧缓存: {oldest_key[:50]}...")
    
    def _save_to_disk(self):
        """保存到本地文件（仅本地端）"""
        if not self._cache_dir:
            return
        
        try:
            cache_file = self._cache_dir / "search_cache.json"
            data = {
                "saved_at": datetime.now().isoformat(),
                "entries": [asdict(e) for e in self._cache.values()]
            }
            cache_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception as e:
            logger.warning(f"[SearchCache] 保存缓存失败: {e}")
    
    def _load_from_disk(self):
        """从本地文件加载（仅本地端）"""
        if not self._cache_dir:
            return
        
        try:
            cache_file = self._cache_dir / "search_cache.json"
            if not cache_file.exists():
                return
            
            data = json.loads(cache_file.read_text(encoding='utf-8'))
            
            for e_data in data.get("entries", []):
                entry = CacheEntry(**e_data)
                # 检查是否过期
                cached_at = datetime.fromisoformat(entry.cached_at)
                if datetime.now() - cached_at < self.ttl:
                    self._cache[entry.normalized_query] = entry
            
            logger.info(f"[SearchCache] 从磁盘加载: {len(self._cache)}条缓存")
        except Exception as e:
            logger.warning(f"[SearchCache] 加载缓存失败: {e}")


# 便捷函数
def create_cache(cfg: Dict) -> SearchCache:
    """
    根据配置创建缓存实例
    
    Args:
        cfg: 配置字典
        
    Returns:
        SearchCache实例
    """
    enabled = cfg.get("enable_search_cache", True)
    ttl_days = cfg.get("search_cache_ttl_days", 20)
    max_items = cfg.get("search_cache_max_items", 1000)
    
    # 本地端使用持久化缓存
    cache_dir = None
    output_dir = cfg.get("output_dir", "")
    if output_dir:
        cache_dir = os.path.join(output_dir, ".cache")
    
    return SearchCache(
        enabled=enabled,
        ttl_days=ttl_days,
        max_items=max_items,
        cache_dir=cache_dir
    )


if __name__ == "__main__":
    # 测试
    cache = SearchCache(enabled=True, ttl_days=20)
    
    # 测试归一化
    test_queries = [
        "新能源汽车 市场规模 2025",
        "新能源汽车 市场规模 2024",
        "  新能源 汽车 市场 规模 2025  ",
    ]
    
    print("归一化测试:")
    for q in test_queries:
        normalized = cache._normalize(q)
        print(f"  '{q}' → '{normalized}'")
    
    # 测试缓存
    print("\n缓存测试:")
    cache.set("test query", [{"title": "test", "url": "http://test.com"}])
    result = cache.get("test query")
    print(f"  命中: {result is not None}")
    
    stats = cache.get_stats()
    print(f"\n统计: {stats}")
