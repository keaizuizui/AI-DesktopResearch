#!/usr/bin/env python3
"""
GPT-Researcher 工作器 - 实际执行研究任务
"""
import asyncio
import json
import warnings
import logging
from typing import Optional

from core.queue import ResearchTask, TaskStatus
from config.settings import config


class GPTRWorker:
    """
    GPT-Researcher 执行器
    封装 gpt-researcher 包的调用，处理超时、异常、进度上报
    """
    
    def __init__(self):
        self.logger = logging.getLogger("gptr_worker")
        self._patch_json()
        self._suppress_warnings()
    
    def _patch_json(self):
        """补丁修复 json.dumps 中的 developer 角色问题。

        只在序列化副本上改 role，不修改调用方字典，避免副作用外溢。
        """
        import json as _json
        _orig_json_dumps = _json.dumps
        
        def _normalized(obj):
            if isinstance(obj, dict):
                new_obj = {k: _normalized(v) for k, v in obj.items()}
                messages = new_obj.get('messages')
                if isinstance(messages, list):
                    new_obj['messages'] = [
                        {**msg, 'role': 'system'}
                        if isinstance(msg, dict) and msg.get('role') == 'developer'
                        else msg
                        for msg in messages
                    ]
                return new_obj
            if isinstance(obj, list):
                return [_normalized(item) for item in obj]
            return obj
        
        def _patched_json_dumps(obj, *args, **kwargs):
            return _orig_json_dumps(_normalized(obj), *args, **kwargs)
        
        _json.dumps = _patched_json_dumps
    
    def _suppress_warnings(self):
        """抑制 GPT-Researcher 的噪音日志；GPTR_LOG_LEVEL=INFO 可打开诊断日志。"""
        import os
        level_name = os.getenv("GPTR_LOG_LEVEL", "ERROR").upper()
        level = getattr(logging, level_name, logging.ERROR)
        logging.getLogger("gpt_researcher").setLevel(level)
        logging.getLogger("bs4").setLevel(logging.ERROR)
        try:
            from bs4 import XMLParsedAsHTMLWarning
            warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
        except ImportError:
            pass
    
    def _mock_langchain_mcp(self):
        """Mock langchain_mcp_adapters 避免导入错误"""
        from unittest.mock import MagicMock
        import sys
        if 'langchain_mcp_adapters' not in sys.modules:
            sys.modules['langchain_mcp_adapters'] = MagicMock()
            sys.modules['langchain_mcp_adapters.client'] = MagicMock()
    
    def _apply_config(self, task_config: dict):
        """应用任务级配置。

        GPTR 的 LLM/检索客户端直接读环境变量，任务级凭据必须注入环境才生效；
        /config/reload 之后仍以任务传入的凭据为准。
        """
        # 先应用全局配置
        config.apply_to_env()

        # 任务级搜索源仍通过环境变量生效（不涉及凭据）
        if task_config.get("search_provider"):
            import os
            os.environ["RETRIEVER"] = task_config["search_provider"]

        # 任务级凭据优先级最高，覆盖环境中的旧 Key
        api_key = task_config.get("llm_api_key")
        base_url = task_config.get("llm_base_url")
        if api_key:
            import os
            prev = os.environ.get("OPENAI_API_KEY", "")
            if prev and api_key != prev:
                self.logger.warning(
                    "[Credential] 检测到任务 Key 与进程当前 Key 不同；本服务按单 Key 设计，"
                    "正常使用不会出现此提示，仅在多个不同 Key 并发提交时需拆分实例"
                )
            os.environ["OPENAI_API_KEY"] = api_key
            os.environ["DEEPSEEK_API_KEY"] = api_key
        if base_url:
            import os
            os.environ["OPENAI_BASE_URL"] = base_url
            os.environ["OPENAI_API_BASE"] = base_url
    
    async def execute(self, task: ResearchTask) -> str:
        """
        执行研究任务
        
        Args:
            task: 研究任务对象
            
        Returns:
            研究报告文本
        """
        self.logger.info(f"[Task {task.task_id}] 开始执行")
        
        # Mock 依赖
        self._mock_langchain_mcp()
        
        # 应用配置
        self._apply_config(task.config)
        
        # 动态导入 GPT-Researcher
        from gpt_researcher import GPTResearcher
        
        # 创建 researcher 实例
        researcher = GPTResearcher(
            query=task.query,
            report_type=task.report_type,
            tone=task.tone,
        )
        
        # 动态注入配置
        if hasattr(researcher, 'cfg') and researcher.cfg is not None:
            cfg = researcher.cfg
            cfg.smart_llm_model = task.config.get("smart_model", config.llm.smart_model)
            cfg.fast_llm_model = task.config.get("fast_model", config.llm.fast_model)
            cfg.smart_llm_provider = "openai"
            cfg.fast_llm_provider = "openai"
            cfg.llm_provider = "openai"
            
            # API 配置
            api_key = task.config.get("llm_api_key", config.llm.api_key)
            base_url = task.config.get("llm_base_url", config.llm.base_url)
            if api_key:
                cfg.openai_api_key = api_key
            if base_url:
                cfg.openai_api_base = base_url
        
        # 更新任务状态
        task.stage = "researching"
        task.progress = 10
        
        # 执行研究
        timeout = float(task.timeout or config.service.task_timeout or 600)
        try:
            report = await asyncio.wait_for(researcher.conduct_research(), timeout=timeout)
            text = report if isinstance(report, str) else (
                json.dumps(report, ensure_ascii=False) if report else ""
            )
            min_chars = int(getattr(config.service, "min_report_chars", 200) or 200)
            if len(text.strip()) < min_chars:
                raise RuntimeError(f"GPTR 返回空上下文（{len(text.strip())} 字符），按失败处理")
            task.progress = 100
            task.stage = "completed"
            self.logger.info(f"[Task {task.task_id}] 完成（{len(text)} 字符）")
            return text
        except asyncio.TimeoutError:
            self.logger.error(f"[Task {task.task_id}] 超时（{timeout:.0f}s）")
            raise
        except Exception as e:
            self.logger.exception(f"[Task {task.task_id}] 失败: {e}")
            raise
    
    async def execute_with_progress(self, task: ResearchTask, progress_callback) -> str:
        """
        执行研究任务，带进度回调
        
        Args:
            task: 研究任务对象
            progress_callback: 进度回调函数 (progress: int, stage: str) -> None
            
        Returns:
            研究报告文本
        """
        # 由于 GPT-Researcher 本身不支持进度回调，这里模拟进度
        # 实际进度需要通过 monkey-patch 或修改 GPT-Researcher 源码实现
        
        progress_callback(5, "initializing")
        
        try:
            report = await self.execute(task)
            progress_callback(100, "completed")
            return report
        except Exception as e:
            progress_callback(0, f"error: {e}")
            raise


# 全局 worker 实例
worker = GPTRWorker()
