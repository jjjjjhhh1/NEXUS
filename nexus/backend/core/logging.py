"""
Nexus 统一日志系统
基于 loguru，支持 JSON / 文本格式
"""
import sys
from loguru import logger

from .config import settings


def setup_logging() -> None:
    """初始化日志配置"""
    # 移除默认 handler
    logger.remove()

    # 控制台 handler
    if settings.log_format == "json":
        # JSON 格式（生产）
        logger.add(
            sys.stderr,
            format='{"time":"{time:YYYY-MM-DD HH:mm:ss.SSS}","level":"{level}","module":"{module}","message":"{message}"}',
            level=settings.log_level,
            serialize=False,
        )
    else:
        # 文本格式（开发）
        logger.add(
            sys.stderr,
            format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{module}:{function}:{line}</cyan> - <level>{message}</level>",
            level=settings.log_level,
        )


# 初始化
setup_logging()


__all__ = ["logger"]