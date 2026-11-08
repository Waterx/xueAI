"""模型层与 HTTP 层之间的内部事件协议，不包含 SSE 格式。"""

from typing import Literal

from pydantic import BaseModel, model_validator


class LLMResult(BaseModel):
    """非流式调用的统一结果；缺失用量时保留 None，不编造统计。"""

    model: str
    content: str
    finish_reason: str
    usage: dict[str, int] | None = None


class LLMEvent(BaseModel):
    type: Literal["delta", "done", "usage"]
    text: str | None = None
    finish_reason: str | None = None
    usage: dict[str, int] | None = None

    @model_validator(mode="after")
    def validate_payload(self):
        required = {"delta": "text", "done": "finish_reason", "usage": "usage"}[self.type]
        for field in ("text", "finish_reason", "usage"):
            value = getattr(self, field)
            if field == required and value is None:
                raise ValueError(f"{self.type} 事件必须提供 {field}")
            if field != required and value is not None:
                raise ValueError(f"{self.type} 事件不能提供 {field}")
        return self
