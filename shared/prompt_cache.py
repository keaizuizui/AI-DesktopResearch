"""
Prompt 缓存复用模块

借鉴 mini-coding-agent 的 Prompt Shape & Cache Reuse 设计：
- 静态前缀（系统提示 + 工具定义 + 项目上下文）只构建一次，可缓存复用
- 动态部分（用户消息 + 历史记录 + 工作记忆）每次变化

使用方式：
    builder = CachedPromptBuilder()
    prompt = builder.build(
        static_prefix=system_prompt + tools_desc,
        dynamic_parts={"history": history, "memory": memory, "user": user_msg}
    )
"""
import hashlib
import time
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field


@dataclass
class PromptCacheEntry:
    """缓存条目"""
    static_hash: str
    static_prefix: str
    created_at: float
    hit_count: int = 0


class CachedPromptBuilder:
    """
    Prompt 构建器，支持静态前缀缓存复用。
    
    核心思想：
    - 静态部分（系统提示、工具定义、项目上下文）变化频率低，构建成本高
    - 动态部分（用户消息、历史记录）变化频率高，构建成本低
    - 通过缓存静态部分，减少重复计算和 token 消耗
    """
    
    def __init__(self, max_cache_size: int = 10):
        self._cache: Dict[str, PromptCacheEntry] = {}
        self._max_cache_size = max_cache_size
        self._total_builds = 0
        self._cache_hits = 0
    
    def _hash_static(self, static_prefix: str) -> str:
        """计算静态前缀的哈希值"""
        return hashlib.sha256(static_prefix.encode('utf-8')).hexdigest()[:16]
    
    def build(
        self,
        static_prefix: str,
        dynamic_parts: Dict[str, str],
        separator: str = "\n\n"
    ) -> str:
        """
        构建完整 prompt
        
        Args:
            static_prefix: 静态前缀（系统提示 + 工具定义 + 上下文）
            dynamic_parts: 动态部分字典，如 {"history": ..., "memory": ..., "user": ...}
            separator: 各部分之间的分隔符
            
        Returns:
            完整 prompt 字符串
        """
        self._total_builds += 1
        static_hash = self._hash_static(static_prefix)
        
        # 检查缓存
        if static_hash in self._cache:
            entry = self._cache[static_hash]
            entry.hit_count += 1
            self._cache_hits += 1
            cached_prefix = entry.static_prefix
        else:
            # 新静态前缀，存入缓存
            cached_prefix = static_prefix
            self._cache[static_hash] = PromptCacheEntry(
                static_hash=static_hash,
                static_prefix=static_prefix,
                created_at=time.time()
            )
            # LRU 淘汰
            self._evict_if_needed()
        
        # 构建动态部分
        dynamic_sections = []
        for key in ['task', 'memory', 'history', 'user']:
            if key in dynamic_parts and dynamic_parts[key]:
                dynamic_sections.append(dynamic_parts[key])
        
        # 合并其他动态部分
        for key, value in dynamic_parts.items():
            if key not in ['task', 'memory', 'history', 'user'] and value:
                dynamic_sections.append(value)
        
        # 组合
        parts = [cached_prefix] + dynamic_sections
        return separator.join(parts)
    
    def _evict_if_needed(self):
        """LRU 淘汰：当缓存超过上限时，移除最少使用的条目"""
        if len(self._cache) > self._max_cache_size:
            # 按 hit_count 排序，移除最少使用的
            sorted_entries = sorted(
                self._cache.items(),
                key=lambda x: (x[1].hit_count, x[1].created_at)
            )
            # 移除前 20%
            to_remove = int(len(sorted_entries) * 0.2) or 1
            for key, _ in sorted_entries[:to_remove]:
                del self._cache[key]
    
    def get_stats(self) -> Dict[str, Any]:
        """获取缓存统计"""
        hit_rate = self._cache_hits / max(self._total_builds, 1)
        return {
            "total_builds": self._total_builds,
            "cache_hits": self._cache_hits,
            "hit_rate": f"{hit_rate:.1%}",
            "cache_size": len(self._cache),
            "max_cache_size": self._max_cache_size,
        }
    
    def clear(self):
        """清空缓存"""
        self._cache.clear()
        self._total_builds = 0
        self._cache_hits = 0


class LLMPromptManager:
    """
    LLM Prompt 管理器，为不同场景提供缓存化的 prompt 构建。
    
    使用示例：
        manager = LLMPromptManager()
        
        # 方案生成场景
        prompt = manager.build_solution_prompt(
            system_prompt="你是营销方案专家...",
            tools_desc="可用工具: search, write...",
            user_requirement="帮我写个奶茶品牌推广方案",
            history=conversation_history
        )
    """
    
    def __init__(self):
        self._builder = CachedPromptBuilder()
    
    def build_solution_prompt(
        self,
        system_prompt: str,
        tools_desc: str,
        user_requirement: str,
        history: List[Dict] = None,
        memory: str = ""
    ) -> str:
        """构建方案生成场景的 prompt"""
        static_prefix = f"{system_prompt}\n\n{tools_desc}"
        dynamic_parts = {
            "user": f"用户需求: {user_requirement}",
            "history": self._format_history(history) if history else "",
            "memory": memory,
        }
        return self._builder.build(static_prefix, dynamic_parts)
    
    def build_research_prompt(
        self,
        system_prompt: str,
        tools_desc: str,
        query: str,
        context: str = "",
        history: List[Dict] = None
    ) -> str:
        """构建调研场景的 prompt"""
        static_prefix = f"{system_prompt}\n\n{tools_desc}"
        dynamic_parts = {
            "user": f"调研主题: {query}",
            "memory": f"已知上下文: {context}" if context else "",
            "history": self._format_history(history) if history else "",
        }
        return self._builder.build(static_prefix, dynamic_parts)
    
    def build_chat_prompt(
        self,
        system_prompt: str,
        user_message: str,
        history: List[Dict] = None,
        memory: str = ""
    ) -> str:
        """构建通用对话场景的 prompt"""
        static_prefix = system_prompt
        dynamic_parts = {
            "memory": memory,
            "history": self._format_history(history) if history else "",
            "user": user_message,
        }
        return self._builder.build(static_prefix, dynamic_parts)
    
    def _format_history(self, history: List[Dict]) -> str:
        """格式化对话历史"""
        if not history:
            return ""
        lines = []
        for msg in history[-10:]:  # 只保留最近 10 轮
            role = msg.get("role", "user")
            content = msg.get("content", "")[:500]  # 单条限制 500 字符
            lines.append(f"[{role}] {content}")
        return "对话历史:\n" + "\n".join(lines)
    
    def get_stats(self) -> Dict[str, Any]:
        """获取缓存统计"""
        return self._builder.get_stats()


# 全局单例（可选）
_prompt_manager: Optional[LLMPromptManager] = None


def get_prompt_manager() -> LLMPromptManager:
    """获取全局 Prompt 管理器"""
    global _prompt_manager
    if _prompt_manager is None:
        _prompt_manager = LLMPromptManager()
    return _prompt_manager
