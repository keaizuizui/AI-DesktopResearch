#!/usr/bin/env python3
"""
保活机制 - 防止服务被系统关闭
"""
import asyncio
import threading
import time
import logging
from typing import Optional
import requests


class KeepAliveManager:
    """
    服务保活管理器
    
    功能：
    1. 自 ping - 定期访问自己的健康检查端点
    2. 内存监控 - 触发 GC 防止内存泄漏
    3. 空闲检测 - 长时间无任务时降低资源占用
    """
    
    def __init__(self, 
                 port: int = 8000,
                 interval: int = 30,
                 memory_threshold: int = 2000,  # MB
                 idle_threshold: int = 300):    # 秒
        self.port = port
        self.interval = interval
        self.memory_threshold = memory_threshold
        self.idle_threshold = idle_threshold
        self.logger = logging.getLogger("keepalive")
        
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._last_request_time = time.time()
        self._request_count = 0
        
    def start(self):
        """启动保活线程"""
        if self._running:
            return
        if self._thread and self._thread.is_alive():
            self._stop_event.set()
            self._thread.join(timeout=5)
        
        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self.logger.info(f"[KeepAlive] 已启动，间隔 {self.interval} 秒")
    
    def stop(self):
        """停止保活线程"""
        self._running = False
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.logger.info("[KeepAlive] 已停止")
    
    def record_request(self):
        """记录请求时间（由 API 层调用）"""
        self._last_request_time = time.time()
        self._request_count += 1
    
    def _loop(self):
        """保活主循环"""
        while self._running:
            try:
                self._do_ping()
                self._check_memory()
                self._check_idle()
                self._stop_event.wait(self.interval)
            except Exception as e:
                self.logger.error(f"[KeepAlive] 错误: {e}")
                self._stop_event.wait(5)
    
    def _do_ping(self):
        """自 ping 健康检查端点"""
        try:
            url = f"http://127.0.0.1:{self.port}/api/v1/health"
            response = requests.get(url, timeout=5)
            if response.status_code == 200:
                self.logger.debug(f"[KeepAlive] 自 ping 成功")
            else:
                self.logger.warning(f"[KeepAlive] 自 ping 返回 {response.status_code}")
        except requests.exceptions.ConnectionError:
            self.logger.error("[KeepAlive] 自 ping 失败 - 服务可能已停止")
        except requests.exceptions.Timeout:
            self.logger.warning("[KeepAlive] 自 ping 超时")
        except Exception as e:
            self.logger.error(f"[KeepAlive] 自 ping 错误: {e}")
    
    def _check_memory(self):
        """检查内存使用"""
        try:
            import psutil
            process = psutil.Process()
            mem_mb = process.memory_info().rss / 1024 / 1024
            
            if mem_mb > self.memory_threshold:
                import gc
                gc.collect()
                self.logger.warning(f"[KeepAlive] 内存使用 {mem_mb:.0f}MB，已触发 GC")
            elif mem_mb > self.memory_threshold * 0.75:
                self.logger.info(f"[KeepAlive] 内存使用 {mem_mb:.0f}MB，偏高")
        except ImportError:
            pass  # psutil 未安装
    
    def _check_idle(self):
        """检查空闲状态"""
        idle_time = time.time() - self._last_request_time
        if idle_time > self.idle_threshold:
            self.logger.info(f"[KeepAlive] 空闲 {idle_time:.0f} 秒，进入低功耗模式")
            # 可以在这里执行资源释放操作


class SystemKeepAlive:
    """
    系统级保活 - 防止 Windows 休眠/关闭服务
    
    功能：
    1. 阻止系统休眠
    2. 设置进程优先级
    """
    
    def __init__(self):
        self.logger = logging.getLogger("system_keepalive")
        self._original_priority = None
        
    def prevent_sleep(self):
        """阻止系统休眠（Windows）"""
        try:
            import ctypes
            # ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED
            result = ctypes.windll.kernel32.SetThreadExecutionState(0x80000042)
            if result:
                self.logger.info("[SystemKeepAlive] 已阻止系统休眠")
            else:
                self.logger.warning("[SystemKeepAlive] 阻止休眠未生效（返回 0）")
        except Exception as e:
            self.logger.warning(f"[SystemKeepAlive] 阻止休眠失败: {e}")
    
    def allow_sleep(self):
        """允许系统休眠"""
        try:
            import ctypes
            # ES_CONTINUOUS
            result = ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
            if result:
                self.logger.info("[SystemKeepAlive] 已允许系统休眠")
            else:
                self.logger.warning("[SystemKeepAlive] 允许休眠未生效（返回 0）")
        except Exception as e:
            self.logger.warning(f"[SystemKeepAlive] 允许休眠失败: {e}")
    
    def set_high_priority(self):
        """设置高进程优先级"""
        try:
            import psutil
            process = psutil.Process()
            self._original_priority = process.nice()
            if hasattr(process, "ionice"):
                process.ionice(psutil.IOPRIO_CLASS_RT)
            self.logger.info("[SystemKeepAlive] 已设置高优先级")
        except Exception as e:
            self.logger.warning(f"[SystemKeepAlive] 设置优先级失败: {e}")
    
    def restore_priority(self):
        """恢复原始优先级"""
        if self._original_priority is not None:
            try:
                import psutil
                process = psutil.Process()
                process.nice(self._original_priority)
                self.logger.info("[SystemKeepAlive] 已恢复优先级")
            except Exception as e:
                self.logger.warning(f"[SystemKeepAlive] 恢复优先级失败: {e}")


# 全局实例
keepalive = KeepAliveManager()
system_keepalive = SystemKeepAlive()
