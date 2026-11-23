#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小醉桌面研究 v1.0 / engine.py
核心引擎：规划注入搜索 + 向量覆盖度评估 + 分批×迭代搜索矩阵 +关键词素材匹配 + TTD-DR逐章去噪 + DSP上下文规划 + 执行经理终审
"""

import os, sys, re, json, time, asyncio, logging, threading, inspect
from typing import List, Dict, Optional, Tuple, Set
from dataclasses import dataclass, field
from datetime import datetime
from v16_1_utils import smart_truncate

# 诚实系统 —— 临时添加shared路径，导入后立即移除，避免search模块冲突
_shared_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'shared')
if _shared_path not in sys.path:
    sys.path.insert(0, _shared_path)
from shared.deduction_engine import DeductionEngine
# Claim Ledger — 声明账本，来源追溯
try:
    from shared.claim_ledger import ClaimLedger, QualityGate, EventLog, extract_claims_from_llm
    _HAS_CLAIM_LEDGER = True
except ImportError:
    _HAS_CLAIM_LEDGER = False
    logger_import = logging.getLogger("xiaozui")
    logger_import.warning("claim_ledger模块未找到，声明账本功能不可用")
if _shared_path in sys.path:
    sys.path.remove(_shared_path)

from v15_3_config import SOURCE_CREDIBILITY, CHAPTER_BUDGET
from config import (
    LLMClient, SearchResult, normalize_query, logger,
    current_year_anchor, safe_json_loads, extract_json, AGENT_PROMPTS,
    get_page_config, SEARCH_DEPTH_MAP,
)
from search import SearchManager, parse_research_to_materials, _extract_plan_key_info
from memory import HybridMemoryStore
from material_synthesizer import (
    MaterialSynthesizer, MaterialClusterer, MaterialCluster, SynthesisReport,
    create_clusters_from_materials,
)
from framework_loader import (
    ResearchFramework, load_framework_from_text,
)
from task_state import TaskStateStore, StageFailed, stage_label

# 搜索路线调度
from shared.billing import RouteDispatcher
from shared.search.models import CLAIM_SELF_PUBLISHED

# ============================================================
# 全局上下文管理
# ============================================================

class GlobalContext:
    """跨章节上下文——防止前后矛盾、重复啰嗦

    支持注入主题簇信息，让后续章节扩写时能感知其他主题簇的关键信息。
    """
    def __init__(self):
        self.core_facts = []
        self.key_conclusions = []
        self.cluster_briefing: str = ""  # 主题簇简报

    def set_cluster_briefing(self, briefing: str):
        """注入主题簇简报"""
        self.cluster_briefing = briefing

    def update_from_chapter(self, chapter_content, chapter_title, llm):
        """从章节中提取核心事实和结论（分块提取，解决长章节截断问题）"""
        if not llm or not llm.available:
            return
        max_chunk = 3000
        all_facts = []
        all_conclusions = []
        # 分块提取，每块3000字
        for i in range(0, len(chapter_content), max_chunk):
            chunk = chapter_content[i:i + max_chunk]
            prompt = f"""【任务】从章节内容中提取核心事实和关键结论
【章节标题】{chapter_title}
【章节内容】{chunk}

【提取规则】
1. 核心事实：3-5个可验证的客观陈述，必须包含具体数据
2. 关键结论：2-3个基于事实的推导，必须与本章主题直接相关
3. 只提取实质性内容，不提取套话和过渡句

【输出要求】
- 只输出纯JSON对象，不要解释文字
- 确保JSON语法正确
- 标准格式：
{{"core_facts":["事实1（含数据）","事实2（含数据）","事实3（含数据）"],"key_conclusions":["结论1","结论2"]}}"""
            try:
                raw = llm.fast([{"role":"user","content":prompt}], max_tokens=300, json_mode=True)
                data = safe_json_loads(raw, {"core_facts":[],"key_conclusions":[]})
                if isinstance(data, dict):
                    all_facts.extend(data.get("core_facts",[]))
                    all_conclusions.extend(data.get("key_conclusions",[]))
            except Exception as e:
                logger.debug(f"[GlobalContext] 提取上下文失败: {e}")
        # 去重 + 截断窗口
        self.core_facts = list(dict.fromkeys(all_facts))[-10:]
        self.key_conclusions = list(dict.fromkeys(all_conclusions))[-5:]

    def get_context_prompt(self) -> str:
        """生成传递给下一章的上下文提示"""
        parts = []
        if self.cluster_briefing:
            # 注入主题簇信息（截断到 800 字，避免过长）
            parts.append(
                "【全篇主题簇概览（避免重复其他章节已覆盖的主题）】：\n"
                + self.cluster_briefing[:800] + "\n"
            )
        if self.core_facts:
            parts.append("【前文核心事实（不要重复）】：\n" + "\n".join(f"- {f}" for f in self.core_facts[-10:]))
        if self.key_conclusions:
            parts.append("【前文核心结论（保持一致）】：\n" + "\n".join(f"- {c}" for c in self.key_conclusions[-5:]))
        if not parts:
            return ""
        parts.append("\n要求：不重复前文已有事实和结论，数据和观点与前文保持一致。避免重复其他章节已覆盖的主题。\n")
        return "\n".join(parts)


@dataclass
class ResearchState:
    confirmed_facts: List[Dict] = field(default_factory=list)
    open_questions: List[str] = field(default_factory=list)
    searched_queries: Set[str] = field(default_factory=set)
    consumed_sources: Set[str] = field(default_factory=set)
    failed_queries: List[str] = field(default_factory=list)
    fruitless_topics: Set[str] = field(default_factory=set)
    verification_map: Dict[str, SearchResult] = field(default_factory=dict)
    search_budget: int = 20
    _budget_lock: threading.Lock = field(default_factory=threading.Lock)

    def consume_budget(self, amount: int = 1) -> bool:
        """原子操作：消耗search_budget，返回是否成功"""
        with self._budget_lock:
            if self.search_budget >= amount:
                self.search_budget -= amount
                return True
            return False

    def has_budget(self) -> bool:
        """原子操作：检查是否有剩余预算"""
        with self._budget_lock:
            return self.search_budget > 0

    def log(self, stage: str):
        logger.info(
            f"[状态-{stage}] 已确认:{len(self.confirmed_facts)} | "
            f"缺口:{len(self.open_questions)} | "
            f"已搜:{len(self.searched_queries)} | "
            f"预算剩余:{self.search_budget} | "
            f"无效主题:{len(self.fruitless_topics)}"
        )

    def should_skip_query(self, query: str) -> bool:
        norm = normalize_query(query)
        if norm in self.searched_queries:
            return True
        for ft in self.fruitless_topics:
            if ft in norm and len(ft) > 4:
                return True
        return False

    def mark_fruitless(self, query: str, results: List[SearchResult]):
        if not results:
            topics = re.findall(r"[\u4e00-\u9fa5]{4,}", query)
            for t in topics:
                self.fruitless_topics.add(t)
            logger.info(f"[止损] 查询无结果，标记主题: {topics}")

    def add_confirmed(self, result: SearchResult):
        self.confirmed_facts.append({
            "fact": result.content[:120],
            "source": result.title,
            "url": result.url,
            "score": result.llm_score,
            "reliability": result.reliability,
        })


class FusionResearchBot:
    def __init__(self, cfg: dict, gui=None, route: str = "deepseek_standard",
                 clarify_callback=None,  # 澄清自动回答回调
                 on_first_batch_done=None):  # 第一轮搜索完成回调
        self.cfg = cfg
        self.gui = gui
        self.clarify_callback = clarify_callback  # 调研翻译器的自动回答回调
        self.on_first_batch_done = on_first_batch_done  # 信源截取回调
        self.llm = LLMClient(cfg)

        # 用户确认过的策略（来自报告反馈对话框）
        self._user_strategy = self._load_user_strategy()

        # 路由配置
        self.route = route
        self.route_config = RouteDispatcher.get_route(route)

        # 创建成本统计回调函数
        cost_callback = gui.add_search_cost if gui and hasattr(gui, 'add_search_cost') else None

        # 搜索管理器注入路线
        self.searcher = SearchManager(cfg, self.llm, cost_callback=cost_callback, route=route)

        self._task_id = ""
        # 任务状态持久化：核心阶段失败可恢复，不静默降级
        self._state_store: Optional[TaskStateStore] = None
        self._material_assessment: Dict = {}
        self._stage_retries = int(cfg.get("stage_retries", 2))

        # 搜索成本追踪
        if self.cfg.get("search_cost_tracking", True):
            from v16_1_utils import SearchCostTracker
            self.cost_tracker = SearchCostTracker(self.cfg)
        else:
            self.cost_tracker = None

        self.memory = HybridMemoryStore(
            cfg.get("ollama_host", "http://localhost:11434"),
            cfg.get("ollama_model", "EntropyYue/jina-embeddings-v2-base-zh:latest"),
            llm=self.llm,
            cfg=cfg,
        )
        # 搜索深度模式 — 标准收束/搜到极致
        search_budget = self.route_config.get("search_budget", cfg.get("max_search_budget", 20))
        self.state = ResearchState(search_budget=search_budget)

        self.raw_task: str = ""
        self.refined_task: str = ""
        self.topic_keywords: List[str] = []  # 任务主题词，用于素材相关性过滤
        self.plan_text: str = ""
        self.inferable_context: str = ""  # 推演引擎可推理项，注入plan/search
        self.perspective_constraint: str = ""  # 推演引擎主导视角约束，注入plan
        self.clarify_context: str = ""  # 澄清阶段用户回答，注入plan/search避免跑偏
        self.plan_directions: List[str] = []
        self.plan_core_questions: List[str] = []
        self.plan_coverage: Dict = {}  # Plan方向覆盖度评估结果
        self.search_batches: List[Dict] = []
        self.outline: Dict = {}
        self.zero_draft: str = ""
        self.final_draft: str = ""
        self.chapters_md: List[str] = []
        self.all_materials: List[SearchResult] = []
        self.report_title: str = "调研报告"
        self.raw_report_path: str = ""
        self.zero_draft_issues: List[Dict] = []
        self.chapter_materials: Dict[int, List[int]] = {}
        self._supplement_triggered: bool = False
        self.global_context = GlobalContext()  # 

        # 素材分层注入 — 聚类器 + 综合器
        self._clusterer = MaterialClusterer(
            ollama_host=cfg.get("ollama_host", "http://localhost:11434"),
            ollama_model=cfg.get("ollama_model", "EntropyYue/jina-embeddings-v2-base-zh:latest"),
        )
        self._synthesizer = MaterialSynthesizer(llm=self.llm)
        self._cluster_reports: List[SynthesisReport] = []  # 缓存的聚类综合报告

        # Claim Ledger — 声明账本 + 事件日志
        self.claim_ledger = None  # 延迟初始化，run()中创建
        self.event_log = None
        self._quality_gate = None  # QualityGate 实例
        # 记录搜索结果注册前的素材数量，用于增量注册
        self._pre_search_material_count = 0

    def _register_search_claims(self):
        """将搜索结果的关键数据点注册到声明账本

        遍历增量素材（自上次注册以来的新增部分），提取标题、来源URL、发布时间，
        注册为 claim。来源标记为搜索结果 URL。
        """
        if not _HAS_CLAIM_LEDGER or not self.claim_ledger:
            return

        from shared.claim_ledger import ClaimType

        # 增量处理：只处理上次注册后新增的素材
        start_idx = self._pre_search_material_count
        new_materials = self.all_materials[start_idx:]

        registered = 0
        for i, m in enumerate(new_materials):
            # 获取素材字段（兼容 SearchResult 对象和 dict）
            title = getattr(m, 'title', '') or (m.get('title', '') if isinstance(m, dict) else '')
            url = getattr(m, 'url', '') or (m.get('url', '') if isinstance(m, dict) else '')
            published_date = getattr(m, 'published_date', '') or (m.get('published_date', '') if isinstance(m, dict) else '')

            if not title:
                continue

            grade = getattr(m, 'tier', '') or getattr(m, 'grade', '')
            if hasattr(grade, 'value'):
                grade = grade.value
            source_meta = {
                "source_grade": str(grade or ""),
                "claim_nature": str(getattr(m, 'claim_nature', '') or ""),
                "source_group": str(getattr(m, 'source_group', '') or ""),
            }

            # 注册标题声明（来源为搜索结果URL）
            source_label = url if url else "搜索引擎结果"
            self.claim_ledger.register_claim(
                field_name=f"搜索结果标题_{start_idx + i}",
                value=title,
                claim_type=ClaimType.FACT,
                source=source_label,
                source_id=url,
                source_date=published_date,
                writer="search_engine",
                meta=dict(source_meta),
            )

            # 如果有发布时间，也注册
            if published_date:
                self.claim_ledger.register_claim(
                    field_name=f"发布时间_{start_idx + i}",
                    value=published_date,
                    claim_type=ClaimType.FACT,
                    source=source_label,
                    source_id=url,
                    writer="search_engine",
                    meta=dict(source_meta),
                )

            registered += 1

        # 更新计数器
        self._pre_search_material_count = len(self.all_materials)

        if registered > 0 and self.event_log:
            self.event_log.log(
                "claims_registered",
                "search",
                details=f"注册 {registered} 条搜索结果声明",
            )
            logger.info(f"[ClaimLedger] 搜索结果注册 {registered} 条声明（总计 {len(self.all_materials)} 条素材）")

    def _extract_report_claims(self):
        """用 extract_claims_from_llm 从报告各章中提取声明

        LLM 基于搜索结果生成的洞察，来源标记为 INFERRED。
        """
        if not _HAS_CLAIM_LEDGER or not self.claim_ledger:
            return

        from shared.claim_ledger import extract_claims_from_llm, ClaimStatus

        total_extracted = 0
        skipped_ai = 0
        for i, chapter in enumerate(self.chapters_md):
            # AI 训练知识章节没有外部来源，不计入无来源声明，避免门禁误伤
            if "【AI训练知识】" in chapter:
                skipped_ai += 1
                continue
            claims = extract_claims_from_llm(
                text=chapter,
                writer=f"report.chapter_{i}",
                default_source="报告未标注引用",
                ledger=self.claim_ledger,
                materials=self.all_materials,
                max_claims=30,
            )
            # extract_claims_from_llm 内部已注册到 ledger
            total_extracted += len(claims)

        if skipped_ai:
            logger.info(f"[ClaimLedger] 跳过 {skipped_ai} 个 AI 训练知识章节（无外部来源，不计入门禁）")

        if total_extracted > 0 and self.event_log:
            self.event_log.log(
                "claims_extracted",
                "report",
                details=f"从报告提取 {total_extracted} 条声明",
            )
            logger.info(f"[ClaimLedger] 从报告提取 {total_extracted} 条声明")

    def _get_source_appendix(self) -> str:
        """构建来源追溯表（Markdown 格式），附加到最终报告"""
        if not _HAS_CLAIM_LEDGER or not self._quality_gate:
            return ""
        try:
            limit = int(self.cfg.get("source_appendix_limit", 50))
        except (TypeError, ValueError):
            limit = 50
        return self._quality_gate.build_source_appendix(limit=limit)

    def _agent_call(self, role: str, messages, **kwargs) -> str:
        """
        Agent 角色调用 LLM。
        映射从硬编码改为配置读取，支持动态调整。
        """
        # 读取 agent -> model 映射（fallback 到旧映射）
        mapping = self.cfg.get("agent_mapping", {})
        model_role = mapping.get(role, role)

        # 模型角色到 LLM 客户端方法的映射
        method_map = {
            "planner": self.llm.plan,
            "search": self.llm.plan,      # search 复用 planner
            "writer": self.llm.fast,
            "evaluator": self.llm.plan,   # evaluator 复用 planner
            "factcheck": self.llm.factcheck,
            "editor": self.llm.editor,
            "clarifier": self.llm.clarify,
            "backtester": self.llm.factcheck,  # backtester fallback到factcheck
            "reviewer": self.llm.editor,       # reviewer fallback到editor
        }

        method = method_map.get(model_role)
        if method:
            return method(messages, **kwargs)
        else:
            # 未知角色 fallback 到 fast
            logger.warning(f"[Agent] 未知角色 '{role}'(映射到 '{model_role}')，fallback 到 fast")
            return self.llm.fast(messages, **kwargs)

    def clarify_questions(self, task: str) -> str:
        logger.info("[阶段0] AI正在审视需求...")
        prompt = f"""【系统约束声明】本工具仅基于互联网公开信息进行桌面研究，无法执行线下访谈、暗访、实地调研、问卷发放或实验验证。
【当前日期】{current_year_anchor()}

【任务】围绕以下调研需求生成澄清问题
需求：{task}

【强制约束】
1. 仅围绕调研主题、范围、侧重点、目标品牌/地区提问
2. 严禁询问线下调研相关事项
3. 必须生成3-5个问题
4. 每个问题必须包含三个部分：
   - question: 问题本身（简洁、具体、可回答）
   - explanation: 为什么问这个问题——帮助用户理解这个问题的调研价值
   - impact: 这个问题如何影响调研方向和报告质量——让用户知道回答后能带来什么改善

【深度追问链——附加要求】
基于用户已提供的信息，请生成1-3个深度追问：
- 每个追问应能显著改变调研方向或深度
- 追问应帮助区分"信息收集"vs"战略建议"的边界
- 如果用户回答"不需要"，则跳过该维度

追问格式示例：
{{
  "question": "您是否需要包含竞争对手的详细对标分析？",
  "explanation": "这将决定我们是做市场全景扫描，还是聚焦竞争策略",
  "impact": "若需要，报告将增加3-5页竞争对标章节；若不需要，可聚焦用户自身策略",
  "skip_if": "用户明确说'只关注我们自己'"
}}

【标准输出格式】（JSON数组）
[
  {{
    "question": "调研的具体目标品牌是哪些？",
    "explanation": "不同品牌的渠道策略、定价模式差异很大，明确目标品牌才能精准对标",
    "impact": "如果目标品牌不明确，调研会泛泛而谈，无法给出有针对性的竞品分析和策略建议"
  }},
  ...
]

如果信息已经充足，返回 [{{"question": "", "explanation": "", "impact": ""}}]"""
        questions_raw = self._agent_call("clarifier", [{"role": "user", "content": prompt}], max_tokens=3000)
        
        # 解析结构化问题
        structured_questions = []
        try:
            import json
            text = questions_raw.strip()
            if '[' in text:
                start = text.index('[')
                end = text.rindex(']') + 1
                raw_list = json.loads(text[start:end])
                for q in raw_list:
                    if isinstance(q, dict) and q.get("question", "").strip():
                        structured_questions.append(q)
                    elif isinstance(q, str) and q.strip():
                        structured_questions.append({"question": q, "explanation": "", "impact": ""})
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(f"[engine] JSON解析失败，使用降级解析: {e}")
        
        # 降级：如果JSON解析失败，按行解析旧格式
        if not structured_questions:
            lines = [l.strip() for l in questions_raw.split('\n') if l.strip()]
            for line in lines[:5]:
                structured_questions.append({"question": line, "explanation": "", "impact": ""})
        
        # 过滤空问题
        structured_questions = [q for q in structured_questions if q.get("question", "").strip()]
        
        q_count = len(structured_questions)
        if q_count < 3:
            logger.warning(f"[澄清] 仅{q_count}个问题，强制重试...")
            questions_raw = self._agent_call("clarifier", [{"role":"user","content":prompt + "\n\n⚠️ 你只输出了少于3个问题。必须输出至少3个澄清问题。重试。"}], max_tokens=3000)
            # 重试结果先放入独立列表，避免直接 append 造成重复累加
            retry_questions = []
            try:
                import json
                text = questions_raw.strip()
                if '[' in text:
                    start = text.index('[')
                    end = text.rindex(']') + 1
                    raw_list = json.loads(text[start:end])
                    for q in raw_list:
                        if isinstance(q, dict) and q.get("question", "").strip():
                            retry_questions.append(q)
            except (json.JSONDecodeError, ValueError):
                pass
            # 先过滤空问题，再按 question 文本去重
            retry_questions = [q for q in retry_questions if q.get("question", "").strip()]
            _seen_retry = set()
            _dedup_retry = []
            for q in retry_questions:
                _key = q["question"].strip()
                if _key not in _seen_retry:
                    _seen_retry.add(_key)
                    _dedup_retry.append(q)
            retry_questions = _dedup_retry
            if len(retry_questions) >= q_count:
                # 重试结果更完整，整体替换原列表
                structured_questions = retry_questions
            else:
                # 重试结果偏少，只补充原列表中没有的问题
                _existing = {q.get("question", "").strip() for q in structured_questions}
                for q in retry_questions:
                    if q["question"].strip() not in _existing:
                        structured_questions.append(q)
                        _existing.add(q["question"].strip())
            structured_questions = [q for q in structured_questions if q.get("question", "").strip()]
            q_count = len(structured_questions)

        # 推演引擎 — 过滤候选问题，分离可推理/必问
        deduction_result = None
        # 推演前保存原始问题，供必问重建时回查 explanation/impact
        original_by_question = {q.get("question", ""): q for q in structured_questions}
        if q_count >= 3:
            try:
                def _llm_call(prompt_text):
                    # 推演结果是结构化 JSON，3000 token 会截断导致解析失败
                    return self._agent_call("planner", [{"role": "user", "content": prompt_text}], max_tokens=6000)
                engine = DeductionEngine(_llm_call)
                deduction_result = engine.filter_questions(
                    task=task,
                    candidate_questions=structured_questions,
                    max_keep=5,
                )
                logger.info(f"[推演引擎] 必问:{len(deduction_result['must_ask'])} 可推理:{len(deduction_result['inferable'])}")

                # 存储可推理项，注入后续plan/search阶段
                self.inferable_context = engine.build_injectable_context(deduction_result)

                # 存储主导视角，用于后续 generate_plan 中作为搜索视角约束
                self.perspective_constraint = engine.build_perspective_constraint(deduction_result)
                if self.perspective_constraint:
                    logger.info(f"[推演引擎] 主导视角: {[p.get('perspective','') for p in deduction_result.get('dominant_perspectives', [])]}")
                else:
                    logger.info("[推演引擎] 无主导视角输出，使用默认五视角")

                if deduction_result["all_could_infer"]:
                    logger.info("[推演引擎] 全部问题可推理，跳过逐条追问")
                    # GUI 下仍需展示推断摘要确认页，避免用户对推断结果无感；
                    # 可推理项统一由 generate_plan 注入一次，此处不再拼接 inferable_context
                    if self.gui is not None:
                        summary_parts = [
                            "系统已根据您的需求自行推断以下信息，无需逐条回答：\n\n"
                        ]
                        for i, item in enumerate(deduction_result.get("inferable", [])[:5], 1):
                            summary_parts.append(f"{i}. {item.get('original_question', '')}\n")
                            if item.get("reason"):
                                summary_parts.append(f"   💡 {item['reason']}\n")
                            summary_parts.append("\n")
                        summary_parts.append(
                            "若推断有误，请在下方补充说明（会作为调研约束）；无补充可直接确认继续。"
                        )
                        answer = self.gui.wait_for_clarify("".join(summary_parts))
                        if answer and answer.strip():
                            return task + "\n\n【用户补充信息】\n" + answer
                    # headless：有/无 clarify_callback 都直接返回 task（全是可推理则无需回调作答）
                    return task

                # 重建 structured_questions 为只含必问项
                # 按 question 文本回查原始项，优先保留原始 explanation/impact
                structured_questions = []
                for q in deduction_result["must_ask"]:
                    original = original_by_question.get(q["question"], {})
                    structured_questions.append({
                        "question": q["question"],
                        "explanation": original.get("explanation") or q.get("reason", ""),
                        "impact": original.get("impact") or f"影响级别: {q.get('gain_level', 'medium')}",
                    })
            except Exception as e:
                logger.warning(f"[推演引擎] 推演失败，回退原始问题: {e}")
                deduction_result = None
        
        q_count = len(structured_questions)

        # 格式化展示文本（带解释和影响）
        display_parts = ["感谢您的详细需求说明！在正式开展调研之前，我有以下几个关键问题需要确认，以确保调研方向精准对齐您的目标：\n\n"]
        for i, q in enumerate(structured_questions[:5], 1):
            display_parts.append(f"{i}. {q['question']}\n")
            if q.get("explanation"):
                display_parts.append(f"   💡 {q['explanation']}\n")
            if q.get("impact"):
                display_parts.append(f"   📌 {q['impact']}\n")
            display_parts.append("\n")
        display_parts.append("确认以上信息后，我将立即启动系统性调研。")
        display_text = "".join(display_parts)
        
        logger.info(f"澄清问题({q_count}个): {display_text[:200]}...")

        # 无 GUI 时，如果有 clarify_callback，调用回调自动回答
        if self.gui is None and self.clarify_callback and structured_questions:
            try:
                answers = self.clarify_callback(structured_questions)
                if answers:
                    enriched = task + "\n\n【澄清问题自动回答】\n"
                    for q_text, ans in answers.items():
                        enriched += (
                            f"Q: {q_text}\n"
                            f"A: {ans.get('answer', '')} "
                            f"(来源: {ans.get('source', 'unknown')}, "
                            f"置信度: {ans.get('confidence', 'medium')})\n"
                        )
                    logger.info(f"[澄清] 自动消化 {len(answers)} 个问题")
                    return enriched
            except Exception as e:
                logger.warning(f"[澄清] 自动回答回调失败: {e}")

        if self.gui:
            answer = self.gui.wait_for_clarify(display_text)
            if answer and answer.strip():
                return task + "\n\n【用户补充信息】\n" + answer
        return task


    def _detect_entity_type(self, task: str) -> str:
        """检测用户brief中的实体类型（企业/品牌/产品类目）
        返回: 'enterprise' | 'brand' | 'category' | 'unknown'
        """
        task_lower = task.lower()
        
        # 产品类目信号
        category_signals = ['品类', '类目', '产品类型', '细分市场', '赛道', '领域', '行业格局', '市场']
        category_count = sum(1 for s in category_signals if s in task_lower)
        
        # 品牌信号
        brand_signals = ['品牌', '牌子', '产品线', '系列', '单品', '品牌格局', '品牌排名']
        brand_count = sum(1 for s in brand_signals if s in task_lower)
        
        # 企业信号
        enterprise_signals = ['企业', '公司', '集团', '厂商', '上市公司', '企业排名', '企业格局']
        enterprise_count = sum(1 for s in enterprise_signals if s in task_lower)
        
        # 决策逻辑
        scores = {
            'category': category_count,
            'brand': brand_count,
            'enterprise': enterprise_count,
        }
        max_type = max(scores, key=scores.get)
        max_score = scores[max_type]
        
        if max_score >= 2:
            return max_type
        return 'unknown'

    def _get_search_keyword_for_entity_type(self, entity_type: str) -> str:
        """根据实体类型返回搜索关键词模板"""
        keywords = {
            'enterprise': '企业排名 头部品牌',
            'brand': '品牌排名 市场份额 品牌格局',
            'category': '市场规模 品牌格局 消费趋势',
            'unknown': '企业排名 头部品牌',
        }
        return keywords.get(entity_type, keywords['unknown'])

    def _get_extract_prompt_for_entity_type(self, entity_type: str) -> str:
        """根据实体类型返回LLM提取prompt"""
        prompts = {
            'enterprise': """【任务】从搜索结果中提取企业/品牌名称清单
【提取规则】
1. 只提取有明确商业模式的企业名（品牌名/集团名/公司名），不提取平台名、媒体名、通用术语
2. 如搜索结果包含"排名""梯队""分类"信息，必须保留分类标注（如：第一梯队-XXX）
3. 自动去重，按知名度/排名降序排序
4. 最多提取40个，超出则截取前40个
【输出】纯JSON数组：["企业名1","企业名2","企业名3"]""",
            'brand': """【任务】从搜索结果中提取品牌名称清单
【提取规则】
1. 只提取具体品牌名（如"安慕希""元气森林"），不提取企业名、平台名、通用术语
2. 如搜索结果包含"排名""梯队""分类"信息，必须保留分类标注
3. 自动去重，按知名度/市场份额降序排序
4. 最多提取40个，超出则截取前40个
【输出】纯JSON数组：["品牌名1","品牌名2","品牌名3"]""",
            'category': """【任务】从搜索结果中提取品牌/企业名称清单
【提取规则】
1. 提取该品类下的主要品牌名和企业名（如"无糖茶饮"品类下提取"农夫山泉""元气森林"）
2. 如搜索结果包含"排名""梯队""分类"信息，必须保留分类标注
3. 自动去重，按知名度/市场份额降序排序
4. 最多提取40个，超出则截取前40个
【输出】纯JSON数组：["品牌/企业名1","品牌/企业名2","品牌/企业名3"]""",
            'unknown': """【任务】从搜索结果中提取企业/品牌名称清单
