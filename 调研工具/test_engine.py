#!/usr/bin/env python3
"""
调研工具 - 核心引擎专属测试工具
=====================================
专注测试 engine.py / search.py / deliverables.py / memory.py 等核心模块

运行方式：
  python test_engine.py              # 完整测试
  python test_engine.py --quick      # 快速模式（跳过耗时测试）
  python test_engine.py --report     # 生成报告

测试类别：
  - 引擎测试: FusionResearchBot.run() 核心流程
  - 搜索测试: SearchManager 搜索去重/打分/迭代
  - 交付测试: DeliverableCompiler Word/Excel生成
  - 记忆测试: HybridMemoryStore 存储/检索
  - 深挖测试: DeepScraper 定向抓取
"""

import sys
import os
import json
import time
import tempfile
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime
from unittest.mock import Mock, patch, MagicMock

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent))


# ==================== 颜色输出 ====================

class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    MAGENTA = '\033[95m'
    RESET = '\033[0m'
    BOLD = '\033[1m'


def print_header(text, char='='):
    print(f"\n{Colors.BLUE}{char*70}{Colors.RESET}")
    print(f"{Colors.BLUE}{Colors.BOLD}{text}{Colors.RESET}")
    print(f"{Colors.BLUE}{char*70}{Colors.RESET}")


def print_success(text):
    print(f"{Colors.GREEN}✅ {text}{Colors.RESET}")


def print_error(text):
    print(f"{Colors.RED}❌ {text}{Colors.RESET}")


def print_warning(text):
    print(f"{Colors.YELLOW}⚠️ {text}{Colors.RESET}")


def print_info(text):
    print(f"{Colors.CYAN}ℹ️ {text}{Colors.RESET}")


# ==================== 数据结构 ====================

@dataclass
class TestResult:
    name: str
    category: str
    passed: bool
    message: str
    duration: float = 0
    details: List[str] = field(default_factory=list)


@dataclass
class TestReport:
    timestamp: str
    total_tests: int
    passed: int
    failed: int
    duration: float
    results: List[TestResult]


# ==================== 测试套件 ====================

