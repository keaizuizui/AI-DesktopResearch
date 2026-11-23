# -*- coding: utf-8 -*-
"""
Claim Ledger — 声明账本（借鉴 BriefLoop）

核心原则：
  "智能部分没有权威，权威部分是确定性的。"
  - Python 拥有状态和账本，永远不调用 LLM
  - LLM 拥有草稿，通过合约交接执行内容工作
  - 每个字段一个写入者，没有模块可以"帮忙"更新它不拥有的状态

功能：
  1. Claim Ledger：每个关键声明必须注册来源，无来源的标记 unsupported
  2. Quality Gates：确定性检查（来源新鲜度、新事实、缺少支持）
  3. Source Appendix：最终输出附带来源追溯表
  4. Event Log：追加式记录所有阶段转换

用法：
    from shared.claim_ledger import ClaimLedger, QualityGate, EventLog

    ledger = ClaimLedger(project_id="brand_kate")
    ledger.register_claim("市场规模", "2800亿元", source="用户回答", source_id="answers_q1")
    ledger.register_claim("品牌阶段", "全新品牌，尚未上市", source="用户描述", source_id="user_desc")

    gate = QualityGate(ledger)
    unsupported = gate.find_unsupported()
    stale = gate.find_stale(max_age_days=365)

    event_log = EventLog(project_id="brand_kate")
    event_log.log("stage_complete", "round1", details="4 sections, 2 gaps")
"""

import time
import json
import re
import hashlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from enum import Enum
from pathlib import Path


# ============================================================
# 数据模型
# ============================================================

class ClaimStatus(str, Enum):
    """声明状态"""
    SUPPORTED = "supported"       # 有来源支持
    UNSUPPORTED = "unsupported"   # 无来源
    INFERRED = "inferred"        # 从已有信息推断（非幻觉，但有风险）
    DISPUTED = "disputed"         # 被后续校验质疑
    OVERRIDE = "override"         # 用户手动覆盖


class ClaimType(str, Enum):
    """声明类型"""
    FACT = "fact"                 # 事实性断言（市场规模、增长率等）
    OPINION = "opinion"           # 判断性结论（品牌定位、竞争格局）
    METRIC = "metric"             # 数值指标（价格、份额、增长率）
    ENTITY = "entity"             # 实体描述（品牌名、竞品名）


@dataclass
class Claim:
    """单个声明"""
    claim_id: str
    claim_type: ClaimType
    field_name: str           # 字段名（如"市场规模"、"品牌阶段"）
    value: str                # 声明值（如"2800亿元"、"知名"）
    status: ClaimStatus = ClaimStatus.UNSUPPORTED
    source: str = ""          # 来源描述（如"用户回答"、"行业报告"）
    source_id: str = ""       # 来源标识（如文件名、回答ID）
    source_date: str = ""     # 来源日期（如"2025-06-30"）
    writer: str = ""          # 写入者（节点ID，如"round1.brand_phase"）
    created_at: float = 0.0
    updated_at: float = 0.0
    meta: Dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.created_at:
            self.created_at = time.time()
            self.updated_at = time.time()

    def to_dict(self) -> Dict:
        return {
            "claim_id": self.claim_id,
            "claim_type": self.claim_type.value,
            "field_name": self.field_name,
            "value": self.value,
            "status": self.status.value,
            "source": self.source,
            "source_id": self.source_id,
            "source_date": self.source_date,
            "writer": self.writer,
            "created_at": self.created_at,
            "meta": self.meta,
        }


@dataclass
class GateResult:
    """门控检查结果"""
    passed: bool
    unsupported_claims: List[Claim] = field(default_factory=list)
    stale_claims: List[Claim] = field(default_factory=list)
    disputed_claims: List[Claim] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    summary: str = ""


# ============================================================
# Claim Ledger
# ============================================================

