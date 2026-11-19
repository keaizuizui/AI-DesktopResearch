"""
Compliance Check Module
统一合规检查模块 — 广告法、敏感词、行业规范

来源：方案工具 src/compliance/compliance_checker.py

设计原则：
- 通用规则库，支持自定义扩展
- 合规检查与事实核查形成双层检查
- 支持批量检查和报告生成
"""
import re
import json
import logging
from typing import Dict, List, Any, Optional, Set
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class ViolationLevel(Enum):
    """违规级别"""
    CRITICAL = "critical"    # 严重违规（必须修改）
    HIGH = "high"            # 高风险（建议修改）
    MEDIUM = "medium"        # 中等风险（注意）
    LOW = "low"              # 低风险（可选修改）


@dataclass
class Violation:
    """违规项"""
    rule_id: str
    level: ViolationLevel
    category: str
    keyword: str
    position: int
    context: str
    suggestion: str


@dataclass
class ComplianceResult:
    """合规检查结果"""
    passed: bool
    violations: List[Violation]
    summary: Dict[str, Any]
    risk_score: float  # 0-100，越高风险越大


# ═══════════════════════════════════════════════════════════════
# 默认合规规则库
# ═══════════════════════════════════════════════════════════════

DEFAULT_RULES = {
    "extreme_words": {
        "name": "极限用语",
        "description": "广告法禁止的极限用语",
        "level": "critical",
        "keywords": [
            "最好", "最佳", "唯一", "顶级", "国家级", "最高级",
            "极品", "极致", "终极", "绝对", "完美", "永久", "万能",
            "100%", "百分之百", "纯天然", "无副作用", "根治", "特效",
            "第一品牌", "行业第一", "全国第一", "全球第一", "世界第一",
            "首家", "首创", "首发", "独家", "独有", "唯一指定",
        ],
        "patterns": [
            r"最[好佳优强快新低价高大小多少远近久短快慢]+(?:的|地|之一|[\u4e00-\u9fa5]{0,3})",
            r"第[一二三四五六七八九十]+[名位家]",
            r"[全总绝]+[球国世界]+[第首独]+",
            # 精确匹配非法极限用语"第X"，排除"第一时间/第一反应/第一步"等正常表达
            r"(?:行业|全国|全球|世界|全网|业内|业界|市场)第[一二三四五六七八九十百千万]+",
        ],
    },
    "false_promises": {
        "name": "虚假承诺",
        "description": "无法兑现的承诺表述",
        "level": "high",
        "keywords": [
            "保证", "承诺", "确保", "肯定", "一定", "必然", "必定",
            "稳赚", "零风险", "高回报", "暴富", "发财", "赚钱",
            "包过", "包治", "包赚", "无效退款", "100%有效",
        ],
        "patterns": [
            r"保证.*[效果收益回报成功]",
            r"[零无]风险.*[高稳].*[收益回报]",
            r"[包保].*[过治赚效]",
        ],
    },
    "sensitive_political": {
        "name": "政治敏感",
        "description": "政治敏感词汇",
        "level": "critical",
        "keywords": [
            "反动", "颠覆", "分裂", "暴乱", "恐怖", "极端",
            "邪教", "赌博", "色情", "毒品", "枪支",
        ],
        "patterns": [],
    },
    "medical_claims": {
        "name": "医疗宣称",
        "description": "非医疗产品的医疗效果宣称",
        "level": "high",
        "keywords": [
            "治疗", "治愈", "疗效", "康复", "疗法", "药方",
            "抗癌", "抗肿瘤", "降血压", "降血糖", "减肥",
            "排毒", "养颜", "抗衰老", "增强免疫力",
        ],
        "patterns": [
            r"治疗.*[疾病症痛]",
            r"[治愈康复疗效].*[病症痛]",
            r"[降控调].*[血压糖脂]",
        ],
    },
    "comparison": {
        "name": "不当对比",
        "description": "贬低竞争对手的不当对比",
        "level": "medium",
        "keywords": [
            "比其他", "比竞品", "碾压", "吊打", "秒杀", "完爆",
            "遥遥领先", "全面超越", "行业领先", "无可匹敌",
        ],
        "patterns": [
            r"比.*[好强优领先]+",
            r"[远超秒杀碾压吊打完爆].*",
            r"[遥遥领先全面超越无可匹敌]",
        ],
    },
    "misleading": {
        "name": "误导性表述",
        "description": "可能引起误解的表述",
        "level": "medium",
        "keywords": [
            "限时", "限量", "最后", "仅剩", "即将", "马上",
            "错过等一年", "错过不再有", "机不可失", "时不再来",
            "倒计时", "抢购", "疯抢", "秒杀", "手慢无",
        ],
        "patterns": [
            r"仅剩\d+.*[个名额份]",
            r"倒计时\d+.*[小时天分钟]",
        ],
    },
    "financial_risk": {
        "name": "金融风险提示",
        "description": "投资理财类内容需提示风险",
        "level": "high",
        "keywords": [
            "保本", "保息", "稳赚不赔", "高收益零风险",
            "内部消息", "内幕", "股神", "荐股", "跟单",
        ],
        "patterns": [
            r"[保本保息].*[理财投资]",
            r"[稳赚不赔高收益零风险].*[理财投资产品]",
        ],
    },
    "privacy": {
        "name": "隐私保护",
        "description": "涉及用户隐私的不当收集承诺",
        "level": "medium",
        "keywords": [
            "绝对保密", "绝不泄露", "100%安全",
        ],
        "patterns": [],
    },
}