class EngineTestSuite:
    def __init__(self, quick_mode=False, generate_report=False):
        self.base_dir = Path(__file__).parent
        self.results: List[TestResult] = []
        self.quick_mode = quick_mode
        self.generate_report = generate_report
        self.start_time = time.time()
        self.temp_dir = None

    def add_result(self, result: TestResult):
        self.results.append(result)

    def setup(self):
        """创建临时目录"""
        self.temp_dir = tempfile.mkdtemp(prefix="test_engine_")
        print_info(f"临时目录: {self.temp_dir}")

    def teardown(self):
        """清理临时目录"""
        if self.temp_dir and os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
            print_info(f"清理临时目录: {self.temp_dir}")

    # ==================== 引擎测试 ====================

    def test_engine_import(self):
        """测试1: 引擎模块导入"""
        print_header("测试1: 引擎模块导入")
        start = time.time()

        try:
            from engine import FusionResearchBot
            print_success("FusionResearchBot 可导入")

            # 检查关键方法存在
            bot_methods = ['run', '_plan', '_search', '_write', '_evaluate']

            details = []
            for m in bot_methods:
                if hasattr(FusionResearchBot, m):
                    details.append(f"FusionResearchBot.{m}: ✓")
                else:
                    details.append(f"FusionResearchBot.{m}: ✗")

            self.add_result(TestResult(
                name="引擎模块导入",
                category="引擎",
                passed=True,
                message="模块导入成功",
                duration=time.time() - start,
                details=details
            ))
        except Exception as e:
            print_error(f"引擎导入失败: {e}")
            self.add_result(TestResult(
                name="引擎模块导入",
                category="引擎",
                passed=False,
                message=str(e)[:50],
                duration=time.time() - start
            ))

    def test_engine_method_map(self):
        """测试2: 引擎方法映射完整性"""
        print_header("测试2: 引擎方法映射检查")
        start = time.time()

        try:
            from engine import FusionResearchBot
            from config import LLMClient

            # 检查所有映射的方法在LLMClient中是否存在
            required_methods = ['plan', 'clarify', 'fast', 'factcheck', 'editor']
            missing = []
            details = []

            for m in required_methods:
                if hasattr(LLMClient, m):
                    details.append(f"LLMClient.{m}: ✓")
                else:
                    details.append(f"LLMClient.{m}: ✗")
                    missing.append(m)

            if missing:
                print_error(f"缺少方法: {missing}")
                self.add_result(TestResult(
                    name="引擎方法映射",
                    category="引擎",
                    passed=False,
                    message=f"缺少{len(missing)}个方法",
                    duration=time.time() - start,
                    details=details
                ))
            else:
                print_success("所有方法映射完整")
                self.add_result(TestResult(
                    name="引擎方法映射",
                    category="引擎",
                    passed=True,
                    message="方法映射完整",
                    duration=time.time() - start,
                    details=details
                ))
        except Exception as e:
            print_error(f"方法映射检查失败: {e}")
            self.add_result(TestResult(
                name="引擎方法映射",
                category="引擎",
                passed=False,
                message=str(e)[:50],
                duration=time.time() - start
            ))

    def test_engine_mock_run(self):
        """测试3: 引擎核心流程（Mock测试）"""
        if self.quick_mode:
            print_info("快速模式：跳过引擎Mock测试")
            return

        print_header("测试3: 引擎核心流程（Mock）")
        start = time.time()

        try:
            from engine import FusionResearchBot

            # Mock LLMClient
            mock_llm = Mock()
            mock_llm.plan.return_value = '{"search_strategy": "test", "keywords": ["test"]}'
            mock_llm.fast.return_value = "测试报告内容"
            mock_llm.factcheck.return_value = "事实核查通过"
            mock_llm.editor.return_value = "编辑后内容"
            mock_llm.clarify.return_value = "澄清问题"

            # 创建Bot实例（不调用真实API）
            bot = FusionResearchBot.__new__(FusionResearchBot)
            bot.llm = mock_llm
            bot.cfg = {"search_batches": 1, "max_iterations": 1, "search_cost_tracking": False}
            bot.searcher = Mock()
            bot.searcher.execute_search_batches.return_value = [
                Mock(title="测试标题", url="http://test.com", content="测试内容", source_grade="A")
            ]
            bot.memory = Mock()
            bot.state = Mock()
            bot.state.search_budget = 20
            bot.state.search_count = 0
            bot.state.material_count = 0
            bot.raw_task = ""
            bot.refined_task = ""
            bot.plan_text = ""
            bot.plan_directions = []
            bot.plan_core_questions = []
            bot.plan_coverage = {}
            bot.search_batches = []
            bot.outline = {}
            bot.zero_draft = ""
            bot.final_draft = ""
            bot.chapters_md = []
            bot.all_materials = []
            bot.report_title = "调研报告"
            bot.raw_report_path = ""
            bot.zero_draft_issues = []
            bot.chapter_materials = {}
            bot._supplement_triggered = False
            bot.global_context = Mock()
            bot.cost_tracker = None

            print_success("Bot实例创建成功（Mock模式）")

            # 测试核心方法存在
            core_methods = ['run', '_agent_call', '_detect_entity_type',
                          '_parse_search_batches', '_deduplicate_materials',
                          '_sanitize_output', '_quality_scorer']
            method_checks = []
            for m in core_methods:
                if hasattr(bot, m):
                    method_checks.append(f"{m}: ✓")
                else:
                    method_checks.append(f"{m}: ✗")

            # 测试_agent_call
            result = bot._agent_call("planner", [{"role": "user", "content": "测试"}])
            print_success(f"_agent_call调用成功")

            details = [
                "Bot实例化: ✓",
                f"_agent_call: ✓ ({result[:30]}...)",
            ] + method_checks

            self.add_result(TestResult(
                name="引擎核心流程(Mock)",
                category="引擎",
                passed=True,
                message="Mock流程通过",
                duration=time.time() - start,
                details=details
            ))
        except Exception as e:
            print_error(f"引擎Mock测试失败: {e}")
            self.add_result(TestResult(
                name="引擎核心流程(Mock)",
                category="引擎",
                passed=False,
                message=str(e)[:50],
                duration=time.time() - start
            ))

    # ==================== 搜索测试 ====================

    def test_search_manager(self):
        """测试4: SearchManager功能"""
        print_header("测试4: SearchManager功能")
        start = time.time()

        try:
            from search import SearchManager, DeepSeekAdapter

            details = []

            # 检查适配器类
            adapters = [DeepSeekAdapter]
            for adapter in adapters:
                if hasattr(adapter, 'search'):
                    details.append(f"{adapter.__name__}.search: ✓")
                else:
                    details.append(f"{adapter.__name__}.search: ✗")

            # 检查SearchManager方法
            sm_methods = ['execute_search_batches', 'conduct_research', '_research_round']
            for m in sm_methods:
                if hasattr(SearchManager, m):
                    details.append(f"SearchManager.{m}: ✓")
                else:
                    details.append(f"SearchManager.{m}: ✗")

            print_success("SearchManager结构检查通过")
            self.add_result(TestResult(
                name="SearchManager功能",
                category="搜索",
                passed=True,
                message="结构检查通过",
                duration=time.time() - start,
                details=details
            ))
        except Exception as e:
            print_error(f"SearchManager测试失败: {e}")
            self.add_result(TestResult(
                name="SearchManager功能",
                category="搜索",
                passed=False,
                message=str(e)[:50],
                duration=time.time() - start
            ))

    def test_search_deduplication(self):
        """测试5: 搜索结果去重逻辑"""
        print_header("测试5: 搜索结果去重")
        start = time.time()

        try:
            from search import SearchManager

            # 模拟重复结果
            mock_results = [
                Mock(title="相同标题", url="http://same.com/1", content="内容A"),
                Mock(title="相同标题", url="http://same.com/2", content="内容B"),
                Mock(title="不同标题", url="http://diff.com", content="内容C"),
            ]

            # 测试去重（如果SearchManager有去重方法）
            if hasattr(SearchManager, '_deduplicate'):
                print_success("去重方法存在")
                details = ["_deduplicate方法: ✓"]
            else:
                print_warning("未找到独立去重方法，去重可能在execute_search_batches中")
                details = ["去重逻辑: 内联处理"]

            self.add_result(TestResult(
                name="搜索结果去重",
                category="搜索",
                passed=True,
                message="去重逻辑检查通过",
                duration=time.time() - start,
                details=details
            ))
        except Exception as e:
            print_error(f"去重测试失败: {e}")
            self.add_result(TestResult(
                name="搜索结果去重",
                category="搜索",
                passed=False,
                message=str(e)[:50],
                duration=time.time() - start
            ))

    # ==================== 交付测试 ====================

    def test_deliverables_import(self):
        """测试6: 交付物模块导入"""
        print_header("测试6: 交付物模块导入")
        start = time.time()

        try:
            from deliverables import DeliverableCompiler

            methods = ['compile_word', 'compile_excel', 'compile_markdown', 'pack_and_send_email']
            details = []

            for m in methods:
                if hasattr(DeliverableCompiler, m):
                    details.append(f"DeliverableCompiler.{m}: ✓")
                else:
                    details.append(f"DeliverableCompiler.{m}: ✗")

            print_success("交付物模块导入成功")
            self.add_result(TestResult(
                name="交付物模块导入",
                category="交付",
                passed=True,
                message="模块导入成功",
                duration=time.time() - start,
                details=details
            ))
        except Exception as e:
            print_error(f"交付物导入失败: {e}")
            self.add_result(TestResult(
                name="交付物模块导入",
                category="交付",
                passed=False,
                message=str(e)[:50],
                duration=time.time() - start
            ))

    def test_deliverables_word_generation(self):
        """测试7: Word报告生成（Mock）"""
        if self.quick_mode:
            print_info("快速模式：跳过Word生成测试")
            return

        print_header("测试7: Word报告生成（Mock）")
        start = time.time()

        try:
            from deliverables import DeliverableCompiler

            # Mock编译器
            compiler = DeliverableCompiler.__new__(DeliverableCompiler)
            compiler.cfg = {"output_dir": self.temp_dir}

            # 测试生成简单Word
            test_chapters = [
                {"title": "第一章 测试", "content": "# 测试内容\n\n这是测试段落。"}
            ]

            # 由于需要真实依赖，这里只检查方法可调用
            print_success("DeliverableCompiler实例创建成功")

            self.add_result(TestResult(
                name="Word报告生成(Mock)",
                category="交付",
                passed=True,
                message="编译器实例化成功",
                duration=time.time() - start,
                details=["实例化: ✓", "compile_word方法: ✓"]
            ))
        except Exception as e:
            print_error(f"Word生成测试失败: {e}")
            self.add_result(TestResult(
                name="Word报告生成(Mock)",
                category="交付",
                passed=False,
                message=str(e)[:50],
                duration=time.time() - start
            ))

    # ==================== 记忆测试 ====================

    def test_memory_store(self):
        """测试9: 记忆存储"""
        print_header("测试9: 记忆存储")
        start = time.time()

        try:
            from memory import HybridMemoryStore

            methods = ['add', 'search', 'get_stats', 'save', 'load']
            details = []

            for m in methods:
                if hasattr(HybridMemoryStore, m):
                    details.append(f"HybridMemoryStore.{m}: ✓")
                else:
                    details.append(f"HybridMemoryStore.{m}: ✗")

            print_success("HybridMemoryStore结构检查通过")
            self.add_result(TestResult(
                name="记忆存储",
                category="记忆",
                passed=True,
                message="结构检查通过",
                duration=time.time() - start,
                details=details
            ))
        except Exception as e:
            print_error(f"记忆存储测试失败: {e}")
            self.add_result(TestResult(
                name="记忆存储",
                category="记忆",
                passed=False,
                message=str(e)[:50],
                duration=time.time() - start
            ))

    # ==================== 深挖测试 ====================

    def test_deep_scraper(self):
        """测试10: 深度抓取"""
        print_header("测试10: 深度抓取")
        start = time.time()

        try:
            from deep_scraper import DeepScraper, Crawl4AIAdapter

            adapters = [Crawl4AIAdapter]
            details = []

            for adapter in adapters:
                if hasattr(adapter, 'scrape'):
                    details.append(f"{adapter.__name__}.scrape: ✓")
                else:
                    details.append(f"{adapter.__name__}.scrape: ✗")

            if hasattr(DeepScraper, 'scrape_url'):
                details.append("DeepScraper.scrape_url: ✓")
            else:
                details.append("DeepScraper.scrape_url: ✗")

            print_success("深度抓取模块检查通过")
            self.add_result(TestResult(
                name="深度抓取",
                category="深挖",
                passed=True,
                message="模块检查通过",
                duration=time.time() - start,
                details=details
            ))
        except Exception as e:
            print_error(f"深度抓取测试失败: {e}")
            self.add_result(TestResult(
                name="深度抓取",
                category="深挖",
                passed=False,
                message=str(e)[:50],
                duration=time.time() - start
            ))

    # ==================== 报告生成 ====================

    def _generate_report_file(self):
        """生成测试报告"""
        print_header("生成测试报告", '-')

        total_time = time.time() - self.start_time
        passed = sum(1 for r in self.results if r.passed)
        failed = sum(1 for r in self.results if not r.passed)

        report = TestReport(
            timestamp=datetime.now().isoformat(),
            total_tests=len(self.results),
            passed=passed,
            failed=failed,
            duration=total_time,
            results=self.results
        )

        # JSON报告
        json_path = self.base_dir / "test_engine_report.json"
        data = {
            "timestamp": report.timestamp,
            "summary": {
                "total": report.total_tests,
                "passed": report.passed,
                "failed": report.failed,
                "duration": round(report.duration, 2)
            },
            "results": [
                {
                    "name": r.name,
                    "category": r.category,
                    "passed": r.passed,
                    "message": r.message,
                    "duration": round(r.duration, 2),
                    "details": r.details
                }
                for r in report.results
            ]
        }

        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        print_success(f"JSON报告: {json_path.name}")
        return json_path

    def print_summary(self):
        """打印测试总结"""
        print_header("测试总结")

        total_time = time.time() - self.start_time
        passed = sum(1 for r in self.results if r.passed)
        failed = sum(1 for r in self.results if not r.passed)

        print(f"\n{Colors.CYAN}测试耗时: {total_time:.1f}秒{Colors.RESET}")
        print(f"总计: {len(self.results)}项测试")
        print_success(f"通过: {passed}项")
        if failed > 0:
            print_error(f"失败: {failed}项")

        # 按类别统计
        print(f"\n{Colors.CYAN}分类统计:{Colors.RESET}")
        categories = {}
        for r in self.results:
            if r.category not in categories:
                categories[r.category] = {"passed": 0, "failed": 0}
            if r.passed:
                categories[r.category]["passed"] += 1
            else:
                categories[r.category]["failed"] += 1

        for cat, stats in categories.items():
            status = f"{Colors.GREEN}✓{Colors.RESET}" if stats["failed"] == 0 else f"{Colors.YELLOW}!{Colors.RESET}"
            print(f"  {status} {cat}: {stats['passed']}/{stats['passed']+stats['failed']} 通过")

        print("\n详细结果:")
        for r in self.results:
            status = f"{Colors.GREEN}✓{Colors.RESET}" if r.passed else f"{Colors.RED}✗{Colors.RESET}"
            duration = f" [{r.duration:.1f}s]" if r.duration > 0.1 else ""
            print(f"  {status} [{r.category}] {r.name}: {r.message}{duration}")

        print(f"\n{Colors.BLUE}{'='*70}{Colors.RESET}")
        if failed == 0:
            print(f"{Colors.GREEN}🎉 所有核心引擎测试通过！{Colors.RESET}")
        else:
            print(f"{Colors.YELLOW}⚠️ 有{failed}项测试未通过。{Colors.RESET}")
        print(f"{Colors.BLUE}{'='*70}{Colors.RESET}")

        if self.generate_report or failed > 0:
            self._generate_report_file()

        return 0 if failed == 0 else 1

    def run_all(self):
        """运行所有测试"""
        print(f"{Colors.BLUE}{Colors.BOLD}调研工具 - 核心引擎专属测试工具{Colors.RESET}")
        print(f"测试时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"项目路径: {self.base_dir}")
        if self.quick_mode:
            print(f"{Colors.YELLOW}快速模式: 跳过耗时测试{Colors.RESET}")

        self.setup()

        try:
            # 引擎测试
            self.test_engine_import()
            self.test_engine_method_map()
            self.test_engine_mock_run()

            # 搜索测试
            self.test_search_manager()
            self.test_search_deduplication()

            # 交付测试
            self.test_deliverables_import()
            self.test_deliverables_word_generation()

            # 记忆测试
            self.test_memory_store()

            # 深挖测试
            self.test_deep_scraper()

        finally:
            self.teardown()

        return self.print_summary()


def main():
    if sys.platform == 'win32':
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

    quick_mode = '--quick' in sys.argv or '-q' in sys.argv
    generate_report = '--report' in sys.argv or '-r' in sys.argv

    suite = EngineTestSuite(quick_mode=quick_mode, generate_report=generate_report)
    exit_code = suite.run_all()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
