import os
from pathlib import Path

from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parents[2]
load_dotenv(ROOT_DIR / ".env")


def get_env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value if value is not None else default


def get_first_env(names: tuple[str, ...], default: str = "") -> str:
    for name in names:
        value = get_env(name, "").strip()
        if value:
            return value
    return default


def get_int_env(name: str, default: int) -> int:
    raw = get_env(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError:
        return default


def get_float_env(name: str, default: float) -> float:
    raw = get_env(name, str(default)).strip()
    try:
        return float(raw)
    except ValueError:
        return default


def get_bool_env(name: str, default: bool = False) -> bool:
    raw = get_env(name, "1" if default else "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


HOST = get_env("HOST", "0.0.0.0")
PORT = int(get_env("PORT", "8010"))
DATABASE_PATH = ROOT_DIR / get_env("DATABASE_PATH", "storage/app.db")
STORAGE_DIR = ROOT_DIR / get_env("STORAGE_DIR", "storage")
UPLOAD_DIR = STORAGE_DIR / "uploads"
OUTPUT_DIR = STORAGE_DIR / "outputs"
FRONTEND_DIST = ROOT_DIR / "frontend" / "dist"

DEFAULT_API_BASE_URL = get_first_env(
    ("IMAGE_API_BASE_URL", "IMAGEGEN_BASE_URL", "OPENAI_BASE_URL", "BASE_URL"),
    "https://api.openai.com",
)
DEFAULT_API_KEY = get_first_env(
    ("IMAGE_API_KEY", "IMAGEGEN_API_KEY", "OPENAI_API_KEY", "API_KEY"),
    "",
)

MAX_CONCURRENT_TASKS = max(1, get_int_env("MAX_CONCURRENT_TASKS", 3))
IMAGE_REQUEST_TIMEOUT_SECONDS = max(30.0, get_float_env("IMAGE_REQUEST_TIMEOUT_SECONDS", 300.0))
CHAT_PLANNER_TIMEOUT_SECONDS = max(30.0, get_float_env("CHAT_PLANNER_TIMEOUT_SECONDS", 180.0))
IMAGE_REQUEST_MAX_ATTEMPTS = max(1, get_int_env("IMAGE_REQUEST_MAX_ATTEMPTS", 3))
CHAT_PLANNER_MAX_ATTEMPTS = max(1, get_int_env("CHAT_PLANNER_MAX_ATTEMPTS", 2))
ENABLE_IMAGE_STABLE_RETRY = get_bool_env("ENABLE_IMAGE_STABLE_RETRY", True)
IMAGE_STABLE_RETRY_QUALITY = get_env("IMAGE_STABLE_RETRY_QUALITY", "medium").strip() or "medium"


def ensure_dirs() -> None:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
