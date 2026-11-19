#!/usr/bin/env python3
"""
FastAPI 路由定义
"""
import logging
import time
from typing import List, Optional
from datetime import datetime

from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field

from core.queue import queue, TaskStatus, TaskPriority, ResearchTask
from core.worker import worker
from core.keepalive import keepalive
from config.settings import config


router = APIRouter()
logger = logging.getLogger("api")
SERVICE_STARTED_AT = time.time()


# ============ 请求/响应模型 ============

class ResearchRequest(BaseModel):
    """研究任务请求"""
    query: str = Field(..., min_length=1, max_length=1000, description="研究查询")
    report_type: str = Field(default="research_report", description="报告类型")
    tone: str = Field(default="Objective", description="语气")
    config: dict = Field(default_factory=dict, description="任务级配置覆盖")
    priority: int = Field(default=2, ge=1, le=4, description="优先级 1-4")
    timeout: int = Field(default=600, ge=60, le=3600, description="超时秒数")
    callback_url: Optional[str] = Field(default=None, description="完成回调URL")


class BatchResearchRequest(BaseModel):
    """批量研究任务请求"""
    tasks: List[ResearchRequest]
    max_concurrent: int = Field(default=3, ge=1, le=10)


class ResearchResponse(BaseModel):
    """研究任务响应"""
    task_id: str
    status: str
    message: str = "任务已提交"


class TaskStatusResponse(BaseModel):
    """任务状态响应"""
    task_id: str
    status: str
    progress: int
    stage: str
    created_at: float
    started_at: Optional[float]
    completed_at: Optional[float]
    elapsed: Optional[float]
    result: Optional[str]
    error: Optional[str]


class ServiceStatusResponse(BaseModel):
    """服务状态响应"""
    status: str
    version: str = "1.0.0"
    uptime: float
    tasks: dict
    config: dict


# ============ 路由定义 ============

@router.post("/research", response_model=ResearchResponse)
async def submit_research(request: ResearchRequest, background_tasks: BackgroundTasks):
    """
    提交研究任务
    
    - **query**: 研究查询内容
    - **report_type**: 报告类型 (research_report/deep_report等)
    - **tone**: 语气 (Objective/Formal/Informal等)
    - **priority**: 优先级 1=低 2=正常 3=高 4=紧急
    - **timeout**: 超时时间（秒）
    """
    keepalive.record_request()
    
    if request.callback_url:
        raise HTTPException(status_code=400, detail="callback_url 暂不支持，请使用任务状态轮询")
    
    priority = TaskPriority(request.priority)
    task_id = await queue.submit(
        query=request.query,
        report_type=request.report_type,
        tone=request.tone,
        config=request.config,
        priority=priority,
        timeout=request.timeout,
        callback_url=request.callback_url
    )
    
    logger.info(f"[API] 提交任务 {task_id}")
    
    return ResearchResponse(
        task_id=task_id,
        status="pending",
        message="任务已提交，请通过 /research/{task_id} 查询状态"
    )


@router.post("/research/batch", response_model=List[ResearchResponse])
async def submit_batch_research(request: BatchResearchRequest):
    """批量提交研究任务"""
    keepalive.record_request()
    
    task_ids = []
    responses = []
    
    # 先校验整批，避免第一条已提交后才因后续 callback_url 返回 400
    for task_req in request.tasks:
        if task_req.callback_url:
            raise HTTPException(status_code=400, detail="callback_url 暂不支持，请使用任务状态轮询")
    
    for task_req in request.tasks:
        priority = TaskPriority(task_req.priority)
        task_id = await queue.submit(
            query=task_req.query,
            report_type=task_req.report_type,
            tone=task_req.tone,
            config=task_req.config,
            priority=priority,
            timeout=task_req.timeout,
            callback_url=task_req.callback_url
        )
        task_ids.append(task_id)
        responses.append(ResearchResponse(
            task_id=task_id,
            status="pending"
        ))
    
    logger.info(f"[API] 批量提交 {len(task_ids)} 个任务")
    return responses


@router.get("/research/{task_id}", response_model=TaskStatusResponse)
async def get_task_status(task_id: str):
    """获取任务状态和结果"""
    keepalive.record_request()
    
    task = queue.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    
    elapsed = None
    if task.started_at:
        end_time = task.completed_at or datetime.now().timestamp()
        elapsed = end_time - task.started_at
    
    return TaskStatusResponse(
        task_id=task.task_id,
        status=task.status.value,
        progress=task.progress,
        stage=task.stage,
        created_at=task.created_at,
        started_at=task.started_at,
        completed_at=task.completed_at,
        elapsed=elapsed,
        result=task.result if task.status == TaskStatus.COMPLETED else None,
        error=task.error if task.status in [TaskStatus.FAILED, TaskStatus.TIMEOUT] else None
    )


@router.delete("/research/{task_id}")
async def cancel_task(task_id: str):
    """取消任务"""
    keepalive.record_request()
    
    success = await queue.cancel(task_id)
    if not success:
        raise HTTPException(status_code=400, detail="任务不存在或无法取消")
    
    return {"task_id": task_id, "status": "cancelled"}


@router.get("/research", response_model=List[TaskStatusResponse])
async def list_tasks(status: Optional[str] = None, limit: int = 50):
    """
    列出所有任务
    
    - **status**: 按状态过滤 (pending/running/completed/failed)
    - **limit**: 返回数量限制（1-500）
    """
    keepalive.record_request()
    
    limit = max(1, min(int(limit), 500))
    tasks = queue.get_all_tasks()
    
    if status:
        tasks = [t for t in tasks if t.status.value == status]
    
    tasks = sorted(tasks, key=lambda t: t.created_at, reverse=True)[:limit]
    
    return [
        TaskStatusResponse(
            task_id=t.task_id,
            status=t.status.value,
            progress=t.progress,
            stage=t.stage,
            created_at=t.created_at,
            started_at=t.started_at,
            completed_at=t.completed_at,
            elapsed=None,
            result=None,
            error=t.error
        )
        for t in tasks
    ]


@router.get("/health", response_model=ServiceStatusResponse)
async def health_check():
    """健康检查端点"""
    uptime = time.time() - SERVICE_STARTED_AT
    
    all_tasks = queue.get_all_tasks()
    running = len([t for t in all_tasks if t.status == TaskStatus.RUNNING])
    pending = len([t for t in all_tasks if t.status == TaskStatus.PENDING])
    completed = len([t for t in all_tasks if t.status == TaskStatus.COMPLETED])
    failed = len([t for t in all_tasks if t.status == TaskStatus.FAILED])
    
    return ServiceStatusResponse(
        status="healthy",
        uptime=uptime,
        tasks={
            "total": len(all_tasks),
            "running": running,
            "pending": pending,
            "completed": completed,
            "failed": failed
        },
        config={
            "max_concurrent": config.service.max_concurrent_tasks,
            "task_timeout": config.service.task_timeout,
            "llm_smart_model": config.llm.smart_model,
            "llm_fast_model": config.llm.fast_model,
            "search_provider": config.search.provider
        }
    )


@router.post("/config/reload")
async def reload_config():
    """重新加载配置（从环境变量）"""
    keepalive.record_request()
    
    config.from_env()
    config.apply_to_env()
    queue.configure(config.service.max_concurrent_tasks)
    
    return {
        "status": "success",
        "message": "配置已重新加载",
        "config": {
            "llm_provider": config.llm.provider,
            "search_provider": config.search.provider,
            "max_concurrent": config.service.max_concurrent_tasks
        }
    }
