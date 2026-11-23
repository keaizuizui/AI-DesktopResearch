#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小醉桌面研究 v1.0 / search.py
搜索层：多源API检索、LLM打分筛选、验证式QA、STORM辩论、搜索密度、GPT Researcher
"""

import os
import re, time, asyncio, json, atexit
from typing import List, Dict, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict

import requests

from datetime import datetime
from v15_3_config import SOURCE_CREDIBILITY, CHAPTER_BUDGET, get_source_tier
# 延迟导入 smart_truncate，避免循环依赖（search → v16_1_utils → config → search）
# smart_truncate 仅在函数内使用，不影响模块级加载
def smart_truncate(text, max_chars, min_ratio=0.7):
    """延迟导入的smart_truncate包装器"""
    from v16_1_utils import smart_truncate as _st
    return _st(text, max_chars, min_ratio)

from config import (
    LLMClient, SearchResult, normalize_query, logger,
    current_year_anchor, safe_json_loads,
    GPTR_DOMAIN_BLACKLIST,
    AGENT_PROMPTS,  # 专用Agent Prompt
)

# Agent 安全管控集成
try:
    from shared.agent_security import get_security_guard
    _SECURITY_AVAILABLE = True
except ImportError:
    _SECURITY_AVAILABLE = False
    logger.warning("[AgentSecurity] shared.agent_security 未找到，安全管控未启用")

# 多路线调度
from shared.billing import RouteDispatcher
from shared.search.models import CLAIM_NATURES, CLAIM_UNKNOWN


def _get_tier_from_url(url: str) -> str:
    """根据URL获取信源等级 - 使用shared模块的grade_source"""
    from shared.search.models import grade_source, SourceGrade
    grade = grade_source(url)
    return grade.value


class BaseSearchAdapter:
    def search(self, query: str, max_results: int = 5) -> List[SearchResult]:
        raise NotImplementedError


class DeepSeekAdapter(BaseSearchAdapter):
    """
    DeepSeek 官方搜索适配器（委托到 shared.search.adapters）

    Endpoint: POST https://api.deepseek.com/v1/tools/search
    复用 DeepSeek LLM 的 API Key。
    """

    def __init__(self, api_key: str, endpoint: str = ""):
        from shared.search.adapters import DeepSeekSearchAdapter as SharedDeepSeek
        self._adapter = SharedDeepSeek(
            api_key=api_key,
            endpoint=endpoint or "https://api.deepseek.com/v1/tools/search",
        )

    def search(self, query: str, max_results: int = 5) -> List[SearchResult]:
        results = self._adapter.search(query, max_results)
        return [_shared_to_search_result(r) for r in results]


def _shared_to_search_result(r) -> SearchResult:
    """
    转换 shared.search.SearchResult 为调研工具的 SearchResult

    shared模型已合并所有字段，直接映射即可。
    增加 data_type 字段映射。
    """
    from shared.search.models import SourceGrade
    grade_val = r.tier or (r.grade.value if hasattr(r, 'grade') and hasattr(r.grade, 'value') else 'B')
    return SearchResult(
        title=r.title,
        url=r.url,
        content=r.content or r.snippet or "",
        source=r.source,
        source_type=r.source_type,
        tier=grade_val,
        llm_score=getattr(r, 'llm_score', 0.0),
        score=getattr(r, 'score', 0.0),
        facts=list(getattr(r, 'facts', [])),
        reliability=getattr(r, 'reliability', 'medium'),
        confidence=getattr(r, 'confidence', 0.0),
        fact_card=getattr(r, 'fact_card', ''),
        data_type=getattr(r, 'data_type', 'unknown'),
    )


class PlaywrightEnhancer:
    def __init__(self, enabled: bool = False, cfg: Optional[dict] = None):
        self.cfg = cfg or {}
        self.enabled = enabled
        self._playwright = None
        self._browser = None
        self._consecutive_failures = 0
        self._max_failures = max(1, int(self.cfg.get("playwright_max_failures", 3)))
        self._fallback = self._build_fallback()

    def _build_fallback(self):
        """Playwright 连续失败后的 DeepSeek Fetch 兜底。"""
        key = (
            self.cfg.get("search_api_key")
            or self.cfg.get("llm_api_key")
            or os.environ.get("DEEPSEEK_API_KEY", "")
        )
        if not key:
            return None
        try:
            from deep_scraper import DeepSeekFetchAdapter
            return DeepSeekFetchAdapter(
                key,
                self.cfg.get("deepseek_fetch_endpoint", "https://api.deepseek.com/v1/tools/fetch"),
            )
        except Exception as e:
            logger.debug(f"[Playwright] DeepSeek 兜底初始化失败: {e}")
            return None

    def _record_failure(self, reason: str):
        self._consecutive_failures += 1
        logger.debug(
            f"[Playwright] 抓取失败 {self._consecutive_failures}/{self._max_failures}: {reason}"
        )
        if self._consecutive_failures >= self._max_failures and self.enabled:
            self.enabled = False
            logger.warning(
                f"[Playwright] 连续{self._max_failures}次失败，已切换到 DeepSeek Fetch"
            )

    def _deepseek_fetch(self, url: str) -> str:
        if not self._fallback:
            return ""
        return self._fallback.scrape(url) or ""

    def _init_browser(self):
        if not self.enabled or self._browser:
            return
        try:
            # Replaced by BrowserManager
            from shared.browser_manager import BrowserManager
            self._bm = BrowserManager()
            self._browser = self._bm.ensure_browser()
            if not self._browser:
                raise RuntimeError("浏览器未就绪")
            logger.info("[Playwright] 浏览器已启动")
        except Exception as e:
            self._record_failure(f"启动失败: {e}")

    def extract_article(self, url: str) -> str:
        """智能全文提取——根据域名特征选择最优提取策略。

        策略分层：
        1. 域名特征匹配（知乎/公众号/新闻站/文档站）
        2. 通用CSS选择器（article/main/.content等）
        3. 兜底：body全文
        连续失败达到阈值后，自动切换到 DeepSeek Fetch。
        """
        if not self.enabled:
            return self._deepseek_fetch(url)
        self._init_browser()
        if not self._browser:
            return self._deepseek_fetch(url)
        page = None
        try:
            page = self._browser.new_page()
            page.goto(url, timeout=15000, wait_until="domcontentloaded")
            
            # 域名特征匹配 → 选择最优提取策略
            domain_selectors = self._get_domain_selectors(url)
            text = ""
            
            # 先尝试域名特定选择器
            for sel in domain_selectors:
                el = page.query_selector(sel)
                if el:
                    text = el.inner_text()[:10000]
                    if len(text) > 500:
                        logger.debug(f"[Playwright] 域名匹配提取成功: {sel} ({len(text)}字)")
                        break
            
            # 再尝试通用选择器
            if not text or len(text) < 500:
                generic_selectors = ["article", "main", ".post-content", ".article-content",
                                     "#content", ".content", ".entry-content", ".rich_media_content"]
                for sel in generic_selectors:
                    el = page.query_selector(sel)
                    if el:
                        text = el.inner_text()[:10000]
                        if len(text) > 500:
                            logger.debug(f"[Playwright] 通用选择器提取成功: {sel} ({len(text)}字)")
                            break
            
            # 兜底：body全文
            if not text or len(text) < 200:
                text = page.inner_text("body")[:10000]
                logger.debug(f"[Playwright] body兜底提取: {len(text)}字")
            
            if text and len(text) >= 200:
                self._consecutive_failures = 0
                return text
            self._record_failure("正文少于200字")
            return self._deepseek_fetch(url)
        except Exception as e:
            self._record_failure(str(e))
            return self._deepseek_fetch(url)
        finally:
            if page:
                try:
                    page.close()
                except (RuntimeError, OSError) as e:
                    logger.debug(f"[Playwright] 页面关闭失败: {e}")

    def _get_domain_selectors(self, url: str) -> List[str]:
        """根据URL域名返回特定的CSS选择器列表"""
        url_lower = url.lower()
        
        # 知乎
        if "zhihu.com" in url_lower:
            return [".RichContent-inner", "#root .RichContent", ".Post-RichTextContainer"]
        
        # 微信公众号
        if "mp.weixin.qq.com" in url_lower or "weixin.qq.com" in url_lower:
            return ["#js_content", ".rich_media_content", "#img-content"]
        
        # 36氪
        if "36kr.com" in url_lower:
            return [".article-content", ".articleDetailContent", "article"]
        
        # 虎嗅
        if "huxiu.com" in url_lower:
            return [".article-content", "#article-content", "article"]
        
        # 钛媒体
        if "tmtpost.com" in url_lower:
            return [".post-content", "article", "#content"]
        
        # 新浪财经
        if "sina.com.cn" in url_lower or "finance.sina" in url_lower:
            return ["#artibody", ".article-content", "article"]
        
        # 网易
        if "163.com" in url_lower:
            return [".post_body", "#content", "article"]
        
        # 腾讯新闻
        if "qq.com" in url_lower:
            return ["#Cnt-Main-Article-QQ", ".content-article", "article"]
        
        # 今日头条
        if "toutiao.com" in url_lower or "ixigua.com" in url_lower:
            return [".article-content", "article", "#content"]
        
        # CSDN
        if "csdn.net" in url_lower:
            return ["#content_views", ".blog_container_aside ~ div", "article"]
        
        # 简书
        if "jianshu.com" in url_lower:
            return ["article", "._2rhmJa", "#content"]
        
        # 百度文库/百家号
        if "baijiahao.baidu.com" in url_lower:
            return [".index-module_articleWrap_2Zphx", "article", "#content"]
        
        # 默认：返回空列表，让通用选择器处理
        return []

    def close(self):
        try:
            if hasattr(self, '_bm') and self._bm:
                self._bm.close()
        except (RuntimeError, OSError) as e:
            # 浏览器关闭失败，记录日志但继续清理
            logger.debug(f"[Playwright] 浏览器关闭失败: {e}")
        self._browser = None
        self._playwright = None


class SearchScorer:
    def __init__(self, llm: LLMClient, cfg: dict):
        self.llm = llm
        self.cfg = cfg

    def score_batch(self, results: List[SearchResult], query: str) -> List[SearchResult]:
        if not results or not self.llm.available:
            return results
        scored = []
        for r in results[:5]:
            prompt = f"""【角色】信息质量评估专家
