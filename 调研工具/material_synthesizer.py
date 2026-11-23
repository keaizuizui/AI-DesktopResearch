#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
素材综合器 — 复刻自 DeepResearcher 的 LLM 综合报告模式
======================================================

专门用于调研工具的素材聚类后综合，不修改方案工具的 deep_research.py。

核心能力：
- 接收素材列表（SeachResult 或 dict），LLM 综合为结构化报告
- 每个素材簇输出：3-5 关键发现 + 2-4 量化数据点 + 200-300 字摘要 + 来源列表
- 支持批量合成（多个主题簇并行）
- 同步/异步双模式

与 DeepResearcher 的关键差异：
- DeepResearcher: 搜索 → 缺口分析 → 补搜 → LLM 合成（业务代码，不动）
- MaterialSynthesizer: 素材列表 → LLM 合成（纯检索+综合，无搜索逻辑）

用法：
    from material_synthesizer import MaterialSynthesizer, MaterialCluster, SynthesisReport

    synth = MaterialSynthesizer(llm=engine.llm)
    clusters = [MaterialCluster(name="市场规模", core_materials=[...], supplementary_materials=[...])]
    reports = await synth.synthesize_all(clusters, query="2024年新能源汽车市场调研")

作者: PersuadeAI Team
版本: v1.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Union

import numpy as np

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════════════════

@dataclass
class MaterialCluster:
    """
    素材簇 — 聚类后的一个主题分组

    Attributes:
        name: 主题名（如"市场规模"、"竞争格局"、"供应链"）
        core_materials: 核心素材（3-5条），扩写时完整使用
        supplementary_materials: 其余素材，扩写时给摘要
        total_count: 该簇总素材数
    """
    name: str
    core_materials: List[Dict] = field(default_factory=list)
    supplementary_materials: List[Dict] = field(default_factory=list)
    total_count: int = 0

    def __post_init__(self):
        if self.total_count == 0:
            self.total_count = len(self.core_materials) + len(self.supplementary_materials)

    @property
    def all_materials(self) -> List[Dict]:
        """所有素材（核心 + 补充）"""
        return self.core_materials + self.supplementary_materials


@dataclass
class SynthesisReport:
    """
    综合报告 — LLM 对素材簇的综合输出

    Attributes:
        cluster_name: 对应的主题簇名
        summary: 200-300字研究摘要
        key_findings: 3-5个关键发现
        data_points: 2-4个量化数据点
        sources: 来源URL列表
        material_count: 被综合的素材数
        raw_response: LLM原始响应（调试用）
    """
    cluster_name: str
    summary: str = ""
    key_findings: List[str] = field(default_factory=list)
    data_points: List[str] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)
    material_count: int = 0
    raw_response: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cluster_name": self.cluster_name,
            "summary": self.summary,
            "key_findings": self.key_findings,
            "data_points": self.data_points,
            "sources": self.sources,
            "material_count": self.material_count,
        }

    def to_prompt_block(self) -> str:
        """
        转换为骨架涌现阶段可直接使用的文本块

        格式适配 assess_materials_and_emerge_outline 的输入需求，
        每个簇约 400 字，5-8 个簇总量 2000-3200 字。
        """
        lines = [f"## 主题簇: {self.cluster_name}"]
        if self.summary:
            lines.append(f"摘要: {self.summary}")
        if self.key_findings:
            lines.append("关键发现:")
            for f in self.key_findings:
                lines.append(f"  - {f}")
        if self.data_points:
            lines.append("数据点:")
            for d in self.data_points:
                lines.append(f"  - {d}")
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
# 素材综合器
# ═══════════════════════════════════════════════════════════════════

