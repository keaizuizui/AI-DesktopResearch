"""
统一 LLM 客户端 - 合并三个工具的 LLM 调用逻辑

功能：
- JSON 提取工具函数（extract_json_text / safe_json_loads）
- OpenAI SDK 客户端创建
- 错误处理和重试
- 同步/异步支持
- Mock 模式支持
"""
import os
import re
import json
import time
import logging
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, Callable, Union, List
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# 统一错误处理
try:
    from shared.error_handler import (
        LLMError, LLMTimeoutError, LLMParseError, LLMRateLimitError,
        MissingAPIKeyError, ConfigError, ErrorCode,
    )
    _ERROR_HANDLER_AVAILABLE = True
except ImportError:
    _ERROR_HANDLER_AVAILABLE = False


# ============================================================
# 独立工具函数：JSON 提取（不依赖任何类，可直接 import 使用）
# ============================================================

def extract_json_text(text: str) -> str:
    """
    从 LLM 返回的文本中提取纯净 JSON 字符串。

    处理策略（按优先级）：
    1. ```json ... ``` 代码块
    2. ``` ... ``` 代码块（无 json 标记）
    3. 最外层 {...} 或 [...] 结构

    注意：本函数只做文本清洗，不做 json.loads 解析。
    调用者可自行 json.loads() 或使用 safe_json_loads()。

    Args:
        text: LLM 返回的原始文本（可能包含 Markdown 包裹）

    Returns:
        提取出的 JSON 字符串；无法提取时返回原文
    """
    text = (text or "").strip()
    if not text:
        return ""

    # 1. ```json ... ```
    m = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    if m:
        return m.group(1).strip()

    # 2. ``` ... ```（无 json 标记）
    m = re.search(r"```\s*(.*?)\s*```", text, re.DOTALL)
    if m:
        return m.group(1).strip()

    # 3. 最外层 {...} 或 [...]
    m = re.search(r"([\{\[].*[\}\]])", text, re.DOTALL)
    if m:
        return m.group(1).strip()

    return text


def safe_json_loads(text: str, default=None):
    """
    安全解析 JSON：先清洗文本再解析，失败返回默认值。

    等价于调研工具 config.py 中的 safe_json_loads()，
    合并了三个工具中散落的 JSON 解析逻辑。

    Args:
        text: 可能包含 Markdown 包裹的 JSON 文本
        default: 解析失败时的返回值，默认 None

    Returns:
        解析后的 Python 对象（dict / list / str 等），
        解析失败时返回 default
    """
    try:
        return json.loads(extract_json_text(text))
    except (json.JSONDecodeError, TypeError, ValueError):
        return default if default is not None else {}


@dataclass
class LLMClientConfig:
    """LLM 客户端配置（用于创建 LLM 客户端实例）

    注意：不要与角色化配置混淆。
    角色化配置包含多模型角色（planner/writer/reviewer），
    而 LLMClientConfig 是创建单个客户端实例所需的参数。
    """
    api_key: str
    base_url: str = "https://api.deepseek.com/v1"
    model: str = "deepseek-v4-flash"
    temperature: float = 0.7
    max_tokens: int = 4096
    max_retries: int = 3
    timeout: float = 60.0


class LLMClient(ABC):
    """LLM 客户端抽象基类"""

    def __init__(self, config: LLMClientConfig):
        self.config = config
        self._client = None

    @abstractmethod
    def chat(self, messages: List[Dict[str, str]], **kwargs) -> str:
        """同步对话"""
        pass

    @abstractmethod
    def chat_structured(self, messages: List[Dict[str, str]], output_schema: Dict, **kwargs) -> Dict:
        """同步结构化输出"""
        pass

    def _maybe_compress_messages(self, messages: List[Dict[str, str]], max_messages: int = 30) -> List[Dict[str, str]]:
        """
        借鉴 mini-coding-agent: 上下文智能压缩
        当消息数超过阈值时，自动压缩旧消息，防止超出上下文窗口。
        """
        if len(messages) <= max_messages:
            return messages
        try:
            from shared.context_compressor import ContextCompressor
            compressor = ContextCompressor()
            return compressor.compress_messages(messages, max_messages)
        except Exception as e:
            # 压缩失败时返回原始消息，不影响核心功能
            logger.debug(f"[LLM] 上下文压缩失败，使用原始消息: {e}")
            return messages

    def extract_json(self, text: str) -> Optional[Dict]:
        """从文本中提取 JSON（复用模块级 extract_json_text）"""
        try:
            return json.loads(extract_json_text(text))
        except (json.JSONDecodeError, TypeError, ValueError):
            return None

    def _retry_with_backoff(
        self,
        func: Callable,
        max_retries: int = None,
        base_delay: float = 1.0
    ) -> Any:
        """带指数退避的重试"""
        max_retries = max_retries or self.config.max_retries

        for attempt in range(max_retries):
            try:
                return func()
            except Exception as e:
                if attempt == max_retries - 1:
                    raise

                delay = base_delay * (2 ** attempt)
                logger.warning(f"[LLMClient] 重试 {attempt + 1}/{max_retries}, 等待 {delay}s: {e}")
                time.sleep(delay)

        return None


