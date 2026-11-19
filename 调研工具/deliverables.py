#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小醉桌面研究 v1.0 / deliverables.py
交付层：Mermaid流程图 + Plotly图表 + 表格自动检测 + Markdown渲染Word + Excel附录
"""

import os, re, json, ast, sys
from typing import List, Dict, Optional, Tuple
from datetime import datetime

import numpy as np

from docx import Document
from docx.shared import Pt, Inches, RGBColor, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.enum.table import WD_TABLE_ALIGNMENT

from v16_1_utils import smart_truncate
from config import logger, LLMClient, extract_json, AGENT_PROMPTS
from shared.llm_client import safe_json_loads

# TimesFM 预测（可选依赖）
try:
    from shared.timesfm_predictor import TimesFMPredictor
    TIMESFM_AVAILABLE = True
except ImportError:
    TIMESFM_AVAILABLE = False

PLOTLY_AVAILABLE = False
KALEIDO_AVAILABLE = False
try:
    import plotly.graph_objects as go
    import plotly.express as px
    from plotly.subplots import make_subplots
    PLOTLY_AVAILABLE = True
except ImportError:
    pass

if PLOTLY_AVAILABLE:
    try:
        import kaleido  # noqa: F401
        KALEIDO_AVAILABLE = True
    except ImportError:
        logger.warning("[Kaleido] 未安装 kaleido 包，PNG 图表将无法生成。安装：pip install kaleido")

EXCEL_AVAILABLE = False
try:
    from openpyxl import Workbook
    EXCEL_AVAILABLE = True
except ImportError:
    pass


# ===== Matplotlib 沙箱：LLM 代码提取与执行前校验=====
SANDBOX_MAX_CODE_CHARS = 6000
SANDBOX_MAX_CODE_LINES = 120


def _looks_like_prose(line: str) -> bool:
    """判断一行是否为模型附带的解释性中文段落（而非代码）。"""
    s = line.strip()
    if not s or s.startswith('#'):
        return False
    if not re.search(r'[\u4e00-\u9fa5]', s):
        return False
    return not any(ch in s for ch in '=()[]{}:,"\'')


def _strip_code_noise(code: str) -> str:
    """去掉代码前后的解释文字、围栏残留与空行。"""
    text = code.replace('\r\n', '\n').replace('\r', '\n')
    lines = text.split('\n')

    # 丢弃首个 import/from 之前的解释文字
    for i, line in enumerate(lines):
        if line.strip().startswith(('import ', 'from ')):
            lines = lines[i:]
            break

    # 丢弃尾部空行与中文散文式解释
    while lines:
        while lines and not lines[-1].strip():
            lines.pop()
        if lines and _looks_like_prose(lines[-1]):
            lines.pop()
            continue
        break
    while lines and not lines[0].strip():
        lines.pop(0)
    return '\n'.join(lines).strip()


def _extract_python_code(raw: str) -> str:
    """从 LLM 输出中稳健提取 Python 代码。

    优先 ```python 围栏（允许围栏未闭合，即输出被截断），其次任意 ``` 围栏，
    最后把整段文本当作代码处理。返回的代码仍需通过语法校验。
    """
    if not raw or not raw.strip():
        return ""
    text = raw.replace('\r\n', '\n').replace('\r', '\n').strip()

    fence = re.search(r'```(?:python|py)[ \t]*\n(.*?)(?:```|\Z)',
                      text, flags=re.DOTALL | re.IGNORECASE)
    if not fence:
        fence = re.search(r'```[ \t]*[a-zA-Z0-9_+#-]*[ \t]*\n(.*?)(?:```|\Z)',
                          text, flags=re.DOTALL)
    if fence:
        return _strip_code_noise(fence.group(1))
    return _strip_code_noise(text)


def _validate_python_code(code: str) -> Tuple[bool, str]:
    """执行前校验：长度 + 语法 + 能力白名单。返回 (是否合法, 错误说明)。

    说明：这是静态能力收敛，不是真正的安全沙箱；运行时仍需隔离环境。
    """
    if not code or not code.strip():
        return False, "代码为空"
    lines = code.count('\n') + 1
    if len(code) > SANDBOX_MAX_CODE_CHARS:
        return False, f"代码过长（{len(code)} 字符，上限 {SANDBOX_MAX_CODE_CHARS}）"
    if lines > SANDBOX_MAX_CODE_LINES:
        return False, f"代码过长（{lines} 行，上限 {SANDBOX_MAX_CODE_LINES}）"
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, f"语法错误（第 {e.lineno} 行）：{e.msg}"
    except (ValueError, MemoryError) as e:
        return False, f"代码无法解析：{e}"

    allowed_roots = {
        "matplotlib", "numpy", "pandas", "math", "statistics",
        "json", "datetime", "itertools", "collections", "random",
    }
    forbidden_names = {
        "open", "exec", "eval", "compile", "__import__", "input",
        "breakpoint", "globals", "locals", "vars", "getattr", "setattr",
        "delattr", "__builtins__", "exit", "quit",
    }
    forbidden_attrs = {
        "system", "popen", "spawn", "spawnl", "spawnv", "remove", "unlink",
        "rmdir", "removedirs", "walk", "listdir", "scandir", "chmod",
        "chown", "kill", "fork", "execv", "execve", "urlopen", "connect",
    }

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in allowed_roots:
                    return False, f"禁止导入模块：{alias.name}"
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root not in allowed_roots:
                return False, f"禁止导入模块：{node.module or ''}"
        elif isinstance(node, ast.Name):
            if node.id in forbidden_names:
                return False, f"禁止使用名称：{node.id}"
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__") or node.attr in forbidden_attrs:
                return False, f"禁止访问属性：{node.attr}"
    return True, ""


def _sandbox_env(work_dir: str) -> Dict[str, str]:
    """为生成代码准备最小化环境，避免继承应用凭据。"""
    keep = {}
    for key in (
        "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH", "COMSPEC",
        "PATHEXT", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
    ):
        value = os.environ.get(key)
        if value:
            keep[key] = value
    keep.update({
        "HOME": work_dir,
        "USERPROFILE": work_dir,
        "TEMP": work_dir,
        "TMP": work_dir,
        "MPLBACKEND": "Agg",
        "MPLCONFIGDIR": work_dir,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    return keep


class ChartGenerator:
    BUSINESS_COLORS = ['#2E75B6', '#ED7D31', '#A5A5A5', '#FFC000', '#5B9BD5',
                       '#70AD47', '#264478', '#9E480E', '#636363', '#4472C4']

    def __init__(self, output_dir: str, llm: LLMClient):
        self.output_dir = output_dir
        self.llm = llm

    def extract_data(self, desc: str, evidence: str) -> Optional[Dict]:
        prompt = f"""【任务】从素材中提取"{desc}"相关的图表数据
【素材】{smart_truncate(evidence, 4000)}

【图表选型规则】
- 时间序列数据 → line/area
- 分类对比数据 → bar/horizontal_bar
- 占比数据（总和≈100%）→ pie
- 多维度评分 → radar
- 矩阵数据 → heatmap
- 多系列混合 → combo/grouped_bar/stacked_bar
- 不适合图表的数据 → table_mode: true

