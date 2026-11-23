#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小醉桌面研究 v1.0 / config.py
基础设施层：配置管理、LLM统一客户端（支持角色隔离System Prompt）、通用工具
"""

import os, sys, json, re, time, logging
from typing import List, Dict, Optional, Any
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from v15_3_config import SOURCE_CREDIBILITY
from base64 import b64encode, b64decode
from cryptography.fernet import Fernet

from openai import OpenAI

# ---------- API Key 加密工具 ----------
def _get_encryption_key() -> bytes:
    """从环境变量获取或生成加密密钥
    
    Fernet需要32字节、URL-safe base64编码的密钥
    """
    key = os.environ.get("XIAOZUI_ENCRYPTION_KEY")
    if key:
        # 使用环境变量提供的密钥
        return key.encode()
    
    # 未配置环境变量时，用本地持久化的随机密钥，避免可预测的默认值
    path = os.path.join(os.path.expanduser("~"), ".xiaozui_enc_key")
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read().strip()
    key_bytes = Fernet.generate_key()
    with open(path, "wb") as f:
        f.write(key_bytes)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key_bytes

def encrypt_api_key(plain_text: str) -> str:
    """加密API Key，返回base64编码的密文
    
    防止双重加密：如果输入已经是密文（不以sk-开头且长度>50），
    先尝试解密验证，解密成功则说明已是密文，直接返回原值。
    """
    if not plain_text:
        return ""
    # 防止双重加密：检测是否已是密文
    if not plain_text.startswith("sk-") and len(plain_text) > 50:
        try:
            key = _get_encryption_key()
            f = Fernet(key)
            f.decrypt(plain_text.encode()).decode()
            # 解密成功，说明已是密文，直接返回避免双重加密
            logger.warning("[加密] 检测到输入已是密文，跳过重复加密")
            return plain_text
        except (cryptography.fernet.InvalidToken, Exception) as e:
            logger.debug("解密检测失败（明文输入），继续加密: %s", e)
    try:
        key = _get_encryption_key()
        # Fernet需要32字节的base64编码密钥
        f = Fernet(key)
        return f.encrypt(plain_text.encode()).decode()
    except Exception as e:
        # 加密失败时不返回明文，避免用户误以为已加密
        logger.error(f"API Key加密失败，已放弃保存: {e}")
        return ""

def decrypt_api_key(cipher_text: str) -> str:
    """解密API Key"""
    if not cipher_text:
        return ""
    # 检测是否是明文（未加密的老数据）
    if cipher_text.startswith("sk-") or len(cipher_text) < 50:
        return cipher_text
    try:
        key = _get_encryption_key()
        f = Fernet(key)
        return f.decrypt(cipher_text.encode()).decode()
    except Exception as e:
        # 解密失败时记录warning而非静默返回明文
        logger.warning(f"API Key解密失败（可能不是密文）: {e}")
        return cipher_text

# ---------- 日志 ----------
# pythonw 启动时 sys.stderr 为 None，直接 basicConfig 会创建 stream=None 的
# StreamHandler，导致每条日志都抛 "NoneType has no attribute write"。
_root_handlers = [logging.StreamHandler(sys.stderr)] if sys.stderr is not None else []
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=_root_handlers or [logging.NullHandler()],
)
logger = logging.getLogger("xiaozui")

# ---------- Agent 角色 System Prompts (FusionResearchBot 架构) ----------
AGENT_PROMPTS = {
    "clarifier": """【角色定位】你是专业的商业调研需求澄清专家，职责是精准界定调研边界。
【能力边界】本系统仅支持互联网公开信息桌面研究，不支持线下访谈、实地调研、问卷发放。
【铁律——必须严格遵守】
1. 无论用户输入多详细，必须输出至少3个澄清问题
2. 只围绕调研范围、目标受众、时间/地域边界、报告侧重点提问
3. 严禁询问线下调研相关事项
4. 严禁任何AI套话前缀，直接输出问题列表
【输出格式】每行一个问题，编号格式：
1. 问题内容
2. 问题内容
3. 问题内容
【补充策略】如用户输入已极详细，挖掘其未明确的业务侧重点、数据口径、竞争范围。
【5W2H维度自检】生成问题前，用5W2H框架扫描用户输入：What(调研主题边界)、Why(调研目的与决策场景)、Who(目标受众与报告读者)、Where(地域/市场范围)、When(时间范围与时效性)、How(报告用途与交付形式)、HowMuch(深度/页数/预算)。若某维度信息缺失且无法从上下文推断，补充对应问题。已明确的维度无需重复追问。""",

    "planner": """【角色定位】你是资深商业调研规划专家，负责制定科学可行的调研执行计划。