class OpenAIClient(LLMClient):
    """OpenAI 客户端实现"""

    def __init__(self, config: LLMClientConfig):
        super().__init__(config)
        self._init_client()

    def _init_client(self):
        """初始化 OpenAI 客户端"""
        try:
            from openai import OpenAI
            self._client = OpenAI(
                api_key=self.config.api_key,
                base_url=self.config.base_url
            )
        except ImportError:
            raise ImportError("需要安装 openai: pip install openai")

    def chat(self, messages: List[Dict[str, str]], **kwargs) -> str:
        """同步对话"""
        # 借鉴 mini-coding-agent: 自动压缩过长消息列表
        messages = self._maybe_compress_messages(messages)
        def _do_chat():
            response = self._client.chat.completions.create(
                model=kwargs.get("model", self.config.model),
                messages=messages,
                temperature=kwargs.get("temperature", self.config.temperature),
                timeout=kwargs.get("timeout", self.config.timeout)
            )
            return response.choices[0].message.content

        return self._retry_with_backoff(_do_chat)

    def chat_structured(self, messages: List[Dict[str, str]], output_schema: Dict, **kwargs) -> Dict:
        """同步结构化输出"""
        schema_prompt = f"\n\n请严格按照以下 JSON Schema 格式输出：\n{json.dumps(output_schema, ensure_ascii=False, indent=2)}"

        has_system = False
        for msg in messages:
            if msg.get("role") == "system":
                msg["content"] += schema_prompt
                has_system = True
                break

        if not has_system:
            messages.insert(0, {"role": "system", "content": schema_prompt})

        response = self.chat(messages, **kwargs)

        result = self.extract_json(response)
        if result is None:
            if _ERROR_HANDLER_AVAILABLE:
                raise LLMParseError(raw=response)
            raise ValueError(f"无法从响应中提取 JSON: {response[:200]}...")

        return result


class AsyncOpenAIClient(LLMClient):
    """异步 OpenAI 客户端"""

    def __init__(self, config: LLMClientConfig):
        super().__init__(config)
        self._init_client()

    def _init_client(self):
        """初始化异步 OpenAI 客户端"""
        try:
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(
                api_key=self.config.api_key,
                base_url=self.config.base_url
            )
        except ImportError:
            raise ImportError("需要安装 openai: pip install openai")

    async def chat(self, messages: List[Dict[str, str]], **kwargs) -> str:
        """异步对话"""
        # 借鉴 mini-coding-agent: 自动压缩过长消息列表
        messages = self._maybe_compress_messages(messages)
        import asyncio

        async def _do_chat():
            response = await self._client.chat.completions.create(
                model=kwargs.get("model", self.config.model),
                messages=messages,
                temperature=kwargs.get("temperature", self.config.temperature),
                timeout=kwargs.get("timeout", self.config.timeout)
            )
            return response.choices[0].message.content

        max_retries = kwargs.get("max_retries", self.config.max_retries)
        for attempt in range(max_retries):
            try:
                return await _do_chat()
            except Exception as e:
                if attempt == max_retries - 1:
                    raise
                delay = 1.0 * (2 ** attempt)
                logger.warning(f"[AsyncLLMClient] 重试 {attempt + 1}/{max_retries}, 等待 {delay}s: {e}")
                await asyncio.sleep(delay)

        return ""

    async def chat_structured(self, messages: List[Dict[str, str]], output_schema: Dict, **kwargs) -> Dict:
        """异步结构化输出"""
        schema_prompt = f"\n\n请严格按照以下 JSON Schema 格式输出：\n{json.dumps(output_schema, ensure_ascii=False, indent=2)}"

        has_system = False
        for msg in messages:
            if msg.get("role") == "system":
                msg["content"] += schema_prompt
                has_system = True
                break

        if not has_system:
            messages.insert(0, {"role": "system", "content": schema_prompt})

        response = await self.chat(messages, **kwargs)

        result = self.extract_json(response)
        if result is None:
            if _ERROR_HANDLER_AVAILABLE:
                raise LLMParseError(raw=response)
            raise ValueError(f"无法从响应中提取 JSON: {response[:200]}...")

        return result


