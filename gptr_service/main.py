#!/usr/bin/env python3
"""
GPT-Researcher Service - 主入口

功能：
- FastAPI Web 服务
- 任务队列管理
- 保活机制
- 优雅关闭
"""
import os
import sys
import asyncio
import logging
from contextlib import asynccontextmanager

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 添加 third_party/gpt_researcher（完整补丁版源码带你的修改）
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'third_party', 'gpt_researcher'))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes import router
from core.queue import queue
from core.worker import worker
from core.keepalive import keepalive, system_keepalive
from config.settings import config


# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('gptr_service.log', encoding='utf-8')
    ]
)
logger = logging.getLogger("main")


async def worker_loop():
    """工作线程循环"""
    await queue.start_worker(worker.execute)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动
    logger.info("=" * 50)
    logger.info("GPT-Researcher Service 启动中...")
    logger.info("=" * 50)
    
    # 加载配置
    config.from_env()
    config.apply_to_env()
    queue.configure(config.service.max_concurrent_tasks)
    logger.info(f"[Config] LLM: {config.llm.smart_model}/{config.llm.fast_model}")
    logger.info(f"[Config] Search: {config.search.provider}")
    logger.info(f"[Config] Max Concurrent: {config.service.max_concurrent_tasks}")
    
    # 启动保活
    keepalive.port = config.service.port
    keepalive.start()
    system_keepalive.prevent_sleep()
    logger.info("[KeepAlive] 保活机制已启动")
    
    # 启动工作线程
    worker_task = asyncio.create_task(worker_loop())
    logger.info("[Worker] 工作线程已启动")
    
    logger.info(f"[Server] 服务已启动: http://{config.service.host}:{config.service.port}")
    
    try:
        yield
    finally:
        # 关闭
        logger.info("=" * 50)
        logger.info("GPT-Researcher Service 关闭中...")
        logger.info("=" * 50)
        
        # 停止保活
        keepalive.stop()
        system_keepalive.allow_sleep()
        logger.info("[KeepAlive] 保活机制已停止")
        
        # 优雅关闭队列
        worker_task.cancel()
        await asyncio.gather(worker_task, return_exceptions=True)
        await queue.shutdown()
        logger.info("[Queue] 任务队列已关闭")
        
        logger.info("[Server] 服务已关闭")


# 创建 FastAPI 应用
app = FastAPI(
    title="GPT-Researcher Service",
    description="统一 GPT-Researcher 微服务 - 支持并行调研、超时控制、故障恢复",
    version="1.0.0",
    lifespan=lifespan
)

# CORS 中间件（从 allow_origins=["*"] 收紧为环境变量配置）
_allowed_origins = os.environ.get("GPTR_CORS_ORIGINS", "http://localhost:8000,http://localhost:3000").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(router, prefix="/api/v1")


@app.get("/")
async def root():
    """根路径"""
    return {
        "service": "GPT-Researcher Service",
        "version": "1.0.0",
        "docs": "/docs",
        "health": "/api/v1/health"
    }


if __name__ == "__main__":
    import uvicorn

    # 绑定端口前先加载 .env/环境变量，避免 SERVICE_PORT 被忽略
    config.from_env()
    uvicorn.run(
        "main:app",
        host=config.service.host,
        port=config.service.port,
        log_level=config.service.log_level.lower(),
        reload=False
    )
