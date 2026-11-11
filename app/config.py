"""All runtime settings come from environment variables (see .env.example)."""
import hashlib
import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{DATA_DIR / 'jobhacker.db'}")

# Login. If APP_PASSWORD is empty the app runs without a login screen
# (fine on a trusted LAN, NOT fine behind Tailscale Funnel / Cloudflare).
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
SECRET_KEY = os.environ.get("SECRET_KEY") or hashlib.sha256(
    ("job-hacker-2:" + APP_PASSWORD).encode()
).hexdigest()
SESSION_DAYS = int(os.environ.get("SESSION_DAYS", "90"))
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "auto")  # "auto" | "true" | "false"

# For automations (n8n, iOS Shortcuts): send header  X-API-Key: <token>
API_TOKEN = os.environ.get("API_TOKEN", "")

# AI features (extraction from pasted text, fit score, resume, cover letter)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
APP_VERSION = "1.10.1"  # shown in Settings and the log, to tell which build is running

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
# Claude models you can pick in the app (id: name, price per million tokens in/out).
CLAUDE_MODELS = {
    "claude-sonnet-5-5": {"name": "Sonnet 5.5", "price": "$2 / $10"},
    "claude-opus-5-5": {"name": "Opus 5.5", "price": "$4 / $20"},
    "claude-fable-5-2": {"name": "Fable 5.2", "price": "$8 / $40"},
}

# Local model through Ollama. host.docker.internal reaches the Mac from inside
# the container (Colima and Docker Desktop both provide it).
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://host.docker.internal:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:14b")
OLLAMA_NUM_CTX = int(os.environ.get("OLLAMA_NUM_CTX", "16384"))  # room for resume + job description
OLLAMA_TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", "900"))
DEFAULT_ENGINE = os.environ.get("DEFAULT_ENGINE", "claude")  # claude | local

# Overnight runs
TIMEZONE = os.environ.get("TZ_NAME", os.environ.get("TZ", "Asia/Singapore"))
NIGHTLY_SCHEDULER = os.environ.get("NIGHTLY_SCHEDULER", "1") not in ("0", "false", "no")
NIGHTLY_MAX_JOBS = int(os.environ.get("NIGHTLY_MAX_JOBS", "40"))

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
