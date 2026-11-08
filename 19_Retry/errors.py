"""将网关与 Provider 异常转换为稳定的公开错误。"""

import anthropic
import httpx
import openai
from fastapi.exceptions import RequestValidationError

from output_monitor import OutputAnomalyError


PROVIDER_ERROR_STATUSES = {
    "authentication_error": 401,
    "invalid_api_key": 401,
    "permission_error": 403,
    "rate_limit_error": 429,
    "rate_limit_exceeded": 429,
    "timeout": 504,
    "timeout_error": 504,
    "api_error": 503,
    "server_error": 503,
    "overloaded_error": 503,
}


class GatewayError(Exception):
    def __init__(self, code: str, message: str, status_code: int, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable

    def payload(self, request_id: str) -> dict[str, str | bool]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "request_id": request_id,
        }


class StructuredOutputError(ValueError):
    """模型未返回完整、合法且符合约束的 JSON。"""

    usage: dict[str, int] | None = None


class UpstreamResponseError(RuntimeError):
    """Provider 返回了无法正常完成的响应。"""

    def __init__(self, message: str, provider_code: str | None = None):
        super().__init__(message)
        self.provider_code = provider_code


def provider_status(exc: openai.APIError | anthropic.APIError | UpstreamResponseError) -> int | None:
    if isinstance(exc, UpstreamResponseError):
        return PROVIDER_ERROR_STATUSES.get(exc.provider_code)
    status = getattr(exc, "status_code", None)
    if status is not None and status >= 400:
        return status

    # SDK 可把 HTTP 200 内的 SSE 错误转换成异常。
    body = exc.body
    detail = body.get("error", body) if isinstance(body, dict) else None
    if isinstance(detail, dict):
        for key in ("code", "type"):
            value = detail.get(key)
            if isinstance(value, str) and value in PROVIDER_ERROR_STATUSES:
                return PROVIDER_ERROR_STATUSES[value]
    return None


def normalize_error(exc: Exception) -> GatewayError:
    if isinstance(exc, GatewayError):
        return exc
    if isinstance(exc, RequestValidationError):
        return GatewayError("INVALID_REQUEST", "请求参数无效，请检查输入。", 422)
    if isinstance(exc, StructuredOutputError):
        return GatewayError("STRUCTURED_OUTPUT_INVALID", "模型输出不符合结构化要求。", 502)
    if isinstance(exc, (openai.APITimeoutError, anthropic.APITimeoutError, httpx.TimeoutException)):
        return GatewayError("UPSTREAM_TIMEOUT", "模型调用超时，请重试。", 504, True)
    if isinstance(exc, (openai.APIConnectionError, anthropic.APIConnectionError, httpx.TransportError)):
        return GatewayError("UPSTREAM_UNAVAILABLE", "模型服务暂时不可用，请稍后重试。", 503, True)
    if isinstance(exc, (openai.APIError, anthropic.APIError, UpstreamResponseError)):
        status = provider_status(exc)
        if status in (401, 403):
            return GatewayError("UPSTREAM_AUTH_ERROR", "模型服务鉴权失败。", 502)
        if status == 429:
            return GatewayError("UPSTREAM_RATE_LIMITED", "模型服务请求过多，请稍后重试。", 503, True)
        if status in (408, 504):
            return GatewayError("UPSTREAM_TIMEOUT", "模型调用超时，请重试。", 504, True)
        if status is not None and 500 <= status <= 599:
            return GatewayError("UPSTREAM_UNAVAILABLE", "模型服务暂时不可用，请稍后重试。", 503, True)
        return GatewayError("UPSTREAM_BAD_RESPONSE", "模型服务返回了异常响应。", 502)
    if isinstance(exc, OutputAnomalyError):
        return GatewayError("UPSTREAM_BAD_RESPONSE", "模型服务返回了异常响应。", 502)
    return GatewayError("INTERNAL_ERROR", "网关处理请求时发生内部错误。", 500)
