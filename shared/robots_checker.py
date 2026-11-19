"""
shared/robots_checker — robots.txt 合规检查
============================================

在抓取网页前检查目标网站的 robots.txt，尊重站点抓取规则。
使用标准库 urllib.robotparser，零外部依赖。

用法：
    from shared.robots_checker import can_fetch
    if can_fetch(url, user_agent="MyBot"):
        # 安全抓取
    else:
        # 跳过或使用 API 代理
"""

import logging
import threading
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

logger = logging.getLogger(__name__)

# 缓存已解析的 robots.txt（域名级）
_cache: dict[str, RobotFileParser] = {}
_cache_lock = threading.Lock()

# 默认 User-Agent（用于 robots.txt 匹配）
DEFAULT_UA = "Mozilla/5.0 (compatible; MarketingResearchBot/1.0)"


def _parse_robots(domain: str) -> RobotFileParser:
    """解析指定域名的 robots.txt（带缓存）"""
    with _cache_lock:
        if domain in _cache:
            return _cache[domain]

    rp = RobotFileParser()
    try:
        robots_url = f"https://{domain}/robots.txt"
        rp.set_url(robots_url)
        rp.read()
        rp.entries  # 触发解析
        logger.debug(f"[robots.txt] 已解析: {domain}")
    except Exception as e:
        logger.debug(f"[robots.txt] 解析失败 {domain}: {e}")
        # 解析失败时视为允许（保守策略：不因网络问题阻断工作流）

    with _cache_lock:
        _cache[domain] = rp
    return rp


def can_fetch(url: str, user_agent: str = DEFAULT_UA) -> bool:
    """
    检查是否允许抓取指定 URL

    规则：
    - robots.txt 存在且明确禁止 → 返回 False
    - robots.txt 不存在或解析失败 → 返回 True（不阻断工作流）
    - robots.txt 未提及该路径 → 返回 True

    Args:
        url: 目标 URL
        user_agent: 用于匹配的 User-Agent 名称

    Returns:
        True = 允许抓取, False = 禁止抓取
    """
    try:
        parsed = urlparse(url)
        domain = parsed.hostname
        if not domain:
            return True  # 无域名的 URL（如本地文件），直接允许

        # 内网地址始终允许
        if domain in ("localhost", "127.0.0.1") or domain.startswith(("192.168.", "10.", "172.16.")):
            return True

        rp = _parse_robots(domain)
        path = parsed.path or "/"
        allowed = rp.can_fetch(user_agent, path)

        if not allowed:
            logger.info(f"[robots.txt] 禁止抓取: {url[:80]}... (UA: {user_agent})")

        return allowed
    except Exception:
        return True  # 异常时不阻断


def clear_cache(domain: str = None):
    """清除缓存（域名级或全部）"""
    with _cache_lock:
        if domain:
            _cache.pop(domain, None)
        else:
            _cache.clear()
