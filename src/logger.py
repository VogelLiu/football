"""
统一日志配置，使用 rich 输出彩色日志。
"""
import logging
import sys
from rich.logging import RichHandler
from src.config import settings


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logger.setLevel(level)

    handler = RichHandler(
        rich_tracebacks=True,
        markup=True,
        show_path=False,
    )
    handler.setLevel(level)
    logger.addHandler(handler)
    return logger
