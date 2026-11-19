#!/usr/bin/env python3
"""
GPT-Researcher Service HTTP 客户端
供调研工具、方案工具、洞察工具调用
"""
import asyncio
import logging
import time
from typing import Optional, Dict, Any

import httpx


logger = logging.getLogger("gptr_client")


class GPTResearcherClient:
    """
    GPT-Researcher 微服务 HTTP 客户端
    
    替代原有的直接 import gpt_researcher 调用方式，
    通过 HTTP 请求与 GPT-Researcher Service 通信。
    """
    
    def __init__(self, base_url: str = "http://127.0.0.1:8000/api/v1"):
        self.base_url = base_url.rstrip("/")
        self.timeout = httpx.Timeout(connect=10.0, read=600.0, write=10.0, pool=10.0)
        self._client: Optional[httpx.AsyncClient] = None
    
    async def _get_client(self) -> httpx.AsyncClient:
        """获取或创建 HTTP 客户端"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout)
        return self._client
    
    async def close(self):
        """关闭客户端"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
    
    async def submit(
        self,
        query: str,
        report_type: str = "research_report",
        tone: str = "Objective",
        config: Optional[Dict[str, Any]] = None,
        priority: int = 2,
        timeout: int = 600,
    ) -> str:
        """
        提交研究任务并等待结果（同步模式）
        
        Args:
            query: 研究查询
            report_type: 报告类型
            tone: 语气
            config: 任务级配置覆盖
            priority: 优先级 1-4
            timeout: 超时秒数
            
        Returns:
            研究报告文本
        """
        client = await self._get_client()
        
        # 提交任务
        resp = await client.post("/research", json={
            "query": query,
            "report_type": report_type,
            "tone": tone,
            "config": config or {},
            "priority": priority,
            "timeout": timeout,
        })
        resp.raise_for_status()
        task_id = resp.json()["task_id"]
        logger.info(f"[GPTR Client] 任务已提交: {task_id}")
        
        # 轮询等待结果（用单调时钟覆盖 HTTP 请求耗时）
        poll_interval = 3
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(poll_interval, remaining))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            
            try:
                resp = await asyncio.wait_for(
                    client.get(f"/research/{task_id}"),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                break
            resp.raise_for_status()
            task = resp.json()
            
            status = task["status"]
            if status == "completed":
                elapsed = timeout - max(0.0, deadline - time.monotonic())
                logger.info(f"[GPTR Client] 任务完成: {task_id}, 耗时 {elapsed:.0f}s")
                return task["result"]
            elif status in ("failed", "timeout", "cancelled"):
                error = task.get("error", "未知错误")
                logger.error(f"[GPTR Client] 任务失败: {task_id}, 原因: {error}")
                raise RuntimeError(f"GPT-Researcher 任务失败: {error}")
            # running / pending → 继续等待
        
        raise TimeoutError(f"GPT-Researcher 任务超时: {task_id}")
    
    async def submit_async(
        self,
        query: str,
        report_type: str = "research_report",
        tone: str = "Objective",
        config: Optional[Dict[str, Any]] = None,
        priority: int = 2,
        timeout: int = 600,
    ) -> str:
        """
        提交研究任务（异步模式，只返回 task_id）
        
        Returns:
            task_id: 任务ID
        """
        client = await self._get_client()
        
        resp = await client.post("/research", json={
            "query": query,
            "report_type": report_type,
            "tone": tone,
            "config": config or {},
            "priority": priority,
            "timeout": timeout,
        })
        resp.raise_for_status()
        task_id = resp.json()["task_id"]
        logger.info(f"[GPTR Client] 异步任务已提交: {task_id}")
        return task_id
    
    async def get_status(self, task_id: str) -> Dict[str, Any]:
        """查询任务状态"""
        client = await self._get_client()
        resp = await client.get(f"/research/{task_id}")
        resp.raise_for_status()
        return resp.json()
    
    async def cancel(self, task_id: str) -> bool:
        """取消任务"""
        client = await self._get_client()
        try:
            resp = await client.delete(f"/research/{task_id}")
            return resp.status_code == 200
        except Exception:
            return False
    
    async def health(self) -> Dict[str, Any]:
        """健康检查"""
        client = await self._get_client()
        resp = await client.get("/health")
        resp.raise_for_status()
        return resp.json()


# 全局客户端实例
# 修复: 使用懒加载避免资源泄漏，提供atexit关闭
gptr_client: Optional[GPTResearcherClient] = None

def get_gptr_client() -> GPTResearcherClient:
    """获取全局GPTR客户端实例（懒加载）"""
    global gptr_client
    if gptr_client is None:
        gptr_client = GPTResearcherClient()
    return gptr_client

async def close_gptr_client():
    """关闭全局GPTR客户端"""
    global gptr_client
    if gptr_client:
        await gptr_client.close()
        gptr_client = None

# 注册程序退出时关闭
import atexit
def _cleanup():
    """清理资源（atexit 无运行中的事件循环）。"""
    try:
        if gptr_client and gptr_client._client and not gptr_client._client.is_closed:
            asyncio.run(gptr_client._client.aclose())
    except Exception as e:
        logger.debug("退出时关闭客户端失败（已忽略）: %s", e)

atexit.register(_cleanup)
