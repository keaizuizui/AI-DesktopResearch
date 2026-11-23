#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""守卫逻辑回归测试：主题锚点、素材匹配、AI 训练知识章节保护。

运行：python test_guards.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import FusionResearchBot


def _bot(keywords=None, refined=""):
    b = object.__new__(FusionResearchBot)
    b.topic_keywords = keywords or []
    b.refined_task = refined
    return b


def test_anchors():
    cases = {
        "白酒和啤酒": {"白酒", "啤酒"},
        "宠物食品与白酒": {"宠物食品", "白酒"},
        "和路雪 市场份额": {"和路雪"},
        "上汽集团 销量": {"上汽集团"},
        "Nvidia 显卡": {"Nvidia", "显卡"},
        "AI chip": {"AI", "chip"},
    }
    for text, expected in cases.items():
        actual = set(FusionResearchBot._task_anchors(text))
        assert expected <= actual, f"{text} -> {actual}，缺少 {expected - actual}"


def test_material_matching():
    b = _bot(keywords=["无糖茶"])
    assert b._material_matches("2025年无糖茶市场规模约50亿元")
    assert not b._material_matches("乘用车出口同比增长5%")

    b2 = _bot(keywords=["Nvidia"])
    assert b2._material_matches("NVIDIA GPU market grew 20%")
    assert not b2._material_matches("AMD CPU market grew 10%")


def test_ai_chapter_detection():
    placeholder = "### 标题\n\n本章无素材，扩写跳过。"
    assert FusionResearchBot._is_placeholder_chapter(placeholder)

    ai_chapter = "### 【AI训练知识】标题\n\n> 声明\n\n" + "正文" * 100
    assert not FusionResearchBot._is_placeholder_chapter(ai_chapter)


def test_topic_keywords():
    class LLM:
        def fast(self, *args, **kwargs):
            return "无糖茶，即饮茶，东方树叶，市场"

    b = _bot()
    b.llm = LLM()
    assert b._extract_topic_keywords("调研无糖茶") == ["无糖茶", "即饮茶", "东方树叶"]

    class BadLLM:
        def fast(self, *args, **kwargs):
            raise RuntimeError("boom")

    b2 = _bot()
    b2.llm = BadLLM()
    fallback = b2._extract_topic_keywords("调研无糖茶")
    assert fallback and "调研" not in fallback, f"应回退规则锚点，实际 {fallback}"


def test_editorial_skip_ai_chapter():
    ch = "### 【AI训练知识】价格带\n\n> 声明\n\n" + "正文" * 100
    b = _bot()
    b.llm = None
    assert b._editorial_review_chapters([ch]) == [ch]


def test_budget_truncation():
    class FakeSearcher:
        def __init__(self):
            self.calls = []

        def multi_search(self, queries, max_results=3, workers=3, aggressive=False):
            self.calls.append(list(queries))
            return []

    b = _bot()
    b.state = type("State", (), {"search_budget": 2})()
    b.searcher = FakeSearcher()

    results, received = b._search_within_budget(["q1", "q2", "q3", "q4", "q5"])

    assert received == ["q1", "q2"], f"应只执行前2条，实际 {received}"
    assert len(b.searcher.calls) == 1
    assert b.searcher.calls[0] == ["q1", "q2"]
    assert results == []
    assert b.state.search_budget >= 0


def test_budget_charge_on_hit():
    """预算只在命中后扣，按实际执行的查询数扣，且不透支。"""
    class FakeState:
        def __init__(self, budget):
            self.search_budget = budget

        def consume_budget(self, amount=1):
            if self.search_budget >= amount:
                self.search_budget -= amount
                return True
            return False

    b = _bot()
    b.state = FakeState(5)

    assert b._charge_search(["q1", "q2", "q3"]) == 3
    assert b.state.search_budget == 2

    assert b._charge_search(["q1", "q2"]) == 2
    assert b.state.search_budget == 0

    assert b._charge_search(["q1"]) == 0, "预算为0时不能透支"
    assert b.state.search_budget == 0


