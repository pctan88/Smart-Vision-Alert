"""
Smart Vision Alert — Logger Setup
File-based rotating logger suitable for shared hosting (no stdout in cron).
"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_initialized = False


def setup_logger(
    name: str = "smart_vision_alert",
    log_dir: Path = None,
    level: str = "INFO",
) -> logging.Logger:
    """
    Set up and return a configured logger instance.

    - Writes to logs/app.log (rotated at 5MB, keep 3 backups)
    - Writes errors to logs/errors.log
    - Also logs to stdout for local debugging
    """
    global _initialized

    logger = logging.getLogger(name)

    if _initialized:
        return logger

    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(module)s:%(lineno)d | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # ── File handler: app.log ──
    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)

        app_handler = RotatingFileHandler(
            log_dir / "app.log",
            maxBytes=5 * 1024 * 1024,  # 5 MB
            backupCount=3,
            encoding="utf-8",
        )
        app_handler.setLevel(logging.DEBUG)
        app_handler.setFormatter(fmt)
        logger.addHandler(app_handler)

        # ── File handler: errors.log ──
        err_handler = RotatingFileHandler(
            log_dir / "errors.log",
            maxBytes=2 * 1024 * 1024,  # 2 MB
            backupCount=2,
            encoding="utf-8",
        )
        err_handler.setLevel(logging.ERROR)
        err_handler.setFormatter(fmt)
        logger.addHandler(err_handler)

    # ── Console handler (for local dev / SSH debugging) ──
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    _initialized = True
    return logger


def get_logger(name: str = "smart_vision_alert") -> logging.Logger:
    """Get an existing logger instance (must call setup_logger first)."""
    return logging.getLogger(name)
