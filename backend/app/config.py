import os
from pathlib import Path

from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parents[2]
load_dotenv(ROOT_DIR / ".env")


def get_env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value if value is not None else default


HOST = get_env("HOST", "0.0.0.0")
PORT = int(get_env("PORT", "8010"))
DATABASE_PATH = ROOT_DIR / get_env("DATABASE_PATH", "storage/app.db")
STORAGE_DIR = ROOT_DIR / get_env("STORAGE_DIR", "storage")
UPLOAD_DIR = STORAGE_DIR / "uploads"
OUTPUT_DIR = STORAGE_DIR / "outputs"
FRONTEND_DIST = ROOT_DIR / "frontend" / "dist"

DEFAULT_API_BASE_URL = get_env("IMAGE_API_BASE_URL", "https://api.openai.com")
DEFAULT_API_KEY = get_env("IMAGE_API_KEY", "")


def ensure_dirs() -> None:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
