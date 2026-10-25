import logging
import sqlite3
from pathlib import Path

from app.clock import utc_now_iso


def setup_logging(log_dir: str) -> None:
    path = Path(log_dir)
    path.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("studio")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logfile = path / "app.log"
    resolved = str(logfile.resolve())
    if not any(
        isinstance(handler, logging.FileHandler) and handler.baseFilename == resolved
        for handler in logger.handlers
    ):
        file_handler = logging.FileHandler(logfile, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(file_handler)
    if not any(type(handler) is logging.StreamHandler for handler in logger.handlers):
        logger.addHandler(logging.StreamHandler())


def log_event(conn: sqlite3.Connection | None, level: str, message: str) -> None:
    logger = logging.getLogger("studio")
    logger.log(getattr(logging, level.upper(), logging.INFO), message)
    if conn is None:
        return
    try:
        conn.execute(
            "INSERT INTO app_logs (created_at, level, message) VALUES (?, ?, ?)",
            (utc_now_iso(), level.upper(), message[:2000]),
        )
    except sqlite3.Error:
        logger.exception("could not write log row")