【任务】评估搜索结果的质量和相关性

【用户查询】{query}
【来源标题】{r.title}
【来源内容】{smart_truncate(r.content, 1500)}

【评估维度】
- score：1-10分，评估相关性和信息密度
- facts：提取3-5个核心事实，必须包含具体数据
- reliability：high/medium/low，评估来源可信度
- reason：一句话评估理由，说明打分依据

【输出要求】
- 只输出纯JSON对象，不要任何其他文字
- 确保JSON语法100%正确
- 标准格式：
{{"score": 8,"facts": ["核心事实1","核心事实2","核心事实3"],"reliability": "high","reason": "评估理由"}}"""
            try:
                raw = self.llm.fast(
                    [{"role": "user", "content": prompt}],
                    system=AGENT_PROMPTS["evaluator"],
                    max_tokens=600, json_mode=True
                )
                data = safe_json_loads(raw, {})
                if isinstance(data, dict):
                    r.llm_score = float(data.get("score", 5))
                    r.facts = data.get("facts", [])
                    r.reliability = data.get("reliability", "medium")
                else:
                    r.llm_score = 5.0
            except Exception as e:
                logger.debug(f"[打分失败] {e}")
                r.llm_score = 5.0
            scored.append(r)
        scored.extend(results[5:])
        return scored

def _extract_plan_key_info(plan_text: str) -> tuple:
    """从plan_text提取调研方向和核心追问，用于注入GPT Researcher。"""
    directions = []
    core_questions = []

    # 提取【建议调研方向】
    dir_match = re.search(r'【建议调研方向】\s*(.+?)(?=【|$)', plan_text, re.DOTALL)
    if dir_match:
        dir_text = dir_match.group(1)
        for line in dir_text.split('\n'):
            line = re.sub(r'^\d+[\.\、\s]*\**', '', line).strip()
            line = re.sub(r'\*+', '', line)
            if len(line) > 10:
                directions.append(line)

    # 提取【核心追问】
    q_match = re.search(r'【核心追问】\s*(.+?)(?=\n\n|$)', plan_text, re.DOTALL)
    if q_match:
        q_text = q_match.group(1)
        for line in q_text.split('\n'):
            line = re.sub(r'^\d+[\.\、\s]*', '', line).strip()
            line = re.sub(r'\*+', '', line)
            if len(line) > 10:
                core_questions.append(line)

    return directions, core_questions


def _build_enriched_query(refined_task: str, plan_text: str) -> str:
    """构建精简查询——任务主体 + 最多 3 个方向标题，上限 150 字。

    注意：不要把“方向一、xxx、方向二、xxx”这类长句碎片拼进来，
    否则 GPTR 会拿整段任务当搜索词，召回广告页/报告列表页。
    """
    directions, core_questions = _extract_plan_key_info(plan_text)

    def _clean_direction(d: str) -> str:
        d = (d or "").strip()
        # "方向一：三重监管..."先去掉方向编号，保留真正标题
        m = re.match(r'^方向[一二三四五六七八九十\d]+[、：:.\s]*(.*)$', d)
        if m and m.group(1).strip():
            d = m.group(1).strip()
        head = re.split(r'[：:\n]', d, maxsplit=1)[0].strip()
        # 去掉括号注释，避免查询词被截断在“（”处
        head = re.sub(r'[（(][^）)]*[）)]', '', head).strip()
        return re.sub(r'^\*+|\*+$', '', head).strip()[:16]

    # 任务主体取第一段，只切掉已知的补充段落标记，避免误伤【调研主题】这类主体
    task_brief = re.split(r'【\s*(?:用户补充信息|澄清问题自动回答|建议调研方向|核心追问)', refined_task, maxsplit=1)[0].strip()
    task_brief = (task_brief or refined_task)[:120].rstrip('。；;，,')
    parts = [task_brief] if task_brief else []
    # 优先取“方向X”标题行，避免把方向下的解释段当成关键词
    dir_titles = [d for d in directions if d.strip().startswith("方向")]
    source = dir_titles or directions
    short_dirs = [s for s in (_clean_direction(d) for d in source[:3]) if s]
    if short_dirs:
        parts.append("；".join(short_dirs))
    elif core_questions:
        parts.append(core_questions[0][:40])
    query = "。".join(parts)
    return query[:150]


def _build_parallel_queries(refined_task: str, plan_text: str, max_queries: int = 3) -> List[str]:
    """基于Plan调研方向拆分多个互补子查询，用于GPTR并行搜索。

    拆分策略：
    - 从plan_text提取【建议调研方向】和【核心追问】
    - 每个方向生成一个独立的子查询（含任务主题+方向+追问）
    - 如果方向不足，用核心追问补充
    - 上限 max_queries 条，每条不超过400字

    Args:
        refined_task: 精炼后的调研任务
        plan_text: Plan文本（含【建议调研方向】和【核心追问】）
        max_queries: 最大子查询数

    Returns:
        子查询列表（1~max_queries条）
    """
    directions, core_questions = _extract_plan_key_info(plan_text)
    task_brief = refined_task[:150].rsplit('\n', 1)[0] if '\n' in refined_task[:150] else refined_task[:150]

    queries = []

    # 策略1：每个调研方向生成一个子查询
    for direction in directions[:max_queries]:
        parts = [task_brief]
        parts.append(f"重点调研：{direction[:100]}")
        # 附加一个相关的核心追问（如果有）
        if core_questions:
            parts.append(f"需回答：{core_questions[0][:60]}")
        query = "。".join(parts)
        queries.append(query[:400])

    # 策略2：方向不足时，用核心追问补充
    if len(queries) < max_queries and core_questions:
        for q in core_questions:
            if len(queries) >= max_queries:
                break
            parts = [task_brief]
            parts.append(f"重点调研：{q[:100]}")
            query = "。".join(parts)
            queries.append(query[:400])

    # 兜底：至少返回一条原始 enriched_query
    if not queries:
        queries.append(_build_enriched_query(refined_task, plan_text))

    return queries


# GPTR 服务熔断（进程级，按 base_url 共享）
# 服务不可达时在冷却期内直接走 DeepSeek 回退，避免每次调研都先等连接失败。
_GPTR_COOLDOWN_SECONDS = 600
_GPTR_HEALTHCHECK_TIMEOUT = 1.5
_GPTR_UNAVAILABLE_UNTIL: Dict[str, float] = {}


class _GPTRServiceUnavailable(Exception):
    """GPTR 服务熔断打开（内部信号，不对外暴露）"""


def _repair_mojibake(text: str) -> str:
    """修复 GPTR 抓取内容常见的 UTF-8→Latin-1 乱码。"""
    if not text:
        return text
    try:
        fixed = text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text
    cjk = lambda s: len(re.findall(r'[\u4e00-\u9fa5]', s))
    return fixed if cjk(fixed) > cjk(text) else text


class GPTRResearcherAdapter:
    def __init__(self, cfg: dict, llm: LLMClient, gptr_retriever: str = "deepseek"):
        self.cfg = cfg
        self.llm = llm
        self.gptr_retriever = gptr_retriever  # 动态 retriever
        self.last_call_failed: bool = False  # GPTR+DeepSeek均失败标志
        # GPT-Researcher Service 地址（微服务化）
        self._gptr_base_url = cfg.get("gptr_service_url", "http://127.0.0.1:8000/api/v1")
        self._gptr_client = None
        self._service_proc = None
        self._auto_start_attempted = False

    def _get_gptr_client(self):
        """延迟导入 HTTP 客户端"""
        if self._gptr_client is None:
            import sys
            gptr_path = self.cfg.get("gptr_service_path", "")
            if gptr_path and gptr_path not in sys.path:
                sys.path.insert(0, gptr_path)
            from gptr_service.client import GPTResearcherClient
            self._gptr_client = GPTResearcherClient(base_url=self._gptr_base_url)
        return self._gptr_client

    def _circuit_open(self) -> bool:
        """熔断是否处于冷却期"""
        return time.monotonic() < _GPTR_UNAVAILABLE_UNTIL.get(self._gptr_base_url, 0.0)

    def _trip_circuit(self, reason: str = "服务不可用") -> None:
        """打开熔断；同一冷却窗口内只记录一条 WARNING"""
        if self._circuit_open():
            return
        _GPTR_UNAVAILABLE_UNTIL[self._gptr_base_url] = (
            time.monotonic() + _GPTR_COOLDOWN_SECONDS
        )
        logger.warning(
            f"[GPTR Service] {reason}（{self._gptr_base_url}），已熔断，"
            f"{_GPTR_COOLDOWN_SECONDS // 60} 分钟内不再尝试"
        )

    async def _gptr_service_reachable(self) -> bool:
        """TCP 健康检查：短超时内能建立连接即视为可用"""
        from urllib.parse import urlparse
        parsed = urlparse(self._gptr_base_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        writer = None
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=_GPTR_HEALTHCHECK_TIMEOUT
            )
            return True
        except Exception:
            return False
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass

    def _start_local_service(self) -> bool:
        """服务未运行时，尝试在后台启动本地 GPTR 服务。"""
        # C1: 仅本机地址才允许自动拉起本地服务；远程地址不得标记为"已尝试启动"
        from urllib.parse import urlparse
        host = (urlparse(self._gptr_base_url).hostname or "").lower()
        if host not in ("127.0.0.1", "localhost", "::1"):
            logger.debug(
                f"[GPTR Service] 远程 GPTR 不自动启动本地服务: {self._gptr_base_url}"
            )
            return False
        if self._auto_start_attempted:
            return False
        self._auto_start_attempted = True
        if not self.cfg.get("gptr_auto_start", True):
            return False

        service_dir = self.cfg.get("gptr_service_path", "") or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "gptr_service"
        )
        main_py = os.path.join(service_dir, "main.py")
        if not os.path.isfile(main_py):
            logger.debug(f"[GPTR Service] 未找到本地服务入口: {main_py}")
            return False

        import subprocess
        import sys
        try:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self._service_proc = subprocess.Popen(
                [sys.executable, "main.py"],
                cwd=service_dir,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
            logger.info(f"[GPTR Service] 本地服务未运行，已在后台启动: {service_dir}")
            atexit.register(self._stop_local_service)
            return True
        except Exception as e:
            logger.warning(f"[GPTR Service] 自动启动失败: {e}")
            return False

    def _stop_local_service(self):
        proc = self._service_proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                pass
        self._service_proc = None

    async def _ensure_gptr_available(self) -> bool:
        """统一闸门：冷却期内直接拒绝；否则做一次短超时健康检查"""
        if self._circuit_open():
            return False
        if await self._gptr_service_reachable():
            _GPTR_UNAVAILABLE_UNTIL.pop(self._gptr_base_url, None)
            return True

        # 服务没起来时尝试自动拉起一次，再给启动留出时间
        if self._start_local_service():
            timeout = max(5, int(self.cfg.get("gptr_start_timeout", 20)))
            for _ in range(timeout * 2):
                await asyncio.sleep(0.5)
                if await self._gptr_service_reachable():
                    logger.info("[GPTR Service] 本地服务已就绪")
                    _GPTR_UNAVAILABLE_UNTIL.pop(self._gptr_base_url, None)
                    return True

        self._trip_circuit()
        return False

    async def _research_round(self, query: str) -> str:
        """通过 GPT-Researcher Service 执行研究（HTTP调用）。
        配置直接透传cfg，避免重复组装。"""
        try:
            try:
                client = self._get_gptr_client()
            except Exception as e:
                # C8: 客户端不可导入时同样打开熔断，避免每次调研重复失败
                self._trip_circuit(reason=f"客户端不可用: {e}")
                logger.warning(
                    f"[GPTR Service] 客户端获取失败（{e}），已熔断并回退 DeepSeek Search"
                )
                raise
            # 直接透传cfg中的GPTR相关配置，避免重复组装
            gptr_config = {
                "smart_model": self.cfg.get("plan_model", "deepseek-v4-flash"),
                "fast_model": self.cfg.get("write_model", "deepseek-v4-flash"),
                "llm_api_key": self.cfg.get("llm_api_key", ""),
                "llm_base_url": self.cfg.get("llm_base_url", ""),
                "language": self.cfg.get("language", "chinese"),
                "max_search_results": self.cfg.get("max_search_results", 10),
                "search_provider": self.gptr_retriever,  # 动态 retriever
            }

            report = await client.submit(
                query=query[:400],
                report_type=self.cfg.get("report_type", "research_report"),
                tone=self.cfg.get("report_tone", "Objective"),
                config=gptr_config,
                timeout=int(self.cfg.get("gptr_task_timeout", 600)),
            )
            return _repair_mojibake(report)
        except Exception as e:
            logger.error(f"[GPTR Service] 调用失败: {e}")
            raise

    async def conduct_research(self, plan_text: str, refined_task: str) -> str:
        # 每次检索入口复位失败标志，避免上一次失败污染本次结果
        self.last_call_failed = False
        # 构建富含规划信息的查询
        enriched_query = _build_enriched_query(refined_task, plan_text)
        logger.info(f"[GPT Researcher] enriched_query ({len(enriched_query)}字): {enriched_query[:200]}...")

        try:
            # 熔断闸门——服务不可达时直接走回退，不再尝试连接
            if not await self._ensure_gptr_available():
                raise _GPTRServiceUnavailable()

            # 通过 GPT-Researcher Service 执行研究（HTTP调用）
            report = await self._research_round(enriched_query)
            if not report or len(report.strip()) < 200:
                raise RuntimeError(f"GPTR 返回空上下文（{len((report or '').strip())} 字符）")

            # 域名黑名单过滤 —— 标记被屏蔽的源
            filtered_lines = []
            blocked_count = 0
            for line in report.split('\n'):
                is_blocked = False
                for domain in GPTR_DOMAIN_BLACKLIST:
                    if domain in line.lower():
                        is_blocked = True
                        blocked_count += 1
                        break
                if not is_blocked:
                    filtered_lines.append(line)
            if blocked_count > 0:
                logger.info(f"[GPTR黑名单] 过滤 {blocked_count} 条来自文档分享平台的低质量内容")
            return '\n'.join(filtered_lines)

        except _GPTRServiceUnavailable:
            pass  # 熔断已在 _trip_circuit 记录唯一一条 WARNING
        except asyncio.TimeoutError:
            logger.warning("[GPT Researcher] 搜索超时")
        except Exception as e:
            logger.warning(f"[GPT Researcher] 调用失败: {e}")

        if not self.cfg.get("gptr_fallback_to_deepseek", False):
            self.last_call_failed = True
            logger.warning(
                "[搜索失败] GPTR 不可用，且未启用 DeepSeek 自动补搜"
                "（gptr_fallback_to_deepseek=False），按搜索失败处理"
            )
            return ""

        logger.info("[回退] 使用 DeepSeek Search 替代 GPT Researcher...")
        keywords = " ".join(re.findall(r'[\u4e00-\u9fa5]{2,6}|[a-zA-Z]{2,20}|20\d{2}', refined_task)[:12])
        queries = [
            f"{keywords} 市场规模 {datetime.now().year}",
            f"{keywords} 竞争格局",
            f"{keywords} 发展趋势",
        ]
        all_res = []
        adapter = DeepSeekAdapter(
            self.cfg.get("search_api_key", "") or self.cfg.get("llm_api_key", ""),
            self.cfg.get("search_api_endpoint", ""),
        )
        for q in queries:
            try:
                all_res.extend(adapter.search(q, 3))
            except Exception as e:
                logger.debug(f"[DeepSeek回退搜索失败] 查询'{q}': {e}")

        # 如果 GPTR 和 DeepSeek 回退都失败，标记搜索彻底失败
        if not all_res:
            self.last_call_failed = True
            logger.warning("[搜索失败] GPTR连接失败且 DeepSeek 回退也失败，标记搜索彻底失败")

        return "\n---\n".join([
            f"来源: {r.title} ({r.url})\n{r.content}"
            for r in all_res
        ])

    async def conduct_research_parallel(self, plan_text: str, refined_task: str,
                                         max_queries: int = 3) -> str:
        """并行GPTR搜索——基于Plan方向拆分子查询，并行提交，合并结果。

        与 conduct_research() 的区别：
        - conduct_research: 1个大查询 → 1次GPTR调用（串行）
        - conduct_research_parallel: N个子查询 → N次GPTR调用（并行）

        成本控制：每个子查询的搜索量与原来相当（GPTR内部仍做sub-query拆分），
        总信息量增加但单次搜索更聚焦，整体成本基本不变。

        Args:
            plan_text: Plan文本
            refined_task: 精炼后的调研任务
            max_queries: 最大并行子查询数（默认3）

        Returns:
            合并后的搜索结果文本
        """
        # 与服务不可用熔断共享同一闸门，避免逐个提交子查询再失败
        if not await self._ensure_gptr_available():
            return await self.conduct_research(plan_text, refined_task)

        sub_queries = _build_parallel_queries(refined_task, plan_text, max_queries)

        # 如果只拆出1条查询，退化为普通串行调用
        if len(sub_queries) <= 1:
            return await self.conduct_research(plan_text, refined_task)

        logger.info(f"[GPTR并行] 拆分为 {len(sub_queries)} 个子查询，使用异步提交模式")
        for i, q in enumerate(sub_queries):
            logger.info(f"[GPTR并行] 子查询{i+1}: {q[:120]}...")

        try:
            client = self._get_gptr_client()
        except Exception as e:
            # C8: 客户端不可导入时打开熔断，并直接走串行路径的 DeepSeek 回退
            self._trip_circuit(reason=f"客户端不可用: {e}")
            logger.warning(
                f"[GPTR Service] 客户端获取失败（{e}），已熔断，并行路径回退串行/DeepSeek"
            )
            return await self.conduct_research(plan_text, refined_task)

        # 阶段1: 异步提交所有任务（只提交不等待）
        task_timeout = int(self.cfg.get("gptr_task_timeout", 600))
        task_ids = []
        for q in sub_queries:
            try:
                tid = await client.submit_async(
                    query=q[:400],
                    report_type=self.cfg.get("report_type", "research_report"),
                    tone=self.cfg.get("report_tone", "Objective"),
                    config={
                        "smart_model": self.cfg.get("plan_model", "deepseek-v4-flash"),
                        "fast_model": self.cfg.get("write_model", "deepseek-v4-flash"),
                        "llm_api_key": self.cfg.get("llm_api_key", ""),
                        "llm_base_url": self.cfg.get("llm_base_url", ""),
                        "language": self.cfg.get("language", "chinese"),
                        "max_search_results": self.cfg.get("max_search_results", 10),
                        "search_provider": self.gptr_retriever,  # 动态 retriever
                    },
                    timeout=task_timeout,
                )
                task_ids.append(tid)
                logger.info(f"[GPTR并行] 子查询已提交: {tid}")
            except Exception as e:
                logger.warning(f"[GPTR并行] 提交失败（已跳过）: {e}")

        if not task_ids:
            logger.warning("[GPTR并行] 所有子查询提交均失败，降级到DeepSeek搜索")
            return await self.conduct_research(plan_text, refined_task)

        # 阶段2: 统一轮询等待所有任务完成（单轮询循环，避免多个submit()的HTTP风暴）
        poll_interval = 5
        max_wait = task_timeout + 60  # 客户端等待需长于子任务超时，避免提前放弃
        elapsed = 0
        results_map = {}  # task_id -> result

        while elapsed < max_wait and len(results_map) < len(task_ids):
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            for tid in task_ids:
                if tid in results_map:
                    continue
                try:
                    status = await client.get_status(tid)
                    if status.get("status") == "completed":
                        results_map[tid] = _repair_mojibake(status.get("result", ""))
                        logger.info(f"[GPTR并行] 子查询完成: {tid} ({elapsed}s)")
                    elif status.get("status") in ("failed", "timeout", "cancelled"):
                        error = status.get("error", "未知错误")
                        logger.warning(f"[GPTR并行] 子查询失败: {tid}, 原因: {error}")
                        results_map[tid] = None  # 标记为失败
                except Exception as e:
                    logger.debug(f"[GPTR并行] 轮询异常: {tid}: {e}")

        # 收集成功结果
        valid_results = [r for r in results_map.values() if r and len(r.strip()) > 50]
        if not valid_results:
            logger.warning("[GPTR并行] 所有子查询均失败，降级到DeepSeek搜索")
            return await self.conduct_research(plan_text, refined_task)

        # 域名黑名单过滤（对合并后的整体结果做一次）
        all_text = "\n\n".join(valid_results)
        filtered_lines = []
        blocked_count = 0
        for line in all_text.split('\n'):
            is_blocked = False
            for domain in GPTR_DOMAIN_BLACKLIST:
                if domain in line.lower():
                    is_blocked = True
                    blocked_count += 1
                    break
            if not is_blocked:
                filtered_lines.append(line)
        if blocked_count > 0:
            logger.info(f"[GPTR黑名单] 过滤 {blocked_count} 条来自文档分享平台的低质量内容")

        merged = '\n'.join(filtered_lines)
        logger.info(f"[GPTR并行] 合并完成: {len(valid_results)}/{len(task_ids)} 个子查询成功, "
                     f"总耗时 {elapsed}s, 总长度 {len(merged)} 字")
        return merged


def parse_research_to_materials(raw_report: str, llm: LLMClient, plan_model: str,
                                 plan_directions: Optional[List[str]] = None) -> List[Dict]:
    """解析GPT Researcher报告时，利用Plan的调研方向标注素材维度。"""
    directions_hint = ""
    if plan_directions:
        dir_list = "\n".join([f"{i+1}. {d}" for i, d in enumerate(plan_directions[:5])])
        directions_hint = f"""