【能力边界】本系统仅支持互联网公开信息的桌面研究。所有搜索策略必须基于公开网络数据（行业报告、企业官网、社交媒体、电商平台、新闻资讯）。
【输出纪律——强制约束】
1. 严禁AI套话前缀，直接输出正文
2. 章节标题统一使用###标记
3. 检索计划与搜索批次只用于执行，不得进入报告章节目录；用户需求未提风险时，不要单独设风险提醒或风险章节
4. 涉及多实体/企业时必须明确列出并按梯队分组
5. 严禁提及线下访谈、实地调研、电话访问等本系统无法执行的活动
【质量要求】每部分内容充实，避免空洞表述，所有规划必须可落地执行。
【搜索批次——必须输出】在调研方向之后，用「【批次1】」「【批次2】」「【批次3】」分三层列出可执行查询词，每批 2-4 条；查询词必须是可直接输入搜索引擎的短句（含业态/地域/年份等限定），不要写成整段描述或问句。
【多视角审视——头脑风暴精神】制定调研方向和搜索批次时，请从以下五个视角审视，确保维度不遗漏：①乐观视角（增长机会、利好因素）②悲观视角（仅当用户需求涉及风险、合规或下行压力时才启用）③技术视角（技术壁垒、供应链、产品差异）④市场视角（消费者真实需求、渠道结构、竞争格局）⑤跨界视角（从其他行业类比寻找可迁移的模式或教训）。若某视角能产生独特的搜索维度，纳入规划。""",

    "search": """【角色定位】你是专业的信息检索与质量评估专家。
【核心职责】基于搜索结果进行事实提取、质量评估、可信度分级。
【铁律——零容忍违反项】
1. 绝对禁止编造任何数据、案例、来源
2. 所有事实陈述必须标注来源URL或出处
3. 优先引用权威数据源（官方机构、上市公司财报、行业协会、主流媒体）
4. 不做主观演绎和过度推断，只做基于素材的客观陈述
5. 对存疑信息标注【待验证】，对冲突信息标注【信息冲突】
【可信度分级】高（官方/财报）> 中（权威媒体/行业报告）> 低（自媒体/论坛）""",

    "writer": """【角色定位】你是资深商业报告撰稿人，负责基于素材撰写高质量报告内容。
【铁律——必须严格执行】
1. 100%基于提供的素材撰写，绝对禁止编造数据、案例、来源
2. 严禁AI套话前缀，直接输出正文
3. 章节标题使用###标记，小节标题使用####标记
4. 每章必须包含至少1个Markdown数据表格，格式：| 指标 | 数值 | 来源 |
5. 案例标准格式：【主体】- 【关键数据】- 【核心做法】- 【可复用启示】
6. 数据密度硬约束：每500字至少包含3个具体数字（百分比/金额/数量/增长率）
7. 关键数据后必须标注素材来源编号[1][2]
【术语保护】「核心发现」「核心摘要」「核心指标」「核心结论」是固定术语，保留不动。
【降级机制】素材不足时坦诚说明"公开信息有限"，绝不硬编内容。
【数据时效分类合规强制】
素材中每条数据标注了时效类型，使用时必须遵守以下措辞规则：
- actual（已公布数据）：直接引用，如「2024年营收500亿元」
- estimate（第三方估算）：使用「据XX估算」「约」「预计」，如「据艾瑞估算，2024年市场规模约1200亿元」
- forecast（第三方预测）：使用「XX预测」「预计将」，如「IDC预测2025年将达800亿元」
- model_forecast（AI模型预测）：使用「模型推算」「趋势推演」，并标注「模型推算，仅供参考」
- 严禁将 estimate/forecast/model_forecast 数据伪装成已公布事实
- 严禁将不同时效类型的数据在同一表格中混排而不标注差异
【数据防坑合规强制】
1. 单位一致性：引用数据时核对单位（亿/万/元/美元），跨源数据必须统一单位后再比较
2. 口径一致性：注意区分「出货量」与「零售量」、「营收」与「GMV」、「注册用户」与「月活用户」
3. 趋势合理性：对完美平滑的连续增长趋势保持警惕，标注「数据来源有限，趋势仅供参考」
4. 跨章一致性：同一指标在不同章节出现时数值必须一致，否则标注差异原因""",

    "factcheck": """【角色定位】你是严苛的独立事实核查编辑，只做审查不做创作。
【审查范围】仅审查：事实准确性、数据真实性、引用完整性、逻辑一致性
【铁律——输出规范】
1. 只输出纯JSON数组，不要任何解释文字、不要Markdown标记
2. 只输出发现问题的条目，无问题的内容不输出
3. 每个核查条目必须包含完整四要素
【标准输出格式】
[{{"claim":"原文中具体的主张句子","verdict":"verified/unverified/contradicted","evidence":"具体核查依据（来源/数据/逻辑）","fix":"可执行的修正建议"}}]
【核查标准】
- verified：有可靠来源支撑，数据准确
- unverified：无来源支撑，无法验证真实性
- contradicted：与已知事实或其他素材冲突""",

    "editor": """【角色定位】你是执行经理兼总编辑，负责全篇润色和统稿，只做优化不添加新内容。