class MockLLMClient(LLMClient):
    """Mock LLM 客户端 - 用于测试"""

    def __init__(self, config: LLMClientConfig = None):
        super().__init__(config or LLMClientConfig(api_key="mock"))

    def chat(self, messages: List[Dict[str, str]], **kwargs) -> str:
        """返回模拟响应"""
        return f"[Mock] 收到 {len(messages)} 条消息，最后一条: {messages[-1].get('content', '')[:50]}..."

    def chat_structured(self, messages: List[Dict[str, str]], output_schema: Dict, **kwargs) -> Dict:
        """返回模拟结构化数据"""
        return {"mock": True, "schema": output_schema}


class AsyncMockLLMClient(MockLLMClient):
    """异步 Mock LLM 客户端"""

    async def chat(self, messages: List[Dict[str, str]], **kwargs) -> str:
        """异步返回模拟响应"""
        import asyncio
        await asyncio.sleep(0.1)
        return super().chat(messages, **kwargs)

    async def chat_structured(self, messages: List[Dict[str, str]], output_schema: Dict, **kwargs) -> Dict:
        """异步返回模拟结构化数据"""
        import asyncio
        await asyncio.sleep(0.1)
        return super().chat_structured(messages, output_schema, **kwargs)


# ============================================================
# 工厂函数
# ============================================================

def create_llm_client(
    api_key: str = None,
    base_url: str = None,
    model: str = "deepseek-v4-flash",
    temperature: float = 0.7,
    async_mode: bool = False,
    mock: bool = False,
    resilient: bool = False,
    **kwargs
) -> LLMClient:
    """
    创建 LLM 客户端

    Args:
        api_key: API Key，None 则从环境变量读取
        base_url: API Base URL
        model: 模型名称
        temperature: 温度
        async_mode: 是否异步
        mock: 是否使用 Mock 客户端
        resilient: 是否使用 ResilientLLMClient（多Provider容灾）
        **kwargs: 传给 ResilientLLMClient 的额外参数
            - provider_configs: List[LLMClientConfig] 多Provider配置
            - health_check_interval: 健康检查间隔（秒）
            - health_check_timeout: 健康检查超时（秒）
            - switch_back_primary: 主Provider恢复后是否自动切回
            - checkpoint_enabled: 是否启用断点续传
            - checkpoint_dir: 断点续传目录

    Returns:
        LLMClient 实例
    """
    if mock:
        return AsyncMockLLMClient() if async_mode else MockLLMClient()

    # 多Provider容灾模式
    if resilient:
        provider_configs = kwargs.get("provider_configs")
        if not provider_configs:
            # 降级：使用当前参数创建一个单Provider的ResilientLLMClient
            config = LLMClientConfig(
                api_key=api_key or os.getenv("OPENAI_API_KEY", ""),
                base_url=base_url or os.getenv("OPENAI_API_BASE", "https://api.deepseek.com/v1"),
                model=model,
                temperature=temperature,
            )
            provider_configs = [config]

        return ResilientLLMClient(
            provider_configs=provider_configs,
            health_check_interval=kwargs.get("health_check_interval", 30),
            health_check_timeout=kwargs.get("health_check_timeout", 5.0),
            switch_back_primary=kwargs.get("switch_back_primary", True),
            checkpoint_enabled=kwargs.get("checkpoint_enabled", True),
            checkpoint_dir=kwargs.get("checkpoint_dir", ".llm_checkpoints"),
        )

    config = LLMClientConfig(
        api_key=api_key or os.getenv("OPENAI_API_KEY", ""),
        base_url=base_url or os.getenv("OPENAI_API_BASE", "https://api.deepseek.com/v1"),
        model=model,
        temperature=temperature
    )

    # 使用 OpenAI SDK 直接调用
    if async_mode:
        return AsyncOpenAIClient(config)
    else:
        return OpenAIClient(config)