【调研方向参考】请将每条素材的tags字段标注为对应的方向编号（如"方向1"、"方向2"）：
{dir_list}
"""
    def _parse_chunk(chunk: str) -> List[Dict]:
        prompt = f"""【任务】将研究报告拆解为结构化素材条目
【原则】每条素材只包含一个核心事实、数据或案例
【跳过】纯导航/菜单/页脚、报告列表页、空白模板、乱码页面直接跳过，不要编造素材
【软文处理】企业官网新闻、招商加盟页、品牌宣传稿不要整篇丢弃：
只提取可核验的事实（主体、动作、数量、时间、价格与政策条款、合作方、门店/城市分布、产品型号），
忽略“万亿蓝海、颠覆行业、独家IP、初心愿景、赋能升级”等评价性话术
【来源判定】claim_nature 只能是以下三种之一：
企业自述 —— 企业官网、招商页、品牌新闻稿、加盟广告
第三方数据 —— 研究机构报告、政府部门、媒体调查、财报、学术论文
未知 —— 无法判断
{directions_hint}

【报告内容】{chunk}

【输出要求】
- 只输出纯JSON数组，不要解释文字
- 每条素材包含完整字段
- 标准格式：
[
  {{
    "id":"MAT-001",
    "title":"素材标题（概括核心内容）",
    "url":"来源URL",
    "content":"核心事实/数据/案例（100-200字）",
    "tags":["方向1","方向2"],
    "source_type":"report/news/official/promo",
    "claim_nature":"企业自述/第三方数据/未知"
  }}
]"""
        for attempt in range(2):
            try:
                raw = llm.plan([{"role": "user", "content": prompt}], max_tokens=6000)
                mats = safe_json_loads(raw, [])
                if isinstance(mats, list) and mats:
                    return mats
                logger.warning(
                    f"[素材解析] 第{attempt+1}次未得到素材数组（LLM 返回 {len(raw or '')} 字）: {(raw or '')[:120]!r}"
                )
            except (json.JSONDecodeError, TypeError, KeyError, AttributeError) as e:
                logger.debug(f"[素材解析失败] {e}")
                time.sleep(1)
        return []

    # 分片解析：单片过大时 LLM 输出会被 max_tokens 截断，导致 JSON 解析失败
    chunk_size = 5000
    all_mats: List[Dict] = []
    for start in range(0, min(len(raw_report), 15000), chunk_size):
        all_mats.extend(_parse_chunk(raw_report[start:start + chunk_size]))

    seen = set()
    cleaned = []
    for m in all_mats:
        if not isinstance(m, dict):
            continue
        key = m.get("title", "") + str(m.get("content", ""))[:50]
        if key in seen:
            continue
        seen.add(key)
        nature = str(m.get("claim_nature", "")).strip()
        m["claim_nature"] = nature if nature in CLAIM_NATURES else CLAIM_UNKNOWN
        cleaned.append(m)
    logger.info(f"[素材解析] 解析出 {len(cleaned)} 条素材")
    return cleaned[:30]


class SearchManager:
    def __init__(self, cfg: dict, llm: LLMClient, cost_callback=None, route: str = "deepseek_standard"):
        self.cfg = cfg
        self.llm = llm
        self.searched_queries: set = set()
        self.cost_callback = cost_callback  # 成本统计回调函数
        self._search_count = 0  # 搜索次数统计

        # 路由配置
        self.route = route
        self.route_config = RouteDispatcher.get_route(route)
        self.gptr_retriever = self.route_config["gptr_retriever"]

        # 初始化搜索缓存（统一使用shared版本，7天TTL）
        from shared.search.cache import SearchCache
        cache_dir = None
        output_dir = cfg.get("output_dir", "")
        if output_dir:
            cache_dir = os.path.join(output_dir, ".cache")
        self.cache = SearchCache(
            enabled=cfg.get("enable_search_cache", True),
            ttl_days=7,
            max_items=cfg.get("search_cache_max_items", 1000),
            cache_dir=cache_dir,
        )

        self.adapters: List[BaseSearchAdapter] = []
        self.search_failure: bool = False  # 搜索彻底失败标志（GPTR+DeepSeek均失败）

        # 全部路线统一使用 DeepSeek 搜索
        self.adapters.append(DeepSeekAdapter(
            cfg.get("search_api_key", "") or cfg.get("llm_api_key", ""),
            cfg.get("search_api_endpoint", ""),
        ))

        self.parallel_search = True

        self.scorer = SearchScorer(llm, cfg)
        self.enhancer = PlaywrightEnhancer(cfg.get("enable_browser_enhance", False), cfg)
        self.gptr = GPTRResearcherAdapter(cfg, llm, self.gptr_retriever)

    def _report_cost(self, num_calls: int = 1):
        """报告搜索成本"""
        self._search_count += num_calls
        if self.cost_callback:
            try:
                self.cost_callback(num_calls)
            except (TypeError, ValueError) as e:
                # 回调函数调用失败，记录日志但不中断搜索
                logger.warning(f"[成本回调失败] {e}")

    def should_skip(self, query: str) -> bool:
        return normalize_query(query) in self.searched_queries

    def mark_searched(self, query: str):
        self.searched_queries.add(normalize_query(query))

    def search(self, query: str, max_results: int = 5, score_results: bool = True,
               enhance_top: int = 0, aggressive: bool = False) -> List[SearchResult]:
        # Agent 安全管控检查
        if _SECURITY_AVAILABLE:
            try:
                guard = get_security_guard()
                allowed, reason, audit_info = guard.govern_search(query, agent_id="research")
                if not allowed:
                    logger.warning(f"[AgentSecurity] 搜索被阻止: {reason}")
                    return []
            except Exception as e:
                logger.debug(f"[AgentSecurity] 检查异常: {e}")

        if self.should_skip(query):
            logger.info(f"[去重跳过] {query}")
            return []

        # 超长查询多为任务原文碎片，缓存结果不可靠，直接走真实搜索
        cached_results = self.cache.get(query) if len(query) <= 50 else None
        if cached_results is not None:
            # 缓存命中，将dict转换回SearchResult
            results = [SearchResult.from_dict(r) for r in cached_results][:max_results]
            logger.info(f"[缓存命中] {query[:50]}... ({len(results)}条结果)")
            return results

        current_year = datetime.now().year
        if not re.search(r'20\d{2}', query):
            query = f"{current_year}年 {query}"

        self.mark_searched(query)
        # 报告搜索成本（每次search调用算作1次API调用）
        self._report_cost(1)
        all_results: List[SearchResult] = []

        # 并行搜索（free/all模式同时调用多源）
        if self.parallel_search and len(self.adapters) > 1:
            with ThreadPoolExecutor(max_workers=len(self.adapters)) as ex:
                futures = {ex.submit(adapter.search, query, max_results): adapter for adapter in self.adapters}
                for fut in as_completed(futures):
                    try:
                        res = fut.result()
                        if res:
                            all_results.extend(res)
                    except Exception as e:
                        logger.debug(f"[并行搜索错误] {e}")
        else:
            for adapter in self.adapters:
                try:
                    res = adapter.search(query, max_results)
                    if res:
                        all_results.extend(res)
                        break
                except Exception as e:
                    logger.warning(f"[搜索适配器错误] {e}")

        if not all_results:
            logger.info(f"[搜索无结果] {query}")
            return []

        seen = set()
        deduped = []
        for r in all_results:
            key = r.url + r.title
            if key and key not in seen:
                seen.add(key)
                deduped.append(r)

        if score_results and self.llm.available:
            deduped = self.scorer.score_batch(deduped, query)

        if enhance_top > 0 and self.enhancer.enabled:
            for r in deduped[:enhance_top]:
                if r.reliability == "high" and len(r.content) < 1000:
                    extra = self.enhancer.extract_article(r.url)
                    if extra:
                        r.raw_html = extra
                        r.content = smart_truncate(extra, 2500)

        if aggressive:
            top_k = self.cfg.get("aggressive_filter_top_k", 3)
            deduped = deduped[:top_k]

        deduped.sort(key=lambda x: x.llm_score, reverse=True)
        # 搜索预处理层
        deduped = self._preprocess_results(deduped)
        
        # 保存到缓存（将SearchResult序列化为dict）
        if deduped:
            cache_data = [r.to_dict() for r in deduped]
            self.cache.set(query, cache_data)

        # 按请求上限截断，避免适配器返回超过 max_results 条（缓存保留完整集）
        return deduped[:max_results]

    def multi_search(self, queries: List[str], max_results: int = 3,
                     workers: int = 5, aggressive: bool = False) -> List[SearchResult]:
        all_res: List[SearchResult] = []
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(self.search, q, max_results, True, 1, aggressive): q for q in queries}
            for fut in as_completed(futures):
                try:
                    res = fut.result()
                    if res:
                        # 标注每条结果的来源查询词，供溯源表使用
                        q = futures[fut]
                        for r in res:
                            r.query = q
                        all_res.extend(res)
                except Exception as e:
                    logger.warning(f"[并行搜索错误] {e}")

        seen = set()
        deduped = []
        for r in all_res:
            key = r.url + r.title
            if key and key not in seen:
                seen.add(key)
                deduped.append(r)
        deduped.sort(key=lambda x: x.llm_score, reverse=True)
        return deduped

    def execute_search_batches(self, batches: List[Dict], max_iterations: int = 1) -> List[SearchResult]:
        """分批×迭代搜索矩阵。每批执行max_iterations轮，每轮基于前一轮结果细化查询。
        TERMINATE信息饱和度检测——新增素材信息增量低于阈值时提前停止。"""
        max_iterations = int(max_iterations)  # 防护：确保整数类型
        all_results: List[SearchResult] = []
        for batch_idx, batch in enumerate(batches):
            queries = batch.get("queries", [])
            max_results = batch.get("max_results", 5)
            desc = batch.get("description", f"批次{batch_idx+1}")

            batch_results: List[SearchResult] = []
            prev_result_count = 0
            low_gain_streak = 0  # 连续低增量计数

            for iteration in range(max_iterations):
                if iteration == 0:
                    current_queries = queries
                else:
                    # 第2+轮迭代——基于前一轮结果，LLM生成细化查询
                    if not batch_results:
                        break
                    current_queries = self._generate_refined_queries(
                        queries[0] if queries else "",
                        batch_results,
                        iteration + 1
                    )
                    if not current_queries:
                        break

                iter_label = f"迭代{iteration+1}/{max_iterations}" if max_iterations > 1 else ""
                logger.info(f"[搜索批次] {desc}{iter_label}: {len(current_queries)}个查询")
                results = self.multi_search(current_queries, max_results=max_results, workers=5, aggressive=False)
                if results:
                    batch_results.extend(results)
                time.sleep(1)

                # TERMINATE信息饱和度检测
                if self.cfg.get("enable_terminate_saturation", False) and iteration > 0:
                    new_count = len(batch_results) - prev_result_count
                    if prev_result_count > 0 and new_count > 0:
                        gain_ratio = new_count / prev_result_count
                    else:
                        gain_ratio = 0.0
                    saturation_threshold = self.cfg.get("terminate_gain_threshold", 0.05)
                    if gain_ratio < saturation_threshold:
                        low_gain_streak += 1
                        logger.info(f"[TERMINATE] {desc} 迭代{iteration+1}: 新增{new_count}条(增量{gain_ratio:.1%}<{saturation_threshold:.0%})，连续低增量{low_gain_streak}/2")
                        if low_gain_streak >= 2:
                            logger.info(f"[TERMINATE] {desc} 信息饱和，提前停止(原计划{max_iterations}轮，实际{iteration+1}轮)")
                            break
                    else:
                        low_gain_streak = 0
                prev_result_count = len(batch_results)

            if batch_results:
                all_results.extend(batch_results)
        return all_results

    def _generate_refined_queries(self, original_query: str, prev_results: List[SearchResult], iteration: int) -> List[str]:
        """基于前一轮搜索结果，让LLM生成细化查询。"""
        if not self.llm.available or not prev_results:
            return []
        results_summary = "\n".join([
            f"- {r.title}: {r.content[:200]}"
            for r in prev_results[:8]
        ])
        prompt = f"""【任务】基于前一轮搜索结果生成细化查询
