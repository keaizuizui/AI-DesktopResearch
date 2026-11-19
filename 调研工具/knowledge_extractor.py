#!/usr/bin/env python3
"""
报告知识提取模块
从完成的报告中自动提取企业清单与数据点，供溯源附录使用
"""

import re
from typing import List, Dict, Optional
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from config import logger, LLMClient, safe_json_loads


# ============================================================
#  System Prompt
# ============================================================

_KNOWLEDGE_EXTRACTOR_SYSTEM = (
    "你是一位专业的行业知识提取专家。你的任务是从调研报告中精准提取结构化信息。"
    "要求：只提取报告中明确提到的内容，不编造数据；输出简洁准确，严格遵循指定格式。"
)


@dataclass
class ExtractedEntity:
    """提取的企业实体"""
    name: str
    tier: str = ""  # 第一梯队/第二梯队/...
    confidence: str = "medium"  # high/medium/low
    verified: bool = False
    verified_at: str = ""
    verification_source: str = ""


@dataclass
class ExtractedDataPoint:
    """提取的数据口径"""
    metric: str  # 指标名称，如"市场规模"
    value: str   # 数值，如"5000亿元"
    year: str    # 年份，如"2025"
    source: str = ""  # 数据来源
    confidence: str = "medium"
    expires_at: str = ""  # 有效期


@dataclass
class ExtractedKnowledge:
    """从报告中提取的完整知识"""
    industry_name: str = ""
    industry_definition: str = ""
    entities: List[ExtractedEntity] = field(default_factory=list)
    data_points: List[ExtractedDataPoint] = field(default_factory=list)
    common_mistakes: List[str] = field(default_factory=list)
    dimensions: List[str] = field(default_factory=list)
    
    # 元数据
    extracted_at: str = field(default_factory=lambda: datetime.now().isoformat())
    source_report: str = ""  # 来源报告路径
    overall_confidence: float = 0.0
    
    def to_kb_md(self) -> str:
        """转换为扩展的.kb.md格式"""
        lines = [
            f"# 行业知识库：{self.industry_name}",
            "",
            "## 行业定义",
            self.industry_definition if self.industry_definition else "待补充",
            "",
            "## 企业清单",
        ]
        
        for entity in self.entities:
            tier_tag = f" ({entity.tier})" if entity.tier else ""
            verified_tag = f" [verified:{entity.verified_at[:10]}, confidence:{entity.confidence}]" if entity.verified else ""
            lines.append(f"- **{entity.name}**{tier_tag}{verified_tag}")
        
        lines.extend(["", "## 关键数据口径"])
        for dp in self.data_points:
            expires_tag = f", expires_at:{dp.expires_at}" if dp.expires_at else ""
            lines.append(f"- {dp.year}年{dp.metric}：{dp.value} [source:{dp.source}, confidence:{dp.confidence}{expires_tag}]")
        
        if self.common_mistakes:
            lines.extend(["", "## 常见误区"])
            for mistake in self.common_mistakes:
                lines.append(f"- {mistake}")
        
        lines.extend([
            "",
            "## 版本历史",
            f"- v1.0: {datetime.now().strftime('%Y-%m-%d')} 从报告提取"
        ])
        
        return "\n".join(lines)


