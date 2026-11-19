#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小醉桌面研究 v1.0 / framework_loader.py
框架解析引擎
- 从文本涌现调研框架
- 解析结果注入engine替代planner输出
- 框架预览导出
"""

import os, re
from typing import List, Dict, Optional
from dataclasses import dataclass, field

from config import logger


@dataclass
class ResearchFramework:
    """调研框架 —— 替代planner输出的结构化调研计划"""
    industry_name: str = ""
    industry_definition: str = ""
    boundary_table: str = ""
    behavior_profile: List[str] = field(default_factory=list)
    entities: List[Dict] = field(default_factory=list)
    tier_criteria: str = ""
    dimensions: List[Dict] = field(default_factory=list)
    data_sources: List[Dict] = field(default_factory=list)
    口径_declaration: str = ""
    deliverable_template: str = ""
    search_batches: List[Dict] = field(default_factory=list)
    core_questions: List[str] = field(default_factory=list)
    common_mistakes: List[str] = field(default_factory=list)
    compliance_notes: str = ""
    source_file: str = ""

    def is_valid(self) -> bool:
        return ((len(self.entities) >= 3 or len(self.dimensions) >= 3) and
                len(self.search_batches) >= 1) or \
               (bool(self.industry_definition) and len(self.entities) >= 1)

    def to_markdown(self) -> str:
        md = f"# 调研框架：{self.industry_name}\n\n"
        if self.industry_definition:
            md += f"## 行业定义\n{self.industry_definition}\n\n"
        if self.boundary_table:
            md += f"## 边界区分\n{self.boundary_table}\n\n"
        if self.behavior_profile:
            md += "## 典型行为画像\n"
            for i, b in enumerate(self.behavior_profile, 1):
                md += f"{i}. {b}\n"
            md += "\n"
        if self.tier_criteria:
            md += f"## 分类标准\n{self.tier_criteria}\n\n"
        if self.entities:
            md += "## 企业清单\n"
            for e in self.entities:
                t = e.get('tier', '')
                ts = f" ({t})" if t else ""
                md += f"- **{e['name']}**{ts}"
                if e.get('notes'):
                    md += f"：{e['notes']}"
                md += "\n"
            md += "\n"
        if self.data_sources:
            md += "## 关键数据口径\n"
            for d in self.data_sources:
                md += f"- {d.get('source','')}（{d.get('credibility','')}）：{d.get('notes','')}\n"
            md += "\n"
        if self.dimensions:
            md += "## 调研维度\n"
            for d in self.dimensions:
                md += f"- **{d.get('name','')}**：{d.get('description','')}\n"
            md += "\n"
        if self.common_mistakes:
            md += "## 常见误区\n"
            for m in self.common_mistakes:
                md += f"- {m}\n"
            md += "\n"
        return md


# ============================================================
# 框架解析
# ============================================================

def _parse_framework(content: str, source_file: str = "") -> ResearchFramework:
    fw = ResearchFramework(source_file=source_file)

    # 标题
    tm = re.search(r'#\s+(.+?)(?:\n|$)', content)
    if tm:
        t = tm.group(1).strip()
        t = re.sub(r'[（(]?V?\d[\d.]*[)）]?.*$', '', t)
        t = re.sub(r'(深度|超深度)?调研(规划|报告)?', '', t).strip()
        fw.industry_name = t[:40]

    # 行业定义
    ds = _extract_section(content, r'##?\s*(?:行业定义|什么是|定义)')
    if ds:
        fw.industry_definition = ds[:800]

    # 边界区分
    bd = _extract_section(content, r'##?\s*(?:边界区分|概念区分|与.*区别)')
    if bd:
        fw.boundary_table = bd[:1500]

    # 行为画像
    bh = _extract_section(content, r'##?\s*(?:典型行为|行为画像|特征)')
    if bh:
        fw.behavior_profile = re.findall(r'\d+[\.\、]\s*(.+)', bh)[:8]

    # 企业清单（表格优先）
    for row in re.findall(r'\|[^|]+\|', content):
        cells = [c.strip() for c in row.split('|') if c.strip()]
        if len(cells) >= 2 and re.match(r'^[\u4e00-\u9fa5·]{2,15}$', cells[0]):
            if not any(kw in cells[0] for kw in ['企业','公司','品牌','指标','概念','模式']):
                e = {"name": cells[0]}
                for c in cells[1:]:
                    if re.search(r'第[一二三]梯|头部|腰部|长尾|全球|中国', c):
                        e["tier"] = c[:30]
                    if re.search(r'亿|增速|复购|流失', c):
                        e["notes"] = c[:50]
                if e["name"] not in [x["name"] for x in fw.entities]:
                    fw.entities.append(e)

    # 列表回退
    if len(fw.entities) < 3:
        for e in re.findall(r'[-*]\s*\*{0,2}([\u4e00-\u9fa5·]{2,12}(?:公司|集团|企业|品牌)?)\*{0,2}', content):
            if e.strip() not in [x["name"] for x in fw.entities] and len(e.strip()) >= 2:
                fw.entities.append({"name": e.strip()})
    fw.entities = fw.entities[:50]

    # 分类标准
    tc = _extract_section(content, r'##?\s*(?:分类标准|梯队|分档|划分)')
    if tc:
        fw.tier_criteria = tc[:600]

    # 调研维度
    dm = _extract_section(content, r'##?\s*(?:调研维度|维度|调研内容)')
    if dm:
        di = re.findall(r'[-*]\s*\*{0,2}(.+?)[：:](.+?)(?:\n|$)', dm)
        for n, d in di[:10]:
            fw.dimensions.append({"name": n.strip(), "description": d.strip()[:200]})
        if not fw.dimensions:
            for d in re.findall(r'[-*]\s*(.{5,80})', dm)[:10]:
                fw.dimensions.append({"name": d.strip()[:30], "description": d.strip()})

    # 数据口径
    kj = _extract_section(content, r'##?\s*(?:数据口径|口径|数据来源|信息源)')
    if kj:
        fw.口径_declaration = kj[:1200]
        for row in re.findall(r'\|\s*(.+?)\s*\|\s*(.{0,20})\s*\|\s*(.{0,10})\s*\|', kj)[:10]:
            fw.data_sources.append({"source": row[0].strip(), "credibility": row[2].strip() if len(row)>2 else "", "notes": row[1].strip() if len(row)>1 else ""})

    # 交付模板
    tp = _extract_section(content, r'##?\s*(?:交付模板|输出模板|档案模板)')
    if tp:
        fw.deliverable_template = tp[:1500]

    # 搜索批次
    bs = _extract_section(content, r'##?\s*(?:搜索批次|搜索规划|批次规划)')
    if bs:
        for block in re.split(r'批次\s*[\d一二三四五六七八九十]+', bs)[1:]:
            qs = re.findall(r'[-*\d]+[\.\、]?\s*(.{10,100})', block)
            if qs:
                fw.search_batches.append({"description": block.split('\n')[0][:50] if block else "", "queries": qs[:8]})

    # 核心追问
    cq = _extract_section(content, r'##?\s*(?:核心追问|核心问题|关键问题)')
    if cq:
        fw.core_questions = re.findall(r'[-*\d]+[\.\、]?\s*(.{10,200})', cq)[:5]

    # 常见误区
    ms = _extract_section(content, r'##?\s*(?:常见误区|误区|注意事项|特别提醒)')
    if ms:
        fw.common_mistakes = re.findall(r'[-*\d]+[\.\、]?\s*(.{10,200})', ms)[:8]

    # 合规
    cp = _extract_section(content, r'##?\s*(?:合规|风险|监管)')
    if cp and len(cp) > 50:
        fw.compliance_notes = cp[:800]

    logger.info(f"[框架解析] 行业={fw.industry_name} | 企业={len(fw.entities)} | 维度={len(fw.dimensions)} | 批次={len(fw.search_batches)} | 口径={'有' if fw.口径_declaration else '无'}")
    return fw


def _extract_section(content: str, header_pattern: str) -> str:
    m = re.search(header_pattern, content, re.IGNORECASE)
    if not m:
        return ""
    rest = content[m.end():]
    nh = re.search(r'\n##?\s', rest)
    return rest[:nh.start()].strip() if nh else rest.strip()


# ============================================================
# 框架涌现（LLM模式）
# ============================================================

def load_framework_from_text(text: str, llm=None) -> Optional[ResearchFramework]:
    """从用户需求文本涌现框架（入口A）。llm=None时降级启发式解析。"""
    if llm is None:
        return _parse_framework(text)

    prompt = f"""【角色】资深行业分析师