【迭代轮次】第{iteration-1}轮
【原始查询】{original_query}
【搜索结果摘要】{results_summary}

【生成规则】
1. 生成3-5个细化搜索查询
2. 覆盖首次搜索中未充分展开的角度
3. 查询必须具体，避免泛泛而谈
4. 每个查询针对一个具体的信息缺口

【输出要求】
- 只输出纯JSON数组，不要解释文字
- 标准格式：
[{{"query":"具体的细化搜索查询"}}]"""
        try:
            raw = self.llm.fast(
                [{"role":"user","content":prompt}],
                system=AGENT_PROMPTS["query_generator"],
                max_tokens=400, json_mode=True
            )
            data = safe_json_loads(raw, [])
            if isinstance(data, dict):
                data = [data]
            queries = []
            for item in data:
                q = item.get("query", "")
                if q and not self.should_skip(q):
                    queries.append(q)
            return list(dict.fromkeys(queries))[:5]
        except Exception as e:
            logger.warning(f"[细化查询生成失败] {e}")
            return []

    def search_density(self, gaps: List[Dict], top_k: int = 10) -> List[SearchResult]:
        if not gaps or not self.llm.available:
            return []
        prompt = f"""【角色】搜索策略专家
【任务】针对知识缺口生成多维度搜索查询

