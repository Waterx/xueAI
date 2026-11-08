"""异常日志的文件路径、格式和轮转配置。"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parent / "logs" / "anomalies.log"


def configure_anomaly_logger(path: Path = LOG_PATH) -> logging.Logger:
    # 使用独立 logger，仅将输出异常写入文件，同时向 uvicorn.error 传播到终端。
    anomaly_logger = logging.getLogger("uvicorn.error.output_anomaly")
    anomaly_logger.setLevel(logging.ERROR)
    if not anomaly_logger.handlers:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path, maxBytes=2 * 1024 * 1024, backupCount=3,
            encoding="utf-8", delay=True,
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        anomaly_logger.addHandler(handler)
    return anomaly_logger


logger = configure_anomaly_logger()
