"""
统一错误处理框架 

提供：
- AppError 异常基类 + 子类（ConfigError/LLMError/SearchError/ValidationError/ExternalAPIError）
- ErrorCode 错误码枚举
- ErrorResponse 统一错误响应模型
- Web 适配器（FastAPI + Flask）
- ToolResult 统一内部返回值
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional
import traceback
import logging

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 错误码枚举
# ═══════════════════════════════════════════════════════════════

class ErrorCode(Enum):
    """统一错误码 — 格式: 模块_类别_序号"""
    # 通用 (1xxx)
    E0001_UNKNOWN = "E0001_UNKNOWN"
    E0002_INTERNAL = "E0002_INTERNAL"
    E0003_TIMEOUT = "E0003_TIMEOUT"
    E0004_RATE_LIMITED = "E0004_RATE_LIMITED"

    # 配置 (1xxx)
    E1001_MISSING_API_KEY = "E1001_MISSING_API_KEY"
    E1002_INVALID_CONFIG = "E1002_INVALID_CONFIG"
    E1003_ENV_MISSING = "E1003_ENV_MISSING"

    # LLM (2xxx)
    E2001_LLM_UNAVAILABLE = "E2001_LLM_UNAVAILABLE"
    E2002_LLM_TIMEOUT = "E2002_LLM_TIMEOUT"
    E2003_LLM_PARSE_ERROR = "E2003_LLM_PARSE_ERROR"
    E2004_LLM_RATE_LIMIT = "E2004_LLM_RATE_LIMIT"
    E2005_LLM_HALLUCINATION = "E2005_LLM_HALLUCINATION"

    # 搜索 (3xxx)
    E3001_SEARCH_ENGINE = "E3001_SEARCH_ENGINE"
    E3002_WEB_SCRAPE = "E3002_WEB_SCRAPE"
    E3003_NO_RESULTS = "E3003_NO_RESULTS"

    # 验证 (4xxx)
    E4001_MISSING_FIELD = "E4001_MISSING_FIELD"
    E4002_INVALID_FORMAT = "E4002_INVALID_FORMAT"
    E4003_INVALID_MODE = "E4003_INVALID_MODE"

    # 外部服务 (5xxx)
    E5001_DATABASE = "E5001_DATABASE"
    E5002_THIRD_PARTY = "E5002_THIRD_PARTY"

    # 权限 (6xxx)
    E6001_LICENSE = "E6001_LICENSE"
    E6002_ROLE_ACCESS = "E6002_ROLE_ACCESS"
    E6003_IP_BLOCKED = "E6003_IP_BLOCKED"


# ═══════════════════════════════════════════════════════════════
# 异常基类
# ═══════════════════════════════════════════════════════════════

class AppError(Exception):
    """统一异常基类 — 所有业务异常继承此类"""

    def __init__(self, code: ErrorCode, message: str = "",
                 detail: str = "", context: Dict[str, Any] = None,
                 cause: Optional[Exception] = None):
        self.code = code
        self.message = message or code.value
        self.detail = detail
        self.context = context or {}
        self.cause = cause
        super().__init__(self.message)

    def to_response(self, tool: str = "", step: str = "",
                    request_id: str = "") -> 'ErrorResponse':
        """转为统一错误响应"""
        return ErrorResponse(
            error_code=self.code.value,
            message=self.message,
            detail=self.detail or traceback.format_exc(),
            tool=tool,
            step=step,
            request_id=request_id,
            context=self.context,
        )


class ConfigError(AppError):
    """配置错误"""
    def __init__(self, message: str = "", code: ErrorCode = None,
                 **kwargs):
        super().__init__(code=code or ErrorCode.E1002_INVALID_CONFIG,
                         message=message, **kwargs)


class MissingAPIKeyError(ConfigError):
    """API Key 缺失"""
    def __init__(self, service: str = "", **kwargs):
        msg = f"缺少 API Key: {service}" if service else "缺少 API Key"
        super().__init__(message=msg, code=ErrorCode.E1001_MISSING_API_KEY,
                         **kwargs)


class LLMError(AppError):
    """LLM 调用错误"""
    def __init__(self, message: str = "", code: ErrorCode = None,
                 **kwargs):
        super().__init__(code=code or ErrorCode.E2001_LLM_UNAVAILABLE,
                         message=message, **kwargs)


class LLMTimeoutError(LLMError):
    def __init__(self, **kwargs):
        super().__init__(message="LLM 调用超时",
                         code=ErrorCode.E2002_LLM_TIMEOUT, **kwargs)


class LLMParseError(LLMError):
    def __init__(self, raw: str = "", **kwargs):
        super().__init__(message="LLM 响应解析失败",
                         code=ErrorCode.E2003_LLM_PARSE_ERROR,
                         detail=raw[:500] if raw else "", **kwargs)


class SearchError(AppError):
    """搜索错误"""
    def __init__(self, message: str = "", code: ErrorCode = None,
                 **kwargs):
        super().__init__(code=code or ErrorCode.E3001_SEARCH_ENGINE,
                         message=message, **kwargs)


class ValidationError(AppError):
    """输入验证错误"""
    def __init__(self, message: str = "", code: ErrorCode = None,
                 **kwargs):
        super().__init__(code=code or ErrorCode.E4002_INVALID_FORMAT,
                         message=message, **kwargs)


class MissingFieldError(ValidationError):
    def __init__(self, field: str = "", **kwargs):
        msg = f"必填字段缺失: {field}" if field else "必填字段缺失"
        super().__init__(message=msg, code=ErrorCode.E4001_MISSING_FIELD,
                         **kwargs)


class ExternalAPIError(AppError):
    """外部服务错误"""
    def __init__(self, message: str = "", code: ErrorCode = None,
                 **kwargs):
        super().__init__(code=code or ErrorCode.E5002_THIRD_PARTY,
                         message=message, **kwargs)


# ═══════════════════════════════════════════════════════════════
# 统一错误响应模型
# ═══════════════════════════════════════════════════════════════

@dataclass
class ErrorResponse:
    """统一错误响应 — 所有模块、所有框架共用"""
    success: bool = False
    error_code: str = ""
    message: str = ""
    detail: str = ""
    tool: str = ""
    step: str = ""
    request_id: str = ""
    timestamp: str = ""
    context: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        import datetime
        d = {
            "success": self.success,
            "error_code": self.error_code,
            "message": self.message,
            "tool": self.tool,
            "step": self.step,
            "request_id": self.request_id,
            "timestamp": self.timestamp or datetime.datetime.now().isoformat(),
        }
        if self.detail:
            d["detail"] = self.detail
        if self.context:
            d["context"] = self.context
        return d

    def to_json(self) -> str:
        import json
        return json.dumps(self.to_dict(), ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════
# 统一内部返回值
# ═══════════════════════════════════════════════════════════════

@dataclass
class ToolResult:
    """工具执行统一返回 — 内部模块间调用使用"""
    success: bool
    data: Any = None
    error: Optional[ErrorResponse] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(cls, data: Any = None, **metadata) -> 'ToolResult':
        return cls(success=True, data=data, metadata=metadata)

    @classmethod
    def fail(cls, error: AppError, tool: str = "", step: str = "",
             **metadata) -> 'ToolResult':
        return cls(success=False,
                   error=error.to_response(tool=tool, step=step),
                   metadata=metadata)

    def to_dict(self, format: str = "new") -> Dict[str, Any]:
        """向后兼容的格式转换"""
        if format == "new" or not self.error:
            return {
                "success": self.success,
                "data": self.data,
                "error": self.error.to_dict() if self.error else None,
                "metadata": self.metadata,
            }
        elif format == "legacy_error":
            return {"error": self.error.message,
                    "step": self.error.step} if self.error else {"data": self.data}
        elif format == "legacy_status":
            return {"status": "error" if not self.success else "ok",
                    "message": self.error.message if self.error else "",
                    "data": self.data}
        elif format == "legacy_ok":
            return {"ok": self.success,
                    "error": self.error.message if self.error else "",
                    "data": self.data}
        return self.to_dict("new")


# ═══════════════════════════════════════════════════════════════
# HTTP 状态码映射
# ═══════════════════════════════════════════════════════════════

_ERROR_HTTP_MAP = {
    ErrorCode.E4001_MISSING_FIELD: 400,
    ErrorCode.E4002_INVALID_FORMAT: 400,
    ErrorCode.E4003_INVALID_MODE: 400,
    ErrorCode.E6001_LICENSE: 401,
    ErrorCode.E6002_ROLE_ACCESS: 403,
    ErrorCode.E6003_IP_BLOCKED: 403,
    ErrorCode.E3001_SEARCH_ENGINE: 404,
    ErrorCode.E3003_NO_RESULTS: 404,
    ErrorCode.E2001_LLM_UNAVAILABLE: 502,
    ErrorCode.E2002_LLM_TIMEOUT: 504,
    ErrorCode.E2004_LLM_RATE_LIMIT: 429,
    ErrorCode.E5001_DATABASE: 500,
    ErrorCode.E5002_THIRD_PARTY: 502,
}


def http_status_for(error: AppError) -> int:
    """获取 AppError 对应的 HTTP 状态码"""
    return _ERROR_HTTP_MAP.get(error.code, 500)


# ═══════════════════════════════════════════════════════════════
# Web 适配器
# ═══════════════════════════════════════════════════════════════

def setup_fastapi(app):
    """为 FastAPI 应用注册 AppError 的全局异常处理"""
    try:
        from fastapi import Request
        from fastapi.responses import JSONResponse

        @app.exception_handler(AppError)
        async def app_error_handler(request: Request, exc: AppError):
            status = http_status_for(exc)
            resp = exc.to_response(
                tool=exc.context.get("tool", ""),
                step=exc.context.get("step", ""),
                request_id=getattr(request.state, "request_id", ""),
            )
            logger.error(
                f"[AppError] {resp.error_code} | {resp.message} | "
                f"tool={resp.tool} step={resp.step}",
                exc_info=True,
            )
            return JSONResponse(status_code=status, content=resp.to_dict())

        @app.exception_handler(Exception)
        async def generic_error_handler(request: Request, exc: Exception):
            logger.error(f"[Unhandled] {type(exc).__name__}: {exc}",
                         exc_info=True)
            return JSONResponse(
                status_code=500,
                content=ErrorResponse(
                    error_code=ErrorCode.E0002_INTERNAL.value,
                    message="服务器内部错误",
                    request_id=getattr(request.state, "request_id", ""),
                ).to_dict(),
            )

        logger.info("[ErrorHandler] FastAPI 适配器已注册")
    except ImportError:
        logger.warning("[ErrorHandler] FastAPI 未安装，跳过适配器注册")


def setup_flask(app):
    """为 Flask 应用注册 AppError 的全局异常处理"""
    try:
        from flask import jsonify, request

        @app.errorhandler(AppError)
        def handle_app_error(exc: AppError):
            status = http_status_for(exc)
            resp = exc.to_response(
                tool=exc.context.get("tool", ""),
                step=exc.context.get("step", ""),
                request_id=getattr(request, "request_id", ""),
            )
            logger.error(
                f"[AppError] {resp.error_code} | {resp.message} | "
                f"tool={resp.tool} step={resp.step}",
                exc_info=True,
            )
            d = resp.to_dict()
            d["ok"] = False  # Flask 兼容字段
            return jsonify(d), status

        @app.errorhandler(Exception)
        def handle_generic_error(exc: Exception):
            logger.error(f"[Unhandled] {type(exc).__name__}: {exc}",
                         exc_info=True)
            return jsonify({
                "success": False,
                "error_code": ErrorCode.E0002_INTERNAL.value,
                "message": "服务器内部错误",
                "ok": False,
            }), 500

        logger.info("[ErrorHandler] Flask 适配器已注册")
    except ImportError:
        logger.warning("[ErrorHandler] Flask 未安装，跳过适配器注册")


# ═══════════════════════════════════════════════════════════════
# 便捷函数
# ═══════════════════════════════════════════════════════════════

def safe_call(func, error_code: ErrorCode = ErrorCode.E0002_INTERNAL,
              tool: str = "", step: str = "", **ctx) -> ToolResult:
    """安全调用 — 捕获异常并转为 ToolResult"""
    try:
        data = func()
        return ToolResult.ok(data=data)
    except AppError as e:
        logger.error(f"[{tool}.{step}] {e.code.value}: {e.message}")
        return ToolResult.fail(e, tool=tool, step=step)
    except Exception as e:
        err = AppError(code=error_code, message=str(e), cause=e,
                       context={"tool": tool, "step": step, **ctx})
        logger.error(f"[{tool}.{step}] {error_code.value}: {e}", exc_info=True)
        return ToolResult.fail(err, tool=tool, step=step)


print("[shared/error_handler] 已加载")
