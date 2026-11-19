"""
Input Guard Module
统一输入防护模块 - 防止 prompt 注入和敏感信息泄露

整合来源：
- 方案工具 src/render/input_guard.py（注入检测、输入截断）
- shared/sanitize.py（敏感信息过滤）

所有接收用户输入的工具都应使用此模块。
"""
from __future__ import annotations
import re
import logging
from typing import Optional, List, Tuple
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class GuardAction(Enum):
    """防护动作"""
    ALLOW = "allow"          # 允许通过
    CLEAN = "clean"          # 清洗后通过
    BLOCK = "block"          # 阻断
    WARN = "warn"            # 警告但通过


class GuardSeverity(Enum):
    """威胁严重级别"""
    CRITICAL = "critical"    # 严重注入攻击
    HIGH = "high"            # 高风险
    MEDIUM = "medium"        # 中等风险
    LOW = "low"              # 低风险


@dataclass
class GuardResult:
    """防护结果"""
    action: GuardAction
    cleaned_text: str
    original_text: str
    threats: List[Tuple[str, GuardSeverity]]  # (威胁描述, 级别)
    truncated: bool = False
    metadata: dict = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


# ============ Prompt 注入检测模式 ============

_INJECTION_PATTERNS = [
    (r'ignore\s+(all\s+)?(?:previous|above|the)\s+(?:instructions?|prompts?|rules?)', GuardSeverity.HIGH),
    (r'disregard\s+(?:all\s+)?(?:previous|above|your)\s+(?:instructions?|prompts?)', GuardSeverity.HIGH),
    (r'forget\s+(?:all\s+)?(?:previous|above|your)\s+(?:instructions?|prompts?|rules?)', GuardSeverity.HIGH),
    (r'you\s+(?:are|are\s+now|become|act\s+as)\s+(?:an?\s+)?(?:unrestricted|unconstrained|free|jailbroken)', GuardSeverity.HIGH),
    (r'(?:pretend|act)\s+(?:you\s+are|as\s+if)\s+', GuardSeverity.MEDIUM),
    (r'dAN\s+mode', GuardSeverity.HIGH),
    (r'(?:输出|打印|显示|返回|告诉我|展示|reveal|output|print|show|return|tell\s+me|display)\s*(?:你的|the|all|full|complete|entire|your)?\s*(?:完整)?(?:system\s*prompt|内部指令|工作流程|prompt|instructions?|rules?|guidelines?|system\s*message|internal\s*(?:prompt|instructions?))', GuardSeverity.CRITICAL),
    (r'(?:重复|repeat|echo|copy|paste)\s*(?:你的|the|back|all)?\s*(?:完整)?(?:system\s+prompt|内部指令|prompt|instructions?|rules?)', GuardSeverity.CRITICAL),
    (r'(?:泄露|expose|leak|disclose|dump)\s+(?:你的|the|all|your)?\s*(?:内部|internal|system|hidden|secret)?\s*(?:指令|prompt|instructions?|rules?|process|methodology|algorithm|架构|architecture)', GuardSeverity.CRITICAL),
    (r'---+\s*(?:以上|above|previous)\s*(?:是指|is|are)\s*(?:无效|invalid|not)', GuardSeverity.HIGH),
    (r'={3,}\s*(?:新|new)\s*(?:指令|instructions?|任务|task)', GuardSeverity.HIGH),
    (r"(?:不要|don'?t|do\s+not)\s*(?:只|only)\s*(?:输出|output)\s*(?:JSON|json)", GuardSeverity.MEDIUM),
    (r'(?:改为|change|switch\s+to)\s*(?:输出|outputting)\s*(?:纯文本|plain\s+text|markdown|自由格式)', GuardSeverity.MEDIUM),
    (r'(?:跳过|skip|bypass)\s*(?:你的|the|all)\s*(?:验证|validation|检查|check|过滤|filter|安全|security)', GuardSeverity.HIGH),
    (r'请?(?:完整|full|详细|detailed)?(?:输出|告诉我|展示|说明|解释|describe|explain)\s*(?:一下|一下)?\s*(?:你的|you|the|this)\s*(?:系统|system|内部|internal|核心|core|底层|underlying)\s*(?:指令|instructions?|规则|rules?|逻辑|logic|原理|principle|方法|method|架构|architecture|设计|design|流程|process)', GuardSeverity.CRITICAL),
]

# 编译正则
_COMPILED_INJECTION_PATTERNS: List[Tuple[re.Pattern, GuardSeverity]] = []
for pattern, severity in _INJECTION_PATTERNS:
    try:
        _COMPILED_INJECTION_PATTERNS.append((re.compile(pattern, flags=re.IGNORECASE), severity))
    except re.error as e:
        logger.error(f"[InputGuard] 正则编译失败: {e}")


# ============ 敏感信息模式 ============

_SENSITIVE_PATTERNS = [
    # API Keys
    (r'sk-[a-zA-Z0-9]{48}', '[API_KEY_REDACTED]'),
    (r'[a-zA-Z0-9]{32}-[a-zA-Z0-9]{32}', '[API_KEY_REDACTED]'),
    (r'[a-f0-9]{64}', '[API_KEY_REDACTED]'),
    # 密码
    (r'password\s*=\s*["\'][^"\']+["\']', 'password = [REDACTED]'),
    (r'passwd\s*=\s*["\'][^"\']+["\']', 'passwd = [REDACTED]'),
    (r'pwd\s*=\s*["\'][^"\']+["\']', 'pwd = [REDACTED]'),
    # 密钥
    (r'secret\s*=\s*["\'][^"\']+["\']', 'secret = [REDACTED]'),
    (r'key\s*=\s*["\'][^"\']{16,}["\']', 'key = [REDACTED]'),
    # 邮箱密码
    (r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}:[^\s@]+', '[EMAIL_CREDENTIALS_REDACTED]'),
]


