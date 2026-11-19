#!/usr/bin/env python3
"""
推演引擎 (Deduction Engine)
──────────────────────────
基于 MIT CSAIL + Harvard SEAS "协作海战棋" (2026) 论文核心方法论：
世界模型 + 蒙特卡洛推理 → 信息增益排序 → 分离可推理/必问

共享模块，被调研工具和方案工具共用。

设计原则:
1. 不替换现有的澄清问题生成逻辑，作为增强层夹在中间
2. 用一个轻量 LLM 调用（复用 planner 角色），推演一遍后筛出真正需要问的
3. 输出两份清单：可推理项（系统自己搞定） + 必问项（最多3-5个）

Author: Trae
Created: 2026-06-15
"""

import json
import logging
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)


# ============================================================================
# Prompt 模板
# ============================================================================

DEDUCTION_PROMPT = """【角色】你是商业推演引擎。你有两项核心能力：
1. 世界模型: 你理解商业任务的因果结构——前提假设影响范围确定，范围确定影响数据获取可行性，数据获取可行性影响最终输出质量。你能预测"缺了信息X会导致步骤Y卡住"。
2. 信息增益排序: 你能判断每个不确定项对最终结果的影响有多大，优先追问影响最大的。

【当前任务背景】
{task}

【AI 已生成的候选问题列表】（共 {total_count} 个）
{candidate_questions}

【推演要求】
对候选问题列表中的每个问题，进行三步判断：

第一步：模拟回答路径
假设用户回答了这个问题，可能给出 2-3 种典型答案，每种答案会导向不同的任务方向吗？

第二步：判断可推理性
- 系统可以通过联网搜索获取吗？（市场规模、竞品数据、行业报告、公开定价...）
- 系统可以通过常识推断或计算推导吗？（基准值、比例估算、行业惯例...）
- 系统可以通过默认假设覆盖吗？（"按行业通用做法"就是合理答案）
如果以上任一为"是" → 标记为"可推理"，系统自行处理

第三步：决定是否必问
只有以下情况才是"必问"：
- 涉及用户的内部信息（预算、资源、决策偏好、未来计划）
- 涉及用户的独特定位（品牌定位、差异化策略）
- 三种可能的答案会导向三种完全不同的任务方向
否则一律标记为"可推理"或"可忽略"

【输出格式】（纯JSON，不要markdown包裹）
{{
  "deduction_summary": "一句话总结推演结论",
  "analyses": [
    {{
      "original_question": "原始问题文本",
      "can_infer": true/false,
      "infer_method": "联网搜索/计算推导/常识推断/默认假设 — 仅can_infer=true时填写",
      "must_ask": true/false,
      "gain_level": "high/medium/low — 仅must_ask=true时填写",
      "reason": "一句话说明为什么做此判断",
      "refined_question": "如果是必问，保持原样或稍作优化；如果可推理，可为空"
    }}
  ],
  "inferable_summary": "系统将自行处理的 {{N}} 个问题简述",
  "dominant_perspectives": [
    {{
      "perspective": "视角名称（如：市场视角、技术视角、竞争视角、消费者视角、政策视角）",
      "reason": "为什么这个视角对本次调研最重要",
      "search_dimensions": ["该视角下应重点搜索的维度1", "维度2"]
    }}
  ]
}}

【dominant_perspectives 输出规则】
- 从任务背景和候选问题中推断：哪些视角对本次调研最关键？
- 输出2-4个主导视角，按重要性排序
- 每个视角必须包含具体的搜索维度建议（2-3个）
- 不要输出与任务无关的视角
- 如果任务背景不足以判断，输出空数组 []
"""


# ============================================================================
# 推演引擎类
# ============================================================================

