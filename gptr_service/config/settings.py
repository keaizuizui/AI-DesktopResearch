#!/usr/bin/env python3
"""
GPT-Researcher Service 统一配置管理（移除Tavily）
"""
import os
from typing import Dict, Any, Optional
from dataclasses import dataclass, field


@dataclass
class LLMConfig:
    """LLM 配置"""
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    provider: str = "openai"
    smart_model: str = "deepseek-v4-flash"
    fast_model: str = "deepseek-v4-flash"
    strategic_model: str = "deepseek-v4-flash"
    temperature: float = 0.7


@dataclass
class SearchConfig:
    """搜索配置（DeepSeek 官方 Search API）"""
    provider: str = "deepseek"
    deepseek_api_key: str = ""
    deepseek_endpoint: str = "https://api.deepseek.com/v1/tools/search"
    max_results_per_query: int = 10
    max_scraper_workers: int = 15


@dataclass
class OllamaConfig:
    """本地 Ollama 配置"""
    host: str = "http://localhost:11434"
    model: str = "EntropyYue/jina-embeddings-v2-base-zh"
    enabled: bool = True  # 关闭后压缩改用词面筛选，不再调用 embedding


@dataclass
class ServiceConfig:
    """服务运行配置"""
    host: str = "127.0.0.1"
    port: int = 8000
    max_concurrent_tasks: int = 3
    task_timeout: int = 600
    keepalive_interval: int = 30
    log_level: str = "INFO"
    min_report_chars: int = 200  # 报告最少字符数，低于此值视为空上下文