【提取规则】
1. 只提取有明确商业模式的企业名或品牌名，不提取平台名、媒体名、通用术语
2. 如搜索结果包含"排名""梯队""分类"信息，必须保留分类标注
3. 自动去重，按知名度/排名降序排序
4. 最多提取40个，超出则截取前40个
【输出】纯JSON数组：["企业/品牌名1","企业/品牌名2","企业/品牌名3"]""",
        }
        return prompts.get(entity_type, prompts['unknown'])

    def _prefetch_entity_list(self, task: str, batches_hint: int) -> str:
        """广度调研模式预处理——搜索获取实体清单，注入planner prompt。
        返回格式化的企业清单提示文本，失败返回空字符串。"""
        try:
            # 提取行业关键词（去停用词+截断至6个）
            keywords = re.findall(r'[\u4e00-\u9fa5]{2,4}', task)
            stop = {'调研','报告','分析','研究','企业','公司','品牌','第','梯队','产品','结构','商业','模式','需求','规划','方案','背景','目的','对象','内容','维度','方面','相关','进行','通过'}
            kws = [k for k in keywords if k not in stop][:6]
            from datetime import datetime
            current_year = datetime.now().year
            entity_type = self._detect_entity_type(task)
            search_keyword = self._get_search_keyword_for_entity_type(entity_type)
            search_query = f"{' '.join(kws)} {search_keyword} {current_year}"

            # 轻量搜索（不用GPTR，快速获取）
            logger.info(f"[实体清单] 预处理搜索: {search_query}")
            results = self.searcher.multi_search([search_query], max_results=5, workers=2, aggressive=False)
            if not results:
                return ""

            # LLM提取实体名
            results_text = "\n".join([f"- {r.title}: {r.content[:300]}" for r in results[:5]])
            extract_prompt = self._get_extract_prompt_for_entity_type(entity_type)
            extract_prompt += f"\n\n【输入】搜索结果：{results_text}"

            raw = self.llm.fast([{"role":"user","content":extract_prompt}], max_tokens=600, json_mode=True)
            data = safe_json_loads(raw, [])
            if not isinstance(data, list) or len(data) == 0:
                return ""

            entities = [e.strip() for e in data if isinstance(e, str) and len(e.strip()) >= 2][:30]
            if not entities:
                return ""

            # 根据实体类型调整提示文本
            type_labels = {'enterprise': '企业', 'brand': '品牌', 'category': '品牌/企业', 'unknown': '实体'}
            label = type_labels.get(entity_type, '实体')
            hint = f"""【预获取{label}清单】（已通过搜索获取{len(entities)}个候选{label}，请据此规划搜索批次）：
{', '.join(entities)}
请将这些{label}按梯队/规模/模式分组，分配到各搜索批次中。每批查询格式："{label}名 调研维度"（如"某品牌 产品线 {datetime.now().year}"）"""
            return hint

        except Exception as ex:
            logger.warning(f"[实体清单预处理失败] {ex}，降级为planner自行规划")
            return ""

    def generate_plan(self, task: str):
        logger.info("[阶段1] 生成调研计划书（搜索批次规划 + DSP上下文规划）...")

        # 注入推演引擎的可推理项到 task 中（只注入一次）
        if self.inferable_context:
            task = task + self.inferable_context
            logger.info(f"[阶段1] 已注入推演可推理项（{len(self.inferable_context)}字符）")
            self.inferable_context = ""

        # 注入澄清阶段回答，确保业态/角色/范围约束进入计划与搜索词
        if self.clarify_context and self.clarify_context[:80] not in task:
            task = task + "\n\n【澄清阶段确认的方向】\n" + self.clarify_context
            logger.info(f"[阶段1] 已注入澄清回答（{len(self.clarify_context)}字符）")

        task_summary = self._compose_task_summary(task, 2000)
        biz_terms = self._business_form_keywords()
        biz_hint = ""
        if biz_terms:
            biz_hint = (
                "【业态关键词——必须体现在搜索查询中】\n"
                f"调研对象的具体业态：{'、'.join(biz_terms)}。\n"
                "每批至少2条查询必须把行业名与业态词组合（如“行业名 门店加盟/单店模型/坪效/SKU/选址/供应链”），"
                "严禁只用行业名词或公司名单独成词。\n"
            )
            logger.info(f"[计划生成] 注入业态关键词: {biz_terms}")
        max_pages = int(self.cfg.get("max_pages", 15))

        # 根据用户选择的批次数调整prompt
        batches_hint = self.cfg.get("search_batches", 3)
        logger.info(f"[搜索批次] 用户选择 {batches_hint} 批搜索")

        if batches_hint >= 4:
            # 广度调研模式 → 强制实体×维度交叉矩阵，每个实体至少2个维度
            batch_template = """【搜索批次规划】（广度调研模式。请根据实体数量生成批次，每批聚焦3-5个实体/主题，每批3-5个查询）
  批次1-行业宏观：行业整体规模、政策环境、发展趋势
  批次2-第一梯队/头部：头部实体逐家交叉搜索 → 每个实体至少2个不同维度（如"实体名 商业模式 产品结构"+"实体名 竞争壁垒 市场份额"）
  批次3-第二梯队A组：腰部企业第1组 → 每个实体至少2个维度交叉搜索
  批次4-第二梯队B组：腰部企业第2组（如需） → 每个实体至少2个维度交叉搜索
  批次5-第三梯队A组：长尾企业第1组 → 每个实体至少2个维度交叉搜索
  ...（根据企业数量继续追加批次）
  最后一批-交叉对比：跨梯队商业模式矩阵、产品结构对比、竞争定位图
  要求：每批查询必须包含具体实体名+调研维度，严禁泛泛而搜。每个实体必须从至少2个维度交叉搜索（如商业模式+竞争壁垒，产品结构+市场份额），确保信息不片面。"""
        else:
            batch_template = """【搜索批次规划】（必须输出，这是核心交付物。请根据调研复杂度生成2-5个批次，每批3-5个查询）
  批次1-宏观层：建立行业基本面认知的关键词组合
  批次2-中观层：锁定竞争格局与头部玩家的关键词组合
  批次3-微观层：抓取转化链路与运营细节的关键词组合
  （如需更多批次，请追加：批次4-细分维度、批次5-交叉对比、批次6-案例深潜等）"""

        # 广度调研模式 → 检测用户是否已自带实体清单
        entity_list_hint = ""
        if batches_hint >= 4:
            # 通用启发式检测——用户是否已自带企业/品牌清单
            # 信号1：编号列表（"1. XX公司" "2. YY集团" 或 "第一梯队：A、B、C"）
            numbered_items = len(re.findall(r'(?:\d+[\.\、\)）]\s*[一-龥]{2,10}|第[一二三四五六七八九十\d]+梯)', task_summary))
            # 信号2：带后缀的企业名（"XX公司""XX集团""XX品牌"）
            suffixed = len(re.findall(r'[一-龥]{2,8}(?:公司|集团|企业|品牌|有限|股份|实业)', task_summary))
            # 信号3：顿号/逗号分隔的短词密集区（≥5个2-8字中文词连续出现）
            comma_clusters = re.findall(r'(?:[一-龥·\w]{2,10}[、，,]){4,}', task_summary)
            cluster_count = sum(len(re.findall(r'[一-龥]{2,10}', c)) for c in comma_clusters)
            # 信号4：输入长度 > 300字（大概率含详细清单）
            is_long = len(task_summary) > 300

            total_signals = numbered_items + suffixed + cluster_count
            has_user_list = total_signals >= 5 or (is_long and total_signals >= 3)
            if has_user_list:
                logger.info(f"[实体清单] 用户已自带清单（信号强度:{total_signals}），跳过预处理")
                entity_list_hint = """【用户已提供实体清单】：
请直接基于用户输入中的实体分类进行批次规划，无需额外搜索实体名单。每批搜索格式："实体名 调研维度"。"""
            else:
                entity_list_hint = self._prefetch_entity_list(task_summary, batches_hint)
                if entity_list_hint:
                    logger.info(f"[实体清单预处理] 获取到候选实体列表，注入planner")

        prompt = f"""【角色】资深商业分析师
【当前日期】{current_year_anchor()}
【报告页数期望】{max_pages}页（全局参考，实际结构由素材决定）
【搜索模式】{batches_hint}批 × {self.cfg.get("max_iterations",2)}迭代
{entity_list_hint}
{self.perspective_constraint}
{biz_hint}

【能力边界声明】本系统仅支持互联网公开信息的桌面研究，无法执行线下访谈、实地暗访、电话调研、问卷发放。搜索策略中严禁提及上述活动。

【需求摘要】{task_summary}

【输出结构——必须严格遵循】
### 【需求理解】（3-4句话，内容充实不空泛）
### 【建议调研方向】（3-5个方向，每个方向说明调研价值）
{batch_template}
### 【核心追问】（1-2个贯穿全文的核心问题，用于后续素材评估）
### 【上下文规划】
  - 信息需求清单：明确各章节需要的数据类型（市场规模/竞争格局/案例/趋势）
  - 信息优先级：区分核心支柱数据 vs 锦上添花数据
  - 预期缺口：标注公开渠道可能难以获取的数据，提前约定"推测""估算"措辞

【强制要求】
1. 每部分至少3-4句话，严禁敷衍
2. 【搜索批次规划】中每批必须包含3-5个具体搜索查询
3. 严禁输出"第一章X页、第二章X页"的固定章节结构
4. 所有规划必须可通过公开搜索执行
5. 查询词必须承接【需求摘要】中的调研对象角色、区域范围与业态，不得退化为行业名词的泛搜"""

        if self._user_strategy:
            prompt += f"\n\n【用户确认过的调研策略】\n{self._user_strategy}\n"

        for attempt in range(3):
            try:
                self.plan_text = self._agent_call("search", [{"role": "user", "content": prompt}], max_tokens=4000)
                content_len = len(self.plan_text.strip())
                logger.info(f"[计划生成] 尝试{attempt+1}/3，返回长度：{content_len}")
                if content_len > 100:
                    logger.info(self.plan_text[:500] + "..." if content_len > 500 else self.plan_text)
                    break
                elif attempt < 2:
                    logger.warning(f"[计划生成] 返回内容过短({content_len}字)，重试...")
                    time.sleep(1)
            except Exception as e:
                logger.error(f"[计划生成错误] {e}")
                if attempt < 2:
                    time.sleep(2)

        self.search_batches = self._parse_search_batches(self.plan_text)
        self._limit_search_batches()
        logger.info(f"[搜索批次] 解析到{len(self.search_batches)}个批次")

        # 提取调研方向和核心追问，用于后续注入搜索和覆盖度评估
        self.plan_directions, self.plan_core_questions = _extract_plan_key_info(self.plan_text)
        logger.info(f"[Plan提取] 方向数:{len(self.plan_directions)}, 追问数:{len(self.plan_core_questions)}")

    def _load_user_strategy(self) -> str:
        """读取最近一次用户在报告反馈中确认的策略。"""
        try:
            strategy_dir = os.path.join(self.cfg.get("output_dir") or "", "strategy")
            files = [
                f for f in os.listdir(strategy_dir)
                if f.startswith("user_strategy_") and f.endswith(".md")
            ]
            if not files:
                return ""
            files.sort(reverse=True)
            with open(os.path.join(strategy_dir, files[0]), encoding="utf-8") as fh:
                return fh.read()[:2500]
        except Exception:
            return ""

    def _limit_search_batches(self):
        """按用户设置的搜索批次数收敛计划生成的批次。"""
        hint = int(self.cfg.get("search_batches", 3) or 3)
        if hint > 0 and len(self.search_batches) > hint:
            logger.info(f"[搜索批次] 计划{len(self.search_batches)}批，按设置收敛到{hint}批")
            self.search_batches = self.search_batches[:hint]

    def _parse_search_batches(self, plan_text: str) -> List[Dict]:
        """从计划书文本解析搜索批次（支持【批次】格式）"""
        batches = []
        batch_blocks = re.split(r'(?:【?批次|Batch)[\s]*[\d一二三四五]+[\s]*[】]?[：:\-]', plan_text)
        for block in batch_blocks[1:]:
            lines = [l.strip() for l in block.split('\n') if l.strip() and not l.strip().startswith('【')]
            queries = []
            for line in lines[:8]:
                q = re.sub(r'^[-\d\.\s]*', '', line).strip()
                if len(q) > 5 and len(q) < 100:
                    queries.append(q)
            if queries:
                batches.append({"queries": queries[:8], "max_results": int(self.cfg.get("max_search_results", 5)), "description": lines[0][:40] if lines else ""})

        if not batches:
            # 计划书没有【批次】格式时用主题词兜底，避免把任务原文切成碎片当查询
            anchors = self.topic_keywords or self._task_anchors(self.refined_task)
            keywords = " ".join((anchors or [])[:4]) or self.refined_task[:20]
            biz = " ".join(self._business_form_keywords(2))
            biz = f" {biz}" if biz else ""
            year = datetime.now().year
            batches = [
                {"queries": [f"{keywords}{biz} 市场规模 {year}", f"{keywords}{biz} 行业趋势"], "max_results": int(self.cfg.get("max_search_results", 5)), "description": "宏观层"},
                {"queries": [f"{keywords}{biz} 竞争格局 头部品牌", f"{keywords}{biz} 市场份额"], "max_results": int(self.cfg.get("max_search_results", 5)), "description": "中观层"},
                {"queries": [f"{keywords}{biz} 案例 运营模式", f"{keywords}{biz} 转化链路"], "max_results": int(self.cfg.get("max_search_results", 5)), "description": "微观层"},
            ]
            logger.warning(f"[搜索批次] 计划书未解析出批次，用主题词兜底查询: {keywords[:40]}")
        return self._inject_business_terms_into_batches(batches)

    def _preflight_search_queries(self):
        """evaluator轻量协作——审视搜索查询词的可执行性，标注泛化和盲区。"""
        if not self.cfg.get("enable_search_preflight", False):
            return
        if not self.search_batches:
            return
        all_queries = []
        for b in self.search_batches:
            all_queries.extend(b.get("queries", []))
        if not all_queries:
            return

        logger.info(f"[搜索预审] evaluator审视{len(all_queries)}个查询词...")
        rewrite_enabled = self.cfg.get("search_preflight_rewrite", True)
        prompt = f"""你是搜索策略专家。审视以下搜索查询词，逐条判断：
1. 是否具体到能搜出有效结果（\u592a\u6cdb\u2192\u6807\u6ce8\"\u6cdb\u5316\"\uff09
2. 是否缺少关键维度（如竞品对比、价格、渠道等）
3. {'给出1条替代查询词' if rewrite_enabled else '仅标注问题，不修改'}

查询词列表：
{chr(10).join(f'{i+1}. {q}' for i, q in enumerate(all_queries))}

输出JSON:
{{"evaluations":[{{"index":1,"query":"原查询","verdict":"ok/泛化/冗余","suggestion":"替代查询词或空"}}],"missing_dimensions":["未覆盖的关键维度"],"overall_score":0.8}}"""
        try:
            raw = self._agent_call("evaluator", [{"role":"user","content":prompt}], max_tokens=600, json_mode=True)
            data = safe_json_loads(raw, {})
            if not isinstance(data, dict):
                return
            evals = data.get("evaluations", [])
            missing = data.get("missing_dimensions", [])
            if missing:
                logger.info(f"[搜索预审] 发现盲区维度: {missing}")

            query_positions = []
            for bi, b in enumerate(self.search_batches):
                for qi in range(len(b.get("queries", []))):
                    query_positions.append((bi, qi))

            changed = 0
            for ev in evals:
                idx = ev.get("index", 0) - 1
                verdict = ev.get("verdict", "ok")
                suggestion = ev.get("suggestion", "")
                if verdict in ("泛化", "冗余") and suggestion and rewrite_enabled and 0 <= idx < len(query_positions):
                    bi, qi = query_positions[idx]
                    old_q = self.search_batches[bi]["queries"][qi]
                    self.search_batches[bi]["queries"][qi] = suggestion[:120]
                    logger.info(f"[搜索预审] 改写: '{old_q[:40]}...' \u2192 '{suggestion[:60]}...'")
                    changed += 1

            overall = data.get("overall_score", 0.8)
            logger.info(f"[搜索预审] 完成: 改写{changed}个, 整体可执行性{overall:.0%}")
        except Exception as e:
            logger.warning(f"[搜索预审] 失败(非致命): {e}")

    async def depth_search(self):
        logger.info("[L1 全景扫描] 启动 GPT Researcher + 批次搜索 + STORM辩论...")
        time_anchored = self.refined_task + f"\n【时间锚定：{current_year_anchor()}】"

        # GPT Researcher 深度研究（plan_text已传入，内部会提取方向+追问构建enriched_query）
        raw_report = await self.searcher.deep_research(self.plan_text, time_anchored)

        # 检查搜索是否彻底失败（GPTR+DDG均失败）
        if self.searcher.search_failure:
            logger.warning("[L1] GPTR+DDG搜索彻底失败，跳过深度搜索后续步骤")
            self.state.search_failure = True
            return

        raw_path = os.path.join(self.cfg["output_dir"], "后台调试", "L1_raw_report.md")
        os.makedirs(os.path.dirname(raw_path), exist_ok=True)
        os.makedirs(self.cfg["output_dir"], exist_ok=True)
        with open(raw_path, "w", encoding="utf-8") as f:
            f.write(raw_report)
        self.raw_report_path = raw_path
        logger.info(f"[L1素材文件] 原始报告已写入 {raw_path} ({len(raw_report)} 字)")

        # 记录 GPT Researcher 成本
        if self.cost_tracker:
            gptr_cost = self.cfg.get("gptr_cost_per_session", 0.8)
            self.cost_tracker.record_search("gptr", self.refined_task[:50], len(raw_report.split('\n')), gptr_cost)

        materials = parse_research_to_materials(raw_report, self.llm, self.cfg.get("plan_model", "deepseek-v4-flash"),
                                                 plan_directions=self.plan_directions)
        if materials:
            # 根据信源等级调整素材截断长度，避免高价值内容丢失
            def _gptr_content_length(m: dict) -> int:
                """根据信源等级返回截断长度"""
                tier = m.get("tier", "")
                if tier in ("S", "A"):
                    return 6000  # S/A级信源保留更多内容
                elif tier == "B":
                    return 4000  # B级信源中等保留
                return 2000      # C级及以下保持原截断

            results = [SearchResult(
                title=m.get("title", ""), url=m.get("url", ""),
                content=m.get("content", "")[:_gptr_content_length(m)], source="gptr",
                source_type=m.get("source_type", "report"),
                claim_nature=m.get("claim_nature", ""),
            ) for m in materials]
            # 用 add_fast 避免LLM压缩阻塞
            # 域名分级处理
            from config import get_merged_domain_priority
            priority_map = get_merged_domain_priority(self.cfg)
            filtered_results = []
            blocked_count = 0
            for r in results:
                url_lower = r.url.lower()
                priority = 5
                for key, pri in priority_map.items():
                    if key in url_lower:
                        priority = pri
                        break
                if priority == 0:
                    # 文档平台：标记但不丢弃
                    r.source_type = "doc_platform"
                    blocked_count += 1
                    # 不加入 filtered_results（GPTR 抓取文档平台无价值）
                else:
                    filtered_results.append(r)
            if blocked_count > 0:
                logger.info(f"[GPTR域名分级] 过滤 {blocked_count} 条来自文档分享平台的低质量素材")
            results = filtered_results

            # 只保留与主题相关的 GPTR 素材；企业自述素材在抽取阶段已判定相关性
            relevant = [
                r for r in results
                if self._material_relevant(r, self.refined_task)
            ]
            if len(relevant) < len(results):
                logger.info(f"[GPTR过滤] 丢弃 {len(results) - len(relevant)} 条与主题无关素材")
            results = relevant
            if not results:
                logger.warning("[GPTR素材] 报告未产出与主题相关的素材（抓取页面可能为广告或无关内容）")

            self.all_materials.extend(results)
            self.memory.add_fast(results)
            for r in results[:10]:
                self.state.add_confirmed(r)

            # 注意：不要把 GPTR 结果写入搜索缓存。否则批次搜索的具体查询可能命中
            # 这批“任务级”缓存，导致 4 次迭代都返回同一批结果、拿不到新素材。

            # GPTR结果写入 Meilisearch 本地搜索索引（跨工具复用）
            try:
                from shared.search.adapters import MeiliAdapter
                meili = MeiliAdapter()  # 使用默认配置
                if meili.available:
                    meili.index_documents(results)
                    logger.info(f"[Meili索引] GPTR {len(results)} 条素材已写入搜索索引")
            except Exception as e:
                logger.debug(f"[Meili索引] 写入跳过: {e}")

        if self.search_batches:
            # 批次搜索预算管控：每批消耗1个budget
            batch_count = len(self.search_batches)
            batch_results = []
            if not self.state.consume_budget(batch_count):
                logger.warning(f"[批次搜索] 预算不足(需{batch_count}，剩{self.state.search_budget})，跳过批次搜索")
            else:
                logger.info(f"[批次搜索] 执行{batch_count}个搜索批次，消耗{batch_count}预算(剩{self.state.search_budget})...")
                batch_results = self.searcher.execute_search_batches(self.search_batches, max_iterations=int(self.cfg.get("max_iterations", 2)))
            if batch_results:
                logger.info(f"[批次搜索] 补充 {len(batch_results)} 条素材")
                self.all_materials.extend(batch_results)
                logger.info(f"[诊断] 即将调用 memory.add_fast({len(batch_results)}条)...")
                self.memory.add_fast(batch_results)
                logger.info(f"[诊断] memory.add_fast 完成")
                # 批次搜索结果写入 Meilisearch
                try:
                    from shared.search.adapters import MeiliAdapter
                    meili = MeiliAdapter()
                    if meili.available:
                        meili.index_documents(batch_results)
                        logger.info(f"[Meili索引] 批次搜索 {len(batch_results)} 条素材已写入搜索索引")
                except Exception:
                    pass

        # 第一轮搜索完成（GPTR + 批次搜索）→ 触发信源截取回调
        # 调用方可以不等后续 STORM/补搜/报告撰写，先取信源注入策略节点
        if self.on_first_batch_done and self.all_materials:
            try:
                sources = [
                    {
                        "title": m.title,
                        "url": m.url,
                        "source": m.source,
                        "tier": m.tier,
                        "content": (m.content or "")[:500],
                        "llm_score": m.llm_score,
                        "reliability": m.reliability,
                    }
                    for m in self.all_materials if m.url
                ]
                if asyncio.iscoroutinefunction(self.on_first_batch_done):
                    asyncio.create_task(self.on_first_batch_done(sources))
                else:
                    self.on_first_batch_done(sources)
                logger.info(f"第一轮信源截取：{len(sources)} 条已回调")
            except Exception as e:
                logger.warning(f"信源截取回调失败: {e}")

        if self.cfg.get("enable_storm_debate", False):
            logger.info("[STORM辩论] 生成多角度查询...")
            storm_queries = self.searcher.storm_debate_queries(self.refined_task)
            qs = [q for q in (storm_queries or []) if q][: min(8, max(0, self.state.search_budget))]
            if not qs:
                logger.warning("[STORM辩论] 搜索预算不足，跳过")
            else:
                storm_results = self.searcher.multi_search(qs, max_results=2, workers=4, aggressive=True)
                if storm_results:
                    self._charge_search(qs)
                    logger.info(
                        f"[STORM辩论] 执行{len(qs)}条查询，补充{len(storm_results)}条素材"
                        f"（预算剩{self.state.search_budget}）"
                    )
                    self.all_materials.extend(storm_results)
                    self.memory.add_fast(storm_results)
                else:
                    logger.warning(f"[STORM辩论] 执行{len(qs)}条查询，无结果")

        # 跨源URL去重
        self._deduplicate_materials()

        # Playwright深度抓取高评分素材
        self._enhance_high_score_materials()

        # Layer 2 定向深挖 —— 对高价值URL做全文抓取
        if self.cfg.get("enable_deep_scrape", False) and self.all_materials:
            try:
                from deep_scraper import deep_scrape_results
                top_n = self.cfg.get("deep_scrape_top_n", 15)
                logger.info(f"[DeepScraper] 对 Top {top_n} 高价值URL做全文抓取...")
                # 将 all_materials 转为 list of dict（deep_scraper 需要）
                scrape_input = [{"url": m.url, "title": m.title, "content": m.content, "llm_score": m.llm_score} for m in self.all_materials if m.url]
                scraped = deep_scrape_results(scrape_input, self.cfg)
                if scraped:
                    # 将抓取到的全文更新到对应素材
                    scraped_map = {s.get("url", ""): s.get("full_content", "") for s in scraped if s.get("full_content")}
                    updated_count = 0
                    for m in self.all_materials:
                        if m.url in scraped_map:
                            m.content = scraped_map[m.url][:8000]
                            updated_count += 1
                    logger.info(f"[DeepScraper] 成功抓取 {updated_count} 个URL的全文")
            except Exception as e:
                logger.warning(f"[DeepScraper] 深挖失败（非致命）: {e}")

        # 素材准入：挡住与主题完全无关的跨行业结果（无关公告、HTML 碎片等）
        before_count = len(self.all_materials)
        relevant = [
            m for m in self.all_materials
            if self._material_relevant(m, self.refined_task)
        ]
        if 3 <= len(relevant) < before_count:
            logger.info(f"[素材准入] 过滤无关素材 {before_count - len(relevant)} 条，剩余 {len(relevant)} 条")
            self.all_materials = relevant
        elif len(relevant) < 3:
            logger.warning(
                f"[素材准入] 仅 {len(relevant)} 条命中主题词，保留原素材池（{before_count} 条）以避免空洞"
            )
        logger.info(f"[L1汇总] 去重后共{len(self.all_materials)}条素材")

        # 将搜索结果注册到声明账本
        self._register_search_claims()

    def _deduplicate_materials(self):
        """跨搜索源URL去重（GPT Researcher/批次搜索/STORM之间）"""
        seen_urls = set()
        deduped = []
        dup_count = 0
        for m in self.all_materials:
            key = m.url.strip() if m.url else m.title.strip()
            if key and key not in seen_urls:
                seen_urls.add(key)
                deduped.append(m)
            else:
                dup_count += 1
        if dup_count > 0:
            logger.info(f"[去重] 跨源删除{dup_count}条重复素材")
            self.all_materials = deduped

    def _enhance_high_score_materials(self):
        """智能全文提取——根据素材质量分层提取，最大化高价值内容获取。
        
        提取策略：
        - Tier 1 (llm_score >= 8): 必提取全文，无论当前长度
        - Tier 2 (llm_score >= 6 且 content < 3000): 提取全文，补充细节
        - Tier 3 (llm_score >= 6 且 source_type in ['doc', 'report']): 文档类素材优先提取
        - 限制：最多处理前20条，避免超时
        """
        if not self.cfg.get("enable_browser_enhance", False):
            return
        
        enhanced_count = 0
        max_enhance = min(20, len(self.all_materials))  # 最多处理20条
        
        # 按优先级排序：高分优先，短内容优先
        sorted_materials = sorted(
            self.all_materials,
            key=lambda m: (
                -m.llm_score,  # 高分优先
                len(m.content) if m.content else 0,  # 短内容优先（更需要补充）
            )
        )
        
        for m in sorted_materials[:max_enhance]:
            if not m.url:
                continue
                
            # Tier 1: 高分素材必提取
            should_extract = False
            if m.llm_score >= 8:
                should_extract = True
                reason = "高分素材"
            # Tier 2: 中高分但内容较短
            elif m.llm_score >= 6 and len(m.content or "") < 3000:
                should_extract = True
                reason = "中高分短内容"
            # Tier 3: 文档/报告类素材
            elif m.llm_score >= 6 and m.source_type in ['doc', 'report', 'doc_platform']:
                should_extract = True
                reason = "文档类素材"
            
            if not should_extract:
                continue
            
            try:
                extra = self.searcher.enhancer.extract_article(m.url)
                if extra and len(extra) > 500:
                    # 智能合并：保留更长的内容
                    if len(extra) > len(m.content or ""):
                        old_len = len(m.content or "")
                        m.content = extra[:10000]  # 提升到10000字上限
                        enhanced_count += 1
                        logger.info(f"[深度抓取] {reason} {m.title[:40]}... {old_len}→{len(m.content)}字")
            except Exception as e:
                logger.debug(f"[深度抓取] 失败 {m.url[:60]}: {e}")
                continue
        
        if enhanced_count > 0:
            logger.info(f"[深度抓取] 共增强{enhanced_count}条素材（Tier策略）")

    def check_plan_coverage(self) -> Dict:
        """对照Plan的调研方向和核心追问，评估素材覆盖度。
        返回: {direction_name: {"status": "充足/一般/空缺", "count": N, "query": "..."}}"""
        logger.info("[覆盖度评估] 对照Plan方向检查素材覆盖度...")

        coverage = {}
        all_text = "\n".join([f"{m.title} {m.content[:800]}" for m in self.all_materials])

        # 检查每个调研方向
        for i, direction in enumerate(self.plan_directions):
            clean_direction = re.sub(r'\*+', '', direction)
            clean_direction = re.sub(r'^\d+[\.\、\s]*', '', clean_direction)
            keywords = re.findall(r'[\u4e00-\u9fa5]{2,5}', clean_direction)
            # 过滤停用词和短词（结果截断至5个）
            stop_words = {
                '重点','聚焦','抓取','不同','定位','相关','梳理','说明','论证','旨在','拆分','该方','我司',
                '项目','产品','模型','研究','分析','模式','设计','方案','机制','策略','案例','进行',
                '通过','如何','一个','哪些','什么','这个','可以','需要','以及','及其','作为','对于',
                '调查','信息','数据','问题','内容','方面','方向','目标','目的','方法','框架',
            }
            key_kw = [kw for kw in keywords if len(kw) >= 3 and kw not in stop_words][:5]

            count = 0
            for kw in key_kw:
                count += len(re.findall(re.escape(kw), all_text))

            # 精确匹配失败时回退到Ollama向量语义搜索
            if count == 0 and self.memory and self.memory.vector_store and self.memory.vector_store.vectors is not None:
                vec_results = self.memory.search(clean_direction, top_k=5)
                if vec_results:
                    sim_count = sum(1 for r in vec_results if r.get("score", 0) > 0.3)
                    if sim_count > 0:
                        count = sim_count
                        logger.info(f"    ↳ 向量语义匹配: {sim_count}条相关素材 (阈值>0.3)")

            if count >= 5:
                status = "充足"
            elif count >= 1:
                status = "一般"
            else:
                status = "空缺"

            coverage[f"方向{i+1}"] = {
                "name": clean_direction[:40],
                "status": status,
                "count": count,
                "query": f"{key_kw[0] if key_kw else clean_direction[:30]} 深度分析" if status == "空缺" else ""
            }
            logger.info(f"  [{status}] {clean_direction[:50]} (关键词:{key_kw[:3]} 命中:{count})")

        # 检查核心追问
        for i, question in enumerate(self.plan_core_questions):
            clean_q = re.sub(r'\*+', '', question)
            q_keywords = re.findall(r'[\u4e00-\u9fa5]{2,5}', clean_q)
            q_keywords = [kw for kw in q_keywords if len(kw) >= 3 and kw not in stop_words][:5]
            q_count = sum(len(re.findall(re.escape(kw), all_text)) for kw in q_keywords)
            # 核心追问也做向量回退
            if q_count == 0 and self.memory and self.memory.vector_store and self.memory.vector_store.vectors is not None:
                vec_results = self.memory.search(clean_q, top_k=3)
                if vec_results:
                    q_count = sum(1 for r in vec_results if r.get("score", 0) > 0.3)
            coverage[f"核心追问{i+1}"] = {
                "name": clean_q[:50],
                "status": "一般" if q_count >= 1 else "空缺",
                "count": q_count,
                "query": clean_q[:80] if q_count < 1 else ""
            }
            logger.info(f"  [{'一般' if q_count >= 1 else '空缺'}] 追问: {clean_q[:50]} (关键词:{q_keywords[:3]} 命中:{q_count})")

        self.plan_coverage = coverage
        return coverage

    def _search_within_budget(self, queries, max_results=3, workers=3, limit=5):
        """按剩余预算和单次上限截断查询；返回 (结果, 实际执行的查询列表)。"""
        candidates = [q for q in queries if q]
        qs = candidates[: min(max(0, limit), max(0, self.state.search_budget))]
        if not qs:
            logger.warning("[搜索] 预算已耗尽或查询为空，跳过本轮查询")
            return [], []
        results = self.searcher.multi_search(qs, max_results=max_results, workers=workers)
        logger.info(f"[搜索] 执行{len(qs)}条查询，命中{len(results or [])}条")
        return results or [], qs

    def _charge_search(self, executed_queries) -> int:
        """按实际执行的查询数扣预算；只在调用方确认命中后调用。"""
        cost = min(len(executed_queries or []), self.state.search_budget)
        if cost and self.state.consume_budget(cost):
            return cost
        return 0

    def auto_supplement_for_gaps(self, coverage: Dict):
        """对覆盖度评估中标记为'空缺'的方向，自动生成补搜查询并执行。

        与批次搜索、写章补搜共用同一个 search_budget，命中后才按实际
        执行的查询数扣费。
        """
        # 受用户设置的自动补搜开关控制
        if not self.cfg.get("auto_supplement", False):
            logger.info("[自动补搜] 未启用，跳过")
            return

        if self.state.search_budget <= 0:
            logger.warning("[自动补搜] 搜索预算已耗尽，跳过")
            return

        gap_queries = []
        gap_items = []
        for key, info in coverage.items():
            if info.get("status") == "空缺" and info.get("query"):
                gap_queries.append(info["query"])
                gap_items.append({"direction": key, "query": info["query"], "status": "空缺"})

        if not gap_queries:
            logger.info("[自动补搜] 所有方向覆盖充足，无需补充搜索")
            return

        # 空缺方向>=3时，启用 search_density 多维度深挖
        if len(gap_items) >= 3:
            density_budget = min(3, self.state.search_budget)
            logger.info(
                f"[自动补搜] 发现{len(gap_items)}个空缺方向，启用搜索密度深挖"
                f"(预算{density_budget}条)..."
            )
            density_results = self.searcher.search_density(
                gap_items, top_k=int(self.cfg.get("search_density_top_k", 10)))
            if density_results:
                self.state.consume_budget(density_budget)
                self.all_materials.extend(density_results)
                self.memory.add_fast(density_results)
                logger.info(
                    f"[搜索密度深挖] 补充{len(density_results)}条素材，预算剩余{self.state.search_budget}")
                # 深挖后重新匹配章节素材
                self._auto_assign_chapter_materials()
                return

        supplement_results, executed_queries = self._search_within_budget(
            gap_queries, max_results=3, workers=3, limit=5)
        supplement_results = [
            m for m in (supplement_results or [])
            if self._material_relevant(m, self.refined_task)
        ]
        if supplement_results:
            self._charge_search(executed_queries)
            self.all_materials.extend(supplement_results)
            self.memory.add_fast(supplement_results)
            logger.info(
                f"[自动补搜] 补充{len(supplement_results)}条素材，预算剩余{self.state.search_budget}")
            # 补搜后立即重新匹配章节素材
            self._auto_assign_chapter_materials()
        else:
            logger.warning("[自动补搜] 补充搜索无结果")

    def _build_cluster_briefing(self) -> str:
        """
        素材分层注入 — 聚类+综合生成简报

        流程：
        1. 信源分级过滤（C级丢弃）
        2. Ollama + jina embedding 聚类 → 5-8 个主题簇
        3. LLM 综合每个簇 → SynthesisReport
        4. 合并为骨架涌现用的完整简报

        Returns:
            格式化的简报文本（2000-3200 字），覆盖全部素材
        """
        logger.info("素材分层注入: 聚类+综合中...")

        # 1. 信源分级过滤
        from shared.search.models import SourceGrade, filter_by_grade
        filtered = filter_by_grade(self.all_materials, min_grade=SourceGrade.B)
        if len(filtered) < len(self.all_materials):
            dropped = len(self.all_materials) - len(filtered)
            logger.info("信源过滤: 丢弃 %d 条 C 级素材，剩余 %d 条", dropped, len(filtered))
        else:
            filtered = self.all_materials

        if len(filtered) < 5:
            logger.warning("素材过少 (<5)，回退到原始模式")
            return "\n".join([
                f"[{i+1}] {m.title}: {m.content[:250]}"
                for i, m in enumerate(self.all_materials[:40])
            ])

        # 2. 聚类
        try:
            clusters = self._clusterer.cluster(
                filtered,
                min_clusters=self.cfg.get("cluster_min_count", 5),
                max_clusters=self.cfg.get("cluster_max_count", 8),
            )
        except Exception as e:
            logger.warning("聚类失败: %s，回退到原始模式", e)
            return "\n".join([
                f"[{i+1}] {m.title}: {m.content[:250]}"
                for i, m in enumerate(self.all_materials[:40])
            ])

        if not clusters:
            logger.warning("聚类结果为空，回退到原始模式")
            return "\n".join([
                f"[{i+1}] {m.title}: {m.content[:250]}"
                for i, m in enumerate(self.all_materials[:40])
            ])

        # 3. LLM 命名簇
        self._clusterer.name_clusters_with_llm(clusters, llm=self.llm)

        # 4. 综合每个簇
        query = self.refined_task or self.raw_task or ""
        reports = self._synthesizer.synthesize_all_sync(clusters, query=query)

        # 5. 缓存（迭代复用）
        self._cluster_reports = reports
        self._clusterer.set_cached_clusters(clusters)

        # 6. 合并为简报
        briefing = self._synthesizer.build_full_briefing(reports, query=query)
        logger.info("简报生成完成: %d 个簇, %d 字",
                     len(reports), len(briefing))

        # 7. 注入 GlobalContext，让后续章节感知其他主题簇
        self.global_context.set_cluster_briefing(briefing)

        return briefing

    def assess_materials_and_emerge_outline(self) -> Dict:
        logger.info("[阶段3.5] 素材评估与骨架涌现...")

        # 素材分层注入 — 素材足够多时自动聚类并生成简报
        if len(self.all_materials) >= 10:
            mats_summary = self._build_cluster_briefing()
        else:
            # 原逻辑：前40条×250字
            mats_summary = "\n".join([
                f"[{i+1}] {m.title}: {m.content[:250]}"
                for i, m in enumerate(self.all_materials[:40])
            ])

        max_pages = int(self.cfg.get("max_pages", 15))

        prompt = f"""【角色】资深商业分析师