# ============================================================
# 角色化 LLM 客户端（借鉴调研工具 config.py LLMClient）
# ============================================================

class RoleBasedLLMClient(LLMClient):
    """
    支持角色隔离的 LLM 客户端。
    
    从统一配置读取各角色的模型配置，
    而非每个模块自己维护一份配置。
    
    使用方式：
        client = RoleBasedLLMClient.from_config()
        result = client.plan(messages)  # 使用 planner 角色配置
        result = client.fast(messages)  # 使用 writer 角色配置
    """
    
    ROLE_PROMPTS = {
        "planner": "你是一位资深研究规划专家...",
        "clarifier": "你是一位需求澄清专家...",
        "writer": "你是一位专业写手...",
        "factcheck": "你是一位事实核查专家...",
        "editor": "你是一位资深编辑...",
    }
    
    def __init__(self, config: LLMClientConfig, role_prompts: Dict[str, str] = None):
        super().__init__(config)
        self.role_prompts = role_prompts or self.ROLE_PROMPTS.copy()
        from shared.prompt_cache import CachedPromptBuilder
        self._prompt_builder = CachedPromptBuilder(max_cache_size=8)
    
    @classmethod
    def from_config(cls, config_path: str = None, role_prompts: Dict[str, str] = None):
        """从本地配置创建角色化客户端"""
        client_cfg = LLMClientConfig(
            api_key=os.getenv("OPENAI_API_KEY", ""),
            base_url=os.getenv("OPENAI_API_BASE", "https://api.deepseek.com/v1"),
            model=os.getenv("MODEL", "deepseek-v4-flash"),
            temperature=float(os.getenv("TEMPERATURE", "0.7")),
        )
        return cls(client_cfg, role_prompts)
    
    def _call_with_role(self, messages, role: str, model: str = None, temperature: float = None, **kwargs):
        """使用指定角色调用 LLM"""
        system = self.role_prompts.get(role, "")
        # 使用 prompt 缓存
        self._prompt_builder.build(system, {"user": str(messages)})
        
        # 构建 messages（插入 system prompt）
        msgs = list(messages)
        if system:
            if msgs and msgs[0].get("role") == "system":
                msgs[0]["content"] = system
            else:
                msgs.insert(0, {"role": "system", "content": system})
        
        return self.chat(msgs, model=model, temperature=temperature, **kwargs)
    
    def plan(self, messages, **kwargs):
        """规划角色"""
        return self._call_with_role(messages, "planner", **kwargs)
    
    def clarify(self, messages, **kwargs):
        """澄清角色"""
        return self._call_with_role(messages, "clarifier", **kwargs)
    
    def fast(self, messages, **kwargs):
        """快速写作角色"""
        return self._call_with_role(messages, "writer", **kwargs)
    
    def factcheck(self, messages, **kwargs):
        """事实核查角色"""
        return self._call_with_role(messages, "factcheck", **kwargs)
    
    def editor(self, messages, **kwargs):
        """编辑角色"""
        return self._call_with_role(messages, "editor", **kwargs)
    
    def set_role_prompt(self, role: str, prompt: str):
        """动态设置角色 prompt"""
        self.role_prompts[role] = prompt
        # 清除缓存（因为静态前缀变化了）
        self._prompt_builder.clear()


# ============================================================
# ResilientLLMClient — 多Provider容灾客户端
# ============================================================

import hashlib
import threading
from pathlib import Path
from datetime import datetime


class HealthChecker:
    """
    Provider 健康检查器

    通过发送最小请求（空消息列表）探测 Provider 是否可用。
    使用短超时避免阻塞，返回布尔状态。
    """

    def __init__(self, timeout: float = 5.0):
        self._timeout = timeout

    def check(self, client: LLMClient) -> bool:
        """
        探测 Provider 是否健康。

        发送一个简单的 ping 消息，设置短超时。
        如果响应正常（即使内容为空），认为健康。
        """
        try:
            ping_messages = [
                {"role": "user", "content": "ping"}
            ]
            result = client.chat(
                ping_messages,
                timeout=self._timeout,
                max_tokens=10,
                temperature=0.0
            )
            return True
        except Exception:
            return False