class MaterialSynthesizer:
    """
    素材综合器

    复刻 DeepResearcher._synthesize_report 的 LLM 综合报告模式，
    但专门用于调研工具的素材聚类后综合，不含搜索逻辑。

    使用方式：
        # 方式1: 传入 LLMClient（engine.llm）
        synth = MaterialSynthesizer(llm=engine.llm)

        # 方式2: 传入 model_router（方案工具风格）
        synth = MaterialSynthesizer(model_router=router)

        # 方式3: 仅用于格式化（无 LLM，返回摘要拼接）
        synth = MaterialSynthesizer()

        reports = await synth.synthesize_all(clusters, query="...")
    """

    # 素材综合的 system prompt（复用 DeepResearcher 的研究分析师角色）
    SYSTEM_PROMPT = (
        "你是一位专业研究分析师，擅长从大量素材中提炼关键信息。"
        "你需要基于提供的素材列表，生成一份精准、结构化的综合报告。"
        "只提取素材中明确存在的证据，不编造、不推测。"
    )

    def __init__(
        self,
        llm=None,           # LLMClient（engine.llm）
        model_router=None,  # ModelRouter（方案工具风格）
        max_materials_per_synthesis: int = 30,
        material_content_max_chars: int = 500,
        claim_ledger=None,   # 可选的声明账本，材料合成时注册来源
    ):
        """
        Args:
            llm: LLMClient 实例（调研工具引擎的 self.llm）
            model_router: ModelRouter 实例（备选，方案工具风格）
            max_materials_per_synthesis: 单次综合最多使用的素材数
            material_content_max_chars: 每条素材内容的最大字符数
            claim_ledger: 可选的 ClaimLedger 实例，合成时将素材来源注册到账本
        """
        self.llm = llm
        self.model_router = model_router
        self.max_materials = max_materials_per_synthesis
        self.content_max_chars = material_content_max_chars
        self.claim_ledger = claim_ledger  # 声明账本

        # 判断可用模式
        if self.llm and hasattr(self.llm, 'available') and self.llm.available:
            self._mode = "role_based"
        elif self.model_router:
            self._mode = "model_router"
        else:
            self._mode = "fallback"
            logger.warning("MaterialSynthesizer: 无可用 LLM，将使用摘要拼接模式")

        logger.info("MaterialSynthesizer 初始化: mode=%s, max_materials=%d, content_max=%d",
                     self._mode, self.max_materials, self.content_max_chars)

    # ─── 公共接口 ─────────────────────────────────────────────────

    async def synthesize_cluster(
        self,
        cluster: MaterialCluster,
        query: str = "",
        context: Optional[Dict] = None,
    ) -> SynthesisReport:
        """
        对单个素材簇做 LLM 综合

        Args:
            cluster: 素材簇
            query: 研究主题（用于上下文提示）
            context: 额外上下文（行业、章节类型等）

        Returns:
            SynthesisReport
        """
        logger.info("综合素材簇: name=%r, materials=%d", cluster.name, cluster.total_count)

        if not cluster.all_materials:
            return SynthesisReport(
                cluster_name=cluster.name,
                summary="该主题无素材可用。",
                material_count=0,
            )

        if self._mode == "fallback":
            return self._fallback_synthesis(cluster, query)

        return await self._llm_synthesize(cluster, query, context)

    async def synthesize_all(
        self,
        clusters: List[MaterialCluster],
        query: str = "",
        context: Optional[Dict] = None,
        max_concurrency: int = 5,
    ) -> List[SynthesisReport]:
        """
        批量综合所有素材簇（并行）

        Args:
            clusters: 素材簇列表
            query: 研究主题
            context: 额外上下文
            max_concurrency: 最大并行数

        Returns:
            SynthesisReport 列表
        """
        if not clusters:
            return []

        logger.info("批量综合 %d 个素材簇, max_concurrency=%d", len(clusters), max_concurrency)

        semaphore = asyncio.Semaphore(max_concurrency)

        async def _synthesize_one(cluster: MaterialCluster) -> SynthesisReport:
            async with semaphore:
                return await self.synthesize_cluster(cluster, query, context)

        tasks = [_synthesize_one(c) for c in clusters]
        return await asyncio.gather(*tasks)

    def synthesize_cluster_sync(
        self,
        cluster: MaterialCluster,
        query: str = "",
        context: Optional[Dict] = None,
    ) -> SynthesisReport:
        """
        同步版本的单个素材簇综合

        用于无法使用 async/await 的旧引擎上下文。
        """
        if not cluster.all_materials:
            return SynthesisReport(
                cluster_name=cluster.name,
                summary="该主题无素材可用。",
                material_count=0,
            )

        if self._mode == "fallback":
            return self._fallback_synthesis(cluster, query)

        return self._llm_synthesize_sync(cluster, query, context)

    def synthesize_all_sync(
        self,
        clusters: List[MaterialCluster],
        query: str = "",
        context: Optional[Dict] = None,
    ) -> List[SynthesisReport]:
        """
        同步版本的批量综合（串行）
        """
        return [self.synthesize_cluster_sync(c, query, context) for c in clusters]

    def build_full_briefing(
        self,
        reports: List[SynthesisReport],
        query: str = "",
    ) -> str:
        """
        将多个 SynthesisReport 合并为骨架涌现用的完整简报

        输出格式适配 assess_materials_and_emerge_outline 的输入，
        5-8 个簇 × 约 400 字 = 2000-3200 字，上下文友好。

        Args:
            reports: 综合报告列表
            query: 研究主题

        Returns:
            格式化的简报文本
        """
        lines = []
        if query:
            lines.append(f"# 研究主题: {query}")
            lines.append("")

        total_materials = sum(r.material_count for r in reports)
        lines.append(f"以下简报基于 {len(reports)} 个主题簇、共 {total_materials} 条素材综合而成：")
        lines.append("")

        for i, report in enumerate(reports, 1):
            lines.append(report.to_prompt_block())
            lines.append("")

        return "\n".join(lines)

    # ─── LLM 综合核心 ─────────────────────────────────────────────

    def _build_materials_text(self, materials: List[Dict]) -> str:
        """构建素材列表文本（供 LLM prompt 使用）"""
        parts = []
        for i, m in enumerate(materials[:self.max_materials], 1):
            title = self._extract_field(m, "title", f"素材{i}")
            content = self._extract_field(m, "content", self._extract_field(m, "snippet", ""))
            url = self._extract_field(m, "url", "")
            grade = self._extract_field(m, "grade", "")
            source = self._extract_field(m, "source", "")

            # 截断内容
            content = content[:self.content_max_chars] if content else ""

            # 信源标签
            grade_label = {"S": "[权威]", "A": "[可靠]", "B": "", "C": "[低信]"}.get(
                str(grade).upper() if grade else "", ""
            )

            parts.append(
                f"[{i}] {grade_label} {title}\n"
                f"    来源: {source} | {url}\n"
                f"    内容: {content}\n"
            )

        # 如果素材超过上限，添加提示
        if len(materials) > self.max_materials:
            skipped = len(materials) - self.max_materials
            parts.append(f"\n（另有 {skipped} 条素材因篇幅限制未列出，请综合考量）")

        return "\n".join(parts)

    async def _llm_synthesize(
        self,
        cluster: MaterialCluster,
        query: str,
        context: Optional[Dict],
    ) -> SynthesisReport:
        """异步 LLM 综合"""
        prompt = self._build_synthesis_prompt(cluster, query, context)

        try:
            if self._mode == "role_based":
                response = await self._call_role_based_llm(prompt)
            elif self._mode == "model_router":
                response = await self._call_model_router(prompt)
            else:
                return self._fallback_synthesis(cluster, query)

            return self._parse_response(response, cluster)

        except Exception as e:
            logger.warning("素材簇 %r 综合失败: %s，使用回退模式", cluster.name, e)
            return self._fallback_synthesis(cluster, query)

    def _llm_synthesize_sync(
        self,
        cluster: MaterialCluster,
        query: str,
        context: Optional[Dict],
    ) -> SynthesisReport:
        """同步 LLM 综合"""
        prompt = self._build_synthesis_prompt(cluster, query, context)

        try:
            if self._mode == "role_based":
                response = self._call_role_based_llm_sync(prompt)
            elif self._mode == "model_router":
                # model_router 是异步的，用 asyncio.run 包装
                response = asyncio.run(self._call_model_router(prompt))
            else:
                return self._fallback_synthesis(cluster, query)

            return self._parse_response(response, cluster)

        except Exception as e:
            logger.warning("素材簇 %r 同步综合失败: %s，使用回退模式", cluster.name, e)
            return self._fallback_synthesis(cluster, query)

    def _build_synthesis_prompt(
        self,
        cluster: MaterialCluster,
        query: str,
        context: Optional[Dict],
    ) -> str:
        """构建综合 prompt"""
        materials_text = self._build_materials_text(cluster.all_materials)

        context_str = json.dumps(context, ensure_ascii=False) if context else "无"

        return f"""基于以下素材列表，生成一份结构化的综合简报。

【研究主题】
{query or "未指定"}

【主题簇】
{cluster.name}

【上下文】
{context_str}

【素材列表】（共 {cluster.total_count} 条）
{materials_text}

【任务】
1. 提炼 3-5 个关键发现（基于素材中的明确证据）
2. 提取 2-4 个量化数据点（如有，保留原始数字和单位）
3. 撰写一段 200-300 字的研究摘要（覆盖该主题的核心信息）
4. 列出信息来源 URL（仅列出素材中明确出现的 URL）

【输出要求】
- 只输出纯 JSON 对象，不要解释文字
- 确保 JSON 语法正确
- 标准格式：
{{{{
    "summary": "研究摘要（200-300字）",
    "key_findings": ["发现1", "发现2", "发现3"],
    "data_points": ["数据点1: 具体数值", "数据点2: 具体数值"],
    "sources": ["url1", "url2"]
}}}}

仅输出 JSON。"""

    async def _call_role_based_llm(self, prompt: str) -> str:
        """通过 LLMClient 调用 LLM（异步）"""
        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]

        # 使用 plan 角色（研究规划/分析），temperature 低以确保一致性
        if hasattr(self.llm, 'plan'):
            return await self._to_async(self.llm.plan, messages, temperature=0.4, max_tokens=1500)
        elif hasattr(self.llm, 'fast'):
            return await self._to_async(self.llm.fast, messages, max_tokens=1500, json_mode=True)
        else:
            return await self._to_async(self.llm.chat, messages, temperature=0.4)

    def _call_role_based_llm_sync(self, prompt: str) -> str:
        """通过 LLMClient 调用 LLM（同步）"""
        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]

        if hasattr(self.llm, 'plan'):
            return self.llm.plan(messages, temperature=0.4, max_tokens=1500)
        elif hasattr(self.llm, 'fast'):
            return self.llm.fast(messages, max_tokens=1500, json_mode=True)
        else:
            return self.llm.chat(messages, temperature=0.4)

    async def _call_model_router(self, prompt: str) -> str:
        """通过 model_router 调用 LLM（异步）"""
        return await self.model_router.generate(
            model_type="smart",
            prompt=self.SYSTEM_PROMPT + "\n\n" + prompt,
            temperature=0.4,
            max_tokens=1500,
        )

    async def _to_async(self, fn, *args, **kwargs):
        """将同步函数转为异步执行"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    def _parse_response(self, response: str, cluster: MaterialCluster) -> SynthesisReport:
        """解析 LLM 响应为 SynthesisReport"""
        # 尝试提取 JSON
        json_match = re.search(r'\{.*\}', response, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group())
                report = SynthesisReport(
                    cluster_name=cluster.name,
                    summary=data.get("summary", ""),
                    key_findings=data.get("key_findings", []),
                    data_points=data.get("data_points", []),
                    sources=data.get("sources", []),
                    material_count=cluster.total_count,
                    raw_response=response,
                )
                # 将合成结果的关键发现注册到声明账本
                self._register_synthesis_claims(report, cluster)
                return report
            except json.JSONDecodeError as e:
                logger.warning("JSON 解析失败: %s，原始响应前200字: %s", e, response[:200])

        # 解析失败，回退到简单提取
        fallback_report = self._fallback_synthesis(cluster, "", response)
        # 回退模式也注册来源
        self._register_synthesis_claims(fallback_report, cluster)
        return fallback_report

    def _register_synthesis_claims(self, report: SynthesisReport, cluster: MaterialCluster):
        """将素材合成的关键发现注册到声明账本

        来源标记策略：
        - 有 URL 的素材：来源标记为搜索结果 URL
        - LLM 综合出的洞察：来源标记为"基于搜索结果推断"（INFERRED）
        - 用户提供的材料：来源标记为文件名
        """
        if not self.claim_ledger:
            return

        try:
            from shared.claim_ledger import ClaimType, ClaimStatus
        except ImportError:
            return

        # 将主题簇名作为声明字段
        cluster_name = cluster.name

        # 注册摘要
        if report.summary:
            self.claim_ledger.register_claim(
                field_name=f"素材合成摘要:{cluster_name}",
                value=report.summary[:200],
                claim_type=ClaimType.OPINION,
                source="基于搜索结果推断",
                source_id="material_synthesizer",
                writer="material_synthesizer",
                status=ClaimStatus.INFERRED,
            )

        # 注册关键发现
        for i, finding in enumerate(report.key_findings):
            self.claim_ledger.register_claim(
                field_name=f"发现:{cluster_name}_{i}",
                value=finding,
                claim_type=ClaimType.OPINION,
                source="基于搜索结果推断",
                source_id="material_synthesizer",
                writer="material_synthesizer",
                status=ClaimStatus.INFERRED,
            )

        # 注册数据点（有明确来源URL的标记为SUPPORTED）
        for i, dp in enumerate(report.data_points):
            # 尝试匹配来源URL
            dp_source = "基于搜索结果推断"
            dp_source_id = "material_synthesizer"
            dp_status = ClaimStatus.INFERRED
            # 如果数据点内容与某个素材标题/URL匹配，标记来源
            for mat in cluster.all_materials:
                mat_title = self._extract_field(mat, "title", "")
                mat_url = self._extract_field(mat, "url", "")
                if mat_title and mat_title in dp:
                    dp_source = mat_url if mat_url else mat_title
                    dp_source_id = mat_url
                    dp_status = ClaimStatus.SUPPORTED
                    break
            self.claim_ledger.register_claim(
                field_name=f"数据点:{cluster_name}_{i}",
                value=dp,
                claim_type=ClaimType.METRIC,
                source=dp_source,
                source_id=dp_source_id,
                writer="material_synthesizer",
                status=dp_status,
            )

        # 注册素材来源URL（标记为SUPPORTED）
        for src_url in report.sources:
            if src_url and src_url.startswith("http"):
                self.claim_ledger.register_claim(
                    field_name=f"引用来源:{cluster_name}",
                    value=src_url,
                    claim_type=ClaimType.FACT,
                    source=src_url,
                    source_id=src_url,
                    writer="material_synthesizer",
                    status=ClaimStatus.SUPPORTED,
                )

        logger.info(f"[ClaimLedger] 素材簇 '{cluster_name}' 注册 "
                     f"{len(report.key_findings)} 发现 + {len(report.data_points)} 数据点")

    # ─── 回退模式 ─────────────────────────────────────────────────

    def _fallback_synthesis(
        self,
        cluster: MaterialCluster,
        query: str = "",
        raw_response: str = "",
    ) -> SynthesisReport:
        """
        无 LLM 时的回退综合

        直接拼接素材摘要，不经过 LLM 提炼。
        """
        mats = cluster.all_materials[:self.max_materials]
        titles = []
        snippets = []
        urls = []

        for m in mats:
            title = self._extract_field(m, "title", "")
            if title:
                titles.append(title)
            content = self._extract_field(m, "content", self._extract_field(m, "snippet", ""))
            if content:
                snippets.append(content[:200])
            url = self._extract_field(m, "url", "")
            if url:
                urls.append(url)

        summary = f"基于 {cluster.total_count} 条素材的自动摘要（未经过 LLM 提炼）。\n"
        summary += "\n".join(f"- {s}" for s in snippets[:5])

        return SynthesisReport(
            cluster_name=cluster.name,
            summary=summary,
            key_findings=titles[:5],
            data_points=[],
            sources=urls,
            material_count=cluster.total_count,
            raw_response=raw_response,
        )

    # ─── 工具方法 ─────────────────────────────────────────────────

    @staticmethod
    def _extract_field(material: Any, field: str, default: str = "") -> str:
        """
        从素材中提取字段（兼容 SearchResult 对象和 dict）

        素材可能是 shared.search.models.SearchResult 对象（有属性访问）
        或普通 dict（有键访问），或 engine 中的 SearchResult（dataclass）。
        """
        if material is None:
            return default

        # 尝试属性访问（dataclass/对象）
        if hasattr(material, field):
            val = getattr(material, field, default)
            if val is not None:
                # 处理 enum 类型（如 SourceGrade）
                if hasattr(val, 'value'):
                    return str(val.value)
                return str(val)

        # 尝试字典访问
        if isinstance(material, dict):
            val = material.get(field, default)
            if val is not None:
                return str(val)

        return default

    @staticmethod
    def from_search_results(
        cluster_name: str,
        results: List[Any],
        core_count: int = 5,
    ) -> MaterialCluster:
        """
        从 SearchResult 列表创建 MaterialCluster（便捷方法）

        Args:
            cluster_name: 主题名
            results: SearchResult 列表
            core_count: 核心素材数量（前 N 条）

        Returns:
            MaterialCluster
        """
        core = results[:core_count]
        supplementary = results[core_count:] if len(results) > core_count else []
        return MaterialCluster(
            name=cluster_name,
            core_materials=core,
            supplementary_materials=supplementary,
            total_count=len(results),
        )


# ═══════════════════════════════════════════════════════════════════
# 便捷函数
# ═══════════════════════════════════════════════════════════════════

def create_clusters_from_materials(
    materials: List[Any],
    cluster_map: Dict[str, List[int]],
) -> List[MaterialCluster]:
    """
    根据聚类结果创建 MaterialCluster 列表

    配合 Ollama + jina embedding 聚类步骤使用。

    Args:
        materials: 全量素材列表（SearchResult 或 dict）
        cluster_map: 聚类结果，格式 {主题名: [素材索引列表]}

    Returns:
        MaterialCluster 列表

    Example:
        # 聚类结果示例
        cluster_map = {
            "市场规模": [0, 3, 7, 12, 15, 22, 28],
            "竞争格局": [1, 4, 8, 13, 18],
            "技术趋势": [2, 5, 9, 14, 19, 23],
        }
        clusters = create_clusters_from_materials(all_materials, cluster_map)
    """
    clusters = []
    for name, indices in cluster_map.items():
        cluster_mats = [materials[i] for i in indices if i < len(materials)]
        # 前 5 条作为核心素材
        core = cluster_mats[:5]
        supplementary = cluster_mats[5:] if len(cluster_mats) > 5 else []
        clusters.append(MaterialCluster(
            name=name,
            core_materials=core,
            supplementary_materials=supplementary,
            total_count=len(cluster_mats),
        ))
    return clusters


# ═══════════════════════════════════════════════════════════════════
# 素材聚类器
# ═══════════════════════════════════════════════════════════════════

class MaterialClusterer:
    """
    素材聚类器 — 使用 Ollama 本地 embedding 对全量素材做语义聚类

    核心流程：
    1. 对全部素材做 embedding（Ollama API）
    2. 余弦相似度矩阵 → 层次聚类（noise-aware）
    3. LLM 为每个簇命名（主题名）
    4. 输出 List[MaterialCluster]

    与 MaterialSynthesizer 配合使用：
        clusterer = MaterialClusterer(ollama_host="http://localhost:11434",
                                      ollama_model="EntropyYue/jina-embeddings-v2-base-zh:latest")
        clusters = clusterer.cluster(all_materials, min_clusters=5, max_clusters=8)
        synth = MaterialSynthesizer(llm=engine.llm)
        reports = synth.synthesize_all_sync(clusters, query="...")
        briefing = synth.build_full_briefing(reports, query="...")

    依赖: numpy, requests, sklearn（可选，用于层次聚类）

    作者: PersuadeAI Team
    版本: v1.0
    """

    def __init__(
        self,
        ollama_host: str = "http://localhost:11434",
        ollama_model: str = "EntropyYue/jina-embeddings-v2-base-zh:latest",
        embed_batch_size: int = 8,
        similarity_threshold: float = 0.5,
        min_cluster_size: int = 3,
    ):
        """
        Args:
            ollama_host: Ollama 服务地址
            ollama_model: embedding 模型名称
            embed_batch_size: 每批 embedding 的素材数
            similarity_threshold: 聚类相似度阈值（低于此值视为噪声/独立簇）
            min_cluster_size: 最小簇大小
        """
        self.ollama_host = ollama_host.rstrip("/")
        self.ollama_model = ollama_model
        self.embed_batch_size = embed_batch_size
        self.similarity_threshold = similarity_threshold
        self.min_cluster_size = min_cluster_size
        self._cached_embeddings: Optional[np.ndarray] = None
        self._cached_clusters: Optional[List[MaterialCluster]] = None

        # 延迟导入 requests
        self._requests = None

    def _ensure_requests(self):
        """延迟导入 requests"""
        if self._requests is None:
            import requests
            self._requests = requests

    def embed(self, texts: List[str]) -> np.ndarray:
        """
        批量计算 embedding（Ollama /api/embed 批量端点，失败回退逐条）

        失败条目不再写死 768 维零向量，而是按实际模型维度补齐，
        避免 bge-m3（1024 维）与旧模型混用时数组形状不一致。
        """
        self._ensure_requests()
        vectors: List[Optional[List[float]]] = [None] * len(texts)
        ollama_down = False
        for start in range(0, len(texts), self.embed_batch_size):
            batch = [t[:600] for t in texts[start:start + self.embed_batch_size]]
            try:
                r = self._requests.post(
                    f"{self.ollama_host}/api/embed",
                    json={"model": self.ollama_model, "input": batch},
                    timeout=120,
                )
                r.raise_for_status()
                embeddings = r.json().get("embeddings", [])
                for offset, emb in enumerate(embeddings[:len(batch)]):
                    if isinstance(emb, list) and emb:
                        vectors[start + offset] = emb
                if any(v is not None for v in vectors[start:start + len(batch)]):
                    continue
            except Exception:
                pass

            # 回退：逐条调用 /api/embeddings；本批批量请求未拿到任何向量且首条也失败，
            # 判定 Ollama 不可用，不再让后续批次重复等待超时
            for j, t in enumerate(batch):
                try:
                    r = self._requests.post(
                        f"{self.ollama_host}/api/embeddings",
                        json={"model": self.ollama_model, "prompt": t},
                        timeout=30,
                    )
                    r.raise_for_status()
                    vec = r.json().get("embedding")
                    if isinstance(vec, list) and vec:
                        vectors[start + j] = vec
                    else:
                        raise RuntimeError("空向量")
                except Exception as e:
                    logger.debug(f"[Clusterer] embedding 失败: {e}")
                    if j == 0:
                        ollama_down = True
                        logger.warning("[Clusterer] Ollama 不可用，停止剩余批次")
                        break
            if ollama_down:
                break

        dim = next((len(v) for v in vectors if v), 0)
        if dim == 0:
            logger.warning("[Clusterer] Ollama 未返回可用向量，本次跳过聚类")
            return np.zeros((0, 0), dtype=np.float32)
        filled = [v if v else [0.0] * dim for v in vectors]
        return np.array(filled, dtype=np.float32)

    def _build_similarity_matrix(self, embeddings: np.ndarray) -> np.ndarray:
        """构建余弦相似度矩阵"""
        # 归一化
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        norms[norms == 0] = 1e-10
        normalized = embeddings / norms
        # 余弦相似度 = 点积
        return np.dot(normalized, normalized.T)

    def _agglomerative_cluster(
        self,
        sim_matrix: np.ndarray,
        n: int,
        min_clusters: int,
        max_clusters: int,
    ) -> List[List[int]]:
        """
        自底向上层次聚类

        从 n 个单独簇开始，不断合并最相似的两个簇，直到簇数在目标范围内。
        合并策略：取两个簇中所有素材对的最大相似度（complete-linkage 变体）。

        Args:
            sim_matrix: 相似度矩阵 (n, n)
            n: 素材总数
            min_clusters: 最小簇数
            max_clusters: 最大簇数

        Returns:
            聚类结果，每个元素是素材索引列表
        """
        # 初始化：每个素材一个簇
        clusters = [[i] for i in range(n)]
        # 簇间相似度缓存
        n_clusters = len(clusters)

        # 迭代合并直到簇数在目标范围内
        while n_clusters > max_clusters:
            # 找最相似的两个簇
            best_sim = -1.0
            best_pair = (0, 1)
            for i in range(len(clusters)):
                for j in range(i + 1, len(clusters)):
                    # 两个簇间所有素材对的平均相似度
                    sims = []
                    for a in clusters[i]:
                        for b in clusters[j]:
                            sims.append(sim_matrix[a, b])
                    avg_sim = sum(sims) / max(len(sims), 1)
                    if avg_sim > best_sim:
                        best_sim = avg_sim
                        best_pair = (i, j)

            # 合并
            i, j = best_pair
            clusters[i].extend(clusters[j])
            clusters.pop(j)
            n_clusters = len(clusters)

        # 尝试继续合并直到达到 min_clusters（如果相似度足够高）
        while n_clusters > min_clusters:
            best_sim = -1.0
            best_pair = (0, 1)
            for i in range(len(clusters)):
                for j in range(i + 1, len(clusters)):
                    sims = []
                    for a in clusters[i]:
                        for b in clusters[j]:
                            sims.append(sim_matrix[a, b])
                    avg_sim = sum(sims) / max(len(sims), 1)
                    if avg_sim > best_sim:
                        best_sim = avg_sim
                        best_pair = (i, j)

            if best_sim < self.similarity_threshold:
                break  # 相似度不够，不再合并

            i, j = best_pair
            clusters[i].extend(clusters[j])
            clusters.pop(j)
            n_clusters = len(clusters)

        return clusters

    def cluster(
        self,
        materials: List[Any],
        min_clusters: int = 5,
        max_clusters: int = 8,
    ) -> List[MaterialCluster]:
        """
        对素材做语义聚类

        Args:
            materials: 素材列表（SearchResult 或 dict）
            min_clusters: 最小簇数
            max_clusters: 最大簇数

        Returns:
            MaterialCluster 列表
        """
        n = len(materials)
        if n == 0:
            return []
        if n <= min_clusters:
            # 素材太少，每个素材一个簇
            return [
                MaterialCluster(
                    name=f"主题{i+1}",
                    core_materials=[materials[i]],
                    total_count=1,
                )
                for i in range(n)
            ]

        logger.info("聚类开始: %d 条素材 → %d-%d 个簇", n, min_clusters, max_clusters)

        # 1. 构建 embedding 文本（标题 + 内容前 800 字）
        texts = []
        for m in materials:
            title = MaterialSynthesizer._extract_field(m, "title", "")
            content = MaterialSynthesizer._extract_field(
                m, "content",
                MaterialSynthesizer._extract_field(m, "snippet", ""),
            )
            texts.append(f"{title} {content[:800]}")

        # 2. 计算 embedding
        embeddings = self.embed(texts)
        logger.info("聚类: embedding 完成, shape=%s", embeddings.shape)
        if embeddings.shape[0] != n or embeddings.shape[1] == 0:
            logger.warning("[Clusterer] 向量不可用，退化为按素材逐条分簇")
            return [
                MaterialCluster(
                    name=f"主题{i+1}",
                    core_materials=[materials[i]],
                    total_count=1,
                )
                for i in range(n)
            ]

        # 3. 构建相似度矩阵
        sim_matrix = self._build_similarity_matrix(embeddings)

        # 4. 层次聚类
        raw_clusters = self._agglomerative_cluster(
            sim_matrix, n, min_clusters, max_clusters
        )

        # 5. 过滤过小的簇（合并到最近的大簇）
        valid_clusters = []
        noise = []
        for c in raw_clusters:
            if len(c) >= self.min_cluster_size:
                valid_clusters.append(c)
            else:
                noise.extend(c)

        if noise and valid_clusters:
            # 将噪声素材分配到最近的簇
            for idx in noise:
                best_cluster = 0
                best_sim = -1.0
                for ci, c in enumerate(valid_clusters):
                    sims = [sim_matrix[idx, i] for i in c]
                    avg_sim = sum(sims) / max(len(sims), 1)
                    if avg_sim > best_sim:
                        best_sim = avg_sim
                        best_cluster = ci
                valid_clusters[best_cluster].append(idx)

        # 6. 构建 MaterialCluster 列表
        clusters = []
        for raw_c in valid_clusters:
            cluster_mats = [materials[i] for i in raw_c]
            core = cluster_mats[:5]
            supplementary = cluster_mats[5:] if len(cluster_mats) > 5 else []
            # 临时名称，后续可用 LLM 命名
            temp_name = f"主题簇{len(clusters)+1}"
            clusters.append(MaterialCluster(
                name=temp_name,
                core_materials=core,
                supplementary_materials=supplementary,
                total_count=len(cluster_mats),
            ))

        logger.info("聚类完成: %d 个簇, 大小: %s",
                     len(clusters),
                     [c.total_count for c in clusters])

        return clusters

    def name_clusters_with_llm(
        self,
        clusters: List[MaterialCluster],
        llm=None,
    ) -> List[MaterialCluster]:
        """
        使用 LLM 为聚类簇命名

        如果未提供 LLM，使用簇中最核心素材的标题关键词作为簇名。

        Args:
            clusters: 聚类簇列表
            llm: LLMClient 实例（可选）

        Returns:
            命名后的 MaterialCluster 列表
        """
        for c in clusters:
            if llm and hasattr(llm, 'available') and llm.available:
                c.name = self._llm_name_cluster(c, llm)
            else:
                c.name = self._keyword_name_cluster(c)
        return clusters

    def _llm_name_cluster(self, cluster: MaterialCluster, llm) -> str:
        """LLM 为簇命名"""
        titles = []
        for m in cluster.core_materials[:5]:
            t = MaterialSynthesizer._extract_field(m, "title", "")
            if t:
                titles.append(t)
        titles_text = "\n".join(f"- {t}" for t in titles)

        prompt = f"""基于以下素材标题，推断这个主题簇的名称。名称应简洁（2-6字），描述该簇覆盖的核心主题。

