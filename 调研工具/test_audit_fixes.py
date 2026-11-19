#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
审计修复的确定性回归测试（C1/C2/C3/C4/C5/C6/C7/C8）

约束：
- 仅使用标准库 unittest + unittest.mock
- 不访问真实网络 / 真实 API / 真实 Ollama
- 所有 sleep 均被 mock，整体运行 < 30 秒

运行：
    cd 调研工具
    python -m unittest test_audit_fixes -v
"""

import asyncio
import json
import os
import unittest
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

from search import GPTRResearcherAdapter
import search
import engine
from material_synthesizer import MaterialClusterer


# UTF-8 中文被 Latin-1 误读后的典型乱码（与生产代码 _repair_mojibake 对应）
MOJIBAKE_CN = "中文测试".encode("utf-8").decode("latin-1")


def _make_adapter(cfg=None):
    """构造不依赖 LLM 的 adapter；llm 传 None 即可，测试路径不会用到。"""
    return GPTRResearcherAdapter(cfg or {}, llm=None)


def _make_bot(gui, inferable_context=""):
    """用 object.__new__ 绕过 FusionResearchBot 的重型构造（LLM/向量库/搜索管理器）。"""
    bot = object.__new__(engine.FusionResearchBot)
    bot.gui = gui
    bot.cfg = {}
    bot.raw_task = "测试任务"
    bot.refined_task = "测试任务"
    bot.plan_text = ""
    bot.inferable_context = inferable_context
    bot.perspective_constraint = ""
    bot.clarify_callback = None
    return bot


def _questions_json(*questions):
    return json.dumps(list(questions), ensure_ascii=False)


class _FakeDeductionEngine:
    """DeductionEngine 的替身：结果与捕获的候选问题由类属性注入。"""

    result = {}
    context_text = ""
    captured = {}

    def __init__(self, llm_call):
        self._llm_call = llm_call

    def filter_questions(self, task, candidate_questions, max_keep=5):
        type(self).captured["candidates"] = list(candidate_questions)
        return type(self).result

    def build_injectable_context(self, result):
        return type(self).context_text

    def build_perspective_constraint(self, result):
        return ""


class TestC1RemoteNoAutoStart(unittest.TestCase):
    """C1: 远程 GPTR 地址不得自动拉起本地服务。"""

    def test_remote_url_does_not_popen(self):
        cfg = {
            "gptr_service_url": "http://10.0.0.5:8000/api/v1",
            "gptr_auto_start": True,
        }
        adapter = _make_adapter(cfg)

        with mock.patch("subprocess.Popen") as popen:
            self.assertFalse(adapter._start_local_service())

        popen.assert_not_called()
        # 远程地址不应把"已尝试启动"置真，否则会污染后续本地判断
        self.assertFalse(adapter._auto_start_attempted)

    def test_local_url_still_attempts_start(self):
        """可选用例：本机地址仍会尝试启动本地服务。"""
        cfg = {
            "gptr_service_url": "http://127.0.0.1:8000/api/v1",
            "gptr_auto_start": True,
            "gptr_service_path": os.path.dirname(os.path.abspath(__file__)),
        }
        adapter = _make_adapter(cfg)
        fake_proc = MagicMock(name="proc")

        with mock.patch("subprocess.Popen", return_value=fake_proc) as popen, \
                mock.patch.object(search.os.path, "isfile", return_value=True), \
                mock.patch.object(search.atexit, "register"):
            self.assertTrue(adapter._start_local_service())

        popen.assert_called_once()
        self.assertIs(adapter._service_proc, fake_proc)


class TestC2C3ParallelTimeoutAndMojibake(unittest.TestCase):
    """C2/C3: 并行子任务 timeout 使用配置值；合并结果修复 Latin-1 乱码。"""

    def test_submit_timeout_and_mojibake_repair(self):
        cfg = {
            "gptr_service_url": "http://127.0.0.1:8000/api/v1",
            "gptr_task_timeout": 600,
        }
        adapter = _make_adapter(cfg)

        submitted = []
        result_text = MOJIBAKE_CN + " " + ("abc" * 20)  # 长度 > 50，满足结果有效性门槛

        async def fake_submit_async(**kwargs):
            submitted.append(kwargs)
            return f"tid-{len(submitted)}"

        async def fake_get_status(tid):
            return {"status": "completed", "result": result_text}

        client = MagicMock()
        client.submit_async = AsyncMock(side_effect=fake_submit_async)
        client.get_status = AsyncMock(side_effect=fake_get_status)

        with mock.patch.object(adapter, "_ensure_gptr_available",
                               new=AsyncMock(return_value=True)), \
                mock.patch.object(adapter, "_get_gptr_client", return_value=client), \
                mock.patch.object(search, "_build_parallel_queries",
                                   return_value=["子查询一", "子查询二"]), \
                mock.patch.object(search.asyncio, "sleep", new=AsyncMock()):
            merged = asyncio.run(
                adapter.conduct_research_parallel("计划", "测试任务")
            )

        # C2: 提交超时必须使用配置里的 gptr_task_timeout
        self.assertEqual(len(submitted), 2)
        for kw in submitted:
            self.assertEqual(kw["timeout"], 600)

        # C3: 乱码被修复为中文
        self.assertIn("中文测试", merged)
        self.assertNotIn(MOJIBAKE_CN, merged)
        self.assertEqual(client.get_status.await_count, 2)


class TestC8CircuitOnClientImportError(unittest.TestCase):
    """C8: 客户端导入失败时应打开熔断。"""

    def setUp(self):
        self.url = "http://gptr-c8-test.invalid:8000/api/v1"
        search._GPTR_UNAVAILABLE_UNTIL.pop(self.url, None)

    def tearDown(self):
        search._GPTR_UNAVAILABLE_UNTIL.pop(self.url, None)

    def test_import_error_trips_circuit(self):
        adapter = _make_adapter({"gptr_service_url": self.url})
        self.assertFalse(adapter._circuit_open())

        with mock.patch.object(adapter, "_get_gptr_client",
                               side_effect=ImportError("gptr_service.client 缺失")):
            # 生产签名：_research_round(query) 只有一个参数
            with self.assertRaises(ImportError):
                asyncio.run(adapter._research_round("测试查询"))

        self.assertTrue(adapter._circuit_open())


class TestC7OllamaBatchFailure(unittest.TestCase):
    """C7: Ollama 不可用时只尝试第一批（一次批量 + 一次单条）。"""

    class _TimeoutRequests:
        def __init__(self):
            self.calls = []

        def post(self, url, **kwargs):
            self.calls.append(url)
            raise TimeoutError("ollama timeout")

    def test_batch_failure_stops_after_first_batch(self):
        fake = self._TimeoutRequests()
        clusterer = MaterialClusterer(embed_batch_size=8)
        clusterer._requests = fake  # 注入替身，_ensure_requests 不会导入真实 requests

        vectors = clusterer.embed([f"文本{i}" for i in range(30)])

        self.assertEqual(len(fake.calls), 2)
        self.assertTrue(fake.calls[0].endswith("/api/embed"))
        self.assertTrue(fake.calls[1].endswith("/api/embeddings"))
        self.assertEqual(vectors.shape, (0, 0))

    def test_batch_success_no_single_fallback(self):
        class _Resp:
            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                pass

            def json(self):
                return self._payload

        class _OkRequests:
            def __init__(self):
                self.calls = []

            def post(self, url, **kwargs):
                self.calls.append(url)
                return _Resp({"embeddings": [[0.1, 0.2, 0.3, 0.4] for _ in range(8)]})

        fake = _OkRequests()
        clusterer = MaterialClusterer(embed_batch_size=8)
        clusterer._requests = fake

        vectors = clusterer.embed([f"文本{i}" for i in range(30)])

        self.assertEqual(vectors.shape, (30, 4))
        self.assertEqual(len(fake.calls), 4)  # 30 条 / 每批 8 条
        self.assertTrue(all(u.endswith("/api/embed") for u in fake.calls))


class TestPlanConfirmFeedback(unittest.TestCase):
    """计划确认页的修改意见必须进入重新规划，而不是被丢弃。"""

    def test_feedback_regenerates_plan(self):
        gui = MagicMock()
        gui.wait_for_plan.return_value = "增加竞品对比与渠道分析"
        bot = object.__new__(engine.FusionResearchBot)
        bot.gui = gui
        bot.refined_task = "原始调研任务"
        bot.plan_text = "旧计划"
        bot.search_batches = [{"queries": ["旧查询"]}]

        with mock.patch.object(engine.FusionResearchBot, "_step_generate_plan") as gen:
            bot._step_confirm_plan()

        gen.assert_called_once()
        task_arg = gen.call_args.args[0]
        self.assertIn("原始调研任务", task_arg)
        self.assertIn("增加竞品对比与渠道分析", task_arg)

    def test_quit_stops_pipeline(self):
        gui = MagicMock()
        gui.wait_for_plan.return_value = "QUIT"
        bot = object.__new__(engine.FusionResearchBot)
        bot.gui = gui
        bot.refined_task = "原始调研任务"
        bot.plan_text = "旧计划"

        with self.assertRaises(RuntimeError):
            bot._step_confirm_plan()


class TestC4AllInferableGuiConfirm(unittest.TestCase):
    """C4: 全部可推理时，GUI 需展示推断摘要确认，且不重复注入 inferable_context。"""

    def setUp(self):
        _FakeDeductionEngine.captured = {}
        _FakeDeductionEngine.context_text = "【推断】目标市场为中国"

    def test_gui_summary_shown_and_no_double_injection(self):
        gui = MagicMock()
        gui.wait_for_clarify = MagicMock(return_value="")
        bot = _make_bot(gui)
        bot._agent_call = MagicMock(return_value=_questions_json(
            {"question": "目标市场？", "explanation": "e1", "impact": "i1"},
            {"question": "调研范围？", "explanation": "e2", "impact": "i2"},
            {"question": "重点关注？", "explanation": "e3", "impact": "i3"},
        ))

        _FakeDeductionEngine.result = {
            "all_could_infer": True,
            "inferable": [{"original_question": "目标市场？", "reason": "从需求可推断"}],
            "must_ask": [],
        }

        with mock.patch.object(engine, "DeductionEngine", _FakeDeductionEngine):
            result = bot.clarify_questions("测试任务")

        gui.wait_for_clarify.assert_called_once()
        displayed = gui.wait_for_clarify.call_args[0][0]
        self.assertIn("推断", displayed)
        self.assertIn("目标市场", displayed)
        # 推断内容由 generate_plan 统一注入一次，此处不得拼回返回值
        self.assertEqual(result, "测试任务")
        self.assertNotIn("【推断】", result)


class TestC5KeepOriginalExplanationImpact(unittest.TestCase):
    """C5: 推演后的必问项应保留原始 explanation/impact。"""

    def setUp(self):
        _FakeDeductionEngine.captured = {}
        _FakeDeductionEngine.context_text = ""

    def test_original_explanation_impact_preserved(self):
        gui = MagicMock()
        gui.wait_for_clarify = MagicMock(return_value="")
        bot = _make_bot(gui)
        bot._agent_call = MagicMock(return_value=_questions_json(
            {"question": "目标市场？", "explanation": "原始解释EXPL123", "impact": "原始影响IMPACT456"},
            {"question": "调研范围？", "explanation": "e2", "impact": "i2"},
            {"question": "重点关注？", "explanation": "e3", "impact": "i3"},
        ))

        _FakeDeductionEngine.result = {
            "all_could_infer": False,
            "inferable": [],
            "must_ask": [{"question": "目标市场？", "reason": "推演理由", "gain_level": "high"}],
        }

        with mock.patch.object(engine, "DeductionEngine", _FakeDeductionEngine):
            bot.clarify_questions("测试任务")

        displayed = gui.wait_for_clarify.call_args[0][0]
        self.assertIn("原始解释EXPL123", displayed)
        self.assertIn("原始影响IMPACT456", displayed)
        # 有原始 impact 时不得用"影响级别:"替代
        self.assertNotIn("影响级别:", displayed)


class TestC6RetryDedup(unittest.TestCase):
    """C6: 澄清问题重试合并时按 question 文本去重。"""

    def setUp(self):
        _FakeDeductionEngine.captured = {}
        _FakeDeductionEngine.context_text = ""

    def test_retry_questions_deduplicated(self):
        gui = MagicMock()
        gui.wait_for_clarify = MagicMock(return_value="")
        bot = _make_bot(gui)

        first = _questions_json(
            {"question": "问题一", "explanation": "e1", "impact": "i1"},
        )
        second = _questions_json(
            {"question": "问题一", "explanation": "e1", "impact": "i1"},  # 与第一轮重复
            {"question": "问题二", "explanation": "e2", "impact": "i2"},
            {"question": "问题三", "explanation": "e3", "impact": "i3"},
        )
        bot._agent_call = MagicMock(side_effect=[first, second])

        # 生产代码在 must_ask 为空时会把结构化问题整体清空，无法观察去重结果；
        # 因此让替身引擎原样回传候选问题（等价于"全部必问"），以验证重试去重。
        class _EchoEngine(_FakeDeductionEngine):
            def filter_questions(self, task, candidate_questions, max_keep=5):
                _FakeDeductionEngine.captured["candidates"] = list(candidate_questions)
                return {
                    "all_could_infer": False,
                    "inferable": [],
                    "must_ask": [{"question": q["question"]} for q in candidate_questions],
                }

        with mock.patch.object(engine, "DeductionEngine", _EchoEngine):
            bot.clarify_questions("测试任务")

        self.assertEqual(bot._agent_call.call_count, 2)

        candidates = _FakeDeductionEngine.captured["candidates"]
        self.assertEqual([q["question"] for q in candidates], ["问题一", "问题二", "问题三"])

        displayed = gui.wait_for_clarify.call_args[0][0]
        self.assertEqual(displayed.count("问题一"), 1)
        self.assertEqual(displayed.count("问题二"), 1)
        self.assertEqual(displayed.count("问题三"), 1)


class TestChartSandboxValidation(unittest.TestCase):
    """图表生成代码的能力白名单校验。"""

    def test_safe_chart_code_passes(self):
        from deliverables import _validate_python_code

        code = (
            "import matplotlib.pyplot as plt\n"
            "import numpy as np\n"
            "fig, ax = plt.subplots()\n"
            "ax.plot(np.arange(3))\n"
            "fig.savefig(r'C:\\tmp\\chart.png')\n"
        )
        ok, err = _validate_python_code(code)
        self.assertTrue(ok, err)

    def test_dangerous_capabilities_rejected(self):
        from deliverables import _validate_python_code

        cases = [
            "import os\nos.system('calc')\n",
            "open(r'C:\\Windows\\win.ini').read()\n",
            "exec('print(1)')\n",
            "import subprocess\n",
            "x = (1).__class__\n",
        ]
        for code in cases:
            ok, err = _validate_python_code(code)
            self.assertFalse(ok, code)
            self.assertTrue(err, code)


class TestDeductionPromptFormat(unittest.TestCase):
    """真实推演提示词必须可格式化，避免 {N} 占位符导致 KeyError。"""

    def test_real_prompt_formats_without_keyerror(self):
        from shared.deduction_engine import DEDUCTION_PROMPT

        rendered = DEDUCTION_PROMPT.format(
            task="测试任务",
            total_count=2,
            candidate_questions="1. 问题一",
        )
        self.assertIn("测试任务", rendered)
        self.assertIn("{N}", rendered)


if __name__ == "__main__":
    unittest.main(verbosity=2)