class ProviderManager:
    """
    Provider 管理器

    管理多个 Provider 实例，支持：
    - 主Provider + 多个备Provider
    - 故障切换（failover）
    - 手动切换
    - 状态查询
    """

    def __init__(self, providers: List[LLMClient], primary_index: int = 0):
        """
        Args:
            providers: LLMClient 实例列表
            primary_index: 主Provider在列表中的索引，默认第一个
        """
        if not providers:
            raise ValueError("至少需要一个Provider")
        self._providers = list(providers)
        self._primary_index = primary_index
        self._active_index = primary_index
        self._lock = threading.Lock()

    @property
    def active(self) -> LLMClient:
        """获取当前活跃的 Provider"""
        with self._lock:
            return self._providers[self._active_index]

    @property
    def active_index(self) -> int:
        """获取当前活跃Provider的索引"""
        with self._lock:
            return self._active_index

    @property
    def active_name(self) -> str:
        """获取当前活跃Provider的名称"""
        with self._lock:
            idx = self._active_index
            cfg = self._providers[idx].config
            if hasattr(cfg, 'model'):
                return f"{cfg.model}"
            return f"provider_{idx}"

    @property
    def is_on_primary(self) -> bool:
        """当前是否在主Provider上"""
        with self._lock:
            return self._active_index == self._primary_index

    @property
    def provider_count(self) -> int:
        """Provider总数"""
        return len(self._providers)

    def switch_to(self, index: int) -> bool:
        """
        切换到指定Provider。

        Args:
            index: 目标Provider索引

        Returns:
            是否切换成功
        """
        with self._lock:
            if 0 <= index < len(self._providers):
                old = self._active_index
                self._active_index = index
                logger.info(
                    f"[ProviderManager] 切换Provider: {old} -> {index}"
                )
                return True
            return False

    def switch_to_primary(self) -> bool:
        """切回主Provider"""
        return self.switch_to(self._primary_index)

    def switch_to_next(self) -> bool:
        """
        切换到下一个可用Provider（循环）。

        Returns:
            是否切换成功
        """
        with self._lock:
            for i in range(len(self._providers)):
                next_idx = (self._active_index + i + 1) % len(self._providers)
                if next_idx != self._active_index:
                    self._active_index = next_idx
                    logger.info(
                        f"[ProviderManager] 故障切换: -> {next_idx}"
                    )
                    return True
            return False

    def get_all_providers(self) -> List[LLMClient]:
        """获取所有Provider实例"""
        return list(self._providers)


class CheckpointManager:
    """
    LLM 调用断点续传管理器

    保存每次调用的 (messages_hash, response) 映射，
    在调用失败后可从断点恢复，避免重复调用。
    """

    def __init__(self, checkpoint_dir: str = ".llm_checkpoints", enabled: bool = True):
        self._enabled = enabled
        self._checkpoint_dir = Path(checkpoint_dir)
        self._cache: Dict[str, str] = {}
        if self._enabled:
            self._checkpoint_dir.mkdir(parents=True, exist_ok=True)
            self._load_cache()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _compute_hash(self, messages: List[Dict[str, str]]) -> str:
        """计算消息列表的哈希值"""
        content = json.dumps(messages, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _get_file_path(self, msg_hash: str) -> Path:
        """获取 checkpoint 文件路径"""
        return self._checkpoint_dir / f"{msg_hash}.json"

    def _load_cache(self):
        """从磁盘加载已有checkpoint到内存缓存"""
        if not self._checkpoint_dir.exists():
            return
        for f in self._checkpoint_dir.glob("*.json"):
            try:
                with open(f, "r", encoding="utf-8") as fp:
                    data = json.load(fp)
                msg_hash = f.stem
                self._cache[msg_hash] = data.get("response", "")
            except Exception:
                logger.debug(f"[CheckpointManager] 跳过损坏文件: {f.name}")

    def get(self, messages: List[Dict[str, str]]) -> Optional[str]:
        """
        获取已缓存的调用结果。

        Args:
            messages: 消息列表

        Returns:
            缓存的响应文本，未命中返回 None
        """
        if not self._enabled:
            return None
        msg_hash = self._compute_hash(messages)
        return self._cache.get(msg_hash)

    def save(self, messages: List[Dict[str, str]], response: str):
        """
        保存调用结果到checkpoint。

        Args:
            messages: 消息列表
            response: LLM 响应文本
        """
        if not self._enabled:
            return
        msg_hash = self._compute_hash(messages)
        self._cache[msg_hash] = response

        file_path = self._get_file_path(msg_hash)
        try:
            tmp_path = file_path.with_suffix(".tmp")
            data = {
                "hash": msg_hash,
                "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "response": response,
            }
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, file_path)
        except Exception as e:
            logger.debug(f"[CheckpointManager] 保存失败: {e}")

    def clear(self):
        """清除所有checkpoint"""
        self._cache.clear()
        if self._checkpoint_dir.exists():
            import shutil
            shutil.rmtree(self._checkpoint_dir, ignore_errors=True)
            self._checkpoint_dir.mkdir(parents=True, exist_ok=True)