素材标题：
{titles_text}

请只输出主题名称，不要解释。"""

        try:
            name = llm.fast(
                [{"role": "user", "content": prompt}],
                max_tokens=50,
                temperature=0.3,
            )
            name = name.strip().strip('"').strip("'")
            if len(name) > 15:
                name = name[:15]
            return name or self._keyword_name_cluster(cluster)
        except Exception as e:
            logger.debug(f"[Clusterer] LLM 命名失败: {e}")
            return self._keyword_name_cluster(cluster)

    def _keyword_name_cluster(self, cluster: MaterialCluster) -> str:
        """关键词提取簇名"""
        # 取核心素材标题中最长的中文短语
        all_titles = []
        for m in cluster.core_materials[:5]:
            t = MaterialSynthesizer._extract_field(m, "title", "")
            if t:
                all_titles.append(t)
        combined = " ".join(all_titles)
        # 提取 4-6 字中文短语
        phrases = re.findall(r'[\u4e00-\u9fa5]{4,6}', combined)
        if phrases:
            # 返回出现次数最多的短语
            counter = Counter(phrases)
            return counter.most_common(1)[0][0]
        return f"主题簇{cluster.total_count}条"

    def get_cached_clusters(self) -> Optional[List[MaterialCluster]]:
        """获取缓存的聚类结果（迭代复用）"""
        return self._cached_clusters

    def set_cached_clusters(self, clusters: List[MaterialCluster]):
        """缓存聚类结果（迭代复用）"""
        self._cached_clusters = clusters

    def clear_cache(self):
        """清除缓存"""
        self._cached_clusters = None
        self._cached_embeddings = None