【任务】从调研需求中提取结构化调研框架

【需求文本】{text[:5000]}

【提取原则】
1. 只提取需求中明确提到的信息，不编造
2. 无法提取的字段留空数组或空字符串
3. 企业必须包含梯队信息
4. 调研维度必须包含说明

【输出要求】
- 只输出纯JSON对象，不要解释文字
- 标准格式：
{{"industry_name":"行业名","industry_definition":"行业定义（100字）","boundary_table":"边界区分","behavior_profile":["行为特征1","行为特征2"],"entities":[{{"name":"企业名","tier":"第一/第二/第三梯队"}}],"tier_criteria":"梯队分类标准","dimensions":[{{"name":"维度名","description":"调研说明"}}],"data_sources":[{{"source":"来源名称","credibility":"high/medium/low"}}],"search_batches":[{{"description":"批次说明","queries":["查询1","查询2"]}}],"core_questions":["核心追问1","核心追问2"],"common_mistakes":["常见误区1","常见误区2"]}}"""
    try:
        from config import safe_json_loads
        raw = llm.plan([{"role":"user","content":prompt}], max_tokens=2500, json_mode=True)
        data = safe_json_loads(raw, {})
        if not isinstance(data, dict):
            return _parse_framework(text)

        fw = ResearchFramework()
        fw.industry_name = str(data.get("industry_name",""))[:40]
        fw.industry_definition = str(data.get("industry_definition",""))[:800]
        fw.boundary_table = str(data.get("boundary_table",""))[:1500]
        fw.behavior_profile = list(data.get("behavior_profile",[]))[:8]
        fw.tier_criteria = str(data.get("tier_criteria",""))[:600]
        fw.entities = list(data.get("entities",[]))[:50]
        fw.dimensions = list(data.get("dimensions",[]))[:12]
        fw.data_sources = list(data.get("data_sources",[]))[:10]
        fw.core_questions = list(data.get("core_questions",[]))[:5]
        fw.common_mistakes = list(data.get("common_mistakes",[]))[:8]
        for b in data.get("search_batches",[]) or []:
            if isinstance(b, dict):
                fw.search_batches.append({"description": str(b.get("description",""))[:50], "queries": list(b.get("queries",[]))[:8]})
        return fw
    except Exception as e:
        logger.warning(f"[框架涌现] LLM失败，降级启发式: {e}")
        return _parse_framework(text)


# ============================================================
# 框架模板系统
# ============================================================

def list_templates():
    """返回所有可用模板"""
    templates_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
    if not os.path.isdir(templates_dir):
        return []
    tmpls = []
    for fn in sorted(os.listdir(templates_dir)):
        if fn.endswith(".md"):
            name = fn.split("_", 1)[1].replace(".md", "") if "_" in fn else fn.replace(".md", "")
            tmpls.append({"id": fn, "name": name})
    return tmpls

def load_template(template_id, variables=None):
    """加载模板并替换变量"""
    templates_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
    path = os.path.join(templates_dir, template_id)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    if variables:
        for key, value in variables.items():
            content = content.replace("{" + key + "}", str(value))
    from framework_loader import load_framework_from_text
    return load_framework_from_text(content)