【任务】基于素材分布设计最优报告结构
【素材摘要】{mats_summary}

【强制约束——零容忍违反项】
1. 章节数量由素材自然分布决定：小型4-6章，标准6-8章，大型8-12章，严禁强行凑数
2. 素材丰富的主题单独成章并分配较多页数，素材匮乏的合并或降级
3. 每章必须标注：title、pages、thesis、cases、tables、material_idxs
4. 如素材涌现出计划外的重要维度，必须新增章节
5. 总页数应接近{max_pages}页
6. material_idxs必须填写该章最相关的素材编号（从1开始），每章至少3个编号，严禁空数组
7. 只输出纯JSON数组，不要Markdown标记、不要解释文字
8. 确保JSON语法100%正确，无 trailing comma

【标准输出格式示例】
[
  {{"title":"核心发现摘要","pages":1,"thesis":"提炼全文最关键3-5个发现","cases":0,"tables":1,"material_idxs":[1,2,3]}},
  {{"title":"行业规模与竞争格局","pages":3,"thesis":"分析市场容量与玩家梯队分布","cases":0,"tables":2,"material_idxs":[4,5,6,7]}}
]"""

        outline_data = None
        for attempt in range(4):  # 从3次增加到4次重试
            try:
                raw = self._agent_call("search", [{"role": "user", "content": prompt}], max_tokens=3000, json_mode=True)
                parsed = safe_json_loads(raw, None)
                # 增强容错，处理各种返回类型
                if parsed is None:
                    logger.warning(f"[JSON解析] 返回None，重试...")
                elif not isinstance(parsed, list):
                    logger.warning(f"[JSON解析] 返回非列表类型({type(parsed)})，重试...")
                elif len(parsed) == 0:
                    logger.warning(f"[JSON解析] 返回空列表，重试...")
                else:
                    outline_data = parsed
                    # 校验material_idxs非空
                    empty_idxs = sum(1 for ch in outline_data if not ch.get("material_idxs"))
                    if empty_idxs > len(outline_data) // 2 and attempt < 3:
                        logger.warning(f"[JSON校验] {empty_idxs}/{len(outline_data)}章material_idxs为空，重试...")
                        outline_data = None  # 重置，继续重试
                        continue
                    break
            except Exception as e:
                logger.warning(f"[素材评估失败] attempt {attempt+1}: {e}，重试...")
            time.sleep(1)

        if not isinstance(outline_data, list) or len(outline_data) == 0:
            logger.warning("[素材评估] 所有尝试失败，使用保底结构")
            # 保底结构也尝试从素材标题自动生成
            fallback_titles = []
            for m in self.all_materials[:15]:
                kw = re.findall(r'[\u4e00-\u9fa5]{4,}', m.title)
                if kw:
                    fallback_titles.append(kw[0])
            if fallback_titles:
                outline_data = [
                    {"title": "调研背景与核心发现", "pages": 2, "thesis": "...", "cases": 0, "tables": 0, "material_idxs": [1, 2, 3]},
                    {"title": "行业现状与趋势分析", "pages": 3, "thesis": "...", "cases": 0, "tables": 1, "material_idxs": [4, 5, 6]},
                    {"title": "核心维度深度分析", "pages": 4, "thesis": "...", "cases": 2, "tables": 2, "material_idxs": [7, 8, 9]},
                    {"title": "典型案例研究", "pages": 3, "thesis": "...", "cases": 3, "tables": 1, "material_idxs": [10, 11, 12]},
                    {"title": "结论与行动建议", "pages": 2, "thesis": "...", "cases": 0, "tables": 0, "material_idxs": [13, 14, 15]},
                ]
            else:
                outline_data = [
                    {"title": "调研背景与核心发现", "pages": 2, "thesis": "...", "cases": 0, "tables": 0, "material_idxs": [1, 2, 3]},
                    {"title": "行业现状与趋势分析", "pages": 3, "thesis": "...", "cases": 0, "tables": 1, "material_idxs": [4, 5, 6]},
                    {"title": "核心维度深度分析", "pages": 4, "thesis": "...", "cases": 2, "tables": 2, "material_idxs": [7, 8, 9]},
                    {"title": "典型案例研究", "pages": 3, "thesis": "...", "cases": 3, "tables": 1, "material_idxs": [10, 11, 12]},
                    {"title": "结论与行动建议", "pages": 2, "thesis": "...", "cases": 0, "tables": 0, "material_idxs": [13, 14, 15]},
                ]

        # 计划书里的“执行附录/搜索批次”属于执行信息，不是报告正文
        outline_data = [
            ch for ch in outline_data
            if not re.search(r"执行附录|执行附件|搜索批次|检索计划|^\s*附", str(ch.get("title", "")))
        ] or outline_data
        outline_data = self._enforce_page_budget(outline_data, max_pages)
        total_pages = sum(ch.get("pages", 1) for ch in outline_data)

        outline = {"title": "调研报告", "chapters": outline_data}
        logger.info(f"[素材评估] 涌现{len(outline_data)}章，总页数{sum(ch.get('pages', 1) for ch in outline_data)}")
        return outline

    def generate_zero_shot_draft(self, task: str, outline: Dict) -> str:
        max_pages = int(self.cfg.get("max_pages", 15))
        page_cfg = get_page_config(max_pages)
        skeleton_words = page_cfg["skeleton_words"]
        max_tokens = page_cfg["max_tokens"]

        logger.info(f"[阶段4] 生成轻量级骨架草稿（素材驱动结构，{len(outline.get('chapters',[]))}章）...")

        chapter_budgets = []
        for ch in outline.get("chapters", []):
            words = int(ch.get("pages", 2) * 300 * 0.5)
            chapter_budgets.append({
                "title": ch["title"],
                "pages": ch.get("pages", 2),
                "words": words,
                "thesis": ch.get("thesis", ""),
                "tables": ch.get("tables", 0),
                "cases": ch.get("cases", 0),
            })

        budget_info = "\n".join([
            f"- {b['title']}: {b['words']}字骨架（约{b['pages']}页），需{b['tables']}个表格占位，{b['cases']}个案例占位"
            for b in chapter_budgets
        ])

        # 数据密集型章节自动添加图表标记
        chart_hint = ""
        data_chapters = []
        for b in chapter_budgets:
            if b['tables'] > 0 or any(kw in b['title'] for kw in ["规模", "数据", "竞争", "市场", "格局", "趋势"]):
                data_chapters.append(b['title'])
        if data_chapters:
            chart_hint = f"\n7. 【图表标记】以下数据密集型章节必须在骨架中插入【图表：xxx】标记：{', '.join(data_chapters[:3])}"

        prompt = f"""【角色】资深商业分析师
【任务】生成轻量级骨架草稿
【当前日期】{current_year_anchor()}
【报告页数期望】{max_pages}页
【骨架完成度】50%（只写标题+概述+占位，不写正文论述）
【章节结构】{budget_info}

⚠️ 【强制约束——违反视为不合格】
1. 【章节完整性】必须严格包含上述所有章节，不得遗漏、不得增删
2. 【标题层级】##仅用于报告总标题；###用于各章标题；####用于小节标题
3. 【骨架-only原则】每个小节只允许：小节标题 + 2-3句话概述（50字以内，说明本节论证方向）。严禁写长段落、案例细节、数据解释
4. 【表格占位】对比表格只画表头+1行示例（写"[数据待补充]"），绝不编造数字
5. 【内部标记禁令】正文中严禁出现：预算、约X页、[待填充]、[CITATION_NEEDED]、本章说明、本章论证、原始报告、素材
6. 【元叙述禁令】概述用>引用块写2-3句核心论点，禁止写"本章旨在..."等自指句子
7. 【字数上限】全文严格不超过{skeleton_words}字
8. 【禁止附录章】不得输出以「附：」开头的章节，不得把检索计划、搜索批次、使用说明写成独立章；用户需求未提风险时，不得新增风险提醒或风险章节
{chart_hint}

【原始需求】{task}"""

        draft = self._agent_call("planner", [{"role": "user", "content": prompt}], max_tokens=max_tokens)

        actual_chapters = len(re.findall(r'^###\s+', draft, re.MULTILINE))
        expected_chapters = len(chapter_budgets)

        self.zero_draft_issues = []
        if actual_chapters < expected_chapters - 1:
            self.zero_draft_issues.append({
                "type": "章节缺失",
                "severity": "high",
                "desc": f"期望约{expected_chapters}章，实际只有{actual_chapters}章",
                "suggest": "回炉重造骨架"
            })

        total_len = len(draft)
        if total_len > skeleton_words * 1.2:
            logger.warning(f"[骨架超预算] 全文{total_len}字，超出预算{skeleton_words}字的20%。")
        else:
            logger.info(f"[骨架完成] 全文{total_len}字，{actual_chapters}章")

        return draft

    def parse_outline_from_draft(self, draft: str, material_assessment: Dict) -> Dict:
        chapters = []
        chapter_matches = list(re.finditer(r"^###\s+(.+)$", draft, re.MULTILINE))
        if not chapter_matches:
            chapter_matches = list(re.finditer(r"^##\s+(.+)$", draft, re.MULTILINE))

        assessed_chapters = material_assessment.get("chapters", [])

        for idx, m in enumerate(chapter_matches):
            title = m.group(1).strip()
            title = re.sub(r"^#+\s*", "", title)
            title = re.sub(r"\*\*", "", title).strip()
            start = m.end()
            if idx + 1 < len(chapter_matches):
                end = chapter_matches[idx + 1].start()
            else:
                end = len(draft)
            section_text = draft[start:end]

            points = []
            overview = re.search(r"^>\s*(.+)$", section_text, re.MULTILINE)
            if overview:
                pt = overview.group(1).strip()
                if len(pt) > 80:
                    pt = pt[:77] + "..."
                points.append(pt)

            sub_sections = re.findall(r"^####\s*(.+)$", section_text, re.MULTILINE)
            for s in sub_sections[:3]:
                s_clean = re.sub(r"\*\*", "", s).strip()
                if len(s_clean) > 60:
                    s_clean = s_clean[:57] + "..."
                points.append(s_clean)

            if not points:
                bullets = [p.strip() for p in re.findall(r"^[\s]*[-*]\s+(.+)$", section_text, re.MULTILINE)[:4]]
                points = bullets if bullets else ["核心分析"]

            points = [p[:90] + "..." if len(p) > 90 else p for p in points[:4]]

            if idx < len(assessed_chapters):
                pages = assessed_chapters[idx].get("pages", 2)
                tables = assessed_chapters[idx].get("tables", 0)
                cases = assessed_chapters[idx].get("cases", 0)
                material_idxs = assessed_chapters[idx].get("material_idxs", [])
            else:
                pages = 2
                tables = 0
                cases = 0
                material_idxs = []

            # 自动检测图表需求
            chart_idea = ""
            if tables > 0 or any(kw in title for kw in ["规模", "数据", "竞争", "市场", "格局", "趋势"]):
                chart_idea = f"市场规模趋势图" if "规模" in title or "趋势" in title else f"{title[:10]}数据对比"

            chapters.append({
                "title": title, "points": points, "pages": pages,
                "tables": tables, "cases": cases, "material_idxs": material_idxs,
                "must_have_data": any(k in title for k in ["规模", "数据", "竞争", "市场", "转化", "链路"]),
                "chart_idea": chart_idea
            })

        if not chapters:
            chapters = [{"title": "核心发现", "points": ["关键数据"], "pages": 2, "tables": 0, "cases": 0, "material_idxs": [], "must_have_data": False, "chart_idea": ""}]

        max_pages = int(self.cfg.get("max_pages", 15))
        chapters = self._enforce_page_budget(chapters, max_pages)
        total_pages = sum(ch.get("pages", 1) for ch in chapters)
        logger.info(f"[页数校验] 已按期望 {max_pages} 页收敛，当前 {len(chapters)} 章 / {total_pages} 页")

        return {"title": "调研报告", "chapters": chapters}

    def _enforce_page_budget(self, chapters, max_pages):
        """按 max_pages 合并尾部章节并分配页数，避免页数控件失效。"""
        if not chapters or max_pages <= 0:
            return chapters

        if len(chapters) > max_pages:
            keep = chapters[:max_pages - 1]
            tail = chapters[max_pages - 1:]
            merged = {
                "title": "综合发现与展望",
                "points": [],
                "pages": 1,
                "tables": 0,
                "cases": 0,
                "material_idxs": [],
                "must_have_data": False,
                "chart_idea": "",
            }
            for ch in tail:
                merged["points"].extend(ch.get("points", []) or [])
                merged["material_idxs"].extend(ch.get("material_idxs", []) or [])
                merged["tables"] += ch.get("tables", 0)
                merged["cases"] += ch.get("cases", 0)
                merged["must_have_data"] = merged["must_have_data"] or ch.get("must_have_data", False)
            keep.append(merged)
            chapters = keep

        base = max(1, max_pages // len(chapters))
        for ch in chapters:
            ch["pages"] = base
        remainder = max_pages - base * len(chapters)
        for ch in chapters[:remainder]:
            ch["pages"] += 1
        return chapters

    def _auto_assign_chapter_materials(self):
        """Ollama向量搜索匹配素材→章节。通过 _mat_idx 直达 all_materials，
        回退时用关键词匹配兜底。"""
        if not self.all_materials:
            return

        chapters = self.outline.get("chapters", [])
        for idx, ch in enumerate(chapters):
            if idx == 0 and ("摘要" in ch.get("title", "") or "概要" in ch.get("title", "")
                             or "背景" in ch.get("title", "") or "发现" in ch.get("title", "")):
                continue

            existing = ch.get("material_idxs", [])
            if len(existing) >= 3:
                continue

            query = f"{ch.get('title', '')} {' '.join(ch.get('points', []))}"
            results = self.memory.search(query, top_k=10)

            matched_idxs = []
            for r in results:
                meta = r.get("meta", {})
                # 优先用 _mat_idx 直达（Ollama向量搜索路径）
                mat_idx = meta.get("_mat_idx")
                if mat_idx is not None and 0 <= mat_idx < len(self.all_materials):
                    matched_idxs.append(mat_idx + 1)  # 1-based
                    continue
                # 回退：URL/标题匹配
                r_url = meta.get("url", "")
                r_title = meta.get("title", "")
                for j, mat in enumerate(self.all_materials):
                    if (r_url and mat.url == r_url) or (r_title and mat.title == r_title):
                        matched_idxs.append(j + 1)
                        break

            # 向量搜索无结果时，回退关键词匹配
            if not matched_idxs:
                search_keywords = []
                title_kw = re.findall(r'[\u4e00-\u9fa5]{4}', ch.get("title", ""))
                search_keywords.extend(title_kw[:5])
                for pt in ch.get("points", [])[:3]:
                    pt_kw = re.findall(r'[\u4e00-\u9fa5]{3,4}', pt)
                    search_keywords.extend(pt_kw[:3])
                search_keywords = list(dict.fromkeys(search_keywords))[:12]
                for j, mat in enumerate(self.all_materials):
                    mat_text = f"{mat.title} {mat.content[:500]}"
                    if sum(1 for kw in search_keywords if kw in mat_text) >= 1:
                        matched_idxs.append(j + 1)
                matched_idxs = matched_idxs[:8]
                logger.info(f"[素材匹配] 第{idx+1}章关键词回退 {len(matched_idxs)}条")

            merged = list(dict.fromkeys(existing + matched_idxs))
            ch["material_idxs"] = merged
            logger.info(f"[素材匹配] 第{idx+1}章「{ch.get('title','')}」合计 {len(merged)} 条素材")

    def format_outline_controls(self, outline: Dict) -> str:
        """格式化控制面文本，同时保存到后台调试目录"""
        chapters = outline.get("chapters", [])
        total_pages = sum(ch.get("pages", 2) for ch in chapters)
        data_chapters = sum(1 for ch in chapters if ch.get("must_have_data"))

        warning_lines = []
        if self.zero_draft_issues:
            warning_lines.extend([
                "⚠️⚠️⚠️  骨架自检发现问题（建议优先处理）  ⚠️⚠️⚠️",
                "-" * 62,
            ])
            for issue in self.zero_draft_issues[:5]:
                sev_icon = "🔴" if issue["severity"] == "high" else "🟡"
                warning_lines.append(f"  {sev_icon} [{issue['type']}] {issue['desc']}")
                warning_lines.append(f"     → 建议: {issue['suggest']}")
                warning_lines.append("")
            warning_lines.extend(["=" * 62, ""])

        # Plan方向覆盖度展示
        coverage_lines = []
        if self.plan_coverage:
            coverage_lines = ["", "=" * 62, "  Plan调研方向覆盖度", "=" * 62]
            for key, info in self.plan_coverage.items():
                status_icon = "✅" if info["status"] == "充足" else ("⚠️" if info["status"] == "一般" else "❌")
                coverage_lines.append(f"  {status_icon} [{info['status']}] {info['name'][:50]} (命中:{info['count']})")
            coverage_lines.extend(["=" * 62, ""])

        lines = [
            "=" * 62,
            f"  报告控制面 —— {outline.get('title', '调研报告')}",
            "=" * 62,
            f"  总章节数: {len(chapters)} 章  |  总页数: {total_pages} 页  |  数据密集型: {data_chapters} 章",
            "-" * 62,
            "  调整说明：",
            "    改页数：第2章页数改5",
            "    加图表：第3章图表 市场份额饼图",
            "    删章节：删除第4章",
            "    调顺序：第2章和第3章交换",
            "    补素材：第2章补搜 竞争格局，市场份额（多个关键词用逗号分隔）",
            "=" * 62,
            "",
        ]
        lines = warning_lines + coverage_lines + lines

        for i, ch in enumerate(chapters, 1):
            pages = ch.get("pages", 2)
            chart = ch.get("chart_idea", "")
            points = ch.get("points", [])
            is_data = ch.get("must_have_data", False)
            bar = "#" * min(pages, 15) + "-" * (15 - min(pages, 15))

            mat_idxs = ch.get("material_idxs", [])
            mat_count = len(mat_idxs)
            mat_richness = min(mat_count * 20, 100)
            richness_bar = "█" * (mat_richness // 10) + "░" * (10 - mat_richness // 10)

            lines.append(f"{'-' * 62}")
            lines.append(f"  【第{i}章】{ch['title']}")
            lines.append(f"  页数: [{bar}] {pages}页 {'(数据密集型)' if is_data else ''}")

            if i == 1 and ("摘要" in ch['title'] or "概要" in ch['title'] or "背景" in ch['title']):
                lines.append(f"  素材: (摘要章，素材由全文汇总生成)")
            else:
                lines.append(f"  素材: [{richness_bar}] {mat_count}条相关素材")
                if mat_count < 3 and pages > 2:
                    lines.append(f"  🔴 硬阻断：素材不足，无法扩写！请补搜或合并后重试")

            if chart:
                lines.append(f"  图表: {chart}")
            if points:
                lines.append(f"  核心要点: {' | '.join(points[:3])}")
            lines.append(f"{'-' * 62}")

        lines.append("")
        lines.append("=" * 62)
        lines.append("请输入调整指令（或直接点【确认通过】）：")
        display = "\n".join(lines)
        try:
            ctrl_path = os.path.join(self.cfg["output_dir"], "后台调试", "control_panel.txt")
            os.makedirs(os.path.dirname(ctrl_path), exist_ok=True)
            with open(ctrl_path, "w", encoding="utf-8") as cf:
                cf.write(display)
        except OSError as e:
            logger.warning(f"[engine] 写入control_panel失败: {e}")
        return display

    def apply_outline_edits(self, outline: Dict, user_text: str) -> Dict:
        chapters = outline.get("chapters", [])
        modified = False

        # 1. 改页数
        for m in re.finditer(r"第(\d+)章.*?页数[:：]?\s*(\d+)", user_text):
            idx = int(m.group(1)) - 1
            pages = int(m.group(2))
            if 0 <= idx < len(chapters):
                chapters[idx]["pages"] = max(1, pages)
                modified = True

        # 2. 加图表
        for m in re.finditer(r"第(\d+)章.*?图表[:：]?\s*(.+?)(?:\n|$)", user_text):
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(chapters):
                chapters[idx]["chart_idea"] = m.group(2).strip()
                modified = True

        # 3. 补搜指令
        for m in re.finditer(r"第(\d+)章.*?补搜\s+(.+?)(?:\n|$)", user_text):
            idx = int(m.group(1)) - 1
            query = m.group(2).strip()
            if 0 <= idx < len(chapters):
                chapters[idx]["_supplement_query"] = query
                self._supplement_triggered = True
                modified = True

        # 4. 删章节（支持"删除第X章"或"删第X章"）
        for m in re.finditer(r"(?:删除|删).*?第(\d+)章", user_text):
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(chapters):
                deleted_title = chapters[idx].get("title", f"第{idx+1}章")
                chapters.pop(idx)
                logger.info(f"[控制面] 已删除第{idx+1}章「{deleted_title}」，剩余{len(chapters)}章")
                modified = True

        # 5. 调顺序（支持"第A章和第B章交换"或"第A章与第B章交换"）
        swap_match = re.search(r"第(\d+)章.*?和|与.*?第(\d+)章.*?交换", user_text)
        if swap_match:
            idx1 = int(swap_match.group(1)) - 1
            idx2 = int(swap_match.group(2)) - 1
            if 0 <= idx1 < len(chapters) and 0 <= idx2 < len(chapters) and idx1 != idx2:
                chapters[idx1], chapters[idx2] = chapters[idx2], chapters[idx1]
                logger.info(f"[控制面] 已交换第{idx1+1}章和第{idx2+1}章的顺序")
                modified = True

        if modified:
            outline["chapters"] = chapters
        return outline

    @staticmethod
    def _is_placeholder_chapter(text: str) -> bool:
        """检测章节正文是否为空或模型占位回复"""
        t = (text or "").strip()
        body = re.sub(r"^#+\s*第[^\n]*\n?", "", t).strip()
        if len(body) < 80:
            return True
        markers = ("未收到本章正文", "请提供后润色", "未收到报告", "无法提取核心发现", "本章无素材")
        return any(m in t for m in markers)

    @staticmethod
    def _chapter_char_count(text: str) -> int:
        """章节正文字数（剔除标题行和空白），用于页数达成校验。"""
        body = re.sub(r"^#{1,6}[^\n]*$", "", text or "", flags=re.MULTILINE)
        return len(re.sub(r"\s+", "", body))

    @staticmethod
    def _is_valid_search_query(q: str, min_len: int = 4) -> bool:
        """审稿意见/错误提示不是搜索词，避免把元叙述拿去联网检索。"""
        if not isinstance(q, str):
            return False
        q = q.strip()
        if not (min_len <= len(q) <= 60):
            return False
        noise = (
            "章节", "正文", "标题", "无法", "缺失", "未提供", "未收到", "请补充",
            "审查", "无内容", "不存在", "论证链条", "事实陈述", "素材", "#",
        )
        return not any(w in q for w in noise)

    # 通用商业词：几乎任何行业报告都会出现，不能作为相关性证据
    _GENERIC_WORDS = (
        "用户补充信息", "调研", "研究", "报告", "分析", "中国", "市场规模", "规模",
        "竞争格局", "格局", "行业", "市场", "份额", "趋势", "发展", "增长",
        "品牌", "企业", "数据", "亿元", "用户", "补充", "信息", "终端", "零售额",
        "销量", "营收", "利润", "增速", "均价", "单价", "毛利", "净利", "销售额", "渗透率",
        "包含", "形成", "一份", "可执行",
    )

    # 仅保留真正的尾部助词；连词/虚词已由正则切断，不能再剪“和/中/上”等品牌首字
    _FUNCTION_CHARS = "的了"
    # 2 字兜底时要排除的泛词
    _TWO_CHAR_STOP = {
        "深度", "相关", "进行", "可以", "以及", "通过", "目前", "今年", "去年",
        "头部", "主要", "重点", "情况", "问题", "方案", "策略", "模式", "结构",
        "价格", "产品", "服务", "客户", "渠道", "区域", "城市", "国内", "海外",
        "全球", "我国", "企业", "品牌",
    }

    # 业态关键词：搜索词只写行业名词（如“脑科学”）会命中无关标的，必须落到具体业态
    _BUSINESS_TERMS = (
        "门店加盟", "加盟费", "加盟", "直营", "连锁", "单店模型", "单店",
        "坪效", "店效", "SKU", "选址", "供应链", "经销商", "渠道",
        "零售", "检测服务", "客单价", "复购", "总部", "分层",
    )
    _CLARIFY_MARKERS = ("【用户补充信息】", "【澄清问题自动回答】")

    def _extract_clarify_context(self, text: str) -> str:
        """从澄清后的任务文本里截出用户回答部分，供后续 plan/search 复用。"""
        text = text or ""
        positions = [text.find(m) for m in self._CLARIFY_MARKERS if m in text]
        if not positions:
            return ""
        return text[min(positions):].strip()[:1500]

    def _business_form_keywords(self, limit: int = 6) -> list[str]:
        """从澄清回答/任务里提取业态关键词（不做 LLM 调用，保持确定性）。"""
        ctx = getattr(self, "clarify_context", "") or getattr(self, "refined_task", "")
        hits = [t for t in self._BUSINESS_TERMS if t in ctx]
        # 去掉被更长命中词包含的短词（如命中“单店模型”时不再重复加“单店”）
        hits = [t for t in hits if not any(t != o and t in o for o in hits)]
        return hits[:limit]

    def _compose_task_summary(self, task: str, limit: int = 2000) -> str:
        """截断需求摘要，但保证澄清回答不被截掉。"""
        task = task or ""
        if len(task) <= limit:
            return task
        ctx = (getattr(self, "clarify_context", "") or "")[:800]
        if ctx and ctx not in task[:limit]:
            head = task[: max(0, limit - len(ctx) - 20)].rstrip()
            return f"{head}\n\n【澄清阶段确认的方向】\n{ctx}"
        return task[:limit]

    def _inject_business_terms_into_batches(self, batches: List[Dict]) -> List[Dict]:
        """给缺少业态维度的批次改写一条查询，避免整批只搜行业名词。"""
        terms = self._business_form_keywords()
        if not terms or not batches:
            return batches
        for b in batches:
            queries = b.get("queries") or []
            if not queries or any(any(t in q for t in terms) for q in queries):
                continue
            suffix = " ".join(terms[:2])
            base = queries[0][: max(10, 60 - len(suffix) - 1)]
            queries[0] = f"{base} {suffix}"
        return batches

    def _extract_topic_keywords(self, task: str) -> list[str]:
        """任务开始时提取一次主题词，优先用模型，失败回退规则抽取。"""
        prompt = f"""从下面的调研任务中提取 3-6 个主题关键词，用中文逗号分隔。