class ConfigManager:
    """配置管理器 - 单例模式"""
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self.llm = LLMConfig()
        self.search = SearchConfig()
        self.ollama = OllamaConfig()
        self.service = ServiceConfig()
        self._initialized = True

    def from_dict(self, cfg: Dict[str, Any]):
        llm_cfg = cfg.get("llm", {})
        self.llm.api_key = llm_cfg.get("api_key", self.llm.api_key)
        self.llm.base_url = llm_cfg.get("base_url", self.llm.base_url)
        self.llm.smart_model = llm_cfg.get("smart_model", self.llm.smart_model)
        self.llm.fast_model = llm_cfg.get("fast_model", self.llm.fast_model)
        self.llm.temperature = llm_cfg.get("temperature", self.llm.temperature)

        search_cfg = cfg.get("search", {})
        self.search.provider = search_cfg.get("provider", self.search.provider)
        self.search.deepseek_api_key = search_cfg.get("deepseek_api_key", self.search.deepseek_api_key)
        self.search.deepseek_endpoint = search_cfg.get("deepseek_endpoint", self.search.deepseek_endpoint)

        ollama_cfg = cfg.get("ollama", {})
        self.ollama.host = ollama_cfg.get("host", self.ollama.host)
        self.ollama.model = ollama_cfg.get("model", self.ollama.model)
        self.ollama.enabled = ollama_cfg.get("enabled", self.ollama.enabled)

        svc_cfg = cfg.get("service", {})
        self.service.host = svc_cfg.get("host", self.service.host)
        self.service.port = svc_cfg.get("port", self.service.port)
        self.service.max_concurrent_tasks = svc_cfg.get("max_concurrent_tasks", self.service.max_concurrent_tasks)
        self.service.task_timeout = svc_cfg.get("task_timeout", self.service.task_timeout)
        self.service.min_report_chars = svc_cfg.get("min_report_chars", self.service.min_report_chars)

    def from_env(self):
        """
        从环境变量加载配置

        注入的环境变量名:
          OPENAI_API_KEY, OPENAI_BASE_URL, SMART_LLM, FAST_LLM,
          DEEPSEEK_API_KEY, SEARCH_PROVIDER 等

        兼容 .env 直接设置的变量名:
          LLM_API_KEY, LLM_BASE_URL, SMART_LLM_MODEL, FAST_LLM_MODEL 等
        """
        # LLM 配置（环境变量优先，旧变量兜底）
        self.llm.api_key = os.getenv("OPENAI_API_KEY") or os.getenv("LLM_API_KEY", self.llm.api_key)
        self.llm.base_url = os.getenv("OPENAI_BASE_URL") or os.getenv("OPENAI_API_BASE") or os.getenv("LLM_BASE_URL", self.llm.base_url)

        # 模型名：注入格式为 "provider:model"，需提取 model 部分
        smart_llm = os.getenv("SMART_LLM") or os.getenv("SMART_LLM_MODEL", "")
        if ":" in smart_llm:
            self.llm.smart_model = smart_llm.split(":", 1)[1]
        else:
            self.llm.smart_model = smart_llm or self.llm.smart_model

        fast_llm = os.getenv("FAST_LLM") or os.getenv("FAST_LLM_MODEL", "")
        if ":" in fast_llm:
            self.llm.fast_model = fast_llm.split(":", 1)[1]
        else:
            self.llm.fast_model = fast_llm or self.llm.fast_model

        # 搜索配置：DeepSeek Search 与 LLM 共用同一把 Key
        self.search.deepseek_api_key = (
            os.getenv("DEEPSEEK_API_KEY")
            or os.getenv("LLM_API_KEY")
            or self.search.deepseek_api_key
            or self.llm.api_key
        )
        self.search.deepseek_endpoint = os.getenv("DEEPSEEK_SEARCH_ENDPOINT", self.search.deepseek_endpoint)
        self.search.provider = os.getenv("SEARCH_PROVIDER") or os.getenv("RETRIEVER", self.search.provider)

        # 服务配置
        self.service.host = os.getenv("SERVICE_HOST") or os.getenv("GPTR_HOST", self.service.host)
        self.service.port = int(os.getenv("SERVICE_PORT") or os.getenv("GPTR_PORT", self.service.port))
        self.service.max_concurrent_tasks = int(os.getenv("MAX_CONCURRENT_TASKS", self.service.max_concurrent_tasks))

        # 本地 embedding 开关与模型
        host = os.getenv("OLLAMA_BASE_URL") or os.getenv("OLLAMA_HOST", self.ollama.host)
        if host and not host.startswith(("http://", "https://")):
            # OLLAMA_HOST 常见写法是 127.0.0.1:11434，需要补协议头
            host = "http://" + host
        self.ollama.host = host
        self.ollama.model = os.getenv("OLLAMA_MODEL", self.ollama.model)
        enabled = os.getenv("OLLAMA_ENABLED")
        if enabled is not None:
            self.ollama.enabled = enabled.strip().lower() in ("1", "true", "yes", "on")
        if os.getenv("GPTR_DISABLE_EMBEDDING") == "1":
            self.ollama.enabled = False

    def to_env_dict(self) -> Dict[str, str]:
        env = {
            "OPENAI_API_KEY": self.llm.api_key,
            "OPENAI_API_BASE": self.llm.base_url,
            "OPENAI_BASE_URL": self.llm.base_url,
            "SMART_LLM": f"{self.llm.provider}:{self.llm.smart_model}",
            "FAST_LLM": f"{self.llm.provider}:{self.llm.fast_model}",
            "STRATEGIC_LLM": f"{self.llm.provider}:{self.llm.strategic_model}",
            "RETRIEVER": self.search.provider,
            "SEARCH_PROVIDER": self.search.provider,
            "DEEPSEEK_API_KEY": self.search.deepseek_api_key,
            "DEEPSEEK_SEARCH_ENDPOINT": self.search.deepseek_endpoint,
            "OLLAMA_BASE_URL": self.ollama.host,
            "OLLAMA_MODEL": self.ollama.model,
            # GPTR 需要一个 embedding provider 才能初始化 Memory；
            # 关闭本地 embedding 时仅停用压缩调用，不影响对象创建。
            "EMBEDDING": f"ollama:{self.ollama.model}",
            "GPTR_DISABLE_EMBEDDING": "0" if self.ollama.enabled else "1",
            "MAX_SEARCH_RESULTS_PER_QUERY": str(self.search.max_results_per_query),
            "MAX_SCRAPER_WORKERS": str(self.search.max_scraper_workers),
            "TEMPERATURE": os.getenv("TEMPERATURE", str(self.llm.temperature)),
            "LANGUAGE": os.getenv("LANGUAGE", "chinese"),
            "REPORT_TYPE": os.getenv("REPORT_TYPE", "research_report"),
            "REPORT_TONE": os.getenv("REPORT_TONE", "Objective"),
        }
        return env

    def apply_to_env(self):
        for key, value in self.to_env_dict().items():
            os.environ[key] = value


config = ConfigManager()
