"""
RouteDispatcher — 搜索深度 × 搜索源 路由调度

根据用户选择的路线标签，决定：
- 搜索适配器组合
- 搜索预算（标准收束 / 搜到极致）
- GPTR retriever 类型
"""

from __future__ import annotations

from typing import Dict, List


class RouteDispatcher:
    """根据用户选择的搜索深度 + 搜索源，决定适配器组合和预算"""

    _DEEPSEEK_STANDARD = {
        "label": "DeepSeek·标准收束",
        "adapters": ["deepseek"],
        "search_budget": 20,
        "search_mode": "standard",
        "enable_terminate_saturation": False,
        "gptr_enabled": True,
        "gptr_parallel": False,
        "gptr_retriever": "deepseek",
    }

    _DEEPSEEK_EXHAUSTIVE = {
        "label": "DeepSeek·搜到极致",
        "adapters": ["deepseek"],
        "search_budget": 999,           # 无上限，靠 TERMINATE 停止
        "search_mode": "exhaustive",
        "enable_terminate_saturation": True,
        "gptr_enabled": True,
        "gptr_parallel": True,
        "gptr_retriever": "deepseek",
    }

    ROUTES: Dict[str, Dict] = {
        "deepseek_standard": _DEEPSEEK_STANDARD,
        "deepseek_exhaustive": _DEEPSEEK_EXHAUSTIVE,
        # 兼容旧 route key：全部指向 DeepSeek
        "bocha_standard": _DEEPSEEK_STANDARD,
        "bocha_exhaustive": _DEEPSEEK_EXHAUSTIVE,
        "xng_standard": _DEEPSEEK_STANDARD,
        "xng_exhaustive": _DEEPSEEK_EXHAUSTIVE,
    }

    @classmethod
    def get_route(cls, route_key: str) -> Dict:
        """获取路由配置，无效 key 返回默认 DeepSeek 标准路线"""
        return cls.ROUTES.get(route_key, cls.ROUTES["deepseek_standard"])

    @classmethod
    def get_route_keys(cls) -> List[str]:
        return list(cls.ROUTES.keys())

    @classmethod
    def get_route_labels(cls) -> Dict[str, str]:
        return {k: v["label"] for k, v in cls.ROUTES.items()}

    @classmethod
    def get_grouped_routes(cls) -> Dict[str, List[Dict]]:
        """按搜索源分组，供 GUI 渲染"""
        groups = {"deepseek": [], "bocha": [], "xng": []}
        for key, route in cls.ROUTES.items():
            group = key.split("_", 1)[0]
            if group in groups:
                groups[group].append({"key": key, **route})
        return groups