每个关键词 2-4 个字，不要长词组或整句；只输出关键词，不要解释。
不要“市场/行业/报告/分析/趋势/规模/份额/数据”这类通用词。
调研任务：{task[:600]}"""
        try:
            raw = self.llm.fast(
                [{"role": "user", "content": prompt}],
                max_tokens=120,
                temperature=0.2,
            )
            words = []
            for part in re.split(r"[，,、;；\n]+", raw or ""):
                w = part.strip(" 。.：:*-")
                if not (2 <= len(w) <= 6):
                    continue
                if w in self._GENERIC_WORDS or w in self._TWO_CHAR_STOP:
                    continue
                words.append(w)
            words = list(dict.fromkeys(words))[:6]
            if words:
                logger.info(f"[主题词] 模型提取: {words}")
                return words
            logger.warning(f"[主题词] 模型输出未通过过滤，回退规则抽取: {(raw or '')[:80]!r}")
        except Exception as e:
            logger.warning(f"[主题词] 模型提取失败，回退规则抽取: {e}")
        # 模型偶发返回空/异常时用规则锚点兜底，避免主题词为空导致查询退化成任务碎片
        return self._task_anchors(task)[:4]

    @classmethod
    def _task_anchors(cls, reference: str) -> list[str]:
        """从调研任务里抽取主题锚点：剔除通用词后剩下的中文片段。"""
        ref = (reference or "")[:800]
        # 去掉“请撰写一份关于…”这类指令前缀
        ref = re.sub(
            r"^(请|麻烦|帮我|帮忙)?\s*(撰写|写|做|生成|输出|完成|开展|进行)?\s*(一份|一个|一下)?\s*(关于)?",
            "", ref,
        )
        for word in cls._GENERIC_WORDS:
            ref = ref.replace(word, "|")
        # 常见连词/虚词直接切断；句首的“和/或”保留，避免误伤“和路雪”这类品牌名
        ref = re.sub(r"(?<=[\u4e00-\u9fa5])[和或](?=[\u4e00-\u9fa5])", "|", ref)
        ref = re.sub(r"[的在与及对为从是有将把被让给向到于]", "|", ref)
        segments = []
        for a in re.split(r"[^\u4e00-\u9fa5]+", ref):
            a = a.strip(cls._FUNCTION_CHARS)
            # 超过 8 字的是句子碎片，不是主题词
            if 2 <= len(a) <= 8:
                segments.append(a)
        anchors = [a for a in dict.fromkeys(segments) if len(a) >= 3]
        # 2 字主题（白酒/芯片/犬粮）不因存在长锚点而被丢弃
        anchors.extend(
            a for a in dict.fromkeys(segments)
            if len(a) == 2 and a not in cls._TWO_CHAR_STOP
        )
        # 英文主题单独保留，供英文素材按词边界匹配
        for a in re.findall(r"[A-Za-z][A-Za-z0-9&+._-]{1,}", ref):
            if a.lower() not in {"the", "and", "for", "with", "report", "data", "market"}:
                anchors.append(a)
        return list(dict.fromkeys(anchors))[:40]

    def _material_matches(self, text: str, reference: str = "", claim_nature: str = "") -> bool:
        """素材与调研主题的相关性门槛，挡住跨行业误检结果。

        企业自述素材在抽取阶段已由模型判定过相关性，标题常被抽象成
        “产品线分医用与民用系列”这类不含主题词的表述，这里直接放行，
        否则会把产品矩阵、渠道结构等核心事实误杀。
        """
        if claim_nature == CLAIM_SELF_PUBLISHED:
            return True
        # 任务开始时提取的主题词优先；模型不可用时回退规则抽取
        anchors = getattr(self, "topic_keywords", None) or self._task_anchors(
            reference or self.refined_task
        )
        if not anchors:
            return True
        for a in anchors:
            if re.fullmatch(r"[A-Za-z0-9&+._-]+", a):
                if re.search(rf"(?<![A-Za-z0-9]){re.escape(a)}(?![A-Za-z0-9])", text, re.IGNORECASE):
                    return True
            elif a in text:
                return True
            elif len(a) >= 4 and any(a[i:i + 3] in text for i in range(len(a) - 2)):
                # 长主题词允许命中其 3 字连续子串（“儿童专注力”→“专注力”），
                # 否则素材只写“专注力”时会被误判为无关
                return True
        return False

    def _material_relevant(self, m, reference: str = "") -> bool:
        """按素材对象判断相关性，统一读取 claim_nature，避免调用点漏传。"""
        text = f"{getattr(m, 'title', '')} {getattr(m, 'content', '')[:2000]}"
        return self._material_matches(text, reference, getattr(m, "claim_nature", ""))

    @staticmethod
    def _material_text(meta: dict) -> str:
        """取素材正文：content 与 fact_card 中第一个非空白的值。"""
        for key in ("content", "fact_card"):
            v = (meta.get(key) or "").strip()
            if v:
                return v
        return ""

    @staticmethod
    def _material_has_content(m) -> bool:
        """素材是否有可用正文（content 或 fact_card 非空白）。"""
        meta = m.get('meta', {}) if isinstance(m, dict) else {}
        return bool(FusionResearchBot._material_text(meta))

    @staticmethod
    def _is_self_published(meta: dict) -> bool:
        """素材是否为企业自述/软文类信源。"""
        return (meta.get("claim_nature") == CLAIM_SELF_PUBLISHED
                or meta.get("source_type") == "promo")

    @staticmethod
    def _material_ref_no(meta: dict, fallback: int) -> int:
        """写作引用编号：优先用 all_materials 全局序号，保证与参考文献一致。"""
        idx = meta.get("_mat_idx")
        if isinstance(idx, int) and idx >= 0:
            return idx + 1
        return fallback + 1

    @staticmethod
    def _claim_prefix(meta: dict) -> str:
        """企业自述素材打标，提示写作层不得当作行业数据使用。"""
        return "[企业自述] " if FusionResearchBot._is_self_published(meta) else ""

    @staticmethod
    def _promo_group(meta: dict) -> str:
        """软文信源分组键：同域名视为同一品牌。"""
        url = (meta.get("url") or "").strip()
        domain = re.sub(r"^https?://", "", url).split("/")[0].lower().replace("www.", "")
        return domain or (meta.get("title") or "")[:20]

    @staticmethod
    def _merge_source_hint(meta: dict) -> str:
        """合并素材提示写作层这是同源多篇，避免被当成多个独立证据。"""
        n = len(meta.get("merged_sources") or [])
        return f"（同源合并 {n} 篇）" if n > 1 else ""

    def _merge_promo_sources(self, tm: List[Dict]) -> List[Dict]:
        """同一品牌的多篇软文合并为一条信源，避免被当成多源佐证。

        只合并企业自述/软文素材，第三方数据与研报不受影响。
        合并时保留最长一条的元信息，正文按去重后拼接（上限 2500 字），
        并在 merged_sources 中保留每条事实的原始出处。
        """
        groups: Dict[str, List[Dict]] = {}
        for m in tm:
            meta = m.get("meta", {})
            if self._is_self_published(meta):
                groups.setdefault(self._promo_group(meta), []).append(m)

        multi = {g: items for g, items in groups.items() if len(items) > 1}
        if not multi:
            return tm

        merged: Dict[str, Dict] = {}
        for g, items in multi.items():
            parts, seen, total = [], set(), 0
            for it in items:
                c = self._material_text(it.get("meta", {}))
                if not c or c[:60] in seen:
                    continue
                seen.add(c[:60])
                if total >= 2500:
                    break
                parts.append(c[:2500 - total])
                total += len(parts[-1])
            lead = max(items, key=lambda x: len(self._material_text(x.get("meta", {}))))
            meta = dict(lead.get("meta", {}))
            meta["content"] = "\n".join(parts) or meta.get("content", "")
            meta["source_group"] = g
            meta["merged_count"] = len(items)
            meta["merged_sources"] = [
                {"title": it.get("meta", {}).get("title", ""), "url": it.get("meta", {}).get("url", "")}
                for it in items
            ]
            merged[g] = {"score": lead.get("score", 0), "text": meta["content"], "meta": meta}

        result, emitted = [], set()
        for m in tm:
            meta = m.get("meta", {})
            if self._is_self_published(meta):
                g = self._promo_group(meta)
                if g in multi:
                    if g not in emitted:
                        emitted.add(g)
                        result.append(merged[g])
                    continue
            result.append(m)

        logger.info(
            f"[软文合并] 同品牌软文 {sum(len(v) for v in multi.values())} 条 → {len(multi)} 个信源"
        )
        return result

    def _rewrite_chapter_queries(self, ch: dict, max_queries: int = 3) -> list[str]:
        """章节补搜无结果时，换用更可搜的关键词组合。

        章节标题常是概念性表述（如“价格带与产品结构”），直接拿去搜索
        往往召回为空。这里让模型改写成“主题 + 具体指标”的关键词，并提供
        模板兜底，避免依赖单次 LLM 输出。
        """
        title = ch.get("title", "")
        points = " ".join(ch.get("points", []))
        anchors = self.topic_keywords or self._task_anchors(self.refined_task)
        topic = anchors[0] if anchors else self.refined_task[:20]
        biz_terms = self._business_form_keywords()
        biz_line = f"业态关键词：{'、'.join(biz_terms)}\n" if biz_terms else ""
        clarify = getattr(self, "clarify_context", "")

        prompt = f"""为下面的报告章节生成 {max_queries} 条联网搜索关键词，每条不超过 20 字。
只输出关键词，用换行分隔，不要写句子或解释。
每条关键词必须包含调研对象的业态/场景词，不能只用行业名词或公司名。

调研主题：{self.refined_task[:600]}
{clarify[:600]}
{biz_line}章节标题：{title}
章节要点：{points}"""

        queries: list[str] = []
        try:
            raw = self.llm.chat([{"role": "user", "content": prompt}], max_tokens=300, temperature=0.3)
            for line in (raw or "").splitlines():
                q = re.sub(r"^[\s\d.、\-•*#]+", "", line).strip().strip("`\"' ")
                if any(p in q for p in ("。", "，", "：", "#")):
                    continue
                if self._is_valid_search_query(q):
                    queries.append(q[:40])
        except Exception as e:
            logger.warning(f"[EXPAND] 关键词改写失败: {e}")

        # 模板兜底：主题 + 业态词 + 章节核心词 + 指标词
        title_core = title
        for word in self._GENERIC_WORDS:
            title_core = title_core.replace(word, " ")
        title_core = " ".join(title_core.replace("与", " ").split())
        biz = " ".join(t for t in biz_terms[:2] if t not in title_core)
        biz = f" {biz}" if biz else ""
        year = datetime.now().year
        templates = [
            f"{topic}{biz} {title_core} {year}",
            f"{topic}{biz} {points} {year} 数据",
            f"{topic}{biz} {title_core} 市场规模 份额 {year}",
        ]
        for t in templates:
            t = " ".join(t.split())[:40]
            if self._is_valid_search_query(t) and t not in queries:
                queries.append(t)
        return list(dict.fromkeys(queries))[:max_queries]

    def _write_ai_knowledge_chapter(self, ch: dict) -> str:
        """补搜无素材时，用模型自身知识填充章节，并在标题上明确标注。"""
        title = ch.get("title", "本章")
        points = " / ".join(ch.get("points", []))
        prompt = f"""【调研主题】{self.refined_task[:300]}
【章节标题】{title}
【素材状态】本章没有可核验的外部素材。
【任务】仅基于你自己的通用知识撰写本章正文，内容必须围绕调研主题展开。

【强制要求】
1. 不确定的具体数字不要编造，用“约”“量级”等定性表述
2. 不要标注引用编号，不要声称有外部来源
3. 覆盖章节要点：{points}
4. 输出 400-700 字，结构清晰，不写套话"""

        body = ""
        for _ in range(2):
            # 这里不能用 writer 角色（其系统提示要求基于素材与引用），直接走通用 chat
            raw = self.llm.chat(
                [{"role": "user", "content": prompt}],
                max_tokens=2500,
                temperature=0.4,
            )
            body = self._sanitize_chapter_output(raw)
            if not self._is_placeholder_chapter(body):
                break
        else:
            # 两轮都是占位内容：返回空串，由调用方记录失败并输出不带【AI训练知识】标记的占位
            logger.warning("[AI补章] 两轮均未产出有效正文，放弃标注")
            return ""

        # 去掉模型自带的标题行，统一由系统标注
        body = re.sub(r"^#{1,4}[^\n]*\n+", "", body.strip()).strip()
        body = re.sub(rf"^#{{1,4}}\s*{re.escape(title)}\s*$", "", body, flags=re.MULTILINE).strip()
        return (
            f"### 【AI训练知识】{title}\n\n"
            f"> 本章缺少可核验的外部素材，以下内容由 AI 训练知识生成，数据请谨慎引用。\n\n"
            f"{body}"
        )

    def write_chapters(self, final_draft: str):
        logger.info("[阶段6] 基于精化草稿扩写...")
        chapters = self.outline.get("chapters", [])
        # 断点恢复：已落盘的章节不再重写
        existing = self._state_store.load_chapters(len(chapters)) if self._state_store else {}

        def _w1(idx, ch):
            if idx in existing:
                logger.info(f"[任务状态] 第{idx+1}章已存在，复用已保存内容")
                return idx, existing[idx]
            logger.info(f"--- 第{idx+1}/{len(chapters)}章：{ch['title']} ---")
            midx = ch.get("material_idxs", [])
            if midx:
                tm = self.memory.get_by_ids([i-1 for i in midx if i > 0])
            else:
                q = f"{ch['title']} {' '.join(ch.get('points', []))}"
                tm = self.memory.search(q, top_k=10)

            # 只保留有正文的素材；“有条目但内容为空”不能算作有素材
            tm = [m for m in tm if self._material_has_content(m)]
            # 同品牌多篇软文合并为一条信源，避免“多篇即多源”的假象
            tm = self._merge_promo_sources(tm)

            # EXPAND素材缺口定向补搜（借鉴AgentCPM-Report）
            # 素材不足时，生成补搜查询→执行搜索→匹配新素材→再扩写
            expand_threshold = self.cfg.get("expand_min_materials", 3)
            empty_attempts = 0
            if self.cfg.get("enable_expand_gap_fill", True) and len(tm) < expand_threshold:
                if not self.state.has_budget():
                    logger.warning(
                        f"[EXPAND] 第{idx+1}章素材不足({len(tm)}<{expand_threshold})，"
                        f"但搜索预算已耗尽，未补搜"
                    )
                    if self._state_store and not tm:
                        self._state_store.optional_failed(
                            "chapter_materials",
                            RuntimeError(f"第{idx+1}章「{ch['title']}」素材不足且预算耗尽，未补搜"),
                        )
                else:
                    logger.info(f"[EXPAND] 第{idx+1}章素材不足({len(tm)}<{expand_threshold})，触发定向补搜...")
                    try:
                        gap_query = f"{ch['title']} {' '.join(ch.get('points', []))} 最新数据 案例"
                        supplement = self.searcher.multi_search([gap_query], max_results=5, workers=3)
                        supplement = [
                            m for m in (supplement or [])
                            if self._material_relevant(m, self.refined_task)
                        ]
                        if supplement:
                            self.all_materials.extend(supplement)
                            self.memory.add_fast(supplement)
                            self.state.consume_budget()
                            tm = self.memory.search(f"{ch['title']} {' '.join(ch.get('points', []))}", top_k=10)
                            tm = [
                                m for m in tm
                                if ((m.get('meta', {}).get('content') or m.get('meta', {}).get('fact_card') or '').strip())
                            ]
                            logger.info(f"[EXPAND] 补搜{len(supplement)}条，素材增至{len(tm)}条，预算剩余{self.state.search_budget}")
                        else:
                            empty_attempts += 1
                            logger.info(f"[EXPAND] 补搜无结果")
                    except Exception as e:
                        empty_attempts += 1
                        logger.warning(f"[EXPAND] 补搜失败: {e}")
            elif not self.cfg.get("enable_expand_gap_fill", True) and len(tm) < expand_threshold:
                logger.warning(f"[EXPAND] 第{idx+1}章素材不足({len(tm)})，但补搜开关已关闭")

            # 首轮补搜仍为空：失败不扣预算，所以换搜索词再试（最多2条）
            if not tm and self.cfg.get("enable_expand_gap_fill", True) and self.state.has_budget():
                for alt_query in self._rewrite_chapter_queries(ch)[:2]:
                    if not self.state.has_budget():
                        break
                    if hasattr(self.searcher, "should_skip") and self.searcher.should_skip(alt_query):
                        logger.info(f"[EXPAND] 替代查询已搜过，跳过: {alt_query}")
                        continue
                    logger.info(f"[EXPAND] 原查询无结果，改用替代查询: {alt_query}")
                    try:
                        supplement = self.searcher.multi_search([alt_query], max_results=5, workers=3)
                    except Exception as e:
                        empty_attempts += 1
                        logger.warning(f"[EXPAND] 替代查询失败: {e}")
                        continue
                    supplement = [
                        m for m in (supplement or [])
                        if self._material_relevant(m, self.refined_task)
                    ]
                    if not supplement:
                        empty_attempts += 1
                        logger.info(f"[EXPAND] 替代查询仍无结果: {alt_query}")
                        continue
                    self.all_materials.extend(supplement)
                    self.memory.add_fast(supplement)
                    self.state.consume_budget()
                    tm = self.memory.search(f"{ch['title']} {' '.join(ch.get('points', []))}", top_k=10)
                    tm = [m for m in tm if self._material_has_content(m)]
                    logger.info(
                        f"[EXPAND] 替代查询命中{len(supplement)}条，素材增至{len(tm)}条，"
                        f"预算剩余{self.state.search_budget}"
                    )
                    break

            # 补搜可能带回新的同品牌软文，进入写作前再合并一次
            tm = self._merge_promo_sources(tm)

            if len(tm) == 0:
                if self.cfg.get("enable_ai_knowledge_fallback", True):
                    logger.warning(
                        f"[写作] 第{idx+1}章补搜{empty_attempts}次无素材，"
                        f"改用 AI 训练知识填充（标题标注【AI训练知识】）"
                    )
                    draft = self._write_ai_knowledge_chapter(ch)
                    if draft:
                        self.global_context.update_from_chapter(draft, ch["title"], self.llm)
                        self._persist_chapter(idx, draft)
                        return idx, draft
                    msg = (
                        f"第{idx+1}章「{ch['title']}」补搜{empty_attempts}次无素材，"
                        f"AI 知识填充两轮均未产出有效正文。"
                        f"请在【控制面修改】输入关键词（逗号分隔），然后点击【补搜本章】重试"
                    )
                    logger.error(f"[写作] {msg}")
                    if self._state_store:
                        self._state_store.optional_failed("chapter_materials", RuntimeError(msg))
                    return idx, (
                        f"### {ch['title']}\n\n"
                        f"> 本章补搜{empty_attempts}次仍无素材，AI 知识填充未产出有效正文。"
                        f"请在【控制面修改】输入关键词（逗号分隔）后点击【补搜本章】重试。\n"
                    )

                msg = (
                    f"第{idx+1}章「{ch['title']}」补搜{empty_attempts}次仍无素材，"
                    f"且 AI 知识填充已关闭（enable_ai_knowledge_fallback=false）。"
                    f"请在【控制面修改】输入关键词（用逗号分隔），例如："
                    f"第{idx+1}章补搜 关键词1，关键词2，然后点击【补搜本章】重试"
                )
                logger.error(f"[写作] {msg}")
                if self._state_store:
                    self._state_store.optional_failed("chapter_materials", RuntimeError(msg))
                return idx, (
                    f"### {ch['title']}\n\n"
                    f"> 本章补搜{empty_attempts}次仍无素材，AI 知识填充已关闭。"
                    f"请在【控制面修改】输入关键词（逗号分隔）后点击【补搜本章】重试。\n"
                )

            mt = "\n".join([
                f"[{self._material_ref_no(m['meta'], i)}] {self._claim_prefix(m['meta'])}{m['meta'].get('title','')}"
                f"{self._merge_source_hint(m['meta'])}: "
                f"{self._sanitize_skeleton(smart_truncate(self._material_text(m['meta']), 800))}"
                for i, m in enumerate(tm)
            ])

            # 素材分层注入 — 核心素材完整 + 语义搜索补充
            if self._cluster_reports:
                mt = self._format_layered_materials(tm, ch, idx, mt)

            if len(tm) == 0 and ch.get("pages",2) > 1 and not ("摘要" in ch.get("title","") or "发现" in ch.get("title","")):
                if self.cfg.get("enable_ai_knowledge_fallback", True):
                    logger.warning(f"[写作] 第{idx+1}章补搜后无素材，改用 AI 训练知识兜底（标题标注【AI训练知识】）")
                    draft = self._write_ai_knowledge_chapter(ch)
                    if draft:
                        self.global_context.update_from_chapter(draft, ch["title"], self.llm)
                        self._persist_chapter(idx, draft)
                        return idx, draft
                return idx, f"### {ch['title']}\n\n> 本章无素材，扩写跳过。\n"
            sk = self._sanitize_skeleton(self._extract_section_from_draft(final_draft, ch["title"]))
            dr = "\n8. 数据密度：至少5个数字、2个品牌、1个案例，数据需有素材支撑" if len(tm) >= 3 else ""
            mc = sum(len((m.get('meta', {}).get('content') or m.get('meta', {}).get('fact_card') or '')) for m in tm)
            target_pages = max(1, int(ch.get("pages", 2) or 2))
            target_words = target_pages * 600  # 1页≈600字，用于篇幅达成校验
            writer_tokens = max(4000, min(8000, int(target_words * 2)))
            p = f"""【章节标题】【{ch['title']}】
