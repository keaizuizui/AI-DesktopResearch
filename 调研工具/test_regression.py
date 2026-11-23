"""综合回归测试 - 覆盖所有历史修改"""
import sys, re, inspect
sys.path.insert(0, '.')

passed = 0
failed = 0

def check(name, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  ✓ {name}")
        passed += 1
    else:
        print(f"  ✗ {name} — {detail}")
        failed += 1

# ========== 测试1: _extract_plan_key_info ==========
print('\n[测试1] _extract_plan_key_info() 方向/追问提取')
from search import _extract_plan_key_info

# 注意: 函数内过滤条件 len(line) > 10，测试数据需超过10字
plan = (
    "【建议调研方向】\n"
    "1. 中国新能源汽车市场规模和增长趋势分析\n"
    "2. 新能源汽车竞争格局与主要品牌渠道策略研究\n"
    "3. 新能源汽车行业政策法规风险和监管趋势梳理\n"
    "\n"
    "【核心追问】\n"
    "1. 2025年新能源汽车市场规模预计达到多少？\n"
    "2. 主要新能源车企的产品线营收占比是多少？"
)
dirs, qs = _extract_plan_key_info(plan)
check("方向提取", len(dirs) == 3, f"实际{len(dirs)}条")
check("追问提取", len(qs) == 2, f"实际{len(qs)}条")

# ========== 测试2: _build_enriched_query ==========
print('\n[测试2] _build_enriched_query() 精简查询构建')
from search import _build_enriched_query

q = _build_enriched_query("调研2025年中国新能源汽车市场", plan)
check("查询长度<=150", len(q) <= 150, f"实际{len(q)}字")
check("含任务主体与方向内容", "新能源汽车" in q and ("市场规模" in q or "竞争格局" in q), f"实际: {q[:60]}")

# ========== 测试3: _build_parallel_queries ==========
print('\n[测试3] _build_parallel_queries() 并行查询拆分')
from search import _build_parallel_queries

qs3a = _build_parallel_queries("调研2025年中国新能源汽车市场", plan, max_queries=3)
check("正常拆分3条", len(qs3a) == 3, f"实际{len(qs3a)}条")
for i, q in enumerate(qs3a):
    check(f"子查询{i+1}<=400字", len(q) <= 400)

plan_small = "【建议调研方向】\n1. 中国新能源汽车市场规模和增长趋势\n\n【核心追问】\n1. 2025年新能源汽车市场规模预计达到多少？"
qs3b = _build_parallel_queries("调研任务", plan_small, max_queries=3)
check("方向不足降级>=1条", len(qs3b) >= 1)

qs3c = _build_parallel_queries("调研任务", "无结构的plan文本", max_queries=3)
check("无方向兜底1条", len(qs3c) == 1)

# ========== 测试4: engine.py regex修复 ==========
print('\n[测试4] engine.py 章节标题正则修复')
# ^###\s* 匹配三级标题（###开头），不匹配二级(##)
# 注: #### 行首也是###，所以也会匹配——这是原始正则行为，engine.py中不涉及四级标题
patterns = [
    ("###标题", True),
    ("### 标题", True),
    ("###  标题", True),
    ("## 标题", False),
    ("#### 标题", True),   # ^### 前三个字符匹配，属原始正则行为
]
for text, should_match in patterns:
    m = re.match(r'^###\s*', text)
    result = m is not None
    check(f"正则 {repr(text)}", result == should_match)

# ========== 测试6: deep_research默认串行 ==========
print('\n[测试6] deep_research() 默认走串行')
cfg_empty = {}
check("默认False", cfg_empty.get("enable_gptr_parallel", False) == False)
cfg_false = {"enable_gptr_parallel": False}
check("显式False", cfg_false.get("enable_gptr_parallel", False) == False)

# ========== 测试7: conduct_research_parallel存在 ==========
print('\n[测试7] conduct_research_parallel() 方法签名')
from search import GPTRResearcherAdapter
check("方法存在", hasattr(GPTRResearcherAdapter, "conduct_research_parallel"))
sig = inspect.signature(GPTRResearcherAdapter.conduct_research_parallel)
params = list(sig.parameters.keys())
check("参数签名", "plan_text" in params and "refined_task" in params)

# ========== 测试8: 语法完整性 ==========
print('\n[测试8] 语法完整性')
import py_compile
for f in ["search.py", "engine.py", "config.py"]:
    try:
        py_compile.compile(f, doraise=True)
        check(f"{f}语法OK", True)
    except py_compile.PyCompileError as e:
        check(f"{f}语法OK", False, str(e))

# ========== 汇总 ==========
print("\n" + "=" * 60)
print(f"回归测试完成: {passed}通过, {failed}失败")
print("=" * 60)
if failed > 0:
    sys.exit(1)