class ResilientLLMClient(LLMClient):
    """
    弹性 LLM 客户端 — 多Provider容灾

    功能：
    - 支持多Provider配置（主+备）
    - 调用前健康探测
    - 失败自动切换（failover）
    - 断点续传（保存调用状态，避免重复调用）
    - 后台轮询主Provider，恢复后自动切回

    使用方式：
        configs = [
            LLMClientConfig(api_key="sk-xxx", base_url="https://api.deepseek.com/v1",
                            model="deepseek-v4-flash"),
            LLMClientConfig(api_key="sk-yyy", base_url="https://api.openai.com/v1",
                            model="gpt-4o"),
        ]
        client = ResilientLLMClient(
            provider_configs=configs,
            health_check_interval=30,
            switch_back_primary=True,
        )
        response = client.chat(messages)
    """

    def __init__(
        self,
        provider_configs: List[LLMClientConfig],
        health_check_interval: int = 30,
        health_check_timeout: float = 5.0,
        switch_back_primary: bool = True,
        checkpoint_enabled: bool = True,
        checkpoint_dir: str = ".llm_checkpoints",
    ):
        """
        Args:
            provider_configs: Provider配置列表，第一个为主Provider
            health_check_interval: 健康检查间隔（秒），0表示不启用后台轮询
            health_check_timeout: 健康检查超时（秒）
            switch_back_primary: 主Provider恢复后是否自动切回
            checkpoint_enabled: 是否启用断点续传
            checkpoint_dir: 断点续传文件目录
        """
        if not provider_configs:
            raise ValueError("至少需要一个Provider配置")

        # 使用第一个Provider的配置作为基类配置
        super().__init__(provider_configs[0])

        # 创建所有Provider实例
        self._provider_clients: List[LLMClient] = []
        for cfg in provider_configs:
            client = OpenAIClient(cfg)
            self._provider_clients.append(client)

        # 初始化子组件
        self._provider_manager = ProviderManager(
            self._provider_clients, primary_index=0
        )
        self._health_checker = HealthChecker(timeout=health_check_timeout)
        self._checkpoint_manager = CheckpointManager(
            checkpoint_dir=checkpoint_dir, enabled=checkpoint_enabled
        )

        # 配置
        self._switch_back_primary = switch_back_primary
        self._health_check_interval = health_check_interval
        self._health_check_timeout = health_check_timeout

        # 后台轮询线程
        self._bg_thread: Optional[threading.Thread] = None
        self._bg_stop_event = threading.Event()

        # 启动后台轮询
        if health_check_interval > 0 and switch_back_primary:
            self._start_background_polling()

    # ==================== 核心接口 ====================

    def chat(self, messages: List[Dict[str, str]], **kwargs) -> str:
        """同步对话（带容灾和断点续传）"""
        # 1. 检查断点缓存
        if self._checkpoint_manager.enabled:
            cached = self._checkpoint_manager.get(messages)
            if cached is not None:
                logger.info("[ResilientLLM] 命中checkpoint缓存，跳过调用")
                return cached

        # 2. 遍历Provider尝试调用
        last_error = None
        for attempt in range(self._provider_manager.provider_count):
            client = self._provider_manager.active

            # 调用前健康探测
            if not self._health_checker.check(client):
                logger.warning(
                    f"[ResilientLLM] Provider {self._provider_manager.active_name} "
                    f"健康检查失败，切换"
                )
                if not self._provider_manager.switch_to_next():
                    break
                continue

            try:
                response = client.chat(messages, **kwargs)

                # 保存checkpoint
                self._checkpoint_manager.save(messages, response)

                logger.info(
                    f"[ResilientLLM] 调用成功 via "
                    f"{self._provider_manager.active_name}"
                )
                return response

            except Exception as e:
                last_error = e
                logger.warning(
                    f"[ResilientLLM] Provider {self._provider_manager.active_name} "
                    f"调用失败: {e}"
                )
                if not self._provider_manager.switch_to_next():
                    break

        # 所有Provider都失败
        raise RuntimeError(
            f"[ResilientLLM] 所有Provider调用失败，共"
            f"{self._provider_manager.provider_count}个。"
            f"最后错误: {last_error}"
        )

    def chat_structured(
        self, messages: List[Dict[str, str]], output_schema: Dict, **kwargs
    ) -> Dict:
        """同步结构化输出（带容灾）"""
        # 1. 检查断点缓存
        if self._checkpoint_manager.enabled:
            cached = self._checkpoint_manager.get(messages)
            if cached is not None:
                try:
                    return json.loads(extract_json_text(cached))
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass  # 缓存损坏，重新调用

        # 2. 遍历Provider尝试调用
        last_error = None
        for attempt in range(self._provider_manager.provider_count):
            client = self._provider_manager.active

            if not self._health_checker.check(client):
                logger.warning(
                    f"[ResilientLLM] Provider {self._provider_manager.active_name} "
                    f"健康检查失败，切换"
                )
                if not self._provider_manager.switch_to_next():
                    break
                continue

            try:
                result = client.chat_structured(messages, output_schema, **kwargs)

                # 保存checkpoint
                self._checkpoint_manager.save(messages, json.dumps(result, ensure_ascii=False))

                logger.info(
                    f"[ResilientLLM] 结构化调用成功 via "
                    f"{self._provider_manager.active_name}"
                )
                return result

            except Exception as e:
                last_error = e
                logger.warning(
                    f"[ResilientLLM] Provider {self._provider_manager.active_name} "
                    f"结构化调用失败: {e}"
                )
                if not self._provider_manager.switch_to_next():
                    break

        raise RuntimeError(
            f"[ResilientLLM] 所有Provider结构化调用失败。"
            f"最后错误: {last_error}"
        )

    # ==================== 后台轮询 ====================

    def _start_background_polling(self):
        """启动后台轮询线程，定期检查主Provider健康状态"""
        if self._bg_thread is not None and self._bg_thread.is_alive():
            return

        self._bg_stop_event.clear()
        self._bg_thread = threading.Thread(
            target=self._polling_loop,
            daemon=True,
            name="ResilientLLM-Polling"
        )
        self._bg_thread.start()
        logger.info(
            f"[ResilientLLM] 后台轮询已启动，间隔={self._health_check_interval}s"
        )

    def _polling_loop(self):
        """后台轮询主循环"""
        while not self._bg_stop_event.wait(self._health_check_interval):
            # 如果已经在主Provider上，无需检查
            if self._provider_manager.is_on_primary:
                continue

            # 检查主Provider是否恢复
            primary_client = self._provider_clients[
                self._provider_manager._primary_index
            ]
            if self._health_checker.check(primary_client):
                logger.info(
                    "[ResilientLLM] 主Provider已恢复，自动切回"
                )
                self._provider_manager.switch_to_primary()

    def stop_polling(self):
        """停止后台轮询"""
        self._bg_stop_event.set()
        if self._bg_thread is not None:
            self._bg_thread.join(timeout=5.0)
            self._bg_thread = None
            logger.info("[ResilientLLM] 后台轮询已停止")

    # ==================== 状态查询 ====================

    @property
    def active_provider_name(self) -> str:
        """当前活跃Provider名称"""
        return self._provider_manager.active_name

    @property
    def is_using_primary(self) -> bool:
        """是否正在使用主Provider"""
        return self._provider_manager.is_on_primary

    def get_status(self) -> Dict[str, Any]:
        """获取客户端状态"""
        return {
            "active_provider": self._provider_manager.active_name,
            "is_primary": self._provider_manager.is_on_primary,
            "provider_count": self._provider_manager.provider_count,
            "checkpoint_enabled": self._checkpoint_manager.enabled,
            "bg_polling": self._bg_thread is not None and self._bg_thread.is_alive(),
            "health_check_interval": self._health_check_interval,
        }

    def __del__(self):
        """析构时停止后台线程"""
        self.stop_polling()


