"""
sources_health — 优质源健康检测

启动时加载 sources.json，对每个源做 HTTP HEAD 请求，
超时/4xx/5xx 自动标记为死源并跳过。

参考 deep-research 的 sources.json 启动健康检测机制。
"""

from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

# sources.json 路径
_SOURCES_PATH = os.path.join(os.path.dirname(__file__), "sources.json")


class SourceHealth:
    """单个源的运行状态"""

    def __init__(self, name: str, url: str, source_type: str, priority: str):
        self.name = name
        self.url = url
        self.type = source_type
        self.priority = priority
        self.alive = False
        self.status_code = 0
        self.latency_ms = 0.0
        self.error = ""

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "url": self.url,
            "type": self.type,
            "priority": self.priority,
            "alive": self.alive,
            "status_code": self.status_code,
            "latency_ms": round(self.latency_ms, 1),
            "error": self.error,
        }


class SourcesHealthChecker:
    """优质源健康检测器"""

    def __init__(self, sources_path: str = None):
        self.sources_path = sources_path or _SOURCES_PATH
        self._sources: List[SourceHealth] = []
        self._checked = False
        self._alive_count = 0
        self._dead_count = 0

    def load_sources(self) -> List[SourceHealth]:
        """加载 sources.json 并转换为 SourceHealth 列表"""
        if not os.path.exists(self.sources_path):
            logger.warning(f"[SourcesHealth] sources.json 未找到: {self.sources_path}")
            return []

        try:
            with open(self.sources_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.warning(f"[SourcesHealth] sources.json 解析失败: {e}")
            return []

        sources = []
        categories = data.get("categories", {})
        for cat_key, cat_data in categories.items():
            priority = cat_data.get("priority", "B")
            for src in cat_data.get("sources", []):
                sources.append(
                    SourceHealth(
                        name=src["name"],
                        url=src["url"],
                        source_type=src.get("type", "unknown"),
                        priority=priority,
                    )
                )

        self._sources = sources
        logger.info(f"[SourcesHealth] 加载 {len(sources)} 个优质源")
        return sources

    def check_one(self, source: SourceHealth, timeout: int = 10, retry: int = 2) -> SourceHealth:
        """
        对单个源做 HTTP HEAD 健康检测。

        Args:
            source: 待检测源
            timeout: 超时秒数
            retry: 重试次数

        Returns:
            更新后的 SourceHealth（含 alive/status_code/latency 信息）
        """
        for attempt in range(retry + 1):
            try:
                start = time.time()
                resp = requests.head(
                    source.url,
                    timeout=timeout,
                    allow_redirects=True,
                    headers={"User-Agent": "SourcesHealthChecker/1.0"},
                )
                latency = (time.time() - start) * 1000
                source.latency_ms = latency
                source.status_code = resp.status_code

                if 200 <= resp.status_code < 500:
                    source.alive = True
                else:
                    source.alive = False
                    source.error = f"HTTP {resp.status_code}"
                return source

            except requests.exceptions.Timeout:
                source.error = "Timeout"
                if attempt < retry:
                    time.sleep(1)
            except requests.exceptions.ConnectionError as e:
                source.error = f"ConnectionError: {str(e)[:80]}"
                if attempt < retry:
                    time.sleep(1)
            except Exception as e:
                source.error = f"{type(e).__name__}: {str(e)[:80]}"
                break

        source.alive = False
        source.status_code = 0
        source.latency_ms = 0.0
        return source

    def check_all(self, timeout: int = 10, retry: int = 2, concurrency: int = 5) -> Dict:
        """
        对所有源执行健康检测（并发）。

        Args:
            timeout: 单个源超时秒数
            retry: 单个源重试次数
            concurrency: 并发数

        Returns:
            {"total": int, "alive": int, "dead": int, "sources": [...], "dead_sources": [...]}
        """
        if not self._sources:
            self.load_sources()

        if not self._sources:
            return {"total": 0, "alive": 0, "dead": 0, "sources": [], "dead_sources": []}

        logger.info(f"[SourcesHealth] 开始检测 {len(self._sources)} 个源（并发={concurrency}）...")

        alive = []
        dead = []

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {
                executor.submit(self.check_one, src, timeout, retry): src
                for src in self._sources
            }
            for future in as_completed(futures):
                src = future.result()
                if src.alive:
                    alive.append(src)
                else:
                    dead.append(src)

        self._checked = True
        self._alive_count = len(alive)
        self._dead_count = len(dead)

        logger.info(
            f"[SourcesHealth] 检测完成: {self._alive_count} 存活 / {self._dead_count} 死亡"
        )
        if dead:
            for d in dead[:5]:
                logger.debug(f"  [死源] {d.name}: {d.error}")
            if len(dead) > 5:
                logger.debug(f"  ... 及其他 {len(dead) - 5} 个死源")

        return {
            "total": len(self._sources),
            "alive": self._alive_count,
            "dead": self._dead_count,
            "sources": [s.to_dict() for s in alive],
            "dead_sources": [s.to_dict() for s in dead],
        }

    def get_alive_sources(self) -> List[SourceHealth]:
        """获取存活源列表"""
        if not self._checked:
            self.check_all()
        return [s for s in self._sources if s.alive]

    def get_alive_urls(self) -> List[str]:
        """获取存活源 URL 列表"""
        return [s.url for s in self.get_alive_sources()]

    def get_alive_by_category(self) -> Dict[str, List[SourceHealth]]:
        """按类别分组获取存活源"""
        alive = self.get_alive_sources()
        categories: Dict[str, List[SourceHealth]] = {}
        for s in alive:
            categories.setdefault(s.type, []).append(s)
        return categories


# ─── 快捷函数 ───

def check_sources_health(
    sources_path: str = None,
    timeout: int = 10,
    retry: int = 2,
    concurrency: int = 5,
) -> Dict:
    """
    快捷函数：加载并检测所有优质源。

    Returns:
        {"total": int, "alive": int, "dead": int, "sources": [...], "dead_sources": [...]}
    """
    checker = SourcesHealthChecker(sources_path)
    return checker.check_all(timeout=timeout, retry=retry, concurrency=concurrency)