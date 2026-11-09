"""Daily log files in DATA_DIR/logs: jobhacker-YYYY-MM-DD.log, one per day, the
last LOG_KEEP_DAYS kept. They record what the app did (imports and what each
row became, links read, AI calls, overnight runs, errors) so a problem can be
traced afterwards. They never contain API keys, resumes or job descriptions."""
from __future__ import annotations

import datetime as dt
import logging
import os
import re
import threading
from pathlib import Path

from . import config

LOG_DIR = config.DATA_DIR / "logs"
KEEP_DAYS = int(os.environ.get("LOG_KEEP_DAYS", "30"))
NAME_RE = re.compile(r"^jobhacker-\d{4}-\d{2}-\d{2}\.log$")

log = logging.getLogger("jobhacker")


def _now() -> dt.datetime:
    """Local time in the app's time zone (TZ_NAME), so file dates and line times agree."""
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo(config.TIMEZONE))
    except Exception:
        return dt.datetime.now()


def _today() -> str:
    return _now().date().isoformat()


class _Formatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        try:
            from zoneinfo import ZoneInfo
            return dt.datetime.fromtimestamp(record.created, ZoneInfo(config.TIMEZONE)).strftime(datefmt or "%H:%M:%S")
        except Exception:
            return super().formatTime(record, datefmt)


class DailyFileHandler(logging.Handler):
    """Writes to the file for today's date, switching files at midnight."""

    def __init__(self, folder: Path):
        super().__init__()
        self.folder = folder
        self._day = ""
        self._fh = None
        self._lock2 = threading.Lock()

    def _file(self):
        day = _today()
        if day != self._day or self._fh is None:
            if self._fh:
                self._fh.close()
            self.folder.mkdir(parents=True, exist_ok=True)
            self._fh = open(self.folder / f"jobhacker-{day}.log", "a", encoding="utf-8")
            self._day = day
            prune()
        return self._fh

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            with self._lock2:
                fh = self._file()
                fh.write(msg + "\n")
                fh.flush()
        except Exception:
            self.handleError(record)


def prune() -> None:
    cutoff = (dt.date.fromisoformat(_today()) - dt.timedelta(days=KEEP_DAYS)).isoformat()
    for p in LOG_DIR.glob("jobhacker-*.log"):
        if NAME_RE.match(p.name) and p.name[10:20] < cutoff:
            p.unlink(missing_ok=True)


class _ToRoot(logging.Handler):
    """Passes records on to the root logger's handlers (or Python's stderr fallback)."""

    def emit(self, record: logging.LogRecord) -> None:
        root = logging.getLogger()
        if root.handlers:
            root.handle(record)
        elif logging.lastResort and record.levelno >= logging.lastResort.level:
            logging.lastResort.handle(record)


_set_up = False


def setup() -> None:
    global _set_up
    if _set_up:
        return
    _set_up = True
    h = DailyFileHandler(LOG_DIR)
    h.setFormatter(_Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
    log.addHandler(h)
    log.setLevel(logging.INFO)
    # Row-by-row detail stays in the daily file; only warnings and errors also
    # reach the console (docker compose logs) or the launcher's log.
    log.propagate = False
    log.addHandler(_ToRoot(logging.WARNING))
    # Server errors (tracebacks) go to the same file.
    logging.getLogger("uvicorn.error").addHandler(h)


def files() -> list[dict]:
    if not LOG_DIR.exists():
        return []
    out = [{"name": p.name, "date": p.name[10:20], "size": p.stat().st_size}
           for p in LOG_DIR.iterdir() if NAME_RE.match(p.name)]
    return sorted(out, key=lambda f: f["name"], reverse=True)


def path_for(name: str) -> Path | None:
    if not NAME_RE.match(name):
        return None
    p = LOG_DIR / name
    return p if p.exists() else None


def short(s: str | None, n: int = 80) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"
