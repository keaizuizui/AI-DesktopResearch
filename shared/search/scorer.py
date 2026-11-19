"""
Search Scorer Module
搜索结果评分模块 — LLM动态打分 + 验证式QA

来源：调研工具 search.py SearchScorer（借鉴 Flash-Searcher 的搜索质量评估思路）

设计原则：
- 通用接口，不依赖特定工具的 LLMClient
- 支持同步/异步双模式
- 评分自动映射到 SourceGrade（与 models.py 对接）
"""
import json
import logging
from typing import List, Dict, Optional, Callable, Any
from dataclasses import dataclass

from shared.search.models import SearchResult, score_to_grade, SourceGrade

logger = logging.getLogger(__name__)


@dataclass
class ScoreConfig:
    """评分配置"""
    enable_verification_qa: bool = True    # 启用验证式QA
    verification_threshold: float = 7.0    # 验证QA触发阈值（llm_score >= 此值）
    min_reliability_for_qa: str = "high"   # 验证QA要求的最低可信度
    max_batch_size: int = 5                # 每批最大打分数量
    max_content_length: int = 1500         # 内容截断长度
    max_qa_results: int = 2                # 每个结果生成的验证问题数
    json_mode: bool = True                 # 使用JSON模式调用LLM


class SearchScorer:
    """
    搜索结果评分器

    功能：
    1. LLM批量打分：相关性、信息密度、可信度
    2. 验证式QA：生成验证问题交叉验证素材真实性
    3. 评分→评级自动映射
    """

    # 默认评估Prompt（通用版，不依赖特定工具）
    DEFAULT_EVAL_PROMPT = """【角色】信息质量评估专家
【任务】评估搜索结果的质量和相关性

【用户查询】{query}
【来源标题】{title}
【来源内容】{content}

【评估维度】
- score：1-10分，评估相关性和信息密度
- facts：提取3-5个核心事实，必须包含具体数据
- reliability：high/medium/low，评估来源可信度
- reason：一句话评估理由，说明打分依据

【数据准确性红线】
- facts字段中的数据必须严格来自【来源内容】，不得编造、推测或引入外部知识
- 如果来源内容中没有具体数据，facts中写"来源未提供具体数据"，不要自行填充
- reliability为low时，必须在reason中说明原因（如：来源不明、内容矛盾、缺乏引用）

【输出要求】
- 只输出纯JSON对象，不要任何其他文字
- 确保JSON语法100%正确
- 标准格式：
{{"score": 8,"facts": ["核心事实1","核心事实2","核心事实3"],"reliability": "high","reason": "评估理由"}}"""

    DEFAULT_QA_PROMPT = """【任务】生成验证性问题，用于交叉验证素材真实性
【素材标题】{title}
【素材内容】{content}

【生成规则】
1. 针对素材中的关键数据和核心论断生成
2. 生成1-2个可通过搜索验证的具体问题
3. 每个问题必须包含预期的标准答案方向

【输出要求】
- 只输出纯JSON数组，不要解释文字
- 标准格式：
[{{"question":"具体的验证性问题","expected_answer":"预期的验证方向"}}]"""

    def __init__(
        self,
        llm_call: Optional[Callable] = None,
        config: Optional[ScoreConfig] = None,
        system_prompt: Optional[str] = None,
    ):
        """
        初始化评分器

        Args:
            llm_call: LLM调用函数，签名 func(messages, system=None, max_tokens=600, json_mode=False) -> str
            config: 评分配置
            system_prompt: 自定义系统提示（覆盖默认）
        """
        self.llm_call = llm_call
        self.config = config or ScoreConfig()
        self.system_prompt = system_prompt or "你是一位专业的信息质量评估专家。"

    def score_batch(
        self,
        results: List[SearchResult],
        query: str,
    ) -> List[SearchResult]:
        """
        批量打分

        Args:
            results: 搜索结果列表
            query: 原始查询

        Returns:
            打分后的结果列表（llm_score和grade已更新）
        """
        if not results or self.llm_call is None:
            return results

        scored = []
        for i, result in enumerate(results):
            # 只处理前 N 个
            if i >= self.config.max_batch_size:
                result.llm_score = 5.0
                result.score = 0.5
                scored.append(result)
                continue

            try:
                scored_result = self._score_single(result, query)
                scored.append(scored_result)
            except Exception as e:
                logger.warning(f"[SearchScorer] 打分失败 {result.url}: {e}")
                result.llm_score = 5.0
                result.score = 0.5
                scored.append(result)

        return scored

    def _score_single(self, result: SearchResult, query: str) -> SearchResult:
        """单个结果打分"""
        content = self._smart_truncate(result.content or result.snippet, self.config.max_content_length)

        prompt = self.DEFAULT_EVAL_PROMPT.format(
            query=query,
            title=result.title,
            content=content,
        )

        raw = self.llm_call(
            [{"role": "user", "content": prompt}],
            system=self.system_prompt,
            max_tokens=600,
            json_mode=self.config.json_mode,
        )

        data = self._safe_json_loads(raw, {})
        if isinstance(data, dict):
            result.llm_score = float(data.get("score", 5))
            result.facts = data.get("facts", [])
            result.reliability = data.get("reliability", "medium")
            result.score_reason = data.get("reason", "")
            result.score = result.llm_score / 10.0

            # 评分理由记录到日志（B角色可查看）
            logger.info(
                f"[SearchScorer] url={result.url[:60]} score={result.llm_score} "
                f"grade={data.get('reliability', '?')} reason={result.score_reason}"
            )

            # 评分→评级映射
            new_grade = score_to_grade(result.llm_score)
            if new_grade:
                result.grade = new_grade
                result.tier = new_grade.value
        else:
            result.llm_score = 5.0
            result.score = 0.5

        return result

    def verification_qa(self, result: SearchResult) -> List[Dict[str, str]]:
        """
        验证式QA：生成验证问题

        Args:
            result: 已打分的结果

        Returns:
            验证问题列表 [{"question": "...", "expected_answer": "..."}]
        """
        if not self.config.enable_verification_qa or self.llm_call is None:
            return []

        if result.llm_score < self.config.verification_threshold:
            return []

        if result.reliability != self.config.min_reliability_for_qa:
            return []

        content = self._smart_truncate(result.content or result.snippet, 800)
        prompt = self.DEFAULT_QA_PROMPT.format(
            title=result.title,
            content=content,
        )

        try:
            raw = self.llm_call(
                [{"role": "user", "content": prompt}],
                max_tokens=400,
                json_mode=self.config.json_mode,
            )
            qas = self._safe_json_loads(raw, [])
            if isinstance(qas, list):
                return qas[:self.config.max_qa_results]
        except Exception as e:
            logger.warning(f"[SearchScorer] 验证QA生成失败: {e}")

        return []

    def score_and_verify(
        self,
        results: List[SearchResult],
        query: str,
    ) -> Dict[str, Any]:
        """
        完整流程：打分 + 验证QA

        Returns:
            {"scored_results": [...], "verification_questions": {...}}
        """
        scored = self.score_batch(results, query)

        verification_map = {}
        for result in scored:
            if result.llm_score >= self.config.verification_threshold:
                qas = self.verification_qa(result)
                if qas:
                    verification_map[result.url] = qas

        return {
            "scored_results": scored,
            "verification_questions": verification_map,
        }

    @staticmethod
    def _smart_truncate(text: str, max_chars: int, min_ratio: float = 0.7) -> str:
        """智能截断：保留开头和结尾，中间省略"""
        if len(text) <= max_chars:
            return text

        keep_start = int(max_chars * min_ratio)
        keep_end = max_chars - keep_start - 3  # 3 for "..."

        if keep_end < 10:
            return text[:max_chars]

        return text[:keep_start] + "..." + text[-keep_end:]

    @staticmethod
    def _safe_json_loads(text: str, default: Any) -> Any:
        """安全JSON解析"""
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # 尝试提取代码块
            import re
            codeblock = re.search(r'```(?:json)?\s*(.*?)\s*```', text, re.DOTALL)
            if codeblock:
                try:
                    return json.loads(codeblock.group(1))
                except json.JSONDecodeError:
                    pass
            # 尝试提取花括号/方括号
            braces = re.search(r'[\[\{].*[\]\}]', text, re.DOTALL)
            if braces:
                try:
                    return json.loads(braces.group(0))
                except json.JSONDecodeError:
                    pass
        return default


# ============ 便捷函数 ============

def score_results(
    results: List[SearchResult],
    query: str,
    llm_call: Callable,
) -> List[SearchResult]:
    """快速打分函数"""
    scorer = SearchScorer(llm_call=llm_call)
    return scorer.score_batch(results, query)


def score_and_verify_results(
    results: List[SearchResult],
    query: str,
    llm_call: Callable,
) -> Dict[str, Any]:
    """快速打分+验证函数"""
    scorer = SearchScorer(llm_call=llm_call)
    return scorer.score_and_verify(results, query)