【缺口列表】{json.dumps(gaps[:5], ensure_ascii=False)}

【生成要求——必须覆盖三个维度】
1. 时间维度：包含{datetime.now().year - 1}/{datetime.now().year}/历史趋势的查询
2. 主体维度：覆盖市场规模/竞争格局/政策法规/风险挑战
3. 反向维度：包含失败案例/争议/瓶颈的查询

【输出要求】
- 至少生成15个具体查询
- 只输出纯JSON数组，不要解释文字
- 标准格式：
[{{"query":"具体搜索查询","angle":"时间/主体/反向","expected_info":"预期获取的信息类型"}}]"""
        try:
            raw = self.llm.plan([{"role": "user", "content": prompt}], max_tokens=2000, json_mode=True)
            candidates = safe_json_loads(raw, [])
            if isinstance(candidates, dict):
                candidates = [candidates]
        except Exception as e:
            logger.warning(f"[搜索密度生成失败] {e}")
            return []

        unique_queries = []
        for c in candidates:
            q = c.get("query", "")
            if q and not self.should_skip(q) and q not in unique_queries:
                unique_queries.append(q)

        if not unique_queries:
            return []

        scored_queries = []
        for q in unique_queries[:25]:
            score_prompt = f"""【任务】评估搜索查询的预期信息价值
