"""
共享模块 - 桌面调研工具使用的公共组件

包含：
- llm_client: 统一 LLM 客户端
- search.models: 统一搜索结果模型
"""

from .llm_client import (
    create_llm_client, LLMClientConfig,
    MockLLMClient, AsyncMockLLMClient,
    extract_json_text, safe_json_loads,
    # 多Provider容灾
    HealthChecker, ProviderManager, CheckpointManager, ResilientLLMClient,
)

# 导出 shared.search.models 中的 SearchResult，供外部统一导入
from .search.models import SearchResult as _SearchResult
SearchResult = _SearchResult


__all__ = [
    # llm_client
    'create_llm_client', 'LLMClientConfig',
    'MockLLMClient', 'AsyncMockLLMClient',
    'extract_json_text', 'safe_json_loads',
    # 多Provider容灾
    'HealthChecker', 'ProviderManager', 'CheckpointManager', 'ResilientLLMClient',
    # search.models
    'SearchResult',
]