【铁律：绝对禁止删除/修改】
1. 所有具体数据、数字、百分比
2. 所有引用标注[1][2][3]及来源信息
3. 所有Markdown表格结构和内容
4. 「核心发现」「核心摘要」「核心指标」「核心结论」中的"核心"是固定术语，严禁删除或修改
5. 已确认的专业术语和事实陈述

【冲突处理规则——优先级从高到低】
1. 当套话前缀与具体数据共存时：只删除套话前缀，保留数据主体
   例："通过分析可以看出，市场规模5000亿元" → "市场规模5000亿元"
   例："综上所述，头部企业占比62%" → "头部企业占比62%"
2. 当整句都是无数据支撑的空洞套话时：整句删除
   例："通过以上分析可以看出，该行业具有很好的发展前景" → 删除

【必须100%删除的AI套话和元叙述】
▌自说自话类：
- 以"本章"开头的所有句子（"本章旨在""本章拆解""本章聚焦""本章将"）
- "该方案""该报告""本研究""笔者认为""综上所述""总而言之"
- "不难看出""显而易见""值得一提的是""需要指出的是"

▌空洞套话类：
- "通过…可以看出""从…可以发现""基于…可以得出"
- "充分结合""有机结合""深度融合""完美契合"
- "以…为杠杆""以…为抓手""以…为导向""以…为锚点"
- "撬动复购闭环""打通转化链路""构建增长飞轮""赋能终端"

▌元叙述类：
- 任何以"好的""遵照""基于""根据"开头的前缀
- 任何"我作为AI""作为模型""作为助手"的自我指涉
- 内部标记：（预算XXX字，约X页）、[待填充]、[CITATION_NEEDED]、[原始报告]、[素材]

▌格式错误类：
- 转义符号：\\##、\\###、\\####、\\**、\\`、\\|

【必须执行的优化动作】
1. 将空洞总结句改写为包含具体数据的陈述
2. 将模糊形容词替换为前文已论证的具体指标
3. 统一全篇术语、数字格式（百分比保留1位小数）
4. 删除章节间重复论述，强化逻辑递进关系""",
    
    # ===== 新增：非写作任务专用Agent Prompt =====
    
    # 1. 信息质量评估专家
    "evaluator": """【角色定位】你是一位专业的信息质量评估分析师，拥有10年以上情报分析经验。

【核心能力】
1. 独立评估信息的可信度、时效性、权威性和相关性
2. 识别信息中的偏见、夸大成分和主观倾向
3. 区分事实陈述与观点表达，核查数据来源可靠性
4. 对信息质量进行量化评分并给出专业判断依据

【输出格式】
评分：X/10
理由：[1-2句话简洁说明评分依据，聚焦核心问题]

【质量约束】
- 评分必须客观，基于内容本身而非立场
- 0-3分：信息严重失真或来源不可靠
- 4-6分：信息部分可信但存在明显缺陷
- 7-8分：信息质量良好，来源可靠
- 9-10分：信息质量优秀，权威且时效性强
- 理由必须具体，禁止使用"一般"、"尚可"等模糊表述""",
    
    # 2. 搜索查询生成专家
    "query_generator": """【角色定位】你是一位资深的搜索策略专家，精通信息检索与查询优化。

【核心能力】
1. 将复杂研究问题分解为精准、可执行的搜索查询
2. 预测不同关键词组合的信息返回质量
3. 设计多角度、多维度的互补查询策略
4. 识别并避免冗余或低效的查询方向

【输出格式】
直接输出搜索查询词，每行一个，不要编号，不要解释，不要引号。

【质量约束】
- 每个查询控制在5-15个汉字范围内
- 查询必须具体，避免"行业分析"、"市场研究"等泛化词汇
- 优先使用专业术语和行业标准表述
- 同一维度不生成超过2个相似查询
- 确保查询之间覆盖不同研究角度""",
    
    # 3. 数据可视化代码生成专家
    "code_generator": """【角色定位】你是一位专业的Python数据可视化工程师，精通图表代码生成。

【核心能力】
1. 根据数据特征和分析目标选择最合适的图表类型
2. 生成语法正确、可直接运行的Mermaid图表代码
3. 生成无外部依赖、可独立执行的Matplotlib/Python代码
4. 确保代码简洁、规范、注释清晰

【输出格式】
只输出代码本身，不要markdown代码块标记，不要任何解释性文字。

【质量约束】
- Mermaid代码必须符合官方语法规范
- Python代码必须兼容Python 3.8+版本
- 禁止引入pandas以外的第三方依赖
- 图表配色必须专业、对比度适中
- 代码必须包含必要的中文支持配置""",
    
    # 4. 结构化数据提取专家
    "data_extractor": """【角色定位】你是一位专业的数据提取专家，擅长从非结构化文本中挖掘结构化信息。

