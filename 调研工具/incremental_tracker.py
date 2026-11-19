#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
增量追踪模式
基于历史报告做差异化更新（Q2更新Q1）
"""

import os
import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Set, Tuple
from pathlib import Path

from config import logger, SearchResult, LLMClient, safe_json_loads, AGENT_PROMPTS


@dataclass
class HistoricalReport:
    """历史报告资产"""
    task_id: str
    topic: str
    created_at: datetime
    skeleton: List[Dict]  # 历史骨架结构
    core_facts: List[Dict]  # 核心事实（带时间戳）
    data_points: List[Dict]  # 数据口径
    references: List[Dict]  # 参考文献
    report_path: Optional[str] = None  # 报告文件路径
    version: str = "1.0"  # 版本号

    def to_dict(self) -> Dict:
        """序列化为字典"""
        return {
            "task_id": self.task_id,
            "topic": self.topic,
            "created_at": self.created_at.isoformat(),
            "skeleton": self.skeleton,
            "core_facts": self.core_facts,
            "data_points": self.data_points,
            "references": self.references,
            "report_path": self.report_path,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "HistoricalReport":
        """从字典反序列化"""
        return cls(
            task_id=data.get("task_id", ""),
            topic=data.get("topic", ""),
            created_at=datetime.fromisoformat(data.get("created_at", datetime.now().isoformat())),
            skeleton=data.get("skeleton", []),
            core_facts=data.get("core_facts", []),
            data_points=data.get("data_points", []),
            references=data.get("references", []),
            report_path=data.get("report_path"),
            version=data.get("version", "1.0"),
        )


@dataclass
class DiffResult:
    """对比结果"""
    continued: List[Dict] = field(default_factory=list)  # 未变化的核心结论
    updated: List[Dict] = field(default_factory=list)    # 数据变化的主题（带新旧对比）
    added: List[Dict] = field(default_factory=list)      # 新出现的热点/企业/趋势
    deprecated: List[Dict] = field(default_factory=list) # 已过时或被证伪的结论


class IncrementalTracker:
    """增量追踪器"""

    def __init__(self, cfg: Dict, llm: Optional[LLMClient] = None):
        self.cfg = cfg
        self.llm = llm
        self.archive_dir = Path(cfg.get("archive_dir", os.path.expanduser("~/.xiaozui/archive")))
        self.archive_dir.mkdir(parents=True, exist_ok=True)

    def _get_archive_path(self, task_id: str) -> Path:
        """获取归档文件路径"""
        return self.archive_dir / f"{task_id}.json"

    def save_historical_report(self, report: HistoricalReport) -> str:
        """保存历史报告资产"""
        archive_path = self._get_archive_path(report.task_id)
        with open(archive_path, "w", encoding="utf-8") as f:
            json.dump(report.to_dict(), f, ensure_ascii=False, indent=2)
        logger.info(f"[增量追踪] 历史报告已归档: {archive_path}")
        return str(archive_path)

    def load_historical_report(self, task_id: str) -> Optional[HistoricalReport]:
        """加载历史报告资产"""
        archive_path = self._get_archive_path(task_id)
        if not archive_path.exists():
            logger.warning(f"[增量追踪] 未找到历史报告: {task_id}")
            return None
        try:
            with open(archive_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return HistoricalReport.from_dict(data)
        except Exception as e:
            logger.error(f"[增量追踪] 加载历史报告失败: {e}")
            return None

    def list_historical_reports(self, topic_filter: Optional[str] = None) -> List[Dict]:
        """列出所有历史报告"""
        reports = []
        for file_path in self.archive_dir.glob("*.json"):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if topic_filter and topic_filter.lower() not in data.get("topic", "").lower():
                    continue
                reports.append({
                    "task_id": data.get("task_id"),
                    "topic": data.get("topic"),
                    "created_at": data.get("created_at"),
                    "version": data.get("version", "1.0"),
                })
            except Exception:
                continue
        return sorted(reports, key=lambda x: x.get("created_at", ""), reverse=True)

    def generate_incremental_queries(self, historical: HistoricalReport) -> List[str]:
        """生成增量搜索查询"""
        queries = []
        last_date = historical.created_at
        time_since = datetime.now() - last_date

        # 1. 时间过滤：优先搜索 "since: 上次报告日期"
        date_str = last_date.strftime("%Y年%m月")
        queries.append(f"{historical.topic} {date_str}以来 最新动态")
        from datetime import datetime
        current_year = datetime.now().year
        queries.append(f"{historical.topic} {current_year}年 新变化")

        # 2. 变更检测：搜索 "xxx 最新动态 / xxx 变化"
        for fact in historical.core_facts[:5]:
            subject = fact.get("subject", "")
            if subject:
                queries.append(f"{subject} 最新进展 {datetime.now().year}")
                queries.append(f"{subject} 变化 趋势")

        # 3. 数据验证：对历史时间敏感数据定向验证有效性
        for dp in historical.data_points:
            metric = dp.get("metric", "")
            value = dp.get("value", "")
            if metric and value:
                queries.append(f"{metric} 最新数据 {datetime.now().year}")
                queries.append(f"{metric} 更新 现状")

        # 4. 基于骨架章节生成查询
        for section in historical.skeleton[:3]:
            title = section.get("title", "")
            if title:
                queries.append(f"{title} {datetime.now().year} 最新")

        # 去重
        seen = set()
        unique_queries = []
        for q in queries:
            norm_q = re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9]', '', q.lower())
            if norm_q not in seen:
                seen.add(norm_q)
                unique_queries.append(q)

        logger.info(f"[增量追踪] 生成{len(unique_queries)}个增量查询")
        return unique_queries[:15]

    def _extract_fact_changes(self, historical_facts: List[Dict], new_materials: List[SearchResult]) -> Tuple[List[Dict], List[Dict], List[Dict]]:
        """提取事实变化"""
        continued = []
        updated = []
        deprecated = []

        historical_subjects = {f.get("subject", ""): f for f in historical_facts}
        new_subjects = set()

        # 分析新材料
        for mat in new_materials:
            content = mat.content.lower()
            for subject, fact in historical_subjects.items():
                if subject.lower() in content:
                    new_subjects.add(subject)
                    # 检查数据是否变化
                    old_value = fact.get("value", "")
                    if old_value and self._detect_value_change(old_value, mat.content):
                        updated.append({
                            "subject": subject,
                            "old_fact": fact,
                            "new_source": mat.title,
                            "new_content": mat.content[:300],
                            "change_type": "data_update"
                        })

        # 未提及的主题可能已过时
        for subject, fact in historical_subjects.items():
            if subject not in new_subjects and subject:
                deprecated.append({
                    "subject": subject,
                    "fact": fact,
                    "reason": "近期无相关报道，可能已过时"
                })
            elif subject in new_subjects:
                continued.append(fact)

        return continued, updated, deprecated

    def _detect_value_change(self, old_value: str, new_content: str) -> bool:
        """检测数值是否发生变化"""
        # 提取旧值中的数字
        old_nums = re.findall(r'\d+(?:\.\d+)?(?:[%万亿])?', old_value)
        if not old_nums:
            return False

        # 检查新内容中是否有不同数值
        new_nums = re.findall(r'\d+(?:\.\d+)?(?:[%万亿])?', new_content)
        for old_num in old_nums:
            # 如果旧数值在新内容中未出现，可能已更新
            if old_num not in new_nums:
                return True
        return False

    def _detect_new_trends(self, historical: HistoricalReport, new_materials: List[SearchResult]) -> List[Dict]:
        """检测新出现的热点/企业/趋势"""
        added = []

        # 提取历史中的实体
        historical_entities = set()
        for fact in historical.core_facts:
            entities = fact.get("entities", [])
            historical_entities.update(entities)

        # 分析新材料中的新实体
        for mat in new_materials:
            if not self.llm or not self.llm.available:
                continue

            prompt = f"""【任务】从素材中提取关键实体（企业、产品、趋势、政策等）
