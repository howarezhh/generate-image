import os
import re
from contextvars import ContextVar, Token
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
STORAGE_DIR = ROOT_DIR / get_env("STORAGE_DIR", "storage")
DATABASE_PATH = ROOT_DIR / get_env("DATABASE_PATH", "storage/app.db")
UPLOAD_DIR = STORAGE_DIR / "uploads"
OUTPUT_DIR = STORAGE_DIR / "outputs"
TENANT_STORAGE_ROOT = STORAGE_DIR / "tenants"
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
CURRENT_STORAGE_SCOPE: ContextVar[str] = ContextVar("current_storage_scope", default="")
STORAGE_SCOPE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def normalize_storage_scope(value: str | None) -> str:
    scope = (value or "").strip().lower()
    if not scope:
        return ""
    if not STORAGE_SCOPE_PATTERN.fullmatch(scope):
        raise ValueError("invalid storage scope")
    return scope


def set_storage_scope(scope: str | None) -> Token[str]:
    return CURRENT_STORAGE_SCOPE.set(normalize_storage_scope(scope))


def reset_storage_scope(token: Token[str]) -> None:
    CURRENT_STORAGE_SCOPE.reset(token)


def current_storage_scope() -> str:
    return CURRENT_STORAGE_SCOPE.get()


def storage_dir_for_scope(scope: str | None = None) -> Path:
    normalized = normalize_storage_scope(current_storage_scope() if scope is None else scope)
    if not normalized:
        return STORAGE_DIR
    return TENANT_STORAGE_ROOT / normalized


def database_path_for_scope(scope: str | None = None) -> Path:
    normalized = normalize_storage_scope(current_storage_scope() if scope is None else scope)
    if not normalized:
        return DATABASE_PATH
    return storage_dir_for_scope(normalized) / "app.db"


def upload_dir_for_scope(scope: str | None = None) -> Path:
    normalized = normalize_storage_scope(current_storage_scope() if scope is None else scope)
    if not normalized:
        return UPLOAD_DIR
    return storage_dir_for_scope(normalized) / "uploads"


def output_dir_for_scope(scope: str | None = None) -> Path:
    normalized = normalize_storage_scope(current_storage_scope() if scope is None else scope)
    if not normalized:
        return OUTPUT_DIR
    return storage_dir_for_scope(normalized) / "outputs"


def current_database_path() -> Path:
    return database_path_for_scope()


def current_upload_dir() -> Path:
    return upload_dir_for_scope()


def current_output_dir() -> Path:
    return output_dir_for_scope()


def ensure_dirs(scope: str | None = None) -> None:
    database_path_for_scope(scope).parent.mkdir(parents=True, exist_ok=True)
    upload_dir_for_scope(scope).mkdir(parents=True, exist_ok=True)
    output_dir_for_scope(scope).mkdir(parents=True, exist_ok=True)