class DeductionEngine:
    """推演引擎：过滤候选问题，分离可推理/必问"""

    def __init__(self, llm_call_fn):
        """
        Args:
            llm_call_fn: 同步 LLM 调用函数，签名为 fn(prompt: str, model_type: str = "fast") -> str
        """
        self._call_llm = llm_call_fn

    def filter_questions(
        self,
        task: str,
        candidate_questions: List[Dict[str, str]],
        max_keep: int = 5
    ) -> Dict[str, Any]:
        """
        对候选问题做推演过滤，返回必问问题 + 可推理项清单。

        Args:
            task: 用户原始任务描述
            candidate_questions: 候选问题列表，每个包含 question/explanation/impact
            max_keep: 最多保留的必问问题数 (默认5)

        Returns:
            {
                "must_ask": [...],          # 必问问题列表（最多max_keep个）
                "inferable": [...],         # 可推理项清单
                "deduction_summary": "str", # 推演笔记摘要
                "all_could_infer": bool,    # 是否全部可推理（即无需追问）
                "dominant_perspectives": [...]  # 主导视角列表（2-4个）
            }
        """
        if not candidate_questions:
            return {
                "must_ask": [],
                "inferable": [],
                "deduction_summary": "无候选问题，跳过推演",
                "all_could_infer": False,
                "dominant_perspectives": [],
            }

        # 格式化候选问题
        q_lines = []
        for i, q in enumerate(candidate_questions, 1):
            q_text = q.get("question", "") if isinstance(q, dict) else str(q)
            explanation = q.get("explanation", "") if isinstance(q, dict) else ""
            impact = q.get("impact", "") if isinstance(q, dict) else ""
            q_lines.append(
                f"{i}. 问题: {q_text}\n   解释: {explanation}\n   影响: {impact}"
            )
        q_text = "\n".join(q_lines)

        prompt = DEDUCTION_PROMPT.format(
            task=task,
            total_count=len(candidate_questions),
            candidate_questions=q_text,
        )

        try:
            raw = self._call_llm(prompt)
            analysis = self._parse_response(raw, candidate_questions)
        except Exception as e:
            logger.warning(f"[推演引擎] LLM调用失败，回退到全部通过: {e}")
            return self._fallback_all_pass(candidate_questions)

        # 分离
        must_ask = []
        inferable = []

        for item in analysis.get("analyses", []):
            if item.get("must_ask") and item.get("gain_level") != "low":
                q_text = item.get("refined_question") or item.get("original_question", "")
                if q_text.strip():
                    must_ask.append({
                        "question": q_text,
                        "gain_level": item.get("gain_level", "medium"),
                        "reason": item.get("reason", ""),
                    })
            elif item.get("can_infer"):
                inferable.append({
                    "original_question": item.get("original_question", ""),
                    "method": item.get("infer_method", ""),
                    "reason": item.get("reason", ""),
                })
            # 既非可推理也非必问（low gain）→ 直接丢弃

        # 按增益排序，只保留最多 max_keep 个
        gain_order = {"high": 0, "medium": 1, "low": 2}
        must_ask.sort(key=lambda q: gain_order.get(q.get("gain_level", "low"), 2))
        must_ask = must_ask[:max_keep]

        all_could_infer = len(must_ask) == 0

        return {
            "must_ask": must_ask,
            "inferable": inferable,
            "deduction_summary": analysis.get("deduction_summary", ""),
            "all_could_infer": all_could_infer,
            "inferable_summary": analysis.get("inferable_summary", ""),
            "dominant_perspectives": analysis.get("dominant_perspectives", []),
        }

    def _parse_response(self, raw: str, fallback_questions: List) -> Dict:
        """解析 LLM 返回的 JSON"""
        text = raw.strip() if raw else ""

        # 去掉可能的 markdown 包裹
        if "```json" in text:
            text = text.split("```json", 1)[1].split("```", 1)[0].strip()
        elif "```" in text:
            text = text.split("```", 1)[1].split("```", 1)[0].strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # 尝试提取大括号内容
            if "{" in text and "}" in text:
                start = text.index("{")
                end = text.rindex("}") + 1
                try:
                    return json.loads(text[start:end])
                except json.JSONDecodeError:
                    pass

            logger.warning(f"[推演引擎] JSON解析失败，回退全部通过。原始响应前200字: {text[:200]}")
            raise

    def _fallback_all_pass(self, questions: List) -> Dict:
        """推演失败时的安全回退：全部问题通过"""
        must_ask = []
        for q in questions:
            q_text = q.get("question", "") if isinstance(q, dict) else str(q)
            if q_text.strip():
                must_ask.append({
                    "question": q_text,
                    "gain_level": "medium",
                    "reason": "推演引擎解析失败，安全回退全部通过",
                })
        return {
            "must_ask": must_ask,
            "inferable": [],
            "deduction_summary": "推演失败，安全回退：全部问题保留",
            "all_could_infer": False,
            "dominant_perspectives": [],
        }

    def format_for_display(self, result: Dict, intro_text: str = "") -> str:
        """格式化推演结果，生成向用户展示的文本"""
        lines = []

        if intro_text:
            lines.append(intro_text)
        else:
            lines.append("AI 已对您的需求进行了推演分析：")

        # 推演摘要
        if result.get("deduction_summary"):
            lines.append(f"\n🔍 {result['deduction_summary']}")

        # 可推理项（让用户知道系统会自己搞定什么）
        inferable = result.get("inferable", [])
        if inferable:
            lines.append(f"\n✅ 系统将自行处理 {len(inferable)} 项信息：")
            for item in inferable[:5]:
                lines.append(f"   · {item.get('original_question', '')[:60]}")
                if item.get("method"):
                    lines.append(f"     → {item['method']}")

        # 必问项
        must_ask = result.get("must_ask", [])
        if must_ask:
            lines.append(f"\n❓ 需要您确认的 {len(must_ask)} 个关键问题：")
            lines.append("")
            for i, q in enumerate(must_ask, 1):
                lines.append(f"{i}. {q['question']}")
                if q.get("reason"):
                    lines.append(f"   💡 {q['reason']}")
                lines.append("")
        elif not inferable:
            lines.append("\n信息充足，无需额外补充。")

        lines.append("确认以上信息后，将立即启动执行。")
        return "\n".join(lines)

    def build_injectable_context(self, result: Dict) -> str:
        """将可推理项构建为可注入后续流程的上下文文本"""
        inferable = result.get("inferable", [])
        if not inferable:
            return ""

        lines = ["\n\n【推演引擎·系统自动处理项】"]
        lines.append("以下信息由系统在后续流程中自行搜索/推理补充，不依赖用户输入：\n")
        for i, item in enumerate(inferable, 1):
            lines.append(f"{i}. {item.get('original_question', '')}")
            lines.append(f"   处理方式: {item.get('method', '自动搜索')}")
        return "\n".join(lines)

    def build_perspective_constraint(self, result: Dict) -> str:
        """将主导视角构建为搜索规划约束文本，注入 planner prompt 中。
        
        与 planner 的固定五视角不同，这是从推演中推导出的、针对本次任务最关键的2-4个视角。
        用于在 generate_plan() 中替换"请从以下五个视角审视"的通用指令。
        """
        perspectives = result.get("dominant_perspectives", [])
        if not perspectives:
            return ""

        lines = ["\n【搜索视角约束——推演引擎分析】"]
        lines.append("以下视角是通过推演分析得出的、对本次调研最关键的方向，请重点围绕这些视角规划搜索批次：\n")
        for i, p in enumerate(perspectives, 1):
            p_name = p.get("perspective", "")
            p_reason = p.get("reason", "")
            p_dims = p.get("search_dimensions", [])
            lines.append(f"{i}. **{p_name}**：{p_reason}")
            if p_dims:
                lines.append(f"   搜索维度：{'、'.join(p_dims[:3])}")
        lines.append("\n若其他视角能产生独特且重要的搜索维度，可以纳入，但上述视角应占搜索批次的主导地位。")
        return "\n".join(lines)