class ComplianceChecker:
    """
    统一合规检查器

    功能：
    1. 广告法合规检查（极限用语、虚假承诺等）
    2. 敏感内容检测
    3. 行业特定合规（医疗、金融等）
    4. 自定义规则扩展

    与事实核查的关系：
    - 事实核查: 检查数据准确性
    - compliance: 检查法律合规性（合规检查）
    - 两者互补，建议同时使用
    """

    def __init__(self, rules: Optional[Dict[str, Any]] = None):
        """
        初始化合规检查器

        Args:
            rules: 自定义规则（覆盖或扩展默认规则）
        """
        self.rules = dict(DEFAULT_RULES)
        if rules:
            self.rules.update(rules)
        self.enabled_categories: Set[str] = set(self.rules.keys())
        logger.info(f"[Compliance] 初始化完成，{len(self.rules)}个规则类别")

    def check(
        self,
        content: str,
        categories: Optional[List[str]] = None,
        context: str = "general",
    ) -> ComplianceResult:
        """
        检查内容合规性

        Args:
            content: 待检查内容
            categories: 指定检查类别（None则检查所有启用的类别）
            context: 使用场景（general/advertising/medical/financial/creative）

        Returns:
            ComplianceResult
        """
        if not content:
            return ComplianceResult(
                passed=True,
                violations=[],
                summary={"checked_categories": [], "total_violations": 0},
                risk_score=0.0,
            )

        # 根据场景调整启用的类别
        active_categories = self._get_categories_for_context(context, categories)

        violations = []
        for category in active_categories:
            if category not in self.rules:
                continue
            rule = self.rules[category]
            category_violations = self._check_category(content, category, rule)
            violations.extend(category_violations)

        # 计算风险分数
        risk_score = self._calculate_risk_score(violations)

        # 判断是否通过（CRITICAL 或 HIGH 级别违规均阻断）
        has_critical_or_high = any(
            v.level in (ViolationLevel.CRITICAL, ViolationLevel.HIGH) for v in violations
        )
        passed = not has_critical_or_high

        summary = {
            "checked_categories": active_categories,
            "total_violations": len(violations),
            "critical_count": sum(1 for v in violations if v.level == ViolationLevel.CRITICAL),
            "high_count": sum(1 for v in violations if v.level == ViolationLevel.HIGH),
            "medium_count": sum(1 for v in violations if v.level == ViolationLevel.MEDIUM),
            "low_count": sum(1 for v in violations if v.level == ViolationLevel.LOW),
            "context": context,
        }

        logger.info(f"[Compliance] 检查完成 | 违规: {len(violations)} | 风险分: {risk_score:.1f}")

        return ComplianceResult(
            passed=passed,
            violations=violations,
            summary=summary,
            risk_score=risk_score,
        )

    def _get_categories_for_context(
        self,
        context: str,
        categories: Optional[List[str]],
    ) -> List[str]:
        """根据场景获取应检查的类别（过滤已禁用的）"""
        if categories:
            return [c for c in categories if c in self.enabled_categories]

        context_mapping = {
            "general": list(self.rules.keys()),
            "advertising": ["extreme_words", "false_promises", "comparison", "misleading"],
            "medical": ["medical_claims", "extreme_words", "false_promises"],
            "financial": ["financial_risk", "false_promises", "extreme_words"],
            "creative": ["extreme_words", "false_promises", "comparison", "misleading"],
            "social": ["sensitive_political", "privacy", "extreme_words"],
        }

        cats = context_mapping.get(context, list(self.rules.keys()))
        return [c for c in cats if c in self.enabled_categories]

    def _check_category(self, content: str, category: str, rule: Dict) -> List[Violation]:
        """检查单个类别"""
        violations = []
        level = ViolationLevel(rule.get("level", "medium"))

        # 检查关键词
        for keyword in rule.get("keywords", []):
            for match in re.finditer(re.escape(keyword), content):
                violations.append(Violation(
                    rule_id=category,
                    level=level,
                    category=rule.get("name", category),
                    keyword=keyword,
                    position=match.start(),
                    context=self._get_context(content, match.start(), keyword),
                    suggestion=f"建议修改或删除'{keyword}'，避免使用{rule.get('name')}",
                ))

        # 检查正则模式
        for pattern in rule.get("patterns", []):
            try:
                for match in re.finditer(pattern, content):
                    violations.append(Violation(
                        rule_id=category,
                        level=level,
                        category=rule.get("name", category),
                        keyword=match.group(),
                        position=match.start(),
                        context=self._get_context(content, match.start(), match.group()),
                        suggestion=f"建议修改表述，避免{rule.get('name')}",
                    ))
            except re.error:
                logger.warning(f"[Compliance] 正则表达式错误: {pattern}")

        return violations

    def _get_context(self, content: str, position: int, keyword: str, window: int = 20) -> str:
        """获取关键词上下文"""
        start = max(0, position - window)
        end = min(len(content), position + len(keyword) + window)
        return content[start:end]

    def _calculate_risk_score(self, violations: List[Violation]) -> float:
        """计算风险分数"""
        if not violations:
            return 0.0

        level_weights = {
            ViolationLevel.CRITICAL: 25,
            ViolationLevel.HIGH: 15,
            ViolationLevel.MEDIUM: 5,
            ViolationLevel.LOW: 1,
        }

        total_weight = sum(level_weights[v.level] for v in violations)
        return min(100, total_weight)

    # ============ 规则管理 ============

    def add_rule(self, category: str, rule: Dict):
        """添加自定义规则"""
        self.rules[category] = rule
        self.enabled_categories.add(category)
        logger.info(f"[Compliance] 已添加规则: {category}")

    def remove_rule(self, category: str):
        """移除规则"""
        if category in self.rules:
            del self.rules[category]
            self.enabled_categories.discard(category)
            logger.info(f"[Compliance] 已移除规则: {category}")

    def enable_category(self, category: str):
        """启用检查类别"""
        if category in self.rules:
            self.enabled_categories.add(category)

    def disable_category(self, category: str):
        """禁用检查类别"""
        self.enabled_categories.discard(category)

    # ============ 批量处理和报告 ============

    def check_batch(self, contents: List[str], context: str = "general") -> List[ComplianceResult]:
        """批量检查"""
        return [self.check(c, context=context) for c in contents]

    def generate_report(self, result: ComplianceResult) -> str:
        """生成检查报告"""
        lines = [
            "=" * 50,
            "合规检查报告",
            "=" * 50,
            f"检查结果: {'通过' if result.passed else '未通过'}",
            f"风险分数: {result.risk_score:.1f}/100",
            f"违规数量: {result.summary['total_violations']}",
            "",
        ]

        if result.violations:
            lines.append("违规详情:")
            lines.append("-" * 50)
            for v in result.violations:
                lines.append(f"[{v.level.value.upper()}] {v.category}")
                lines.append(f"  关键词: {v.keyword}")
                lines.append(f"  上下文: ...{v.context}...")
                lines.append(f"  建议: {v.suggestion}")
                lines.append("")

        return "\n".join(lines)