# ============================================================
# 兼容层：从 core.llm_client 迁移的组件
# ============================================================

class SyncMockLLMClient:
    """
    Mock LLM客户端（同步版本）

    用于无法使用 async/await 的旧引擎。
    接口与 MockLLMClient 一致，但 chat() 是同步方法。
    支持智能关键词匹配返回不同结构的 JSON 响应。
    """

    def __init__(self, default_response: str = None):
        self.default_response = default_response
        self.call_count = 0
        self.call_history: List[Dict] = []

    def chat(self, messages: List[Dict], temperature: float = 0.7) -> str:
        """返回Mock响应（同步版本）"""
        self.call_count += 1
        self.call_history.append({"messages": messages, "temperature": temperature})
        if self.default_response is not None:
            return self.default_response
        user_content = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                user_content = msg.get("content", "")
                break
        if "JSON" in user_content or "json" in user_content:
            return '{"result": "mock_response", "score": 75, "status": "ok"}'
        elif "评分" in user_content or "score" in user_content.lower():
            return '{"overall_score": 75, "passed": true, "issues": []}'
        elif "风险" in user_content or "risk" in user_content.lower():
            return '{"risk_score": 90, "risks": []}'
        elif "流畅" in user_content or "fluency" in user_content.lower():
            return '{"fluency_score": 80, "issues": []}'
        elif "缺口" in user_content or "gap" in user_content.lower():
            return '{"gaps": [{"description": "缺少数据支撑", "search_query": "行业数据统计", "priority": "medium"}]}'
        elif "修订" in user_content or "revise" in user_content.lower():
            return '{"revised_content": "这是Mock修订后的内容。"}'
        elif "扩写" in user_content or "draft" in user_content.lower():
            return '{"draft_content": "这是Mock生成的扩写内容。", "meta": {"word_count": 150}}'
        else:
            return '{"result": "mock_response", "message": "这是同步Mock的默认响应。"}'

    def reset(self):
        """重置调用计数和历史记录"""
        self.call_count = 0
        self.call_history = []