【素材标题】{mat.title}
【素材内容】{mat.content[:800]}

【输出要求】
- 只输出纯JSON数组，不要解释文字
- 格式：[{{"entity":"实体名称","type":"企业/产品/趋势/政策/其他","relevance":"高/中/低"}}]"""
            try:
                raw = self.llm.fast(
                    [{"role": "user", "content": prompt}],
                    system=AGENT_PROMPTS["evaluator"],
                    max_tokens=300, json_mode=True
                )
                entities = safe_json_loads(raw, [])
                for e in entities:
                    entity_name = e.get("entity", "")
                    if entity_name and entity_name not in historical_entities:
                        added.append({
                            "entity": entity_name,
                            "type": e.get("type", "其他"),
                            "relevance": e.get("relevance", "中"),
                            "source": mat.title,
                            "is_new": True
                        })
            except Exception:
                continue

        # 去重
        seen = set()
        unique_added = []
        for item in added:
            key = item.get("entity", "")
            if key and key not in seen:
                seen.add(key)
                unique_added.append(item)

        return unique_added

    def compare_and_diff(self, historical: HistoricalReport, new_materials: List[SearchResult]) -> DiffResult:
        """对比新旧素材，生成分类"""
        logger.info(f"[增量追踪] 开始对比分析: {historical.topic}")

        diff = DiffResult()

        # 1. 分析核心事实变化
        continued, updated, deprecated = self._extract_fact_changes(
            historical.core_facts, new_materials
        )
        diff.continued = continued
        diff.updated = updated
        diff.deprecated = deprecated

        # 2. 检测新趋势
        added = self._detect_new_trends(historical, new_materials)
        diff.added = added

        logger.info(f"[增量追踪] 对比完成: 延续{len(continued)} | 更新{len(updated)} | 新增{len(added)} | 失效{len(deprecated)}")
        return diff

    def generate_incremental_report(self, historical: HistoricalReport, diff: DiffResult, new_materials: List[SearchResult]) -> str:
        """生成差异化报告"""
        report_lines = [
            f"# 增量追踪报告：{historical.topic}",
            f"",
            f"**基准报告**: {historical.created_at.strftime('%Y年%m月%d日')} (v{historical.version})",
            f"**本次更新**: {datetime.now().strftime('%Y年%m月%d日')}",
            f"**更新周期**: {(datetime.now() - historical.created_at).days}天",
            f"",
            "---",
            f"",
        ]

        # 1. 延续部分
        report_lines.extend([
            "## 一、延续结论",
            "",
            "以下核心结论在最新调研中仍然有效，无需调整：",
            "",
        ])
        for fact in diff.continued[:10]:
            subject = fact.get("subject", "")
            value = fact.get("value", "")
            if subject:
                report_lines.append(f"- **{subject}**: {value}")
        if not diff.continued:
            report_lines.append("*暂无延续结论*")
        report_lines.append("")

        # 2. 更新部分
        report_lines.extend([
            "## 二、数据更新",
            "",
            "以下主题出现数据变化，建议更新报告：",
            "",
        ])
        for update in diff.updated[:10]:
            subject = update.get("subject", "")
            old = update.get("old_fact", {}).get("value", "")
            new = update.get("new_content", "")[:200]
            report_lines.extend([
                f"### {subject}",
                f"",
                f"**历史数据**: {old}",
                f"",
                f"**最新动态**: {new}",
                f"",
            ])
        if not diff.updated:
            report_lines.append("*暂无数据更新*")
        report_lines.append("")

        # 3. 新增部分
        report_lines.extend([
            "## 三、新增热点",
            "",
            "以下为新出现的热点、企业或趋势，建议补充：",
            "",
        ])
        for item in diff.added[:10]:
            entity = item.get("entity", "")
            etype = item.get("type", "")
            relevance = item.get("relevance", "")
            report_lines.append(f"- **{entity}** ({etype}) - 相关度: {relevance}")
        if not diff.added:
            report_lines.append("*暂无新增热点*")
        report_lines.append("")

        # 4. 失效部分
        report_lines.extend([
            "## 四、已失效结论",
            "",
            "以下结论可能已过时或被证伪，建议核实后删除：",
            "",
        ])
        for dep in diff.deprecated[:10]:
            subject = dep.get("subject", "")
            reason = dep.get("reason", "")
            report_lines.append(f"- **{subject}**: {reason}")
        if not diff.deprecated:
            report_lines.append("*暂无失效结论*")
        report_lines.append("")

        # 5. 行动建议
        report_lines.extend([
            "---",
            "",
            "## 五、行动建议",
            "",
        ])

        if diff.updated:
            report_lines.append(f"1. **数据更新**: {len(diff.updated)}个主题需要更新数据")
        if diff.added:
            report_lines.append(f"2. **内容补充**: {len(diff.added)}个新热点建议补充")
        if diff.deprecated:
            report_lines.append(f"3. **内容清理**: {len(diff.deprecated)}个过时结论建议核实删除")
        if not any([diff.updated, diff.added, diff.deprecated]):
            report_lines.append("*本次追踪未发现显著变化，建议保持现有报告内容*")

        report_lines.extend([
            "",
            "---",
            "",
            f"*报告生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*",
        ])

        return "\n".join(report_lines)

    def create_incremental_task(self, historical: HistoricalReport, diff: DiffResult) -> Dict:
        """基于差异创建增量任务配置"""
        # 构建增量搜索批次
        incremental_batches = []

        # 批次1：验证更新
        if diff.updated:
            queries = [f"{u.get('subject', '')} 最新数据 {datetime.now().year}" for u in diff.updated[:5]]
            incremental_batches.append({
                "description": "验证数据更新",
                "queries": queries,
                "max_results": 5,
            })

        # 批次2：探索新增
        if diff.added:
            queries = [f"{a.get('entity', '')} 详情 分析" for a in diff.added[:5]]
            incremental_batches.append({
                "description": "探索新增热点",
                "queries": queries,
                "max_results": 5,
            })

        # 批次3：补充搜索
        incremental_batches.append({
            "description": "增量补充搜索",
            "queries": self.generate_incremental_queries(historical)[:5],
            "max_results": 5,
        })

        return {
            "base_task_id": historical.task_id,
            "topic": historical.topic,
            "mode": "incremental",
            "incremental_batches": incremental_batches,
            "focus_areas": [u.get("subject", "") for u in diff.updated[:3]],
            "new_entities": [a.get("entity", "") for a in diff.added[:5]],
            "suggested_outline_updates": self._suggest_outline_updates(historical, diff),
        }

    def _suggest_outline_updates(self, historical: HistoricalReport, diff: DiffResult) -> List[Dict]:
        """建议大纲更新"""
        suggestions = []

        # 建议新增章节
        for item in diff.added[:3]:
            if item.get("relevance") == "高":
                suggestions.append({
                    "action": "add_section",
                    "title": f"{item.get('entity', '')}分析",
                    "reason": f"新出现的高相关度{item.get('type', '热点')}",
                })

        # 建议更新章节
        for update in diff.updated[:3]:
            suggestions.append({
                "action": "update_section",
                "title": update.get("subject", ""),
                "reason": "数据发生变化，需要更新",
            })

        return suggestions


class IncrementalReportBuilder:
    """增量报告构建器 - 用于合并历史报告和新素材"""

    def __init__(self, tracker: IncrementalTracker):
        self.tracker = tracker

    def build_merged_outline(self, historical: HistoricalReport, diff: DiffResult) -> Dict:
        """构建合并后的大纲"""
        outline = {
            "title": f"{historical.topic} - 增量更新",
            "sections": [],
            "metadata": {
                "base_version": historical.version,
                "base_date": historical.created_at.isoformat(),
                "update_type": "incremental",
            }
        }

        # 保留原有骨架
        for section in historical.skeleton:
            outline["sections"].append({
                **section,
                "status": "keep",
                "update_note": "",
            })

        # 标记需要更新的章节
        for update in diff.updated:
            subject = update.get("subject", "")
            for section in outline["sections"]:
                if subject.lower() in section.get("title", "").lower():
                    section["status"] = "update"
                    section["update_note"] = "数据已更新，需修订"

        # 添加新增章节
        for item in diff.added[:3]:
            if item.get("relevance") == "高":
                outline["sections"].append({
                    "title": f"{item.get('entity', '')}分析",
                    "status": "add",
                    "update_note": f"新增{item.get('type', '热点')}",
                })

        return outline

    def generate_merge_prompt(self, historical: HistoricalReport, new_content: str, diff: DiffResult) -> str:
        """生成合并提示词"""
        prompt = f"""【任务】基于历史报告和最新素材，生成增量更新版本

