"""检测跨分片的异常输出，记录证据并抛出专用异常。"""

import json
from collections import deque
from uuid import uuid4

from logging_config import logger


class OutputAnomalyError(RuntimeError):
    """输出包含异常控制标记，禁止作为成功回答保存。"""


class OutputMonitor:
    def __init__(self, model: str):
        self.trace_id = uuid4().hex[:12]
        self.model = model
        self.completion_id = None
        self.tail = ""
        self.offset = 0
        self.delta_index = 0
        self.recent = deque(maxlen=16)

    def check(self, delta: str):
        self.delta_index += 1
        self.recent.append({"index": self.delta_index, "text": delta[-160:]})
        combined = self.tail + delta
        marker = "<|im_end|>"
        position = combined.find(marker)
        if position != -1:
            evidence = {
                "trace_id": self.trace_id,
                "model": self.model,
                "completion_id": self.completion_id,
                "marker": marker,
                "character_offset": self.offset - len(self.tail) + position,
                "recent_deltas": list(self.recent),
            }
            logger.error("LLM_OUTPUT_ANOMALY %s", json.dumps(evidence, ensure_ascii=False))
            raise OutputAnomalyError(f"检测到异常输出，诊断编号：{self.trace_id}")
        self.offset += len(delta)
        self.tail = combined[-80:]