def test_ai_fallback_failure_not_marked():
    """两轮都是占位回复时返回空串，不能产出带【AI训练知识】的空壳章节。"""
    class FakeLLM:
        def __init__(self):
            self.calls = 0

        def chat(self, messages, **kwargs):
            self.calls += 1
            return "未收到内容，请提供章节素材。"

    b = _bot()
    b.llm = FakeLLM()
    out = b._write_ai_knowledge_chapter(
        {"title": "价格带与产品结构", "points": ["价格带分布"]}
    )

    assert out == "", f"占位回复应返回空串，实际：{out[:60]}"
    assert "【AI训练知识】" not in out
    assert b.llm.calls == 2, f"应重试两轮，实际 {b.llm.calls}"


def test_blank_material_filtered():
    """空白正文素材视为无素材；fact_card 有内容则可用。"""
    assert not FusionResearchBot._material_has_content({"meta": {"content": "   "}})
    assert not FusionResearchBot._material_has_content(
        {"meta": {"content": "", "fact_card": "  \n "}}
    )
    assert FusionResearchBot._material_has_content({"meta": {"content": "正文内容"}})
    assert FusionResearchBot._material_has_content(
        {"meta": {"fact_card": "<div>市场份额 30%</div>"}}
    )


def test_promo_merge_and_label():
    """同品牌软文合并为一条信源并保留来源；企业自述带标注且跳过主题词过滤。"""
    def mat(url, title, content):
        return {"score": 0.5, "text": content,
                "meta": {"url": url, "title": title, "content": content,
                         "source_type": "promo", "claim_nature": "企业自述"}}

    b = _bot(keywords=["无糖茶"])
    tm = [
        mat("https://aob.com.cn/1", "首馆揭牌", "2025年9月哈尔滨首馆揭牌。"),
        mat("https://aob.com.cn/2", "七省市布局", "北京、黑龙江、吉林等七省市分馆相继揭牌。"),
        mat("https://aob.com.cn/3", "整馆输出", "南京医械展主推整馆输出模式。"),
        mat("https://news.example.com/a", "市监局通报", "青岛市场监管局通报加盟店虚假宣传。"),
    ]
    merged = b._merge_promo_sources(tm)
    assert len(merged) == 2, f"3条同域软文应合并为1条，实际 {len(merged)}"
    promo = [m for m in merged if m["meta"].get("merged_count") == 3][0]
    assert "首馆揭牌" in promo["meta"]["content"] and "整馆输出" in promo["meta"]["content"]
    assert len(promo["meta"]["merged_sources"]) == 3
    assert b._claim_prefix(promo["meta"]) == "[企业自述] "
    assert b._claim_prefix({"claim_nature": "第三方数据"}) == ""
    assert "同源合并 3 篇" in b._merge_source_hint(promo["meta"])

    # 软文标题已抽象掉主题词仍要放行；无关第三方数据仍被挡
    assert b._material_matches("首馆揭牌 哈尔滨", b.refined_task, "企业自述")
    assert not b._material_matches("乘用车出口同比增长5%", b.refined_task, "第三方数据")


def test_numeric_cell_and_material_relevant():
    """数值单元格识别（含约数/区间/单位）与素材对象相关性判定。"""
    from deliverables import _is_numeric_cell
    assert _is_numeric_cell("约 40%")
    assert _is_numeric_cell("15-20 万元")
    assert _is_numeric_cell("6-16 岁")
    assert _is_numeric_cell("3个月")
    assert _is_numeric_cell("5%-10%")
    assert _is_numeric_cell("-5%")
    assert _is_numeric_cell("约 15-20 万元")
    assert not _is_numeric_cell("28家，居全国首位")
    assert not _is_numeric_cell("百万级")

    class _M:
        def __init__(self, t, c, n=""):
            self.title, self.content, self.claim_nature = t, c, n

    b = _bot(keywords=["无糖茶"])
    assert b._material_relevant(_M("门店模型", "无糖茶门店 单店投入 15-20 万元", "第三方数据"), b.refined_task)
    assert b._material_relevant(_M("招商话术", "加盟政策与区域保护", "企业自述"), b.refined_task)
    assert not b._material_relevant(_M("乘用车", "出口同比增长", "第三方数据"), b.refined_task)


if __name__ == "__main__":
    test_anchors()
    test_material_matching()
    test_ai_chapter_detection()
    test_topic_keywords()
    test_editorial_skip_ai_chapter()
    test_budget_truncation()
    test_budget_charge_on_hit()
    test_ai_fallback_failure_not_marked()
    test_blank_material_filtered()
    test_promo_merge_and_label()
    test_numeric_cell_and_material_relevant()
    print("guards: 11/11 passed")