【历史报告主题】{historical.topic}
【历史报告日期】{historical.created_at.strftime('%Y年%m月%d日')}

【延续结论】（保持）
"""
        for fact in diff.continued[:5]:
            prompt += f"- {fact.get('subject', '')}: {fact.get('value', '')}\n"

        prompt += f"""
【需要更新】（替换为新数据）
"""
        for update in diff.updated[:5]:
            prompt += f"- {update.get('subject', '')}: 原数据→新数据\n"

        prompt += f"""
【新增内容】（补充）
"""
        for item in diff.added[:5]:
            prompt += f"- {item.get('entity', '')} ({item.get('type', '')})\n"

        prompt += f"""
【最新素材】
{new_content[:2000]}

【输出要求】
1. 保留历史报告的有效框架和结论
2. 用新数据替换过时的数据
3. 补充新增热点和企业
4. 删除已失效的内容
5. 输出完整的更新后报告
"""
        return prompt


# 便捷函数
def create_tracker(cfg: Dict, llm: Optional[LLMClient] = None) -> IncrementalTracker:
    """创建增量追踪器实例"""
    return IncrementalTracker(cfg, llm)


def quick_incremental_update(task_id: str, new_materials: List[SearchResult], cfg: Dict, llm: Optional[LLMClient] = None) -> str:
    """快速增量更新 - 一站式函数"""
    tracker = create_tracker(cfg, llm)

    # 加载历史报告
    historical = tracker.load_historical_report(task_id)
    if not historical:
        return f"错误：未找到历史报告 {task_id}"

    # 对比分析
    diff = tracker.compare_and_diff(historical, new_materials)

    # 生成报告
    report = tracker.generate_incremental_report(historical, diff, new_materials)

    return report
