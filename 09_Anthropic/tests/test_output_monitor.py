import unittest
import tempfile
from pathlib import Path

from logging_config import configure_anomaly_logger, logger
from output_monitor import OutputAnomalyError, OutputMonitor


class OutputMonitorTests(unittest.TestCase):
    def test_anomaly_written_to_utf8_file(self):
        original_handlers = logger.handlers[:]
        original_propagate = logger.propagate
        logger.handlers = []
        logger.propagate = False
        try:
            # 使用项目日志目录，兼容只允许工作区写入的运行环境。
            log_dir = Path(__file__).resolve().parents[1] / "logs"
            log_dir.mkdir(exist_ok=True)
            with tempfile.TemporaryDirectory(dir=log_dir) as directory:
                path = Path(directory) / "logs" / "anomalies.log"
                configure_anomaly_logger(path)
                configure_anomaly_logger(path)
                self.assertEqual(len(logger.handlers), 1)
                monitor = OutputMonitor("deepseek-flash")
                with self.assertRaises(OutputAnomalyError):
                    monitor.check("你好<|im_end|>")
                contents = path.read_text(encoding="utf-8")
                self.assertIn("LLM_OUTPUT_ANOMALY", contents)
                self.assertIn(monitor.trace_id, contents)
                self.assertIn("你好", contents)
                for handler in logger.handlers:
                    handler.close()
        finally:
            for handler in logger.handlers:
                handler.close()
            logger.handlers = original_handlers
            logger.propagate = original_propagate

    def test_normal_text(self):
        monitor = OutputMonitor("deepseek-flash")
        for text in ("你好", "！", "有什么我可以帮你的吗？"):
            monitor.check(text)

    def test_marker_at_every_split_boundary(self):
        marker = "<|im_end|>"
        for split in range(1, len(marker)):
            with self.subTest(split=split):
                monitor = OutputMonitor("deepseek-flash")
                monitor.check("你好" + marker[:split])
                with self.assertLogs("uvicorn.error", level="ERROR") as logs:
                    with self.assertRaises(OutputAnomalyError):
                        monitor.check(marker[split:] + "帮你的吗？")
                self.assertIn("LLM_OUTPUT_ANOMALY", logs.output[0])
                self.assertIn('"character_offset": 2', logs.output[0])

    def test_marker_split_into_individual_characters(self):
        monitor = OutputMonitor("deepseek-flash")
        for text in ("帮", "<", "|", "im", "_end", "|"):
            monitor.check(text)
        with self.assertLogs("uvicorn.error", level="ERROR"):
            with self.assertRaises(OutputAnomalyError):
                monitor.check(">")
