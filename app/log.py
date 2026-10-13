"""Log em data/logs/easyjoycon.log (gira em 1 MB × 3)."""
import logging
import logging.handlers

from .config import DATA_DIR

LOG_DIR = DATA_DIR / "logs"
LOG_FILE = LOG_DIR / "easyjoycon.log"


def setup(console=False):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handlers = [logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")]
    if console:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
