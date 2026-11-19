"""
Agent 安全管控层（轻量级实现）

参考 agent-governance-toolkit 的设计，提供：
1. 策略执行（域名白名单、API调用限制）
2. 审计日志（记录每次Agent决策）
3. 敏感信息过滤

不依赖外部包，纯Python实现。
"""

import os
import re
import json
import hashlib
import logging
from typing import Dict, List, Optional, Tuple
from datetime import datetime
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)


@dataclass
class SecurityPolicy:
    """安全策略"""
    name: str
    description: str
    # 域名白名单（空列表=允许所有）
    allowed_domains: List[str]
    # 域名黑名单
    blocked_domains: List[str]
    # API调用频率限制（每分钟）
    rate_limit_per_minute: int
    # 敏感信息模式
    sensitive_patterns: List[str]
    # 允许的操作类型
    allowed_actions: List[str]


@dataclass
class AuditRecord:
    """审计记录"""
    timestamp: str
    agent_id: str
    action: str
    target: str
    decision: str  # "allow" / "deny" / "warn"
    reason: str
    policy_name: str
    context: Dict


class AgentSecurityGuard:
    """Agent 安全守卫

    为调研工具、洞察工具等提供安全管控。
    """

    # 默认策略：营销调研场景
    DEFAULT_POLICY = SecurityPolicy(
        name="marketing_research",
        description="营销调研安全策略",
        allowed_domains=[
            "baidu.com", "bing.com", "google.com",
            "zhihu.com", "weibo.com", "xiaohongshu.com",
            "toutiao.com", "36kr.com", "iheima.com",
            "sec.gov", "reuters.com", "bloomberg.com",
            "github.com", "pypi.org",
        ],
        blocked_domains=[
            "localhost", "127.0.0.1", "0.0.0.0",
            "192.168.", "10.0.", "172.16.",
        ],
        rate_limit_per_minute=60,
        sensitive_patterns=[
            r"api[_-]?key\s*[:=\s]\s*['\"]?[\w-]+",
            r"password\s+is\s+['\"]?[\w-]+",
            r"password\s*[:=]\s*['\"]?[\w-]+",
            r"token\s*[:=]\s*['\"]?[\w-]+",
            r"secret\s*[:=]\s*['\"]?[\w-]+",
        ],
        allowed_actions=["search", "fetch", "analyze", "report"],
    )

    def __init__(self, policy: SecurityPolicy = None, audit_dir: str = None):
        """初始化安全守卫

        Args:
            policy: 安全策略（默认营销调研策略）
            audit_dir: 审计日志目录
        """
        self.policy = policy or self.DEFAULT_POLICY
        self.audit_dir = audit_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", "audit_logs"
        )
        os.makedirs(self.audit_dir, exist_ok=True)

        # 调用频率控制
        self._call_history: List[float] = []

        logger.info(f"[AgentSecurity] 初始化完成 | 策略: {self.policy.name}")

    def check_url(self, url: str, agent_id: str = "default") -> Tuple[bool, str]:
        """检查URL是否允许访问

        Returns:
            (allowed, reason)
        """
        # 提取域名
        domain_match = re.search(r"https?://([^/]+)", url)
        if not domain_match:
            self._audit(agent_id, "check_url", url, "deny", "无法解析域名")
            return False, "无法解析URL域名"

        domain = domain_match.group(1).lower()

        # 检查黑名单
        for blocked in self.policy.blocked_domains:
            if blocked in domain:
                self._audit(agent_id, "check_url", url, "deny", f"命中黑名单: {blocked}")
                return False, f"域名在黑名单中: {blocked}"

        # 检查白名单（如果配置了）
        if self.policy.allowed_domains:
            allowed = any(allowed in domain for allowed in self.policy.allowed_domains)
            if not allowed:
                self._audit(agent_id, "check_url", url, "deny", "不在白名单中")
                return False, f"域名不在白名单中: {domain}"

        self._audit(agent_id, "check_url", url, "allow", "通过检查")
        return True, "通过"

    def check_rate_limit(self, agent_id: str = "default") -> Tuple[bool, str]:
        """检查是否超过频率限制

        Returns:
            (allowed, reason)
        """
        now = datetime.now().timestamp()
        # 清理1分钟前的记录
        self._call_history = [t for t in self._call_history if now - t < 60]

        if len(self._call_history) >= self.policy.rate_limit_per_minute:
            self._audit(agent_id, "rate_limit", "", "deny", "超过频率限制")
            return False, f"超过频率限制: {self.policy.rate_limit_per_minute}次/分钟"

        self._call_history.append(now)
        return True, "通过"

    def check_sensitive_info(self, text: str, agent_id: str = "default") -> Tuple[bool, str, List[str]]:
        """检查是否包含敏感信息

        Returns:
            (clean, reason, found_patterns)
        """
        found = []
        for pattern in self.policy.sensitive_patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            found.extend(matches)

        if found:
            # 打码处理
            masked_text = text
            for match in found:
                masked = match[:4] + "****" + match[-4:] if len(match) > 8 else "****"
                masked_text = masked_text.replace(match, masked)

            self._audit(agent_id, "sensitive_check", "", "warn", f"发现{len(found)}处敏感信息")
            return False, f"发现敏感信息（已打码）", found

        return True, "无敏感信息", []

    def check_action(self, action: str, agent_id: str = "default") -> Tuple[bool, str]:
        """检查操作是否允许

        Returns:
            (allowed, reason)
        """
        if action not in self.policy.allowed_actions:
            self._audit(agent_id, "check_action", action, "deny", "不允许的操作类型")
            return False, f"不允许的操作: {action}"

        self._audit(agent_id, "check_action", action, "allow", "允许的操作")
        return True, "通过"

    def govern_search(self, query: str, url: str = None, agent_id: str = "research") -> Tuple[bool, str, Dict]:
        """治理搜索请求（组合检查）

        Returns:
            (allowed, reason, audit_info)
        """
        checks = []

        # 1. 操作类型检查
        ok, reason = self.check_action("search", agent_id)
        checks.append({"check": "action", "ok": ok, "reason": reason})
        if not ok:
            return False, reason, {"checks": checks}

        # 2. 频率限制检查
        ok, reason = self.check_rate_limit(agent_id)
        checks.append({"check": "rate_limit", "ok": ok, "reason": reason})
        if not ok:
            return False, reason, {"checks": checks}

        # 3. URL检查（如果有）
        if url:
            ok, reason = self.check_url(url, agent_id)
            checks.append({"check": "url", "ok": ok, "reason": reason})
            if not ok:
                return False, reason, {"checks": checks}

        # 4. 敏感信息检查
        ok, reason, found = self.check_sensitive_info(query, agent_id)
        checks.append({"check": "sensitive", "ok": ok, "reason": reason, "found": found})

        return True, "所有检查通过", {"checks": checks}

    def _audit(self, agent_id: str, action: str, target: str, decision: str, reason: str):
        """记录审计日志"""
        record = AuditRecord(
            timestamp=datetime.now().isoformat(),
            agent_id=agent_id,
            action=action,
            target=target,
            decision=decision,
            reason=reason,
            policy_name=self.policy.name,
            context={},
        )

        # 写入日志文件
        log_file = os.path.join(self.audit_dir, f"audit_{datetime.now().strftime('%Y%m%d')}.jsonl")
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")

        # 同时输出到logger
        if decision == "deny":
            logger.warning(f"[AgentSecurity] 拒绝: {action} | {target} | {reason}")
        elif decision == "warn":
            logger.warning(f"[AgentSecurity] 警告: {action} | {target} | {reason}")


# 全局单例
_security_guard_instance: Optional[AgentSecurityGuard] = None


def get_security_guard() -> AgentSecurityGuard:
    """获取安全守卫单例"""
    global _security_guard_instance
    if _security_guard_instance is None:
        _security_guard_instance = AgentSecurityGuard()
    return _security_guard_instance
