#!/usr/bin/env python3
"""
任务队列管理 - 支持优先级、超时、并发控制
"""
import asyncio
import time
import uuid
import logging
from enum import Enum
from typing import Dict, Any, Optional, List, Callable
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


class TaskStatus(Enum):
    """任务状态"""
    PENDING = "pending"      # 等待中
    RUNNING = "running"      # 运行中
    COMPLETED = "completed"  # 完成
    FAILED = "failed"        # 失败
    CANCELLED = "cancelled"  # 已取消
    TIMEOUT = "timeout"      # 超时


class TaskPriority(Enum):
    """任务优先级"""
    LOW = 1
    NORMAL = 2
    HIGH = 3
    URGENT = 4


@dataclass
class ResearchTask:
    """研究任务"""
    task_id: str
    query: str
    report_type: str = "research_report"
    tone: str = "Objective"
    config: Dict[str, Any] = field(default_factory=dict)
    priority: TaskPriority = TaskPriority.NORMAL
    timeout: int = 600  # 秒
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    status: TaskStatus = TaskStatus.PENDING
    progress: int = 0  # 0-100
    stage: str = ""   # 当前阶段描述
    result: Optional[str] = None
    error: Optional[str] = None
    callback_url: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "task_id": self.task_id,
            "query": self.query,
            "report_type": self.report_type,
            "tone": self.tone,
            "priority": self.priority.value,
            "timeout": self.timeout,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "status": self.status.value,
            "progress": self.progress,
            "stage": self.stage,
            "result": self.result,
            "error": self.error,
        }