【素材】{len(tm)}条/{mc}字  【骨架】{smart_truncate(sk,800)}
【本章目标】约{target_pages}页，正文不少于{target_words}字（不含标题与空白）
【素材列表】{mt}
【铁律】基于素材写，标注编号，每个小节最多1个表格，案例四要素，严禁套话
【自述素材】标[企业自述]的素材只用于描述友商在做什么，不得用于论证市场规模、增速或行业趋势；引用其数字须注明“企业自述”
{dr}"""
            draft = self._sanitize_chapter_output(self._agent_call("writer", [{"role":"user","content":p}], max_tokens=writer_tokens))
            if self._is_placeholder_chapter(draft):
                logger.warning(f"[写作] 第{idx+1}章疑似占位/空正文，重试一次")
                retry_p = p + "\n【重要】直接输出本章正文，禁止回复“未收到”“请提供内容”等占位句。"
                draft = self._sanitize_chapter_output(self._agent_call("writer", [{"role":"user","content":retry_p}], max_tokens=writer_tokens))
            if self._is_placeholder_chapter(draft):
                if self.cfg.get("enable_ai_knowledge_fallback", True):
                    logger.warning(
                        f"[写作] 第{idx+1}章两轮均为占位，改用 AI 训练知识兜底（标题标注【AI训练知识】）"
                    )
                    draft = self._write_ai_knowledge_chapter(ch)
                    if draft:
                        self.global_context.update_from_chapter(draft, ch["title"], self.llm)
                        self._persist_chapter(idx, draft)
                        return idx, draft
                raise RuntimeError(f"第{idx+1}章正文生成失败（占位或空正文）：{draft[:80]}")
            # 返回校验：按 页数×600 字判定篇幅，不足时扩写重试一次
            if self._chapter_char_count(draft) < target_words:
                logger.warning(
                    f"[写作] 第{idx+1}章字数不足（{self._chapter_char_count(draft)}<{target_words}），扩写重试一次"
                )
                retry_p = p + (
                    f"\n【篇幅不足】当前正文约{self._chapter_char_count(draft)}字，"
                    f"请在不编造数据的前提下扩写到不少于{target_words}字，补充机制拆解、口径说明与分场景对比。"
                )
                longer = self._sanitize_chapter_output(
                    self._agent_call("writer", [{"role":"user","content":retry_p}], max_tokens=writer_tokens)
                )
                if not self._is_placeholder_chapter(longer) and self._chapter_char_count(longer) > self._chapter_char_count(draft):
                    draft = longer
            # 超限收敛：超过目标 1.15 倍时精简一次，避免素材充足后总页数失控
            over = self._chapter_char_count(draft)
            if over > target_words * 1.15:
                logger.warning(f"[写作] 第{idx+1}章字数超限（{over}>{int(target_words * 1.15)}），精简一次")
                trim_p = (
                    f"请把下面这一章从约{over}字压缩到{target_words}字左右。"
                    "必须保留全部数据、编号引用与案例四要素，只删除重复论述、套话与同义复述，直接输出正文：\n\n"
                    + draft
                )
                trimmed = self._sanitize_chapter_output(
                    self._agent_call("writer", [{"role": "user", "content": trim_p}], max_tokens=writer_tokens)
                )
                if not self._is_placeholder_chapter(trimmed) \
                        and self._chapter_char_count(trimmed) >= target_words * 0.6:
                    draft = trimmed
            # 扩写后仍明显不足：预算允许时定向补搜一次并重写，避免章节空心化
            if self._chapter_char_count(draft) < target_words * 0.6 \
                    and self.cfg.get("enable_expand_gap_fill", True) and self.state.has_budget():
                logger.warning(f"[写作] 第{idx+1}章扩写后仍不足目标60%，尝试定向补搜重写")
                try:
                    hits = []
                    for alt_query in self._rewrite_chapter_queries(ch, max_queries=2):
                        if not self.state.has_budget():
                            break
                        got = self.searcher.multi_search([alt_query], max_results=5, workers=3) or []
                        got = [
                            m for m in got
                            if self._material_relevant(m, self.refined_task)
                        ]
                        if got:
                            self.state.consume_budget()
                            hits.extend(got)
                            break
                    if hits:
                        self.all_materials.extend(hits)
                        self.memory.add_fast(hits)
                        tm_new = self.memory.search(
                            f"{ch['title']} {' '.join(ch.get('points', []))}", top_k=10
                        )
                        tm_new = [m for m in tm_new if self._material_has_content(m)]
                        tm_new = self._merge_promo_sources(tm_new)
                        if len(tm_new) > len(tm):
                            mt_new = "\n".join([
                                f"[{self._material_ref_no(m['meta'], i)}] {self._claim_prefix(m['meta'])}{m['meta'].get('title','')}"
                                f"{self._merge_source_hint(m['meta'])}: "
                                f"{self._sanitize_skeleton(smart_truncate(self._material_text(m['meta']), 800))}"
                                for i, m in enumerate(tm_new)
                            ])
                            mc_new = sum(
                                len(self._material_text(m.get('meta', {})))
                                for m in tm_new
                            )
                            p = p.replace(mt, mt_new).replace(f"{len(tm)}条/{mc}字", f"{len(tm_new)}条/{mc_new}字")
                            longer = self._sanitize_chapter_output(
                                self._agent_call("writer", [{"role": "user", "content": p}], max_tokens=writer_tokens)
                            )
                            if not self._is_placeholder_chapter(longer) and self._chapter_char_count(longer) > self._chapter_char_count(draft):
                                draft = longer
                                logger.info(f"[写作] 第{idx+1}章补搜重写后 {self._chapter_char_count(draft)} 字")
                except Exception as e:
                    logger.warning(f"[写作] 第{idx+1}章定向补搜失败: {e}")
            # 所有扩写/重写完成后统一校验，避免未校验的替换稿直接落盘
            if self.cfg.get("enable_self_check_writing", True):
                draft = self._factcheck_chapter(draft, ch["title"], idx)
            draft = self._sanitize_chapter_output(self.critic_and_rewrite(draft, ch["title"]))
            if self.cfg.get("enable_fabrication_check", False):
                draft = self._fabrication_check(draft, ch["title"])
            self.global_context.update_from_chapter(draft, ch["title"], self.llm)
            self._persist_chapter(idx, draft)
            return idx, draft

        # 重试时 chapters_md 可能已有部分内容，先清空，已落盘章节由 existing 复用
        self.chapters_md = []
        for i, ch in enumerate(chapters):
            _, d = _w1(i, ch)
            self.chapters_md.append(d)

        if not self.chapters_md or not any(c.strip() for c in self.chapters_md):
            raise RuntimeError("章节写作未产出任何内容")

        if len(self.chapters_md) > 1:
            logger.info("[阶段7] 执行经理(EditorAgent)全篇统稿...")
            self.chapters_md = self._editorial_review(self.chapters_md)

    def _sanitize_output(self, text: str) -> str:
        if not text:
            return ""
        text = re.sub(r'\\(#{1,4})\s+', r'\1 ', text)
        text = re.sub(r'\\(#{1,4})$', r'\1', text, flags=re.MULTILINE)
        text = re.sub(r'（预算[^）]+）', '', text)
        text = re.sub(r'\[原始报告[^\]]*\]', '', text)
        text = re.sub(r'\[素材[^\]]*\]', '', text)
        text = re.sub(r'\[待填充\]', '', text)
        text = re.sub(r'\[CITATION_NEEDED\]', '', text)
        text = re.sub(r'>?\s*本章(?:说明|论证|旨在|聚焦|拆解)[：:]\s*', '', text)
        text = re.sub(r'请提供报告要点[^\n]*', '', text)
        return text.strip()

    def _sanitize_skeleton(self, text: str) -> str:
        return self._sanitize_output(text)

    def _sanitize_chapter_output(self, text: str) -> str:
        return self._sanitize_output(text)

    def chapter_level_denoise(self, chapter_md: str, chapter_title: str) -> str:
        if self._is_placeholder_chapter(chapter_md):
            logger.info(f"[TTD-DR去噪] {chapter_title} 正文为空，跳过补搜")
            return chapter_md
        if "【AI训练知识】" in (chapter_md or ""):
            logger.info(f"[TTD-DR去噪] {chapter_title} 为 AI 训练知识章节，跳过补搜与改写")
            return chapter_md
        logger.info(f"[TTD-DR去噪] {chapter_title}")

        prompt = f"""【任务】审查本章，识别缺乏数据支撑的论点并给出补搜建议
【本章内容】{smart_truncate(chapter_md, 3000)}

【审查标准】
1. 识别所有无数据支撑的主观论断
2. 识别所有缺少案例的泛泛而谈
3. 识别所有缺少来源标注的关键数据
4. 给出具体的搜索查询建议

【输出要求】
- 只输出纯JSON数组，不要解释文字
- 每个条目包含三要素
- 标准格式：
[{{"claim":"原文中具体的论断","missing":"需要补充的数据/案例/来源类型","query":"具体的搜索查询建议"}}]"""

        try:
            raw = self._agent_call("factcheck", [{"role": "user", "content": prompt}], max_tokens=800, json_mode=True)
            gaps = safe_json_loads(raw, [])
            if not isinstance(gaps, list):
                gaps = []
        except Exception:
            gaps = []

        if not gaps:
            return chapter_md

        queries = [g["query"] for g in gaps if self._is_valid_search_query(g.get("query", ""))]
        if not queries:
            return chapter_md

        new_mats, executed_queries = self._search_within_budget(
            queries, max_results=3, workers=3)
        if not executed_queries:
            logger.warning(f"[定向补搜] 预算不足，跳过（剩余{self.state.search_budget}）")
            return chapter_md

        logger.info(f"[定向补搜] {chapter_title} 缺口{len(queries)}个，执行补搜（命中后才扣预算）")
        new_mats = [
            m for m in (new_mats or [])
            if self._material_relevant(m, self.refined_task)
        ]

        if new_mats:
            cost = self._charge_search(executed_queries)
            logger.info(f"[定向补搜] 命中{len(new_mats)}条，消耗{cost}预算，剩余{self.state.search_budget}")
            self.all_materials.extend(new_mats)
            self.memory.add_fast(new_mats)

            evidence = "\n".join([f"[{m.title}] {m.content[:400]}" for m in new_mats[:5]])
            rewrite_prompt = f"""【任务】用新素材补充本章缺失的数据和案例，保持原有结构
【原则】只补充不重写，保留原有章节结构和核心论点

【原章节内容】{chapter_md[:3000]}
【补充素材】{evidence}

【执行要求】
1. 将新素材中的数据、案例自然融入对应位置
2. 为新增内容标注来源编号
3. 不改变原有章节的标题、结构、逻辑顺序
4. 不删除原有已验证的内容
5. 确保补充后内容流畅自然"""
            return self._agent_call("writer", [{"role": "user", "content": rewrite_prompt}], max_tokens=4000)

        return chapter_md

    def _factcheck_chapter(self, chapter_text: str, chapter_title: str, chapter_idx: int) -> str:
        if self._is_placeholder_chapter(chapter_text):
            return chapter_text
        if "【AI训练知识】" in (chapter_text or ""):
            return chapter_text
        check_prompt = f"""【任务】审查章节【{chapter_title}】的质量问题

【审查维度】
1. 是否存在未解决的[CITATION_NEEDED]标记？
2. 本章论点是否与前面章节存在矛盾？
3. 是否有关键数据缺失，需要补充搜索？
4. 是否存在AI套话或元叙述？
5. 引用标注是否规范完整？

【输出要求】
- 只输出纯JSON对象，不要解释文字
- 标准格式：
{{"need_search": true/false, "issues": ["具体问题1","具体问题2"], "queries": ["搜索查询1","搜索查询2"]}}
- 如无问题，issues和queries返回空数组"""
        try:
            raw = self._agent_call("factcheck", [{"role": "user", "content": check_prompt}], max_tokens=400, json_mode=True)
            check = safe_json_loads(raw, {})
            if not isinstance(check, dict):
                return chapter_text
        except Exception:
            return chapter_text

        if not check.get("need_search", False):
            return chapter_text

        queries = [q for q in check.get("queries", []) if self._is_valid_search_query(q)]
        if not queries:
            return chapter_text

        logger.info(f"[自主回溯] {chapter_title} 自检发现缺口，定向补搜：{queries}")
        extra_mats, executed_queries = self._search_within_budget(queries, max_results=2, workers=3)
        extra_mats = [
            m for m in (extra_mats or [])
            if self._material_relevant(m, self.refined_task)
        ]
        if extra_mats:
            self._charge_search(executed_queries)
            self.memory.add_fast(extra_mats)
            self.all_materials.extend(extra_mats)
            evidence = "\n".join([f"[{m.title}] {m.content[:300]}" for m in extra_mats[:5]])
            # 优化润色prompt
            refine_prompt = f"""用新素材润色章节内容。

【任务】
1. 将新素材中的关键数据/案例融入章节
2. 保持原有结构和逻辑不变
3. 数据类信息直接引用，观点类信息注明来源
4. 如新素材无法补充任何内容，保持原样

【约束】
- 不改变原有章节长度
- 不添加章节中没有的新论点
- 数据必须精确引用

章节原文：
{chapter_text[:4000]}

新素材：
{evidence}

输出：润色后的完整章节内容"""
            return self._agent_call("writer", [{"role": "user", "content": refine_prompt}], max_tokens=3000)
        return chapter_text

    def _fabrication_check(self, chapter_text: str, chapter_title: str) -> str:
        """轻量级编造检测"""
        if not self.cfg.get("enable_fabrication_check", False):
            return chapter_text
        if not chapter_text or not self.all_materials:
            return chapter_text
        mats = []
        for i, m in enumerate(self.all_materials[:15]):
            t = getattr(m, "title", "") or ""
            c = getattr(m, "content", "") or ""
            mats.append(f"[{i+1}] {t}: {smart_truncate(c, 300)}")
        mt = "\n".join(mats)
        if not mt:
            return chapter_text
        prompt = f"""提取5个关键事实断言，判断是否在素材中有依据
【章节】{chapter_title}
【内容】{smart_truncate(chapter_text, 4000)}
【素材】{smart_truncate(mt, 5000)}
输出JSON: [{{"claim":"断言","found_in_source":true/false}}]"""
        try:
            raw = self._agent_call("evaluator", [{"role":"user","content":prompt}], max_tokens=800, json_mode=True)
            chk = safe_json_loads(raw, []) if isinstance(raw, str) and raw.strip().startswith("[") else []
            if not isinstance(chk, list): return chapter_text
        except Exception:
            return chapter_text
        ok = sum(1 for c in chk if c.get("found_in_source"))
        n = len(chk)
        r = ok / max(n, 1) if n > 0 else 1.0
        logger.info(f"[编造检测] {chapter_title} 可信度{r:.0%}")
        if r < 0.4:
            u = [c for c in chk if not c.get("found_in_source")]
            dd = "".join(f"\n- {str(c.get(chr(99)+chr(108)+chr(97)+chr(105)+chr(109),chr(39)))[:80]}" for c in u[:3])
            chapter_text += f"\n\n> **编造检测**：仅{r:.0%}事实有依据，请人工核实。{dd}"
        return chapter_text

    def critic_and_rewrite_legacy(self, chapter_text: str, chapter_title: str) -> str:
        """备份：原单人审稿逻辑（保留作为回退）。"""
        critic_prompt = f"""【角色】极其挑剔的资深审稿人
【任务】对章节进行严格质量审查

【章节标题】{chapter_title}
【章节内容】{smart_truncate(chapter_text, 6000)}

【审查维度】
- fact_error：事实错误、数据错误
- logic_gap：逻辑漏洞、论证不充分
- data_conflict：数据冲突、前后不一致
- missing_citation：缺少引用、来源不明
- vague_claim：模糊论断、无数据支撑

【输出要求】
- 只输出纯JSON对象，不要任何其他文字
- 确保JSON语法100%正确
- 标准格式：
{{
  "issues": [
    {{
      "type": "fact_error / logic_gap / data_conflict / missing_citation / vague_claim",
      "location": "涉及的具体句子原文",
      "description": "具体问题描述，指出错在哪里",
      "query": "可选：用于补搜的简短关键词（不超过20字，只写关键词，不要写问题描述）",
      "severity": "high / medium / low"
    }}
  ],
  "rewrite_needed": true/false,
  "overall_comment": "总体评价（50字以内）"
}}"""
        try:
            raw = self._agent_call("factcheck", [{"role": "user", "content": critic_prompt}], max_tokens=1500, json_mode=True)
            critique = safe_json_loads(raw, {})
            if not isinstance(critique, dict):
                return chapter_text
        except Exception:
            return chapter_text

        issues = critique.get("issues", [])
        rewrite_needed = critique.get("rewrite_needed", False)
        if not rewrite_needed or not issues:
            return chapter_text

        serious = [i for i in issues if i.get("severity") in ("high", "medium")]
        if not serious:
            return chapter_text

        logger.info(f"[审稿.legacy] {chapter_title} 发现 {len(serious)} 个问题，触发重写")
        return self._do_rewrite(chapter_text, chapter_title, serious)

    def critic_and_rewrite(self, chapter_text: str, chapter_title: str) -> str:
        """ArenaRL式双向审稿验证。

        双向评分消除LLM-as-Judge的位置偏见（Position Bias）：
        1. 正向审稿：按原始顺序审稿
        2. 反向审稿：段落顺序颠倒后审稿
        3. 取交集：两份审稿意见的交集作为最终问题列表
        4. 回退：若双向审稿失败，回退到legacy单人审稿

        Args:
            chapter_text: 章节内容
            chapter_title: 章节标题

        Returns:
            修正后的章节内容（或原文）
        """
        if "【AI训练知识】" in (chapter_text or ""):
            return chapter_text
        critic_prompt_fwd = f"""【角色】极其挑剔的资深审稿人
【任务】对章节进行严格质量审查

【章节标题】{chapter_title}
【章节内容】{smart_truncate(chapter_text, 6000)}

【审查维度】
- fact_error：事实错误、数据错误
- logic_gap：逻辑漏洞、论证不充分
- data_conflict：数据冲突、前后不一致
- missing_citation：缺少引用、来源不明
- vague_claim：模糊论断、无数据支撑

【输出要求】
- 只输出纯JSON对象，不要任何其他文字
- 确保JSON语法100%正确
- 标准格式：
{{
  "issues": [
    {{
      "type": "fact_error / logic_gap / data_conflict / missing_citation / vague_claim",
      "location": "涉及的具体句子原文",
      "description": "具体问题描述，指出错在哪里",
      "query": "可选：用于补搜的简短关键词（不超过20字，只写关键词，不要写问题描述）",
      "severity": "high / medium / low"
    }}
  ],
  "rewrite_needed": true/false,
  "overall_comment": "总体评价（50字以内）"
}}"""

        # 反向审稿：段落顺序颠倒
        paragraphs = [p.strip() for p in chapter_text.split("\n\n") if p.strip()]
        if len(paragraphs) >= 2:
            reversed_text = "\n\n".join(paragraphs[::-1])
            critic_prompt_rev = f"""【角色】极其挑剔的资深审稿人
【任务】对章节进行严格质量审查（注意：段落顺序已随机打乱，请专注内容本身而非段落顺序）

【章节标题】{chapter_title}
【章节内容】{smart_truncate(reversed_text, 6000)}

【审查维度】
- fact_error：事实错误、数据错误
- logic_gap：逻辑漏洞、论证不充分
- data_conflict：数据冲突、前后不一致
- missing_citation：缺少引用、来源不明
- vague_claim：模糊论断、无数据支撑

【输出要求】
- 只输出纯JSON对象，不要任何其他文字
- 确保JSON语法100%正确
- 标准格式：
{{
  "issues": [
    {{
      "type": "fact_error / logic_gap / data_conflict / missing_citation / vague_claim",
      "location": "涉及的具体句子原文",
      "description": "具体问题描述，指出错在哪里",
      "query": "可选：用于补搜的简短关键词（不超过20字，只写关键词，不要写问题描述）",
      "severity": "high / medium / low"
    }}
  ],
  "rewrite_needed": true/false,
  "overall_comment": "总体评价（50字以内）"
}}"""
        else:
            # 段落太少，无法做双向验证，直接用legacy
            return self.critic_and_rewrite_legacy(chapter_text, chapter_title)

        # 并行执行双向审稿
        fwd_critique = None
        rev_critique = None
        try:
            raw_fwd = self._agent_call("factcheck", [{"role": "user", "content": critic_prompt_fwd}], max_tokens=1500, json_mode=True)
            fwd_critique = safe_json_loads(raw_fwd, {})
        except Exception as e:
            logger.warning(f"[审稿] 正向审稿失败: {e}")

        try:
            raw_rev = self._agent_call("factcheck", [{"role": "user", "content": critic_prompt_rev}], max_tokens=1500, json_mode=True)
            rev_critique = safe_json_loads(raw_rev, {})
        except Exception as e:
            logger.warning(f"[审稿] 反向审稿失败: {e}")

        # 任意一方失败，回退
        if not isinstance(fwd_critique, dict) or not isinstance(rev_critique, dict):
            logger.info("[审稿] 双向审稿不完整，回退legacy")
            return self.critic_and_rewrite_legacy(chapter_text, chapter_title)

        fwd_issues = fwd_critique.get("issues", [])
        rev_issues = rev_critique.get("issues", [])
        fwd_needed = fwd_critique.get("rewrite_needed", False)
        rev_needed = rev_critique.get("rewrite_needed", False)

        # 取交集：两份审稿同时发现的问题（更可信）
        # 如果两份审稿在同一个问题上达成一致（描述相似），保留
        fwd_desc_set = {i.get("description", "")[:50] for i in fwd_issues if i.get("description")}
        rev_desc_set = {i.get("description", "")[:50] for i in rev_issues if i.get("description")}

        consensus_issues = []
        for issue in fwd_issues:
            desc_prefix = issue.get("description", "")[:50]
            if desc_prefix in rev_desc_set:
                consensus_issues.append(issue)
            # 也检查severity=high的，即使反向没发现（保守策略）
            elif issue.get("severity") == "high":
                consensus_issues.append(issue)

        # 同样检查反向的high问题
        for issue in rev_issues:
            desc_prefix = issue.get("description", "")[:50]
            if issue.get("severity") == "high" and desc_prefix not in fwd_desc_set:
                consensus_issues.append(issue)

        # 去重
        seen = set()
        deduped = []
        for issue in consensus_issues:
            key = issue.get("description", "")[:50]
            if key not in seen:
                seen.add(key)
                deduped.append(issue)
        consensus_issues = deduped

        logger.info(
            f"[审稿.双向] 正向:{len(fwd_issues)}个问题, 反向:{len(rev_issues)}个问题, "
            f"共识:{len(consensus_issues)}个问题"
        )

        # 双方都认为不需要重写，且没有共识问题
        if (not fwd_needed and not rev_needed) or not consensus_issues:
            return chapter_text

        serious = [i for i in consensus_issues if i.get("severity") in ("high", "medium")]
        if not serious:
            return chapter_text

        logger.info(f"[审稿.双向] {chapter_title} 共识问题 {len(serious)} 个，触发重写")
        return self._do_rewrite(chapter_text, chapter_title, serious)

    def _do_rewrite(self, chapter_text: str, chapter_title: str, serious: list) -> str:
        """审稿后重写逻辑（抽取为独立方法，供双向和legacy共用）。"""
        import json
        extra_queries = []
        for issue in serious[:3]:
            # 只接受审稿意见里显式给出的搜索词；描述文本是元叙述，不能直接联网
            q = issue.get("query") or issue.get("search_query") or ""
            if self._is_valid_search_query(q):
                extra_queries.append(q[:60])

        extra_mats = []
        if extra_queries:
            extra_mats, executed_queries = self._search_within_budget(extra_queries, max_results=2, workers=3)
            extra_mats = [
                m for m in (extra_mats or [])
                if self._material_relevant(m, self.refined_task)
            ]
            if extra_mats:
                self._charge_search(executed_queries)
                self.memory.add_fast(extra_mats)
                self.all_materials.extend(extra_mats)

        _nl = "\n"
        _extra = _nl.join([f"[{m.title}] {m.content[:300]}" for m in extra_mats[:5]])
        fix_prompt = f"""【角色】资深商业分析师
【任务】根据审稿意见和新素材重写章节

【章节标题】{chapter_title}
【原章节内容】{chapter_text[:5000]}
【审稿意见】{json.dumps(serious, ensure_ascii=False, indent=2)}
【新素材】{_extra}

【重写要求】
1. 必须解决所有high/medium级别问题
2. 保留原有章节结构和核心论点
3. 补充缺失的引用或数据，标注来源
4. 修正事实错误和逻辑漏洞
5. 输出完整重写后的章节内容
6. 不添加审稿意见中未提及的新内容"""
        rewritten = self._agent_call("writer", [{"role": "user", "content": fix_prompt}], max_tokens=4000)
        return rewritten

    def _format_layered_materials(self, tm: List[Dict], ch: Dict, idx: int, orig_mt: str) -> str:
        """
        素材分层注入 — 格式化章节素材列表

        三层结构：
        1. 核心素材（对应簇的前 5 条）：完整内容（smart_truncate 到 2000 字）
        2. 语义搜索补充（HybridMemoryStore 向量+BM25 双搜索）：从全量拉 top 3
        3. 其余素材：180 字摘要（ContextCompressor 风格）

        Args:
            tm: 当前章节的素材列表（来自 memory.get_by_ids 或 memory.search）
            ch: 章节信息
            idx: 章节索引
            orig_mt: 原始格式化文本（回退用）

        Returns:
            分层格式化后的素材文本
        """
        try:
            # 收集核心素材的 URL（用于识别）
            core_urls = set()
            for cluster in (self._clusterer.get_cached_clusters() or []):
                for cm in cluster.core_materials:
                    url = MaterialSynthesizer._extract_field(cm, "url", "")
                    if url:
                        core_urls.add(url)

            # 分层处理
            core_parts = []
            semantic_parts = []
            other_parts = []

            for i, m in enumerate(tm):
                meta = m.get("meta", {})
                m_url = meta.get("url", "")
                title = meta.get("title", "")
                content = self._material_text(meta)
                hint = self._merge_source_hint(meta)

                if m_url and m_url in core_urls:
                    # 核心素材：完整内容
                    core_parts.append(
                        f"[{self._material_ref_no(meta, i)}] [核心] {self._claim_prefix(meta)}{title}{hint}: {self._sanitize_skeleton(smart_truncate(content, 2000))}"
                    )
                else:
                    # 其余素材：180 字摘要（ContextCompressor 风格）
                    other_parts.append(
                        f"[{self._material_ref_no(meta, i)}] {self._claim_prefix(meta)}{title}{hint}: {self._sanitize_skeleton(smart_truncate(content, 180))}"
                    )

            # 语义搜索补充：从全量素材中拉取 top 3
            if self.cfg.get("layered_semantic_supplement", True):
                q = f"{ch.get('title', '')} {' '.join(ch.get('points', []))}"
                supp_results = self.memory.search(q, top_k=3)
                existing_urls = {m.get("meta", {}).get("url", "") for m in tm}
                for si, sr in enumerate(supp_results):
                    sr_url = sr.get("meta", {}).get("url", "")
                    if sr_url not in existing_urls:
                        sr_title = sr.get("meta", {}).get("title", "")
                        sr_content = sr.get("meta", {}).get("content", "")
                        semantic_parts.append(
                            f"[{self._material_ref_no(sr.get('meta', {}), si)}] [语义补充] {self._claim_prefix(sr.get('meta', {}))}{sr_title}: {self._sanitize_skeleton(smart_truncate(sr_content, 800))}"
                        )

            # 组装
            final_parts = []
            if core_parts:
                final_parts.append("【核心素材（完整内容）】")
                final_parts.extend(core_parts)
            if semantic_parts:
                final_parts.append("【语义搜索补充】")
                final_parts.extend(semantic_parts)
            if other_parts:
                final_parts.append("【其他相关素材】")
                final_parts.extend(other_parts)

            if final_parts:
                return "\n".join(final_parts)
            return orig_mt

        except Exception as e:
            logger.warning("分层注入失败: %s，回退到原始格式", e)
            return orig_mt

    def _editorial_review(self, chapters: List[str]) -> List[str]:
        cleaned = []
        ai_fluff_patterns = [
            r"^[\s]*(?:好的[，,]?[\s]*)?(?:作为)?(?:一位)?(?:资深)?(?:商业分析师|AI助手|人工智能|模型|我)[，,。:\s]*",
            r"^[\s]*(?:以下是|如下是|下面是我|以下为我|接下来|现在|首先)[，,。:\s]*",
            r"^[\s]*(?:根据|基于|按照|遵照)[^，。]*?(?:需求|指示|要求|任务)[，,。:\s]*",
        ]

        for ch in chapters:
            lines_in_ch = ch.split("\n")
            new_lines = []
            for line in lines_in_ch:
                stripped = line.strip()
                if stripped.startswith(">"):
                    new_lines.append(line)
                    continue
                for pat in ai_fluff_patterns:
                    line = re.sub(pat, "", line, flags=re.IGNORECASE)
                if line.strip() or stripped == "":
                    new_lines.append(line)
            cleaned.append("\n".join(new_lines))

        combined = "\n\n---CHAPTER_BREAK---\n\n".join(cleaned)
        # 分片统稿——解决大报告只处理前7000字的问题
        chunk_size = 5000
        overlap = 200
        total_len = len(combined)
        if total_len <= chunk_size + overlap:
            # 小报告：一次性统稿
            prompt = f"""【角色】资深商业报告总编辑
