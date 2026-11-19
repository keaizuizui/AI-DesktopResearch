#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小醉桌面研究 v1.0 / deep_scraper.py
Layer 2 定向深挖模块 —— 对 LLM 打分 Top N 的高价值 URL 做全文抓取

支持多 Provider（Crawl4AI），自动降级，并行抓取。
被 engine.py 在 L2/L3 补搜阶段调用。
"""

import os
import requests
from typing import List, Optional, Dict, Any
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import logger


# ============================================================
#  Adapter 基类
# ============================================================

class BaseDeepScrapeAdapter:
    """全文抓取适配器基类"""

    def scrape(self, url: str) -> Optional[str]:
        """抓取单个 URL，返回全文文本；失败返回 None"""
        raise NotImplementedError

    @property
    def name(self) -> str:
        return self.__class__.__name__


# ============================================================
#  DeepSeek Fetch 适配器
# ============================================================

class DeepSeekFetchAdapter(BaseDeepScrapeAdapter):
    """通过 DeepSeek 官方 Fetch 接口抓取网页正文（Markdown）"""

    MAX_CHARS = 15000
    TIMEOUT = 60

    def __init__(self, api_key: str, endpoint: str = "https://api.deepseek.com/v1/tools/fetch"):
        self.api_key = api_key or ""
        self.endpoint = endpoint or "https://api.deepseek.com/v1/tools/fetch"

    def scrape(self, url: str) -> Optional[str]:
        if not self.api_key or not url:
            return None
        try:
            resp = requests.post(
                self.endpoint,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={"url": url},
                timeout=self.TIMEOUT,
            )
            if resp.status_code != 200:
                logger.debug(f"[DeepSeekFetch] HTTP {resp.status_code} for {url}")
                return None
            content = (resp.json().get("content") or "").strip()
            if not content:
                return None
            logger.info(f"[DeepSeekFetch] 抓取成功 {url} ({len(content)}字)")
            return content[:self.MAX_CHARS]
        except requests.exceptions.Timeout:
            logger.debug(f"[DeepSeekFetch] 超时 {url}")
            return None
        except Exception as e:
            logger.debug(f"[DeepSeekFetch] 抓取失败 {url}: {e}")
            return None


# ============================================================
#  Crawl4AI 适配器
# ============================================================

class Crawl4AIAdapter(BaseDeepScrapeAdapter):
    """通过本地 Crawl4AI 服务抓取全文"""

    # 成功时截取前 15000 字符
    MAX_CHARS = 15000
    TIMEOUT = 60

    def __init__(self, api_url: str):
        """
        Args:
            api_url: Crawl4AI 服务地址，例如 http://localhost:11235
        """
        self.api_url = api_url.rstrip("/")

    def scrape(self, url: str) -> Optional[str]:
        if not self.api_url:
            return None
        try:
            resp = requests.post(
                f"{self.api_url}/crawl",
                json={"urls": [url], "priority": 8},
                timeout=self.TIMEOUT,
            )
            if resp.status_code != 200:
                logger.debug(f"[Crawl4AI] HTTP {resp.status_code} for {url}")
                return None
            data = resp.json()

            # Crawl4AI 返回格式：可能是列表或字典，提取 markdown / text 字段
            result = None
            if isinstance(data, list) and data:
                result = data[0]
            elif isinstance(data, dict):
                # 如果有 results 字段则取第一个
                if "results" in data and isinstance(data["results"], list) and data["results"]:
                    result = data["results"][0]
                else:
                    result = data

            if not isinstance(result, dict):
                return None

            # 优先取 markdown，其次取 text
            text = result.get("markdown") or result.get("text") or ""
            text = text.strip()
            if not text:
                return None
            logger.info(f"[Crawl4AI] 抓取成功 {url} ({len(text)}字)")
            return text[:self.MAX_CHARS]
        except requests.exceptions.Timeout:
            logger.debug(f"[Crawl4AI] 超时 {url}")
            return None
        except Exception as e:
            logger.debug(f"[Crawl4AI] 抓取失败 {url}: {e}")
            return None


# ============================================================
#  DeepScraper 主类
# ============================================================

class DeepScraper:
    """Layer 2 定向深挖 —— 对高价值URL做全文抓取"""

    def __init__(self, cfg: dict):
        """
        Args:
            cfg: 配置字典，与 v16_1_utils.py 中的 V16_1_DEFAULT_CONFIG 配合使用。
                 关键字段：
                   - deep_scrape_provider: "crawl4ai" | "auto"
                   - crawl4ai_api_url: Crawl4AI 服务地址
                   - deep_scrape_top_n: Top N 数量（默认 5）
        """
        self.cfg = cfg
        self.top_n: int = int(cfg.get("deep_scrape_top_n", 5))

        # ---------- 构建适配器链 ----------
        provider = cfg.get("deep_scrape_provider", "deepseek").lower()

        self._primary: Optional[BaseDeepScrapeAdapter] = None
        self._fallback: Optional[BaseDeepScrapeAdapter] = None

        deepseek_key = (
            cfg.get("deepseek_api_key", "")
            or cfg.get("search_api_key", "")
            or cfg.get("llm_api_key", "")
            or os.environ.get("DEEPSEEK_API_KEY", "")
        )
        deepseek_endpoint = cfg.get(
            "deepseek_fetch_endpoint", "https://api.deepseek.com/v1/tools/fetch"
        )
        crawl4ai_url = cfg.get("crawl4ai_api_url", "")

        # DeepSeek Fetch 为默认，Crawl4AI 作为可选备用
        if provider in ("deepseek", "auto") and deepseek_key:
            self._primary = DeepSeekFetchAdapter(deepseek_key, deepseek_endpoint)
            if crawl4ai_url:
                self._fallback = Crawl4AIAdapter(crawl4ai_url)
        elif crawl4ai_url:
            self._primary = Crawl4AIAdapter(crawl4ai_url)

        # ---------- 可用性检查 ----------
        if self._primary is None:
            logger.warning(
                "[DeepScraper] 未配置任何全文抓取 Provider。"
                "请配置 DeepSeek API Key 或 crawl4ai_api_url。"
            )

    def scrape_url(self, url: str) -> Optional[str]:
        """抓取单个 URL，返回全文或 None。

        自动降级逻辑：主力失败时切换到备用适配器，最后尝试搜索合法转载来源。
        所有直接抓取方式均尊重 robots.txt。
        """
        if not url:
            return None

        # 检查 robots.txt（对直接抓取方式生效）
        try:
            from shared.robots_checker import can_fetch
            if not can_fetch(url):
                logger.info(f"[DeepScraper] robots.txt 禁止抓取，跳过: {url}")
                # robots.txt 禁止时，尝试搜索合法转载来源
                if self.cfg.get("enable_search_fallback", True):
                    return self._search_legal_source(url)
                return None
        except ImportError:
            pass  # robots_checker 不可用时不阻断

        # 主力适配器
        if self._primary is not None:
            text = self._primary.scrape(url)
            if text is not None:
                return text
            logger.debug(f"[DeepScraper] 主力 {self._primary.name} 失败，尝试降级...")

        # 备用适配器
        if self._fallback is not None:
            text = self._fallback.scrape(url)
            if text is not None:
                return text
            logger.debug(f"[DeepScraper] 备用 {self._fallback.name} 也失败")

        # →搜索合法转载来源（替代 OCR 兜底）
        if self.cfg.get("enable_search_fallback", True):
            return self._search_legal_source(url)

        return None

    def _search_legal_source(self, url: str) -> Optional[str]:
        """搜索合法转载来源 — 替代 OCR 兜底

        当原始 URL 无法直接抓取时（robots.txt 禁止/付费墙/反爬），
        从 URL 提取报告名称，通过搜索引擎查找该报告的合法转载来源
        （如媒体引用、行业论坛转载、政府公开引用等）。

        适用于：艾瑞、亿欧、199IT、CBNData 等高价值付费报告站点。
        """
        try:
            # 从 URL 提取报告标题关键词
            title_keywords = self._extract_title_from_url(url)
            if not title_keywords:
                logger.debug(f"[DeepScraper] 无法从 URL 提取标题关键词: {url}")
                return None

            # 构建搜索查询：报告名称 + 转载/引用关键词
            search_queries = [
                f'"{title_keywords}" 转载',
                f'"{title_keywords}" 引用 数据',
                f'"{title_keywords}" 摘要 要点',
            ]

            # 尝试搜索合法来源
            for query in search_queries:
                try:
                    from shared.search import SearchManager, SearchConfig
                    cfg = SearchConfig(
                        deepseek_api_key=(
                            self.cfg.get("search_api_key", "")
                            or self.cfg.get("llm_api_key", "")
                            or os.environ.get("DEEPSEEK_API_KEY", "")
                        )
                    )
                    mgr = SearchManager(cfg)
                    results = mgr.search(query, max_results=3)

                    if results:
                        # 从搜索结果中提取正文
                        for r in results:
                            if r.content and len(r.content) > 200:
                                logger.info(
                                    f"[DeepScraper] 搜索合法来源成功: "
                                    f"原始={url[:50]}... → 合法来源={r.url[:50]}..."
                                )
                                return r.content[:15000]
                except Exception as e:
                    logger.debug(f"[DeepScraper] 搜索合法来源失败: {e}")
                    continue

            logger.debug(f"[DeepScraper] 未找到合法转载来源: {url}")
            return None

        except Exception as e:
            logger.debug(f"[DeepScraper] 搜索合法来源异常: {e}")
            return None

    def _extract_title_from_url(self, url: str) -> str:
        """从 URL 路径中提取报告标题关键词"""
        from urllib.parse import urlparse, unquote

        try:
            parsed = urlparse(url)
            # 去掉扩展名，取路径最后几段
            path = unquote(parsed.path)
            # 去掉 .html, .pdf, / 等后缀
            for suffix in ['.html', '.htm', '.pdf', '.shtml', '.asp', '.jsp']:
                path = path.replace(suffix, '')
            parts = [p for p in path.split('/') if p and len(p) > 2]
            if not parts:
                return ""
            # 取最后 2-3 段作为标题
            title_parts = parts[-min(3, len(parts)):]
            return ' '.join(title_parts)
        except Exception:
            return ""

    def _screenshot_url(self, url: str) -> Optional[str]:
        """使用BrowserManager（CloakBrowser优先）对URL截图，返回截图文件路径"""
        try:
            from shared.browser_manager import BrowserManager

            with BrowserManager(headless=True) as bm:
                page = bm.get_page()
                if not page:
                    return None

                page.goto(url, timeout=20000, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)  # 等待页面渲染

                # 保存截图到临时文件
                import tempfile
                screenshot_path = tempfile.mktemp(suffix=".png")
                page.screenshot(path=screenshot_path, full_page=True)
                page.close()
                return screenshot_path

        except ImportError:
            logger.debug("[DeepScraper OCR] BrowserManager未安装")
            return None
        except Exception as e:
            logger.debug(f"[DeepScraper OCR] 截图失败: {e}")
            return None

    def scrape_batch(self, results: list, top_n: int = None) -> list:
        """对搜索结果列表按 llm_score 排序，取 Top N 做全文抓取。

        Args:
            results: 搜索结果列表（SearchResult 或 dict）。
                     每个元素需有 url 属性/键 和 llm_score 属性/键。
            top_n:   覆盖默认的 Top N 数量。

        Returns:
            增强后的结果列表。每个元素会新增 full_content 字段（dict 模式）
            或 full_content 属性（SearchResult 模式）。
        """
        if not results:
            return results

        n = top_n if top_n is not None else self.top_n

        # ---------- 按 llm_score 降序排序，取 Top N ----------
        def _get_score(item: Any) -> float:
            if isinstance(item, dict):
                return float(item.get("llm_score", 0))
            return float(getattr(item, "llm_score", 0))

        def _get_url(item: Any) -> str:
            if isinstance(item, dict):
                return item.get("url", "")
            return getattr(item, "url", "")

        sorted_results = sorted(results, key=_get_score, reverse=True)
        top_items = sorted_results[:n]

        # 过滤掉没有 URL 的条目
        candidates = [(i, item) for i, item in enumerate(top_items) if _get_url(item)]

        if not candidates:
            logger.info("[DeepScraper] 无有效 URL 可抓取")
            return results

        logger.info(f"[DeepScraper] 开始并行抓取 Top {len(candidates)} 条高价值URL...")

        # ---------- 并行抓取（最多 5 线程） ----------
        def _do_scrape(idx: int, item: Any) -> tuple:
            url = _get_url(item)
            text = self.scrape_url(url)
            return idx, item, text

        scraped_map: Dict[int, str] = {}
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {
                executor.submit(_do_scrape, orig_idx, item): orig_idx
                for orig_idx, item in candidates
            }
            for fut in as_completed(futures):
                try:
                    orig_idx, item, text = fut.result()
                    if text is not None:
                        scraped_map[orig_idx] = text
                except Exception as e:
                    logger.debug(f"[DeepScraper] 线程异常: {e}")

        # ---------- 将全文写回结果 ----------
        for orig_idx, full_text in scraped_map.items():
            item = results[orig_idx]
            if isinstance(item, dict):
                item["full_content"] = full_text
            else:
                # SearchResult dataclass —— 动态添加属性
                setattr(item, "full_content", full_text)

        success_count = len(scraped_map)
        logger.info(
            f"[DeepScraper] 抓取完成: {success_count}/{len(candidates)} 成功"
        )
        return results


# ============================================================
#  便捷函数 —— 供 engine.py 直接调用
# ============================================================

def deep_scrape_results(results: list, cfg: dict) -> list:
    """便捷函数：对搜索结果做 Layer 2 全文深挖。

    Args:
        results: 搜索结果列表（SearchResult 或 dict）
        cfg:     配置字典

    Returns:
        增强后的结果列表（新增 full_content 字段）
    """
    scraper = DeepScraper(cfg)
    return scraper.scrape_batch(results)
