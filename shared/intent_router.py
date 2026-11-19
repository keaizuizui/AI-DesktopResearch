"""
意图路由器 (Intent Router)
================================
轻量级输入路由器，只判断"用户输入够不够清晰"。

不做意图分类（入口 tag 已区分工具类型），
不做跨工具建议（用户已选择入口），
不做需求导向语言转换和假问题过滤（归盘问引擎）。

三层判断：
  1. 模糊度 — 输入是否信息不足，需要盘问？
  2. 复杂度 — 如果需要盘问，需要多深？
  3. Phase 0 — 是否需要先盲搜发现维度？

设计原则：
  - 纯规则引擎，不依赖 LLM（零延迟）
  - 每个工具入口调用 1 行代码
  - 输出简单 dataclass，调用方自行决策

使用方式：
    from shared.intent_router import IntentRouter, IntentResult

    router = IntentRouter()
    result = router.assess(user_input)

    if result.fuzzy:
        engine = InterrogationEngine(skeleton=INSIGHT_SKELETON)
        ...
    else:
        # 直接处理
        ...
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class FuzzyLevel(Enum):
    """模糊度"""
    CLEAR = "clear"       # 信息充分，直接处理
    LIGHT = "light"       # 轻度模糊，1-2 轮澄清
    MODERATE = "moderate"  # 中度模糊，BMC 扫描 3-4 格
    DEEP = "deep"         # 深度模糊，完整盘问 + Phase 0


class Complexity(Enum):
    """问题复杂度"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# ── 模糊信号词 ──

_VAGUE_SIGNALS = {
    "帮我看看", "分析一下", "评估一下", "给个建议", "怎么做",
    "有什么想法", "帮我想想", "看看这个", "能做吗", "可行吗",
    "帮我分析", "帮我看看", "想了解一下", "了解了解",
    "好不好", "行不行", "合不合适", "怎么样",
}

# ── 信息充分信号词 ──

_SUFFICIENT_SIGNALS = {
    "营收", "利润", "成本", "团队", "客户数", "转化率", "复购",
    "渠道", "供应链", "供应商", "竞品", "市场份额", "月活",
    "客单价", "毛利率", "净利率", "获客成本", "留存率",
    "预算", "时间", "人数", "规模", "阶段", "估值",
    "融资", "股权", "合伙人", "门店", "产品线",
}

# ── 高复杂度信号词 ──

_HIGH_COMPLEXITY_SIGNALS = {
    "系统", "全面", "整体", "全盘", "从零", "从头", "整套",
    "全年", "年度", "整个行业", "全局", "架构", "战略",
    "转型", "上市", "并购", "生态",
}

# ── 低复杂度信号词 ──

_LOW_COMPLEXITY_SIGNALS = {
    "这个", "那个", "一下", "简单", "随便", "大概",
    "是不是", "能不能", "行不行",
}

# ── 领域关键词（用于 Phase 0 判断） ──

_DOMAIN_KEYWORDS = {
    "餐饮", "电商", "SaaS", "金融", "教育", "医疗",
    "快消", "制造", "房地产", "汽车", "AI", "科技",
    "互联网", "零售", "美妆", "母婴", "新能源",
    "物流", "旅游", "游戏", "文娱", "农业",
    "服装", "食品", "饮料", "家电", "家居",
}

# ── 隐含假设模板 ──

_IMPLICIT_ASSUMPTIONS = {
    "diagnose": [
        "用户可能已经尝试过某些方法（但失败了）",
        "用户可能对问题原因有预设判断（但不一定对）",
        "用户可能低估了问题的系统性",
    ],
    "compare": [
        "用户可能已经倾向某一方（只是来找验证）",
        "比较维度可能不完整（只考虑了部分维度）",
        '用户可能忽略了"都不选"的选项',
    ],
    "plan": [
        "用户可能已经有一个模糊方向（只是需要确认）",
        "用户可能低估了资源约束",
        "用户可能高估了可执行性",
    ],
    "analyze": [
        "分析维度可能不完整",
        '用户可能拿错了"地图"（用错了分析框架）',
        '用户可能混淆了"想知道"和"需要知道"',
    ],
    "produce": [
        "用户可能对输出格式有隐含期望（没说出口）",
        "用户可能高估了可用信息的充分性",
        "用户可能低估了定制化需求",
    ],
}


@dataclass
class IntentResult:
    """意图路由结果"""
    fuzzy: bool = False
    fuzzy_level: FuzzyLevel = FuzzyLevel.CLEAR
    complexity: Complexity = Complexity.LOW
    needs_phase0: bool = False
    implicit_assumptions: List[str] = field(default_factory=list)
    interrogation_depth: int = 0  # 0=不盘问, 1-2=澄清, 3-4=BMC, 5-8=完整
    reason: str = ""
    confidence: float = 0.0


