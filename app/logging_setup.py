import logging
import logging.handlers
from pathlib import Path

from app.config import settings


def configure_logging() -> None:
    root = logging.getLogger()
    if root.handlers:
        return  # already configured

    root.setLevel(settings.log_level)
    formatter = logging.Formatter(
        "%(asctime)s - %(levelname)s - %(name)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    log_dir = Path("./data/logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "app.log", maxBytes=5_000_000, backupCount=3
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
