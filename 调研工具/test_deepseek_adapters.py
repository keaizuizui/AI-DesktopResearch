#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DeepSeek 搜索/抓取适配器单元测试（mock HTTP，不发真实请求）"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
TOOL_DIR = Path(__file__).resolve().parent
for path in (str(ROOT), str(TOOL_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from shared.search.adapters import DeepSeekSearchAdapter
from shared.billing.router import RouteDispatcher
from deep_scraper import DeepSeekFetchAdapter


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class TestDeepSeekSearchAdapter(unittest.TestCase):
    @patch("shared.search.adapters.requests.post")
    def test_maps_results_and_limits_params(self, mock_post):
        mock_post.return_value = FakeResponse(200, {
            "results": [{
                "rerank_score": 2.4,
                "title": "测试标题",
                "url": "https://example.com/a",
                "snippet": "短摘要",
                "summary": "长摘要内容",
                "content": "",
                "date": "2026-06-01T00:00:00+00:00",
                "authority": 3,
            }]
        })

        results = DeepSeekSearchAdapter("test-key").search("q" * 200, 80)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "测试标题")
        self.assertEqual(results[0].source, "deepseek")
        self.assertEqual(results[0].grade.value, "A")
        self.assertEqual(results[0].content, "长摘要内容")
        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(len(payload["query"]), 128)
        self.assertEqual(payload["count"], 50)

    @patch("shared.search.adapters.requests.post")
    def test_returns_empty_on_auth_error(self, mock_post):
        mock_post.return_value = FakeResponse(401, {})
        self.assertEqual(DeepSeekSearchAdapter("bad-key").search("query"), [])


class TestDeepSeekFetchAdapter(unittest.TestCase):
    @patch("deep_scraper.requests.post")
    def test_maps_content(self, mock_post):
        mock_post.return_value = FakeResponse(200, {"title": "T", "content": "# 正文"})
        self.assertEqual(
            DeepSeekFetchAdapter("test-key").scrape("https://example.com/a"),
            "# 正文",
        )

    @patch("deep_scraper.requests.post")
    def test_returns_none_on_http_error(self, mock_post):
        mock_post.return_value = FakeResponse(404, {})
        self.assertIsNone(DeepSeekFetchAdapter("test-key").scrape("https://example.com/a"))


class TestDeepSeekRoute(unittest.TestCase):
    def test_default_route_uses_deepseek(self):
        route = RouteDispatcher.get_route("deepseek_standard")
        self.assertEqual(route["adapters"], ["deepseek"])
        self.assertEqual(route["gptr_retriever"], "deepseek")


if __name__ == "__main__":
    unittest.main(verbosity=2)