# ============================================================================
# 便捷工厂函数
# ============================================================================

def create_deduction_engine(llm_call_fn):
    """创建推演引擎实例"""
    return DeductionEngine(llm_call_fn)


async def filter_questions_async(
    llm_call_async_fn,
    task: str,
    candidate_questions: List[Dict[str, str]],
    max_keep: int = 5
) -> Dict[str, Any]:
    """
    异步版 filter_questions，用于方案工具等 async 上下文。
    
    Args:
        llm_call_async_fn: 异步 LLM 调用函数，签名 async fn(prompt_text: str) -> str
        task: 原始任务
        candidate_questions: 候选问题列表
        max_keep: 最多保留的必问问题数
    
    Returns:
        与 DeductionEngine.filter_questions 相同的结构
    """
    # 创建临时引擎（llm_call_fn 在 async 上下文中不会被用到，
    # 因为我们直接用 async fn 调用）
    temp_engine = DeductionEngine(lambda p: p)

    # 流程与 filter_questions 相同，只是 LLM 调用用 async
    if not candidate_questions:
        return {
            "must_ask": [], "inferable": [],
            "deduction_summary": "无候选问题，跳过推演",
            "all_could_infer": False,
            "dominant_perspectives": [],
        }

    q_lines = []
    for i, q in enumerate(candidate_questions, 1):
        q_text = q.get("question", "") if isinstance(q, dict) else str(q)
        explanation = q.get("explanation", "") if isinstance(q, dict) else ""
        impact = q.get("impact", "") if isinstance(q, dict) else ""
        q_lines.append(
            f"{i}. 问题: {q_text}\n   解释: {explanation}\n   影响: {impact}"
        )

    prompt = DEDUCTION_PROMPT.format(
        task=task,
        total_count=len(candidate_questions),
        candidate_questions="\n".join(q_lines),
    )

    try:
        raw = await llm_call_async_fn(prompt)
        analysis = temp_engine._parse_response(raw, candidate_questions)
    except Exception as e:
        logger.warning(f"[推演引擎·异步] LLM调用失败，回退到全部通过: {e}")
        return temp_engine._fallback_all_pass(candidate_questions)

    must_ask = []
    inferable = []
    for item in analysis.get("analyses", []):
        if item.get("must_ask") and item.get("gain_level") != "low":
            q_text = item.get("refined_question") or item.get("original_question", "")
            if q_text.strip():
                must_ask.append({
                    "question": q_text,
                    "gain_level": item.get("gain_level", "medium"),
                    "reason": item.get("reason", ""),
                })
        elif item.get("can_infer"):
            inferable.append({
                "original_question": item.get("original_question", ""),
                "method": item.get("infer_method", ""),
                "reason": item.get("reason", ""),
            })

    gain_order = {"high": 0, "medium": 1, "low": 2}
    must_ask.sort(key=lambda q: gain_order.get(q.get("gain_level", "low"), 2))
    must_ask = must_ask[:max_keep]

    return {
        "must_ask": must_ask,
        "inferable": inferable,
        "deduction_summary": analysis.get("deduction_summary", ""),
        "all_could_infer": len(must_ask) == 0,
        "inferable_summary": analysis.get("inferable_summary", ""),
        "dominant_perspectives": analysis.get("dominant_perspectives", []),
    }
