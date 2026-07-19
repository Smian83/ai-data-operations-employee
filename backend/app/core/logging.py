"""
Production logging configuration.

Configures the root logger and uvicorn loggers to emit structured JSON logs
(suitable for log aggregation systems) or human-readable console output for
local development, based on LOG_FORMAT.
"""
import logging
import logging.config
import sys

from app.core.config import get_settings


def build_logging_config() -> dict:
    settings = get_settings()

    if settings.log_format == "json":
        formatter = {
            "()": "pythonjsonlogger.jsonlogger.JsonFormatter",
            "format": (
                "%(asctime)s %(levelname)s %(name)s %(message)s "
                "%(filename)s %(lineno)d"
            ),
        }
    else:
        formatter = {
            "format": "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        }

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"default": formatter},
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "default",
                "stream": sys.stdout,
            }
        },
        "root": {
            "level": settings.log_level.upper(),
            "handlers": ["console"],
        },
        "loggers": {
            "uvicorn": {"level": settings.log_level.upper(), "handlers": ["console"], "propagate": False},
            "uvicorn.error": {"level": settings.log_level.upper(), "handlers": ["console"], "propagate": False},
            "uvicorn.access": {"level": settings.log_level.upper(), "handlers": ["console"], "propagate": False},
            "sqlalchemy.engine": {"level": "WARNING", "handlers": ["console"], "propagate": False},
        },
    }


def configure_logging() -> None:
    """Apply the logging configuration. Call once at application startup."""
    logging.config.dictConfig(build_logging_config())