# ============ 便捷函数 ============

def quick_check(content: str, context: str = "general") -> Dict[str, Any]:
    """快速合规检查"""
    checker = ComplianceChecker()
    result = checker.check(content, context=context)

    return {
        "passed": result.passed,
        "risk_score": result.risk_score,
        "violation_count": len(result.violations),
        "critical_issues": [
            {"keyword": v.keyword, "category": v.category}
            for v in result.violations if v.level == ViolationLevel.CRITICAL
        ],
    }


def check_creative_content(content: str) -> ComplianceResult:
    """创意内容专用检查（广告法合规）"""
    checker = ComplianceChecker()
    return checker.check(content, context="creative")


def check_advertising_content(content: str) -> ComplianceResult:
    """广告内容专用检查"""
    checker = ComplianceChecker()
    return checker.check(content, context="advertising")


# ═══════════════════════════════════════════════════════════════
# 行业风险案例库（从方案工具 risk_library_v2.py 迁移）
# ═══════════════════════════════════════════════════════════════

INDUSTRY_RISK_CASES = {
    "medical_claims": {
        "name": "医疗功效宣传",
        "description": "非医疗产品宣传治疗功效，违反医疗广告规定",
        "level": "critical",
        "keywords": ["治疗", "治愈", "疗效", "药效", "治愈率", "有效率"],
        "patterns": [r"[治愈康复疗效].*[病症痛]", r"[降控调].*[血压糖脂]"],
        "industries": ["医疗", "保健品"],
        "consequences": ["行政处罚", "吊销执照"],
        "solution": "删除所有医疗相关描述",
    },
    "medical_cure_rate": {
        "name": "治愈率宣传",
        "description": "宣传治愈率、有效率等医疗数据",
        "level": "high",
        "keywords": ["治愈率", "有效率"],
        "patterns": [r"治愈率高达\d+", r"有效率\d+"],
        "industries": ["医疗", "保健品"],
        "consequences": ["医疗广告违规处罚", "吊销执照"],
        "solution": "删除所有治愈率相关表述",
    },
    "finance_guarantee": {
        "name": "保本保收益承诺",
        "description": "非存款类金融产品承诺保本保收益",
        "level": "critical",
        "keywords": ["保本", "保息", "稳赚", "零风险", "高回报零风险"],
        "patterns": [r"[保本保息].*[理财投资]", r"[零无]风险.*[高稳].*[收益回报]"],
        "industries": ["金融"],
        "consequences": ["金融监管处罚", "投资者索赔"],
        "solution": "删除保本保收益承诺，添加风险提示",
    },
    "finance_yield_hint": {
        "name": "收益暗示",
        "description": "使用暗示性语言承诺收益",
        "level": "high",
        "keywords": ["躺赚", "轻松实现财富自由", "日入过万", "内幕消息", "股神", "荐股"],
        "patterns": [],
        "industries": ["金融"],
        "consequences": ["监管关注", "合规风险"],
        "solution": "使用中性表述，如'潜在收益''可能回报'",
    },
    "edu_result_promise": {
        "name": "升学结果承诺",
        "description": "承诺升学、通过考试等结果",
        "level": "high",
        "keywords": ["包过", "保录", "必过", "承诺上岸"],
        "patterns": [r"[包保必].*[过录岸]"],
        "industries": ["教育", "培训"],
        "consequences": ["教育部门处罚", "虚假宣传认定"],
        "solution": "删除结果承诺，改为'提升通过率''助力备考'",
    },
    "edu_famous_school": {
        "name": "名校背书",
        "description": "利用名校、名师做背书",
        "level": "medium",
        "keywords": ["清华名师", "北大教授", "前命题人"],
        "patterns": [],
        "industries": ["教育", "培训"],
        "consequences": ["侵权风险"],
        "solution": "获得授权后方可使用",
    },
    "brand_negative_value": {
        "name": "负面价值观",
        "description": "传播负面价值观或暗示",
        "level": "medium",
        "keywords": ["快速发财", "一夜暴富"],
        "patterns": [],
        "industries": ["通用"],
        "consequences": ["品牌形象受损", "舆论危机"],
        "solution": "替换为'事业成功''稳健发展'等正面表述",
    },
    "brand_denoise_competitor": {
        "name": "贬低竞品",
        "description": "直接或间接贬低竞争对手",
        "level": "medium",
        "keywords": ["比XX好十倍", "吊打所有竞品", "碾压", "完爆"],
        "patterns": [r"[远超秒杀碾压吊打完爆].*"],
        "industries": ["通用"],
        "consequences": ["不正当竞争诉讼"],
        "solution": "使用客观数据对比，避免主观评价",
    },
    "cultural_gender": {
        "name": "性别歧视",
        "description": "含有性别歧视或刻板印象",
        "level": "medium",
        "keywords": ["女人就应该", "男人不能"],
        "patterns": [],
        "industries": ["通用"],
        "consequences": ["舆论批评", "品牌危机"],
        "solution": "删除歧视性表述",
    },
    "cultural_region": {
        "name": "地域歧视",
        "description": "含有地域歧视内容",
        "level": "medium",
        "keywords": ["地方的人都"],
        "patterns": [r"\w+地方的人都"],
        "industries": ["通用"],
        "consequences": ["地域矛盾"],
        "solution": "删除地域相关表述",
    },
    "reputation_clickbait": {
        "name": "过度营销/标题党",
        "description": "过度夸张的标题党营销",
        "level": "low",
        "keywords": ["震惊", "重磅消息", "竟然"],
        "patterns": [],
        "industries": ["通用"],
        "consequences": ["用户反感", "品牌low化"],
        "solution": "使用客观、专业的表述",
    },
    "reputation_fear_marketing": {
        "name": "恐吓营销",
        "description": "使用恐吓、焦虑式营销",
        "level": "low",
        "keywords": ["再不行动就晚了", "错过这次后悔一辈子"],
        "patterns": [],
        "industries": ["通用"],
        "consequences": ["品牌形象负面"],
        "solution": "使用积极、正向的表述",
    },
}


def check_industry_risks(content: str, industry: str = "通用") -> ComplianceResult:
    """行业风险案例检查（从 risk_library_v2.py 迁移）"""
    checker = ComplianceChecker(rules={
        k: v for k, v in INDUSTRY_RISK_CASES.items()
        if industry in v.get("industries", []) or "通用" in v.get("industries", [])
    })
    return checker.check(content, context="general")
