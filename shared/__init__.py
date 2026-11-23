"""
共享模块 - 桌面调研工具使用的公共组件

包含：
- llm_client: JSON 清洗与解析工具
- search.models: 统一搜索结果模型
"""

from .llm_client import (
    extract_json_text, safe_json_loads,
)

# 导出 shared.search.models 中的 SearchResult，供外部统一导入
from .search.models import SearchResult as _SearchResult
SearchResult = _SearchResult


__all__ = [
    # llm_client
    'extract_json_text', 'safe_json_loads',
    # search.models
    'SearchResult',
]