class KnowledgeExtractor:
    """知识提取器"""
    
    def __init__(self, cfg: Dict, llm_client: Optional[LLMClient] = None):
        self.cfg = cfg
        self.llm = llm_client or LLMClient(cfg)
        
    def extract_from_report(self, report_md: str, industry_hint: str = "") -> ExtractedKnowledge:
        """
        从完成的报告Markdown中提取结构化知识
        
        Args:
            report_md: 报告内容（Markdown格式）
            industry_hint: 行业名称提示（可选）
        """
        knowledge = ExtractedKnowledge()
        
        # 1. 提取行业名称和定义
        knowledge.industry_name = self._extract_industry_name(report_md, industry_hint)
        knowledge.industry_definition = self._extract_industry_definition(report_md)
        
        # 2. 提取企业清单
        knowledge.entities = self._extract_entities(report_md)
        
        # 3. 提取数据口径
        knowledge.data_points = self._extract_data_points(report_md)
        
        # 4. 提取常见误区
        knowledge.common_mistakes = self._extract_common_mistakes(report_md)
        
        # 5. 计算整体置信度
        knowledge.overall_confidence = self._calculate_overall_confidence(knowledge)
        
        return knowledge
    
    def _extract_industry_name(self, report_md: str, hint: str = "") -> str:
        """提取行业名称"""
        if hint:
            return hint
            
        # 尝试从标题提取
        title_match = re.search(r'^#\s*(.+?)(?:调研|分析|报告|行业)', report_md, re.MULTILINE)
        if title_match:
            return title_match.group(1).strip()
        
        # 用LLM提取
        prompt = f"""从以下报告标题/内容中提取行业名称，只返回行业名称本身，不要解释：

{report_md[:2000]}

行业名称："""
        try:
            response = self.llm.chat(
                [{"role": "user", "content": prompt}],
                model=self.cfg.get("plan_model", "deepseek-v4-flash"),
                system=_KNOWLEDGE_EXTRACTOR_SYSTEM,
            )
            return response.strip().replace("行业名称：", "").strip()
        except Exception as e:
            logger.warning("[KnowledgeExtractor] 行业名称提取失败: %s", e)
            return "未知行业"
    
    def _extract_industry_definition(self, report_md: str) -> str:
        """提取行业定义"""
        # 尝试从摘要或第一章提取
        definition_match = re.search(r'##?\s*.*(?:定义|概述|简介).*\n\n(.+?)(?:\n\n|\n##)', report_md, re.DOTALL)
        if definition_match:
            return definition_match.group(1).strip()[:500]
        
        # 用LLM提取
        prompt = f"""从以下报告中提取行业定义/概念描述。

【规则】
1. 提取报告中对行业的明确定义或概念描述
2. 控制在100字以内
3. 只返回定义内容，不要"行业定义："等前缀
4. 如果报告中没有明确定义，返回空字符串

报告内容：
---
{report_md[:3000]}
---

输出（仅定义内容，100字以内）："""
        try:
            response = self.llm.chat(
                [{"role": "user", "content": prompt}],
                model=self.cfg.get("plan_model", "deepseek-v4-flash"),
                system=_KNOWLEDGE_EXTRACTOR_SYSTEM,
            )
            return response.strip().replace("行业定义：", "").strip()[:500]
        except Exception as e:
            logger.warning("[KnowledgeExtractor] 行业定义提取失败: %s", e)
            return ""
    
    def _extract_entities(self, report_md: str) -> List[ExtractedEntity]:
        """提取企业清单（使用LLM）"""
        prompt = f"""从以下行业报告中提取所有提到的企业/公司名单。

【规则】
1. 只提取报告明确提及的企业名称，不编造
2. 按行业地位分梯队：第一梯队（龙头）、第二梯队（主要玩家）、第三梯队（新兴/小众）
3. 无法确定梯队的企业归入"其他"

报告内容：
---
{report_md[:8000]}
---

输出格式（严格JSON）：
{{"entities": [
  {{"name": "企业名称", "tier": "第一梯队"}},
  {{"name": "企业名称", "tier": "第二梯队"}}
]}}

JSON输出："""
        
        try:
            response = self.llm.chat(
                [{"role": "user", "content": prompt}],
                model=self.cfg.get("plan_model", "deepseek-v4-flash"),
                system=_KNOWLEDGE_EXTRACTOR_SYSTEM,
                json_mode=True,
                max_tokens=8000,
            )
            # 提取JSON（兼容对象或数组返回）
            data = safe_json_loads(response, default={})
            if isinstance(data, list):
                data = {"entities": data}
            if not isinstance(data, dict):
                return []
            entities = []
            for e in data.get("entities", []):
                if not isinstance(e, dict):
                    continue
                entities.append(ExtractedEntity(
                    name=e.get("name", ""),
                    tier=e.get("tier", "")
                ))
            return entities
        except Exception as e:
            logger.warning(f"[KnowledgeExtractor] 企业提取失败: {e}")
        
        return []
    
    def _extract_data_points(self, report_md: str) -> List[ExtractedDataPoint]:
        """提取数据口径（使用LLM）"""
        prompt = f"""从以下行业报告中提取所有关键数据点。

【规则】
1. 只提取有具体数值的数据（市场规模、增长率、渗透率、份额等）
2. 必须包含：指标名称、数值
3. 尽量包含：年份、数据来源
4. 不编造数据，不确定的字段留空
5. 最多提取 30 个最关键的数据点，避免输出被截断

报告内容：
---
{report_md[:8000]}
---

输出格式（严格JSON）：
{{"data_points": [
  {{"metric": "市场规模", "value": "5000亿元", "year": "2025", "source": "中汽协"}},
  {{"metric": "渗透率", "value": "35%", "year": "2026", "source": ""}}
]}}

JSON输出："""
        
        try:
            response = self.llm.chat(
                [{"role": "user", "content": prompt}],
                model=self.cfg.get("plan_model", "deepseek-v4-flash"),
                system=_KNOWLEDGE_EXTRACTOR_SYSTEM,
                json_mode=True,
                max_tokens=8000,
            )
            data = safe_json_loads(response, default={})
            if isinstance(data, list):
                data = {"data_points": data}
            if not isinstance(data, dict):
                return []
            data_points = []
            for dp in data.get("data_points", []):
                if not isinstance(dp, dict):
                    continue
                # 年份可能为空或"2026年初"，取其中的4位年份，缺省用当前年
                year_text = str(dp.get("year") or "").strip()
                year_match = re.search(r"(20\d{2})", year_text)
                base_year = int(year_match.group(1)) if year_match else datetime.now().year
                expires = (datetime(base_year, 12, 31) + timedelta(days=365)).strftime("%Y-%m-%d")

                data_points.append(ExtractedDataPoint(
                    metric=dp.get("metric", ""),
                    value=dp.get("value", ""),
                    year=year_text,
                    source=dp.get("source", ""),
                    expires_at=expires
                ))
            return data_points
        except Exception as e:
            logger.warning(f"[KnowledgeExtractor] 数据点提取失败: {e}")
        
        return []
    
    def _extract_common_mistakes(self, report_md: str) -> List[str]:
        """提取常见误区"""
        # 尝试从数据可靠性声明部分提取
        mistake_match = re.search(r'常见调研误区[:：](.+?)(?:\n\n|\n##|$)', report_md, re.DOTALL)
        if mistake_match:
            mistakes = re.findall(r'[-•]\s*(.+)', mistake_match.group(1))
            return [m.strip() for m in mistakes if m.strip()][:5]
        return []
    
    def _calculate_overall_confidence(self, knowledge: ExtractedKnowledge) -> float:
        """计算整体置信度"""
        scores = []
        
        # 企业置信度
        for e in knowledge.entities:
            if e.confidence == "high":
                scores.append(1.0)
            elif e.confidence == "medium":
                scores.append(0.6)
            else:
                scores.append(0.3)
        
        # 数据点置信度
        for dp in knowledge.data_points:
            if dp.confidence == "high":
                scores.append(1.0)
            elif dp.confidence == "medium":
                scores.append(0.6)
            else:
                scores.append(0.3)
        
        return sum(scores) / len(scores) if scores else 0.5
