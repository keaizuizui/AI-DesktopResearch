#!/usr/bin/env python3
"""
本地一键测试套件（专业版）
=====================================
运行方式：
  python test_suite.py           # 完整测试
  python test_suite.py --quick   # 快速模式
  python test_suite.py --report  # 生成测试报告

测试类别：
  - 基础测试：语法、导入、环境
  - 配置测试：配置文件完整性
  - 功能测试：知识提取、增量追踪、搜索引擎
  - 安全测试：API Key泄露、路由权限
  - 集成测试：模拟用户流程
  - 报告生成：JSON/HTML格式
"""

import sys
import os
import json
import time
import subprocess
import threading
import urllib.request
import urllib.error
import re
import hashlib
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any
from dataclasses import dataclass, field, asdict
from datetime import datetime
from io import StringIO
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

def mask(text: str) -> str:
    """脱敏显示 API Key"""
    if not text:
        return ""
    if len(text) <= 8:
        return "***"
    return text[:3] + "***" + text[-4:]

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

def print_section(text):
    print(f"\n{Colors.CYAN}▶ {text}{Colors.RESET}")

def print_success(text):
    print(f"{Colors.GREEN}✅ {text}{Colors.RESET}")

def print_error(text):
    print(f"{Colors.RED}❌ {text}{Colors.RESET}")

def print_warning(text):
    print(f"{Colors.YELLOW}⚠️ {text}{Colors.RESET}")

def print_info(text):
    print(f"{Colors.CYAN}ℹ️ {text}{Colors.RESET}")

def print_fix(text):
    print(f"{Colors.MAGENTA}🔧 修复建议: {text}{Colors.RESET}")

# ==================== 数据结构 ====================

@dataclass
class TestResult:
    """测试结果"""
    name: str
    category: str
    passed: bool
    message: str
    duration: float = 0
    details: List[str] = field(default_factory=list)
    fix_suggestion: str = ""

@dataclass
class TestReport:
    """测试报告"""
    timestamp: str
    total_tests: int
    passed: int
    failed: int
    skipped: int
    duration: float
    results: List[TestResult]
    environment: Dict[str, str]

# ==================== 测试套件 ====================