async def call_chat_async(llm_client, messages, temperature=0.7) -> str:
    """统一调用 llm.chat，兼容 sync 和 async 客户端

    对于尚未完全 async 化的引擎，这个方法提供过渡桥梁：
    - 如果 llm_client.chat 是 async 函数 → 返回 await 结果
    - 如果 llm_client.chat 是 sync 函数  → 直接返回结果
    """
    import inspect
    chat_fn = llm_client.chat
    if inspect.iscoroutinefunction(chat_fn):
        result = await chat_fn(messages, temperature=temperature)
    else:
        result = chat_fn(messages, temperature=temperature)
    return result


class ModelRouterLLMClient:
    """
    模型路由器适配客户端

    将统一的 chat 接口适配到 model_router.generate() 调用。
    自动将 messages 列表转换为 model_router 所需的 prompt 格式。
    """

    def __init__(self, model_router, model_type: str = "fast"):
        self.model_router = model_router
        self.model_type = model_type

    async def chat(self, messages: List[Dict], temperature: float = 0.7, model: str = None) -> str:
        """通过 model_router 发送消息"""
        prompt_parts = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                prompt_parts.append(f"[System]: {content}")
            elif role == "user":
                prompt_parts.append(f"[User]: {content}")
            elif role == "assistant":
                prompt_parts.append(f"[Assistant]: {content}")

        prompt = "\n\n".join(prompt_parts)

        return await self.model_router.generate(
            model_type=model or self.model_type,
            prompt=prompt,
            temperature=temperature,
        )