【查询】{q}
【调研主题】{self.cfg.get('report_type', 'research_report')}

【评分标准】1-10分
- 10分：非常具体，预期能获得高价值数据
- 5分：一般具体，预期获得一般性信息
- 1分：过于宽泛，预期信息价值低

【输出要求】只输出数字，不要任何其他文字"""
            try:
                score_raw = self.llm.fast(
                    [{"role": "user", "content": score_prompt}],
                    system=AGENT_PROMPTS["evaluator"],
                    max_tokens=10
                )
                score = float(re.search(r'\d+', score_raw).group()) if re.search(r'\d+', score_raw) else 5
            except (ValueError, TypeError, AttributeError) as e:
                # LLM返回格式异常，使用默认分数
                logger.debug(f"[评分解析失败] {e}")
                score = 5
            scored_queries.append((q, score))

        scored_queries.sort(key=lambda x: x[1], reverse=True)
        top_queries = [q for q, s in scored_queries[:top_k]]
        logger.info(f"[搜索密度] 生成{len(unique_queries)}个候选，执行Top{len(top_queries)}")
        return self.multi_search(top_queries, max_results=3, workers=5, aggressive=True)

    def storm_debate_queries(self, topic: str) -> List[str]:
        if not self.cfg.get("enable_storm_debate", False) or not self.llm.available:
            return []
        prompt = f"""【任务】模拟三方辩论生成多角度搜索查询