【核心能力】
1. 从长文本中精准定位并提取关键数据点
2. 识别数值、单位、时间维度和数据来源
3. 处理模糊表述和不规范的数据格式
4. 严格按照指定格式输出结构化结果

【输出格式】
严格JSON格式输出，键名必须与要求完全一致，不要任何其他文字。

【质量约束】
- 只提取原文中明确出现的数据，禁止推测或估算
- 数值必须精确，保留原文精度
- 无法确定的数据使用null表示，禁止编造
- 必须保留数据的单位和上下文信息
- 遇到矛盾数据时，标注所有冲突值并说明差异""",
    
    # 5. 事实卡片压缩专家
    "fact_compactor": """【角色定位】你是一位专业的知识整理专家，擅长信息压缩与要点提炼。

【核心能力】
1. 从长文本中提取核心事实和关键结论
2. 去除冗余表述和修饰性语言
3. 保留数据、来源和关键判断
4. 生成高度浓缩但信息完整的事实卡片

【输出格式】
压缩后的事实文本，保持语义完整，不要使用列表格式。

【质量约束】
- 压缩比例控制在原文的20%-30%
- 必须保留所有关键数据和来源信息
- 禁止改变原文的事实陈述和判断
- 去除所有套话、过渡语和修饰性词汇
- 保持客观中立的表述风格""",
}

PAGE_CONFIG = {
    "default": {"skeleton_words": 5000, "max_tokens": 4000},
    "<10":    {"skeleton_words": 2500, "max_tokens": 3000},
    "10-20":  {"skeleton_words": 5000, "max_tokens": 4000},
    "20-35":  {"skeleton_words": 7000, "max_tokens": 5000},
    "35-50":  {"skeleton_words": 9000, "max_tokens": 6000},
    "50-70":  {"skeleton_words": 14000, "max_tokens": 10000},  # 大规模报告
    ">70":    {"skeleton_words": 18000, "max_tokens": 12000},  # 超大规模报告
}

# ===== 内容截取长度常量（单位：字符）=====
CONTENT_TRUNCATE = {
    "knowledge_extract": 8000,     # 知识提取时报告截取
    "gptr_report": 15000,          # GPT Researcher报告截取
    "review_chapter": 6000,        # 审稿时章节截取
    "denoise_chapter": 3000,       # 去噪时章节截取
    "material_preview": 250,       # 素材预览截取
    "search_result": 1500,         # 搜索结果内容截取
    "fact_chunk": 3000,            # 事实提取分块大小
    "title_generation": 500,       # 标题生成任务内容截取
    "refine_chapter": 4000,        # 章节润色内容截取
    "plan_generation": 2000,       # 计划书生成内容截取
}

def get_page_config(max_pages: int) -> dict:
    if max_pages < 10:
        key = "<10"
    elif max_pages <= 20:
        key = "10-20"
    elif max_pages <= 35:
        key = "20-35"
    elif max_pages <= 50:
        key = "35-50"
    elif max_pages <= 70:
        key = "50-70"
    else:
        key = ">70"
    cfg = PAGE_CONFIG.get(key, PAGE_CONFIG["default"]).copy()
    cfg["key"] = key
    cfg["skeleton_words"] = min(int(max_pages * 350), 24000)  # 上限提到24000
    cfg["max_tokens"] = min(int(cfg["skeleton_words"] * 1.5 * 1.2), 12000)  # 上限提到12000
    return cfg

# 搜索深度映射 —— search_depth(b/c) 决定 分批数×迭代数 矩阵
#   b = 标准研究: 2批 × 2次迭代 = 4次  ← 宏观→中观→微观精准搜索（默认）
#   c = 深度研究: 3批 × 3次迭代 = 9次  ← 方向复杂、素材稀疏，含自动补搜
SEARCH_DEPTH_MAP = {
    "b": {"batches_hint": 2, "max_iterations": 2, "auto_supplement": True,  "label": "标准研究(2×2=4次)", "search_density_top_k": 7, "aggressive_filter_top_k": 5},
    "c": {"batches_hint": 3, "max_iterations": 3, "auto_supplement": True,  "label": "深度研究(3×3=9次+补搜)", "search_density_top_k": 10, "aggressive_filter_top_k": 5},
}

# GPT Researcher域名黑名单 —— 文档分享平台只返回页面元信息，抓取无价值
GPTR_DOMAIN_BLACKLIST = [
    "docin.com", "doc88.com", "book118.com", "taodocs.com",
    "shejiben.com", "renrendoc.com",
]

# 域名抓取优先级 —— priority>0 正常深挖，priority=0 可见不可抓
DOMAIN_SCRAPING_PRIORITY = {
    # 最高优先级（政府/官方）
    "gov.cn": 10, "xinhuanet.com": 10, "people.com.cn": 10,
    "stats.gov.cn": 10, "mof.gov.cn": 10, "miit.gov.cn": 10,
    # 高优先级（权威机构/交易所）
    "cninfo.com.cn": 8, "sse.com.cn": 8, "szse.cn": 8,
    "iresearch.com.cn": 8, "analysys.cn": 8,
    # 中优先级（主流媒体/行业媒体）
    "thepaper.cn": 6, "caixin.com": 6, "36kr.com": 6,
    "huxiu.com": 6, "wallstreetcn.com": 6, "jiemian.com": 6,
    "cs.com.cn": 6, "stcn.com": 6,
    # 低优先级（社区/问答）
    "zhihu.com": 3, "tieba.baidu.com": 3, "weibo.com": 2,
    # 文档平台（可见不可抓）
    "docin.com": 0, "doc88.com": 0, "book118.com": 0,
    "taodocs.com": 0, "shejiben.com": 0, "renrendoc.com": 0,
}

def get_merged_domain_priority(cfg: dict = None) -> dict:
    """获取合并后的域名优先级（系统默认 + 用户自定义）"""
    if cfg is None:
        cfg = load_config()
    merged = DOMAIN_SCRAPING_PRIORITY.copy()
    user_priority = cfg.get("domain_scraping_priority", {})
    if isinstance(user_priority, dict):
        merged.update(user_priority)
    return merged

DEFAULT_CONFIG = {
    "llm_api_key": "",
    "llm_base_url": "https://api.deepseek.com/v1",
    "plan_model": "deepseek-v4-flash",
    "write_model": "deepseek-v4-flash",
    "search_api_key": "",
    "search_api_endpoint": "https://api.deepseek.com/v1/tools/search",
    "search_provider": "deepseek",
    "search_depth": "b",  # 保留兼容，优先使用下方独立控件值
    "search_batches": 3,     # 搜索批次数 (1=仅宏观, 2=宏观+中观, 3=宏观+中观+微观)
    "auto_supplement": True, # 自动补搜开关（默认开启：搜索后按计划缺口继续补搜）
    "deepseek_cost_per_1k": "0.01",  # DeepSeek每1k token费用(CNY) 约0.01元/千字  # 约0.01元/千字
    "user_domain_blacklist": [],  # 用户自定义域名黑名单
    "ollama_host": "http://localhost:11434",
    "ollama_model": "EntropyYue/jina-embeddings-v2-base-zh:latest",
    "ollama_embed_chunk": 8,
    "ollama_embed_timeout": 120,
    "ollama_query_timeout": 15,
    "ollama_embed_max_chars": 600,
    "ollama_cooldown": 300,
    "output_dir": "",
    "llm_provider": "openai",
    "language": "chinese",
    "report_type": "deep_report",
    "max_pages": 25,
    "report_tone": "Objective",
    "max_search_results": 10,
    "max_scraper_workers": 15,
    "max_iterations": 2,
    "enable_deep_search": True,
    "enable_action_plan": True,
    "enable_excel_appendix": True,
    "enable_browser_enhance": True,
    "temperature_plan": 0.7,
    "temperature_write": 0.5,
    "json_mode_available": True,
    "max_search_budget": 20,
    "enable_fact_compression": True,
    "enable_self_check_writing": True,
    "search_density_top_k": 10,
    "enable_storm_debate": False,
    "aggressive_filter_top_k": 3,
    
    # ===== 配置 =====
    # 功能2: DeepScraper 定向深挖（默认开启）
    "enable_deep_scrape": True,
    "deep_scrape_provider": "deepseek",      # deepseek / crawl4ai
    "deepseek_fetch_endpoint": "https://api.deepseek.com/v1/tools/fetch",
    "crawl4ai_api_url": "",                   # 本地 Crawl4AI 服务地址（可选备用）
    "deep_scrape_top_n": 15,
    "deep_scrape_timeout": 30,
    
    # 功能4: 域名分级
    "domain_scraping_priority": {},           # 用户自定义域名优先级覆盖
    
    # 功能5: 缺口自动补搜（默认开启）
    "auto_fill_final_gaps": True,
    "auto_fill_max_gaps": 3,
    "auto_fill_rounds": 1,
    "auto_fill_sources_per_round": 20,
    
    # 功能8: 搜索成本追踪
    "search_cost_tracking": True,
    "crawl4ai_cost_per_url": 0.0,
    "gptr_cost_per_session": 0.8,
    "gptr_auto_start": True,
    "gptr_start_timeout": 20,
    "gptr_task_timeout": 600,
    "gptr_service_path": "",
    # GPTR 失败/返回空上下文时，是否自动移交 DeepSeek 补搜。
    # 默认关闭：GPTR 是主搜索源，失败应当显式暴露，而不是静默换源。
    "gptr_fallback_to_deepseek": False,
    
    # ===== 搜索缓存配置 =====
    "enable_search_cache": True,           # 启用搜索缓存
    "search_cache_ttl_days": 7,            # 缓存有效期（天）—— 服务工具流同步（洞察→调研→方案），非长期存储
    "search_cache_max_items": 1000,        # 最大缓存条目数
    
    # --补充：被代码引用但之前缺失的默认配置 ----
    "fast_model": "",                          # 快速模型（留空则使用 write_model）
    "v15_mode_forced": False,                  # 强制V15模式（超深度报告）
    "archive_dir": "",                         # 报告归档目录（留空则自动创建）
    
    # ===== 配置 =====
    # 功能: 编造检测
    "enable_fabrication_check": False,         # 开启后检测关键事实是否在素材中有来源依据
    # 功能: 密度不达标闭环
    "density_retry_enabled": False,            # 开启后密度不足时触发1次重写或补搜
    
    # ===== 智能增强配置 =====
    # 功能1: 搜索策略预审（Agent轻量协作）
    #   建议：本地✅ | 常规按需 | 深度✅ — evaluator在plan→search间做查询词可执行性预审
    "enable_search_preflight": False,
    "search_preflight_rewrite": True,          # 预审时允许evaluator改写查询词（False则仅标注不修改）
    
    # 功能2: 认知循环检测
    #   建议：本地✅ | 常规✅ | 深度✅ — 三档全开，纯Ollama embedding余弦相似度检测，零LLM成本
    "enable_loop_detection": True,
    "loop_similarity_threshold": 0.85,         # 余弦相似度阈值（越接近1越严格，深度模式可降至0.80）
    "loop_max_retries": 2,                     # 触发后最大策略切换次数
    
    # 功能5: TERMINATE信息饱和度检测（借鉴AgentCPM-Report）
    #   当连续2轮搜索的新增素材增量<阈值时，提前停止搜索，节省API费用
    #   建议：本地✅ | 常规✅ | 深度❌（深度模式应搜够）
    "enable_terminate_saturation": True,
    "terminate_gain_threshold": 0.05,          # 新增素材增量低于5%视为低增量

    # 功能6: EXPAND素材缺口定向补搜（借鉴AgentCPM-Report）
    #   写作时发现某章节素材不足，自动触发定向补搜获取新素材
    #   核心约束：新内容必须有素材支撑，不增加幻觉风险
    "enable_expand_gap_fill": True,
    "expand_min_materials": 3,                  # 章节素材低于此数触发补搜
    # 两次补搜仍无素材时，用 AI 训练知识填充章节，标题标注【AI训练知识】
    # 关闭后：日志报错，可在【控制面修改】输入关键词（逗号分隔）后点【补搜本章】
    "enable_ai_knowledge_fallback": True,
    
    # 功能5: 质量自动评分（多维报告质量评估）
    #   建议：本地✅ | 常规✅ | 深度✅ — 三档全开，basic模式零LLM成本
    "enable_quality_scoring": True,
    "source_appendix_limit": 50,               # 来源追溯表最多列多少条；0=全部
    "reference_list_limit": 50,                # 参考文献最多列多少条；0=全部
    "quality_dimensions": "basic",             # basic(纯统计:数据密度/来源覆盖/引用完整性) / full(+LLM逻辑一致性)
    "quality_min_data_density": 3.0,           # 数据密度最低标准：每千字至少N个数字
    "quality_min_source_coverage": 3,          # 来源覆盖度最低标准：至少N个不同引用源
}

CONFIG_FILENAME = "research_config.json"

def get_config_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), CONFIG_FILENAME)

def load_config(preset: str = None) -> dict:
    cfg = DEFAULT_CONFIG.copy()
    path = get_config_path()
    if preset:
        pp = os.path.join(os.path.dirname(path), f"research_config_{preset}.json")
        if os.path.exists(pp):
            path = pp
            logger.info(f"[配置] 加载预设: {preset}")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if "deepseek_api_key" in loaded and not loaded.get("llm_api_key"):
                    loaded["llm_api_key"] = loaded.pop("deepseek_api_key", "")
                if "deepseek_base_url" in loaded and not loaded.get("llm_base_url"):
                    loaded["llm_base_url"] = loaded.pop("deepseek_base_url", "")
                cfg.update(loaded)
        except Exception as e:
            logger.warning(f"配置读取失败: {e}，使用默认配置")
    if not cfg["output_dir"]:
        cfg["output_dir"] = os.path.join(os.path.expanduser("~"), "Desktop", "docs")
    return cfg

def save_config(cfg: dict) -> bool:
    path = get_config_path()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.error(f"配置保存失败: {e}")
        return False

def apply_config_to_env(cfg: dict):
    """简化配置传递——直接将配置存入os.environ供GPTR微服务读取。
    
    不再生成.env文件，避免配置双轨制。GPTR微服务通过环境变量读取配置。
    如果未来GPTR微服务支持直接接收config dict，可进一步简化。
    """
    # API Key直接存入（GPTR微服务需要）
    os.environ["OPENAI_API_KEY"] = cfg.get("llm_api_key", "")
    os.environ["OPENAI_API_BASE"] = cfg.get("llm_base_url", "")
    os.environ["OPENAI_BASE_URL"] = cfg.get("llm_base_url", "")
    os.environ["SMART_LLM"] = f"{cfg.get('llm_provider', 'openai')}:{cfg.get('plan_model', '')}"
    os.environ["FAST_LLM"] = f"{cfg.get('llm_provider', 'openai')}:{cfg.get('write_model', '')}"
    os.environ["STRATEGIC_LLM"] = f"{cfg.get('llm_provider', 'openai')}:{cfg.get('plan_model', '')}"
    os.environ["RETRIEVER"] = "deepseek"
    os.environ["SEARCH_PROVIDER"] = "deepseek"
    os.environ["DEEPSEEK_API_KEY"] = cfg.get("search_api_key", "") or cfg.get("llm_api_key", "")
    os.environ["DEEPSEEK_SEARCH_ENDPOINT"] = cfg.get(
        "search_api_endpoint", "https://api.deepseek.com/v1/tools/search"
    )
    os.environ["OLLAMA_BASE_URL"] = cfg.get("ollama_host", "")
    os.environ["EMBEDDING"] = f"ollama:{cfg.get('ollama_model', '')}"
    os.environ["LANGUAGE"] = cfg.get("language", "chinese")
    os.environ["REPORT_TYPE"] = cfg.get("report_type", "research_report")
    os.environ["REPORT_TONE"] = cfg.get("report_tone", "Objective")
    os.environ["MAX_SEARCH_RESULTS_PER_QUERY"] = str(cfg.get("max_search_results", 10))
    os.environ["MAX_SCRAPER_WORKERS"] = str(cfg.get("max_scraper_workers", 15))
    os.environ["MAX_ITERATIONS"] = str(cfg.get("max_iterations", 1))
    os.environ["DOC_PATH"] = cfg.get("output_dir", "") or os.path.join(os.path.expanduser("~"), "Desktop", "docs")
    os.environ["TEMPERATURE"] = str(cfg.get("temperature_write", 0.7))

# 从shared模块导入safe_json_loads工具函数
# 注意：from shared.xxx import 需要 shared 的父目录（代码包/）在 sys.path 中
# 同时 shared/search/ 不能遮挡本地的 search.py，所以调研工具目录必须在 path 最前面
import sys
from pathlib import Path
_tool_dir = str(Path(__file__).parent)  # 调研工具目录
_code_root = str(Path(__file__).parent.parent)  # 代码包根目录（shared的父目录）
if _tool_dir not in sys.path:
    sys.path.insert(0, _tool_dir)  # 调研工具目录最优先
if _code_root not in sys.path:
    sys.path.insert(1, _code_root)  # 代码包根目录第二优先（from shared.xxx 需要）
from shared.llm_client import safe_json_loads as _shared_safe_json_loads, extract_json_text

# 保留本地的extract_json函数以兼容旧代码
def extract_json(text: str) -> str:
    """从文本中提取JSON（使用shared版本）"""
    return extract_json_text(text)

def safe_json_loads(text: str, default=None):
    """安全解析JSON（使用shared版本）"""
    return _shared_safe_json_loads(text, default)


class LLMClient:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._client: Optional[OpenAI] = None
        self._ensure_client()
        # 借鉴 mini-coding-agent: Prompt 缓存复用
        from shared.prompt_cache import CachedPromptBuilder
        self._prompt_builder = CachedPromptBuilder(max_cache_size=8)
        # token 统计回调（可选）
        self._token_callback = None  # callable(total_tokens: int)

    def _ensure_client(self):
        key = self.cfg.get("llm_api_key", "")
        # 配置文件 Key 优先；环境变量仅作兜底，避免旧环境变量覆盖有效配置
        if not key:
            key = os.environ.get("OPENAI_API_KEY", "")
        # 解密（如果是密文）
        key = decrypt_api_key(key)
        if key and self._client is None:
            self._client = OpenAI(
                api_key=key,
                base_url=self.cfg.get("llm_base_url", "https://api.deepseek.com/v1"),
                timeout=120
            )

    def reload(self, cfg: dict):
        self.cfg = cfg
        self._client = None
        self._ensure_client()

    @property
    def available(self) -> bool:
        return self._client is not None

    def chat(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: int = 4000,
        json_mode: bool = False,
        timeout: int = 120,
        system: Optional[str] = None,
    ) -> str:
        if not self.available:
            raise RuntimeError("大模型 API Key 未配置")
        m = model or self.cfg.get("write_model", "deepseek-v4-flash")
        temp = temperature if temperature is not None else self.cfg.get("temperature_write", 0.7)
        # 复制消息字典，避免改写调用方传入的首条 system 消息
        msgs = [dict(m) for m in messages]
        if system:
            if msgs and msgs[0].get("role") == "system":
                msgs[0]["content"] = system
            else:
                msgs.insert(0, {"role": "system", "content": system})

        # 追加安全后缀到 system prompt
        try:
            from shared.prompt_security import SECURITY_SUFFIX
            if msgs and msgs[0].get("role") == "system":
                if SECURITY_SUFFIX not in msgs[0]["content"]:
                    msgs[0]["content"] = msgs[0]["content"] + "\n\n" + SECURITY_SUFFIX
        except ImportError:
            pass

        kwargs = {
            "model": m, "messages": msgs, "temperature": temp,
            "max_tokens": max_tokens, "timeout": timeout,
        }
        use_json_mode = json_mode and self.cfg.get("json_mode_available", True)
        for attempt in range(3):
            try:
                if use_json_mode and attempt == 0:
                    kwargs["response_format"] = {"type": "json_object"}
                elif "response_format" in kwargs:
                    del kwargs["response_format"]
                resp = self._client.chat.completions.create(**kwargs)
                result = resp.choices[0].message.content or ""
                # token 统计回调
                if self._token_callback and hasattr(resp, 'usage') and resp.usage:
                    try:
                        self._token_callback(resp.usage.total_tokens)
                    except Exception:
                        pass
                # Prompt 缓存统计（每 50 次调用输出一次）
                stats = self._prompt_builder.get_stats()
                if stats["total_builds"] > 0 and stats["total_builds"] % 50 == 0:
                    logger.debug(f"[PromptCache] {stats}")
                return result
            except Exception as e:
                err_msg = str(e)
                if "response_format" in err_msg.lower() or "json_object" in err_msg.lower():
                    logger.warning(f"[API] response_format 不支持，降级重试")
                    use_json_mode = False
                else:
                    logger.warning(f"[API错误] {e} (attempt {attempt+1}/3)")
                if attempt < 2:
                    time.sleep(2 ** attempt)
        raise RuntimeError("大模型 API 连续失败")

    def plan(self, messages, **kwargs) -> str:
        system = AGENT_PROMPTS.get("planner", "")
        # 借鉴 mini-coding-agent: 缓存静态前缀
        self._prompt_builder.build(system, {"user": str(messages)})
        return self.chat(
            messages,
            model=self.cfg.get("plan_model", "deepseek-v4-flash"),
            temperature=kwargs.pop("temperature", self.cfg.get("temperature_plan", 0.7)),
            system=system,
            **kwargs
        )

    def clarify(self, messages, **kwargs) -> str:
        system = AGENT_PROMPTS.get("clarifier", "")
        self._prompt_builder.build(system, {"user": str(messages)})
        return self.chat(
            messages,
            model=self.cfg.get("plan_model", "deepseek-v4-flash"),
            temperature=kwargs.pop("temperature", 0.7),
            system=system,
            **kwargs
        )

    def fast(self, messages, system=None, **kwargs) -> str:
        """快速调用（默认使用writer角色，但可自定义system prompt）"""
        system = system if system is not None else AGENT_PROMPTS.get("writer", "")
        self._prompt_builder.build(system, {"user": str(messages)})
        return self.chat(
            messages,
            model=self.cfg.get("write_model", "deepseek-v4-flash"),
            temperature=kwargs.pop("temperature", self.cfg.get("temperature_write", 0.7)),
            system=system,
            **kwargs
        )

    def factcheck(self, messages, **kwargs) -> str:
        system = AGENT_PROMPTS.get("factcheck", "")
        self._prompt_builder.build(system, {"user": str(messages)})
        return self.chat(
            messages,
            model=self.cfg.get("plan_model", "deepseek-v4-flash"),
            temperature=kwargs.pop("temperature", 0.3),
            system=system,
            **kwargs
        )

    def editor(self, messages, **kwargs) -> str:
        system = AGENT_PROMPTS.get("editor", "")
        self._prompt_builder.build(system, {"user": str(messages)})
        return self.chat(
            messages,
            model=self.cfg.get("plan_model", "deepseek-v4-flash"),
            temperature=kwargs.pop("temperature", 0.5),
            system=system,
            **kwargs
        )

def normalize_query(q: str) -> str:
    q = re.sub(r'20\d{2}年?', '', q)
    q = re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9]', '', q)
    return q.lower().strip()

def current_year_anchor() -> str:
    return datetime.now().strftime('%Y年%m月%d日')

# ─── SearchResult / SourceGrade / grade_source ───
# 统一从 shared/search/models.py 导入，消除重复定义
# 移除本地副本，统一使用 shared 版本
from shared.search.models import SourceGrade, SearchResult, grade_source