class IntentRouter:
    """意图路由器 —— 轻量级规则引擎"""

    def assess(self, user_input: str) -> IntentResult:
        """
        评估用户输入，返回处理建议。

        规则优先级：
          1. 过短 → 深度模糊
          2. 无模糊信号词 + 有具体信号词 → 清晰
          3. 有模糊信号词 → 按文本长度和复杂度信号词分级
          4. 高复杂度 + 无领域关键词 → 触发 Phase 0
        """
        if not user_input or len(user_input.strip()) < 5:
            return IntentResult(
                fuzzy=True, fuzzy_level=FuzzyLevel.DEEP, complexity=Complexity.MEDIUM,
                interrogation_depth=5, needs_phase0=False,
                reason="用户输入过短，信息严重不足", confidence=0.2,
            )

        text = user_input.strip()
        text_len = len(text)

        # 第1步：判断是否有具体信息
        has_sufficient = any(s in text for s in _SUFFICIENT_SIGNALS)
        # 第2步：判断是否有模糊信号
        has_vague = any(s in text for s in _VAGUE_SIGNALS)
        # 第3步：判断复杂度
        complexity = self._assess_complexity(text)
        # 第4步：判断是否需要 Phase 0
        needs_phase0 = self._needs_phase0(text, complexity)

        # 信息充分 → 不盘问
        if has_sufficient and not has_vague:
            return IntentResult(
                fuzzy=False, fuzzy_level=FuzzyLevel.CLEAR, complexity=complexity,
                reason="用户输入包含具体业务数据，信息充分", confidence=0.85,
            )

        # 信息充分 + 有模糊信号 → 轻度（可能只是口语化表达）
        if has_sufficient and has_vague:
            return IntentResult(
                fuzzy=False, fuzzy_level=FuzzyLevel.LIGHT, complexity=complexity,
                reason="用户输入有具体数据但带口语化表达，可直接处理", confidence=0.75,
            )

        # 文本短 + 无具体信息 → 深度模糊
        if text_len < 30 and not has_sufficient:
            return IntentResult(
                fuzzy=True, fuzzy_level=FuzzyLevel.DEEP, complexity=complexity,
                interrogation_depth=8, needs_phase0=needs_phase0,
                reason="用户输入短且无具体信息，需要完整盘问", confidence=0.6,
            )

        # 文本短 + 模糊信号 → 中度模糊
        if text_len < 50 and has_vague and not has_sufficient:
            return IntentResult(
                fuzzy=True, fuzzy_level=FuzzyLevel.MODERATE, complexity=complexity,
                interrogation_depth=4, needs_phase0=needs_phase0,
                reason="用户输入模糊且缺少具体数据，需要盘问补充信息", confidence=0.65,
            )

        # 文本长 + 无具体信息 → 中度模糊
        if text_len >= 50 and not has_sufficient:
            return IntentResult(
                fuzzy=True, fuzzy_level=FuzzyLevel.MODERATE, complexity=complexity,
                interrogation_depth=3, needs_phase0=needs_phase0,
                reason="用户输入较长但缺少具体数据，需要结构化盘问", confidence=0.7,
            )

        # 默认：不盘问
        return IntentResult(
            fuzzy=False, fuzzy_level=FuzzyLevel.CLEAR, complexity=complexity,
            reason="用户输入信息基本充分", confidence=0.8,
        )

    def _assess_complexity(self, text: str) -> Complexity:
        """评估问题复杂度"""
        high_count = sum(1 for s in _HIGH_COMPLEXITY_SIGNALS if s in text)
        low_count = sum(1 for s in _LOW_COMPLEXITY_SIGNALS if s in text)

        if high_count >= 2:
            return Complexity.HIGH
        if high_count >= 1:
            return Complexity.MEDIUM
        if low_count >= 2 and len(text) < 30:
            return Complexity.LOW
        return Complexity.MEDIUM

    def _needs_phase0(self, text: str, complexity: Complexity) -> bool:
        """是否需要 Phase 0 盲搜"""
        if complexity != Complexity.HIGH:
            return False
        # 高复杂度 + 无领域关键词 → 需要盲搜
        has_domain = any(kw in text for kw in _DOMAIN_KEYWORDS)
        return not has_domain

    def detect_implicit_assumptions(self, text: str) -> List[str]:
        """检测隐含假设（供盘问引擎使用）"""
        assumptions = []
        # 短文本 → 所有假设都可能存在
        if len(text) < 30:
            for cat in _IMPLICIT_ASSUMPTIONS.values():
                assumptions.extend(cat[:1])
        else:
            # 长文本 → 只取前两条
            for cat in list(_IMPLICIT_ASSUMPTIONS.values())[:2]:
                assumptions.extend(cat[:1])
        return assumptions

    def get_user_facing_notice(self, result: IntentResult) -> str:
        """生成面向用户的提示语"""
        if not result.fuzzy:
            return ""

        if result.fuzzy_level == FuzzyLevel.LIGHT:
            return "你的描述比较简略，我先确认几个关键信息，很快就好。"

        if result.fuzzy_level == FuzzyLevel.MODERATE:
            return (
                "为了给你更准确的建议，我需要先了解一些具体情况。"
                f"大概需要 {result.interrogation_depth} 轮问答，每轮 1-2 个问题。"
                "我们从最简单的开始——"
            )

        if result.fuzzy_level == FuzzyLevel.DEEP:
            parts = [
                "你的问题涉及面比较广，我需要先系统地了解情况。",
                f"大概需要 {result.interrogation_depth} 轮问答，分几个阶段进行。",
            ]
            if result.needs_phase0:
                parts.append("我会先快速了解这个领域的基本情况，再针对性地提问。")
            parts.append("我们开始吧——")
            return " ".join(parts)

        return ""