【调研主题】"{topic}"

【三方视角】
【支持者角度】：这个主题有哪些积极因素、机会、成功案例？
【质疑者角度】：这个主题有哪些风险、挑战、失败案例、争议点？
【旁观者角度】：有哪些被忽视的数据或视角？

【输出要求】
1. 生成6-8个搜索查询
2. 确保覆盖正反两面和盲区
3. 只输出纯JSON数组，不要解释文字
4. 标准格式：
[{{"query":"具体搜索查询","perspective":"支持者/质疑者/旁观者"}}]"""
        try:
            raw = self.llm.plan([{"role": "user", "content": prompt}], max_tokens=1200, json_mode=True)
            data = safe_json_loads(raw, [])
            if isinstance(data, dict):
                data = [data]
            queries = []
            for item in data:
                q = item.get("query", "")
                if q and not self.should_skip(q):
                    queries.append(q)
            return list(dict.fromkeys(queries))[:8]
        except Exception as e:
            logger.warning(f"[STORM辩论查询失败] {e}")
            return []

    async def deep_research(self, plan_text: str, refined_task: str) -> str:
        """GPTR搜索入口。

        并行模式因Ollama embedding单点瓶颈暂不可用，默认串行。
        上服务器后（无Ollama依赖或embedding服务独立部署）可重新启用：
        设置 enable_gptr_parallel=True, gptr_parallel_queries=3
        """
        # 检索入口复位失败标志，成功后不会残留上一次的 True
        self.gptr.last_call_failed = False
        if self.cfg.get("enable_gptr_parallel", False):
            max_queries = self.cfg.get("gptr_parallel_queries", 3)
            report = await self.gptr.conduct_research_parallel(
                plan_text, refined_task, max_queries=max_queries
            )
        else:
            report = await self.gptr.conduct_research(plan_text, refined_task)

        # 按本次调用结果复位失败标志，避免上一次的 True 残留
        self.search_failure = bool(self.gptr.last_call_failed)
        return report

    def _preprocess_results(self, results: List[SearchResult]) -> List[SearchResult]:
        """搜索预处理层（豆包方案4简化版）。
        去重+质量排序+结构化前置。"""
        if not results:
            return []

        # 1. 语义去重（基于标题+内容前200字的Jaccard相似度）
        def jaccard_sim(a: str, b: str) -> float:
            set_a = set(a)
            set_b = set(b)
            if not set_a or not set_b:
                return 0.0
            return len(set_a & set_b) / len(set_a | set_b)

        deduped = []
        for r in results:
            r_text = (r.title + r.content[:200]).lower().replace(' ', '')
            is_dup = False
            for d in deduped:
                d_text = (d.title + d.content[:200]).lower().replace(' ', '')
                if jaccard_sim(r_text, d_text) > 0.85:
                    is_dup = True
                    break
            if not is_dup:
                deduped.append(r)

        # 2. 质量排序：llm_score + tier权重 + confidence
        tier_weight = {"S": 4, "A": 3, "B": 2, "C": 1}
        reliability_order = {"high": 3, "medium": 2, "low": 1}
        deduped.sort(key=lambda x: (
            x.llm_score + x.confidence * 3 + tier_weight.get(x.tier, 2),
            reliability_order.get(x.reliability, 1),
            len(x.content)
        ), reverse=True)

        # 3. 域名分级处理（priority=0 标记但不丢弃）
        from config import get_merged_domain_priority
        priority_map = get_merged_domain_priority()
        filtered = []
        for r in deduped:
            r_domain = ""
            if r.url:
                from urllib.parse import urlparse
                parsed = urlparse(r.url)
                r_domain = parsed.netloc.replace("www.", "")
            priority = 5  # 默认
            for key, pri in priority_map.items():
                if key in r_domain:
                    priority = pri
                    break
            if priority == 0:
                # 文档平台：标记为"需手动上传"，保留在结果中
                r.source_type = "doc_platform"
                r.content = f"[文档平台] {r.title}\n来源: {r.url}\n提示: 该文档需要手动上传后解析全文"
                filtered.append(r)
            else:
                filtered.append(r)

        removed = len(results) - len(filtered)
        if removed > 0:
            logger.info(f"[预处理] 去重{len(results)-len(deduped)}条 → 保留{len(filtered)}条")
        return filtered

    def close(self):
        self.enhancer.close()