【输出要求】
- 只输出纯JSON对象，不要解释文字
- 无数据则输出null
- 标准格式：
{{
  "type": "auto",
  "title": "图表标题",
  "unit": "单位如亿元、%",
  "labels": ["类别1","类别2"],
  "series": [{{"name": "系列A", "values": [1,2,3], "type": "bar"}}],
  "annotations": ["注释1"],
  "table_mode": false
}}
- table_mode为true时增加headers和rows字段"""
        try:
            raw = self.llm.fast(
                [{"role": "user", "content": prompt}],
                system=AGENT_PROMPTS["data_extractor"],
                max_tokens=1200, temperature=0.3, json_mode=True
            )
            if not raw or "null" in raw.lower():
                return None
            data = safe_json_loads(extract_json(raw))
            if data and (data.get("values") or data.get("series") or data.get("rows")):
                return data
        except Exception as e:
            logger.debug(f"[LLM提取失败] {e}")
        return self._regex_extract(desc, evidence)

    def _regex_extract(self, desc: str, text: str) -> Optional[Dict]:
        table_rows = re.findall(r'\|\s*([^\|\n]+?)\s*\|\s*(\d+[\.,]?\d*)\s*\|', text)
        if table_rows and len(table_rows) >= 3:
            labels = [r[0].strip() for r in table_rows[:8]]
            values = [float(r[1].replace(',', '')) for r in table_rows[:8]]
            return {"type": "auto", "title": desc, "unit": "", "labels": labels,
                    "series": [{"name": "数值", "values": values}]}

        pairs = re.findall(r'([\u4e00-\u9fa5]{2,8}|[a-zA-Z\s]{3,20})[：:]\s*(\d+[\.,]?\d*)\s*%?', text)
        if pairs and len(pairs) >= 3:
            labels = [p[0].strip() for p in pairs[:8]]
            values = [float(p[1].replace(',', '')) for p in pairs[:8]]
            return {"type": "auto", "title": desc, "unit": "", "labels": labels,
                    "series": [{"name": "数值", "values": values}]}

        nums = re.findall(r'(20\d{2})[年\s]*.*?(\d+\.?\d*)[\s]*(?:亿元|万人|%|个)', text)
        if nums and len(nums) >= 3:
            labels = [n[0] for n in nums[:8]]
            values = [float(n[1]) for n in nums[:8]]
            unit = re.search(r'(?:亿元|万人|%|个)', text)
            return {"type": "line", "title": desc, "unit": unit.group(0) if unit else "",
                    "labels": labels, "series": [{"name": "数值", "values": values}]}
        return None

    def _augment_with_forecast(self, data: Dict, ctype: str) -> Optional[Dict]:
        """
        对时间序列数据自动追加 TimesFM 预测值。

        检测条件：
        - 图表类型为 line/area
        - labels 为年份格式（20xx）
        - values 有 >=3 个数据点
        - TimesFM 可用

        返回修改后的 data（追加预测 series 和未来年份 labels），
        或 None（预测不可用/不适用）。
        """
        if not TIMESFM_AVAILABLE:
            return None
        if ctype not in ("line", "area"):
            return None

        labels = data.get("labels", [])
        series = data.get("series", [])
        if not series or not labels:
            return None

        values = series[0].get("values", [])
        if len(values) < 3:
            return None

        # 检测年份标签
        years = []
        for lbl in labels:
            m = re.match(r'^(20\d{2})$', str(lbl))
            years.append(int(m.group(1)) if m else None)
        if not all(years):
            return None

        try:
            predictor = TimesFMPredictor(lazy=True)
            if not predictor.is_available():
                return None

            # 预测步数 = 数据长度的 1/3，最少 3 步，最多 6 步
            forecast_horizon = max(3, min(len(values) // 3, 6))
            point, quantile = predictor.predict(values, horizon=forecast_horizon)

            # 生成未来年份标签
            last_year = years[-1]
            future_labels = [str(last_year + i + 1) for i in range(forecast_horizon)]

            # 构造预测数据：合并历史 + 预测
            forecast_values = point.tolist()
            p10_values = quantile[:, 0].tolist()
            p90_values = quantile[:, -1].tolist()

            # 修改原始数据：追加预测年份
            data["labels"] = labels + future_labels
            # 历史值用 None 补齐预测部分
            data["series"][0]["values"] = values + [None] * forecast_horizon

            # 追加预测 series
            data["series"].append({
                "name": "预测值",
                "values": [None] * len(values) + forecast_values,
                "type": "line",
                "line": {"dash": "dash", "color": "#ED7D31"}
            })

            # 追加置信区间
            data["series"].append({
                "name": "预测上界(P90)",
                "values": [None] * len(values) + p90_values,
                "type": "line",
                "line": {"dash": "dot", "color": "#A5A5A5", "width": 1}
            })
            data["series"].append({
                "name": "预测下界(P10)",
                "values": [None] * len(values) + p10_values,
                "type": "line",
                "line": {"dash": "dot", "color": "#A5A5A5", "width": 1}
            })

            # 更新图表类型为 combo（历史折线+虚线预测）
            data["type"] = "combo"
            data["chart_hint"] = "model_forecast"

            logger.info(
                f"[TimesFM] 预测 {len(values)}→{forecast_horizon} 步, "
                f"趋势: {'↑' if forecast_values[-1] > values[-1] else '↓'}"
            )
            return data

        except Exception as e:
            logger.debug(f"[TimesFM预测失败] {e}")
            return None

    def _auto_select_chart(self, data: Dict) -> str:
        """增强智能选型——基于数据特征分析"""
        series = data.get("series", [])
        labels = data.get("labels", [])
        title = data.get("title", "").lower()

        if data.get("table_mode"):
            return "table"

        # 多系列
        if len(series) > 1:
            types = [s.get("type", "bar") for s in series]
            if set(types) == {"bar", "line"} or set(types) == {"line", "bar"}:
                return "combo"
            if len(labels) <= 6 and all(v >= 0 for s in series for v in s.get("values", [])):
                return "stacked_bar"
            return "grouped_bar"

        values = series[0].get("values", []) if series else []
        if not values:
            return "bar"

        # 时间序列检测
        time_pattern = any(re.match(r'20\d{2}|\d{4}[-/]\d{1,2}|Q[1-4]|第[一二三四]季度', str(l)) for l in labels[:3])
        if time_pattern and len(labels) >= 4:
            return "area" if len(labels) <= 8 else "line"

        # 占比检测（所有权重非负且和为100%附近）
        total = sum(v for v in values if v >= 0)
        if total > 0 and len(labels) <= 8 and all(v >= 0 for v in values):
            if 0.8 <= total <= 1.2 and max(values) / total < 0.8:
                return "pie"
            if 80 <= total <= 120 and max(values) / total < 0.8:
                return "pie"

        # 排名检测（值递减、类别型标签）
        if len(labels) > 8 and len(labels) <= 20:
            return "horizontal_bar"

        # 对比类关键词
        if any(kw in title for kw in ['vs','对比','排名','排行','份额','占比','比例']):
            if len(labels) <= 6 and total > 0:
                return "pie" if max(values)/total < 0.8 else "bar"
            return "horizontal_bar"

        # 趋势/变化类关键词
        if any(kw in title for kw in ['趋势','增长','变化','走势','历年','季度','月度']):
            return "line"

        # 矩阵/雷达类
        if len(labels) <= 8 and len(labels) >= 3:
            if all(isinstance(v, (int, float)) and 0 <= v <= 100 for v in values):
                if any(kw in title for kw in ['评分','能力','维度','指标','评估','矩阵']):
                    return "radar"

        # 瀑布/构成类
        if total > 0 and all(v >= 0 for v in values):
            if any(kw in title for kw in ['构成','结构','组成','分布','分类']):
                if len(labels) <= 6:
                    return "pie"

        # 默认
        if len(labels) > 10:
            return "horizontal_bar"
        return "bar"

    def _generate_mermaid(self, desc: str, evidence: str, idx: int) -> Optional[Tuple[str, str]]:
        """Mermaid文本图表生成。当Plotly数据提取失败时，用LLM生成Mermaid流程图/架构图/时序图。"""
        prompt = f"""【角色】数据可视化专家
