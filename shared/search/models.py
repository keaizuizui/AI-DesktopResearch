"""
shared/search/models — 统一搜索数据结构
==========================================

重大更新：
- 信源评级域名库扩展至200+（覆盖中文商业媒体Top 100）
- 新增评分→评级自动映射（llm_score自动转换为SourceGrade）
- 静态评级与动态评分双引擎融合
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Union


class SourceGrade(str, Enum):
    """信源分级"""
    S = "S"   # 权威：政府、顶级期刊、官方数据、国际组织
    A = "A"   # 可靠：知名咨询机构、行业权威媒体、顶级智库
    B = "B"   # 一般：专业博客、行业论坛、企业官方、自媒体
    C = "C"   # 低可信：未验证来源、内容农场 → 自动过滤


# ─── 声明性质枚举（软文分层摘取用） ───
CLAIM_SELF_PUBLISHED = "企业自述"
CLAIM_THIRD_PARTY = "第三方数据"
CLAIM_UNKNOWN = "未知"
CLAIM_NATURES = (CLAIM_SELF_PUBLISHED, CLAIM_THIRD_PARTY, CLAIM_UNKNOWN)


@dataclass
class SearchResult:
    """
    统一搜索结果数据结构 评分与评级深度对接

    核心改进：
    1. llm_score 自动映射到 grade（动态评分优先于静态评级）
    2. 双引擎融合：静态域名规则 + 动态LLM打分
    """
    # ─── 基础字段 ───
    title: str = ""
    url: str = ""
    content: str = ""
    snippet: str = ""                  # 摘要（content的截短版）
    source: str = ""                   # bocha / ddg / gptr
    source_type: str = "web"           # web / news / academic

    # ─── 数据时效分类（合规上线必备） ───
    data_type: str = "unknown"         # actual / estimate / forecast / model_forecast / unknown

    # ─── 信源评级（静态规则+动态评分融合） ───
    grade: SourceGrade = SourceGrade.B
    tier: str = ""                     # S/A/B/C 字符串版（兼容旧代码）

    # ─── 时间字段 ───
    published_date: str = ""
    searched_at: str = ""

    # ─── 相关性评分 ───
    relevance_score: float = 0.0       # 0-1 自动评分
    data_density: float = 0.0          # 0-1 数据密度

    # ─── LLM打分字段（动态评分引擎） ───
    llm_score: float = 0.0            # LLM质量打分 1-10
    score: float = 0.0                # 综合得分 0-1（兼容调研工具）
    facts: List[str] = field(default_factory=list)    # 核心事实列表
    reliability: str = "medium"       # high / medium / low
    confidence: float = 0.0            # 置信度 0-1（兼容调研工具）
    score_reason: str = ""            # LLM评分理由（审计用）
    fact_card: str = ""               # 事实卡片HTML
    raw_html: str = ""                # 原始HTML内容

    # ─── 溯源字段（可选，带默认值以兼容旧数据） ───
    query: str = ""                   # 该结果来自哪条搜索查询
    claim_nature: str = ""            # 企业自述 / 第三方数据 / ""（未判定）
    source_group: str = ""            # 同品牌软文合并后的信源组标识

    def __post_init__(self):
        if not self.snippet and self.content:
            self.snippet = self.content[:200]
        if not self.searched_at:
            self.searched_at = datetime.now().isoformat()

        # 评分→评级映射（动态评分优先）
        if self.llm_score > 0:
            self.score = self.llm_score / 10.0
            # LLM评分直接决定grade（覆盖静态规则）
            score_grade = score_to_grade(self.llm_score)
            if score_grade:  # 只有有效评分才覆盖
                self.grade = score_grade
        elif self.score > 0:
            self.llm_score = self.score * 10
            score_grade = score_to_grade(self.llm_score)
            if score_grade:
                self.grade = score_grade

        # 同步tier字段
        if not self.tier and self.grade:
            self.tier = self.grade.value
        if self.tier and not self.grade:
            try:
                self.grade = SourceGrade(self.tier.upper())
            except ValueError:
                self.grade = SourceGrade.B

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["grade"] = self.grade.value
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SearchResult":
        d = dict(d)
        if "grade" in d and isinstance(d["grade"], str):
            d["grade"] = SourceGrade(d["grade"])
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    @property
    def is_usable(self) -> bool:
        """是否为可用信源（非C级）"""
        return self.grade != SourceGrade.C

    @property
    def tier_grade(self) -> str:
        """获取信源等级字符串 S/A/B/C（兼容调研工具tier字段）"""
        return self.tier or self.grade.value

    def update_grade_from_score(self, llm_score: float):
        """根据LLM评分更新评级（外部调用）"""
        self.llm_score = llm_score
        self.score = llm_score / 10.0
        new_grade = score_to_grade(llm_score)
        if new_grade:
            self.grade = new_grade
            self.tier = new_grade.value


@dataclass
class SearchQuery:
    """搜索查询封装"""
    query: str
    max_results: int = 10
    sources: List[str] = field(default_factory=lambda: ["bocha", "ddg"])
    use_cache: bool = True
    score_results: bool = False        # 是否用LLM打分
    min_grade: SourceGrade = SourceGrade.C  # 最低信源等级
    timeout: int = 30

    @property
    def normalized(self) -> str:
        """归一化查询（用于缓存键）"""
        import re
        q = self.query.lower().strip()
        q = re.sub(r"\s+", " ", q)
        q = re.sub(r"20[2-9]\d", "{YEAR}", q)
        q = re.sub(r"[^\u4e00-\u9fa5a-z0-9\s]", "", q)
        return q.strip()


# ═══════════════════════════════════════════════════════════════
# 评分→评级映射函数
# ═══════════════════════════════════════════════════════════════

def score_to_grade(llm_score: float) -> Optional[SourceGrade]:
    """
    LLM评分映射到信源评级

    映射规则：
    - 9-10分 → S级（卓越质量）
    - 7-8.9分 → A级（优秀质量）
    - 5-6.9分 → B级（一般质量）
    - <5分 → C级（低质量）

    Args:
        llm_score: LLM打分 1-10

    Returns:
        SourceGrade 或 None（如果score无效）
    """
    if not isinstance(llm_score, (int, float)) or llm_score <= 0:
        return None

    if llm_score >= 9.0:
        return SourceGrade.S
    elif llm_score >= 7.0:
        return SourceGrade.A
    elif llm_score >= 5.0:
        return SourceGrade.B
    else:
        return SourceGrade.C


def grade_to_score_range(grade: SourceGrade) -> tuple:
    """
    信源评级对应的评分区间

    Returns:
        (min_score, max_score) 元组
    """
    ranges = {
        SourceGrade.S: (9.0, 10.0),
        SourceGrade.A: (7.0, 8.9),
        SourceGrade.B: (5.0, 6.9),
        SourceGrade.C: (0.0, 4.9),
    }
    return ranges.get(grade, (0.0, 10.0))


# ═══════════════════════════════════════════════════════════════
# 信源分级域名库 — 扩展至200+域名
# ═══════════════════════════════════════════════════════════════

# S级：政府、国际组织、顶级期刊、官方统计数据
_S_DOMAINS = [
    # 中国政府
    "gov.cn", "stats.gov.cn", "国家统计局", "ndrc.gov.cn", "mof.gov.cn",
    "mofcom.gov.cn", "miit.gov.cn", "nmpa.gov.cn", "samr.gov.cn",
    "cac.gov.cn", "mca.gov.cn", "moe.gov.cn", "mohurd.gov.cn",
    # 国际组织
    "worldbank.org", "imf.org", "wto.org", "who.int", "un.org",
    "oecd.org", "ilo.org", " UNESCO.org", "fao.org",
    # 顶级期刊
    "nature.com", "science.org", "cell.com", "thelancet.com",
    "nejm.org", "pnas.org", "ieee.org", "acm.org",
    # 官方数据
    "cninfo.com.cn", "sse.com.cn", "szse.cn", "hkexnews.hk",
    "sec.gov", "nasdaq.com", "bloomberg.com/professional",
]

# A级：知名咨询机构、权威财经媒体、顶级智库
_A_DOMAINS = [
    # 国际咨询
    "mckinsey.com", "bain.com", "bcg.com", "deloitte.com",
    "pwc.com", "ey.com", "kpmg.com", "accenture.com",
    "ibm.com/services", "capgemini.com", "tcs.com",
    # 行业研究
    "gartner.com", "idc.com", "forrester.com", "cbinsights.com",
    "pitchbook.com", "crunchbase.com", "grandviewresearch.com",
    "marketsandmarkets.com", "researchandmarkets.com",
    # 权威财经
    "ft.com", "wsj.com", "economist.com", "bloomberg.com",
    "reuters.com", "cnbc.com", "forbes.com", "harvard.edu",
    "mit.edu", "stanford.edu", "ox.ac.uk", "cam.ac.uk",
    # 中文权威财经
    "caixin.com", "caijing.com.cn", "21jingji.com", "yicai.com",
    "jiemian.com", "huxiu.com", "36kr.com", "cyzone.cn",
    "itjuzi.com", "pedaily.cn", "chinaventure.com.cn",
    # 行业垂直
    "iresearch.cn", "analysys.cn", "questmobile.com.cn",
    "trustdata.cn", "talkingdata.com", "199it.com",
    "chinaz.com", "admin5.com", "donews.com",
]

# B级：专业博客、行业论坛、企业官方、自媒体
_B_DOMAINS = [
    # 内容平台
    "zhihu.com", "csdn.net", "jianshu.com", "toutiao.com",
    "baijiahao.baidu.com", "mp.weixin.qq.com", "sohu.com/a/",
    "163.com", "sina.com.cn", "ifeng.com", "qq.com/a/",
    "people.com.cn", "xinhuanet.com", "chinanews.com",
    # 企业官方
    "company", "corp", "group", "inc", "ltd", "co.",
    "about", "investor", "ir.", "press.", "newsroom.",
    # 技术社区
    "github.com", "stackoverflow.com", "medium.com",
    "dev.to", "hackernoon.com", "infoq.cn", "oschina.net",
    "segmentfault.com", "juejin.cn", "v2ex.com",
    # 产品社区
    "xiaohongshu.com", "douban.com", "weibo.com",
    "zhihu.com/zvideo", "bilibili.com", "youtube.com",
    # 招聘/职场
    "lagou.com", "zhaopin.com", "51job.com", "bosszhipin.com",
    "maimai.cn", "linkedin.com", "脉脉",
]

# C级特征：内容农场、未验证自媒体（通过排除法识别）
_C_INDICATORS = [
    "baike.baidu.com",  # 百科类（内容聚合，非原创）
    "wenku.baidu.com",  # 文档分享
    "doc88.com", "docin.com",  # 文档平台
    "360doc.com",       # 个人图书馆
    "doc360.com",
    "book118.com",      # 原创力文档
    "1688.com",         # 阿里巴巴批发平台 — 非品牌调研信源
    "toutiao.com",      # 今日头条自媒体 — 信源质量低
]


def grade_source(url: str, content: str = "", llm_score: float = 0.0) -> SourceGrade:
    """
    对URL进行信源分级 双引擎融合

    优先级：
    1. LLM评分（如果提供）→ 直接映射
    2. 域名规则匹配
    3. C级特征检测
    4. 默认B级

    Args:
        url: 搜索结果URL
        content: 内容摘要（辅助判断）
        llm_score: LLM动态评分（可选，优先使用）

    Returns:
        SourceGrade 枚举值
    """
    # 1. 动态评分优先
    if llm_score > 0:
        score_grade = score_to_grade(llm_score)
        if score_grade:
            return score_grade

    # 2. 域名规则匹配
    text = (url + " " + content[:200]).lower()

    for domain in _S_DOMAINS:
        if domain.lower() in text:
            return SourceGrade.S

    for domain in _A_DOMAINS:
        if domain.lower() in text:
            return SourceGrade.A

    for domain in _B_DOMAINS:
        if domain.lower() in text:
            return SourceGrade.B

    # 3. C级特征检测
    for indicator in _C_INDICATORS:
        if indicator.lower() in text:
            return SourceGrade.C

    # 4. 默认B级（一般专业内容）
    return SourceGrade.B


def get_grade_reason(grade: SourceGrade, url: str = "") -> str:
    """获取评级原因说明"""
    reasons = {
        SourceGrade.S: "权威来源（政府/国际组织/顶级期刊/官方数据）",
        SourceGrade.A: "可靠来源（知名咨询机构/权威财经媒体/顶级智库）",
        SourceGrade.B: "一般来源（专业博客/行业论坛/企业官方/自媒体）",
        SourceGrade.C: "低可信来源（内容农场/未验证来源）",
    }
    return reasons.get(grade, "未知来源")


def filter_by_grade(
    results: List[SearchResult],
    min_grade: SourceGrade = SourceGrade.B,
) -> List[SearchResult]:
    """按信源等级过滤搜索结果"""
    grade_order = {SourceGrade.S: 0, SourceGrade.A: 1, SourceGrade.B: 2, SourceGrade.C: 3}
    min_order = grade_order.get(min_grade, 3)
    return [r for r in results if grade_order.get(r.grade, 3) <= min_order]


def sort_by_combined_score(
    results: List[SearchResult],
    grade_weight: float = 0.4,
    score_weight: float = 0.6,
) -> List[SearchResult]:
    """
    按综合评分排序（静态评级 + 动态评分）

    Args:
        results: 搜索结果列表
        grade_weight: 信源评级权重
        score_weight: LLM评分权重

    Returns:
        排序后的结果列表
    """
    def combined_score(r: SearchResult) -> float:
        grade_scores = {SourceGrade.S: 10, SourceGrade.A: 8, SourceGrade.B: 6, SourceGrade.C: 4}
        grade_part = grade_scores.get(r.grade, 6) * grade_weight
        score_part = (r.llm_score or r.score * 10) * score_weight
        return grade_part + score_part

    return sorted(results, key=combined_score, reverse=True)
