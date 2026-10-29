from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from app.core.redact import redact


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        record.msg = redact(msg)
        record.args = ()
        return True


def configure_logging(logs_dir: Path, level: int = logging.INFO) -> Path:
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / "nexus.log"
    root = logging.getLogger()
    if any(getattr(h, "_nexus", False) for h in root.handlers):
        return log_file
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    fh = logging.handlers.RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    fh.addFilter(RedactingFilter())
    fh._nexus = True  # type: ignore[attr-defined]
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    sh.addFilter(RedactingFilter())
    sh._nexus = True  # type: ignore[attr-defined]
    root.addHandler(fh)
    if sys.stderr is not None:
        root.addHandler(sh)
    root.setLevel(level)
    return log_file
