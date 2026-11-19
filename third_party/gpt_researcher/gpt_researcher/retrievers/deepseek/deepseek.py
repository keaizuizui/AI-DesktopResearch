# DeepSeek Search Retriever
#
# 使用 DeepSeek 官方网页搜索接口：
#   POST https://api.deepseek.com/v1/tools/search
# 鉴权与 DeepSeek LLM 相同，计费 5 元/千次成功请求。

import os
import logging
import requests

logger = logging.getLogger(__name__)


class DeepSeekSearch:
    """DeepSeek 官方网页搜索 Retriever"""

    def __init__(self, query, query_domains=None):
        self.query = query
        self.query_domains = query_domains or None
        self.api_key = (
            os.environ.get("DEEPSEEK_API_KEY")
            or os.environ.get("LLM_API_KEY")
            or os.environ.get("OPENAI_API_KEY", "")
        )
        self.endpoint = os.environ.get(
            "DEEPSEEK_SEARCH_ENDPOINT",
            "https://api.deepseek.com/v1/tools/search",
        )

    def search(self, max_results=7) -> list[dict[str, str]]:
        """执行搜索，返回 GPT-Researcher 统一的结果格式"""
        if not self.api_key or not self.query:
            return []

        # DeepSeek 限制 query 1-128 字符、count 1-50
        query = self.query.strip()[:128]
        count = max(1, min(int(max_results or 7), 50))

        try:
            resp = requests.post(
                self.endpoint,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={"query": query, "count": count, "need_content": False},
                timeout=30,
            )
            resp.raise_for_status()
            results = resp.json().get("results", []) or []
            return [
                {
                    "title": item.get("title", ""),
                    "href": item.get("url", ""),
                    "body": (item.get("summary") or item.get("snippet") or "")[:2000],
                }
                for item in results
            ]
        except Exception as e:
            logger.warning(f"[DeepSeekSearch] 搜索失败: {e}")
            return []
