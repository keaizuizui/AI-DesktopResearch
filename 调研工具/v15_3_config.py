# 新增配置常量（独立文件，避免破坏config.py结构）

# 信源分级体系（数值评分 + 字母等级）
# S级(0.95-1.0): 国家级官方、顶级权威机构
# A级(0.80-0.94): 主流媒体、知名智库、上市公司财报
# B级(0.60-0.79): 行业媒体、垂直平台、自媒体
# C级(<0.60): 论坛、问答、未验证来源

SOURCE_CREDIBILITY = {
    # S级 - 国家级官方
    "gov.cn": 1.0,
    "stats.gov.cn": 0.98,
    "ndrc.gov.cn": 0.98,
    "mof.gov.cn": 0.98,
    "cbirc.gov.cn": 0.98,
    "csrc.gov.cn": 0.98,
    "miit.gov.cn": 0.97,
    "samr.gov.cn": 0.97,
    "xinhuanet.com": 0.96,
    "people.com.cn": 0.96,
    "cctv.com": 0.96,
    
    # A级 - 主流媒体、权威机构
    "thepaper.cn": 0.92,           # 澎湃新闻
    "caixin.com": 0.92,            # 财新
    "21jingji.com": 0.90,          # 21经济
    "jjckb.cn": 0.90,              # 经济参考报
    "stcn.com": 0.90,              # 证券时报
    "cs.com.cn": 0.90,             # 中国证券报
    "cninfo.com.cn": 0.94,         # 巨潮资讯（上市公司公告）
    "sse.com.cn": 0.94,            # 上交所
    "szse.cn": 0.94,               # 深交所
    "hkexnews.hk": 0.94,           # 港交所
    "sec.gov": 0.94,               # 美国SEC
    "iresearch.com.cn": 0.90,      # 艾瑞咨询
    "analysys.cn": 0.88,           # 易观
    "cbndata.com": 0.85,           # CBNData
    "questmobile.com.cn": 0.85,    # QuestMobile
    
    # B级 - 行业媒体、垂直平台
    "36kr.com": 0.78,              # 36氪
    "huxiu.com": 0.75,             # 虎嗅
    "iheima.com": 0.72,            # i黑马
    "cyzone.cn": 0.72,             # 创业邦
    "iyiou.com": 0.70,             # 亿欧
    "pedaily.cn": 0.70,            # 投资界
    "chinaventure.com.cn": 0.70,   # 投中网
    "jiemian.com": 0.72,           # 界面
    "wallstreetcn.com": 0.75,      # 华尔街见闻
    "cls.cn": 0.75,                # 财联社
    "yicai.com": 0.78,             # 第一财经
    "hexun.com": 0.72,             # 和讯
    "jrj.com": 0.70,               # 金融界
    "csdn.net": 0.65,              # CSDN
    "zhihu.com": 0.60,             # 知乎
    "baijiahao.baidu.com": 0.62,   # 百度百家号
    "sohu.com": 0.65,              # 搜狐
    "163.com": 0.68,               # 网易
    "sina.com.cn": 0.68,           # 新浪
    "ifeng.com": 0.68,             # 凤凰
    
    # C级 - 论坛、问答、未验证
    "tieba.baidu.com": 0.45,
    "weibo.com": 0.50,
    "douban.com": 0.55,
    "xiaohongshu.com": 0.50,
    "douyin.com": 0.50,
    "bilibili.com": 0.55,
    "default": 0.60
}

# 信源等级映射（数值 -> 字母等级）
def get_source_tier(score: float) -> str:
    """根据可信度评分返回字母等级"""
    if score >= 0.95:
        return "S"
    elif score >= 0.80:
        return "A"
    elif score >= 0.60:
        return "B"
    else:
        return "C"

def get_source_tier_color(tier: str) -> str:
    """返回等级对应的颜色（用于前端展示）"""
    colors = {
        "S": "#d4af37",  # 金色
        "A": "#27ae60",  # 绿色
        "B": "#3498db",  # 蓝色
        "C": "#95a5a6",  # 灰色
    }
    return colors.get(tier, "#95a5a6")

# 等级描述

CHAPTER_BUDGET = {
    "市场规模": {"max_tokens": 2500},
    "竞争格局": {"max_tokens": 2500},
    "企业": {"max_tokens": 2000},
    "商业模式": {"max_tokens": 2000},
    "消费者": {"max_tokens": 1500},
    "趋势": {"max_tokens": 1500},
    "风险": {"max_tokens": 1200},
    "建议": {"max_tokens": 1200},
    "摘要": {"max_tokens": 800},
    "概况": {"max_tokens": 1000},
    "default": {"max_tokens": 1500}
}