【任务】生成Mermaid格式图表代码

【图表需求】{desc}
【相关素材】{smart_truncate(evidence, 3000)}

【选型规则】
- flowchart：适合链路/闭环/架构图
- gantt：适合时间线
- sequenceDiagram：适合交互时序
- pie：适合比例图

【输出要求】
1. 节点使用中文，保持专业术语
2. 不要包含markdown代码块标记
3. 直接输出Mermaid语法
4. 数据不足无法生成则输出"SKIP"
5. 只输出代码或SKIP，不要任何解释文字"""
        try:
            raw = self.llm.fast(
                [{"role":"user","content":prompt}],
                system=AGENT_PROMPTS["code_generator"],
                max_tokens=800, temperature=0.3
            )
            raw = raw.strip()
            if not raw or "SKIP" in raw.upper():
                return None
            # 清洗markdown代码块标记
            raw = re.sub(r'^```mermaid\\s*', '', raw, flags=re.MULTILINE)
            raw = re.sub(r'^```\s*', '', raw, flags=re.MULTILINE)
            raw = raw.strip()
            if not raw:
                return None

            safe_name = re.sub(r'[^\w\u4e00-\u9fa5]', '_', desc)[:25]
            mmd_path = os.path.join(self.output_dir, f"chart_{idx:02d}_{safe_name}.mmd")
            png_path = os.path.join(self.output_dir, f"chart_{idx:02d}_{safe_name}.png")

            # 保存.mmd源文件
            with open(mmd_path, 'w', encoding='utf-8') as f:
                f.write(raw)

            # 尝试 mermaid-cli 渲染PNG
            png_ok = False
            try:
                import subprocess
                result = subprocess.run(
                    ['mmdc', '-i', mmd_path, '-o', png_path, '-s', '2', '-b', 'white'],
                    capture_output=True, text=True, timeout=15
                )
                if result.returncode == 0 and os.path.exists(png_path) and os.path.getsize(png_path) > 200:
                    png_ok = True
                    logger.info(f"[Mermaid→PNG] {desc[:30]}...")
                else:
                    logger.info(f"[Mermaid→PNG] mmdc失败: {result.stderr[:100]}")
            except FileNotFoundError:
                logger.info("[Mermaid] mermaid-cli未安装，降级为代码块")
            except Exception as e:
                logger.info(f"[Mermaid→PNG] 异常: {e}")

            if png_ok:
                return (png_path, "")  # PNG可用于Word插入
            else:
                # fallback: 返回mmd文件路径，compile_word将内容作为代码块插入
                return (mmd_path, "mermaid")
        except Exception as e:
            logger.warning(f"[Mermaid生成失败] {e}")
            return None

    def _request_sandbox_code(self, desc: str, evidence: str, error_note: str = "") -> str:
        """请求一次 matplotlib 代码；error_note 非空时为修复性重试。"""
        repair = ""
        if error_note:
            repair = f"""
【上一次输出不合格】{error_note}
【重新生成要求】必须一次性输出完整、语法正确的代码：所有括号/方括号/引号必须闭合，
数据列表必须写完整，禁止截断、禁止省略。"""
        prompt = f"""【角色】Python数据可视化专家
【任务】生成matplotlib代码创建图表

【图表需求】{desc}
【相关素材数据】{smart_truncate(evidence, 3000)}
{repair}
⚠️ 【沙箱执行规则——任何错误都会导致生成失败】
1. 只使用matplotlib（环境未安装seaborn，禁止import seaborn），不要用plotly
2. 代码开头五行固定为：
   import matplotlib
   matplotlib.use('Agg')
   import matplotlib.pyplot as plt
   plt.rcParams['font.sans-serif'] = ['Microsoft YaHei', 'SimHei', 'DejaVu Sans']
   plt.rcParams['axes.unicode_minus'] = False
