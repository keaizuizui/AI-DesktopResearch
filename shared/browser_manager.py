#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BrowserManager —— 统一管理浏览器生命周期
优先使用 CloakBrowser（隐身Chromium），降级到 Playwright。

CloakBrowser: 源码级C++指纹补丁，30/30反机器人检测全过
Playwright: 标准浏览器自动化（降级方案）

替代 search.py / deep_scraper.py 中的重复初始化代码。
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ── CloakBrowser 优先 ──
try:
    from cloakbrowser import launch as cloak_launch
    CLOAK_AVAILABLE = True
    logger.info("[BrowserManager] CloakBrowser 可用，将使用隐身浏览器")
except ImportError:
    CLOAK_AVAILABLE = False
    logger.debug("[BrowserManager] CloakBrowser 未安装，将降级到 Playwright")

# ── Playwright 降级 ──
try:
    from playwright.sync_api import sync_playwright, Browser, Page
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    Browser = None
    Page = None

# 任一可用即可工作
BROWSER_AVAILABLE = CLOAK_AVAILABLE or PLAYWRIGHT_AVAILABLE


class BrowserManager:
    """统一浏览器管理类，负责浏览器的初始化、页面获取和资源释放。
    
    策略：
    1. CloakBrowser 可用 → 使用隐身浏览器（反检测30/30全过）
    2. CloakBrowser 不可用 → 降级到 Playwright 标准模式
    3. 都不可用 → 所有操作返回 None
    """

    def __init__(self, headless: bool = True, humanize: bool = False):
        """
        Args:
            headless: 是否无头模式（默认True）
            humanize: 是否模拟人类行为（仅CloakBrowser支持，鼠标贝塞尔曲线+逐字输入）
        """
        self._headless = headless
        self._humanize = humanize
        self._playwright = None  # 仅Playwright模式需要
        self._browser = None
        self._using_cloak = False  # 标记当前使用的引擎

    def ensure_browser(self):
        """获取或初始化浏览器实例。优先CloakBrowser，降级Playwright。"""
        if self._browser and hasattr(self._browser, 'contexts'):
            return self._browser

        # 策略1: CloakBrowser
        if CLOAK_AVAILABLE:
            try:
                launch_kwargs = {"headless": self._headless}
                if self._humanize:
                    launch_kwargs["humanize"] = True
                self._browser = cloak_launch(**launch_kwargs)
                self._using_cloak = True
                logger.info("[BrowserManager] CloakBrowser 隐身浏览器已启动")
                return self._browser
            except Exception as e:
                logger.warning(f"[BrowserManager] CloakBrowser 启动失败，降级到 Playwright: {e}")

        # 策略2: Playwright 降级
        if PLAYWRIGHT_AVAILABLE:
            try:
                self._playwright = sync_playwright().start()
                self._browser = self._playwright.chromium.launch(headless=self._headless)
                self._using_cloak = False
                logger.info("[BrowserManager] Playwright 浏览器已启动（标准模式）")
                return self._browser
            except Exception as e:
                logger.error(f"[BrowserManager] Playwright 启动失败: {e}")
                return None

        logger.warning("[BrowserManager] 无可用浏览器引擎（CloakBrowser和Playwright均未安装）")
        return None

    def get_page(self):
        """获取一个新页面实例。"""
        browser = self.ensure_browser()
        if browser:
            try:
                return browser.new_page(viewport={"width": 1920, "height": 1080})
            except Exception as e:
                logger.error(f"[BrowserManager] 创建页面失败: {e}")
        return None

    @property
    def using_cloak(self) -> bool:
        """当前是否使用CloakBrowser隐身模式"""
        return self._using_cloak

    @property
    def available(self) -> bool:
        """是否有可用浏览器引擎"""
        return BROWSER_AVAILABLE

    def close(self):
        """释放浏览器资源。"""
        try:
            if self._browser:
                self._browser.close()
                self._browser = None
        except Exception as e:
            logger.debug(f"[BrowserManager] 关闭浏览器: {e}")
        try:
            if self._playwright:
                self._playwright.stop()
                self._playwright = None
        except Exception as e:
            logger.debug(f"[BrowserManager] 停止 Playwright: {e}")

    def __enter__(self):
        self.ensure_browser()
        return self

    def __exit__(self, *args):
        self.close()