# ============ 输入长度限制配置 ============

DEFAULT_MAX_LENGTH = 5000
TOOL_LENGTH_LIMITS = {
    "brief": 2000,       # 方案需求
    "topic": 1000,       # 调研/洞察主题
    "statement": 2000,   # 商业主张
    "content": 5000,     # 通用内容
    "query": 1000,       # 搜索查询
    "chat": 3000,        # 聊天输入
}


class InputGuard:
    """
    统一输入防护器

    功能：
    1. Prompt 注入检测与清洗
    2. 敏感信息过滤
    3. 输入长度截断
    4. 威胁分级与动作决策
    """

    def __init__(self, max_length: int = DEFAULT_MAX_LENGTH):
        self.max_length = max_length

    def check(self, text: str, context: str = "general") -> GuardResult:
        """
        全面检查输入

        Args:
            text: 原始输入文本
            context: 使用场景（brief/topic/statement/content/query/chat/general）

        Returns:
            GuardResult: 防护结果
        """
        if not isinstance(text, str):
            return GuardResult(
                action=GuardAction.ALLOW,
                cleaned_text=str(text),
                original_text=str(text),
                threats=[],
            )

        original = text
        threats = []
        truncated = False

        # 1. 长度检查与截断
        limit = TOOL_LENGTH_LIMITS.get(context, self.max_length)
        if len(text) > limit:
            text = text[:limit]
            truncated = True
            logger.debug(f"[InputGuard] 输入截断: {len(original)}->{limit}")

        # 2. 注入检测
        for regex, severity in _COMPILED_INJECTION_PATTERNS:
            if regex.search(text):
                threats.append((f"检测到注入模式: {regex.pattern[:50]}...", severity))
                text = regex.sub("[已清洗]", text)

        # 3. 敏感信息过滤
        for pattern, replacement in _SENSITIVE_PATTERNS:
            if re.search(pattern, text, flags=re.IGNORECASE):
                threats.append((f"检测到敏感信息", GuardSeverity.MEDIUM))
                text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)

        # 4. 清理多余空行
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = text.strip()

        # 5. 决策动作
        action = self._decide_action(threats)

        return GuardResult(
            action=action,
            cleaned_text=text,
            original_text=original,
            threats=threats,
            truncated=truncated,
            metadata={"context": context, "limit": limit},
        )

    def _decide_action(self, threats: List[Tuple[str, GuardSeverity]]) -> GuardAction:
        """根据威胁级别决定动作"""
        if not threats:
            return GuardAction.ALLOW

        severities = [s for _, s in threats]
        if GuardSeverity.CRITICAL in severities:
            return GuardAction.BLOCK
        if GuardSeverity.HIGH in severities:
            return GuardAction.CLEAN
        if GuardSeverity.MEDIUM in severities:
            return GuardAction.WARN
        return GuardAction.ALLOW

    # ============ 便捷方法 ============

    def sanitize_brief(self, brief: str) -> GuardResult:
        """方案需求专用清洗（更严格，限制2000字）"""
        return self.check(brief, context="brief")

    def sanitize_topic(self, topic: str) -> GuardResult:
        """调研/洞察主题专用清洗（限制1000字）"""
        return self.check(topic, context="topic")

    def sanitize_statement(self, statement: str) -> GuardResult:
        """商业主张专用清洗（限制2000字）"""
        return self.check(statement, context="statement")

    def sanitize_content(self, content: str, max_length: int = None) -> GuardResult:
        """通用内容清洗"""
        old_limit = self.max_length
        if max_length:
            self.max_length = max_length
        result = self.check(content, context="content")
        self.max_length = old_limit
        return result

    def sanitize_query(self, query: str) -> GuardResult:
        """搜索查询清洗"""
        return self.check(query, context="query")


# ============ 全局单例 ============

_default_guard = InputGuard()


def sanitize(text: str, max_length: int = DEFAULT_MAX_LENGTH) -> str:
    """快速清洗：返回清洗后的文本（兼容旧接口）"""
    guard = InputGuard(max_length=max_length)
    result = guard.check(text)
    return result.cleaned_text


def sanitize_brief(brief: str) -> str:
    """方案需求专用清洗"""
    return _default_guard.sanitize_brief(brief).cleaned_text


def sanitize_topic(topic: str) -> str:
    """调研/洞察主题专用清洗"""
    return _default_guard.sanitize_topic(topic).cleaned_text


def sanitize_statement(statement: str) -> str:
    """商业主张专用清洗"""
    return _default_guard.sanitize_statement(statement).cleaned_text


def sanitize_content(content: str, max_length: int = 5000) -> str:
    """通用内容清洗"""
    return _default_guard.sanitize_content(content, max_length=max_length).cleaned_text


def check_input(text: str, context: str = "general") -> GuardResult:
    """完整检查：返回详细结果"""
    return _default_guard.check(text, context=context)