class TaskQueue:
    """
    异步任务队列 - 支持优先级和并发控制
    """
    def __init__(self, max_concurrent: int = 3, max_terminal_tasks: int = 200):
        self.max_concurrent = max_concurrent
        self.max_terminal_tasks = max_terminal_tasks
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._tasks: Dict[str, ResearchTask] = {}
        self._running: Dict[str, asyncio.Task] = {}
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._callbacks: List[Callable[[ResearchTask], None]] = []
        self._shutdown = False
        self._started = False

    def configure(self, max_concurrent: int):
        """在启动前应用配置的并发上限。"""
        max_concurrent = max(1, int(max_concurrent))
        if max_concurrent == self.max_concurrent:
            return
        if self._started:
            logger.warning("[Queue] 调度器已启动，并发上限变更需重启服务")
            return
        self.max_concurrent = max_concurrent
        self._semaphore = asyncio.Semaphore(max_concurrent)

    def _prune_terminal(self):
        """只保留最近的终态任务，避免任务表无限增长。"""
        terminal = [
            t for t in self._tasks.values()
            if t.status in (
                TaskStatus.COMPLETED, TaskStatus.FAILED,
                TaskStatus.CANCELLED, TaskStatus.TIMEOUT,
            )
        ]
        overflow = len(terminal) - self.max_terminal_tasks
        if overflow <= 0:
            return
        terminal.sort(key=lambda t: t.completed_at or t.created_at)
        for task in terminal[:overflow]:
            self._tasks.pop(task.task_id, None)
        
    async def submit(self, 
                     query: str,
                     report_type: str = "research_report",
                     tone: str = "Objective",
                     config: Optional[Dict[str, Any]] = None,
                     priority: TaskPriority = TaskPriority.NORMAL,
                     timeout: int = 600,
                     callback_url: Optional[str] = None) -> str:
        """
        提交任务
        
        Returns:
            task_id: 任务ID
        """
        if callback_url:
            raise ValueError("callback_url 暂不支持，请使用任务状态轮询")
        task_id = f"r-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex}"
        while task_id in self._tasks:
            task_id = f"r-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex}"
        task = ResearchTask(
            task_id=task_id,
            query=query,
            report_type=report_type,
            tone=tone,
            config=config or {},
            priority=priority,
            timeout=timeout,
            callback_url=callback_url
        )
        
        self._tasks[task_id] = task
        # 优先级队列: (优先级值, 创建时间, 任务ID) 确保可比较
        await self._queue.put((-priority.value, task.created_at, task_id))
        
        return task_id
    
    async def submit_batch(self, tasks: List[Dict[str, Any]]) -> List[str]:
        """批量提交任务"""
        task_ids = []
        for task_data in tasks:
            tid = await self.submit(
                query=task_data["query"],
                report_type=task_data.get("report_type", "research_report"),
                tone=task_data.get("tone", "Objective"),
                config=task_data.get("config"),
                priority=TaskPriority(task_data.get("priority", 2)),
                timeout=task_data.get("timeout", 600),
                callback_url=task_data.get("callback_url")
            )
            task_ids.append(tid)
        return task_ids
    
    def get_task(self, task_id: str) -> Optional[ResearchTask]:
        """获取任务信息"""
        return self._tasks.get(task_id)
    
    def get_all_tasks(self) -> List[ResearchTask]:
        """获取所有任务"""
        return list(self._tasks.values())
    
    def get_running_tasks(self) -> List[ResearchTask]:
        """获取运行中的任务"""
        return [t for t in self._tasks.values() if t.status == TaskStatus.RUNNING]
    
    def get_pending_tasks(self) -> List[ResearchTask]:
        """获取等待中的任务"""
        return [t for t in self._tasks.values() if t.status == TaskStatus.PENDING]
    
    def _finalize(self, task: ResearchTask):
        """统一终态收尾：补完成时间、通知回调、裁剪历史。"""
        if getattr(task, "_finalized", False):
            return
        task._finalized = True
        if task.completed_at is None:
            task.completed_at = time.time()
        for callback in self._callbacks:
            try:
                callback(task)
            except Exception as e:
                logger.debug(f"队列回调异常: {e}")
        self._prune_terminal()

    async def cancel(self, task_id: str) -> bool:
        """取消任务"""
        task = self._tasks.get(task_id)
        if not task:
            return False
        
        if task.status == TaskStatus.PENDING:
            task.status = TaskStatus.CANCELLED
            self._finalize(task)
            return True
        
        if task.status == TaskStatus.RUNNING and task_id in self._running:
            self._running[task_id].cancel()
            task.status = TaskStatus.CANCELLED
            return True
        
        return False
    
    async def _execute_task(self, task: ResearchTask, worker_func):
        """执行单个任务（信号量槽位由 start_worker 预留）。"""
        try:
            if self._shutdown or task.status != TaskStatus.PENDING:
                return
            
            task.status = TaskStatus.RUNNING
            task.started_at = time.time()
            
            try:
                # 设置超时
                result = await asyncio.wait_for(
                    worker_func(task),
                    timeout=task.timeout
                )
                task.result = result
                task.status = TaskStatus.COMPLETED
            except asyncio.TimeoutError:
                task.status = TaskStatus.TIMEOUT
                task.error = "Task execution timeout"
            except asyncio.CancelledError:
                task.status = TaskStatus.CANCELLED
                task.error = "Task was cancelled"
            except Exception as e:
                task.status = TaskStatus.FAILED
                task.error = str(e)
        finally:
            self._running.pop(task.task_id, None)
            self._finalize(task)
            self._semaphore.release()
    
    async def start_worker(self, worker_func):
        """启动工作线程"""
        self._started = True
        while not self._shutdown:
            try:
                # 先占住执行槽位，再从优先队列取任务；避免低优先级任务
                # 提前出队后与后到的紧急任务发生优先级反转。
                await self._semaphore.acquire()
                try:
                    _, _, task_id = await self._queue.get()
                except BaseException:
                    self._semaphore.release()
                    raise
                task = self._tasks.get(task_id)
                
                if not task or task.status != TaskStatus.PENDING:
                    self._semaphore.release()
                    continue
                
                exec_task = asyncio.create_task(
                    self._execute_task(task, worker_func)
                )
                self._running[task_id] = exec_task

                def _on_done(done, tid=task_id, t=task):
                    # 协程若在启动前被取消，finally 不会执行；此处兜底清理
                    if tid in self._running:
                        self._running.pop(tid, None)
                        self._finalize(t)
                        self._semaphore.release()

                exec_task.add_done_callback(_on_done)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Worker Error] {e}")
                await asyncio.sleep(1)
    
    def on_task_complete(self, callback: Callable[[ResearchTask], None]):
        """注册任务完成回调"""
        self._callbacks.append(callback)
    
    async def shutdown(self):
        """优雅关闭"""
        self._shutdown = True
        # 排队中的任务不会再被调度，统一置为取消并收尾
        for task in list(self._tasks.values()):
            if task.status == TaskStatus.PENDING:
                task.status = TaskStatus.CANCELLED
                self._finalize(task)
        # 取消所有运行中的任务
        for task in self._running.values():
            task.cancel()
        # 等待所有任务完成
        if self._running:
            await asyncio.gather(*self._running.values(), return_exceptions=True)


# 全局队列实例
queue = TaskQueue()