【任务】全篇润色和统稿

【保留清单——绝对不得改动】
- 所有具体数据、数字、百分比
- 所有引用标注[1][2][3]
- 所有Markdown表格结构和内容
- 专业术语和已确认的事实陈述

【必须执行的改写】
- 将空洞总结句改写为包含具体数据的陈述
- 将模糊形容词替换为前文已论证的具体指标

【必须执行的删除】
- 段落开头的所有元叙述前缀
- 无实质信息的过渡句（"综上所述""总而言之"后若无新数据则删除）
- 内部标记：（预算XXX字，约X页）、[待填充]、[原始报告]、[素材]
- 转义符号：\\##、\\###、\\####、\\**

【必须执行的统一】
- 全篇术语表述一致
- 数字格式统一（百分比保留1位小数）
- 删除章节间重复论述，强化逻辑递进关系

【报告内容】
{combined}"""
            try:
                edited = self._agent_call("editor", [{"role": "user", "content": prompt}], max_tokens=4000)
            except Exception as e:
                raise RuntimeError(f"EditorAgent 全文统稿失败: {e}") from e
            if "---CHAPTER_BREAK---" in edited:
                parts = [p.strip() for p in edited.split("---CHAPTER_BREAK---") if p.strip()]
                if len(parts) == len(cleaned):
                    return parts
                logger.warning(
                    f"[EditorAgent] 全文统稿章节数不匹配（{len(parts)} vs {len(cleaned)}），改为逐章统稿")
            else:
                logger.warning("[EditorAgent] 全文统稿未返回章节分隔符，改为逐章统稿")
            return self._editorial_review_chapters(cleaned)
        else:
            # 大报告：分片统稿，每片5000字，重叠200字
            logger.info(f"[EditorAgent] 报告共{total_len}字，采用分片统稿（每片{chunk_size}字，重叠{overlap}字）")
            edited_chunks = []
            start = 0
            chunk_idx = 0
            while start < total_len:
                chunk = combined[start:start + chunk_size + overlap]
                chunk_idx += 1
                prompt = f"""【角色】资深商业报告总编辑
