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
APP_VERSION = "1.17.0"  # shown in Settings and the log, to tell which build is running

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
# ollama, or openai for servers with the OpenAI chat API (LM Studio, llama.cpp, Jan, vLLM); changeable in Settings
LOCAL_KIND = os.environ.get("LOCAL_KIND", "ollama")
# Common Ollama models, shown in Settings with their pull command. Sizes are the download, roughly.
RECOMMENDED_LOCAL_MODELS = [
    {"id": "qwen3:14b", "name": "Qwen3 14B", "size": "9 GB", "note": "Good writing and instruction following. A solid default with 16 GB of memory or more."},
    {"id": "qwen3:8b", "name": "Qwen3 8B", "size": "5 GB", "note": "Faster and lighter than 14B, a little less polished."},
    {"id": "gemma3:12b", "name": "Gemma 3 12B", "size": "8 GB", "note": "Google's model. Natural, readable writing."},
    {"id": "llama3.1:8b", "name": "Llama 3.1 8B", "size": "5 GB", "note": "Meta's model. Quick, fine for fit checks and reading job pages."},
    {"id": "mistral-small3.2", "name": "Mistral Small 3.2 (24B)", "size": "15 GB", "note": "Strong writing, needs 24 GB of memory or more."},
    {"id": "phi4", "name": "Phi-4 14B", "size": "9 GB", "note": "Microsoft's model. Careful and structured; good at following formats."},
    {"id": "gpt-oss:20b", "name": "gpt-oss 20B", "size": "14 GB", "note": "OpenAI's open-weight model. Thinks before answering, so it's slower."},
]
OLLAMA_NUM_CTX = int(os.environ.get("OLLAMA_NUM_CTX", "16384"))  # room for resume + job description
OLLAMA_TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", "900"))
DEFAULT_ENGINE = os.environ.get("DEFAULT_ENGINE", "claude")  # claude | local

# Overnight runs
TIMEZONE = os.environ.get("TZ_NAME", os.environ.get("TZ", "Asia/Singapore"))
NIGHTLY_SCHEDULER = os.environ.get("NIGHTLY_SCHEDULER", "1") not in ("0", "false", "no")
NIGHTLY_MAX_JOBS = int(os.environ.get("NIGHTLY_MAX_JOBS", "40"))
# "Fit-check new jobs" overnight: jobs saved in the last few days without a score, up to this many a night
NIGHTLY_FIT_NEW_DAYS = int(os.environ.get("NIGHTLY_FIT_NEW_DAYS", "3"))
NIGHTLY_FIT_NEW_MAX = int(os.environ.get("NIGHTLY_FIT_NEW_MAX", "100"))

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