class ClaimLedger:
    """声明账本：所有关键声明的来源追踪"""

    def __init__(self, project_id: str = "default"):
        self.project_id = project_id
        self._claims: Dict[str, Claim] = {}  # claim_id -> Claim
        self._field_index: Dict[str, str] = {}  # field_name -> claim_id（每字段一个写入者）

    @staticmethod
    def _gen_id(field_name: str, writer: str) -> str:
        """生成声明ID"""
        raw = f"{field_name}:{writer}:{time.time()}"
        return hashlib.md5(raw.encode()).hexdigest()[:12]

    def register_claim(
        self,
        field_name: str,
        value: str,
        claim_type: ClaimType = ClaimType.FACT,
        source: str = "",
        source_id: str = "",
        source_date: str = "",
        writer: str = "",
        status: ClaimStatus = None,
        meta: Dict = None,
    ) -> Claim:
        """注册一个声明

        规则：
        - 如果字段已有声明，且新声明有来源，覆盖旧声明
        - 如果新声明无来源，不覆盖已有 supported 的声明
        """
        # 检查是否已有此字段的声明
        existing_id = self._field_index.get(field_name)
        if existing_id and existing_id in self._claims:
            existing = self._claims[existing_id]
            # 如果新声明无来源且旧声明已支持，拒绝覆盖
            if (not source and
                existing.status == ClaimStatus.SUPPORTED and
                status != ClaimStatus.OVERRIDE):
                return existing

        # 自动判断状态
        if status is None:
            status = ClaimStatus.SUPPORTED if source else ClaimStatus.UNSUPPORTED

        claim_id = self._gen_id(field_name, writer or "unknown")
        claim = Claim(
            claim_id=claim_id,
            claim_type=claim_type,
            field_name=field_name,
            value=value,
            status=status,
            source=source,
            source_id=source_id,
            source_date=source_date,
            writer=writer,
            meta=meta or {},
        )

        self._claims[claim_id] = claim
        self._field_index[field_name] = claim_id
        return claim

    def get_claim(self, field_name: str) -> Optional[Claim]:
        """获取字段的声明"""
        claim_id = self._field_index.get(field_name)
        if claim_id:
            return self._claims.get(claim_id)
        return None

    def get_value(self, field_name: str, default: str = "") -> str:
        """获取字段的值（便捷方法）"""
        claim = self.get_claim(field_name)
        return claim.value if claim else default

    def mark_disputed(self, field_name: str, reason: str = ""):
        """标记声明为被质疑"""
        claim = self.get_claim(field_name)
        if claim:
            claim.status = ClaimStatus.DISPUTED
            claim.updated_at = time.time()
            if reason:
                claim.meta["dispute_reason"] = reason

    def mark_supported(self, field_name: str, source: str, source_id: str = ""):
        """标记声明为已支持（补充来源）"""
        claim = self.get_claim(field_name)
        if claim:
            claim.status = ClaimStatus.SUPPORTED
            claim.source = source
            claim.source_id = source_id
            claim.updated_at = time.time()

    def get_all_claims(self, status: ClaimStatus = None) -> List[Claim]:
        """获取所有声明"""
        claims = list(self._claims.values())
        if status:
            claims = [c for c in claims if c.status == status]
        return claims

    def count_by_status(self) -> Dict[str, int]:
        """按状态统计"""
        result = {}
        for claim in self._claims.values():
            key = claim.status.value
            result[key] = result.get(key, 0) + 1
        return result

    def to_dict(self) -> Dict:
        """导出为字典"""
        return {
            "project_id": self.project_id,
            "claims": [c.to_dict() for c in self._claims.values()],
            "stats": self.count_by_status(),
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def save(self, filepath: str):
        """保存到文件"""
        Path(filepath).write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def load(cls, filepath: str, project_id: str = "default") -> "ClaimLedger":
        """从文件加载"""
        ledger = cls(project_id=project_id)
        data = json.loads(Path(filepath).read_text(encoding="utf-8"))
        for c_dict in data.get("claims", []):
            claim = Claim(
                claim_id=c_dict["claim_id"],
                claim_type=ClaimType(c_dict["claim_type"]),
                field_name=c_dict["field_name"],
                value=c_dict["value"],
                status=ClaimStatus(c_dict["status"]),
                source=c_dict.get("source", ""),
                source_id=c_dict.get("source_id", ""),
                source_date=c_dict.get("source_date", ""),
                writer=c_dict.get("writer", ""),
                created_at=c_dict.get("created_at", time.time()),
                meta=c_dict.get("meta", {}),
            )
            ledger._claims[claim.claim_id] = claim
            ledger._field_index[claim.field_name] = claim.claim_id
        return ledger


# ============================================================
# Quality Gate
# ============================================================

class QualityGate:
    """质量门控：确定性检查声明质量

    不依赖 LLM 判断，用 Python 代码执行检查。
    """

    def __init__(self, ledger: ClaimLedger):
        self.ledger = ledger

    def find_unsupported(self) -> List[Claim]:
        """找出所有无直接来源的声明（含推断声明）"""
        return [c for c in self.ledger.get_all_claims()
                if c.status in (ClaimStatus.UNSUPPORTED, ClaimStatus.INFERRED)]

    def find_stale(self, max_age_days: int = 365) -> List[Claim]:
        """找出来源过期的声明"""
        now = time.time()
        stale = []
        for claim in self.ledger.get_all_claims(ClaimStatus.SUPPORTED):
            if claim.source_date:
                try:
                    from datetime import datetime
                    source_ts = datetime.strptime(claim.source_date, "%Y-%m-%d").timestamp()
                    if (now - source_ts) > max_age_days * 86400:
                        stale.append(claim)
                except (ValueError, OSError):
                    pass
        return stale

    def find_disputed(self) -> List[Claim]:
        """找出所有被质疑的声明"""
        return [c for c in self.ledger.get_all_claims()
                if c.status == ClaimStatus.DISPUTED]

    def run_gate(self, max_age_days: int = 365) -> GateResult:
        """运行完整门控检查"""
        unsupported = self.find_unsupported()
        stale = self.find_stale(max_age_days)
        disputed = self.find_disputed()

        warnings = []
        if unsupported:
            warnings.append(f"有 {len(unsupported)} 个声明无直接来源（含推断）")
        if stale:
            warnings.append(f"有 {len(stale)} 个声明来源可能过时")
        if disputed:
            warnings.append(f"有 {len(disputed)} 个声明被后续校验质疑")

        passed = len(unsupported) == 0 and len(disputed) == 0
        summary = "通过" if passed else f"不通过（{len(unsupported)} 无直接来源, {len(disputed)} 被质疑）"

        return GateResult(
            passed=passed,
            unsupported_claims=unsupported,
            stale_claims=stale,
            disputed_claims=disputed,
            warnings=warnings,
            summary=summary,
        )

    def build_source_appendix(self, limit: int = 50) -> str:
        """构建来源追溯表（按来源去重）。

        limit=0 表示不截断；默认 50，可通过 source_appendix_limit 调整。
        """
        claims = self.ledger.get_all_claims(ClaimStatus.SUPPORTED)
        if not claims:
            return "（无已支持的声明）"

        sources = {}
        for c in sorted(claims, key=lambda x: x.created_at):
            title = (c.meta.get("source_title") or "").strip()
            if not title:
                title = (c.value or c.field_name or "").strip()
            url = (c.source or "").strip()
            key = c.source_id or url or title
            if not key or key in sources:
                continue
            sources[key] = (title, url)

        def _cell(text: str, limit: int = 0) -> str:
            """Markdown 表格单元格转义：竖线转义、换行折成空格。"""
            s = (text or "").replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()
            return s[:limit] if limit else s

        items = list(sources.items())
        shown = items[:limit] if limit and limit > 0 else items
        lines = ["| 序号 | 来源 | 链接 |",
                 "|------|------|------|"]
        for i, (_, (title, url)) in enumerate(shown, 1):
            lines.append(f"| {i} | {_cell(title, 80) or '—'} | {_cell(url) or '—'} |")
        if len(items) > len(shown):
            lines.append(
                f"\n共 {len(items)} 个来源，已列前 {len(shown)} 个；"
                "可通过 source_appendix_limit 调整上限（0=全部）。"
            )

        return "\n".join(lines)


# ============================================================
# Event Log
# ============================================================

class EventLog:
    """事件日志：追加式记录所有阶段转换"""

    def __init__(self, project_id: str = "default"):
        self.project_id = project_id
        self._events: List[Dict] = []

    def log(self, event_type: str, stage: str, details: str = "", meta: Dict = None):
        """记录事件"""
        self._events.append({
            "event_type": event_type,
            "stage": stage,
            "details": details,
            "meta": meta or {},
            "timestamp": time.time(),
        })

    def get_events(self, event_type: str = None, stage: str = None) -> List[Dict]:
        """获取事件"""
        events = self._events
        if event_type:
            events = [e for e in events if e["event_type"] == event_type]
        if stage:
            events = [e for e in events if e["stage"] == stage]
        return events

    def to_json(self, indent: int = 2) -> str:
        return json.dumps({
            "project_id": self.project_id,
            "events": self._events,
        }, ensure_ascii=False, indent=indent, default=str)


# ============================================================
# 便捷函数：LLM 声明提取
# ============================================================

_GENERIC_METRIC_PATTERNS = [
    # 金额（含亿/万/千与常见货币）
    r'(?:约|近|超过|不足|超|突破|达到)?\s*\d+(?:\.\d+)?\s*(?:亿|万|千)?\s*(?:元|美元|人民币|港元)',
    # 百分比与百分比区间
    r'\d+(?:\.\d+)?\s*%\s*(?:[-~～至—–]\s*\d+(?:\.\d+)?\s*%)?',
    # 数量、门店、人群、时长（不含“年”，避免把年份当声明）
    r'\d+(?:\.\d+)?\s*(?:亿|万|千)?\s*(?:家|人|个|台|店|门店|所|例|项|倍|小时|天|个月|岁)',
    # 排名
    r'第\s*\d+\s*[名位]',
]


def _claim_source_from_citation(sentence: str, materials: List[Any]):
    """从句子里的 [n] 解析素材来源，返回 (编号, URL, 等级, 声明性质, 标题)。"""
    m = re.search(r'\[(\d+)\]', sentence or "")
    if not m:
        return 0, "", "", "", ""
    idx = int(m.group(1))
    if not materials:
        return idx, f"引用编号[{idx}]（素材表未加载）", "", "", ""
    if idx < 1 or idx > len(materials):
        return idx, f"引用编号[{idx}]越界", "", "", ""
    mat = materials[idx - 1]
    if isinstance(mat, dict):
        url = mat.get("url", "") or mat.get("title", "")
        tier = mat.get("tier", "") or mat.get("grade", "")
        nature = mat.get("claim_nature", "")
        title = mat.get("title", "")
    else:
        url = getattr(mat, "url", "") or getattr(mat, "title", "")
        tier = getattr(mat, "tier", "") or getattr(mat, "grade", "")
        nature = getattr(mat, "claim_nature", "")
        title = getattr(mat, "title", "")
    return (idx, str(url or ""), str(getattr(tier, "value", tier) or ""),
            str(nature or ""), str(title or ""))


def extract_claims_from_llm(
    text: str,
    writer: str,
    default_source: str = "",
    ledger: ClaimLedger = None,
    materials: List[Any] = None,
    max_claims: int = 30,
) -> List[Claim]:
    """从 LLM 输出中提取关键声明并注册到账本。

    声明是否 SUPPORTED 只取决于能否绑定到具体来源：
    - 句子里有有效 [n] → 绑定 all_materials[n-1]，SUPPORTED
    - 没有 [n] 或编号越界 → UNSUPPORTED
    - JSON 键值同理，不再默认 SUPPORTED
    """
    if ledger is None:
        ledger = ClaimLedger()

    claims: List[Claim] = []
    seen = set()

    def _register(field: str, value: str, claim_type: ClaimType, sentence: str, origin: str):
        idx, source, tier, nature, title = _claim_source_from_citation(sentence, materials or [])
        if idx and source and "越界" not in source and "未加载" not in source:
            status = ClaimStatus.SUPPORTED
            source_id = f"material:{idx}"
        else:
            status = ClaimStatus.UNSUPPORTED
            source_id = ""
            source = source or default_source or "报告未标注引用"
        claims.append(ledger.register_claim(
            field_name=field,
            value=value,
            claim_type=claim_type,
            source=source,
            source_id=source_id,
            writer=writer,
            status=status,
            meta={
                "citation_index": idx,
                "source_grade": tier,
                "claim_nature": nature,
                "source_title": title,
                "extracted_from": origin,
            },
        ))

    # 1) JSON 键值（通用：不再限制品牌调研字段）
    try:
        json_match = re.search(r'\{.*\}', text, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group())
            if isinstance(data, dict):
                for key, val in data.items():
                    if len(claims) >= max_claims:
                        break
                    sval = str(val).strip() if val is not None else ""
                    if not sval or len(sval) > 200:
                        continue
                    field = f"report_field:{writer}:{key}"
                    if field in seen:
                        continue
                    seen.add(field)
                    ctype = ClaimType.METRIC if re.search(r'\d', sval) else ClaimType.FACT
                    _register(field, sval, ctype, sval, "json")
    except (json.JSONDecodeError, TypeError, re.error):
        pass

    # 2) 正文数值指标（金额、百分比、数量、排名）
    for sentence in re.split(r'(?<=[。！？；\n])', text):
        if len(claims) >= max_claims:
            break
        if not sentence.strip():
            continue
        clean = re.sub(r'\[\d+\]', '', sentence)
        for pattern in _GENERIC_METRIC_PATTERNS:
            for match in re.findall(pattern, clean):
                if len(claims) >= max_claims:
                    break
                value = match.strip()
                if not value or value in seen:
                    continue
                if re.fullmatch(r'20\d{2}\s*年?', value):
                    continue
                seen.add(value)
                _register(f"report_metric:{writer}:{len(claims)}", value,
                          ClaimType.METRIC, sentence, "text")
            if len(claims) >= max_claims:
                break

    return claims