【任务】全篇润色和统稿（第{chunk_idx}片，共约{(total_len + chunk_size - 1) // chunk_size}片）

【保留清单——绝对不得改动】
- 所有具体数据、数字、百分比
- 所有引用标注[1][2][3]
- 所有Markdown表格结构和内容
- 专业术语和已确认的事实陈述

【必须执行的改写】
- 将空洞总结句改写为包含具体数据的陈述
- 将模糊形容词替换为前文已论证的具体指标

【必须执行的删除】
- 段落开头的所有元叙述前缀
- 无实质信息的过渡句（"综上所述""总而言之"后若无新数据则删除）
- 内部标记：（预算XXX字，约X页）、[待填充]、[原始报告]、[素材]
- 转义符号：\\##、\\###、\\####、\\**

【必须执行的统一】
- 全篇术语表述一致
- 数字格式统一（百分比保留1位小数）
- 删除章节间重复论述，强化逻辑递进关系

【报告内容（第{chunk_idx}片）】
{chunk}"""
                try:
                    edited = self._agent_call("editor", [{"role": "user", "content": prompt}], max_tokens=4000)
                    edited_chunks.append(edited)
                except Exception as e:
                    raise RuntimeError(f"EditorAgent 第{chunk_idx}片统稿失败: {e}") from e
                start += chunk_size
            # 拼接分片结果
            full_edited = "\n\n".join(edited_chunks)
            if "---CHAPTER_BREAK---" in full_edited:
                parts = [p.strip() for p in full_edited.split("---CHAPTER_BREAK---") if p.strip()]
                if len(parts) == len(cleaned):
                    return parts
                logger.warning(
                    f"[EditorAgent] 分片统稿章节数不匹配（{len(parts)} vs {len(cleaned)}），改为逐章统稿")
            else:
                logger.warning("[EditorAgent] 分片统稿未返回章节分隔符，改为逐章统稿")
            return self._editorial_review_chapters(cleaned)

    def _editorial_review_chapters(self, chapters: List[str]) -> List[str]:
        """逐章统稿兜底：保证章节数量与标题不丢失。"""
        logger.info(f"[EditorAgent] 逐章统稿 {len(chapters)} 章，确保章节不丢失")
        reviewed = []
        failures = 0
        skipped = 0
        ai_skipped = 0
        for i, ch in enumerate(chapters):
            if "【AI训练知识】" in (ch or ""):
                reviewed.append(ch)
                ai_skipped += 1
                logger.info(f"[EditorAgent] 第{i+1}章为 AI 训练知识章节，跳过统稿以保留标注")
                continue
            if len(ch) > 12000:
                reviewed.append(ch)
                skipped += 1
                logger.warning(f"[EditorAgent] 第{i+1}章过长({len(ch)}字)，跳过统稿并保留原文")
                continue
            title_match = re.search(r"^###\s+(.+)$", ch, re.MULTILINE)
            title = title_match.group(1).strip() if title_match else f"第{i+1}章"
            prompt = f"""【角色】资深商业报告总编辑
【任务】只润色本章，不改变结构，不新增事实
【保留清单】所有具体数据、引用标注、表格结构、专业术语必须原样保留
【必须删除】AI套话、元叙述前缀、内部标记（[待填充]/[素材]等）、转义符（\\##、\\**）
【输出要求】直接输出润色后的完整章节正文，必须以 ### {title} 开头，不要解释

【本章内容】
{ch}"""
            try:
                out = self._agent_call("editor", [{"role": "user", "content": prompt}], max_tokens=4000).strip()
            except Exception as e:
                failures += 1
                reviewed.append(ch)
                logger.warning(f"[EditorAgent] 第{i+1}章逐章统稿失败，保留原文: {e}")
                continue
            if not out:
                failures += 1
                reviewed.append(ch)
                logger.warning(f"[EditorAgent] 第{i+1}章逐章统稿返回空内容，保留原文")
                continue
            if not re.search(r"^###\s+", out, re.MULTILINE):
                out = f"### {title}\n\n{out}"
            reviewed.append(out)
        if failures == len(chapters):
            raise RuntimeError("EditorAgent 逐章统稿全部失败")
        logger.info(
            f"[EditorAgent] 逐章统稿完成：成功{len(chapters)-failures-skipped-ai_skipped}章，"
            f"保留原文{failures}章，AI跳过{ai_skipped}章，过长跳过{skipped}章"
        )
        return reviewed

    def _extract_section_from_draft(self, draft: str, chapter_title: str) -> str:
        """从骨架草稿中提取指定章节，兼容 ## / ### 标题与中文序号。"""
        def _norm(t: str) -> str:
            return re.sub(r'^[一二三四五六七八九十]+[、.．]\s*', '', (t or "").strip())

        lines = (draft or "").split('\n')
        target = _norm(chapter_title)
        start, level = None, 0
        for i, line in enumerate(lines):
            m = re.match(r'^(#{1,4})\s+(.+)$', line.strip())
            if not m:
                continue
            title = m.group(2).strip()
            if start is None:
                if target and (target in title or _norm(title) == target):
                    start = i
                    level = len(m.group(1))
            elif len(m.group(1)) <= level:
                return "\n".join(lines[start:i])
        if start is not None:
            return "\n".join(lines[start:])
        return (draft or "")[:1200]

    def final_gap_check(self) -> List[str]:
        if not self.final_draft or len(self.final_draft.strip()) < 500:
            return []
        prompt = f"""【任务】审视报告，识别最应该补充但未覆盖的子话题
【报告内容】{self.final_draft[:5000]}

【要求】
1. 识别3个最关键的信息缺口
2. 只输出话题名称，不要解释
3. 每行一个话题

【输出格式】
话题1
话题2
话题3"""
        raw = self._agent_call("search", [{"role": "user", "content": prompt}], max_tokens=200)
        gaps = [line.strip("- ") for line in raw.split("\n") if line.strip()]

        if not self.cfg.get("auto_fill_final_gaps", True) or not gaps:
            return gaps

        max_gaps = int(self.cfg.get("auto_fill_max_gaps", 3))
        rounds = max(1, int(self.cfg.get("auto_fill_rounds", 1)))
        sources_per_round = max(1, int(self.cfg.get("auto_fill_sources_per_round", 20)))
        gaps_to_fill = gaps[:max_gaps]
        per_gap = max(1, sources_per_round // max(1, len(gaps_to_fill)))
        modifiers = ["", " 数据 案例", " 趋势 对比", " 政策 风险"]

        for round_idx in range(rounds):
            if not gaps_to_fill or self.state.search_budget <= 0:
                break
            logger.info(
                f"[缺口补搜] 第{round_idx + 1}/{rounds}轮，目标{sources_per_round}个信源"
                f"（每个缺口{per_gap}条）"
            )
            for gap in gaps_to_fill:
                if self.state.search_budget <= 0:
                    logger.warning("[缺口补搜] 预算耗尽，停止补搜")
                    break
                gap_query = gap.get("query", "") if isinstance(gap, dict) else str(gap)
                if not self._is_valid_search_query(gap_query):
                    continue
                query = gap_query + (modifiers[round_idx] if round_idx < len(modifiers) else "")
                if self.state.search_budget <= 0:
                    break
                try:
                    results = self.searcher.multi_search([query], max_results=per_gap, workers=2)
                    results = [
                        m for m in (results or [])
                        if self._material_relevant(m, self.refined_task)
                    ]
                    if results:
                        self.state.consume_budget(1)
                        self.all_materials.extend(results)
                        logger.info(
                            f"[缺口补搜] 第{round_idx + 1}轮 '{query[:30]}' → 补充 {len(results)} 条"
                            f"（剩{self.state.search_budget}预算）"
                        )
                except Exception as e:
                    logger.warning(f"[缺口补搜] 第{round_idx + 1}轮失败: {e}")

        return gaps

    def add_action_plan(self) -> str:
        """基于报告核心内容生成可执行的行动建议。"""
        if not self.cfg.get("enable_action_plan", True):
            return ""
        if not self.chapters_md:
            return ""

        full_text = "\n".join(self.chapters_md)[:5000]
        prompt = f"""【任务】基于报告核心内容生成可执行的行动建议
【报告内容】{full_text}

【建议标准】
1. 生成3-5条具体建议
2. 每条必须包含三要素：
   - What：具体做什么
   - How：如何执行（具体步骤）
   - Impact：预期效果（尽可能量化）
3. 建议必须基于报告中的数据和发现，不得凭空提出
4. 直接输出Markdown列表，不要解释，不要索要额外信息

【输出格式】
- **建议1**：做什么 → 如何执行 → 预期效果
- **建议2**：做什么 → 如何执行 → 预期效果"""
        return self._agent_call("writer", [{"role": "user", "content": prompt}], max_tokens=800)

    def supplement_search(self, chapter_idx: int, query: str) -> bool:
        """手动补搜：支持逗号分隔的多个关键词，逐个尝试直到抓到素材。"""
        keywords = [k.strip() for k in re.split(r"[，,;；\n]+", query or "") if k.strip()]

        # query为空时用章节标题回退
        if not keywords:
            chapters = self.outline.get("chapters", [])
            title = chapters[chapter_idx].get("title", "") if 0 <= chapter_idx < len(chapters) else ""
            keywords = [title or self.report_title]
            logger.info(f"[补搜] 第{chapter_idx+1}章 使用标题回退: {keywords[0]}")

        logger.info(f"[补搜] 第{chapter_idx+1}章 关键词: {keywords}")
        for keyword in keywords:
            if not self._is_valid_search_query(keyword, min_len=2):
                logger.warning(f"[补搜] 关键词无效，跳过: {keyword}")
                continue
            if self.state.search_budget <= 0:
                logger.warning("[补搜] 搜索预算已耗尽")
                return False

            logger.info(f"[补搜] 第{chapter_idx+1}章 尝试: {keyword}")
            results = self.searcher.multi_search([keyword], max_results=5, workers=3)
            results = [
                m for m in (results or [])
                if self._material_relevant(m, self.refined_task)
            ]
            if not results:
                logger.info(f"[补搜] 关键词无结果: {keyword}")
                continue

            # 命中后才扣预算
            self.state.consume_budget(1)
            self.all_materials.extend(results)
            self.memory.add_fast(results)
            chapters = self.outline.get("chapters", [])
            if 0 <= chapter_idx < len(chapters):
                if "material_idxs" not in chapters[chapter_idx]:
                    chapters[chapter_idx]["material_idxs"] = []
                start_idx = max(1, len(self.all_materials) - len(results) + 1)
                for i in range(start_idx, len(self.all_materials) + 1):
                    if i not in chapters[chapter_idx]["material_idxs"]:
                        chapters[chapter_idx]["material_idxs"].append(i)
            logger.info(f"[补搜] 命中{len(results)}条，已归入第{chapter_idx+1}章")
            return True

        logger.warning(f"[补搜] 所有关键词均无结果: {keywords}")
        return False



    def _rewrite_low_density_chapters(self, indices):

        """重写低密度章节"""
        chs = self.outline.get("chapters", [])

        # 认知循环检测——重写前检查是否陷入重复模式
        loop_detected = self._detect_cognitive_loop(
            [self.chapters_md[i] for i in indices if i < len(self.chapters_md)],
            [chs[i].get("title", "") for i in indices if i < len(chs)]
        )
        if loop_detected:
            max_retries = self.cfg.get("loop_max_retries", 2)
            if not hasattr(self, '_loop_retry_count'):
                self._loop_retry_count = 0
            self._loop_retry_count += 1
            if self._loop_retry_count > max_retries:
                logger.warning(f"[循环检测] 已达最大重试次数({max_retries})，放弃重写，保留原章节")
                return list(self.chapters_md)
            logger.info(f"[循环检测] 策略切换：尝试切换writer模型({self._loop_retry_count}/{max_retries})")

        nc = list(self.chapters_md)

        for idx in indices:

            if idx >= len(nc) or idx >= len(chs): continue

            ct = chs[idx].get("title", f"第{idx+1}章")

            p = f"基于新素材重写{ct}\n原章：{smart_truncate(nc[idx], 3000)}\n需增加具体数据和案例"

            d = self._agent_call("writer", [{"role":"user","content":p}], max_tokens=4000)

            if d and len(d) > 100:

                nc[idx] = self._sanitize_chapter_output(d)

        return nc


    def _detect_cognitive_loop(self, chapter_texts: list, chapter_titles: list) -> bool:
        """认知循环检测——对近3轮LLM输出做embedding余弦相似度，检测重复无效策略。
        纯Ollama embedding计算，零LLM成本。"""
        if not self.cfg.get("enable_loop_detection", True):
            return False
        if len(chapter_texts) < 2:
            return False
        try:
            from memory import OllamaVectorStore
            threshold = self.cfg.get("loop_similarity_threshold", 0.85)
            store = OllamaVectorStore(
                self.cfg.get("ollama_host", "http://localhost:11434"),
                self.cfg.get("ollama_model", "EntropyYue/jina-embeddings-v2-base-zh:latest"),
            )
            recent = chapter_texts[-3:] if len(chapter_texts) >= 3 else chapter_texts[-2:]
            truncated = [t[:2000] for t in recent]
            vecs = store.embed(truncated)
            import numpy as np
            max_sim = 0.0
            for i in range(len(vecs)):
                for j in range(i+1, len(vecs)):
                    a, b = vecs[i], vecs[j]
                    norm_a = np.linalg.norm(a)
                    norm_b = np.linalg.norm(b)
                    if norm_a < 1e-10 or norm_b < 1e-10:
                        continue
                    sim = float(np.dot(a, b) / (norm_a * norm_b))
                    if sim > max_sim:
                        max_sim = sim
            logger.info(f"[循环检测] 近{len(recent)}轮输出最大相似度: {max_sim:.3f} (阈值:{threshold})")
            if max_sim > threshold:
                logger.warning(f"[循环检测] 检测到认知循环(相似度{max_sim:.3f}>{threshold})，触发策略切换")
                return True
            return False
        except Exception as e:
            logger.warning(f"[循环检测] 失败(非致命): {e}")
            return False

    def _post_generation_self_check(self) -> Dict:

        """报告生成后扫描数字密度、引用密度、章节重复度。"""
        if not self.chapters_md:
            return {"status": "skipped"}
        full_text = "\n".join(self.chapters_md)
        total_chars = len(full_text)

        # 数字密度检查
        numbers = re.findall(r'\d+\.?\d*%?', full_text)
        num_density = len(numbers) / max(total_chars / 1000, 1)

        # 引用密度检查
        citations = re.findall(r'\[\d+\]', full_text)
        cite_density = len(citations) / max(len(self.chapters_md), 1)

        # 章节重复度检查
        if len(self.chapters_md) >= 2:
            import difflib
            overlaps = []
            for i in range(len(self.chapters_md) - 1):
                ratio = difflib.SequenceMatcher(None, self.chapters_md[i][:2000], self.chapters_md[i+1][:2000]).ratio()
                overlaps.append(ratio)
            max_overlap = max(overlaps) if overlaps else 0
        else:
            max_overlap = 0

        result = {
            "total_chars": total_chars,
            "num_density": round(num_density, 1),
            "num_density_ok": num_density >= 3,
            "cite_density": round(cite_density, 1),
            "cite_density_ok": cite_density >= 2,
            "max_chapter_overlap": round(max_overlap * 100, 1),
            "overlap_ok": max_overlap < 0.3,
        }
        # 引用编号必须落在 1..参考文献实际列出的范围内
        invalid_citations = []
        try:
            ref_limit = int(self.cfg.get("reference_list_limit", 50) or 0)
        except (TypeError, ValueError):
            ref_limit = 50
        cited_max = max((int(x.strip("[]")) for x in citations), default=0)
        # 参考文献上限至少覆盖正文实际引用到的最大编号，避免与写作编号打架
        if ref_limit > 0:
            ref_limit = max(ref_limit, cited_max)
        max_ref = (len(self.all_materials) if ref_limit <= 0
                   else min(len(self.all_materials), ref_limit))
        if self.all_materials and citations:
            for n_str in set(citations):
                n = int(n_str.strip("[]"))
                if n < 1 or n > max_ref:
                    invalid_citations.append(n)
        result["citation_count"] = len(citations)
        result["invalid_citations"] = sorted(invalid_citations)
        result["citation_ok"] = not invalid_citations
        result["all_ok"] = (result["num_density_ok"] and result["cite_density_ok"]
                            and result["overlap_ok"] and result["citation_ok"])

        if result["invalid_citations"]:
            logger.warning(f"[自检] ⚠️ 引用编号越界: {result['invalid_citations'][:10]}")
        logger.info(f"[自检] 数字密度:{result['num_density']}/千字 {'✅' if result['num_density_ok'] else '⚠️'} | "
                     f"引用密度:{result['cite_density']}/章 {'✅' if result['cite_density_ok'] else '⚠️'} | "
                     f"章间重叠:{result['max_chapter_overlap']}% {'✅' if result['overlap_ok'] else '⚠️'}")
        if not result["all_ok"]:
            logger.warning("[自检] ⚠️ 报告质量不达标，建议检查并重新生成")
            if not result["num_density_ok"]:
                logger.warning("  → 数字密度不足（每千字应>=3个数字），可尝试补搜数据密集型方向")
            if not result["cite_density_ok"]:
                logger.warning("  → 引用密度不足（每章应>=2个引用标注），素材匹配可能不充分")
            if not result["overlap_ok"]:
                logger.warning("  → 章节间内容重复度偏高，建议合并或缩减")
            if self.cfg.get("density_retry_enabled", False) and not result["num_density_ok"]:
                lo = [i for i,ch in enumerate(self.chapters_md) if len(re.findall(r"\d+\.?\d*%?",ch))/max(len(ch)/1000,1) < 3]
                if lo and self.state.search_budget > 0:
                    chs = self.outline.get("chapters",[])
                    for i in lo[:3]:
                        q = chs[i].get("title","") if i<len(chs) else ""
                        if self.state.search_budget > 0:
                            self.supplement_search(i, q)
                    self.chapters_md = self._rewrite_low_density_chapters(lo[:3])
                    nums = re.findall(r"\d+\.?\d*%?","\n".join(self.chapters_md))
                    nd = len(nums)/max(len("\n".join(self.chapters_md))/1000,1)
                    result["num_density"]=round(nd,1)
                    result["num_density_ok"]=nd>=3

        # 本地模式追加逐章密度警示标注（仅测试用，Web模式不输出）
        if self.gui and not result["num_density_ok"]:
            for i, ch in enumerate(self.chapters_md):
                ch_nums = re.findall(r'\d+\.?\d*%?', ch)
                ch_chars = len(ch)
                ch_density = len(ch_nums) / max(ch_chars / 1000, 1)
                if ch_density < 3:
                    self.chapters_md[i] += f"\n\n> ⚠️ [本地测试] 本章数据密度 {ch_density:.1f}/千字（标准≥3），建议补搜后重新生成。"

        return result

    def _data_trap_check(self) -> Dict:
        """数据防坑检查——单位搞混/趋势造假/张冠李戴/跨章一致性。

        在报告生成后、质量评分前执行。使用 LLM 一次性检查所有数据陷阱，
        配合正则规则做结构化预检。

        Returns:
            {"unit_conflicts": [...], "fabricated_trends": [...],
             "misattributions": [...], "cross_chapter_inconsistencies": [...],
             "all_ok": bool}
        """
        if not self.chapters_md:
            return {"status": "skipped", "all_ok": True}

        full_text = "\n".join(self.chapters_md)
        result = {
            "unit_conflicts": [],
            "fabricated_trends": [],
            "misattributions": [],
            "cross_chapter_inconsistencies": [],
            "all_ok": True,
        }

        # ─── 规则层：正则预检（零LLM成本） ───
        # 1. 单位混搞检测：同一段落中同时出现"亿"和"万"且数值跳跃
        import re
        unit_patterns = [
            (r'(\d+\.?\d*)\s*亿.*?(\d+\.?\d*)\s*万', "亿/万混用"),
            (r'(\d+\.?\d*)\s*万亿', "万亿单位"),
            (r'(\d+\.?\d*)\s*billion.*?(\d+\.?\d*)\s*million', "billion/million混用"),
            (r'(\d+\.?\d*)\s*美元.*?(\d+\.?\d*)\s*元(?!/|每)', "美元/人民币混用"),
        ]
        for pattern, desc in unit_patterns:
            matches = re.findall(pattern, full_text, re.IGNORECASE)
            if matches:
                result["unit_conflicts"].append({
                    "type": desc,
                    "count": len(matches),
                    "samples": [str(m) for m in matches[:3]],
                })

        # 2. 口径混淆检测：出货量/零售量/营收/GMV 等关键口径词
        caliber_pairs = [
            (r'出货量', r'零售量', "出货量 vs 零售量"),
            (r'营收', r'GMV', "营收 vs GMV"),
            (r'注册用户', r'月活用户', "注册用户 vs 月活用户"),
            (r'产值', r'增加值', "产值 vs 增加值"),
            (r'销售额', r'净收入', "销售额 vs 净收入"),
        ]
        for kw1, kw2, desc in caliber_pairs:
            has_kw1 = bool(re.search(kw1, full_text))
            has_kw2 = bool(re.search(kw2, full_text))
            if has_kw1 and has_kw2:
                result["misattributions"].append({
                    "type": "口径可能混淆",
                    "detail": desc,
                    "advice": f"检查「{kw1}」和「{kw2}」是否在报告中被混用或错误比较",
                })

        # 3. 跨章一致性：提取所有「数字+单位」对，检查重复指标是否一致
        metric_pattern = re.findall(
            r'(\d+\.?\d*)\s*(亿|万|%|元|美元|亿元|万元|人次|家|个)\s*[,，\s]',
            full_text
        )
        # 简化：检查同一章节内是否有矛盾数值（如增速不一致）
        for i, ch in enumerate(self.chapters_md):
            growth_rates = re.findall(r'(增长|增速|增长率|同比|环比)[^。]*?(\d+\.?\d*%)', ch)
            if len(growth_rates) >= 2:
                rates = [float(r[1].replace('%', '')) for r in growth_rates]
                if max(rates) - min(rates) > 30:  # 增速差异超过30个百分点
                    result["cross_chapter_inconsistencies"].append({
                        "chapter": i + 1,
                        "type": "增速大幅波动",
                        "detail": f"增速范围 {min(rates):.1f}% ~ {max(rates):.1f}%，差异{max(rates)-min(rates):.1f}个百分点",
                    })

        # ─── LLM层：深度数据陷阱检测 ───
        if self.cfg.get("enable_data_trap_llm", True):
            try:
                # 取每章前800字做样本（控制token）
                sample = ""
                for i, ch in enumerate(self.chapters_md[:6]):
                    sample += f"第{i+1}章: {ch[:800]}...\n\n"

                prompt = f"""你是数据质量审查专家。请检查以下报告是否存在数据陷阱。
只关注以下四类问题，无问题则返回空数组：

1. 单位搞混：同一语境下单位不一致（如"销售额500亿"和"成本3000万"直接比较）
2. 趋势造假：连续多年数据呈现完美平滑增长（如每年固定增长x%，缺乏真实波动）
3. 张冠李戴：将不同口径数据当作同一指标（如把出货量当零售量、把GMV当营收）
4. 跨章矛盾：同一指标在不同章节出现不同数值

报告样本：
{sample[:4000]}

输出JSON格式：
{{"unit_conflicts":[{{"location":"第X章","detail":"描述","suggestion":"修复建议"}}],
 "fabricated_trends":[{{"location":"第X章","detail":"描述","suggestion":"修复建议"}}],
 "misattributions":[{{"location":"第X章","detail":"描述","suggestion":"修复建议"}}],
 "cross_chapter_inconsistencies":[{{"location":"第X章","detail":"描述","suggestion":"修复建议"}}]}}"""

                raw = self._agent_call(
                    "evaluator",
                    [{"role": "user", "content": prompt}],
                    max_tokens=800,
                    json_mode=True,
                )
                llm_result = safe_json_loads(raw, {})

                if isinstance(llm_result, dict):
                    for key in ["unit_conflicts", "fabricated_trends", "misattributions", "cross_chapter_inconsistencies"]:
                        if llm_result.get(key):
                            result[key].extend(llm_result[key])
            except Exception as e:
                logger.warning(f"[数据防坑] LLM检查失败: {e}")

        # ─── 汇总 ───
        total_issues = (
            len(result["unit_conflicts"])
            + len(result["fabricated_trends"])
            + len(result["misattributions"])
            + len(result["cross_chapter_inconsistencies"])
        )
        result["all_ok"] = total_issues == 0

        if total_issues > 0:
            logger.warning(
                f"[数据防坑] ⚠️ 发现 {total_issues} 个数据陷阱: "
                f"单位:{len(result['unit_conflicts'])} "
                f"趋势:{len(result['fabricated_trends'])} "
                f"口径:{len(result['misattributions'])} "
                f"跨章:{len(result['cross_chapter_inconsistencies'])}"
            )
            for cat, issues in [
                ("单位搞混", result["unit_conflicts"]),
                ("趋势造假", result["fabricated_trends"]),
                ("张冠李戴", result["misattributions"]),
                ("跨章矛盾", result["cross_chapter_inconsistencies"]),
            ]:
                for issue in issues[:3]:
                    logger.warning(f"  → [{cat}] {issue.get('detail', str(issue)[:80])}")
        else:
            logger.info("[数据防坑] ✅ 未发现数据陷阱")

        return result

    def _check_source_diversity(self) -> Dict:
        """来源多样性硬线检查。

        参考 deep-research RULES.md：
        - 总独立来源 < 8 触发补强
        - 全部 URL 集中在 ≤3 个域名 触发补强

        Returns:
            {"unique_sources": int, "unique_domains": int, "domain_concentration": float,
             "data_limited": bool, "reason": str}
        """
        if not self.all_materials:
            return {"unique_sources": 0, "unique_domains": 0,
                    "domain_concentration": 1.0, "data_limited": True,
                    "reason": "无任何搜索素材"}

        from urllib.parse import urlparse

        # 统计独立域名
        domains = {}
        for m in self.all_materials:
            url = ""
            if hasattr(m, 'url'):
                url = m.url
            elif isinstance(m, dict):
                url = m.get("url", "")
            if url:
                try:
                    domain = urlparse(url).netloc
                    domains[domain] = domains.get(domain, 0) + 1
                except Exception:
                    pass

        unique_domains = len(domains)
        unique_sources = len(self.all_materials)

        # 域名集中度：最大域名的占比
        max_domain_count = max(domains.values()) if domains else 0
        domain_concentration = max_domain_count / max(unique_sources, 1)

        data_limited = False
        reason = ""

        if unique_sources < 8:
            data_limited = True
            reason = f"独立来源仅 {unique_sources} 个（标准≥8）"
        elif unique_domains <= 3:
            data_limited = True
            reason = f"全部 URL 集中在 {unique_domains} 个域名（标准>3）"
        elif domain_concentration > 0.5:
            data_limited = True
            reason = f"单一域名占比 {domain_concentration:.0%}（标准≤50%）"

        result = {
            "unique_sources": unique_sources,
            "unique_domains": unique_domains,
            "domain_concentration": round(domain_concentration, 2),
            "data_limited": data_limited,
            "reason": reason,
        }

        if data_limited:
            logger.warning(f"[来源多样性] ⚠️ {reason}")
        else:
            logger.info(f"[来源多样性] ✅ {unique_sources}个来源 / {unique_domains}个域名")

        return result

    def _insert_data_limited_notice(self):
        """在报告中插入数据受限声明。

        当 _check_source_diversity 判定 data_limited=True 时，
        在报告开头插入醒目的数据说明。
        """
        notice = (
            "\n\n> ⚠️ **数据说明**：本报告基于有限的互联网公开信息生成。"
            "部分数据来源不够充分，结论仅供参考，"
            "建议结合一手数据或多源交叉验证后使用。\n"
        )
        if self.chapters_md:
            self.chapters_md[0] = notice + self.chapters_md[0]
            logger.info("[数据受限声明] 已插入报告开头")

    def _insert_quality_gate_notice(self, gate):
        """质量门禁未通过时，在报告开头插入可追溯说明（不阻断交付）。"""
        lines = [f"\n\n> ⚠️ **质量门禁未通过**：{gate.summary}"]
        for c in gate.unsupported_claims[:5]:
            lines.append(f"> - 无来源/推断：{c.field_name} = {str(c.value)[:60]}")
        for c in gate.disputed_claims[:3]:
            lines.append(f"> - 被质疑：{c.field_name} = {str(c.value)[:60]}")
        lines.append("> 以上声明请结合原文来源复核。\n")
        if self.chapters_md:
            self.chapters_md[0] = "\n".join(lines) + self.chapters_md[0]
            logger.info("[质量门禁] 已在报告开头插入未通过说明")

    def _quality_scorer(self) -> Dict:
        """多维度报告质量自动评分。
        basic模式：纯统计（数据密度/来源覆盖/引用完整性），零LLM成本。
        full模式：额外LLM逻辑一致性评估(~200t)。
        新增过程感知维度——搜索策略质量评估（搜前规划→搜中执行→搜后利用）。"""
        if not self.cfg.get("enable_quality_scoring", True):
            return {"status": "disabled"}
        if not self.chapters_md:
            return {"status": "no_content"}
        
        full_text = "".join(self.chapters_md)
        total_chars = max(len(full_text), 1)
        
        import re
        numbers = re.findall(r'\d+\.?\d*%?', full_text)
        num_density = round(len(numbers) / (total_chars / 1000), 1)
        
        unique_refs = set(re.findall(r'\[(\d+)\]', full_text))
        source_coverage = len(unique_refs)
        
        citations = re.findall(r'\[\d+\]', full_text)
        paragraphs = [p for p in full_text.split('\n') if len(p.strip()) > 20]
        para_count = max(len(paragraphs), 1)
        cite_density = round(len(citations) / para_count, 1) if para_count > 0 else 0
        
        chapter_lens = [len(ch) for ch in self.chapters_md if len(ch) > 50]
        if len(chapter_lens) >= 2:
            avg_len = sum(chapter_lens) / len(chapter_lens)
            deviations = [abs(cl - avg_len) / max(avg_len, 1) for cl in chapter_lens]
            balance_score = round(1.0 - sum(deviations) / len(deviations), 2)
        else:
            balance_score = 1.0
        
        result = {
            "total_chars": total_chars,
            "chapter_count": len(self.chapters_md),
            "num_density_per_1k": num_density,
            "num_density_ok": num_density >= self.cfg.get("quality_min_data_density", 3.0),
            "unique_sources": source_coverage,
            "source_coverage_ok": source_coverage >= self.cfg.get("quality_min_source_coverage", 3),
            "cite_density_per_para": cite_density,
            "chapter_balance": balance_score,
        }
        
        if self.cfg.get("quality_dimensions", "basic") == "full":
            try:
                sample = "".join([ch[:500] + "...\n" for ch in self.chapters_md[:4]])[:3000]
                prompt = f"""审视报告各章间的逻辑连贯性，只关注：前后矛盾、论证跳跃、结论与章节内容不符。
报告片段：{sample}
输出JSON: {{"logic_score":0.8,"issues":["问题描述或空数组"],"coherence":"评价"}}"""
                raw = self._agent_call("evaluator", [{"role":"user","content":prompt}], max_tokens=300, json_mode=True)
                logic_data = safe_json_loads(raw, {})
                if isinstance(logic_data, dict):
                    result["logic_score"] = logic_data.get("logic_score", 0.8)
                    result["logic_issues"] = logic_data.get("issues", [])
                    result["logic_coherence"] = logic_data.get("coherence", "")
            except Exception as e:
                logger.warning(f"[质量评分] LLM逻辑评估失败: {e}")

            # 过程感知质量——搜索策略评估
            try:
                result["search_quality"] = self._evaluate_search_strategy_quality()
            except Exception as e:
                logger.warning(f"[质量评分] 搜索策略评估失败: {e}")
                result["search_quality"] = {"error": str(e)}
        
        scores = []
        if result["num_density_ok"]: scores.append(1.0)
        else: scores.append(0.5)
        if result["source_coverage_ok"]: scores.append(1.0)
        else: scores.append(0.5)
        scores.append(max(0, result["chapter_balance"]))
        if "logic_score" in result:
            scores.append(float(result["logic_score"]))
        result["overall_quality"] = round(sum(scores) / len(scores), 2) if scores else 0.0
        
        logger.info(
            f"[质量评分] 综合{result['overall_quality']:.0%} | "
            f"数据密度:{result['num_density_per_1k']}/千字 | "
            f"来源:{result['unique_sources']}个 | "
            f"均衡度:{result['chapter_balance']:.0%}"
        )
        return result

    def _evaluate_search_strategy_quality(self) -> Dict:
        """过程感知质量评估——搜索策略质量。

        ArenaRL论文强调：开放任务Agent的质量不仅看最终输出，
        还要看推理过程（CoT）和工具调用（Tool Use）的质量。
        本方法评估搜索策略的三个阶段：
        1. 搜前规划：search_batches设计是否合理
        2. 搜中执行：素材覆盖度、饱和度
        3. 搜后利用：素材→章节的匹配效率

        Returns:
            {"pre_search": score, "in_search": score, "post_search": score, "overall": score}
        """
        result = {"pre_search": 0.0, "in_search": 0.0, "post_search": 0.0, "overall": 0.0}

        # 1. 搜前规划质量：批次设计是否合理
        if self.search_batches:
            batch_count = len(self.search_batches)
            total_queries = sum(len(b.get("queries", [])) if isinstance(b, dict) else len(b) if isinstance(b, list) else 0 for b in self.search_batches)
            # 每批至少3个查询，总查询数合理
            if batch_count >= 3 and total_queries >= 9:
                result["pre_search"] = 0.9
            elif batch_count >= 2 and total_queries >= 6:
                result["pre_search"] = 0.7
            elif batch_count >= 1 and total_queries >= 3:
                result["pre_search"] = 0.5
            else:
                result["pre_search"] = 0.3
            logger.info(f"[过程质量] 搜前规划: {batch_count}批/{total_queries}条查询 → {result['pre_search']:.2f}")
        else:
            result["pre_search"] = 0.0
            logger.warning("[过程质量] 无搜索批次记录")

        # 2. 搜中执行质量：素材覆盖度
        if self.all_materials:
            mat_count = len(self.all_materials)
            # 来源多样性
            domain_set = set()
            for m in self.all_materials:
                url = getattr(m, "url", "") or ""
                domain = url.split("/")[2] if "//" in url and len(url.split("/")) > 2 else ""
                if domain:
                    domain_set.add(domain)
            unique_domains = len(domain_set)

            # 覆盖度评分
            if mat_count >= 30 and unique_domains >= 8:
                result["in_search"] = 0.9
            elif mat_count >= 15 and unique_domains >= 5:
                result["in_search"] = 0.7
            elif mat_count >= 8 and unique_domains >= 3:
                result["in_search"] = 0.5
            else:
                result["in_search"] = 0.3
            logger.info(f"[过程质量] 搜中执行: {mat_count}素材/{unique_domains}域 → {result['in_search']:.2f}")
        else:
            result["in_search"] = 0.0

        # 3. 搜后利用质量：素材→章节匹配效率
        if self.outline and self.all_materials:
            chapters = self.outline.get("chapters", [])
            if chapters:
                matched_count = sum(1 for ch in chapters if ch.get("material_idxs") and len(ch.get("material_idxs", [])) > 0)
                total_chapters = len(chapters)
                match_ratio = matched_count / max(total_chapters, 1)

                if match_ratio >= 0.8:
                    result["post_search"] = 0.9
                elif match_ratio >= 0.5:
                    result["post_search"] = 0.7
                elif match_ratio >= 0.3:
                    result["post_search"] = 0.5
                else:
                    result["post_search"] = 0.3
                logger.info(f"[过程质量] 搜后利用: {matched_count}/{total_chapters}章有素材匹配 → {result['post_search']:.2f}")
        else:
            result["post_search"] = 0.5  # 无记录时给中立分

        # 综合：三阶段加权平均（搜前0.25 + 搜中0.40 + 搜后0.35）
        result["overall"] = round(
            result["pre_search"] * 0.25 + result["in_search"] * 0.40 + result["post_search"] * 0.35, 2
        )
        logger.info(f"[过程质量] 综合: {result['overall']:.2f} (搜前:{result['pre_search']:.2f} 搜中:{result['in_search']:.2f} 搜后:{result['post_search']:.2f})")
        return result

    def _ensure_chapter_integrity(self):
        """统稿/结构复审后按骨架对齐章节；缺失章节补写，避免静默丢章。"""
        chapters = self.outline.get("chapters", [])
        if not chapters or not self.chapters_md:
            return

        def _key(title: str) -> str:
            t = re.sub(r'^第[一二三四五六七八九十百\d]+章[\s:：、.]*', '', title or "").strip()
            t = re.sub(r'^\d+(?:\.\d+)*[.、]?\s*', '', t).strip()
            return re.sub(r'[\s《》“”"\']', '', t)[:40]

        def _overlap(a: str, b: str) -> float:
            """按中文二元组重合度判断两个标题是否指向同一章。"""
            if not a or not b:
                return 0.0
            ga = {a[i:i + 2] for i in range(len(a) - 1)}
            gb = {b[i:i + 2] for i in range(len(b) - 1)}
            if not ga or not gb:
                return 1.0 if a == b else 0.0
            return len(ga & gb) / min(len(ga), len(gb))

        def _title_of(text: str) -> str:
            # 兼容 ## / ### 两级章节标题
            m = re.search(r'^#{1,3}\s+(.+)$', text or "", re.MULTILINE)
            return _key(m.group(1)) if m else ""

        keys = [_key(m.get("title", "")) for m in chapters]
        cand_keys = [_title_of(c) for c in self.chapters_md]

        def _trim_nested(text: str, own_key: str) -> str:
            """裁掉章节内嵌的其他章标题（骨架提取越界会带入下一章内容）。"""
            lines = (text or "").split('\n')
            for li, line in enumerate(lines):
                if li == 0:
                    continue
                m = re.match(r'^#{1,3}\s+(.+)$', line.strip())
                if not m:
                    continue
                k = _key(m.group(1))
                if k and k != own_key and any(
                    k == keys[j] or _overlap(k, keys[j]) >= 0.3 for j in range(len(keys))
                ):
                    return "\n".join(lines[:li]).strip()
            return text

        aligned, missing, used = [], [], set()
        # 1) 精确匹配优先：先把完全同名的章节锁定，避免近似匹配抢占唯一候选
        exact_hit = {}
        for i, key in enumerate(keys):
            for idx, ck in enumerate(cand_keys):
                if idx not in used and ck and ck == key:
                    exact_hit[i] = idx
                    used.add(idx)
                    break

        for i, key in enumerate(keys):
            if i in exact_hit:
                text = _trim_nested(self.chapters_md[exact_hit[i]], key)
                aligned.append(text)
                if self._chapter_char_count(text) < 120:
                    missing.append(i)
                continue
            # 2) 标题高度重叠（近似匹配）
            hit = None
            for idx, ck in enumerate(cand_keys):
                if idx in used or not ck:
                    continue
                if _overlap(ck, key) >= 0.3:
                    hit = idx
                    break
            if hit is not None:
                text = _trim_nested(self.chapters_md[hit], key)
                aligned.append(text)
                used.add(hit)
                if self._chapter_char_count(text) < 120:
                    missing.append(i)
                continue
            # 2) 位置兜底：该位置内容不属于任何其他骨架章时，保留为该章内容
            text = ""
            if i < len(self.chapters_md) and i not in used:
                if not cand_keys[i]:
                    # 无标题但正文可观，按位置保留，避免被空白占位覆盖
                    if self._chapter_char_count(self.chapters_md[i]) >= 300:
                        text = self.chapters_md[i]
                        used.add(i)
                else:
                    belongs_elsewhere = any(
                        j != i and (cand_keys[i] == keys[j] or _overlap(cand_keys[i], keys[j]) >= 0.3)
                        for j in range(len(keys)) if j not in used
                    )
                    if not belongs_elsewhere:
                        text = _trim_nested(self.chapters_md[i], key)
                        used.add(i)
            if self._chapter_char_count(text) < 120:
                missing.append(i)
            aligned.append(text)

        if len(aligned) != len(self.chapters_md):
            logger.warning(f"[章节校验] 按骨架对齐章节：{len(self.chapters_md)} → {len(aligned)} 章")
        self.chapters_md = aligned

        for i in missing:
            title = chapters[i].get("title", f"第{i+1}章")
            logger.warning(f"[章节校验] 第{i+1}章「{title}」缺失或为空，尝试补写")
            draft = ""
            if self.cfg.get("enable_ai_knowledge_fallback", True):
                draft = self._write_ai_knowledge_chapter(chapters[i])
            if draft:
                self.chapters_md[i] = draft
                self._persist_chapter(i, draft)
                continue
            self.chapters_md[i] = (
                f"### {title}\n\n"
                f"> 本章生成失败，请在【控制面修改】输入关键词（逗号分隔），然后点击【补搜本章】重试。\n"
            )

    def _structure_review(self):
        """涌现后结构复审——重排章节、添加过渡、检测重复标题。"""
        if len(self.chapters_md) < 3:
            return
        logger.info("[结构复审] 审查章节逻辑递进与重复...")
        titles = []
        for ch_text in self.chapters_md:
            m = re.search(r'^###\s+(.+)$', ch_text, re.MULTILINE)
            if m:
                titles.append(re.sub(r"^#+\s*", "", m.group(1).strip()))
        dup_titles = [t for t in set(titles) if titles.count(t) > 1]
        if dup_titles:
            logger.warning(f"[结构复审] 发现重复标题: {dup_titles}")
            new_chapters = []
            seen_titles = set()
            for i, ch_text in enumerate(self.chapters_md):
                m = re.search(r'^###\s+(.+)$', ch_text, re.MULTILINE)
                title = re.sub(r"^#+\s*", "", m.group(1).strip()) if m else f"第{i+1}章"
                if title in seen_titles:
                    for j in range(len(new_chapters) - 1, -1, -1):
                        prev_m = re.search(r'^###\s+(.+)$', new_chapters[j], re.MULTILINE)
                        if prev_m and prev_m.group(1).strip() == title:
                            new_chapters[j] += "\n\n" + re.sub(r'^###\s+.+\n', '', ch_text)
                            break
                else:
                    seen_titles.add(title)
                    new_chapters.append(ch_text)
            self.chapters_md = new_chapters
            logger.info(f"[结构复审] 合并重复后 {len(self.chapters_md)} 章")
        _ch_title_pat = r'^###\s+(.+)$'
        outline_text = "\n---\n".join([
            f"第{i+1}章: {re.search(_ch_title_pat, ch, re.MULTILINE).group(1) if re.search(_ch_title_pat, ch, re.MULTILINE) else '未知'}"
            for i, ch in enumerate(self.chapters_md)
        ])
        combined = "\n\n".join([ch[:800] for ch in self.chapters_md])
        prompt = f"""【任务】审查报告章节结构的逻辑合理性
【章节顺序】{outline_text}
【报告摘要】{combined[:5000]}

【审查维度】
1. 章节顺序是否符合认知逻辑和调研逻辑
2. 相邻章节之间是否存在跳跃，是否需要过渡段
3. 是否存在应该拆分或合并的章节

【输出要求】
- 只输出纯JSON对象，不要解释文字
- 标准格式：
{{"reorder":[0,1,2,3],"transitions":[{{"between":"第X章→第Y章","text":"过渡段内容2-3句"}}],"split_or_merge":"无/具体建议"}}
- 如章节顺序合理，reorder返回当前顺序数组"""
        try:
            raw = self._agent_call("editor", [{"role":"user","content":prompt}], max_tokens=1200, json_mode=True)
            review = safe_json_loads(raw, {})
            if isinstance(review, dict):
                new_order = review.get("reorder", [])
                if new_order and len(new_order) == len(self.chapters_md) and new_order != list(range(1, len(self.chapters_md)+1)):
                    reordered = [self.chapters_md[i-1] for i in new_order if 1 <= i <= len(self.chapters_md)]
                    self.chapters_md = reordered
                    logger.info(f"[结构复审] 重排章节: {new_order}")
                transitions = review.get("transitions", [])
                for t in transitions:
                    between = t.get("between", "")
                    text = t.get("text", "")
                    m = re.match(r'第(\d+)章.*第(\d+)章', between)
                    if m and text:
                        a = int(m.group(1))
                        if 0 <= a-1 < len(self.chapters_md):
                            self.chapters_md[a-1] += f"\n\n> {text}\n"
                    logger.info(f"[结构复审] 插入过渡: {between}")
        except Exception as e:
            logger.warning(f"[结构复审失败] {e}")

    def _synthesize_patterns(self) -> str:
        """跨章模式提炼——从已完成的章节中提取可复用的模式。"""
        if len(self.chapters_md) < 3:
            return ""
        logger.info("[模式提炼] 跨章提取可复用模式...")
        combined = "\n\n".join(self.chapters_md)
        prompt = f"""【任务】从报告中提取可复用的运营模式或方法论
【报告内容】{combined[:8000]}

【提取标准】
1. 提取2-4个贯穿多个章节的模式
2. 每个模式必须至少在2个以上章节有体现
3. 模式必须可直接复用，包含具体步骤
4. 预期效果必须引用报告中的具体数据

【输出要求】
- 只输出纯JSON数组，不要解释文字
- 标准格式：
[
  {{
    "name":"模式名（8字以内）",
    "chapters":["涉及章节1","涉及章节2"],
    "logic":"核心逻辑（1句话）",
    "steps":["具体步骤1","具体步骤2","具体步骤3"],
    "conditions":"适用条件",
    "expected_impact":"预期效果（引用报告数据）"
  }}
]"""
        try:
            raw = self._agent_call("planner", [{"role":"user","content":prompt}], max_tokens=2000, json_mode=True)
            patterns = safe_json_loads(raw, [])
            if not isinstance(patterns, list) or len(patterns) == 0:
                return ""
            md_parts = ["## 可复用模式提炼\n\n"]
            for i, p in enumerate(patterns):
                if not isinstance(p, dict):
                    continue
                md_parts.append(f"### 模式{i+1}：{p.get('name','')}\n\n")
                md_parts.append(f"**核心逻辑**：{p.get('logic','')}\n\n")
                md_parts.append(f"**涉及章节**：{'、'.join(p.get('chapters',[]))}\n\n")
                for j, s in enumerate(p.get('steps',[])):
                    md_parts.append(f"{j+1}. {s}\n")
                md_parts.append(f"\n**适用条件**：{p.get('conditions','')}\n\n")
                md_parts.append(f"**预期效果**：{p.get('expected_impact','')}\n\n---\n\n")
            md = "".join(md_parts)
            logger.info(f"[模式提炼] 提取{len(patterns)}个可复用模式")
            return md
        except Exception as e:
            logger.warning(f"[模式提炼失败] {e}")
            return ""

    def _generate_reference_list(self) -> str:
        """从all_materials生成参考文献列表，编号与正文引用保持一致。"""
        if not self.all_materials:
            return ""
        try:
            limit = int(self.cfg.get("reference_list_limit", 50) or 0)
        except (TypeError, ValueError):
            limit = 50
        # 正文已引用的最大编号必须出现在参考文献里
        cited_max = 0
        for ch in self.chapters_md:
            for x in re.findall(r'\[(\d+)\]', ch):
                cited_max = max(cited_max, int(x))
        if limit > 0:
            limit = max(limit, cited_max)
        mats = self.all_materials[:limit] if limit > 0 else self.all_materials
        seen = {}
        refs = []
        for i, m in enumerate(mats, 1):
            key = m.url.strip() if m.url else m.title.strip()
            if key and key in seen:
                refs.append(f"[{i}] 同 [{seen[key]}]")
                continue
            if key:
                seen[key] = i
            tier_tag = f"[{m.tier}]" if hasattr(m, 'tier') and m.tier else ""
            refs.append(f"[{i}] **{m.title}** {tier_tag} \n    {m.url}  \n    来源: {m.source} | 评分: {m.llm_score:.0f}")
        if not refs:
            return ""
        md = "## 参考文献与数据来源\n\n"
        md += "\n\n".join(refs)
        logger.info(f"[参考文献] 生成{len(refs)}条来源")
        return md

    def _format_framework_for_display(self, fw: ResearchFramework) -> str:
        lines = ["=" * 62, "  超深度调研框架", "=" * 62, ""]
        if fw.industry_name:
            lines.append(f"  行业：{fw.industry_name}")
        if fw.industry_definition:
            lines.append(f"  定义：{fw.industry_definition[:120]}...")
        lines.append(f"  来源：{fw.source_file or '框架涌现'}")
        lines.append(f"  企业数量：{len(fw.entities)}家 | 维度：{len(fw.dimensions)}个 | 批次：{len(fw.search_batches)}批")
        lines.append(f"  数据口径：{'已声明' if fw.口径_declaration else '未声明'}")
        lines.append("")
        if fw.entities:
            lines.append("  【实体清单】")
            for ent in fw.entities[:15]:
                t = ent.get('tier','')
                lines.append(f"    - {ent['name']}{' ('+t+')' if t else ''}")
            if len(fw.entities) > 15:
                lines.append(f"    ... 共{len(fw.entities)}家")
        if fw.dimensions:
            lines.append("\n  【调研维度】")
            for d in fw.dimensions[:8]:
                lines.append(f"    - {d.get('name','')}: {d.get('description','')[:120]}")
        lines.append("\n" + "=" * 62)
        lines.append("  请确认框架后点击【确认框架并执行】")
        display = "\n".join(lines)
        try:
            ctrl_path = os.path.join(self.cfg["output_dir"], "后台调试", "control_panel.txt")
            os.makedirs(os.path.dirname(ctrl_path), exist_ok=True)
            with open(ctrl_path, "w", encoding="utf-8") as cf:
                cf.write(display)
        except OSError as e:
            logger.warning(f"[engine] 写入control_panel失败: {e}")
        return display

    def _generate_plan_with_framework(self, task: str):
        """注入框架信息的增强计划生成——产出致死量规划"""
        fw = self.framework
        logger.info("生成增强调研计划（含框架信息）...")
        task_summary = task[:2000] if len(task) > 2000 else task
        max_pages = int(self.cfg.get("max_pages", 60))
        batches_hint = self.cfg.get("search_batches", 3)
        fw_injection_parts = []
        if fw.industry_name: fw_injection_parts.append(f"【行业】{fw.industry_name}\n")
        if fw.industry_definition: fw_injection_parts.append(f"【行业定义】{fw.industry_definition[:300]}\n")
        if fw.口径_declaration: fw_injection_parts.append(f"【数据口径】{fw.口径_declaration[:300]}\n")
        if fw.entities:
            tiers = {}
            for e in fw.entities[:40]:
                t = e.get('tier','未分类')
                if t not in tiers: tiers[t] = []
                tiers[t].append(e['name'])
            fw_injection_parts.append("【已识别实体清单（按梯队）】\n")
            for t, names in tiers.items(): fw_injection_parts.append(f"  {t}: {', '.join(names[:12])}\n")
        if fw.dimensions:
            fw_injection_parts.append("【调研维度】\n")
            for d in fw.dimensions[:8]: fw_injection_parts.append(f"  - {d.get('name','')}: {d.get('description','')[:120]}\n")
        if fw.core_questions:
            fw_injection_parts.append("【核心追问】\n")
            for q in fw.core_questions[:5]: fw_injection_parts.append(f"  - {q}\n")
        if fw.common_mistakes:
            fw_injection_parts.append("【常见误区】\n")
            for m in fw.common_mistakes[:5]: fw_injection_parts.append(f"  - {m}\n")
        if self._user_strategy:
            fw_injection_parts.append(f"\n【用户确认过的调研策略】\n{self._user_strategy}\n")
        fw_injection = "".join(fw_injection_parts)
        batch_template = f"""【搜索批次规划】（深度模式，请根据企业数量生成{batches_hint}-{batches_hint+2}个批次）
维度约束：只覆盖3-4个最重要的维度（如商业模式、竞争壁垒、产品结构、市场份额），每个维度做三级深度追问（What→Why→What If），而非浅尝辄止搜10个维度。
批次1-行业宏观（规模→增长驱动因素→关键变量）
批次2-第一梯队头部（商业模式→为何有效→如果前提变化会怎样）
批次3-第二梯队A组（同样逻辑：What→Why→What If三层追问）
批次4-第二梯队B组
...最后一批-交叉对比矩阵
每批3-5个查询含实体名+调研维度，每个查询应体现追问链深度"""
        prompt = f"""【角色】资深商业分析师
【任务】输出详尽的深度调研计划书
【当前日期】{current_year_anchor()}
【报告页数期望】{max_pages}页
【搜索模式】深度模式 {batches_hint}批×{self.cfg.get("max_iterations",2)}迭代
【能力边界】仅互联网公开信息桌面研究
{fw_injection}

【需求摘要】{task_summary}

【输出结构——必须严格遵循，每部分必须充实】
### 【需求理解】（5-8句话，分析竞争态势、行业阶段、核心矛盾）
### 【竞争格局速描】（基于已识别企业，按梯队简述各玩家定位与差异化）
### 【市场规模初步框算】（如有数据估算TAM/SAM/SOM，无数据则标注"待搜索验证"并给出估算逻辑）
### 【建议调研方向】（5-7个方向，每个至少3句话说明：为什么重要、要看什么数据、对报告的贡献）
### 【搜索批次规划】（深度模式，生成{batches_hint}-{batches_hint+2}个批次）
   批次1-行业宏观  批次2-第一梯队头部  批次3-第二梯队A组  批次4-第二梯队B组 ... 最后一批-交叉对比矩阵
   每批3-5个查询，必须包含企业名+调研维度
### 【核心追问】（3-5个贯穿全文的核心问题，每个说明该问题如何影响报告结论）
### 【数据需求矩阵】（表格列出：章节/所需数据类型/优先级/预计来源）

【强制要求】
1. 每部分至少5句话，严禁敷衍
2. 搜索批次每批3-5个具体查询，必须包含实体名
3. 所有规划必须可通过公开搜索执行"""
        for attempt in range(3):
            try:
                self.plan_text = self._agent_call("search", [{"role":"user","content":prompt}], max_tokens=5000)
                if len(self.plan_text.strip()) > 150:
                    logger.info(f"[增强计划] 长度{len(self.plan_text)}字")
                    break
                elif attempt < 2:
                    logger.warning("[增强计划] 返回过短，重试...")
                    time.sleep(1)
            except Exception as e:
                logger.error(f"[增强计划错误] {e}")
                if attempt < 2: time.sleep(2)
        self.search_batches = self._parse_search_batches(self.plan_text)
        self._limit_search_batches()
        self.plan_directions, self.plan_core_questions = _extract_plan_key_info(self.plan_text)
        logger.info(f"[增强计划] 方向{len(self.plan_directions)} 追问{len(self.plan_core_questions)} 批次{len(self.search_batches)}")

    async def _run_v15_full_pipeline(self):
        """深度模式完整流水线——搜索→评估→涌现→扩写→整理"""
        logger.info("执行完整深度流水线...")
        # 确保搜索批次已设置（Web端直接调用_run_v15时可能跳过_generate_plan_with_framework）
        if not self.search_batches and self.framework:
            self._build_search_batches_from_framework()
            self._limit_search_batches()
        await self.depth_search()
        self.check_plan_coverage()
        self.auto_supplement_for_gaps(self.plan_coverage)
        if getattr(self.state, "search_failure", False) and not self.all_materials:
            raise RuntimeError("GPTR 搜索失败，自动补搜也未获得可用素材，已终止本次调研")
        ma = self.assess_materials_and_emerge_outline()
        self.zero_draft = self.generate_zero_shot_draft(self.refined_task, ma)
        self.outline = self.parse_outline_from_draft(self.zero_draft, ma)
        self.report_title = self.outline.get("title", f"{self.framework.industry_name}深度调研报告") if self.framework else "深度调研报告"
        self._auto_assign_chapter_materials()
        self.final_draft = self.zero_draft
        self.write_chapters(self.final_draft)
        self._append_v15_appendix()
        for idx, ch_text in enumerate(self.chapters_md):
            chs = self.outline.get("chapters", [])
            ct = chs[idx].get("title", f"第{idx+1}章") if idx < len(chs) else f"第{idx+1}章"
            if idx < len(chs) and not any(kw in ct for kw in ["声明","误区","附录"]):
                self.chapters_md[idx] = self.chapter_level_denoise(ch_text, ct)
        self._structure_review()
        pm = self._synthesize_patterns(); rm = self._generate_reference_list()
        if pm: self.chapters_md.append(pm)
        if rm: self.chapters_md.append(rm)
        # 最终缺口检查（与常规模式保持一致）
        self.final_draft = "\n".join(self.chapters_md)
        gaps = self.final_gap_check()
        if gaps:
            logger.info(f"[最终缺口检查] 发现未覆盖话题: {gaps}")
        try:
            self._extract_report_claims()
        except Exception as e:
            logger.warning(f"[ClaimLedger] 提取跳过: {e}")
        # 与常规路径统一收尾：自检、数据防坑、来源多样性、质量评分、确定性门禁
        await self._run_stage("quality", lambda: self._step_quality_check(), critical=False)
        logger.info(f"流水线完成，共{len(self.chapters_md)}章")

    def _append_v15_appendix(self):
        """追加深度模式数据声明和误区"""
        fw = self.framework
        if not fw: return
        
        # 统计信源等级分布
        tier_stats = {"S": 0, "A": 0, "B": 0, "C": 0}
        for m in self.all_materials:
            if hasattr(m, 'tier') and m.tier in tier_stats:
                tier_stats[m.tier] += 1
        
        dl = ["\n\n## 数据可靠性声明\n"]
        dl.append(f"本报告共引用 {len(self.all_materials)} 条数据来源，信源等级分布：")
        dl.append(f"- **S级**（国家级官方）：{tier_stats['S']} 条")
        dl.append(f"- **A级**（主流媒体/智库）：{tier_stats['A']} 条")
        dl.append(f"- **B级**（行业媒体/垂直平台）：{tier_stats['B']} 条")
        dl.append(f"- **C级**（论坛/问答/未验证）：{tier_stats['C']} 条")
        dl.append("")
        
        for ds in fw.data_sources[:10]:
            dl.append(f"- {ds.get('source','')}：{ds.get('credibility','')} — {ds.get('notes','')}")
        if fw.口径_declaration: dl.append(f"\n口径声明：{fw.口径_declaration[:200]}")
        if fw.common_mistakes:
            dl.append("\n**常见调研误区：**")
            for m in fw.common_mistakes[:5]: dl.append(f"- {m}")
        self.chapters_md.append("\n".join(dl))

    def _build_search_batches_from_framework(self):
        """从框架实体清单自动构建搜索批次（Web端未经过generate_plan时的回退）"""
        fw = self.framework
        total = len(fw.entities)
        if not fw.search_batches or total > 0:
            if total == 0:
                # 无企业：使用框架预设的搜索批次
                self.search_batches = fw.search_batches if fw.search_batches else [{"description":"通用搜索","queries":[fw.industry_name]}]
            elif total <= 8:
                queries = [f"{e['name']} {fw.industry_name}" for e in fw.entities]
                self.search_batches = [{"description":"全部企业","queries":queries}]
            elif total <= 30:
                bs = 7; self.search_batches = []
                for i in range(0, total, bs):
                    grp = fw.entities[i:i+bs]
                    self.search_batches.append({"description":f"企业组{i//bs+1}({len(grp)}家)","queries":[f"{e['name']} {fw.industry_name}" for e in grp]})
            else:
                bs = 5; self.search_batches = []
                for i in range(0, total, bs):
                    grp = fw.entities[i:i+bs]
                    self.search_batches.append({"description":f"企业组{i//bs+1}({len(grp)}家)","queries":[f"{e['name']} {fw.industry_name} 商业模式" for e in grp][:8]})
        # 从框架维度构建 plan_directions
        if not self.plan_directions and fw.dimensions:
            self.plan_directions = [d.get('name','') for d in fw.dimensions[:8]]
        if not self.plan_core_questions and fw.core_questions:
            self.plan_core_questions = fw.core_questions[:5]
        logger.info(f"[回退] 搜索批次{len(self.search_batches)} 方向{len(self.plan_directions)} 追问{len(self.plan_core_questions)}")

    # ============================================================
    # run() 拆分的私有步骤方法
    # ============================================================

    def _step_prepare_task(self, task):
        """步骤1: 任务准备 —— 输入安全清洗

        Args:
            task: 原始任务字符串

        Returns:
            经安全清洗后的任务字符串
        """
        # 输入安全清洗
        try:
            from shared.input_guard import sanitize_content
            sanitized = sanitize_content(task)
            if sanitized:
                task = sanitized
                self.raw_task = task
        except ImportError:
            pass

        return task

    # ============================================================
    # 任务状态、阶段重试与断点恢复
    # ============================================================

    def _is_transient_error(self, exc: Exception) -> bool:
        """只对网络/超时/限流类错误自动重试，程序性错误直接暴露。"""
        markers = (
            "timeout", "timed out", "connection", "connect", "network", "rate limit",
            "429", "500", "502", "503", "504", "temporarily", "overloaded",
            "try again", "超时", "连接失败", "限流",
        )
        if isinstance(exc, (TimeoutError, ConnectionError)):
            return True
        text = f"{type(exc).__name__}: {exc}".lower()
        return any(marker in text for marker in markers)

    async def _run_stage(self, name: str, fn, critical: bool = True):
        """执行一个阶段：已完成则跳过；瞬时错误自动重试；最终失败落盘并抛出。"""
        store = self._state_store
        label = stage_label(name)
        if store and store.is_stage_done(name):
            logger.info(f"[任务状态] 跳过已完成阶段：{label}")
            return None

        max_attempts = 1 + max(0, self._stage_retries)
        attempts = 0
        while True:
            attempts += 1
            if store:
                store.begin_stage(name)
            started = time.time()
            try:
                result = fn()
                if inspect.isawaitable(result):
                    result = await result
                if store:
                    store.finish_stage(name)
                logger.info(f"[阶段完成] {label} | 尝试{attempts}次 | {time.time()-started:.1f}s")
                return result
            except Exception as e:
                transient = self._is_transient_error(e)
                logger.error(
                    f"[阶段失败] {label} | 第{attempts}/{max_attempts}次 | "
                    f"{'瞬时错误' if transient else '非瞬时错误'} | {type(e).__name__}: {e}"
                )
                if store:
                    store.attempt_failed(name, e, attempts)
                if transient and attempts < max_attempts:
                    backoff = self.cfg.get("stage_retry_backoff", [5, 15])
                    delay = backoff[min(attempts - 1, len(backoff) - 1)] if isinstance(backoff, list) else 5
                    logger.info(f"[阶段重试] {label} | {delay}秒后自动重试")
                    await asyncio.sleep(delay)
                    continue
                if critical:
                    if store:
                        store.fail_stage(name, e)
                    raise StageFailed(name, e, attempts) from e
                if store:
                    store.optional_failed(name, e)
                logger.warning(f"[阶段降级] {label} 失败，记录后继续（非核心阶段）")
                return None

    def _snapshot_pipeline(self):
        if not self._state_store:
            return
        self._state_store.save_artifact("pipeline", {
            "raw_task": self.raw_task,
            "refined_task": self.refined_task,
            "plan_text": self.plan_text,
            "plan_directions": self.plan_directions,
            "plan_core_questions": self.plan_core_questions,
            "plan_coverage": self.plan_coverage,
            "search_batches": self.search_batches,
            "outline": self.outline,
            "zero_draft": self.zero_draft,
            "report_title": getattr(self, "report_title", ""),
            "material_assessment": self._material_assessment,
            "chapters_md": self.chapters_md,
            "search_budget": self.state.search_budget,
        })

    def _save_materials(self):
        if not self._state_store:
            return
        self._state_store.save_artifact("materials", [
            m.to_dict() if hasattr(m, "to_dict") else m for m in self.all_materials
        ])

    def _restore_from_state(self):
        """恢复上次失败的现场：属性快照 + 素材库。"""
        store = self._state_store
        snap = store.load_artifact("pipeline") or {}
        for field in ("raw_task", "refined_task", "plan_text", "plan_directions",
                      "plan_core_questions", "plan_coverage", "search_batches",
                      "outline", "zero_draft", "chapters_md"):
            if field in snap:
                setattr(self, field, snap[field])
        self.report_title = snap.get("report_title", getattr(self, "report_title", "调研报告"))
        self._material_assessment = snap.get("material_assessment") or {}
        if "search_budget" in snap:
            self.state.search_budget = int(snap["search_budget"])

        mats = store.load_artifact("materials") or []
        if mats:
            self.all_materials = [SearchResult.from_dict(d) for d in mats]
            # 写作完成后重试只需复用产物，无需重建向量库
            if not store.is_stage_done("writing"):
                self.memory.clear()
                self.memory.add_fast(self.all_materials)

        saved_cfg = store.data.get("config") or {}
        if saved_cfg:
            self.cfg.update(saved_cfg)
        logger.info(
            f"[任务状态] 已恢复任务 {store.task_id}："
            f"素材{len(self.all_materials)}条，大纲{len(self.outline.get('chapters', []))}章，"
            f"已写章节{len([c for c in self.chapters_md if c])}章"
        )

    def _set_material_assessment(self, assessment):
        self._material_assessment = assessment or {}
        return self._material_assessment

    def _persist_chapter(self, idx: int, text: str):
        if self._state_store and text and text.strip():
            self._state_store.save_text(os.path.join("chapters", f"{idx+1:02d}.md"), text)

    def _persist_chapters(self):
        if not self._state_store:
            return
        self._state_store.save_chapters(self.chapters_md)
        self._state_store.save_text("report.md", "\n\n".join(self.chapters_md))

    # ============================================================
    # run() 拆分的私有步骤方法
    # ============================================================

    async def _step_v15_deep_mode(self, task):
        """步骤3: V15超深度模式 —— 框架涌现

        如果V15模式成功触发，执行完整V15管线并返回True；
        否则返回False，继续常规模式流程。

        Args:
            task: 当前任务字符串

        Returns:
            True表示V15模式已处理完毕(run()应直接return)，
            False表示未触发V15模式，继续常规流程
        """
        # 超深度报告入口
        self.v15_mode = False
        if self.cfg.get("v15_mode_forced", False):
            self.v15_mode = True
            logger.info("超深度模式：框架涌现")
            self.framework = load_framework_from_text(task, self.llm)
            if not (self.framework and self.framework.is_valid()):
                logger.warning("框架涌现失败，降级V14")
                self.v15_mode = False

        if self.v15_mode and self.framework:
            self.refined_task = task
            self.topic_keywords = self._extract_topic_keywords(task)
            if self.gui and hasattr(self.gui, "wait_for_framework_confirm"):
                framework_text = self.framework.to_markdown()
                action = self.gui.wait_for_framework_confirm(framework_text)
                if action not in ("framework_confirm", "confirm"):
                    logger.info("用户未确认框架，回退常规模式")
                    self.v15_mode = False
                    self.framework = None
                    return False
            logger.info("框架已确认，生成增强调研计划...")
            self._generate_plan_with_framework(task)
            if self.gui:
                result = self.gui.wait_for_plan(self.plan_text)
                if result == "QUIT": return True
                if result and result.strip(): self.plan_text = result
            await self._run_v15_full_pipeline()
            return True

        return False

    def _step_clarify(self, task):
        """步骤4: 澄清追问 —— 失败即抛出，由阶段重试接管

        Args:
            task: 当前任务字符串
        """
        self.refined_task = self.clarify_questions(task)
        if not self.refined_task or not self.refined_task.strip():
            raise RuntimeError("澄清阶段没有返回有效任务描述")
        self.clarify_context = self._extract_clarify_context(self.refined_task)
        if self.clarify_context:
            logger.info(f"[澄清] 已提取用户回答{len(self.clarify_context)}字符，注入后续计划与搜索")
        self.topic_keywords = self._extract_topic_keywords(self.refined_task)

    def _step_setup_search_matrix(self):
        """步骤5: 搜索矩阵配置 —— 从SEARCH_DEPTH_MAP推导搜索参数"""
        # 搜索矩阵——直接使用用户选择的分批数×迭代数×补搜开关
        # 兼容 search_depth 配置：如果新键不存在，从SEARCH_DEPTH_MAP推导
        if "search_batches" not in self.cfg:
            depth_cfg = SEARCH_DEPTH_MAP.get(self.cfg.get("search_depth", "b"), SEARCH_DEPTH_MAP["b"])
            self.cfg["search_batches"] = int(depth_cfg["batches_hint"])
            self.cfg["max_iterations"] = int(depth_cfg["max_iterations"])
            self.cfg["auto_supplement"] = depth_cfg["auto_supplement"]
            if "search_density_top_k" in depth_cfg:
                self.cfg["search_density_top_k"] = depth_cfg["search_density_top_k"]
            if "aggressive_filter_top_k" in depth_cfg:
                self.cfg["aggressive_filter_top_k"] = depth_cfg["aggressive_filter_top_k"]
        logger.info(f"[搜索矩阵] {self.cfg['search_batches']}批 × {self.cfg['max_iterations']}迭代 = {self.cfg['search_batches']*self.cfg['max_iterations']}次搜索 | 补搜={'开' if self.cfg.get('auto_supplement') else '关'}")

    def _step_generate_plan(self, refined_task):
        """步骤6: 生成调研计划 + 搜索策略预审

        Args:
            refined_task: 澄清后的任务描述
        """
        self.generate_plan(refined_task)

        # 搜索策略预审（Agent轻量协作）
        self._preflight_search_queries()

    def _step_confirm_plan(self):
        """步骤7: 人工确认调研计划；有修改意见时重新生成计划。"""
        if not self.gui:
            return
        result = self.gui.wait_for_plan(self.plan_text)
        if result == "QUIT":
            raise RuntimeError("用户取消计划确认")
        if result and result.strip():
            logger.info("[计划确认] 收到用户修改意见，重新生成计划与搜索批次...")
            self._step_generate_plan(
                f"{self.refined_task}\n\n【用户对调研计划的修改意见】\n{result.strip()}"
            )
            logger.info(f"[计划确认] 已按修改意见重新生成 {len(self.search_batches)} 个批次")

    async def _step_deep_search(self):
        """步骤8: 深度搜索。无素材不在此处抛错，交给步骤9自动补搜后再判定。"""
        await self.depth_search()

    def _step_gap_detection(self):
        """步骤9: 覆盖度检查 + 补搜。补搜后仍无素材才判定失败。"""
        coverage = self.check_plan_coverage()
        self.plan_coverage = coverage
        self.auto_supplement_for_gaps(coverage)
        if not self.all_materials:
            # 搜索与补搜同属可重试范围：恢复任务时 search 阶段应重新执行
            if getattr(self, "_state_store", None):
                self._state_store.reset_stage("search")
            if getattr(self.state, "search_failure", False):
                raise RuntimeError("GPTR 搜索失败，自动补搜也未获得可用素材，已终止本次调研")
            raise RuntimeError("深度搜索未获得任何素材，自动补搜也未获得可用素材，已终止本次调研")

    def _step_outline_iteration(self):
        """步骤10: 素材评估与大纲生成。"""
        return self.assess_materials_and_emerge_outline()

    def _step_skeleton_generation(self, refined_task, material_assessment):
        """步骤11: 零样本骨架生成 + 大纲解析 + 素材分配

        Args:
            refined_task: 澄清后的任务描述
            material_assessment: 素材评估结果
        """
        self.zero_draft = self.generate_zero_shot_draft(refined_task, material_assessment)
        z_path = os.path.join(self.cfg["output_dir"], "后台调试", "zero_shot_draft.md")
        os.makedirs(os.path.dirname(z_path), exist_ok=True)
        try:
            os.makedirs(self.cfg["output_dir"], exist_ok=True)
            with open(z_path, "w", encoding="utf-8") as f:
                f.write(self.zero_draft)
            logger.info(f"[骨架文件] 已保存: {z_path}")
        except Exception as e:
            logger.warning(f"[骨架文件] 保存失败: {e}")

        self.outline = self.parse_outline_from_draft(self.zero_draft, material_assessment)
        if not self.outline.get("chapters"):
            raise RuntimeError("大纲生成失败：未解析到任何章节")
        self.report_title = self.outline.get("title", "调研报告")

        # 关键词直接匹配素材（替代记忆库搜索+URL匹配）
        self._auto_assign_chapter_materials()

    def _step_outline_interaction(self, material_assessment):
        """步骤12: 大纲交互 —— GUI 确认/修改/回炉/补搜

        GUI交互支持：confirm/edit/remelt/supplement/quit。
        如果用户选择quit，会直接调用sys.exit(0)。
        """
        if self.gui:
            while True:
                display = self.format_outline_controls(self.outline)
                action, text = self.gui.wait_for_outline_controls(display)
                if action == "confirm":
                    break
                elif action == "edit":
                    self.outline = self.apply_outline_edits(self.outline, text)
                    for i, ch in enumerate(self.outline.get("chapters", [])):
                        sq = ch.pop("_supplement_query", None)
                        if sq:
                            self.supplement_search(i, sq)
                            if self.gui:
                                self.gui._safe_ui(lambda: self.gui.status_label.config(
                                    text=f"第{i+1}章已补搜素材，建议点击【回炉重造骨架】重新生成"
                                ))
                elif action == "remelt":
                    logger.info("[回炉] 重新生成骨架...")
                    self.zero_draft = self.generate_zero_shot_draft(self.refined_task, material_assessment)
                    self.outline = self.parse_outline_from_draft(self.zero_draft, material_assessment)
                    self._supplement_triggered = False
                    logger.info("[回炉] 骨架已更新，刷新控制面")
                elif action == "supplement":
                    logger.info(f"[补搜] 收到指令: {text}")
                    # 兼容多种输入格式
                    # 格式1: "第2章补搜 竞争格局 2026"（原有格式）
                    m = re.search(r"第(\d+)章.*?补搜\s+(.+)", text)
                    if m:
                        idx = int(m.group(1)) - 1
                        query = m.group(2).strip()
                        self.supplement_search(idx, query)
                        if self.gui:
                            self.gui._safe_ui(lambda i=idx: self.gui.status_label.config(
                                text=f"第{i+1}章已补搜，建议点击【回炉重造骨架】重新纳入新素材"
                            ))
                    # 格式2: "补搜第4章" 或 "补搜第4章 关键词"
                    elif re.search(r"补搜第\s*(\d+)", text):
                        m2 = re.search(r"补搜第\s*(\d+)", text)
                        idx = int(m2.group(1)) - 1
                        # 尝试提取查询词，否则用章标题
                        rest = re.sub(r"补搜第\s*\d+\s*", "", text).strip()
                        query = rest if rest else ""
                        self.supplement_search(idx, query)
                        if self.gui:
                            self.gui._safe_ui(lambda i=idx: self.gui.status_label.config(
                                text=f"第{i+1}章已补搜，建议点击【回炉重造骨架】重新纳入新素材"
                            ))
                    # 格式3: "补搜全部" —— 对所有章补搜
                    elif "全部" in text or "所有" in text:
                        chapters = self.outline.get("chapters", [])
                        for i in range(len(chapters)):
                            if i == 0 and ("摘要" in chapters[i].get("title", "") or "发现" in chapters[i].get("title", "")):
                                continue
                            self.supplement_search(i, "")
                        if self.gui:
                            self.gui._safe_ui(lambda: self.gui.status_label.config(
                                text=f"已对所有章节补搜，建议点击【回炉重造骨架】重新纳入新素材"
                            ))
                    else:
                        logger.warning(f"[补搜] 无法解析指令格式: {text}")
                elif action == "quit":
                    sys.exit(0)

    def _step_write_and_polish(self):
        """步骤13: 章节写作 + 降噪 + 最终缺口检查 + 涌现后整理

        包括：结构复审、模式提炼、参考文献生成。
        """
        self.final_draft = self.zero_draft
        self.write_chapters(self.final_draft)

        for idx, ch_text in enumerate(self.chapters_md):
            chapters_list = self.outline.get("chapters", [])
            if idx < len(chapters_list):
                ch_title = chapters_list[idx].get("title", f"第{idx+1}章")
            else:
                ch_title = f"第{idx+1}章"
            try:
                self.chapters_md[idx] = self.chapter_level_denoise(ch_text, ch_title)
                self._persist_chapter(idx, self.chapters_md[idx])
            except Exception as e:
                logger.warning(f"[降噪] 第{idx+1}章跳过: {e}")

        # 最终缺口检查必须审视定稿章节，而不是最初的骨架草稿
        self.final_draft = "\n".join(self.chapters_md)
        gaps = self.final_gap_check()
        if gaps:
            logger.info(f"最终缺口: {gaps}")

        # 涌现后整理层——结构复审 → 模式提炼 → 参考文献
        try:
            self._structure_review()
        except Exception as e:
            logger.warning(f"[结构复审] 跳过: {e}")
        # 统稿与结构复审都可能丢章，这里按骨架对齐并补写缺失章节
        try:
            self._ensure_chapter_integrity()
        except Exception as e:
            logger.warning(f"[章节校验] 跳过: {e}")
        try:
            patterns_md = self._synthesize_patterns()
        except Exception as e:
            logger.warning(f"[模式提炼] 跳过: {e}")
            patterns_md = ""
        reference_md = self._generate_reference_list()
        if patterns_md:
            self.chapters_md.append(patterns_md)
        if reference_md:
            self.chapters_md.append(reference_md)

        # 从最终报告各章中提取声明到账本
        try:
            self._extract_report_claims()
        except Exception as e:
            logger.warning(f"[ClaimLedger] 提取跳过: {e}")

        self._persist_chapters()

    def _step_quality_check(self):
        """步骤14: 质量检查 —— 报告后自检 + 数据防坑 + 来源多样性 + 质量自动评分"""
        # 报告后自检
        self._post_generation_self_check()

        # 数据防坑检查（单位搞混/趋势造假/张冠李戴/跨章一致性）
        self._last_data_trap = self._data_trap_check()

        # 来源多样性硬线 + 数据受限声明
        source_diversity = self._check_source_diversity()
        if source_diversity.get("data_limited"):
            self._insert_data_limited_notice()

        # 质量自动评分
        quality_report = self._quality_scorer()
        if quality_report.get("status") != "disabled":
            self._last_quality_report = quality_report

        # 确定性质量门禁：无来源/推断/被质疑/过时
        if getattr(self, "_quality_gate", None):
            try:
                gate = self._quality_gate.run_gate()
                self._last_gate_result = gate
                if self.event_log:
                    self.event_log.log(
                        "quality_gate", "quality", details=gate.summary,
                        meta={
                            "passed": gate.passed,
                            "unsupported": len(gate.unsupported_claims),
                            "disputed": len(gate.disputed_claims),
                            "stale": len(gate.stale_claims),
                        },
                    )
                logger.info(f"[质量门禁] {gate.summary}")
                if not gate.passed:
                    self._insert_quality_gate_notice(gate)
                if self._state_store:
                    self._state_store.record_deliverable("质量门禁", gate.passed, gate.summary)
            except Exception as e:
                logger.warning(f"[质量门禁] 执行失败: {e}")

    def _step_output_to_gui(self):
        """步骤16: 输出到 GUI"""
        # 记录任务完成事件 + 保存声明账本
        if self.event_log:
            stats = self.claim_ledger.count_by_status() if self.claim_ledger else {}
            self.event_log.log(
                "task_complete",
                "output",
                details=f"声明统计: {stats}",
            )
            # 保存声明账本到调试目录
            if self.claim_ledger:
                try:
                    ledger_path = os.path.join(self.cfg.get("output_dir", ""), "后台调试", f"claim_ledger_{self._task_id}.json")
                    os.makedirs(os.path.dirname(ledger_path), exist_ok=True)
                    self.claim_ledger.save(ledger_path)
                    logger.info(f"[ClaimLedger] 账本已保存: {ledger_path}")
                except Exception as e:
                    logger.warning(f"[ClaimLedger] 保存账本失败: {e}")

        if self.gui:
            self.gui.set_chapters(self.chapters_md)
            self.gui.set_action_plan(self.add_action_plan())
            self.gui.set_all_materials([m.to_dict() if hasattr(m, 'to_dict') else m for m in self.all_materials])

    async def run(self, task: str = None):
        """调研管道主入口 —— 按阶段编排，失败落盘、可重试

        Args:
            task: 可选，调研任务文本。如果为 None，从 GUI 获取或使用默认值。
        """
        if task is None:
            task = self.gui.wait_for_task() if self.gui else "默认调研任务"

        # 任务身份：GUI 可指定 task_id；resume_task_id 表示从上次失败处继续
        self._task_id = self.cfg.get("task_id") or f"research_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        resume_id = self.cfg.get("resume_task_id") or ""
        effective_id = resume_id or self._task_id
        output_dir = self.cfg.get("output_dir") or os.path.join(os.getcwd(), "output")

        self._state_store = TaskStateStore(
            output_dir, effective_id, task_text=task, config=self.cfg
        )
        if resume_id:
            if self._state_store.exists:
                self._restore_from_state()
                task = self.raw_task or task
                logger.info(f"[任务状态] 从上次失败处继续：{effective_id}")
                if self._state_store.data.get("status") == "completed" and self.chapters_md:
                    logger.info("[任务状态] 任务此前已完成，仅重新编译交付物")
                    return
            else:
                logger.warning(f"[任务状态] 未找到任务 {resume_id} 的历史状态，按新任务执行")
        self.raw_task = task

        # 初始化声明账本和事件日志（每个任务独立）
        if _HAS_CLAIM_LEDGER:
            self.claim_ledger = ClaimLedger(project_id=self._task_id)
            self.event_log = EventLog(project_id=self._task_id)
            self._quality_gate = QualityGate(self.claim_ledger)
            self.event_log.log("task_start", "init", details=f"任务: {task[:80]}")
            # 将声明账本注入到素材综合器
            self._synthesizer.claim_ledger = self.claim_ledger

        prepared = await self._run_stage("prepare", lambda: self._step_prepare_task(task))
        if prepared is not None:
            task = prepared
        self._snapshot_pipeline()

        if await self._run_stage("v15", lambda: self._step_v15_deep_mode(task)):
            # 超深度路径也需要把章节/素材/账本交给 GUI，否则交付编译拿不到数据
            await self._run_stage("output", lambda: self._step_output_to_gui())
            self._state_store.complete()
            return  # 模式已处理

        await self._run_stage("clarify", lambda: self._step_clarify(task))
        self._snapshot_pipeline()

        def _plan_stage():
            self._step_setup_search_matrix()
            self._step_generate_plan(self.refined_task)

        await self._run_stage("plan", _plan_stage)
        self._snapshot_pipeline()

        await self._run_stage("plan_confirm", self._step_confirm_plan)
        self._snapshot_pipeline()

        await self._run_stage("search", lambda: self._step_deep_search())
        self._save_materials()
        self._snapshot_pipeline()

        await self._run_stage("gap_check", self._step_gap_detection)
        self._save_materials()
        self._snapshot_pipeline()

        await self._run_stage(
            "outline",
            lambda: self._set_material_assessment(self._step_outline_iteration()),
        )
        self._save_materials()
        self._snapshot_pipeline()

        await self._run_stage(
            "skeleton",
            lambda: self._step_skeleton_generation(self.refined_task, self._material_assessment),
        )
        self._state_store.save_artifact("outline", self.outline)
        self._state_store.save_artifact("material_assessment", self._material_assessment)
        self._state_store.save_text("zero_shot_draft.md", self.zero_draft or "")
        self._snapshot_pipeline()

        await self._run_stage(
            "outline_confirm",
            lambda: self._step_outline_interaction(self._material_assessment),
        )

        await self._run_stage("writing", lambda: self._step_write_and_polish())
        self._persist_chapters()
        self._snapshot_pipeline()

        # 质量检查为非核心阶段，失败会记录但不阻断交付
        await self._run_stage("quality", lambda: self._step_quality_check(), critical=False)

        await self._run_stage("output", lambda: self._step_output_to_gui())
        self._state_store.complete()
        logger.info(f"[任务状态] 任务完成：{effective_id}")