3. 图表保存必须用：plt.savefig('OUTPUT_PNG', dpi=150, bbox_inches='tight', facecolor='white')
4. 不要调用plt.show()、plt.close()——只savefig
5. 数据不足无法生成则输出"SANDBOX_SKIP"
6. 只输出Python代码或SANDBOX_SKIP，不要markdown标记，不要解释文字
7. 必须输出完整可执行代码，不要省略数据或语句（禁止出现 ...、省略、TODO），总行数不超过60行"""
        return self.llm.fast(
            [{"role":"user","content":prompt}],
            system=AGENT_PROMPTS["code_generator"],
            max_tokens=3000, temperature=0.1 if error_note else 0.2
        )

    def _sandbox_matplotlib(self, desc: str, evidence: str, idx: int) -> Optional[Tuple[str, str]]:
        """Python沙箱产图。LLM生成matplotlib代码 → 本地subprocess执行 → PNG。
        作为Plotly和Mermaid之后的第三级fallback。

        执行前做代码提取 + ast 语法校验；不完整则用更严格提示重生成一次，
        仍不合法则告警并跳过执行（不再把截断代码丢给 subprocess）。"""
        import subprocess, tempfile

        try:
            safe_name = re.sub(r'[^\w\u4e00-\u9fa5]', '_', desc)[:25]
            png_path = os.path.join(self.output_dir, f"chart_{idx:02d}_{safe_name}.png")
            png_literal = repr(png_path)

            code, err, ok = "", "", False
            for attempt in range(2):
                raw = self._request_sandbox_code(desc, evidence, err if attempt else "")
                if not raw or "SANDBOX_SKIP" in raw.upper():
                    return None

                code = _extract_python_code(raw)
                # 替换OUTPUT_PNG占位符（兼容带引号与不带引号两种写法）
                code = re.sub(r"""(['"])OUTPUT_PNG\1""", lambda _m: png_literal, code)
                code = code.replace('OUTPUT_PNG', png_literal)

                ok, err = _validate_python_code(code)
                if ok and 'matplotlib' not in code.lower():
                    ok, err = False, "未包含 matplotlib 导入"
                if ok:
                    break
                logger.warning(f"[沙箱Matplotlib] 代码校验未通过（{err}）"
                               + ("，用严格提示重新生成..." if attempt == 0 else ""))

            if not ok:
                logger.warning(f"[沙箱Matplotlib] 代码两次校验失败，放弃执行: {err}")
                return None

            # 写入临时文件执行
            with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, encoding='utf-8') as f:
                f.write(code)
                tmp_path = f.name

            try:
                work_dir = os.path.dirname(tmp_path) or tempfile.gettempdir()
                result = subprocess.run(
                    [sys.executable, "-I", tmp_path],
                    capture_output=True, text=True, timeout=30,
                    cwd=work_dir, env=_sandbox_env(work_dir)
                )
            except FileNotFoundError:
                logger.warning("[沙箱Matplotlib] python 命令未找到")
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                return None
            except subprocess.TimeoutExpired:
                logger.warning("[沙箱Matplotlib] 执行超时")
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                return None
            except Exception as e:
                logger.warning(f"[沙箱Matplotlib] 执行异常: {e}")
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                return None

            # 清理临时文件
            try:
                os.unlink(tmp_path)
            except OSError as e:
                logger.debug(f"清理临时文件失败: {e}")

            if result.returncode != 0:
                logger.warning(f"[沙箱Matplotlib] 执行失败: {result.stderr[:200]}")
                return None

            if os.path.exists(png_path) and os.path.getsize(png_path) > 500:
                logger.info(f"[沙箱Matplotlib] {desc[:30]}... → {png_path}")
                return (png_path, "")
            else:
                logger.info(f"[沙箱Matplotlib] {desc[:30]}... PNG未生成或过小")
                return None

        except subprocess.TimeoutExpired:
            logger.warning(f"[沙箱Matplotlib] 执行超时")
            return None
        except Exception as e:
            logger.warning(f"[沙箱Matplotlib生成失败] {e}")
            return None

    def generate(self, idx: int, desc: str, evidence: str) -> Optional[Tuple[str, str]]:
        if not PLOTLY_AVAILABLE:
            return None

        data = self.extract_data(desc, evidence)
        if not data:
            # Plotly失败 → Mermaid → Matplotlib沙箱 三级fallback
            logger.info(f"[Plotly跳过] {desc} 无提取到数据，尝试Mermaid...")
            mermaid_result = self._generate_mermaid(desc, evidence, idx)
            if mermaid_result:
                return mermaid_result
            logger.info(f"[Mermaid跳过] {desc} 无法生成Mermaid，尝试Matplotlib沙箱...")
            return self._sandbox_matplotlib(desc, evidence, idx)

        ctype = data.get("type", "auto")
        if ctype == "auto":
            ctype = self._auto_select_chart(data)

        # 时间序列数据自动追加 TimesFM 预测
        forecast_data = self._augment_with_forecast(data, ctype)
        if forecast_data:
            data = forecast_data
            ctype = data.get("type", ctype)

        try:
            fig = self._create_figure(data, ctype)
            if not fig:
                return None

            safe_name = re.sub(r'[^\w\u4e00-\u9fa5]', '_', desc)[:30]
            png_path = os.path.join(self.output_dir, f"chart_{idx:02d}_{safe_name}.png")
            html_path = os.path.join(self.output_dir, f"chart_{idx:02d}_{safe_name}.html")

            fig.update_layout(
                title=dict(text=data.get("title", desc), font=dict(size=18, family="Microsoft YaHei")),
                template="plotly_white",
                font=dict(family="Microsoft YaHei", size=12),
                margin=dict(l=60, r=40, t=80, b=60),
                legend=dict(orientation="h", yanchor="bottom", y=-0.25, xanchor="center", x=0.5)
            )

            fig.write_html(html_path)
            if KALEIDO_AVAILABLE:
                try:
                    fig.write_image(png_path, width=900, height=520, scale=2)
                except Exception as e:
                    logger.warning(f"[Kaleido] PNG 写入失败: {e}")
            else:
                logger.info(f"[Kaleido] 跳过 PNG，仅生成 HTML 图表")

            logger.info(f"[Plotly] {ctype} → {png_path}")
            return png_path, html_path
        except Exception as e:
            logger.error(f"[图表生成错误] {e}")
            return None

    def _create_figure(self, data: Dict, ctype: str):
        labels = data.get("labels", [])
        series = data.get("series", [{"name": "数值", "values": data.get("values", [])}])
        unit = data.get("unit", "")
        colors = self.BUSINESS_COLORS

        if ctype == "table":
            return self._create_table(data)
        if ctype == "pie":
            values = series[0]["values"] if series else []
            return go.Figure(data=[go.Pie(
                labels=labels, values=values, hole=.4,
                textinfo="label+percent", textfont_size=12,
                marker=dict(colors=colors[:len(labels)], line=dict(color='#FFFFFF', width=2))
            )])
        if ctype == "radar":
            values = series[0]["values"] if series else []
            return go.Figure(data=[go.Scatterpolar(
                r=values + [values[0]], theta=labels + [labels[0]],
                fill='toself', name=series[0].get("name", "数值"), line_color=colors[0]
            )]).update_layout(polar=dict(radialaxis=dict(visible=True, range=[0, max(values)*1.2])))
        if ctype == "horizontal_bar":
            values = series[0]["values"] if series else []
            return go.Figure([go.Bar(
                y=labels, x=values, orientation='h', marker=dict(color=colors[0]),
                text=[f"{v}{unit}" for v in values], textposition='outside'
            )]).update_layout(yaxis=dict(autorange="reversed"))
        if ctype == "stacked_bar":
            fig = go.Figure()
            for i, s in enumerate(series):
                fig.add_trace(go.Bar(name=s["name"], x=labels, y=s["values"], marker_color=colors[i % len(colors)]))
            fig.update_layout(barmode='stack')
            return fig
        if ctype == "grouped_bar":
            fig = go.Figure()
            for i, s in enumerate(series):
                fig.add_trace(go.Bar(name=s["name"], x=labels, y=s["values"], marker_color=colors[i % len(colors)]))
            fig.update_layout(barmode='group')
            return fig
        if ctype == "area":
            fig = go.Figure()
            for i, s in enumerate(series):
                fig.add_trace(go.Scatter(x=labels, y=s["values"], mode='lines', fill='tozeroy',
                                          name=s["name"], line=dict(color=colors[i % len(colors)])))
            return fig
        if ctype == "combo":
            fig = go.Figure()
            for i, s in enumerate(series):
                stype = s.get("type", "bar")
                if stype == "line":
                    fig.add_trace(go.Scatter(x=labels, y=s["values"], name=s["name"], mode='lines+markers',
                                              yaxis="y2", line=dict(color=colors[i % len(colors)], width=3)))
                else:
                    fig.add_trace(go.Bar(x=labels, y=s["values"], name=s["name"],
                                          marker_color=colors[i % len(colors)], opacity=0.7))
            fig.update_layout(
                yaxis=dict(title=series[0].get("name", "")),
                yaxis2=dict(title=series[1].get("name", "") if len(series) > 1 else "", overlaying="y", side="right")
            )
            return fig
        if ctype == "line":
            fig = go.Figure()
            for i, s in enumerate(series):
                fig.add_trace(go.Scatter(x=labels, y=s["values"], name=s["name"], mode='lines+markers',
                                          line=dict(color=colors[i % len(colors)], width=3), marker=dict(size=8)))
            return fig

        values = series[0]["values"] if series else []
        return go.Figure([go.Bar(
            x=labels, y=values,
            marker=dict(color=[colors[i % len(colors)] for i in range(len(values))]),
            text=[f"{v}{unit}" for v in values], textposition='outside'
        )])

    def _create_table(self, data: Dict):
        headers = data.get("headers", ["指标"] + [s["name"] for s in data.get("series", [])])
        rows = data.get("rows", [])
        if not rows and data.get("series"):
            labels = data.get("labels", [])
            series = data["series"]
            rows = []
            for i, label in enumerate(labels):
                row = [label]
                for s in series:
                    vals = s.get("values", [])
                    row.append(str(vals[i]) if i < len(vals) else "")
                rows.append(row)

        return go.Figure(data=[go.Table(
            header=dict(
                values=headers, fill_color='#2E75B6', align='center',
                font=dict(color='white', size=13, family="Microsoft YaHei"),
                line=dict(color='white', width=1)
            ),
            cells=dict(
                values=list(zip(*rows)) if rows else [[]],
                fill_color=[['#F5F7FA', '#FFFFFF'] * (len(rows)//2 + 1)],
                align='center', font=dict(size=12, family="Microsoft YaHei"),
                line=dict(color='#E1E4E8', width=1), height=28
            )
        )]).update_layout(margin=dict(l=20, r=20, t=40, b=20))


class MarkdownDocxCompiler:
    def __init__(self, doc: Document):
        self.doc = doc
        self._set_default_font()

    def _set_default_font(self):
        style = self.doc.styles["Normal"]
        style.font.name = "微软雅黑"
        style._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")
        style.font.size = Pt(10.5)

    def _make_run(self, paragraph, text: str, bold: bool = False, italic: bool = False, 
                  font_name: str = "微软雅黑", font_size: Pt = None, color: RGBColor = None):
        run = paragraph.add_run(text)
        run.font.name = font_name
        run._element.rPr.rFonts.set(qn("w:eastAsia"), font_name)
        if bold:
            run.bold = True
        if italic:
            run.italic = True
        if font_size:
            run.font.size = font_size
        if color:
            run.font.color.rgb = color
        return run

    def add_markdown(self, text: str):
        text = text.replace('\\## ', '## ').replace('\\### ', '### ').replace('\\#### ', '#### ')
        text = text.replace('\\**', '**').replace('\\*', '*')

        lines = text.split("\n")
        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()

            if not stripped:
                i += 1
                continue

            if stripped.startswith("## "):
                self.doc.add_heading(stripped[3:], level=2)
                i += 1
                continue
            if stripped.startswith("### "):
                self.doc.add_heading(stripped[4:], level=3)
                i += 1
                continue
            if stripped.startswith("#### "):
                self.doc.add_heading(stripped[5:], level=4)
                i += 1
                continue

            if stripped == "---" or stripped == "***":
                self.doc.add_paragraph()
                i += 1
                continue

            if stripped.startswith("|"):
                table_lines = []
                while i < len(lines) and lines[i].strip().startswith("|"):
                    table_lines.append(lines[i].strip())
                    i += 1
                self._render_table(table_lines)
                continue

            if stripped.startswith("- ") or stripped.startswith("* "):
                self._render_list_item(stripped[2:], is_bullet=True)
                i += 1
                continue

            m = re.match(r"^(\d+)\.\s+(.*)$", stripped)
            if m:
                self._render_list_item(m.group(2), is_bullet=False)
                i += 1
                continue

            self._render_paragraph(stripped)
            i += 1

    def _render_paragraph(self, text: str):
        p = self.doc.add_paragraph()
        self._add_inline_runs(p, text)

    def _render_list_item(self, text: str, is_bullet: bool):
        style = "List Bullet" if is_bullet else "List Number"
        p = self.doc.add_paragraph(style=style)
        self._add_inline_runs(p, text)

    def _add_inline_runs(self, paragraph, text: str):
        parts = re.split(r'(`[^`]+`)', text)
        for part in parts:
            if part.startswith('`') and part.endswith('`'):
                self._make_run(paragraph, part[1:-1], font_name="Consolas", 
                               font_size=Pt(9.5), color=RGBColor(0xC0, 0x00, 0x00))
                continue

            subparts = re.split(r'(\*\*[^*]+\*\*|\*[^*]+\*)', part)
            for sub in subparts:
                if sub.startswith('**') and sub.endswith('**'):
                    self._make_run(paragraph, sub[2:-2], bold=True)
                elif sub.startswith('*') and sub.endswith('*'):
                    self._make_run(paragraph, sub[1:-1], italic=True)
                else:
                    sub = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', sub)
                    if sub:
                        self._make_run(paragraph, sub)

    def _render_table(self, lines: List[str]):
        if len(lines) < 2:
            return
        header_cells = [c.strip() for c in lines[0].split("|") if c.strip() != ""]
        rows = []
        for line in lines[2:]:
            cells = [c.strip() for c in line.split("|") if c.strip() != ""]
            if cells:
                rows.append(cells)

        if not header_cells:
            return

        table = self.doc.add_table(rows=1 + len(rows), cols=len(header_cells))
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        table.style = 'Light Grid Accent 1'

        hdr_cells = table.rows[0].cells
        for j, h in enumerate(header_cells):
            hdr_cells[j].text = ""
            p = hdr_cells[j].paragraphs[0]
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            self._make_run(p, h, bold=True, font_size=Pt(10))

        for i, row in enumerate(rows):
            row_cells = table.rows[i+1].cells
            for j, val in enumerate(row):
                if j < len(row_cells):
                    row_cells[j].text = ""
                    p = row_cells[j].paragraphs[0]
                    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    self._make_run(p, val)

        self.doc.add_paragraph()


class DeliverableCompiler:
    def __init__(self, output_dir: str, cfg: dict, llm: LLMClient):
        self.output_dir = output_dir
        self.cfg = cfg
        self.llm = llm
        self.chart_gen = ChartGenerator(output_dir, llm)
        self.chart_paths: List[Tuple[str, str, str]] = []
        self.chart_data: List[Dict] = []

    def generate_charts(self, chapters_md: List[str], all_materials: List[Dict]):
        logger.info("[图表生成] Plotly + Mermaid 智能图表引擎...")
        if not PLOTLY_AVAILABLE:
            logger.warning("plotly 未安装，跳过图表生成")
            return

        full = "\n".join(chapters_md)
        markers = re.findall(r"【图表：(.*?)】", full)

        # 主动扫描Markdown表格，自动添加为图表标记
        table_pattern = re.compile(r'\|.*\|.*\|\n\|[\s\-:]+\|[\s\-:]+\|')
        table_matches = table_pattern.findall(full)
        if table_matches:
            logger.info(f"[表格检测] 发现 {len(table_matches)} 个Markdown表格，自动生成图表")
            for i, tbl_match in enumerate(table_matches):
                # 提取表格第一行作为图表描述
                first_row = tbl_match.split('\n')[0]
                cells = [c.strip() for c in first_row.split('|') if c.strip()]
                tbl_desc = f"数据对比：{' vs '.join(cells[:3])}"
                if tbl_desc not in markers:
                    markers.append(tbl_desc)

        if not markers:
            return

        from memory import OllamaVectorStore
        evidence_materials = all_materials[:30] if all_materials else []
        mini_store = None
        if evidence_materials:
            try:
                mini_store = OllamaVectorStore(
                    self.cfg.get("ollama_host", "http://localhost:11434"),
                    self.cfg.get("ollama_model", "bge-m3:latest")
                )
                texts = [f"{m.get('title','')} {m.get('content','')}" for m in evidence_materials]
                metas = [{"idx": i, "content": m.get("content", "")} for i, m in enumerate(evidence_materials)]
                mini_store.add(texts, metas)
            except Exception as e:
                logger.warning(f"[图表证据] 向量索引失败，改用空证据: {e}")
                mini_store = None

        for i, desc in enumerate(markers[:10], 1):
            related = []
            if mini_store:
                try:
                    related = mini_store.search(desc, top_k=5)
                except Exception as e:
                    logger.debug(f"[图表证据] 检索失败: {e}")
            evidence = "\n".join([f"[{r['meta']['idx']}] {smart_truncate(r['text'], 500)}" for r in related])
            result = self.chart_gen.generate(i, desc, evidence)
            if result:
                png_path, html_path = result
                self.chart_paths.append(("image", png_path, desc))
                if html_path:
                    self.chart_data.append({"title": desc, "html_file": html_path, "type": "advanced"})
            else:
                logger.info(f"[图表跳过] {desc} 无数据，不插入占位符")

    def _generate_executive_summary(self, chapters_md: List[str]) -> str:
        """精炼TLDR ≤150字，适合封面页展示。"""
        if not chapters_md:
            return ""
        combined = ""
        for ch in chapters_md:
            para = ch.split('\n\n')[0][:200] if ch else ""
            if para:
                combined += para + "\n"
        combined = smart_truncate(combined, 4000)
        prompt = f"""【任务】从报告各章首段提取核心发现
【输入】报告各章首段内容

【提取规则】
1. 提取3-5条核心发现
2. 每条≤30字，必须包含1个具体数据点
3. 最后给出1条一句话行动建议
4. 严禁空话，必须用报告中最接近的实际数据

【输出格式】
• 核心发现1（含具体数据）
• 核心发现2（含具体数据）
• 核心发现3（含具体数据）
→ 行动建议（一句话，基于数据）

【示例】
• 市场规模约5000亿元
• 第一梯队占比62%
→ 建议对标头部企业CDM模式"""


        try:
            result = self.llm.editor([{"role":"user","content":prompt}], max_tokens=400)
            result = result.strip()
            if len(result) < 20: raise ValueError("TLDR too short")
            return result
        except Exception as e:
            logger.warning(f"[核心发现生成失败] {e}")
            snippets = []
            for ch in chapters_md:
                first = ch.split('\n\n')[0].strip(); first = re.sub(r'^#{1,3}\s*第[^章]+章\s*','',first); first = re.sub(r'^#{1,3}\s*','',first)
                if len(first)>20: snippets.append(first[:120])
            return " • ".join(snippets)[:200] if snippets else "本报告基于互联网公开信息自动生成。"

    def generate_excel_appendix(self, all_materials: List[Dict], report_path: str = ""):
        if not EXCEL_AVAILABLE or not self.cfg.get("enable_excel_appendix", True):
            return
        logger.info("[生成 Excel 数据附录]")
        wb = Workbook()
        ws = wb.active
        ws.title = "数据来源"
        ws.append(["引用编号", "标题", "URL", "内容摘要", "来源类型", "LLM评分", "可靠度"])
        for i, m in enumerate(all_materials, 1):
            ws.append([
                f"[{i}]", m.get("title", ""), m.get("url", ""),
                str(m.get("content", ""))[:500], m.get("source_type", "web"),
                m.get("llm_score", ""), m.get("reliability", "medium")
            ])
        # 与报告同名保存
        if report_path:
            xlsx_path = os.path.splitext(report_path)[0] + "_数据来源.xlsx"
        else:
            xlsx_path = os.path.join(self.output_dir, "data_appendix.xlsx")
        wb.save(xlsx_path)
        logger.info(f"[Excel] {xlsx_path}")

    def generate_excel_appendix_with_traceability(self, data_points: List[Dict], report_path: str = "") -> str:
        """
        生成带溯源信息的Excel附录

        每行包含：
        - 数据项名称
        - 数值
        - 单位
        - 年份
        - 数据来源（URL或用户上传标记）
        - 信源等级（S/A/B/C）
        - 置信度（high/medium/low）
        - 验证状态
        - 有效期
        """
        if not EXCEL_AVAILABLE:
            logger.warning("[Excel溯源] openpyxl未安装，跳过生成")
            return ""

        logger.info("[生成 Excel 溯源数据附录]")

        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter

        wb = Workbook()
        ws = wb.active
        ws.title = "数据溯源"

        # 定义样式
        header_fill = PatternFill(start_color="2E75B6", end_color="2E75B6", fill_type="solid")
        header_font = Font(bold=True, color="FFFFFF", size=11)
        header_alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

        tier_colors = {
            "S": "C6EFCE",  # 绿色
            "A": "FFEB9C",  # 黄色
            "B": "FFC7CE",  # 浅红
            "C": "FF6B6B"   # 红色
        }

        confidence_colors = {
            "high": "C6EFCE",
            "medium": "FFEB9C",
            "low": "FFC7CE"
        }

        # 设置表头
        headers = [
            "序号", "数据项名称", "数值", "单位", "年份",
            "数据来源", "信源等级", "置信度", "验证状态", "有效期", "搜索查询", "素材编号"
        ]
        ws.append(headers)

        # 应用表头样式
        for col_num, header in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col_num)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = header_alignment

        # 填充数据行
        for i, dp in enumerate(data_points, 1):
            row_data = [
                i,
                dp.get("name", ""),
                dp.get("value", ""),
                dp.get("unit", ""),
                dp.get("year", ""),
                dp.get("source", ""),
                dp.get("tier", "B"),
                dp.get("confidence", "medium"),
                dp.get("verification_status", "待验证"),
                dp.get("valid_until", ""),
                dp.get("search_query", ""),
                dp.get("material_index", "")
            ]
            ws.append(row_data)

            # 应用条件格式样式
            row_num = i + 1

            # 信源等级颜色
            tier_cell = ws.cell(row=row_num, column=7)
            tier = dp.get("tier", "B")
            if tier in tier_colors:
                tier_cell.fill = PatternFill(start_color=tier_colors[tier], end_color=tier_colors[tier], fill_type="solid")

            # 置信度颜色
            conf_cell = ws.cell(row=row_num, column=8)
            confidence = dp.get("confidence", "medium")
            if confidence in confidence_colors:
                conf_cell.fill = PatternFill(start_color=confidence_colors[confidence], end_color=confidence_colors[confidence], fill_type="solid")

        # 设置列宽
        column_widths = {
            "A": 6,   # 序号
            "B": 25,  # 数据项名称
            "C": 15,  # 数值
            "D": 10,  # 单位
            "E": 10,  # 年份
            "F": 40,  # 数据来源
            "G": 10,  # 信源等级
            "H": 10,  # 置信度
            "I": 12,  # 验证状态
            "J": 12,  # 有效期
            "K": 30,  # 搜索查询
            "L": 10   # 素材编号
        }

        for col_letter, width in column_widths.items():
            ws.column_dimensions[col_letter].width = width

        # 冻结首行
        ws.freeze_panes = "A2"

        # 添加筛选
        ws.auto_filter.ref = ws.dimensions

        # 保存文件
        if report_path:
            xlsx_path = os.path.splitext(report_path)[0] + "_数据溯源.xlsx"
        else:
            xlsx_path = os.path.join(self.output_dir, f"data_traceability_{datetime.now().strftime('%m%d_%H%M')}.xlsx")

        wb.save(xlsx_path)
        logger.info(f"[Excel溯源] {xlsx_path}")
        return xlsx_path

    def generate_dashboard(self):
        if not self.chart_data:
            return
        logger.info("[生成交互仪表盘]")
        items = "\n".join([
            f'<div class="chart-card"><h3>{c["title"]}</h3>'
            f'<iframe src="{os.path.basename(c["html_file"])}" width="100%" height="400"></iframe></div>'
            for c in self.chart_data
        ])
        html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>数据看板</title>
<style>body{{font-family:"Microsoft YaHei";background:#f5f7fa;padding:20px}}h1{{color:#2E75B6}}
.chart-card{{background:white;border-radius:8px;padding:15px;margin:15px 0;box-shadow:0 2px 10px rgba(0,0,0,0.05)}}
iframe{{border:none;border-radius:4px}}</style>
</head><body><h1>📊 交互式数据看板</h1>{items}</body></html>"""
        dash_path = os.path.join(self.output_dir, "dashboard.html")
        with open(dash_path, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info(f"[仪表盘] {dash_path}")

    def compile_word(self, report_title: str, chapters_md: List[str], action_plan: str,
                     task_id: str = "", source_appendix: str = "") -> str:
        logger.info("[组装 Word 文档 ]")
        # 按任务ID组织输出目录
        if task_id:
            task_dir = os.path.join(self.output_dir, "cache", task_id)
            client_dir = os.path.join(task_dir, "交付客户")
            debug_dir = os.path.join(task_dir, "后台调试")
            os.makedirs(client_dir, exist_ok=True)
            os.makedirs(debug_dir, exist_ok=True)
            orig_output = self.output_dir
            self.output_dir = client_dir
        else:
            task_dir = client_dir = debug_dir = orig_output = None

        # ====== 构建完整 Markdown 文本（供 pandoc 使用） ======
        md_parts = []

        # 封面页
        md_parts.append(f"# {report_title}\n")
        md_parts.append("**基于互联网公开信息的自动化桌面研究报告**\n")
        md_parts.append("")

        # TLDR
        summary = self._generate_executive_summary(chapters_md)
        if summary:
            md_parts.append(f"## 核心发现\n\n{summary}\n")

        md_parts.append(f"\n**生成日期：{datetime.now().strftime('%Y年%m月%d日')}**\n")
        md_parts.append("\n*本报告由AI自动生成，仅供参考*\n")
        md_parts.append("\n---\n")  # 分页符（pandoc 支持）

        # 章节内容 + 自动编号
        chapter_no = 0
        for i, ch_text in enumerate(chapters_md):
            lines = ch_text.split("\n")
            for j, line in enumerate(lines):
                stripped = line.strip()
                if stripped.startswith("### ") or stripped.startswith("## "):
                    prefix = "###" if stripped.startswith("### ") else "##"
                    old_title = stripped[len(prefix) + 1:]
                    old_title = re.sub(r'^第[一二三四五六七八九十百\d]+章[\s:：、.]*', '', old_title).strip()
                    # 摘要章与参考文献不参与正文章节编号
                    if (i == 0 and ("摘要" in old_title or "概要" in old_title or "发现" in old_title)) \
                            or "参考文献" in old_title:
                        lines[j] = f"{prefix} {old_title}"
                    else:
                        chapter_no += 1
                        cn_num = ["一","二","三","四","五","六","七","八","九","十"][chapter_no-1] if chapter_no <= 10 else str(chapter_no)
                        lines[j] = f"{prefix} 第{cn_num}章  {old_title}"
                    break
            md_parts.append("\n".join(lines))

        # 图表引用（以文本形式嵌入）
        chart_idx = 0
        for i, ch_text in enumerate(chapters_md):
            for line in ch_text.split("\n"):
                match = re.search(r"【图表：(.*?)】", line.strip())
                if match:
                    desc = match.group(1)
                    if chart_idx < len(self.chart_paths):
                        entry = self.chart_paths[chart_idx]
                        img_type = entry[0]
                        img_data = entry[1]
                        if img_type == "image" and img_data and os.path.exists(img_data):
                            # pandoc 支持图片嵌入语法
                            md_parts.append(f"\n![{desc}]({img_data})\n")
                        elif img_type == "mermaid" and isinstance(img_data, str) and os.path.exists(img_data):
                            with open(img_data, 'r', encoding='utf-8') as mf:
                                mermaid_code = mf.read()
                            md_parts.append(f"\n**[Mermaid图表：{desc}]**\n\n```\n{mermaid_code}\n```\n")
                    chart_idx += 1

        # 行动建议
        if action_plan and action_plan.strip():
            md_parts.append("\n---\n")
            md_parts.append(f"\n# 行动建议\n\n{action_plan}\n")

        # 附加来源追溯表（Claim Ledger 生成）
        if source_appendix and source_appendix.strip():
            md_parts.append("\n---\n")
            md_parts.append(f"\n# 来源追溯表\n\n{source_appendix}\n")

        full_md = "\n".join(md_parts)

        safe = re.sub(r'[^\w\u4e00-\u9fa5]', '_', report_title)[:40]
        path = os.path.join(self.output_dir, f"{safe}_{datetime.now().strftime('%m%d_%H%M')}.docx")

        # ====== 优先使用 pandoc ======
        pandoc_success = False
        try:
            from shared.md_to_docx import markdown_to_docx
            pandoc_success = markdown_to_docx(full_md, path)
        except Exception as e:
            logger.warning(f"[pandoc] 调研报告Word导出失败: {e}，回退到 python-docx")

        # ====== 降级到 python-docx（仅在 pandoc 失败时） ======
        if not pandoc_success:
            doc = Document()
            compiler = MarkdownDocxCompiler(doc)

            # 封面页
            title_para = doc.add_paragraph()
            title_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = title_para.add_run(report_title)
            run.font.size = Pt(26)
            run.bold = True
            run.font.name = "微软雅黑"
            run._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")

            subtitle = doc.add_paragraph()
            subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
            sub_run = subtitle.add_run("基于互联网公开信息的自动化桌面研究报告")
            sub_run.font.size = Pt(12)
            sub_run.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
            sub_run.font.name = "微软雅黑"
            sub_run._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")

            doc.add_paragraph()

            summary = self._generate_executive_summary(chapters_md)
            if summary:
                tldr_title = doc.add_paragraph()
                tldr_title.alignment = WD_ALIGN_PARAGRAPH.CENTER
                tldr_run = tldr_title.add_run("核心发现")
                tldr_run.bold = True
                tldr_run.font.size = Pt(14)
                tldr_run.font.name = "微软雅黑"
                tldr_run._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")
                tldr_p = doc.add_paragraph()
                tldr_p.alignment = WD_ALIGN_PARAGRAPH.LEFT
                tldr_text = tldr_p.add_run(summary)
                tldr_text.font.size = Pt(10)
                tldr_text.font.name = "微软雅黑"
                tldr_text._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")

            doc.add_paragraph()
            date_p = doc.add_paragraph()
            date_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            date_run = date_p.add_run(f"生成日期：{datetime.now().strftime('%Y年%m月%d日')}")
            date_run.font.size = Pt(9)
            date_run.font.color.rgb = RGBColor(0x99, 0x99, 0x99)
            date_run.font.name = "微软雅黑"
            date_run._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")

            disc_p = doc.add_paragraph()
            disc_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            disc_run = disc_p.add_run("本报告由AI自动生成，仅供参考")
            disc_run.font.size = Pt(8)
            disc_run.font.color.rgb = RGBColor(0xAA, 0xAA, 0xAA)
            disc_run.font.name = "微软雅黑"
            disc_run._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")

            doc.add_page_break()

            # 章节编号
            chapters_with_numbers = []
            chapter_no = 0
            for i, ch_text in enumerate(chapters_md):
                lines = ch_text.split("\n")
                for j, line in enumerate(lines):
                    stripped = line.strip()
                    if stripped.startswith("### ") or stripped.startswith("## "):
                        prefix = "###" if stripped.startswith("### ") else "##"
                        old_title = stripped[len(prefix) + 1:]
                        old_title = re.sub(r'^第[一二三四五六七八九十百\d]+章[\s:：、.]*', '', old_title).strip()
                        if (i == 0 and ("摘要" in old_title or "概要" in old_title or "发现" in old_title)) \
                                or "参考文献" in old_title:
                            lines[j] = f"{prefix} {old_title}"
                        else:
                            chapter_no += 1
                            cn_num = ["一","二","三","四","五","六","七","八","九","十"][chapter_no-1] if chapter_no <= 10 else str(chapter_no)
                            lines[j] = f"{prefix} 第{cn_num}章  {old_title}"
                        break
                chapters_with_numbers.append("\n".join(lines))

            chart_idx = 0
            for ch_text in chapters_with_numbers:
                lines = ch_text.split("\n")
                i = 0
                while i < len(lines):
                    line = lines[i]
                    stripped = line.strip()

                    chart_match = re.search(r"【图表：(.*?)】", stripped)
                    if chart_match:
                        desc = chart_match.group(1)
                        if chart_idx < len(self.chart_paths):
                            entry = self.chart_paths[chart_idx]
                            img_type = entry[0]
                            img_data = entry[1]
                            if img_type == "mermaid" or (isinstance(img_data, str) and img_data.endswith('.mmd')):
                                if os.path.exists(img_data):
                                    with open(img_data, 'r', encoding='utf-8') as mf:
                                        mermaid_code = mf.read()
                                    code_para = doc.add_paragraph()
                                    code_run = code_para.add_run(f"[Mermaid图表：{desc}]\n{mermaid_code}")
                                    code_run.font.size = Pt(8)
                                    code_run.font.name = "Consolas"
                            elif img_type == "image" and img_data and os.path.exists(img_data):
                                p = doc.add_paragraph()
                                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                                p.add_run().add_picture(img_data, width=Inches(5.8))
                            # 删除不可达的elif分支（第1101行已覆盖mermaid文件路径情况）
                            chart_idx += 1
                        i += 1
                        continue

                    buffer_lines = []
                    while i < len(lines):
                        l = lines[i]
                        if re.search(r"【图表：(.*?)】", l.strip()):
                            break
                        buffer_lines.append(l)
                        i += 1
                    if buffer_lines:
                        compiler.add_markdown("\n".join(buffer_lines))

            if action_plan and action_plan.strip():
                doc.add_page_break()
                doc.add_heading("行动建议", level=1)
                compiler.add_markdown(action_plan)

            doc.save(path)

        logger.info(f"[完成] Word 已保存：{path}")

        # 保存调试文件 + 恢复output_dir
        if task_dir:
            md_path = os.path.join(debug_dir, "report_full.md")
            with open(md_path, 'w', encoding='utf-8') as f:
                f.write(full_md)
            self.output_dir = orig_output
        return path

    # ────────────────────────────────────────────────────────────
    # 精炼报告（同步产出给用户，三种模式不同页数）
    # ────────────────────────────────────────────────────────────

    def compile_brief(self, report_title: str, chapters_md: List[str],
                      action_plan: str, mode: str = "regular",
                      task_id: str = "") -> str:
        """
        生成精炼版报告（Word 格式，pandoc 优先 + python-docx 降级）

        页数限制：regular ≤1-3页, deep ≤3-5页, breadth ≤5-7页

        Args:
            report_title: 报告标题
            chapters_md: 章节内容列表
            action_plan: 行动建议
            mode: 调研模式（regular/deep/breadth）
            task_id: 任务ID

        Returns:
            精炼版 Word 文件路径
        """
        # 页数 → 字数映射（约800字/页）
        page_limits = {"regular": 3, "deep": 5, "breadth": 7}
        max_pages = page_limits.get(mode, 5)
        max_chars = max_pages * 800

        # 拼合完整报告
        full_report = f"# {report_title}\n\n"
        for i, ch in enumerate(chapters_md):
            full_report += ch + "\n\n"
        if action_plan:
            full_report += f"# 行动建议\n\n{action_plan}\n"

        # 截断过长报告（LLM 上下文有限）
        report_for_llm = full_report[:12000]

        prompt = f"""【任务】将以下调研报告压缩为精炼版，控制在 {max_pages} 页以内（约 {max_chars} 字）。

【输出结构】
## 核心结论
- 一句话总结（1行）
- 发现1（含具体数据，≤50字）
- 发现2（含具体数据，≤50字）
- 发现3（含具体数据，≤50字）
{"- 发现4（含具体数据，≤50字）" if max_pages >= 5 else ""}
{"- 发现5（含具体数据，≤50字）" if max_pages >= 7 else ""}

## 关键数据速览
| 指标 | 数值 | 来源/说明 |
|------|------|-----------|
（提取5-10个最关键的量化指标）

## 行动建议
1. **优先级高**：建议 + 理由（1-2句）
2. **优先级中**：建议 + 理由（1-2句）
3. **优先级低**：建议 + 理由（1-2句）

【输入报告】
{report_for_llm}"""

        try:
            brief = self.llm.editor(
                [{"role": "user", "content": prompt}],
                max_tokens=min(max_chars * 2, 4000),
            )
        except Exception as e:
            logger.warning(f"[精炼报告] LLM 调用失败: {e}")
            return ""

        if not brief:
            return ""

        # 构建精炼版 Markdown（用于 pandoc 或 python-docx）
        md_parts = []
        md_parts.append(f"# {report_title}（精炼版）\n")
        md_parts.append(f"**调研模式：{mode} | 生成日期：{datetime.now().strftime('%Y年%m月%d日')}**\n")
        md_parts.append("\n*本报告由AI自动生成，仅供参考*\n")
        md_parts.append("\n---\n\n")
        md_parts.append(brief)

        brief_md = "\n".join(md_parts)

        # 确定输出目录
        safe = re.sub(r'[^\w\u4e00-\u9fa5]', '_', report_title)[:40]
        if task_id:
            task_dir = os.path.join(self.output_dir, "cache", task_id)
            client_dir = os.path.join(task_dir, "交付客户")
            os.makedirs(client_dir, exist_ok=True)
            brief_dir = client_dir
        else:
            brief_dir = self.output_dir

        docx_path = os.path.join(brief_dir,
            f"{safe}_{datetime.now().strftime('%m%d_%H%M')}_精炼版.docx")

        # ====== 优先使用 pandoc ======
        pandoc_success = False
        try:
            from shared.md_to_docx import markdown_to_docx
            pandoc_success = markdown_to_docx(brief_md, docx_path)
        except Exception as e:
            logger.warning(f"[精炼报告] pandoc 导出失败: {e}，回退到 python-docx")

        # ====== 降级到 python-docx ======
        if not pandoc_success:
            doc = Document()

            # 标题
            title_para = doc.add_paragraph()
            title_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = title_para.add_run(f"{report_title}（精炼版）")
            run.font.size = Pt(18)
            run.bold = True
            run.font.name = "微软雅黑"
            run._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")

            # 副标题
            sub = doc.add_paragraph()
            sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
            sub_run = sub.add_run(f"调研模式：{mode} | 生成日期：{datetime.now().strftime('%Y年%m月%d日')}")
            sub_run.font.size = Pt(10)
            sub_run.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
            sub_run.font.name = "微软雅黑"
            sub_run._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")

            disc = doc.add_paragraph()
            disc.alignment = WD_ALIGN_PARAGRAPH.CENTER
            disc_run = disc.add_run("本报告由AI自动生成，仅供参考")
            disc_run.font.size = Pt(8)
            disc_run.font.color.rgb = RGBColor(0xAA, 0xAA, 0xAA)
            disc_run.font.name = "微软雅黑"
            disc_run._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")

            doc.add_paragraph()

            # 正文：按 Markdown 标题/段落渲染
            compiler = MarkdownDocxCompiler(doc)
            for line in brief.split("\n"):
                stripped = line.strip()
                if stripped.startswith("## "):
                    doc.add_heading(stripped[3:], level=1)
                elif stripped.startswith("### "):
                    doc.add_heading(stripped[4:], level=2)
                elif stripped.startswith("- ") or stripped.startswith("* "):
                    doc.add_paragraph(stripped, style='List Bullet')
                elif stripped.startswith("| "):
                    # 表格行：python-docx 表格渲染
                    continue  # 精炼版表格用 MarkdownDocxCompiler 处理
                elif stripped:
                    compiler.add_markdown(stripped)

            doc.save(docx_path)

        logger.info(f"[精炼报告] Word 已保存：{docx_path}（{mode}模式，≤{max_pages}页）")
        return docx_path