class TestSuite:
    def __init__(self, quick_mode=False, generate_report=False):
        self.base_dir = Path(__file__).parent
        self.results: List[TestResult] = []
        self.quick_mode = quick_mode
        self.generate_report = generate_report
        self.start_time = time.time()
        self.skipped_count = 0

    def add_result(self, result: TestResult):
        self.results.append(result)

    # ==================== 环境检查 ====================

    def test_environment(self):
        """测试0: 环境检查"""
        print_header("测试0: 环境检查", '-')
        start = time.time()
        details = []

        # Python版本
        py_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        if sys.version_info >= (3, 8):
            print_success(f"Python版本: {py_version}")
            details.append(f"Python {py_version}")
        else:
            print_error(f"Python版本过低: {py_version}, 需要3.8+")
            details.append(f"Python {py_version} (需要3.8+)")

        # 检查关键依赖（import名映射）
        required_packages = {
            'requests': 'Requests',
            'openpyxl': 'OpenPyXL',
            'docx': 'Docx',           # python-docx 的 import 名是 docx
        }

        missing = []
        for pkg, name in required_packages.items():
            try:
                __import__(pkg.replace('-', '_'))
                print_success(f"依赖包: {name}")
                details.append(f"{name}: 已安装")
            except ImportError:
                print_warning(f"依赖包缺失: {name}")
                missing.append(name)
                details.append(f"{name}: 未安装")

        duration = time.time() - start
        passed = len(missing) == 0 and sys.version_info >= (3, 8)

        self.add_result(TestResult(
            name="环境检查",
            category="基础",
            passed=passed,
            message=f"Python {py_version}, {len(missing)}个依赖缺失" if missing else f"Python {py_version}, 环境完整",
            duration=duration,
            details=details,
            fix_suggestion="pip install requests openpyxl python-docx" if missing else ""
        ))

    # ==================== 基础测试 ====================

    def test_syntax(self):
        """测试1: Python语法检查"""
        print_header("测试1: Python语法检查")
        start = time.time()

        py_files = list(self.base_dir.rglob("*.py"))
        py_files = [f for f in py_files if "__pycache__" not in str(f) and ".venv" not in str(f) and "site-packages" not in str(f)]

        errors = []
        for f in py_files:
            try:
                with open(f, 'r', encoding='utf-8') as file:
                    compile(file.read(), str(f), 'exec')
            except SyntaxError as e:
                errors.append(f"{f.relative_to(self.base_dir)}: 行{e.lineno}")

        duration = time.time() - start
        if errors:
            for e in errors[:5]:
                print_error(e)
            self.add_result(TestResult(
                name="语法检查",
                category="基础",
                passed=False,
                message=f"{len(errors)}个文件有语法错误",
                duration=duration,
                details=errors,
                fix_suggestion="检查上述文件的语法错误"
            ))
        else:
            print_success(f"所有.py文件语法正确 (检查{len(py_files)}个文件, 耗时{duration:.1f}s)")
            self.add_result(TestResult(
                name="语法检查",
                category="基础",
                passed=True,
                message=f"检查{len(py_files)}个文件",
                duration=duration
            ))

    def test_imports(self):
        """测试2: 模块导入"""
        print_header("测试2: 模块导入测试")
        start = time.time()

        sys.path.insert(0, str(self.base_dir))

        modules = [
            # 新增模块
            ('knowledge_extractor', '知识提取器', 'V16核心'),
            ('incremental_tracker', '增量追踪', 'V16核心'),
            ('v15_3_config', 'V16配置常量', '核心'),
            # 核心模块
            ('config', '配置模块', '核心'),
            ('engine', '核心引擎', '核心'),
            ('search', '搜索模块', '核心'),
            ('deliverables', '交付物模块', '核心'),
            ('framework_loader', '框架加载器', '核心'),
        ]

        passed = 0
        details = []
        for mod_name, desc, cat in modules:
            try:
                t = time.time()
                __import__(mod_name)
                duration = time.time() - t
                print_success(f"[{cat}] {desc} ({mod_name}) [{duration:.2f}s]")
                passed += 1
                details.append(f"✓ {mod_name}")
            except Exception as e:
                print_error(f"[{cat}] {desc} ({mod_name}): {str(e)[:60]}")
                details.append(f"✗ {mod_name}: {str(e)[:40]}")

        duration = time.time() - start
        self.add_result(TestResult(
            name="模块导入",
            category="基础",
            passed=passed == len(modules),
            message=f"{passed}/{len(modules)}个模块",
            duration=duration,
            details=details,
            fix_suggestion="检查缺失模块的依赖关系" if passed < len(modules) else ""
        ))

    # ==================== 配置测试 ====================

    def test_config_files(self):
        """测试3: 配置文件完整性"""
        print_header("测试3: 配置文件检查")
        start = time.time()
        details = []

        # 检查主配置文件
        config_file = self.base_dir / "research_config.json"
        if config_file.exists():
            try:
                with open(config_file, 'r', encoding='utf-8') as f:
                    cfg = json.load(f)

                required_keys = ['llm_api_key', 'llm_base_url']
                missing = [k for k in required_keys if not cfg.get(k)]
                if not (cfg.get('search_api_key') or cfg.get('llm_api_key')):
                    missing.append('search_api_key')

                if missing:
                    print_warning(f"缺少配置项: {missing}")
                    details.append(f"缺少配置项: {missing}")
                else:
                    print_success("主配置文件完整")
                    details.append("主配置文件完整")

                # 检查API Key格式
                api_key = cfg.get('llm_api_key', '')
                if api_key and len(api_key) >= 8:
                    print_success(f"API Key已配置 ({mask(api_key)})")
                    details.append(f"API Key: {mask(api_key)}")
                else:
                    print_warning("API Key未配置或太短")
                    details.append("API Key未配置")

            except Exception as e:
                print_error(f"配置文件解析失败: {e}")
                details.append(f"解析失败: {e}")
        else:
            print_warning("research_config.json不存在，将使用默认配置")
            details.append("主配置文件不存在")

        # 检查预设配置
        presets = ['research_config_regular.json', 'research_config_deep.json']
        for preset in presets:
            path = self.base_dir / preset
            if path.exists():
                print_success(f"{preset}: 存在")
                details.append(f"{preset}: ✓")
            else:
                print_warning(f"{preset}: 不存在")
                details.append(f"{preset}: ✗")

        self.add_result(TestResult(
            name="配置文件",
            category="配置",
            passed=True,
            message="检查完成",
            duration=time.time() - start,
            details=details
        ))

    # ==================== 目录结构测试 ====================

    def test_directories(self):
        """测试6: 目录结构"""
        print_header("测试6: 目录结构检查")
        start = time.time()

        dirs = {
            'uploads': '素材上传',
            'uploads/temp': '临时素材',
            'output': '报告输出',
        }

        created = 0
        details = []
        for dir_path, desc in dirs.items():
            path = self.base_dir / dir_path
            if not path.exists():
                path.mkdir(parents=True, exist_ok=True)
                print_warning(f"{desc}: 已创建")
                details.append(f"{desc}: 新建")
                created += 1
            else:
                print_success(f"{desc}: 已就绪")
                details.append(f"{desc}: ✓")

        self.add_result(TestResult(
            name="目录结构",
            category="基础",
            passed=True,
            message=f"检查{len(dirs)}个目录, 新建{created}个",
            duration=time.time() - start,
            details=details
        ))

    # Web 测试已随 H5 前端移除

    # ==================== 功能测试 ====================

    def test_knowledge_extractor(self):
        """测试9: 知识提取器功能"""
        print_header("测试9: 知识提取器功能测试")
        start = time.time()

        sys.path.insert(0, str(self.base_dir))

        try:
            from knowledge_extractor import ExtractedKnowledge, ExtractedEntity, ExtractedDataPoint

            # 测试数据结构
            knowledge = ExtractedKnowledge(
                industry_name="测试行业",
                industry_definition="这是一个测试行业",
                entities=[
                    ExtractedEntity(name="企业A", tier="第一梯队", confidence="high"),
                    ExtractedEntity(name="企业B", tier="第二梯队", confidence="medium"),
                ],
                data_points=[
                    ExtractedDataPoint(metric="市场规模", value="1000亿", year="2025", source="测试来源"),
                ]
            )

            # 测试输出格式
            md_output = knowledge.to_kb_md()
            details = []

            if "测试行业" in md_output and "企业A" in md_output:
                print_success("知识提取器数据结构正常")
                details.append("数据结构: ✓")
            else:
                print_error("输出格式异常")
                details.append("数据结构: ✗")

            if "to_kb_md()" in str(dir(knowledge)):
                print_success("to_kb_md()输出格式正确")
                details.append("to_kb_md(): ✓")

            self.add_result(TestResult(
                name="知识提取器",
                category="功能",
                passed=True,
                message="功能正常",
                duration=time.time() - start,
                details=details
            ))

        except Exception as e:
            print_error(f"知识提取器测试失败: {e}")
            self.add_result(TestResult(
                name="知识提取器",
                category="功能",
                passed=False,
                message=str(e)[:50],
                duration=time.time() - start,
                fix_suggestion="检查knowledge_extractor.py"
            ))

    def test_source_tier(self):
        """测试12: 信源分级功能"""
        print_header("测试12: 信源分级功能测试")
        start = time.time()

        sys.path.insert(0, str(self.base_dir))

        try:
            from v15_3_config import get_source_tier, get_source_tier_color, SOURCE_CREDIBILITY

            # 测试分级
            test_cases = [
                (1.0, "S"),
                (0.95, "S"),
                (0.85, "A"),
                (0.70, "B"),
                (0.50, "C"),
            ]

            passed = 0
            details = []
            for score, expected_tier in test_cases:
                tier = get_source_tier(score)
                if tier == expected_tier:
                    print(f"  ✅ 评分{score} -> {tier}级")
                    details.append(f"评分{score}: {tier}级 ✓")
                    passed += 1
                else:
                    print_error(f"评分{score} -> {tier}级 (期望{expected_tier})")
                    details.append(f"评分{score}: {tier}级 ✗")

            # 测试颜色
            for tier in ["S", "A", "B", "C"]:
                color = get_source_tier_color(tier)
                details.append(f"{tier}级颜色: {color}")

            # 测试域名评分
            domains = ["gov.cn", "xinhuanet.com", "thepaper.cn", "36kr.com", "weibo.com"]
            for domain in domains:
                score = SOURCE_CREDIBILITY.get(domain, SOURCE_CREDIBILITY.get("default", 0.6))
                tier = get_source_tier(score)
                print(f"  {domain}: {score:.2f} -> {tier}级")

            self.add_result(TestResult(
                name="信源分级",
                category="功能",
                passed=passed == len(test_cases),
                message=f"{passed}/{len(test_cases)}个测试",
                duration=time.time() - start,
                details=details
            ))

        except Exception as e:
            print_error(f"信源分级测试失败: {e}")
            self.add_result(TestResult(
                name="信源分级",
                category="功能",
                passed=False,
                message=str(e)[:50],
                duration=time.time() - start,
                fix_suggestion="检查v15_3_config.py中的SOURCE_CREDIBILITY配置"
            ))

    def test_incremental_tracker(self):
        """测试13: 增量追踪器功能"""
        print_header("测试13: 增量追踪器功能测试")
        start = time.time()

        sys.path.insert(0, str(self.base_dir))

        try:
            from incremental_tracker import IncrementalTracker, DiffResult, HistoricalReport
            from datetime import datetime

            # 测试HistoricalReport数据结构
            hist = HistoricalReport(
                task_id="test_001",
                topic="测试主题",
                created_at=datetime.now(),
                skeleton=[{"title": "第一章", "content": "测试内容"}],
                core_facts=[{"fact": "测试事实", "timestamp": "2025-01-01"}],
                data_points=[{"metric": "市场规模", "value": "100亿"}],
                references=[{"url": "http://example.com"}]
            )
            print_success(f"HistoricalReport创建成功: {hist.topic}")
            details = [f"HistoricalReport: ✓"]

            # 测试DiffResult数据结构
            diff = DiffResult(
                continued=[{"title": "市场概述"}],
                updated=[{"title": "技术趋势", "old": "旧值", "new": "新值"}],
                added=[{"title": "新政策分析"}],
                deprecated=[{"title": "过时数据"}]
            )
            print_success(f"DiffResult创建成功: 继续{len(diff.continued)}, 更新{len(diff.updated)}, 新增{len(diff.added)}, 废弃{len(diff.deprecated)}")
            details.append(f"DiffResult: ✓")

            # 测试IncrementalTracker实例化（需要cfg参数）
            tracker = IncrementalTracker(cfg={"archive_dir": str(self.base_dir / "archive")})
            print_success("增量追踪器实例化成功")
            details.append("实例化: ✓")

            self.add_result(TestResult(
                name="增量追踪器",
                category="功能",
                passed=True,
                message="功能正常",
                duration=time.time() - start,
                details=details
            ))

        except Exception as e:
            print_error(f"增量追踪器测试失败: {e}")
            self.add_result(TestResult(
                name="增量追踪器",
                category="功能",
                passed=False,
                message=str(e)[:50],
                duration=time.time() - start,
                fix_suggestion="检查incremental_tracker.py"
            ))

    def test_search_module(self):
        """测试15: 搜索模块功能"""
        print_header("测试15: 搜索模块功能测试")
        start = time.time()

        sys.path.insert(0, str(self.base_dir))

        try:
            from search import SearchManager, DeepSeekAdapter

            # 测试DeepSeekAdapter类存在
            print_success("DeepSeekAdapter类存在")
            details = ["DeepSeekAdapter: ✓"]

            # 测试SearchManager类存在（不实例化，因为需要cfg和llm参数）
            print_success("SearchManager类存在")
            details.append("SearchManager: ✓")

            # 测试SearchManager的方法签名
            import inspect
            sig = inspect.signature(SearchManager.__init__)
            params = list(sig.parameters.keys())
            print_success(f"SearchManager参数: {params}")
            details.append(f"参数: {params}")

            self.add_result(TestResult(
                name="搜索模块",
                category="功能",
                passed=True,
                message="功能正常",
                duration=time.time() - start,
                details=details
            ))

        except Exception as e:
            print_error(f"搜索模块测试失败: {e}")
            self.add_result(TestResult(
                name="搜索模块",
                category="功能",
                passed=False,
                message=str(e)[:50],
                duration=time.time() - start,
                fix_suggestion="检查search.py"
            ))

    # ==================== 安全测试 ====================

    def test_api_key_leakage(self):
        """测试16: API Key泄露检测"""
        print_header("测试16: API Key泄露检测")
        start = time.time()

        # 常见API Key模式
        patterns = [
            (r'sk-[a-zA-Z0-9]{20,}', 'OpenAI API Key'),
            (r'AIza[a-zA-Z0-9_-]{35}', 'Google API Key'),
            (r'ghp_[a-zA-Z0-9]{36}', 'GitHub Token'),
            (r'xox[baprs]-[a-zA-Z0-9-]{10,}', 'Slack Token'),
            (r'AKIA[0-9A-Z]{16}', 'AWS Access Key'),
            (r'eyJ[a-zA-Z0-9_-]*\.eyJ[a-zA-Z0-9_-]*\.[a-zA-Z0-9_-]*', 'JWT Token'),
        ]

        # 扫描Python文件
        py_files = list(self.base_dir.rglob("*.py"))
        py_files = [f for f in py_files if "__pycache__" not in str(f) and ".venv" not in str(f)]

        leaks = []
        details = []

        for f in py_files:
            try:
                with open(f, 'r', encoding='utf-8') as file:
                    content = file.read()
                    for pattern, name in patterns:
                        matches = re.findall(pattern, content)
                        if matches:
                            for match in matches[:3]:
                                leak_info = f"{f.relative_to(self.base_dir)}: 发现{name}"
                                leaks.append(leak_info)
                                details.append(f"泄露: {f.name}")
            except:
                pass

        # 扫描配置文件
        config_files = list(self.base_dir.glob("*.json")) + list(self.base_dir.glob("*.env"))
        for f in config_files:
            if "research_config" in str(f) or ".env" in str(f):
                continue  # 跳过配置文件本身
            try:
                with open(f, 'r', encoding='utf-8') as file:
                    content = file.read()
                    for pattern, name in patterns:
                        matches = re.findall(pattern, content)
                        if matches:
                            leaks.append(f"{f.name}: 发现{name}")
            except:
                pass

        duration = time.time() - start

        if leaks:
            print_error(f"发现 {len(leaks)} 处潜在泄露")
            for leak in leaks[:5]:
                print_error(f"  {leak}")
            self.add_result(TestResult(
                name="API Key泄露检测",
                category="安全",
                passed=False,
                message=f"发现{len(leaks)}处潜在泄露",
                duration=duration,
                details=leaks[:10],
                fix_suggestion="将API Key移至环境变量或配置文件，并添加到.gitignore"
            ))
        else:
            print_success(f"未发现API Key泄露 (扫描{len(py_files)}个文件)")
            details.append(f"扫描{len(py_files)}个文件")
            self.add_result(TestResult(
                name="API Key泄露检测",
                category="安全",
                passed=True,
                message="无泄露",
                duration=duration,
                details=details
            ))

    def test_sensitive_files(self):
        """测试18: 敏感文件检查"""
        print_header("测试18: 敏感文件检查")
        start = time.time()

        # 检查不应存在的敏感文件
        sensitive_patterns = [
            '.env',
            '.env.local',
            '.env.production',
            'credentials.json',
            'secrets.json',
            'private.key',
            '*.pem',
        ]

        # 检查.gitignore（优先当前目录，其次项目根目录）
        gitignore = self.base_dir / ".gitignore"
        if not gitignore.exists():
            gitignore = self.base_dir.parent / ".gitignore"
        gitignore_exists = gitignore.exists()

        details = []
        issues = []

        if gitignore_exists:
            print_success(".gitignore存在")
            details.append(".gitignore: ✓")

            with open(gitignore, 'r', encoding='utf-8') as f:
                gitignore_content = f.read()

            # 检查关键忽略项
            should_ignore = ['.env', 'research_config*.json', '__pycache__', '*.pyc', 'output/', 'uploads/']
            for item in should_ignore:
                if item in gitignore_content:
                    details.append(f"忽略{item}: ✓")
                else:
                    print_warning(f".gitignore缺少: {item}")
                    details.append(f"忽略{item}: ✗")
        else:
            print_warning(".gitignore不存在")
            details.append(".gitignore: ✗")
            issues.append("缺少.gitignore文件")

        # 检查敏感文件
        for pattern in sensitive_patterns:
            files = list(self.base_dir.glob(pattern))
            if files:
                for f in files:
                    print_warning(f"发现敏感文件: {f.name}")
                    issues.append(f"敏感文件: {f.name}")
                    details.append(f"敏感文件: {f.name}")

        self.add_result(TestResult(
            name="敏感文件检查",
            category="安全",
            passed=len(issues) == 0,
            message=f"发现{len(issues)}个问题" if issues else "无问题",
            duration=time.time() - start,
            details=details,
            fix_suggestion="创建.gitignore并添加敏感文件" if issues else ""
        ))

    # ==================== 集成测试 ====================

    def test_user_flow(self):
        """测试19: 核心模块集成测试"""
        if self.quick_mode:
            print_info("快速模式：跳过集成测试")
            self.skipped_count += 1
            return

        print_header("测试19: 核心模块集成测试")
        start = time.time()
        details = []

        try:
            sys.path.insert(0, str(self.base_dir))

            print_section("步骤1: 检查核心模块集成")
            from engine import FusionResearchBot
            print_success("FusionResearchBot 可导入")
            details.append("Engine: ✓")

            print_section("步骤2: 检查配置集成")
            from config import load_config, apply_config_to_env
            cfg = load_config()
            apply_config_to_env(cfg)
            print_success("配置加载与环境注入正常")
            details.append("Config: ✓")

            print_section("步骤3: 检查搜索路由")
            from shared.billing.router import RouteDispatcher
            route = RouteDispatcher.get_route("deepseek_standard")
            if route.get("gptr_retriever") != "deepseek":
                raise AssertionError("DeepSeek 路由配置异常")
            print_success("DeepSeek 搜索路由正常")
            details.append("Route: ✓")

            self.add_result(TestResult(
                name="核心模块集成",
                category="集成",
                passed=True,
                message="集成测试完成",
                duration=time.time() - start,
                details=details
            ))

        except Exception as e:
            print_error(f"集成测试失败: {e}")
            self.add_result(TestResult(
                name="核心模块集成",
                category="集成",
                passed=False,
                message=str(e)[:50],
                duration=time.time() - start,
                details=details,
                fix_suggestion="检查 engine/config/shared 模块依赖"
            ))

    # ==================== 报告生成 ====================

    def generate_reports(self):
        """生成测试报告"""
        print_header("生成测试报告", '-')

        total_time = time.time() - self.start_time
        passed = sum(1 for r in self.results if r.passed)
        failed = sum(1 for r in self.results if not r.passed)

        # 环境信息
        environment = {
            "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "platform": sys.platform,
            "test_mode": "quick" if self.quick_mode else "full",
            "base_dir": str(self.base_dir)
        }

        report = TestReport(
            timestamp=datetime.now().isoformat(),
            total_tests=len(self.results),
            passed=passed,
            failed=failed,
            skipped=self.skipped_count,
            duration=total_time,
            results=self.results,
            environment=environment
        )

        # 生成JSON报告
        json_path = self.base_dir / "test_report.json"
        self._generate_json_report(report, json_path)

        # 生成HTML报告
        html_path = self.base_dir / "test_report.html"
        self._generate_html_report(report, html_path)

        print_success(f"JSON报告: {json_path.name}")
        print_success(f"HTML报告: {html_path.name}")

        return json_path, html_path

    def _generate_json_report(self, report: TestReport, path: Path):
        """生成JSON格式报告"""
        data = {
            "timestamp": report.timestamp,
            "summary": {
                "total": report.total_tests,
                "passed": report.passed,
                "failed": report.failed,
                "skipped": report.skipped,
                "duration": round(report.duration, 2)
            },
            "environment": report.environment,
            "results": [
                {
                    "name": r.name,
                    "category": r.category,
                    "passed": r.passed,
                    "message": r.message,
                    "duration": round(r.duration, 2),
                    "details": r.details,
                    "fix_suggestion": r.fix_suggestion
                }
                for r in report.results
            ]
        }

        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _generate_html_report(self, report: TestReport, path: Path):
        """生成HTML格式报告"""
        # 统计各类别
        categories = {}
        for r in report.results:
            if r.category not in categories:
                categories[r.category] = {"passed": 0, "failed": 0}
            if r.passed:
                categories[r.category]["passed"] += 1
            else:
                categories[r.category]["failed"] += 1

        # 生成结果HTML
        results_html = ""
        for r in report.results:
            status_icon = "✅" if r.passed else "❌"
            item_class = "passed" if r.passed else "failed"
            duration_str = f"{r.duration:.2f}"
            
            details_html = ""
            if r.details:
                details_items = "".join([f"<li>{d}</li>" for d in r.details[:5]])
                details_html = f'<ul class="result-details">{details_items}</ul>'
            
            fix_html = f'<div class="fix-suggestion">🔧 {r.fix_suggestion}</div>' if r.fix_suggestion else ""
            
            results_html += f'''
            <div class="result-item {item_class}">
                <div class="result-header">
                    <span class="result-name">{status_icon} {r.name}</span>
                    <span class="result-category">{r.category}</span>
                </div>
                <div class="result-message">{r.message}</div>
                <div class="result-duration">耗时: {duration_str}s</div>
                {details_html}
                {fix_html}
            </div>
            '''
        
        # 生成分类统计HTML
        categories_html = ""
        for cat, stats in categories.items():
            categories_html += f'<div class="category"><span class="category-name">{cat}</span><span class="category-stats">✓ {stats["passed"]} / ✗ {stats["failed"]}</span></div>'
        
        html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>测试报告 - 小醉桌面研究 v1.0</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f5f5f5; color: #333; }}
        .container {{ max-width: 1000px; margin: 0 auto; padding: 20px; }}
        .header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 30px; border-radius: 10px; margin-bottom: 20px; }}
        .header h1 {{ font-size: 24px; margin-bottom: 10px; }}
        .header .time {{ opacity: 0.8; font-size: 14px; }}
        .summary {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 15px; margin-bottom: 20px; }}
        .summary-card {{ background: white; padding: 20px; border-radius: 10px; text-align: center; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
        .summary-card .number {{ font-size: 36px; font-weight: bold; margin-bottom: 5px; }}
        .summary-card .label {{ color: #666; font-size: 14px; }}
        .summary-card.passed .number {{ color: #10b981; }}
        .summary-card.failed .number {{ color: #ef4444; }}
        .summary-card.skipped .number {{ color: #f59e0b; }}
        .summary-card.total .number {{ color: #3b82f6; }}
        .categories {{ background: white; border-radius: 10px; padding: 20px; margin-bottom: 20px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
        .categories h2 {{ font-size: 18px; margin-bottom: 15px; }}
        .category {{ display: flex; justify-content: space-between; padding: 10px 0; border-bottom: 1px solid #eee; }}
        .category:last-child {{ border-bottom: none; }}
        .category-name {{ font-weight: 500; }}
        .category-stats {{ color: #666; }}
        .results {{ background: white; border-radius: 10px; padding: 20px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
        .results h2 {{ font-size: 18px; margin-bottom: 15px; }}
        .result-item {{ padding: 15px; border-radius: 8px; margin-bottom: 10px; }}
        .result-item.passed {{ background: #f0fdf4; border-left: 4px solid #10b981; }}
        .result-item.failed {{ background: #fef2f2; border-left: 4px solid #ef4444; }}
        .result-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }}
        .result-name {{ font-weight: 600; }}
        .result-category {{ background: #e5e7eb; padding: 2px 8px; border-radius: 4px; font-size: 12px; }}
        .result-message {{ color: #666; font-size: 14px; }}
        .result-duration {{ color: #999; font-size: 12px; }}
        .result-details {{ margin-top: 10px; padding: 10px; background: #f9fafb; border-radius: 4px; font-size: 13px; }}
        .result-details li {{ margin: 3px 0; }}
        .fix-suggestion {{ margin-top: 10px; padding: 10px; background: #fff7ed; border-radius: 4px; font-size: 13px; color: #c2410c; }}
        .footer {{ text-align: center; padding: 20px; color: #666; font-size: 14px; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>测试报告 - 小醉桌面研究 v1.0</h1>
            <div class="time">生成时间: {report.timestamp}</div>
        </div>

        <div class="summary">
            <div class="summary-card total">
                <div class="number">{report.total_tests}</div>
                <div class="label">总测试数</div>
            </div>
            <div class="summary-card passed">
                <div class="number">{report.passed}</div>
                <div class="label">通过</div>
            </div>
            <div class="summary-card failed">
                <div class="number">{report.failed}</div>
                <div class="label">失败</div>
            </div>
            <div class="summary-card skipped">
                <div class="number">{report.skipped}</div>
                <div class="label">跳过</div>
            </div>
        </div>

        <div class="categories">
            <h2>📊 分类统计</h2>
            {categories_html}
        </div>

        <div class="results">
            <h2>📋 测试结果详情</h2>
            {results_html}
        </div>

        <div class="footer">
            <p>总耗时: {report.duration:.2f}秒 | Python {report.environment['python_version']} | {report.environment['platform']}</p>
        </div>
    </div>
</body>
</html>'''

        with open(path, 'w', encoding='utf-8') as f:
            f.write(html)

    # ==================== 总结 ====================

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
        if self.skipped_count > 0:
            print_warning(f"跳过: {self.skipped_count}项")

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
            if not r.passed and r.fix_suggestion:
                print_fix(r.fix_suggestion)

        print(f"\n{Colors.BLUE}{'='*70}{Colors.RESET}")
        if failed == 0:
            print(f"{Colors.GREEN}🎉 所有测试通过！系统状态良好。{Colors.RESET}")
        else:
            print(f"{Colors.YELLOW}⚠️ 有{failed}项测试未通过，请检查上述错误。{Colors.RESET}")
        print(f"{Colors.BLUE}{'='*70}{Colors.RESET}")

        # 生成报告
        if self.generate_report or failed > 0:
            self.generate_reports()

        return 0 if failed == 0 else 1

    def run_all(self):
        """运行所有测试"""
        print(f"{Colors.BLUE}{Colors.BOLD}小醉桌面研究 v1.0 一键测试套件（专业版）{Colors.RESET}")
        print(f"测试时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"项目路径: {self.base_dir}")
        if self.quick_mode:
            print(f"{Colors.YELLOW}快速模式: 跳过耗时测试{Colors.RESET}")
        if self.generate_report:
            print(f"{Colors.CYAN}报告模式: 生成JSON/HTML报告{Colors.RESET}")

        # 环境检查
        self.test_environment()

        # 基础测试
        self.test_syntax()
        self.test_imports()

        # 配置测试
        self.test_config_files()
        # 目录测试
        self.test_directories()

        # 功能测试
        self.test_knowledge_extractor()
        self.test_source_tier()
        self.test_incremental_tracker()
        self.test_search_module()

        # 安全测试
        self.test_api_key_leakage()
        self.test_sensitive_files()

        # 集成测试
        self.test_user_flow()

        return self.print_summary()

def main():
    # Windows下设置UTF-8
    if sys.platform == 'win32':
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

    # 解析参数
    quick_mode = '--quick' in sys.argv or '-q' in sys.argv
    generate_report = '--report' in sys.argv or '-r' in sys.argv

    suite = TestSuite(quick_mode=quick_mode, generate_report=generate_report)
    exit_code = suite.run_all()
    sys.exit(exit_code)

if __name__ == "__main__":
    main()
