"""
上下文智能压缩模块

借鉴 mini-coding-agent 的 Context Reduction 设计：
- 长输出自动裁剪（clip）
- 重复读取去重（dedup）
- 旧记录压缩（只保留摘要）
- 总长度限制（确保不超出上下文窗口）

使用方式：
    from shared.context_compressor import ContextCompressor
    
    compressor = ContextCompressor(max_tokens=12000)
    compressed = compressor.compress_history(history)
"""
import re
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass


@dataclass
class HistoryItem:
    """历史记录项"""
    role: str  # "user", "assistant", "tool"
    content: str
    name: str = ""  # 工具名称（如果是 tool 角色）
    args: Optional[Dict] = None  # 工具参数
    is_recent: bool = False  # 是否是最近记录


class ContextCompressor:
    """
    上下文压缩器，智能管理对话历史长度。
    
    核心策略（借鉴 mini-coding-agent）：
    1. 保留最近 N 条记录的完整内容
    2. 旧记录压缩（只保留前 180 字符）
    3. 重复读取去重（相同文件的多次读取只保留最后一次）
    4. 总长度限制（超出时裁剪最旧的记录）
    """
    
    def __init__(
        self,
        max_tokens: int = 12000,
        recent_count: int = 6,
        recent_limit: int = 900,
        old_limit: int = 180,
        max_tool_output: int = 4000
    ):
        """
        Args:
            max_tokens: 总长度限制（字符数）
            recent_count: 最近保留完整内容的记录数
            recent_limit: 最近记录的内容长度限制
            old_limit: 旧记录的内容长度限制
            max_tool_output: 工具输出的最大长度
        """
        self.max_tokens = max_tokens
        self.recent_count = recent_count
        self.recent_limit = recent_limit
        self.old_limit = old_limit
        self.max_tool_output = max_tool_output
    
    def compress_history(
        self,
        history: List[Dict[str, Any]],
        preserve_tools: List[str] = None
    ) -> str:
        """
        压缩历史记录为文本
        
        Args:
            history: 原始历史记录列表
            preserve_tools: 需要保留完整内容的工具名称列表
            
        Returns:
            压缩后的文本
        """
        if not history:
            return "- empty"
        
        preserve_tools = preserve_tools or []
        
        # 转换为 HistoryItem
        items = self._parse_history(history)
        
        # 标记最近记录
        recent_start = max(0, len(items) - self.recent_count)
        for i in range(recent_start, len(items)):
            items[i].is_recent = True
        
        # 去重：相同文件的重复读取
        items = self._dedup_file_reads(items)
        
        # 压缩每条记录
        lines = []
        for item in items:
            line = self._compress_item(item, preserve_tools)
            if line:
                lines.append(line)
        
        # 总长度限制
        result = "\n".join(lines)
        if len(result) > self.max_tokens:
            result = self._truncate_total(result)
        
        return result
    
    def _parse_history(self, history: List[Dict]) -> List[HistoryItem]:
        """解析原始历史为 HistoryItem"""
        items = []
        for entry in history:
            role = entry.get("role", "user")
            content = entry.get("content", "")
            
            if role == "tool":
                items.append(HistoryItem(
                    role="tool",
                    content=content,
                    name=entry.get("name", ""),
                    args=entry.get("args", {})
                ))
            else:
                items.append(HistoryItem(role=role, content=content))
        
        return items
    
    def _dedup_file_reads(self, items: List[HistoryItem]) -> List[HistoryItem]:
        """
        去重：相同文件的重复读取只保留最近的一次。
        
        借鉴 mini-coding-agent 的 seen_reads 机制：
        - 如果文件被 write/patch 修改过，之前的读取记录失效
        - 非最近记录中的重复读取被跳过
        """
        seen_reads: set = set()
        result = []
        
        # 逆序遍历（从最新到最旧）
        for item in reversed(items):
            if item.role == "tool" and item.name == "read_file":
                path = str(item.args.get("path", "")) if item.args else ""
                if path:
                    if path in seen_reads and not item.is_recent:
                        # 跳过非最近的重复读取
                        continue
                    seen_reads.add(path)
            
            # 如果文件被修改，之前的读取记录失效
            if item.role == "tool" and item.name in ("write_file", "patch_file"):
                path = str(item.args.get("path", "")) if item.args else ""
                if path in seen_reads:
                    seen_reads.discard(path)
            
            result.append(item)
        
        # 恢复正序
        result.reverse()
        return result
    
    def _compress_item(
        self,
        item: HistoryItem,
        preserve_tools: List[str]
    ) -> Optional[str]:
        """压缩单条记录"""
        if item.role == "tool":
            # 工具调用记录
            args_str = str(item.args) if item.args else "{}"
            header = f"[tool:{item.name}] {args_str}"
            
            # 判断是否需要保留完整内容
            keep_full = (
                item.is_recent or
                item.name in preserve_tools or
                item.name in ("write_file", "patch_file")
            )
            
            limit = self.recent_limit if keep_full else self.old_limit
            content = self._clip(item.content, limit)
            
            return f"{header}\n{content}"
        
        else:
            # 用户/助手消息
            limit = self.recent_limit if item.is_recent else self.old_limit
            content = self._clip(item.content, limit)
            return f"[{item.role}] {content}"
    
    def _clip(self, text: str, limit: int) -> str:
        """裁剪文本到指定长度"""
        text = str(text)
        if len(text) <= limit:
            return text
        return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"
    
    def _truncate_total(self, text: str) -> str:
        """当总长度超出限制时，从中间截断"""
        if len(text) <= self.max_tokens:
            return text
        
        # 保留开头和结尾，中间截断
        keep = self.max_tokens - 50  # 预留截断标记空间
        head = keep // 2
        tail = keep - head
        
        return (
            text[:head] +
            f"\n\n...[中间截断 {len(text) - keep} 字符]...\n\n" +
            text[-tail:]
        )
    
    def compress_messages(
        self,
        messages: List[Dict[str, str]],
        max_messages: int = 20
    ) -> List[Dict[str, str]]:
        """
        压缩消息列表（用于 OpenAI/LiteLLM 的 messages 参数）
        
        Args:
            messages: 原始消息列表
            max_messages: 最大保留消息数
            
        Returns:
            压缩后的消息列表
        """
        if len(messages) <= max_messages:
            return messages
        
        # 保留系统消息
        system_msgs = [m for m in messages if m.get("role") == "system"]
        
        # 保留最近的消息
        other_msgs = [m for m in messages if m.get("role") != "system"]
        recent_msgs = other_msgs[-max_messages:]
        
        # 压缩旧消息（如果有）
        old_msgs = other_msgs[:-max_messages]
        if old_msgs:
            summary = self._summarize_old_messages(old_msgs)
            summary_msg = {"role": "system", "content": f"[历史摘要] {summary}"}
            return system_msgs + [summary_msg] + recent_msgs
        
        return system_msgs + recent_msgs
    
    def _summarize_old_messages(self, messages: List[Dict[str, str]]) -> str:
        """总结旧消息（简化版）"""
        # 统计角色分布
        roles = {}
        for m in messages:
            role = m.get("role", "unknown")
            roles[role] = roles.get(role, 0) + 1
        
        # 提取关键信息（工具调用）
        tools = []
        for m in messages:
            content = m.get("content", "")
            if "tool" in content.lower() or "执行" in content:
                tools.append(content[:50])
        
        summary_parts = []
        if roles:
            summary_parts.append(f"共 {len(messages)} 轮对话")
        if tools:
            summary_parts.append(f"涉及 {len(tools)} 次工具调用")
        
        return "; ".join(summary_parts) if summary_parts else "早期对话"
    
    def estimate_tokens(self, text: str) -> int:
        """
        估算文本的 token 数（粗略估计：1 token ≈ 1.5 中文字符 或 4 英文字符）
        """
        # 简单估算
        chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
        other_chars = len(text) - chinese_chars
        return int(chinese_chars / 1.5 + other_chars / 4)
    
    def get_stats(self) -> Dict[str, Any]:
        """获取压缩器统计"""
        return {
            "max_tokens": self.max_tokens,
            "recent_count": self.recent_count,
            "recent_limit": self.recent_limit,
            "old_limit": self.old_limit,
        }


# ============================================================
# 便捷函数
# ============================================================

def compress_chat_history(
    history: List[Dict],
    max_tokens: int = 12000
) -> str:
    """便捷函数：压缩对话历史"""
    compressor = ContextCompressor(max_tokens=max_tokens)
    return compressor.compress_history(history)


def compress_messages_for_llm(
    messages: List[Dict[str, str]],
    max_messages: int = 20
) -> List[Dict[str, str]]:
    """便捷函数：压缩消息列表供 LLM 使用"""
    compressor = ContextCompressor()
    return compressor.compress_messages(messages, max_messages)
