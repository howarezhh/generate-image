import asyncio
import copy
import hashlib
import hmac
import html
import io
import json
import re
import sqlite3
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import database as db
from .config import (
    DEFAULT_API_BASE_URL,
    DEFAULT_API_KEY,
    CHAT_PLANNER_MAX_ATTEMPTS,
    CHAT_PLANNER_TIMEOUT_SECONDS,
    current_output_dir,
    current_storage_scope,
    current_upload_dir,
    ENABLE_IMAGE_STABLE_RETRY,
    FRONTEND_DIST,
    get_env,
    IMAGE_REQUEST_MAX_ATTEMPTS,
    IMAGE_REQUEST_TIMEOUT_SECONDS,
    IMAGE_STABLE_RETRY_QUALITY,
    MAX_CONCURRENT_TASKS,
    ROOT_DIR,
    TENANT_STORAGE_ROOT,
    ensure_dirs,
    reset_storage_scope,
    set_storage_scope,
)
from .openai_compat import (
    data_url_for_file,
    ensure_image_variants,
    extract_images_from_responses,
    extract_text_from_responses,
    guess_mime,
    is_retryable_http_exception,
    post_chat_completions,
    post_json,
    post_json_stream,
    public_url_for_storage_path,
    safe_storage_folder,
    sanitize_response,
    save_upload,
)


app = FastAPI(title="GPT Image Studio", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

RUNNING_TASKS: dict[str, asyncio.Task[Any]] = {}
TASK_EVENT_SUBSCRIBERS: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
TASK_EVENT_SNAPSHOTS: dict[str, dict[str, dict[str, Any]]] = {}
IMAGE_PROVIDER_POOL_LOCK: asyncio.Lock | None = None
IMAGE_PROVIDER_POOL_STATE: dict[str, dict[str, Any]] = {}
TASK_SCHEDULER_LOOP: asyncio.Task[Any] | None = None
ACCESS_COOKIE_NAME = "studio_access"
ACCESS_PASSWORD = "hhs54666"
ADDITIONAL_DEFAULT_ACCESS_PASSWORDS = ("hhs666666",)
ACCESS_PASSWORD_PATTERN = re.compile(r"^[A-Za-z0-9]{8,32}$")
ACCESS_USER_SETTINGS_KEY = "access_users"
ACCESS_ERROR_MESSAGE = "密码错误，请联系管理员，管理员QQ为3286385052。"
ACCESS_LOGIN_PATH = "/auth/login"
ACCESS_ALLOWED_PATHS = {ACCESS_LOGIN_PATH, "/favicon.ico", "/favicon.svg"}
MAX_BATCH_VARIANTS = 24
SCHEDULER_POLL_INTERVAL_SECONDS = 1.5
DEFAULT_ACCESS_PASSWORD = ACCESS_PASSWORD.lower()
IMMUTABLE_PRIVATE_CACHE_CONTROL = "private, max-age=31536000, immutable"
NO_STORE_CACHE_CONTROL = "no-store"
PROVIDER_POOL_AUTO_RETRY_REASON = "all_providers_unavailable"
PROVIDER_POOL_AUTO_RETRY_DELAYS = (1800, 3600, 7200, 18000)


def add_vary_cookie(response: Any) -> None:
    vary = response.headers.get("Vary", "")
    if not any(part.strip().lower() == "cookie" for part in vary.split(",")):
        response.headers["Vary"] = f"{vary}, Cookie" if vary else "Cookie"


def apply_response_cache_headers(response: Any, path: str) -> Any:
    if path.startswith("/media/") or path.startswith("/assets/"):
        response.headers.setdefault("Cache-Control", IMMUTABLE_PRIVATE_CACHE_CONTROL)
        add_vary_cookie(response)
    elif path in {"/", "/index.html", ACCESS_LOGIN_PATH}:
        response.headers.setdefault("Cache-Control", NO_STORE_CACHE_CONTROL)
    return response


def normalize_access_password(value: str) -> str:
    return str(value or "").strip().lower()


DEFAULT_ACCESS_PASSWORDS = tuple(dict.fromkeys([DEFAULT_ACCESS_PASSWORD, *[normalize_access_password(value) for value in ADDITIONAL_DEFAULT_ACCESS_PASSWORDS]]))


def configured_base_access_passwords() -> tuple[str, ...]:
    raw = [normalize_access_password(part) for part in get_env("ACCESS_PASSWORDS", ",".join(DEFAULT_ACCESS_PASSWORDS)).split(",")]
    return tuple(
        dict.fromkeys(
            value
            for value in [*raw, *DEFAULT_ACCESS_PASSWORDS]
            if value and ACCESS_PASSWORD_PATTERN.fullmatch(value)
        )
    )


BASE_ACCESS_PASSWORDS = configured_base_access_passwords()


def access_storage_scope(value: str) -> str:
    normalized = normalize_access_password(value)
    if not normalized or normalized == DEFAULT_ACCESS_PASSWORD:
        return ""
    return hashlib.sha256(f"gpt-image-studio:tenant:{normalized}".encode("utf-8")).hexdigest()[:24]


def access_cookie_token(value: str) -> str:
    normalized = normalize_access_password(value)
    return hashlib.sha256(f"gpt-image-studio:access:{normalized}".encode("utf-8")).hexdigest()


def default_scope_settings_value(key: str) -> dict[str, Any]:
    token = set_storage_scope("")
    try:
        with db.connect() as conn:
            row = conn.execute("select value from settings where key = ?", (key,)).fetchone()
        if not row:
            return {}
        value = json.loads(row["value"])
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}
    finally:
        reset_storage_scope(token)


def save_default_scope_settings_value(key: str, value: dict[str, Any]) -> None:
    token = set_storage_scope("")
    try:
        with db.connect() as conn:
            conn.execute(
                "insert or replace into settings (key, value, updated_at) values (?, ?, ?)",
                (key, db.json_dumps(value), db.now_iso()),
            )
    finally:
        reset_storage_scope(token)


def load_access_user_registry() -> dict[str, Any]:
    payload = default_scope_settings_value(ACCESS_USER_SETTINGS_KEY)
    users = payload.get("users") if isinstance(payload.get("users"), list) else []
    disabled = payload.get("disabled_passwords") if isinstance(payload.get("disabled_passwords"), list) else []
    return {
        "users": [
            {"password": normalize_access_password(item.get("password")), "created_at": item.get("created_at"), "updated_at": item.get("updated_at")}
            for item in users
            if isinstance(item, dict) and ACCESS_PASSWORD_PATTERN.fullmatch(normalize_access_password(item.get("password")))
        ],
        "disabled_passwords": [
            value
            for value in (normalize_access_password(item) for item in disabled)
            if value and value != DEFAULT_ACCESS_PASSWORD and ACCESS_PASSWORD_PATTERN.fullmatch(value)
        ],
    }


def save_access_user_registry(payload: dict[str, Any]) -> None:
    save_default_scope_settings_value(ACCESS_USER_SETTINGS_KEY, payload)


def effective_access_passwords() -> tuple[str, ...]:
    registry = load_access_user_registry()
    disabled = set(registry["disabled_passwords"])
    managed = [item["password"] for item in registry["users"]]
    return tuple(
        dict.fromkeys(
            value
            for value in [*BASE_ACCESS_PASSWORDS, *managed, DEFAULT_ACCESS_PASSWORD]
            if value and value not in disabled and ACCESS_PASSWORD_PATTERN.fullmatch(value)
        )
    )


def refresh_access_password_cache() -> None:
    global ACCESS_PASSWORDS, ACCESS_COOKIE_TO_SCOPE, ACCESS_COOKIE_TO_PASSWORD
    ACCESS_PASSWORDS = effective_access_passwords()
    ACCESS_COOKIE_TO_SCOPE = {access_cookie_token(value): access_storage_scope(value) for value in ACCESS_PASSWORDS}
    ACCESS_COOKIE_TO_PASSWORD = {access_cookie_token(value): value for value in ACCESS_PASSWORDS}


ACCESS_PASSWORDS: tuple[str, ...] = ()
ACCESS_COOKIE_TO_SCOPE: dict[str, str] = {}
ACCESS_COOKIE_TO_PASSWORD: dict[str, str] = {}
refresh_access_password_cache()


def validate_api_key_text(value: str | None, *, field_label: str = "API Key") -> str:
    text = str(value or "").strip()
    if text and any(ord(char) > 127 for char in text):
        raise HTTPException(
            status_code=400,
            detail={
                "message": f"{field_label} 包含非 ASCII 字符，疑似误粘贴了中文标点或全角字符。",
                "suggestion": "请检查并删除中文句号、中文逗号、全角空格等字符后重试。",
            },
        )
    return text


def all_known_storage_scopes() -> list[str]:
    scopes = [""]
    for password in ACCESS_PASSWORDS:
        scope = access_storage_scope(password)
        if scope not in scopes:
            scopes.append(scope)
    if TENANT_STORAGE_ROOT.exists():
        for child in sorted(TENANT_STORAGE_ROOT.iterdir()):
            if not child.is_dir():
                continue
            try:
                normalized = child.name.strip().lower()
            except Exception:
                continue
            if normalized and normalized not in scopes:
                scopes.append(normalized)
    return scopes


class ClientConfig(BaseModel):
    base_url: str | None = None
    api_key: str | None = None


class ProviderRequest(BaseModel):
    name: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    api_key: str = ""


class AppSettingsRequest(BaseModel):
    value: dict[str, Any] = Field(default_factory=dict)


class ImageMetadataRequest(BaseModel):
    favorite: int | None = Field(default=None, ge=0, le=1)
    tags: list[str] | None = None


class AccessUserRequest(BaseModel):
    password: str = Field(min_length=8, max_length=32)


class PromptRequest(BaseModel):
    content: str = Field(min_length=1)
    source: str = "manual"
    mode: str | None = None
    favorite: int = 0


class StyleLockRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    subject_lock: str = ""
    composition_lock: str = ""
    color_tone_lock: str = ""
    lighting_lock: str = ""
    texture_lock: str = ""
    negative_lock: str = ""
    notes: str = ""


class CharacterProfileRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    age: str = ""
    gender: str = ""
    appearance: str = ""
    wardrobe: str = ""
    personality: str = ""
    voice_style: str = ""
    signature_items: str = ""
    extra_prompt: str = ""
    notes: str = ""


class VariantPlanItem(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    prompt_suffix: str = ""
    quality: str | None = None
    size: str | None = None
    background: str | None = None
    output_format: str | None = None
    output_compression: int | None = Field(default=None, ge=0, le=100)
    n: int | None = Field(default=None, ge=1, le=10)
    image_title: str | None = None
    style_lock_id: int | None = None
    delay_seconds: int = Field(default=0, ge=0, le=86400)


class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=1)
    image_title: str | None = None
    conversation_id: int | None = None
    model: str = "gpt-5.4"
    image_model: str = "gpt-image-2"
    size: str = "2560x1440"
    quality: str = "high"
    n: int = Field(default=1, ge=1, le=10)
    background: str = "auto"
    output_format: str = "png"
    output_compression: int | None = Field(default=None, ge=0, le=100)
    moderation: str = "auto"
    action: str = "generate"
    partial_images: int = Field(default=0, ge=0, le=3)
    style_lock_id: int | None = None
    character_profile_ids: list[int] = Field(default_factory=list)
    schedule_at: str | None = None
    schedule_spacing_seconds: int = Field(default=0, ge=0, le=86400)
    batch_label: str | None = None
    variant_plan: list[VariantPlanItem] = Field(default_factory=list)
    config: ClientConfig = Field(default_factory=ClientConfig)


class ConversationCreate(BaseModel):
    title: str = "新的生图对话"
    context_limit: int = Field(default=10, ge=0, le=50)
    mode: str | None = None


class ConversationUpdate(BaseModel):
    title: str | None = None
    context_limit: int | None = Field(default=None, ge=0, le=50)


class MessageUpdate(BaseModel):
    content: str = Field(min_length=1)


class ChatRequest(BaseModel):
    prompt: str = Field(min_length=1)
    model: str = "gpt-5.4"
    planner_model: str | None = None
    planner_endpoint: str = "responses"
    image_model: str = "gpt-image-2"
    action: str = "auto"
    size: str = "2560x1440"
    quality: str = "high"
    background: str = "auto"
    output_format: str = "png"
    output_compression: int | None = Field(default=None, ge=0, le=100)
    moderation: str = "auto"
    input_fidelity: str = "auto"
    partial_images: int = Field(default=0, ge=0, le=3)
    context_limit: int = Field(default=10, ge=0, le=50)
    reference_image_ids: list[int] = Field(default_factory=list)
    reference_image_roles: dict[str, str] = Field(default_factory=dict)
    reference_image_selection_modes: dict[str, str] = Field(default_factory=dict)
    upload_reference_roles: list[str] = Field(default_factory=list)
    upload_selection_modes: list[str] = Field(default_factory=list)
    style_lock_id: int | None = None
    character_profile_ids: list[int] = Field(default_factory=list)
    config: ClientConfig = Field(default_factory=ClientConfig)
    planner_config: ClientConfig | None = None


class StoryboardRequest(BaseModel):
    prompt: str = Field(min_length=1)
    model: str = "gpt-5.4"
    planner_model: str | None = None
    planner_endpoint: str = "responses"
    image_model: str = "gpt-image-2"
    size: str = "2560x1440"
    quality: str = "high"
    background: str = "auto"
    output_format: str = "png"
    output_compression: int | None = Field(default=None, ge=0, le=100)
    moderation: str = "auto"
    input_fidelity: str = "high"
    partial_images: int = Field(default=0, ge=0, le=3)
    context_limit: int = Field(default=10, ge=0, le=50)
    shot_limit: int = Field(default=20, ge=1, le=100)
    reference_image_ids: list[int] = Field(default_factory=list)
    reference_image_roles: dict[str, str] = Field(default_factory=dict)
    reference_image_selection_modes: dict[str, str] = Field(default_factory=dict)
    upload_reference_roles: list[str] = Field(default_factory=list)
    upload_selection_modes: list[str] = Field(default_factory=list)
    style_lock_id: int | None = None
    character_profile_ids: list[int] = Field(default_factory=list)
    config: ClientConfig = Field(default_factory=ClientConfig)
    planner_config: ClientConfig | None = None


REFERENCE_ROLE_LABELS = {
    "character": "角色锚点",
    "scene": "场景锚点",
    "wardrobe_prop": "服装道具锚点",
    "style": "风格锚点",
}
REFERENCE_SELECTION_MODE_LABELS = {
    "edit_target": "直接修改目标",
    "reference": "辅助参考",
}
DEFAULT_REFERENCE_ROLE_ORDER = ("character", "scene", "wardrobe_prop")
REFERENCE_ROLE_PRIORITY = {role: index for index, role in enumerate(("character", "scene", "wardrobe_prop", "style"))}
CONVERSATION_MODES = {"chat", "storyboard", "generate", "edit"}
MAX_STORYBOARD_SHOTS = 100
INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
PROVIDER_UNAVAILABLE_RETRY_COUNT = 1
PROVIDER_UNAVAILABLE_COOLDOWN_SECONDS = 90.0


def resolve_access_password(value: str) -> str | None:
    refresh_access_password_cache()
    normalized = normalize_access_password(value)
    if not ACCESS_PASSWORD_PATTERN.fullmatch(normalized):
        return None
    return normalized if normalized in ACCESS_PASSWORDS else None


def validate_access_password(value: str) -> bool:
    return resolve_access_password(value) is not None


def access_cookie_valid(request: Request) -> bool:
    token = str(request.cookies.get(ACCESS_COOKIE_NAME) or "")
    return token in ACCESS_COOKIE_TO_SCOPE


def request_storage_scope(request: Request) -> str | None:
    token = str(request.cookies.get(ACCESS_COOKIE_NAME) or "")
    return ACCESS_COOKIE_TO_SCOPE.get(token)


def request_access_password(request: Request) -> str | None:
    token = str(request.cookies.get(ACCESS_COOKIE_NAME) or "")
    return ACCESS_COOKIE_TO_PASSWORD.get(token)


def require_access_user_admin(request: Request) -> None:
    if request_access_password(request) != DEFAULT_ACCESS_PASSWORD:
        raise HTTPException(
            status_code=403,
            detail={
                "message": "只有主账号 hhs54666 可以管理访问用户。",
                "status_code": 403,
            },
        )


def runtime_scope_key(scope: str | None = None) -> str:
    normalized = scope if scope is not None else current_storage_scope()
    return normalized or "__default__"


def task_runtime_key(task_id: int, scope: str | None = None) -> str:
    return f"{runtime_scope_key(scope)}:{task_id}"


def provider_runtime_key(provider_id: int, scope: str | None = None) -> str:
    return f"{runtime_scope_key(scope)}:provider:{provider_id}"


def sanitized_next_path(value: str | None) -> str:
    target = str(value or "").strip()
    if not target.startswith("/") or target.startswith("//"):
        return "/"
    if target.startswith(ACCESS_LOGIN_PATH):
        return "/"
    return target


def login_page_html(next_path: str = "/", error_message: str = "") -> str:
    safe_next = html.escape(sanitized_next_path(next_path), quote=True)
    safe_error = html.escape(error_message.strip()) if error_message else ""
    error_block = (
        f'<div class="loginError" role="alert">{safe_error}</div>'
        if safe_error
        else '<div class="loginHint">请输入访问密码后继续。</div>'
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <link rel="icon" type="image/svg+xml" href="/favicon.svg" />
  <title>访问验证 - GPT Image Studio</title>
  <style>
    :root {{
      color-scheme: dark;
      font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
      background: #11130f;
      color: #f5f3ea;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      background:
        radial-gradient(circle at top left, rgba(45, 146, 127, 0.24), transparent 32%),
        radial-gradient(circle at top right, rgba(238, 203, 112, 0.18), transparent 26%),
        linear-gradient(180deg, rgba(255,255,255,0.03), transparent 40%),
        #11130f;
      padding: 24px;
    }}
    .loginCard {{
      width: min(420px, 100%);
      border: 1px solid rgba(245, 243, 234, 0.12);
      border-radius: 20px;
      padding: 28px;
      background: rgba(17, 19, 15, 0.92);
      box-shadow: 0 26px 72px rgba(0, 0, 0, 0.42);
      backdrop-filter: blur(18px);
    }}
    .loginMark {{
      width: 52px;
      height: 52px;
      border-radius: 14px;
      display: grid;
      place-items: center;
      background: #eecb70;
      color: #11130f;
      font-weight: 800;
      margin-bottom: 18px;
      font-size: 18px;
    }}
    h1 {{
      margin: 0;
      font-size: 26px;
      line-height: 1.2;
    }}
    p {{
      margin: 10px 0 0;
      color: rgba(245, 243, 234, 0.68);
      line-height: 1.65;
      font-size: 14px;
    }}
    form {{
      margin-top: 20px;
      display: grid;
      gap: 12px;
    }}
    label {{
      display: grid;
      gap: 8px;
      color: rgba(245, 243, 234, 0.76);
      font-size: 13px;
    }}
    input {{
      width: 100%;
      min-height: 46px;
      border-radius: 12px;
      border: 1px solid rgba(245, 243, 234, 0.16);
      background: rgba(255, 255, 255, 0.04);
      color: #f5f3ea;
      padding: 0 14px;
      outline: none;
      letter-spacing: 1px;
    }}
    input:focus {{
      border-color: #eecb70;
    }}
    button {{
      min-height: 46px;
      border: 0;
      border-radius: 12px;
      background: #eecb70;
      color: #11130f;
      font-weight: 700;
      cursor: pointer;
    }}
    .loginError, .loginHint {{
      margin-top: 16px;
      border-radius: 12px;
      padding: 12px 14px;
      line-height: 1.6;
      font-size: 13px;
    }}
    .loginError {{
      border: 1px solid rgba(214, 76, 57, 0.4);
      background: rgba(214, 76, 57, 0.12);
      color: #ffd5ce;
    }}
    .loginHint {{
      border: 1px solid rgba(238, 203, 112, 0.2);
      background: rgba(238, 203, 112, 0.08);
      color: rgba(245, 243, 234, 0.84);
    }}
  </style>
</head>
<body>
  <section class="loginCard">
    <div class="loginMark">鉴权</div>
    <h1>请输入访问密码</h1>
    <p>访问当前项目之前，需要先完成统一密码验证。密码仅支持 8-32 位数字或英文字母，英文字母不区分大小写。</p>
    {error_block}
    <form method="post" action="{ACCESS_LOGIN_PATH}">
      <input type="hidden" name="next" value="{safe_next}" />
      <label>
        <span>访问密码</span>
        <input
          type="password"
          name="password"
          maxlength="32"
          minlength="8"
          pattern="[A-Za-z0-9]{{8,32}}"
          autocomplete="current-password"
          inputmode="text"
          autofocus
          required
        />
      </label>
      <button type="submit">进入项目</button>
    </form>
  </section>
</body>
</html>"""


@app.middleware("http")
async def require_project_password(request: Request, call_next: Callable[..., Any]):
    path = request.url.path or "/"
    if request.method == "OPTIONS" or path in ACCESS_ALLOWED_PATHS:
        response = await call_next(request)
        return apply_response_cache_headers(response, path)
    scope = request_storage_scope(request)
    if scope is not None:
        token = set_storage_scope(scope)
        request.state.storage_scope = scope
        try:
            response = await call_next(request)
            return apply_response_cache_headers(response, path)
        finally:
            reset_storage_scope(token)
    if path.startswith("/api/"):
        return JSONResponse(
            status_code=401,
            content={
                "detail": {
                    "message": "未通过访问验证，请先输入项目访问密码。",
                    "status_code": 401,
                }
            },
        )
    if path == ACCESS_LOGIN_PATH:
        return await call_next(request)
    if request.method == "GET":
        next_path = request.url.path
        if request.url.query:
            next_path = f"{next_path}?{request.url.query}"
        login_url = f"{ACCESS_LOGIN_PATH}?next={quote(sanitized_next_path(next_path), safe='/?=&')}"
        return RedirectResponse(url=login_url, status_code=303)
    return HTMLResponse(login_page_html("/", ACCESS_ERROR_MESSAGE), status_code=401)


def public_task_image(
    item: tuple[Path, str, str],
    *,
    title: str | None = None,
    bucket: str | None = None,
    task_id: int | None = None,
    conversation_id: int | None = None,
    message_id: int | None = None,
) -> dict[str, Any]:
    file_path, public_url, mime_type = item
    variants = ensure_image_variants(file_path, mime_type, existing={"public_url": public_url})
    image_id = db.add_image(
        source="api",
        file_path=file_path,
        public_url=str(variants.get("public_url") or public_url),
        thumb_path=str(variants.get("thumb_path") or file_path),
        thumb_url=str(variants.get("thumb_url") or public_url),
        medium_path=str(variants.get("medium_path") or file_path),
        medium_url=str(variants.get("medium_url") or public_url),
        width=int(variants["width"]) if variants.get("width") is not None else None,
        height=int(variants["height"]) if variants.get("height") is not None else None,
        byte_size=int(variants["byte_size"]) if variants.get("byte_size") is not None else None,
        mime_type=mime_type,
        title=title,
        bucket=bucket,
        task_id=task_id,
        conversation_id=conversation_id,
        message_id=message_id,
    )
    return serialize_image_record(
        {
            "id": image_id,
            "source": "api",
            "task_id": task_id,
            "conversation_id": conversation_id,
            "message_id": message_id,
            "title": title,
            "bucket": bucket,
            "file_path": str(file_path),
            "public_url": str(variants.get("public_url") or public_url),
            "thumb_path": str(variants.get("thumb_path") or file_path),
            "thumb_url": str(variants.get("thumb_url") or public_url),
            "medium_path": str(variants.get("medium_path") or file_path),
            "medium_url": str(variants.get("medium_url") or public_url),
            "mime_type": mime_type,
            "width": variants.get("width"),
            "height": variants.get("height"),
            "byte_size": variants.get("byte_size"),
        }
    )


def serialize_path_image(path: Path, mime_type: str | None = None) -> dict[str, Any] | None:
    if not path.exists():
        return None
    mime = mime_type or guess_mime(path)
    variants = ensure_image_variants(path, mime, existing={"public_url": public_url_for_storage_path(path)})
    public_url = str(variants.get("public_url") or public_url_for_storage_path(path))
    return {
        "url": public_url,
        "public_url": public_url,
        "thumb_url": str(variants.get("thumb_url") or public_url),
        "medium_url": str(variants.get("medium_url") or public_url),
        "file_path": str(path),
        "thumb_path": str(variants.get("thumb_path") or path),
        "medium_path": str(variants.get("medium_path") or path),
        "filename": path.name,
        "mime_type": mime,
        "width": variants.get("width"),
        "height": variants.get("height"),
        "byte_size": variants.get("byte_size"),
    }


def persist_image_variants(conn: sqlite3.Connection, image_id: int, image: dict[str, Any]) -> None:
    conn.execute(
        """
        update images
        set public_url = ?, thumb_path = ?, thumb_url = ?, medium_path = ?, medium_url = ?, width = ?, height = ?, byte_size = ?
        where id = ?
        """,
        (
            image.get("public_url"),
            image.get("thumb_path"),
            image.get("thumb_url"),
            image.get("medium_path"),
            image.get("medium_url"),
            image.get("width"),
            image.get("height"),
            image.get("byte_size"),
            image_id,
        ),
    )


def ensure_image_payload(image: dict[str, Any], conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    file_path = str(image.get("file_path") or "").strip()
    if not file_path:
        return image
    path = Path(file_path)
    if not path.exists():
        return image
    public_url = str(image.get("public_url") or public_url_for_storage_path(path))
    variants = ensure_image_variants(
        path,
        str(image.get("mime_type") or guess_mime(path)),
        existing={
            "public_url": public_url,
            "thumb_path": image.get("thumb_path"),
            "thumb_url": image.get("thumb_url"),
            "medium_path": image.get("medium_path"),
            "medium_url": image.get("medium_url"),
            "width": image.get("width"),
            "height": image.get("height"),
            "byte_size": image.get("byte_size"),
        },
    )
    changed = False
    for field in ("public_url", "thumb_path", "thumb_url", "medium_path", "medium_url", "width", "height", "byte_size"):
        next_value = variants.get(field)
        if next_value is None and field in {"width", "height", "byte_size"}:
            continue
        if image.get(field) != next_value:
            image[field] = next_value
            changed = True
    if image.get("id") and changed:
        image_id = int(image["id"])
        if conn is not None:
            persist_image_variants(conn, image_id, image)
        else:
            with db.connect() as inner_conn:
                persist_image_variants(inner_conn, image_id, image)
    return image


def serialize_image_record(image: dict[str, Any] | sqlite3.Row, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    item = db.row_to_dict(image) if isinstance(image, sqlite3.Row) else dict(image)
    item = ensure_image_payload(item, conn)
    file_path = str(item.get("file_path") or "").strip()
    filename = Path(file_path).name if file_path else str(item.get("filename") or "generated-image.png")
    public_url = str(item.get("public_url") or "")
    thumb_url = str(item.get("thumb_url") or public_url)
    medium_url = str(item.get("medium_url") or public_url)
    return {
        "id": item.get("id"),
        "url": public_url,
        "public_url": public_url,
        "thumb_url": thumb_url,
        "medium_url": medium_url,
        "source": item.get("source"),
        "task_id": item.get("task_id"),
        "conversation_id": item.get("conversation_id"),
        "message_id": item.get("message_id"),
        "mime_type": item.get("mime_type"),
        "filename": filename,
        "file_path": file_path,
        "thumb_path": item.get("thumb_path"),
        "medium_path": item.get("medium_path"),
        "title": item.get("title"),
        "bucket": item.get("bucket"),
        "favorite": int(item.get("favorite") or 0),
        "tags": parse_image_tags(item.get("tags")),
        "created_at": item.get("created_at"),
        "conversation_title": item.get("conversation_title"),
        "message_content": item.get("message_content"),
        "task_prompt": item.get("task_prompt"),
        "task_mode": item.get("task_mode"),
        "origin_source": item.get("origin_source"),
        "width": item.get("width"),
        "height": item.get("height"),
        "byte_size": item.get("byte_size"),
    }


def public_upload_image(path_value: str) -> dict[str, Any] | None:
    path = Path(path_value)
    return serialize_path_image(path)


def public_input_image(
    item: tuple[Path, str],
    *,
    source: str = "input",
    title: str | None = None,
    task_id: int | None = None,
    conversation_id: int | None = None,
    message_id: int | None = None,
) -> dict[str, Any] | None:
    path, mime_type = item
    public = public_upload_image(str(path))
    if not public:
        return None
    variants = ensure_image_variants(path, mime_type, existing={"public_url": public["public_url"]})
    image_id = db.add_image(
        source=source,
        file_path=path,
        public_url=public["public_url"],
        thumb_path=str(variants.get("thumb_path") or path),
        thumb_url=str(variants.get("thumb_url") or public["public_url"]),
        medium_path=str(variants.get("medium_path") or path),
        medium_url=str(variants.get("medium_url") or public["public_url"]),
        width=int(variants["width"]) if variants.get("width") is not None else None,
        height=int(variants["height"]) if variants.get("height") is not None else None,
        byte_size=int(variants["byte_size"]) if variants.get("byte_size") is not None else None,
        mime_type=mime_type,
        title=title,
        task_id=task_id,
        conversation_id=conversation_id,
        message_id=message_id,
    )
    public["id"] = image_id
    public["source"] = source
    public["title"] = title
    return public


def public_message_upload_image(image: dict[str, Any]) -> dict[str, Any] | None:
    file_path = str(image.get("file_path") or "").strip()
    if not file_path:
        return None
    public = serialize_image_record(image)
    if not public:
        return None
    public.update(
        {
            "id": image.get("id"),
            "source": image.get("source") or "input",
            "title": image.get("title"),
            "task_mode": image.get("task_mode"),
            "prompt_text": image.get("task_prompt") or image.get("message_content") or image.get("title"),
        }
    )
    return public


def compact_params(data: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in data.items()
        if value is not None and value != "" and value != "default"
    }


def normalize_conversation_mode(value: Any) -> str | None:
    mode = str(value or "").strip().lower()
    return mode if mode in CONVERSATION_MODES else None


def resolved_conversation_mode(conversation: dict[str, Any] | sqlite3.Row | None, fallback: str = "chat") -> str:
    if not conversation:
        return fallback
    if isinstance(conversation, dict):
        current = normalize_conversation_mode(conversation.get("mode"))
        latest = normalize_conversation_mode(conversation.get("latest_task_mode"))
    else:
        current = normalize_conversation_mode(conversation["mode"]) if "mode" in conversation.keys() else None
        latest = normalize_conversation_mode(conversation["latest_task_mode"]) if "latest_task_mode" in conversation.keys() else None
    return current or latest or fallback


def serialize_conversation_row(conversation: dict[str, Any] | sqlite3.Row, *, latest_task_mode: str | None = None) -> dict[str, Any]:
    item = db.row_to_dict(conversation) if not isinstance(conversation, dict) else dict(conversation)
    item["mode"] = normalize_conversation_mode(item.get("mode")) or normalize_conversation_mode(latest_task_mode) or "chat"
    return item


def conversation_mode_label(mode: str | None) -> str:
    return {"chat": "对话", "storyboard": "分镜", "generate": "生图", "edit": "编辑"}.get(str(mode or ""), "当前")


def task_status_label(status: str | None) -> str:
    return {
        "scheduled": "待执行",
        "queued": "排队中",
        "running": "运行中",
        "done": "已完成",
        "failed": "失败",
        "canceled": "已停止",
    }.get(str(status or ""), "处理中")


def normalize_image_tags(values: Any) -> list[str]:
    raw_items: list[Any]
    if isinstance(values, str):
        raw_items = re.split(r"[,，\s]+", values)
    elif isinstance(values, list):
        raw_items = values
    else:
        raw_items = []
    tags: list[str] = []
    for item in raw_items:
        tag = re.sub(r"\s+", " ", str(item or "").strip())
        tag = tag.strip("#＃,，")
        if not tag:
            continue
        tag = tag[:32]
        if tag not in tags:
            tags.append(tag)
        if len(tags) >= 20:
            break
    return tags


def parse_image_tags(value: Any) -> list[str]:
    if isinstance(value, list):
        return normalize_image_tags(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return normalize_image_tags(value)
        return normalize_image_tags(parsed)
    return []


def normalize_reference_image_ids(values: Any) -> list[int]:
    clean_ids: list[int] = []
    for value in values or []:
        try:
            image_id = int(value)
        except (TypeError, ValueError):
            continue
        if image_id > 0 and image_id not in clean_ids:
            clean_ids.append(image_id)
    return clean_ids


def load_app_settings_value() -> dict[str, Any]:
    with db.connect() as conn:
        row = conn.execute("select value from settings where key = ?", ("app_settings",)).fetchone()
    if not row:
        return {}
    try:
        value = json.loads(row["value"] or "{}")
    except json.JSONDecodeError:
        value = {}
    return value if isinstance(value, dict) else {}


def normalize_provider_id_list(values: Any) -> list[int]:
    ids: list[int] = []
    for value in values if isinstance(values, list) else []:
        try:
            provider_id = int(value)
        except (TypeError, ValueError):
            continue
        if provider_id > 0 and provider_id not in ids:
            ids.append(provider_id)
    return ids


def configured_image_provider_pool_ids(settings_value: dict[str, Any] | None = None) -> list[int]:
    value = settings_value or load_app_settings_value()
    ids = normalize_provider_id_list(value.get("imageProviderPool"))
    if ids:
        return ids
    legacy = value.get("modeProviders") if isinstance(value.get("modeProviders"), dict) else {}
    return normalize_provider_id_list(list(legacy.values()))


def load_provider_rows() -> list[dict[str, Any]]:
    ensure_default_provider()
    with db.connect() as conn:
        rows = conn.execute("select * from providers order by id asc").fetchall()
    return [db.row_to_dict(row) for row in rows]


def provider_client_config(provider: dict[str, Any]) -> ClientConfig:
    return ClientConfig(
        base_url=str(provider.get("base_url") or DEFAULT_API_BASE_URL),
        api_key=str(provider.get("api_key") or DEFAULT_API_KEY),
    )


def load_image_provider_pool() -> list[dict[str, Any]]:
    providers = load_provider_rows()
    if not providers:
        return []
    configured_ids = configured_image_provider_pool_ids()
    by_id = {int(provider["id"]): provider for provider in providers}
    ordered = [by_id[provider_id] for provider_id in configured_ids if provider_id in by_id]
    return ordered or providers


def provider_pool_capacity() -> int:
    return max(1, len(load_image_provider_pool())) * MAX_CONCURRENT_TASKS


def ensure_provider_pool_lock() -> asyncio.Lock:
    global IMAGE_PROVIDER_POOL_LOCK
    if IMAGE_PROVIDER_POOL_LOCK is None:
        IMAGE_PROVIDER_POOL_LOCK = asyncio.Lock()
    return IMAGE_PROVIDER_POOL_LOCK


def ensure_provider_pool_state(provider: dict[str, Any], order_index: int) -> dict[str, Any]:
    provider_id = int(provider["id"])
    state_key = provider_runtime_key(provider_id)
    state = IMAGE_PROVIDER_POOL_STATE.get(state_key)
    if state is None:
        state = {
            "provider": dict(provider),
            "order": order_index,
            "semaphore": asyncio.Semaphore(MAX_CONCURRENT_TASKS),
            "assigned_count": 0,
            "running_count": 0,
            "unavailable_until": 0.0,
            "last_unavailable_error": None,
        }
        IMAGE_PROVIDER_POOL_STATE[state_key] = state
        return state
    state["provider"] = dict(provider)
    state["order"] = order_index
    return state


def provider_unavailable_seconds(state: dict[str, Any], now: float | None = None) -> int:
    current = time.time() if now is None else now
    remaining = float(state.get("unavailable_until") or 0.0) - current
    if remaining <= 0:
        return 0
    return int(remaining) if remaining.is_integer() else int(remaining) + 1


def provider_is_temporarily_unavailable(state: dict[str, Any], now: float | None = None) -> bool:
    return provider_unavailable_seconds(state, now) > 0


def clear_provider_unavailable_state(state: dict[str, Any]) -> None:
    state["unavailable_until"] = 0.0
    state["last_unavailable_error"] = None


def mark_provider_unavailable(state: dict[str, Any], detail: Any) -> None:
    state["unavailable_until"] = time.time() + PROVIDER_UNAVAILABLE_COOLDOWN_SECONDS
    state["last_unavailable_error"] = copy.deepcopy(detail)


def provider_error_message(detail: Any) -> str:
    if isinstance(detail, dict):
        return str(detail.get("message") or detail.get("detail") or detail.get("error") or "提供商请求失败").strip() or "提供商请求失败"
    return str(detail or "提供商请求失败").strip() or "提供商请求失败"


def provider_attempt_entry(provider: dict[str, Any], detail: Any, *, action: str, attempt: int) -> dict[str, Any]:
    return {
        "provider_id": int(provider["id"]),
        "provider_name": str(provider["name"]),
        "action": action,
        "attempt": attempt,
        "error": copy.deepcopy(detail),
        "message": provider_error_message(detail),
    }


def provider_attempts_from_error_detail(detail: Any) -> list[dict[str, Any]]:
    if not isinstance(detail, dict):
        return []
    attempts = detail.get("provider_attempts")
    if not isinstance(attempts, list):
        return []
    return [copy.deepcopy(item) for item in attempts if isinstance(item, dict)]


def merge_provider_attempt_logs(existing: list[dict[str, Any]], detail: Any) -> list[dict[str, Any]]:
    merged = copy.deepcopy(existing)
    for item in provider_attempts_from_error_detail(detail):
        if item not in merged:
            merged.append(item)
    return merged


def provider_failure_error_detail(
    *,
    message: str,
    attempts: list[dict[str, Any]],
    current_provider: dict[str, Any] | None = None,
    suggestion: str | None = None,
) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "message": message,
        "provider_attempts": copy.deepcopy(attempts),
    }
    if current_provider:
        detail["image_provider"] = {"id": current_provider["id"], "name": current_provider["name"]}
    if suggestion:
        detail["suggestion"] = suggestion
    return detail


def is_provider_unavailable_error(exc: HTTPException) -> bool:
    detail_text = json.dumps(exc.detail, ensure_ascii=False).lower() if not isinstance(exc.detail, str) else exc.detail.lower()
    if any(marker in detail_text for marker in ("moderation_blocked", "content_policy", "safety_violations")):
        return False
    if is_retryable_http_exception(exc):
        return True
    if int(exc.status_code or 0) in {401, 403, 404}:
        return True
    return any(
        marker in detail_text
        for marker in (
            "authentication",
            "unauthorized",
            "forbidden",
            "api key",
            "model_not_found",
            "not found",
            "connection refused",
            "name or service not known",
        )
    )


def all_providers_unavailable_detail(states: list[dict[str, Any]], attempted: list[dict[str, Any]]) -> dict[str, Any]:
    now = time.time()
    providers: list[dict[str, Any]] = []
    for state in states:
        provider = state["provider"]
        unavailable_seconds = provider_unavailable_seconds(state, now)
        providers.append(
            {
                "id": int(provider["id"]),
                "name": str(provider["name"]),
                "running_tasks": int(state["running_count"]),
                "assigned_tasks": int(state["assigned_count"]),
                "unavailable_seconds": unavailable_seconds,
                "last_error": copy.deepcopy(state.get("last_unavailable_error")),
            }
        )
    return {
        "message": "当前生图池中的所有提供商都暂时不可用，无法继续自动切换生图线路。",
        "providers": providers,
        "provider_attempts": copy.deepcopy(attempted),
        "suggestion": "请检查提供商接口地址、密钥和模型兼容性，或等待线路恢复后重试。",
    }


def image_provider_pool_snapshot() -> dict[str, Any]:
    pool = load_image_provider_pool()
    now = time.time()
    providers: list[dict[str, Any]] = []
    for index, provider in enumerate(pool):
        state = ensure_provider_pool_state(provider, index)
        assigned = int(state["assigned_count"]) if state else 0
        running = int(state["running_count"]) if state else 0
        unavailable_seconds = provider_unavailable_seconds(state, now) if state else 0
        available = unavailable_seconds <= 0
        providers.append(
            {
                "id": provider["id"],
                "name": provider["name"],
                "base_url": provider["base_url"],
                "assigned_tasks": assigned,
                "running_tasks": running,
                "idle_slots": max(0, MAX_CONCURRENT_TASKS - running) if available else 0,
                "order": index,
                "available": available,
                "status": "unavailable" if not available else ("busy" if assigned > 0 else "idle"),
                "unavailable_seconds": unavailable_seconds,
                "last_error": copy.deepcopy(state.get("last_unavailable_error")) if state else None,
            }
        )
    total = len(providers)
    used = sum(1 for provider in providers if provider["assigned_tasks"] > 0)
    unavailable = sum(1 for provider in providers if not provider["available"])
    idle = sum(1 for provider in providers if provider["available"] and provider["assigned_tasks"] <= 0)
    return {
        "total_providers": total,
        "used_providers": used,
        "idle_providers": idle,
        "available_providers": max(0, total - unavailable),
        "unavailable_providers": unavailable,
        "limit_per_provider": MAX_CONCURRENT_TASKS,
        "total_capacity": max(1, total) * MAX_CONCURRENT_TASKS,
        "assigned_tasks": sum(provider["assigned_tasks"] for provider in providers),
        "running_tasks": sum(provider["running_tasks"] for provider in providers),
        "providers": providers,
    }


def normalize_reference_role(value: Any, ordinal: int = 1) -> str:
    role = str(value or "").strip().lower()
    if role in REFERENCE_ROLE_LABELS:
        return role
    if ordinal <= len(DEFAULT_REFERENCE_ROLE_ORDER):
        return DEFAULT_REFERENCE_ROLE_ORDER[ordinal - 1]
    return "style"


def reference_role_label(role: str) -> str:
    return REFERENCE_ROLE_LABELS.get(role, REFERENCE_ROLE_LABELS["style"])


def normalize_reference_selection_mode(value: Any, *, default: str = "reference") -> str:
    mode = str(value or "").strip().lower()
    if mode in REFERENCE_SELECTION_MODE_LABELS:
        return mode
    return default


def reference_selection_mode_label(mode: str) -> str:
    return REFERENCE_SELECTION_MODE_LABELS.get(mode, REFERENCE_SELECTION_MODE_LABELS["reference"])


def normalize_edit_upload_selection_modes(raw_modes: Any, image_count: int) -> list[str]:
    modes: list[str] = []
    raw_list = raw_modes if isinstance(raw_modes, list) else []
    for index in range(max(0, image_count)):
        default = "edit_target" if index == 0 else "reference"
        modes.append(normalize_reference_selection_mode(raw_list[index] if index < len(raw_list) else None, default=default))
    if modes and "edit_target" not in modes:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "编辑模式至少需要指定一张直接修改目标图。",
                "suggestion": "请把要直接修改的图片标记为“直接修改”，其它图片标记为“辅助参考”。",
            },
        )
    return modes


def reference_candidate_hint(candidate: dict[str, Any]) -> str:
    hint = str(candidate.get("hint") or "").strip()
    if hint:
        return hint
    return "无额外说明"


def build_reference_input_note(candidate: dict[str, Any], index: int, *, usage: str = "reference") -> str:
    role = str(candidate.get("role") or "style")
    role_label = reference_role_label(role)
    hint = reference_candidate_hint(candidate)
    source = "用户本轮上传" if candidate.get("source") == "upload" else "用户显式选择的历史参考"
    if usage == "edit_target":
        return (
            f"Input image {index}: 直接修改目标图。"
            f"用户明确希望修改这张图；优先保留其主体身份、构图、空间关系和未被点名修改的区域。"
            f"来源={source}；角色={role_label}；已知说明={hint}。"
        )
    return (
        f"Input image {index}: 辅助参考图，角色={role_label}。"
        f"把这张图当作固定锚点或风格参考，不要把它当作默认编辑目标；"
        f"优先保留与该角色对应的身份/场景/服装/风格信息；"
        f"来源={source}；已知说明={hint}。"
    )


def serialize_seed_images(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for candidate in candidates:
        path = candidate.get("path")
        if not isinstance(path, Path):
            continue
        selection_mode = normalize_reference_selection_mode(candidate.get("selection_mode"), default="reference")
        items.append(
            {
                "ref": candidate.get("ref"),
                "source": candidate.get("source"),
                "id": candidate.get("id"),
                "message_id": candidate.get("message_id"),
                "task_id": candidate.get("task_id"),
                "file_path": str(path),
                "mime_type": candidate.get("mime_type") or "image/png",
                "hint": candidate.get("hint") or "",
                "role": candidate.get("role") or "style",
                "selection_mode": selection_mode,
            }
        )
    return items


def sanitize_reference_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {key: value for key, value in candidate.items() if key not in {"path"}}
        for candidate in candidates
    ]


def clamp_image_count(value: Any) -> int:
    try:
        count = int(value)
    except (TypeError, ValueError):
        count = 1
    return max(1, min(count, 10))


def parse_params(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="params_json is not valid JSON") from exc
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="params_json must be an object")
    return value


def fix_mojibake(value: str) -> str:
    if not any(marker in value for marker in ("Ã", "Â", "°", "Ñ", "Ö", "£", "¬")):
        return value
    for encoding in ("latin1", "cp1252"):
        try:
            fixed = value.encode(encoding).decode("utf-8")
            if fixed != value:
                return fixed
        except UnicodeError:
            continue
    return value


def normalize_text_fields(data: dict[str, Any], keys: tuple[str, ...] = ("prompt",)) -> dict[str, Any]:
    for key in keys:
        if isinstance(data.get(key), str):
            data[key] = fix_mojibake(data[key])
    return data


def normalize_image_title(value: Any, fallback: str = "") -> str:
    text = fix_mojibake(str(value or "")).replace("\u3000", " ").strip()
    text = INVALID_FILENAME_CHARS.sub("-", text)
    text = re.sub(r"\s+", " ", text).strip(" .-_")
    return text[:80] or fallback


def normalize_free_text(value: Any, limit: int = 1000) -> str:
    text = fix_mojibake(str(value or "")).replace("\u3000", " ").strip()
    return text[:limit]


def normalize_profile_id_list(values: Any, limit: int = 6) -> list[int]:
    ids: list[int] = []
    for value in values if isinstance(values, list) else []:
        try:
            profile_id = int(value)
        except (TypeError, ValueError):
            continue
        if profile_id > 0 and profile_id not in ids:
            ids.append(profile_id)
        if len(ids) >= limit:
            break
    return ids


def normalize_schedule_at(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"message": "定时执行时间格式不正确，请使用浏览器日期时间控件提交的时间。"},
        ) from exc
    if stamp.tzinfo is None:
        local_tz = datetime.now().astimezone().tzinfo or timezone.utc
        stamp = stamp.replace(tzinfo=local_tz)
    return stamp.astimezone(timezone.utc).isoformat()


def schedule_time_label(value: str | None) -> str:
    if not value:
        return ""
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    return stamp.astimezone().strftime("%m-%d %H:%M")


def scheduled_task_stage(
    scheduled_for: str | None,
    *,
    queue_position: int | None = None,
    queue_total: int | None = None,
    waiting_reason: str | None = None,
) -> str:
    queue_text = ""
    if queue_total and queue_total > 1 and queue_position:
        queue_text = f"第 {queue_position}/{queue_total} 个"
    prefix = f"{queue_text}批量任务" if queue_text else "任务"
    if waiting_reason:
        return f"{prefix}等待执行：{waiting_reason}"
    time_text = schedule_time_label(scheduled_for)
    return f"{prefix}将于 {time_text} 执行" if time_text else "等待定时执行"


def retry_delay_label(seconds: int) -> str:
    value = max(int(seconds), 0)
    if value % 3600 == 0 and value > 0:
        return f"{value // 3600}小时"
    if value % 60 == 0 and value > 0:
        return f"{value // 60}分钟"
    return f"{value}秒"


def provider_pool_auto_retry_stage(scheduled_for: str, retry_index: int, total_retries: int, delay_seconds: int) -> str:
    time_text = schedule_time_label(scheduled_for)
    delay_text = retry_delay_label(delay_seconds)
    return f"全部生图提供商暂不可用，已进入自动重试队列：第 {retry_index}/{total_retries} 次将于 {time_text} 执行（{delay_text}后）"


def serialize_style_lock_row(row: dict[str, Any] | sqlite3.Row) -> dict[str, Any]:
    item = db.row_to_dict(row) if not isinstance(row, dict) else dict(row)
    return {
        **item,
        "name": normalize_image_title(item.get("name"), fallback="未命名风格锁"),
        "subject_lock": normalize_free_text(item.get("subject_lock"), 600),
        "composition_lock": normalize_free_text(item.get("composition_lock"), 600),
        "color_tone_lock": normalize_free_text(item.get("color_tone_lock"), 600),
        "lighting_lock": normalize_free_text(item.get("lighting_lock"), 600),
        "texture_lock": normalize_free_text(item.get("texture_lock"), 600),
        "negative_lock": normalize_free_text(item.get("negative_lock"), 600),
        "notes": normalize_free_text(item.get("notes"), 1000),
    }


def serialize_character_profile_row(row: dict[str, Any] | sqlite3.Row) -> dict[str, Any]:
    item = db.row_to_dict(row) if not isinstance(row, dict) else dict(row)
    return {
        **item,
        "name": normalize_image_title(item.get("name"), fallback="未命名角色"),
        "age": normalize_free_text(item.get("age"), 120),
        "gender": normalize_free_text(item.get("gender"), 120),
        "appearance": normalize_free_text(item.get("appearance"), 800),
        "wardrobe": normalize_free_text(item.get("wardrobe"), 800),
        "personality": normalize_free_text(item.get("personality"), 600),
        "voice_style": normalize_free_text(item.get("voice_style"), 400),
        "signature_items": normalize_free_text(item.get("signature_items"), 400),
        "extra_prompt": normalize_free_text(item.get("extra_prompt"), 800),
        "notes": normalize_free_text(item.get("notes"), 1000),
    }


def load_style_lock(style_lock_id: int | None) -> dict[str, Any] | None:
    if not style_lock_id:
        return None
    with db.connect() as conn:
        row = conn.execute("select * from style_locks where id = ?", (int(style_lock_id),)).fetchone()
    return serialize_style_lock_row(row) if row else None


def load_character_profiles(profile_ids: list[int] | Any) -> list[dict[str, Any]]:
    ids = normalize_profile_id_list(profile_ids)
    if not ids:
        return []
    placeholders = ", ".join("?" for _ in ids)
    with db.connect() as conn:
        rows = conn.execute(
            f"select * from character_profiles where id in ({placeholders}) order by id asc",
            ids,
        ).fetchall()
    by_id = {int(row["id"]): serialize_character_profile_row(row) for row in rows}
    return [by_id[item] for item in ids if item in by_id]


def style_lock_prompt_block(style_lock: dict[str, Any] | None) -> str:
    if not style_lock:
        return ""
    lines: list[str] = [f"风格锁定：{style_lock.get('name') or '未命名风格锁'}"]
    mapping = [
        ("主体保持", style_lock.get("subject_lock")),
        ("构图保持", style_lock.get("composition_lock")),
        ("色调保持", style_lock.get("color_tone_lock")),
        ("光线保持", style_lock.get("lighting_lock")),
        ("材质质感保持", style_lock.get("texture_lock")),
        ("需要规避", style_lock.get("negative_lock")),
    ]
    for label, value in mapping:
        text = normalize_free_text(value, 800)
        if text:
            lines.append(f"- {label}：{text}")
    return "\n".join(lines)


def character_profiles_prompt_block(character_profiles: list[dict[str, Any]]) -> str:
    if not character_profiles:
        return ""
    lines = ["角色档案锁定："]
    for index, profile in enumerate(character_profiles, start=1):
        parts = [f"{index}. {profile.get('name') or f'角色{index}'}"]
        if profile.get("age"):
            parts.append(f"年龄={profile['age']}")
        if profile.get("gender"):
            parts.append(f"性别={profile['gender']}")
        for label, key in [
            ("外观", "appearance"),
            ("服装", "wardrobe"),
            ("性格", "personality"),
            ("标志物", "signature_items"),
            ("额外提示", "extra_prompt"),
        ]:
            text = normalize_free_text(profile.get(key), 800)
            if text:
                parts.append(f"{label}={text}")
        lines.append("；".join(parts))
    return "\n".join(lines)


def planner_constraint_block(
    character_profiles: list[dict[str, Any]] | None = None,
    style_lock: dict[str, Any] | None = None,
) -> str:
    blocks = [
        character_profiles_prompt_block(character_profiles or []),
        style_lock_prompt_block(style_lock),
    ]
    blocks = [block for block in blocks if block]
    if not blocks:
        return ""
    return "你还必须遵守以下长期一致性约束：\n" + "\n".join(blocks)


def apply_locked_prompt(
    prompt: str,
    *,
    character_profiles: list[dict[str, Any]] | None = None,
    style_lock: dict[str, Any] | None = None,
    variant_prompt_suffix: str = "",
) -> str:
    sections = [normalize_free_text(prompt, 4000)]
    extra = normalize_free_text(variant_prompt_suffix, 1200)
    if extra:
        sections.append(f"本次变体附加要求：{extra}")
    constraint = planner_constraint_block(character_profiles, style_lock)
    if constraint:
        sections.append(f"最终执行时必须严格保持以下约束：\n{constraint}")
    return "\n\n".join(part for part in sections if part).strip()


def normalize_variant_plan(values: Any) -> list[dict[str, Any]]:
    variants: list[dict[str, Any]] = []
    for index, item in enumerate(values if isinstance(values, list) else [], start=1):
        if len(variants) >= MAX_BATCH_VARIANTS:
            break
        if not isinstance(item, dict):
            continue
        try:
            payload = VariantPlanItem(**item).model_dump()
        except Exception as exc:
            raise HTTPException(status_code=400, detail={"message": f"第 {index} 个批量变体配置无效，请检查名称、数量和参数格式。"}) from exc
        payload["name"] = normalize_image_title(payload.get("name") or f"变体{index}", fallback=f"变体{index}")
        payload["prompt_suffix"] = normalize_free_text(payload.get("prompt_suffix"), 1200)
        payload["image_title"] = normalize_image_title(payload.get("image_title") or "")
        variants.append(payload)
    return variants


def task_batch_group_id(mode: str, prompt: str) -> str:
    seed = f"{mode}:{prompt}:{db.now_iso()}:{time.time_ns()}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]


def add_seconds_to_iso(value: str, seconds: int) -> str:
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (stamp + timedelta(seconds=max(int(seconds), 0))).astimezone(timezone.utc).isoformat()


def normalize_filename_stem(value: Any, fallback: str = "image") -> str:
    stem = normalize_image_title(value, fallback=fallback)
    stem = stem.rstrip(". ").strip()
    return stem or fallback


def build_sequenced_title(base_title: str, index: int, total: int) -> str:
    title = normalize_image_title(base_title)
    if not title:
        title = "图片"
    if total > 1:
        return f"{title}-{index:02d}"
    return title


def build_timestamp_label(value: str | None = None) -> str:
    raw = str(value or "").strip()
    if raw:
        try:
            stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            stamp = datetime.now(timezone.utc)
    else:
        stamp = datetime.now(timezone.utc)
    return stamp.astimezone().strftime("%Y%m%d-%H%M%S")


def conversation_title_for_naming(conversation_id: int | None, fallback: str = "") -> str:
    if conversation_id:
        with db.connect() as conn:
            row = conn.execute("select title from conversations where id = ?", (conversation_id,)).fetchone()
        if row and str(row["title"] or "").strip():
            return normalize_image_title(row["title"], fallback=fallback)
    return normalize_image_title(fallback)


def build_direct_mode_base_title(
    preferred_title: str | None,
    *,
    conversation_id: int | None,
    prompt: str,
    created_at: str | None = None,
) -> str:
    explicit = normalize_image_title(preferred_title)
    if explicit:
        return explicit
    conversation_title = conversation_title_for_naming(conversation_id, fallback=prompt[:20] or "图片")
    return normalize_image_title(f"{build_timestamp_label(created_at)} {conversation_title}", fallback=build_timestamp_label(created_at))


def summarize_task(row: Any, *, include_response: bool = True) -> dict[str, Any]:
    item = db.row_to_dict(row)
    raw_response_json = item.get("response_json")
    checkpoint_json = item.get("checkpoint_json")
    if isinstance(item.get("params_json"), str):
        try:
            item["params"] = json.loads(item["params_json"])
        except json.JSONDecodeError:
            item["params"] = {}
    if include_response and isinstance(raw_response_json, str):
        try:
            item["response"] = json.loads(raw_response_json)
        except json.JSONDecodeError:
            item["response"] = None
        if len(raw_response_json) > 2000:
            item["response_json"] = f"[response omitted, {len(raw_response_json)} chars]"
    elif isinstance(raw_response_json, str):
        item["response"] = None
        item["response_json"] = f"[response omitted, {len(raw_response_json)} chars]"
    if isinstance(item.get("error"), str) and item["error"]:
        try:
            item["error_detail"] = json.loads(item["error"])
        except json.JSONDecodeError:
            item["error_detail"] = item["error"]
    if isinstance(checkpoint_json, str):
        try:
            item["checkpoint"] = json.loads(checkpoint_json)
        except json.JSONDecodeError:
            item["checkpoint"] = None
    item["prompt_text"] = prompt_text_for_task(item)
    return item


def prompt_text_for_task(task: dict[str, Any]) -> str:
    response = task.get("response")
    if isinstance(response, dict):
        raw = response.get("raw")
        if isinstance(raw, dict) and raw.get("image_prompt"):
            return str(raw["image_prompt"])
        if isinstance(response.get("image_prompt"), str):
            return response["image_prompt"]
    params = task.get("params")
    if isinstance(params, dict) and params.get("prompt"):
        return str(params["prompt"])
    return str(task.get("prompt") or "")


def enrich_images_with_prompt(images: list[dict[str, Any]], task: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    prompt_text = prompt_text_for_task(task) if task else ""
    storyboard_prompts: dict[str, str] = {}
    if task and task.get("mode") == "storyboard":
        params = task.get("params") if isinstance(task.get("params"), dict) else {}
        storyboard = params.get("storyboard") if isinstance(params.get("storyboard"), dict) else {}
        storyboard_shots = storyboard.get("shots", [])
        if not isinstance(storyboard_shots, list):
            storyboard_shots = []
        for shot in storyboard_shots:
            if isinstance(shot, dict) and shot.get("name"):
                shot_prompt = str(shot.get("execution_prompt") or shot.get("prompt") or "").strip()
                if shot_prompt:
                    storyboard_prompts[str(shot["name"])] = shot_prompt
    for image in images:
        if image.get("source") == "api" and storyboard_prompts.get(str(image.get("title") or "")):
            image["prompt_text"] = storyboard_prompts[str(image.get("title") or "")]
            continue
        if image.get("source") == "api" and prompt_text:
            image["prompt_text"] = prompt_text
    return images


def task_with_images(task_id: int) -> dict[str, Any]:
    with db.connect() as conn:
        row = conn.execute("select * from tasks where id = ?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="task not found")
        images = [
            serialize_image_record(image, conn)
            for image in conn.execute(
                "select * from images where task_id = ? order by id asc",
                (task_id,),
            ).fetchall()
        ]
    item = summarize_task(row)
    item["images"] = enrich_images_with_prompt(images, item)
    return item


def compact_error_detail(detail: Any) -> str:
    return json.dumps(detail, ensure_ascii=False, indent=2) if not isinstance(detail, str) else detail


def sse_format(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


def publish_task_event(task_id: int, event: str, data: dict[str, Any], *, snapshot: bool = True) -> None:
    runtime_key = task_runtime_key(task_id)
    payload = {"event": event, "data": data}
    if snapshot:
        TASK_EVENT_SNAPSHOTS.setdefault(runtime_key, {})[event] = payload
    dead: list[asyncio.Queue[dict[str, Any]]] = []
    for queue in TASK_EVENT_SUBSCRIBERS.get(runtime_key, set()):
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            dead.append(queue)
    for queue in dead:
        TASK_EVENT_SUBSCRIBERS.get(runtime_key, set()).discard(queue)


def publish_task_snapshot(task_id: int) -> None:
    try:
        task = task_with_images(task_id)
    except HTTPException:
        task = None
    if task:
        publish_task_event(task_id, "task_update", {"task": task}, snapshot=True)


def summarize_task_like(task: dict[str, Any]) -> dict[str, Any]:
    item = dict(task)
    if isinstance(item.get("params_json"), str):
        try:
            item["params"] = json.loads(item["params_json"])
        except json.JSONDecodeError:
            item["params"] = {}
    if isinstance(item.get("response_json"), str):
        try:
            item["response"] = json.loads(item["response_json"])
        except json.JSONDecodeError:
            item["response"] = None
    if isinstance(item.get("error"), str) and item["error"]:
        try:
            item["error_detail"] = json.loads(item["error"])
        except json.JSONDecodeError:
            item["error_detail"] = item["error"]
    if isinstance(item.get("checkpoint_json"), str):
        try:
            item["checkpoint"] = json.loads(item["checkpoint_json"] or "{}")
        except json.JSONDecodeError:
            item["checkpoint"] = None
    item["prompt_text"] = prompt_text_for_task(item)
    return item


def compact_checkpoint_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: compact_checkpoint_payload(item)
            for key, item in value.items()
            if item is not None
        }
    if isinstance(value, list):
        return [compact_checkpoint_payload(item) for item in value]
    return value


def serialize_upload_items(items: list[tuple[Path, str]]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for path, mime_type in items:
        payload.append({"file_path": str(path), "mime_type": mime_type})
    return payload


def restore_upload_items(items: list[dict[str, Any]] | Any) -> list[tuple[Path, str]]:
    restored: list[tuple[Path, str]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        path = Path(str(item.get("file_path") or ""))
        if not path.exists():
            continue
        restored.append((path, str(item.get("mime_type") or guess_mime(path))))
    return restored


def task_checkpoint_dict(task: dict[str, Any] | None) -> dict[str, Any]:
    if not task:
        return {}
    checkpoint = task.get("checkpoint")
    return checkpoint if isinstance(checkpoint, dict) else {}


def existing_task_output_images(task: dict[str, Any] | None, *, completed_count: int | None = None) -> list[dict[str, Any]]:
    images = [copy.deepcopy(image) for image in (task or {}).get("images", []) if image.get("source") == "api"]
    if completed_count is None:
        return images
    return images[: max(int(completed_count), 0)]


def strip_auto_retry_checkpoint(checkpoint: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(checkpoint, dict):
        return {}
    sanitized = copy.deepcopy(checkpoint)
    sanitized.pop("auto_retry", None)
    return sanitized


def is_all_providers_unavailable_detail(detail: Any) -> bool:
    if not isinstance(detail, dict):
        return False
    message = str(detail.get("message") or "").strip()
    providers = detail.get("providers")
    return bool(message and "所有提供商都暂时不可用" in message and isinstance(providers, list))


def is_all_providers_unavailable_exception(exc: HTTPException) -> bool:
    return int(exc.status_code or 0) == 503 and is_all_providers_unavailable_detail(exc.detail)


def schedule_provider_pool_auto_retry(task_id: int, exc: HTTPException) -> dict[str, Any] | None:
    task = task_with_images(task_id)
    checkpoint = task_checkpoint_dict(task)
    auto_retry = checkpoint.get("auto_retry") if isinstance(checkpoint.get("auto_retry"), dict) else {}
    scheduled_count = max(int(auto_retry.get("scheduled_count") or 0), 0)
    if scheduled_count >= len(PROVIDER_POOL_AUTO_RETRY_DELAYS):
        return None
    delay_seconds = PROVIDER_POOL_AUTO_RETRY_DELAYS[scheduled_count]
    next_retry_index = scheduled_count + 1
    scheduled_for = add_seconds_to_iso(db.now_iso(), delay_seconds)
    detail = copy.deepcopy(exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)})
    auto_retry_state = {
        "reason": PROVIDER_POOL_AUTO_RETRY_REASON,
        "scheduled_count": next_retry_index,
        "max_retries": len(PROVIDER_POOL_AUTO_RETRY_DELAYS),
        "next_retry_at": scheduled_for,
        "next_delay_seconds": delay_seconds,
        "retry_delays_seconds": list(PROVIDER_POOL_AUTO_RETRY_DELAYS),
        "last_error": detail,
        "last_failure_at": db.now_iso(),
    }
    next_checkpoint = compact_checkpoint_payload(
        {
            **checkpoint,
            "updated_at": db.now_iso(),
            "auto_retry": auto_retry_state,
        }
    )
    stage = provider_pool_auto_retry_stage(scheduled_for, next_retry_index, len(PROVIDER_POOL_AUTO_RETRY_DELAYS), delay_seconds)
    db.update_task(
        task_id,
        status="scheduled",
        scheduled_for=scheduled_for,
        stage=stage,
        checkpoint_json=db.json_dumps(next_checkpoint),
        error=None,
        cancel_requested=0,
    )
    return {
        "retry_index": next_retry_index,
        "max_retries": len(PROVIDER_POOL_AUTO_RETRY_DELAYS),
        "scheduled_for": scheduled_for,
        "delay_seconds": delay_seconds,
        "stage": stage,
        "reason": PROVIDER_POOL_AUTO_RETRY_REASON,
    }


def persist_task_checkpoint(
    task_id: int,
    *,
    mode: str,
    step: str,
    progress: int | None = None,
    stage: str | None = None,
    can_resume: bool = False,
    **data: Any,
) -> dict[str, Any]:
    if "auto_retry" not in data:
        existing_task = db.get_task(task_id)
        if existing_task and isinstance(existing_task.get("checkpoint_json"), str):
            try:
                existing_checkpoint = json.loads(existing_task["checkpoint_json"] or "{}")
            except json.JSONDecodeError:
                existing_checkpoint = {}
            if isinstance(existing_checkpoint, dict) and isinstance(existing_checkpoint.get("auto_retry"), dict):
                data["auto_retry"] = copy.deepcopy(existing_checkpoint["auto_retry"])
    runtime_fields: dict[str, Any] = {}
    if progress is not None:
        runtime_fields["progress"] = progress
    if stage is not None:
        runtime_fields["stage"] = stage
    checkpoint = compact_checkpoint_payload(
        {
            "version": 1,
            "mode": mode,
            "step": step,
            "can_resume": can_resume,
            "updated_at": db.now_iso(),
            **runtime_fields,
            **data,
        }
    )
    fields: dict[str, Any] = {"checkpoint_json": db.json_dumps(checkpoint)}
    if progress is not None:
        fields["progress"] = progress
    if stage is not None:
        fields["stage"] = stage
    db.update_task(task_id, **fields)
    return checkpoint


def merge_task_checkpoint_state(task_id: int, **data: Any) -> dict[str, Any] | None:
    task = db.get_task(task_id)
    if not task:
        return None
    raw_checkpoint = task.get("checkpoint_json")
    checkpoint: dict[str, Any] = {}
    if isinstance(raw_checkpoint, str) and raw_checkpoint.strip():
        try:
            parsed = json.loads(raw_checkpoint or "{}")
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            checkpoint = parsed
    if not isinstance(checkpoint, dict):
        checkpoint = {}
    base_checkpoint = {
        "version": int(checkpoint.get("version") or 1),
        "mode": str(checkpoint.get("mode") or task.get("mode") or ""),
        "step": str(data.get("step") or checkpoint.get("step") or "runtime_update"),
        "can_resume": bool(data["can_resume"]) if "can_resume" in data else bool(checkpoint.get("can_resume")),
    }
    next_checkpoint = compact_checkpoint_payload(
        {
            **base_checkpoint,
            **checkpoint,
            "updated_at": db.now_iso(),
            **data,
        }
    )
    db.update_task(task_id, checkpoint_json=db.json_dumps(next_checkpoint))
    return next_checkpoint


def requeue_task_for_manual_retry(task: dict[str, Any], checkpoint: dict[str, Any]) -> dict[str, Any]:
    task_id = int(task["id"])
    can_resume = bool(checkpoint.get("can_resume"))
    if not can_resume and str(task.get("mode") or "") == "storyboard":
        storyboard = checkpoint.get("storyboard") if isinstance(checkpoint.get("storyboard"), dict) else {}
        shots = storyboard.get("shots") if isinstance(storyboard.get("shots"), list) else []
        can_resume = any(isinstance(shot, dict) and shot.get("status") == "done" for shot in shots)
    stage = "已加入重试队列，准备按当前进度继续" if can_resume else "已加入重试队列，准备重新执行当前任务"
    progress = min(max(int(task.get("progress") or 5), 5), 95)
    fields: dict[str, Any] = {
        "status": "scheduled",
        "progress": progress,
        "stage": stage,
        "error": None,
        "response_json": None,
        "cancel_requested": 0,
        "scheduled_for": None,
        "image_provider_id": None,
        "image_provider_name": None,
    }
    sanitized_checkpoint = strip_auto_retry_checkpoint(checkpoint)
    if sanitized_checkpoint:
        fields["checkpoint_json"] = db.json_dumps(
            compact_checkpoint_payload(
                {
                    **sanitized_checkpoint,
                    "updated_at": db.now_iso(),
                    "stage": stage,
                    "progress": progress,
                    "last_status": "scheduled",
                    "last_error": None,
                    "manual_retry_requested_at": db.now_iso(),
                }
            )
        )
    db.update_task(task_id, **fields)
    return task_with_images(task_id)


def clone_image_record_to_task(
    image: dict[str, Any],
    *,
    task_id: int | None,
    conversation_id: int | None,
    message_id: int | None,
) -> dict[str, Any] | None:
    file_path = Path(str(image.get("file_path") or ""))
    if not file_path.exists():
        return None
    image_id = db.add_image(
        source=str(image.get("source") or "api"),
        file_path=file_path,
        public_url=str(image.get("public_url") or image.get("url") or ""),
        thumb_path=str(image.get("thumb_path") or file_path),
        thumb_url=str(image.get("thumb_url") or image.get("public_url") or image.get("url") or ""),
        medium_path=str(image.get("medium_path") or file_path),
        medium_url=str(image.get("medium_url") or image.get("public_url") or image.get("url") or ""),
        width=int(image["width"]) if image.get("width") is not None else None,
        height=int(image["height"]) if image.get("height") is not None else None,
        byte_size=int(image["byte_size"]) if image.get("byte_size") is not None else None,
        mime_type=str(image.get("mime_type") or "image/png"),
        title=image.get("title"),
        bucket=image.get("bucket"),
        task_id=task_id,
        conversation_id=conversation_id,
        message_id=message_id,
    )
    copied = {
        **image,
        "id": image_id,
        "task_id": task_id,
        "conversation_id": conversation_id,
        "message_id": message_id,
    }
    return serialize_image_record(copied)


def restore_completed_output_images(
    old_task: dict[str, Any],
    *,
    new_task_id: int,
    conversation_id: int | None,
    message_id: int | None,
    completed_count: int,
) -> list[dict[str, Any]]:
    restored: list[dict[str, Any]] = []
    if completed_count <= 0:
        return restored
    source_images = [image for image in old_task.get("images", []) if image.get("source") == "api"]
    for image in source_images[:completed_count]:
        copied = clone_image_record_to_task(
            image,
            task_id=new_task_id,
            conversation_id=conversation_id,
            message_id=message_id,
        )
        if copied:
            copied["prompt_text"] = image.get("prompt_text")
            restored.append(copied)
    return restored


def update_message_meta(message_id: int, updates: dict[str, Any], response_id: str | None = None) -> None:
    with db.connect() as conn:
        row = conn.execute("select meta_json from messages where id = ?", (message_id,)).fetchone()
        if not row:
            return
        try:
            meta = json.loads(row["meta_json"] or "{}")
        except json.JSONDecodeError:
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        meta.update(updates)
        values: list[Any] = [db.json_dumps(meta), db.now_iso()]
        assignments = "meta_json = ?, updated_at = ?"
        if response_id is not None:
            assignments += ", response_id = ?"
            values.append(response_id)
        values.append(message_id)
        conn.execute(f"update messages set {assignments} where id = ?", values)


def update_message_content(message_id: int, content: str, response_id: str | None = None) -> None:
    with db.connect() as conn:
        values: list[Any] = [content, db.now_iso()]
        assignments = "content = ?, updated_at = ?"
        if response_id is not None:
            assignments += ", response_id = ?"
            values.append(response_id)
        values.append(message_id)
        conn.execute(f"update messages set {assignments} where id = ?", values)


def cancel_running_task(task_id: int) -> None:
    running = RUNNING_TASKS.get(task_runtime_key(task_id))
    if running:
        running.cancel()


def safe_delete_media_files(paths: list[str]) -> None:
    roots = [current_upload_dir().resolve(), current_output_dir().resolve()]
    for raw_path in dict.fromkeys(path for path in paths if path):
        path = Path(raw_path)
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if not any(resolved == root or root in resolved.parents for root in roots):
            continue
        try:
            if resolved.is_file():
                resolved.unlink()
        except OSError:
            pass


def deletable_media_paths(rows: list[Any], delete_ids: list[int]) -> list[str]:
    if not rows:
        return []
    ids = [int(value) for value in delete_ids]
    placeholders = ",".join("?" for _ in ids) if ids else "null"
    paths: list[str] = []
    with db.connect() as conn:
        for row in rows:
            for field in ("file_path", "thumb_path", "medium_path"):
                path = str(row[field] or "").strip() if field in row.keys() else ""
                if not path or path in paths:
                    continue
                if ids:
                    count = conn.execute(
                        f"""
                        select count(*) as count from images
                        where (file_path = ? or thumb_path = ? or medium_path = ?)
                          and id not in ({placeholders})
                        """,
                        [path, path, path, *ids],
                    ).fetchone()["count"]
                else:
                    count = conn.execute(
                        """
                        select count(*) as count from images
                        where file_path = ? or thumb_path = ? or medium_path = ?
                        """,
                        (path, path, path),
                    ).fetchone()["count"]
                if int(count) == 0:
                    paths.append(path)
    return paths


def image_prompts_from_message_meta(meta: dict[str, Any]) -> list[str]:
    prompts: list[str] = []
    image_status = str(meta.get("image_status") or "").strip().lower()
    if image_status == "done":
        image_prompt = str(meta.get("image_prompt") or "").strip()
        if image_prompt:
            prompts.append(image_prompt)
        plan = meta.get("plan") if isinstance(meta.get("plan"), dict) else {}
        plan_prompt = str(plan.get("image_prompt") or "").strip()
        if plan_prompt:
            prompts.append(plan_prompt)
    storyboard = meta.get("storyboard") if isinstance(meta.get("storyboard"), dict) else {}
    shots = storyboard.get("shots") if isinstance(storyboard.get("shots"), list) else []
    for shot in shots:
        if not isinstance(shot, dict):
            continue
        if str(shot.get("status") or "").strip().lower() != "done":
            continue
        name = str(shot.get("name") or "").strip()
        shot_prompt = str(shot.get("planner_prompt") or shot.get("prompt") or "").strip()
        if shot_prompt:
            prompts.append(f"{name}：{shot_prompt}" if name else shot_prompt)
    return list(dict.fromkeys(prompts))


def parse_message_meta(item: dict[str, Any]) -> dict[str, Any]:
    if isinstance(item.get("meta"), dict):
        return item["meta"]
    if isinstance(item.get("meta_json"), str):
        try:
            parsed = json.loads(item["meta_json"] or "{}")
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return {}
    return {}


def load_recent_messages(
    conversation_id: int,
    limit: int,
    *,
    exclude_message_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    clean_limit = max(int(limit or 0), 0)
    if clean_limit <= 0:
        return []
    excluded = [message_id for message_id in normalize_reference_image_ids(exclude_message_ids or []) if message_id > 0]
    where_clause = "where conversation_id = ?"
    query_values: list[Any] = [conversation_id]
    if excluded:
        placeholders = ",".join("?" for _ in excluded)
        where_clause += f" and id not in ({placeholders})"
        query_values.extend(excluded)
    query_values.append(clean_limit)
    with db.connect() as conn:
        rows = conn.execute(
            f"""
            select id, role, content, meta_json, created_at
            from messages
            {where_clause}
            order by id desc
            limit ?
            """,
            query_values,
        ).fetchall()
    return list(reversed([db.row_to_dict(row) for row in rows]))


def build_context_prompt(history: list[dict[str, Any]], prompt: str) -> str:
    if not history:
        return prompt
    lines = ["以下是最近的文字对话和已生成图片对应的单张图片生图提示词，请结合它们理解当前需求："]
    for item in history:
        meta = parse_message_meta(item)
        if item.get("role") == "assistant":
            planner_status = str(meta.get("planner_status") or "").strip().lower()
            if planner_status and planner_status != "done":
                continue
        role = "用户" if item.get("role") == "user" else "助手"
        content = str(item.get("content") or "").strip()
        if content:
            lines.append(f"{role}: {content}")
        for index, image_prompt in enumerate(image_prompts_from_message_meta(meta), start=1):
            lines.append(f"{role}关联的第 {index} 张图片生图提示词（仅对应一张图片）: {image_prompt}")
    lines.append(f"当前用户需求: {prompt}")
    return "\n".join(lines)


def build_chat_planner_prompt(
    history: list[dict[str, Any]],
    prompt: str,
    has_images: bool,
    image_candidates: list[dict[str, Any]] | None = None,
    attach_reference_images: bool = True,
    character_profiles: list[dict[str, Any]] | None = None,
    style_lock: dict[str, Any] | None = None,
) -> str:
    context = build_context_prompt(history, prompt)
    image_candidates = image_candidates or []
    consistency_note = planner_constraint_block(character_profiles, style_lock)
    if image_candidates:
        source_note = "用户本轮已经明确指定了参考图片。" if has_images else "用户本轮没有指定参考图片。"
        lines = [f"{source_note} 已指定参考图如下；如果你决定执行图片修改，应使用这些参考图，不要自行选择其它历史图片："]
        for index, image in enumerate(image_candidates, start=1):
            prompt_hint = image.get("hint") or ""
            if image.get("source") == "upload":
                prompt_part = "用户本轮上传的参考图，没有对应生图提示词"
            else:
                prompt_part = f"该参考图对应的一张图片生图提示词/说明={prompt_hint or '无'}"
            lines.append(
                f"- 候选{index}: ref={image['ref']}, source={image.get('source')}, "
                f"role={reference_role_label(str(image.get('role') or 'style'))}, "
                f"selection_mode={reference_selection_mode_label(str(image.get('selection_mode') or 'reference'))}, "
                f"image_id={image.get('id')}, message_id={image.get('message_id')}, "
                f"task_id={image.get('task_id')}, {prompt_part}"
            )
        if attach_reference_images:
            lines.append("候选顺序与随请求附带给你的参考图片顺序一致。")
        else:
            lines.append("当前 planner 使用 chat/completions 兼容模式，只提供参考图文字说明和已知生图提示词，不附带参考图片本体；不要声称你已经看到了图片内容。")
        image_note = "\n".join(lines)
    else:
        image_note = "本轮用户没有上传或选择参考图片。"
    return f"""
你是本项目“对话式生图 planner”。你只负责理解用户、追问需求、判断是否开始生图、撰写最终提示词；真正的图片生成由后续 image_generation 工具执行。

对话模式工作流：
1. 先读文字上下文和本轮用户输入，判断用户是在闲聊/补充需求，还是已经明确要生成或修改图片。
2. 若画面主体、场景、风格或修改目标仍不清楚，should_generate=false，只问最关键的 1-2 个问题，不要急着生图。
3. 若用户明确说“生成、开始、按这个来、继续改、重画、修改”等，且信息足够，should_generate=true。
4. 若是从零生成新图，action=generate，image_prompt 必须是一张图片的最终生图提示词，只描述这一张图的画面，不要写解释、流程、JSON、镜头列表或多张图信息。
5. 若是编辑参考图，action=edit；只能使用本轮用户上传或选择的参考图，禁止自行猜测其它历史图片。
6. 若用户想改图但没有提供参考图，或无法判断要改哪张参考图，should_generate=false，请用户上传或选择参考图。
7. 若使用参考图，reference_image_refs 必须填写本次实际用到的全部 ref；reference_image_ids 只填写已选历史生成图里实际用到的 image_id。用户本轮上传的参考图没有 image_id，也没有对应生图提示词，不要编造。
8. 若 action=edit，edit_target_image_refs 必须填写“被直接修改”的目标图 ref；若是历史生成图，同时在 edit_target_image_ids 里填写对应 image_id。若有辅助参考图但不是直接修改对象，它们只能出现在 reference_image_refs 里，不能混进 edit_target_image_refs。
9. image_prompt 必须把用户意图改写成适合 image_generation 的单张图片提示词，并明确保留不应变化的主体、构图、风格或参考图特征。
10. 若 should_generate=true，必须顺便给这张图片生成一个简短中文名称 image_name；名称要便于用户识别、适合直接作为图片名，禁止带文件扩展名。

{consistency_note}

{image_note}

请只输出 JSON，不要 Markdown，不要代码块。格式：
{{
  "reply": "给用户看的中文回复。若要生图，说明你将如何生成/修改；若不要生图，提出下一步问题或建议。",
  "should_generate": true 或 false,
  "action": "generate" 或 "edit" 或 "auto",
  "image_name": "should_generate 为 true 时填写这张图片的中文名称；否则为空字符串",
  "image_prompt": "should_generate 为 true 时填写一张图片的最终生图提示词；它只能对应一张图片，不能包含解释、流程、多图列表或其它信息；否则为空字符串",
  "reference_image_refs": [],
  "reference_image_ids": [],
  "edit_target_image_refs": [],
  "edit_target_image_ids": [],
  "reason": "简短说明判断依据"
}}

对话上下文和当前用户输入：
{context}
""".strip()


def parse_planner_json(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end >= start:
        raw = raw[start : end + 1]
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {
            "reply": text.strip() or "我还需要更多画面要求，再帮你生成会更稳。",
            "should_generate": False,
            "action": "auto",
            "image_name": "",
            "image_prompt": "",
            "reference_image_refs": [],
            "reference_image_ids": [],
            "edit_target_image_refs": [],
            "edit_target_image_ids": [],
            "reason": "planner returned non-json text",
        }
    reference_refs = parsed.get("reference_image_refs") or []
    if not isinstance(reference_refs, list):
        reference_refs = []
    reference_ids = parsed.get("reference_image_ids") or []
    if not isinstance(reference_ids, list):
        reference_ids = []
    edit_target_refs = parsed.get("edit_target_image_refs") or []
    if not isinstance(edit_target_refs, list):
        edit_target_refs = []
    edit_target_ids = parsed.get("edit_target_image_ids") or []
    if not isinstance(edit_target_ids, list):
        edit_target_ids = []
    return {
        "reply": fix_mojibake(str(parsed.get("reply") or "").strip()) or "我理解了。",
        "should_generate": bool(parsed.get("should_generate")),
        "action": str(parsed.get("action") or "auto").strip(),
        "image_name": normalize_image_title(parsed.get("image_name") or ""),
        "image_prompt": fix_mojibake(str(parsed.get("image_prompt") or "").strip()),
        "reference_image_refs": [str(value).strip() for value in reference_refs if str(value).strip()],
        "reference_image_ids": [int(value) for value in reference_ids if str(value).isdigit()],
        "edit_target_image_refs": [str(value).strip() for value in edit_target_refs if str(value).strip()],
        "edit_target_image_ids": [int(value) for value in edit_target_ids if str(value).isdigit()],
        "reason": fix_mojibake(str(parsed.get("reason") or "").strip()),
    }


def build_storyboard_planner_prompt(
    history: list[dict[str, Any]],
    prompt: str,
    image_candidates: list[dict[str, Any]] | None,
    shot_limit: int,
    attach_reference_images: bool = True,
    character_profiles: list[dict[str, Any]] | None = None,
    style_lock: dict[str, Any] | None = None,
) -> str:
    context = build_context_prompt(history, prompt)
    image_candidates = image_candidates or []
    consistency_note = planner_constraint_block(character_profiles, style_lock)
    if image_candidates:
        lines = ["用户本轮已经明确指定了以下角色/场景参考图；第一镜头必须优先使用这些锚点规划。"]
        for index, image in enumerate(image_candidates, start=1):
            lines.append(
                f"- 候选{index}: ref={image['ref']}, source={image.get('source')}, "
                f"role={reference_role_label(str(image.get('role') or 'style'))}, "
                f"selection_mode={reference_selection_mode_label(str(image.get('selection_mode') or 'reference'))}, "
                f"image_id={image.get('id')}, task_id={image.get('task_id')}, "
                f"说明={reference_candidate_hint(image)}"
            )
        if attach_reference_images:
            lines.append("候选顺序与随请求附带给你的参考图片顺序一致。")
        else:
            lines.append("当前 planner 使用 chat/completions 兼容模式，只提供参考图文字说明和已知生图提示词，不附带图片本体；不要声称你已经直接看到了图片内容。")
        image_note = "\n".join(lines)
    else:
        image_note = "用户本轮没有提供参考图，第一镜头可从文本生成开始。"
    return f"""
你是本项目“分镜连续生图 planner”。你只负责和用户完善视频意图、规划镜头、为每个镜头撰写单张首帧图片提示词；真正的逐张生图由后续 image_generation 工具按顺序执行。

分镜模式工作流：
1. 先判断用户是在讨论创意，还是已经准备开始生成连续镜头首帧。
2. 如果主题、主角、场景、风格、镜头数量或连续动作仍不足以稳定规划，should_generate=false，只提出最关键的补充问题。
3. 如果用户明确要求开始、生成、按这个方案做，或上下文已经足够形成镜头序列，should_generate=true。
4. 必须先给出 character_summary 和 scene_summary，作为所有镜头的人物、场景、光线、风格不变量。
5. 每个 shots[i].prompt 都必须是一张图片的生图提示词，只对应该镜头的首帧图片；禁止把多个镜头、解释文字、流程说明或文件保存说明写进同一个 prompt。
6. 每个镜头只生成一张图，代表该镜头最开始的一帧画面；不要合图、拼图、多格漫画或一次描述多张图。
7. 镜头必须连续：第 N 镜头的首帧要承接第 N-1 镜头画面，保持人物、服装、道具、空间位置、光线逻辑和故事动作一致。
8. 第 1 镜头可基于文本生成，也可在用户显式指定的参考图上直接修改；若第 1 镜头要直接修改参考图，必须在该镜头里明确写 action=edit，并给出 edit_target_image_refs。
9. 第 2 个及后续镜头必须使用 action=edit，并默认以上一镜头输出画面作为直接编辑目标；若还要补充用户显式参考图作为辅助锚点，可在该镜头里填写 reference_image_refs。
10. 你需要为每个镜头生成中文名字，名字要短、能作为文件名，必须包含镜头顺序含义，但不要包含文件扩展名。
11. 最多输出 {shot_limit} 个镜头；如果用户没有指定数量，优先 3-5 个镜头。

{consistency_note}

{image_note}

请只输出 JSON，不要 Markdown，不要代码块。格式：
{{
  "reply": "给用户看的中文回复，说明是否还需要补充，或说明将按哪些镜头生成。",
  "should_generate": true 或 false,
  "character_summary": "人物外观、服装、身份、关键不变量的中文概述；不足则为空",
  "scene_summary": "场景、时代、光线、色彩、镜头风格的中文概述；不足则为空",
  "shots": [
    {{
      "order": 1,
      "name": "01-中文镜头名",
      "action": "generate 或 edit；第 2 个及后续镜头必须是 edit",
      "prompt": "这一镜头的一张首帧图片生图提示词，包含人物/场景/构图/动作/连续性/禁止变化项；只能对应这一张图片",
      "continuity": "这一镜头与上一镜头的衔接关系；第一镜头说明开场状态",
      "reference_image_refs": [],
      "reference_image_ids": [],
      "edit_target_image_refs": [],
      "edit_target_image_ids": []
    }}
  ],
  "reason": "简短说明判断依据"
}}

对话上下文和当前用户输入：
{context}
""".strip()


def parse_storyboard_plan(text: str, shot_limit: int) -> dict[str, Any]:
    base = parse_planner_json(text)
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
    start = raw.find("{")
    end = raw.rfind("}")
    parsed: dict[str, Any] = {}
    if start >= 0 and end >= start:
        try:
            value = json.loads(raw[start : end + 1])
            if isinstance(value, dict):
                parsed = value
        except json.JSONDecodeError:
            parsed = {}
    if not parsed:
        return {
            "reply": base["reply"],
            "should_generate": False,
            "character_summary": "",
            "scene_summary": "",
            "shots": [],
            "reason": base["reason"] or "storyboard planner returned non-json text",
        }
    shots: list[dict[str, Any]] = []
    raw_shots = parsed.get("shots") if isinstance(parsed.get("shots"), list) else []
    for index, item in enumerate(raw_shots[: max(1, min(int(shot_limit), MAX_STORYBOARD_SHOTS))], start=1):
        if not isinstance(item, dict):
            continue
        name = fix_mojibake(str(item.get("name") or f"{index:02d}-镜头{index}").strip())
        prompt = fix_mojibake(str(item.get("prompt") or "").strip())
        if not prompt:
            continue
        reference_refs = item.get("reference_image_refs") if isinstance(item.get("reference_image_refs"), list) else []
        reference_ids = item.get("reference_image_ids") if isinstance(item.get("reference_image_ids"), list) else []
        edit_target_refs = item.get("edit_target_image_refs") if isinstance(item.get("edit_target_image_refs"), list) else []
        edit_target_ids = item.get("edit_target_image_ids") if isinstance(item.get("edit_target_image_ids"), list) else []
        action = str(item.get("action") or ("generate" if index == 1 else "edit")).strip().lower()
        if action not in {"generate", "edit"}:
            action = "generate" if index == 1 else "edit"
        if index > 1:
            action = "edit"
        shots.append(
            {
                "order": int(item.get("order") or index),
                "name": normalize_shot_name(name, index),
                "action": action,
                "prompt": prompt,
                "planner_prompt": prompt,
                "execution_prompt": "",
                "continuity": fix_mojibake(str(item.get("continuity") or "").strip()),
                "reference_image_refs": [str(value).strip() for value in reference_refs if str(value).strip()],
                "reference_image_ids": [int(value) for value in reference_ids if str(value).isdigit()],
                "edit_target_image_refs": [str(value).strip() for value in edit_target_refs if str(value).strip()],
                "edit_target_image_ids": [int(value) for value in edit_target_ids if str(value).isdigit()],
                "status": "pending",
            }
        )
    should_generate = bool(parsed.get("should_generate")) and bool(shots)
    return {
        "reply": fix_mojibake(str(parsed.get("reply") or "").strip()) or ("我会按分镜顺序生成连续画面。" if should_generate else base["reply"]),
        "should_generate": should_generate,
        "character_summary": fix_mojibake(str(parsed.get("character_summary") or "").strip()),
        "scene_summary": fix_mojibake(str(parsed.get("scene_summary") or "").strip()),
        "shots": shots,
        "reason": fix_mojibake(str(parsed.get("reason") or "").strip()),
    }


def normalize_shot_name(name: str, order: int) -> str:
    clean = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "-", fix_mojibake(name).strip()).strip("-")
    if not clean:
        clean = f"镜头{order}"
    if not re.match(r"^\d{2}[-_]", clean):
        clean = f"{order:02d}-{clean}"
    return clean[:48]


def rename_output_image(item: tuple[Path, str, str], name: str, fallback_stem: str = "image") -> tuple[Path, str, str]:
    path, _public_url, mime_type = item
    stem = normalize_filename_stem(name, fallback=fallback_stem)
    suffix = path.suffix or ".png"
    target = path.with_name(f"{stem}{suffix}")
    counter = 2
    while target.exists() and target != path:
        target = path.with_name(f"{stem}-{counter}{suffix}")
        counter += 1
    if target != path:
        path.rename(target)
    public_url = "/media/outputs/" + target.relative_to(current_output_dir()).as_posix()
    return target, public_url, mime_type


def update_storyboard_task_state(task_id: int, payload: dict[str, Any], state: dict[str, Any]) -> None:
    payload["storyboard"] = state
    db.update_task(task_id, params_json=db.json_dumps(payload))


def publish_storyboard_image_saved(
    task_id: int,
    *,
    conversation_id: int | None,
    message_id: int | None,
    image: dict[str, Any],
    shot: dict[str, Any],
    index: int,
    total: int,
) -> None:
    publish_task_snapshot(task_id)
    publish_task_event(
        task_id,
        "storyboard_image",
        {
            "task_id": task_id,
            "conversation_id": conversation_id,
            "message_id": message_id,
            "image": image,
            "shot": shot,
            "index": index,
            "total": total,
        },
        snapshot=False,
    )


def build_uploaded_image_candidates(
    uploaded: list[tuple[Path, str]],
    upload_roles: list[str] | None = None,
    upload_selection_modes: list[str] | None = None,
    *,
    start_order: int = 1,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for index, (path, mime_type) in enumerate(uploaded, start=1):
        ordinal = start_order + index - 1
        role = normalize_reference_role(upload_roles[index - 1] if upload_roles and index - 1 < len(upload_roles) else None, ordinal)
        selection_mode = normalize_reference_selection_mode(
            upload_selection_modes[index - 1] if upload_selection_modes and index - 1 < len(upload_selection_modes) else None,
            default="reference",
        )
        candidates.append(
            {
                "ref": f"upload:{index}",
                "source": "upload",
                "id": None,
                "message_id": None,
                "task_id": None,
                "path": path,
                "mime_type": mime_type,
                "role": role,
                "role_label": reference_role_label(role),
                "selection_mode": selection_mode,
                "selection_mode_label": reference_selection_mode_label(selection_mode),
                "hint": f"本轮用户上传的第 {index} 张图片，没有历史生图提示词",
            }
        )
    return candidates


def build_selected_image_candidates(
    selected: list[dict[str, Any]],
    reference_roles: dict[str, str] | None = None,
    selection_modes: dict[str, str] | None = None,
    *,
    start_order: int = 1,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for index, item in enumerate(selected, start=1):
        path = Path(item["file_path"])
        if not path.exists():
            continue
        ordinal = start_order + index - 1
        role = normalize_reference_role((reference_roles or {}).get(str(item["id"])), ordinal)
        selection_mode = normalize_reference_selection_mode(
            (selection_modes or {}).get(str(item["id"])),
            default="reference",
        )
        candidates.append(
            {
                "ref": f"image:{item['id']}",
                "source": "selected",
                "id": item["id"],
                "message_id": item.get("message_id"),
                "task_id": item.get("task_id"),
                "path": path,
                "mime_type": item.get("mime_type") or "image/png",
                "role": role,
                "role_label": reference_role_label(role),
                "selection_mode": selection_mode,
                "selection_mode_label": reference_selection_mode_label(selection_mode),
                "hint": item.get("task_prompt") or item.get("message_content") or item.get("title") or "用户指定的历史图片",
            }
        )
    return candidates


def load_selected_reference_images(image_ids: list[int], limit: int = 3, conversation_id: int | None = None) -> list[dict[str, Any]]:
    clean_ids = normalize_reference_image_ids(image_ids)[: max(int(limit or 0), 0)]
    if not clean_ids:
        return []
    placeholders = ",".join("?" for _ in clean_ids)
    conversation_clause = "and i.conversation_id = ?" if conversation_id is not None else ""
    query_values: list[Any] = [*clean_ids]
    if conversation_id is not None:
        query_values.append(conversation_id)
    with db.connect() as conn:
        rows = conn.execute(
            f"""
            select i.*,
                   m.content as message_content,
                   t.prompt as task_prompt,
                   t.mode as task_mode
            from images i
            left join messages m on m.id = i.message_id
            left join tasks t on t.id = i.task_id
            where i.id in ({placeholders}) and i.source = 'api' {conversation_clause}
            """,
            query_values,
        ).fetchall()
    by_id = {int(row["id"]): db.row_to_dict(row) for row in rows}
    return [by_id[image_id] for image_id in clean_ids if image_id in by_id and Path(by_id[image_id]["file_path"]).exists()]


def require_selected_reference_images(
    image_ids: list[int] | Any,
    *,
    limit: int,
    conversation_id: int | None,
    label: str,
) -> list[dict[str, Any]]:
    clean_ids = normalize_reference_image_ids(image_ids)
    max_count = max(int(limit or 0), 0)
    if len(clean_ids) > max_count:
        message = f"{label}最多只能选择 {max_count} 张，请先移除多余参考图后再继续。"
        if max_count <= 0:
            message = f"{label}已达到上限，请先移除一张或减少上传图片后再继续。"
        raise HTTPException(
            status_code=400,
            detail={
                "message": message,
                "requested_image_ids": clean_ids,
                "limit": max_count,
            },
        )
    selected = load_selected_reference_images(clean_ids, limit=max_count, conversation_id=conversation_id)
    loaded_ids = {int(item["id"]) for item in selected if item.get("id") is not None}
    missing_ids = [image_id for image_id in clean_ids if image_id not in loaded_ids]
    if missing_ids:
        raise HTTPException(
            status_code=400,
            detail={
                "message": f"{label}里有图片已不存在、不属于当前会话，或原文件已丢失，无法继续保证上下文完整性。",
                "missing_image_ids": missing_ids,
                "requested_image_ids": clean_ids,
            },
        )
    return selected


def load_conversation_image_candidates(conversation_id: int, limit: int = 8) -> list[dict[str, Any]]:
    with db.connect() as conn:
        rows = conn.execute(
            """
            select i.id, i.file_path, i.mime_type, i.message_id, i.task_id, i.title, i.created_at,
                   m.content as message_content,
                   t.prompt as task_prompt,
                   t.mode as task_mode
            from images i
            left join messages m on m.id = i.message_id
            left join tasks t on t.id = i.task_id
            where i.conversation_id = ? and i.source = 'api'
            order by i.id desc
            limit ?
            """,
            (conversation_id, max(1, min(limit, MAX_STORYBOARD_SHOTS))),
        ).fetchall()
    candidates: list[dict[str, Any]] = []
    for row in rows:
        path = Path(row["file_path"])
        if not path.exists():
            continue
        item = db.row_to_dict(row)
        hint = item.get("task_prompt") or item.get("message_content") or item.get("title") or ""
        item["ref"] = f"image:{item['id']}"
        item["source"] = "history"
        item["path"] = path
        item["hint"] = str(hint)[:220]
        candidates.append(item)
    return candidates


def selected_candidate_uploads(
    candidates: list[dict[str, Any]],
    reference_ids: list[int],
    reference_refs: list[str],
) -> list[tuple[Path, str]]:
    return [
        (item["path"], item.get("mime_type") or "image/png")
        for item in resolve_selected_candidates(candidates, reference_ids, reference_refs)
    ]


def candidate_identity(candidate: dict[str, Any]) -> str:
    item_id = candidate.get("id")
    if item_id is not None:
        try:
            return f"id:{int(item_id)}"
        except (TypeError, ValueError):
            pass
    return f"ref:{str(candidate.get('ref') or '')}"


def unique_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in candidates:
        key = candidate_identity(item)
        if key in seen:
            continue
        selected.append(item)
        seen.add(key)
    return selected


def resolve_selected_candidates(
    candidates: list[dict[str, Any]],
    reference_ids: list[int] | None = None,
    reference_refs: list[str] | None = None,
    *,
    fallback_to_all: bool = False,
) -> list[dict[str, Any]]:
    wanted: set[int] = set()
    wanted_refs = {str(value).strip() for value in reference_refs or [] if str(value).strip()}
    for value in reference_ids or []:
        try:
            image_id = int(value)
        except (TypeError, ValueError):
            continue
        if image_id > 0:
            wanted.add(image_id)
    if not wanted and not wanted_refs:
        return unique_candidates(candidates) if fallback_to_all else []
    selected: list[dict[str, Any]] = []
    for item in candidates:
        item_ref = str(item.get("ref") or "")
        matched = item_ref in wanted_refs
        if not matched and item.get("id") is not None:
            try:
                matched = int(item["id"]) in wanted
            except (TypeError, ValueError):
                matched = False
        if matched:
            selected.append(item)
    return unique_candidates(selected)


def merge_candidate_lists(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for group in groups:
        merged.extend(group)
    return unique_candidates(merged)


def explicit_edit_target_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        item
        for item in unique_candidates(candidates)
        if normalize_reference_selection_mode(item.get("selection_mode"), default="reference") == "edit_target"
    ]


def candidate_refs(candidates: list[dict[str, Any]]) -> list[str]:
    refs: list[str] = []
    for item in candidates:
        ref = str(item.get("ref") or "").strip()
        if ref:
            refs.append(ref)
    return refs


def candidate_ids(candidates: list[dict[str, Any]]) -> list[int]:
    values: list[int] = []
    for item in candidates:
        item_id = item.get("id")
        try:
            image_id = int(item_id)
        except (TypeError, ValueError):
            continue
        if image_id > 0:
            values.append(image_id)
    return values


def build_candidate_input_bundle(
    primary_candidates: list[dict[str, Any]],
    reference_candidates: list[dict[str, Any]],
) -> tuple[list[tuple[Path, str]], list[str]]:
    uploads: list[tuple[Path, str]] = []
    notes: list[str] = []
    seen_paths: set[str] = set()
    for usage, items in (("edit_target", primary_candidates), ("reference", reference_candidates)):
        for candidate in items:
            path = candidate.get("path")
            if not isinstance(path, Path):
                continue
            resolved = str(path.resolve())
            if resolved in seen_paths:
                continue
            uploads.append((path, candidate.get("mime_type") or "image/png"))
            notes.append(build_reference_input_note(candidate, len(uploads), usage=usage))
            seen_paths.add(resolved)
    return uploads, notes


def build_edit_input_bundle(
    uploaded: list[tuple[Path, str]],
    upload_selection_modes: list[str] | None = None,
) -> tuple[list[tuple[Path, str]], list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    selection_modes = normalize_edit_upload_selection_modes(upload_selection_modes, len(uploaded))
    candidates = build_uploaded_image_candidates(uploaded, upload_selection_modes=selection_modes)
    target_candidates = explicit_edit_target_candidates(candidates)
    reference_candidates = [
        item
        for item in candidates
        if candidate_identity(item) not in {candidate_identity(target) for target in target_candidates}
    ]
    edit_inputs, input_image_notes = build_candidate_input_bundle(target_candidates, reference_candidates)
    return edit_inputs, input_image_notes, reference_candidates, target_candidates


def storyboard_anchor_candidates(candidates: list[dict[str, Any]], limit: int = 3) -> list[dict[str, Any]]:
    anchors = unique_candidates([item for item in candidates if isinstance(item.get("path"), Path)])
    return anchors[: max(0, limit)]


def build_storyboard_generation_inputs(
    previous_image: tuple[Path, str] | None,
    seed_candidates: list[dict[str, Any]],
    *,
    action: str,
    edit_target_candidates: list[dict[str, Any]] | None = None,
) -> tuple[list[tuple[Path, str]], list[str]]:
    uploads: list[tuple[Path, str]] = []
    notes: list[str] = []
    seen_paths: set[str] = set()
    seed_candidates = unique_candidates(seed_candidates)
    edit_target_candidates = unique_candidates(edit_target_candidates or [])
    if previous_image is not None:
        previous_path, previous_mime = previous_image
        uploads.append((previous_path, previous_mime))
        notes.append(
            "Input image 1: 上一镜头输出画面。必须把它作为连续编辑基底，优先保留人物身份、构图关系、空间方位、光线方向和镜头语义连续性。"
        )
        seen_paths.add(str(previous_path.resolve()))
    elif action == "edit":
        for candidate in edit_target_candidates:
            path = candidate.get("path")
            if not isinstance(path, Path):
                continue
            resolved = str(path.resolve())
            if resolved in seen_paths:
                continue
            uploads.append((path, candidate.get("mime_type") or "image/png"))
            notes.append(build_reference_input_note(candidate, len(uploads), usage="edit_target"))
            seen_paths.add(resolved)
    for candidate in storyboard_anchor_candidates(seed_candidates, limit=max(1, len(seed_candidates) or 1)):
        path = candidate.get("path")
        if not isinstance(path, Path):
            continue
        resolved = str(path.resolve())
        if resolved in seen_paths:
            continue
        uploads.append((path, candidate.get("mime_type") or "image/png"))
        notes.append(build_reference_input_note(candidate, len(uploads)))
        seen_paths.add(resolved)
    return uploads, notes


def resolve_storyboard_shot_inputs(
    previous_image: tuple[Path, str] | None,
    seed_candidates: list[dict[str, Any]],
    shot: dict[str, Any],
    *,
    index: int,
) -> tuple[str, list[tuple[Path, str]], list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    action = str(shot.get("action") or ("generate" if previous_image is None and index == 1 else "edit")).strip().lower()
    if action not in {"generate", "edit"}:
        action = "generate" if previous_image is None and index == 1 else "edit"
    if previous_image is not None or index > 1:
        action = "edit"
    requested_reference_candidates = resolve_selected_candidates(
        seed_candidates,
        shot.get("reference_image_ids") if isinstance(shot.get("reference_image_ids"), list) else [],
        shot.get("reference_image_refs") if isinstance(shot.get("reference_image_refs"), list) else [],
        fallback_to_all=bool(seed_candidates),
    )
    user_target_candidates = explicit_edit_target_candidates(seed_candidates)
    target_candidates = resolve_selected_candidates(
        seed_candidates,
        shot.get("edit_target_image_ids") if isinstance(shot.get("edit_target_image_ids"), list) else [],
        shot.get("edit_target_image_refs") if isinstance(shot.get("edit_target_image_refs"), list) else [],
    )
    if not target_candidates and previous_image is None and action == "edit" and user_target_candidates:
        target_candidates = list(user_target_candidates)
    if not target_candidates and previous_image is None and user_target_candidates:
        target_candidates = list(user_target_candidates)
    if previous_image is None and target_candidates:
        action = "edit"
    if previous_image is None and index == 1 and action == "edit" and not target_candidates:
        if len(requested_reference_candidates) == 1:
            target_candidates = list(requested_reference_candidates)
        elif not requested_reference_candidates and len(seed_candidates) == 1:
            target_candidates = list(seed_candidates)
        else:
            raise HTTPException(
                status_code=400,
                detail={
                    "message": f"镜头 {index} 需要直接修改参考图，但当前无法唯一确定要改哪一张。",
                    "suggestion": "请为该镜头明确指定 edit_target_image_refs，或只保留一张直接修改目标图。",
                },
            )
    merged_references = merge_candidate_lists(
        target_candidates if previous_image is None and action == "edit" else [],
        requested_reference_candidates,
    )
    target_keys = {candidate_identity(item) for item in target_candidates}
    auxiliary_reference_candidates = [
        item
        for item in merged_references
        if candidate_identity(item) not in target_keys
    ]
    uploads, notes = build_storyboard_generation_inputs(
        previous_image,
        auxiliary_reference_candidates if action == "edit" else merged_references,
        action=action,
        edit_target_candidates=target_candidates,
    )
    return action, uploads, notes, merged_references, target_candidates


def load_seed_images_from_payload(
    payload: dict[str, Any],
    *,
    strict: bool = False,
    label: str = "参考图快照",
) -> list[dict[str, Any]]:
    raw_items = payload.get("seed_images") if isinstance(payload.get("seed_images"), list) else []
    candidates: list[dict[str, Any]] = []
    missing_refs: list[str] = []
    for index, item in enumerate(raw_items, start=1):
        if not isinstance(item, dict):
            continue
        path_value = str(item.get("file_path") or "").strip()
        if not path_value:
            if strict:
                missing_refs.append(str(item.get("ref") or f"seed:{index}"))
            continue
        path = Path(path_value)
        if not path.exists():
            if strict:
                missing_refs.append(str(item.get("ref") or f"seed:{index}"))
            continue
        role = normalize_reference_role(item.get("role"), index)
        selection_mode = normalize_reference_selection_mode(item.get("selection_mode"), default="reference")
        candidates.append(
            {
                "ref": item.get("ref") or f"seed:{index}",
                "source": item.get("source") or "seed",
                "id": item.get("id"),
                "message_id": item.get("message_id"),
                "task_id": item.get("task_id"),
                "path": path,
                "file_path": str(path),
                "mime_type": item.get("mime_type") or "image/png",
                "hint": item.get("hint") or "",
                "role": role,
                "role_label": reference_role_label(role),
                "selection_mode": selection_mode,
                "selection_mode_label": reference_selection_mode_label(selection_mode),
            }
        )
    if strict and missing_refs:
        raise HTTPException(
            status_code=400,
            detail={
                "message": f"{label}缺失，当前无法保证把原始背景上下文完整传给模型。",
                "missing_refs": missing_refs,
            },
        )
    return candidates


def load_seed_images_from_task_images(images: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seed_rows = [image for image in images if image.get("source") in {"input", "input_reference"}]
    for index, item in enumerate(seed_rows, start=1):
        path_value = str(item.get("file_path") or "").strip()
        if not path_value:
            continue
        path = Path(path_value)
        if not path.exists():
            continue
        role = normalize_reference_role(None, index)
        candidates.append(
            {
                "ref": f"task-image:{item.get('id') or index}",
                "source": item.get("source") or "seed",
                "id": item.get("id"),
                "message_id": item.get("message_id"),
                "task_id": item.get("task_id"),
                "path": path,
                "file_path": str(path),
                "mime_type": item.get("mime_type") or "image/png",
                "hint": item.get("title") or "",
                "role": role,
                "role_label": reference_role_label(role),
            }
        )
    return candidates


def recover_task_image_candidates(
    task: dict[str, Any],
    params: dict[str, Any],
    *,
    conversation_id: int | None,
    label: str,
) -> list[dict[str, Any]]:
    snapshot_candidates = load_seed_images_from_payload(params, strict=True, label=label)
    if snapshot_candidates:
        return snapshot_candidates
    uploaded = [
        (Path(str(image.get("file_path") or "")), str(image.get("mime_type") or "image/png"))
        for image in task.get("images", [])
        if image.get("source") == "input" and Path(str(image.get("file_path") or "")).exists()
    ]
    selected_reference_images = require_selected_reference_images(
        params.get("reference_image_ids") or [],
        limit=max(0, 3 - len(uploaded)),
        conversation_id=conversation_id,
        label=label,
    )
    return [
        *build_uploaded_image_candidates(uploaded, params.get("upload_reference_roles"), params.get("upload_selection_modes")),
        *build_selected_image_candidates(
            selected_reference_images,
            params.get("reference_image_roles"),
            params.get("reference_image_selection_modes"),
            start_order=len(uploaded) + 1,
        ),
    ]


def build_image_generation_tool(
    *,
    image_model: str,
    size: str,
    quality: str,
    output_format: str,
    background: str | None = None,
    output_compression: int | None = None,
    moderation: str | None = None,
    action: str | None = None,
    partial_images: int | None = None,
) -> dict[str, Any]:
    partial_value = None
    if partial_images is not None:
        partial_value = int(partial_images)
        if partial_value == 0:
            partial_value = None
    tool = compact_params(
        {
            "type": "image_generation",
            "model": image_model,
            "size": size,
            "quality": quality,
            "output_format": output_format,
            "background": background,
            "output_compression": output_compression,
            "moderation": moderation,
            "partial_images": partial_value,
        }
    )
    if action and action != "auto":
        tool["action"] = action
    return tool


def is_gateway_timeout_error(exc: HTTPException) -> bool:
    detail = exc.detail
    status_code = exc.status_code
    text = json.dumps(detail, ensure_ascii=False).lower() if not isinstance(detail, str) else detail.lower()
    return status_code in {520, 522, 524} or "timeout" in text or "timed out" in text or "超时" in text


def stable_retry_quality(current: str) -> str | None:
    fallback = IMAGE_STABLE_RETRY_QUALITY
    if not fallback or fallback == current:
        return None
    if current in {"high", "auto"}:
        return fallback
    return None


def build_responses_input(
    *,
    prompt: str,
    uploaded: list[tuple[Path, str]] | None = None,
    mask: tuple[Path, str] | None = None,
    input_fidelity: str | None = None,
    input_image_notes: list[str] | None = None,
) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
    has_edit_inputs = bool(uploaded or mask)
    if has_edit_inputs and input_fidelity and input_fidelity != "auto":
        if input_fidelity == "high":
            content.append(
                {
                    "type": "input_text",
                    "text": "Edit fidelity: high. Preserve the source image layout, identity, proportions, geometry, and non-targeted regions as strictly as possible.",
                }
            )
        else:
            content.append(
                {
                    "type": "input_text",
                    "text": "Edit fidelity: low. Preserve the source image broadly, but allow moderate stylistic reinterpretation where needed.",
                }
            )
    for idx, (path, mime_type) in enumerate(uploaded or [], start=1):
        note = input_image_notes[idx - 1] if input_image_notes and idx - 1 < len(input_image_notes) else None
        content.append(
            {
                "type": "input_text",
                "text": note or f"Input image {idx}: primary reference image. Preserve its identity/layout unless the prompt explicitly changes it.",
            }
        )
        content.append({"type": "input_image", "image_url": data_url_for_file(path, mime_type)})
    if mask is not None:
        path, mime_type = mask
        content.append(
            {
                "type": "input_text",
                "text": "Mask image: treat the following input image as the edit mask reference. Change only the masked/indicated region and preserve everything else.",
            }
        )
        content.append({"type": "input_image", "image_url": data_url_for_file(path, mime_type)})
    return [{"role": "user", "content": content}]


async def call_responses_image_generation(
    *,
    model: str,
    prompt: str,
    image_model: str,
    size: str,
    quality: str,
    output_format: str,
    background: str | None,
    output_compression: int | None,
    moderation: str | None,
    action: str | None,
    partial_images: int | None,
    config: ClientConfig,
    uploaded: list[tuple[Path, str]] | None = None,
    mask: tuple[Path, str] | None = None,
    input_fidelity: str | None = None,
    input_image_notes: list[str] | None = None,
    previous_response_id: str | None = None,
    on_stable_retry: Callable[[str], None] | None = None,
    on_stream_event: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    def build_payload(tool_quality: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "input": build_responses_input(
                prompt=prompt,
                uploaded=uploaded,
                mask=mask,
                input_fidelity=input_fidelity,
                input_image_notes=input_image_notes,
            ),
            "tools": [
                build_image_generation_tool(
                    image_model=image_model,
                    size=size,
                    quality=tool_quality,
                    output_format=output_format,
                    background=background,
                    output_compression=output_compression,
                    moderation=moderation,
                    action=action,
                    partial_images=partial_images,
                )
            ],
            "tool_choice": {"type": "image_generation"},
        }
        if previous_response_id:
            payload["previous_response_id"] = previous_response_id
        return payload

    payload = build_payload(quality)
    try:
        if on_stream_event is not None:
            try:
                return await post_json_stream(
                    "responses",
                    payload,
                    base_url=config.base_url,
                    api_key=config.api_key,
                    timeout=IMAGE_REQUEST_TIMEOUT_SECONDS,
                    max_attempts=IMAGE_REQUEST_MAX_ATTEMPTS,
                    on_event=on_stream_event,
                )
            except HTTPException as exc:
                if exc.status_code not in {400, 404, 405}:
                    raise
        return await post_json(
            "responses",
            payload,
            base_url=config.base_url,
            api_key=config.api_key,
            timeout=IMAGE_REQUEST_TIMEOUT_SECONDS,
            max_attempts=IMAGE_REQUEST_MAX_ATTEMPTS,
        )
    except HTTPException as exc:
        fallback_quality = stable_retry_quality(quality)
        if not ENABLE_IMAGE_STABLE_RETRY or not fallback_quality or not is_gateway_timeout_error(exc):
            raise
        if on_stable_retry:
            on_stable_retry(fallback_quality)
        stable_payload = build_payload(fallback_quality)
        if on_stream_event is not None:
            try:
                return await post_json_stream(
                    "responses",
                    stable_payload,
                    base_url=config.base_url,
                    api_key=config.api_key,
                    timeout=IMAGE_REQUEST_TIMEOUT_SECONDS,
                    max_attempts=IMAGE_REQUEST_MAX_ATTEMPTS,
                    on_event=on_stream_event,
                )
            except HTTPException as stream_exc:
                if stream_exc.status_code not in {400, 404, 405}:
                    raise
        return await post_json(
            "responses",
            stable_payload,
            base_url=config.base_url,
            api_key=config.api_key,
            timeout=IMAGE_REQUEST_TIMEOUT_SECONDS,
            max_attempts=1,
        )


def update_timeout_retry_stage(task_id: int, quality: str) -> None:
    update_task_stage(
        task_id,
        f"上游网关超时，已自动切换到{quality}清晰度稳定重试",
        progress=52,
    )


def handle_image_stream_event(task_id: int, event: dict[str, Any]) -> None:
    event_type = str(event.get("type") or "")
    if event_type.endswith(".in_progress") or event_type == "response.in_progress":
        update_task_stage(task_id, "上游已开始处理图像请求", progress=45)
    elif event_type == "response.image_generation_call.partial_image":
        index = event.get("partial_image_index")
        label = f"上游返回局部预览 {index}" if index is not None else "上游返回局部预览"
        update_task_stage(task_id, label, progress=68)
    elif event_type == "response.output_item.done":
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        if item.get("type") == "image_generation_call":
            update_task_stage(task_id, "上游已返回最终图片", progress=88)
    elif event_type == "response.completed":
        update_task_stage(task_id, "上游响应完成，正在保存图片", progress=92)


def handle_storyboard_stream_event(task_id: int, shot_index: int, total: int, shot_name: str, event: dict[str, Any]) -> None:
    event_type = str(event.get("type") or "")
    prefix = f"镜头 {shot_index}/{total}：{shot_name}"
    if event_type.endswith(".in_progress") or event_type == "response.in_progress":
        update_task_stage(task_id, f"{prefix} 上游已开始处理")
    elif event_type == "response.image_generation_call.partial_image":
        update_task_stage(task_id, f"{prefix} 上游返回局部预览")
    elif event_type == "response.output_item.done":
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        if item.get("type") == "image_generation_call":
            update_task_stage(task_id, f"{prefix} 上游已返回最终图片")
    elif event_type == "response.completed":
        update_task_stage(task_id, f"{prefix} 上游响应完成，正在保存图片")


def responses_payload_for_planner(
    *,
    model: str,
    content: list[dict[str, Any]],
    previous_response_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "input": [{"role": "user", "content": content}],
    }
    if previous_response_id:
        payload["previous_response_id"] = previous_response_id
    return payload


def text_delta_from_stream_event(event: dict[str, Any]) -> str:
    event_type = str(event.get("type") or "")
    if event_type == "chat.completion.delta":
        delta = event.get("delta")
        return str(delta) if isinstance(delta, str) else ""
    if event_type in {"response.output_text.delta", "response.text.delta"}:
        delta = event.get("delta") or event.get("text")
        return str(delta) if isinstance(delta, str) else ""
    if event_type == "response.output_item.done":
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "message":
            return extract_text_from_responses({"output": [item]})
    return ""


def extract_partial_json_string_field(buffer: str, field: str) -> str:
    match = re.search(rf'"{re.escape(field)}"\s*:\s*"', buffer)
    if not match:
        return ""
    index = match.end()
    chars: list[str] = []
    while index < len(buffer):
        char = buffer[index]
        if char == '"':
            break
        if char != "\\":
            chars.append(char)
            index += 1
            continue
        if index + 1 >= len(buffer):
            break
        esc = buffer[index + 1]
        if esc == "u":
            raw = buffer[index + 2 : index + 6]
            if len(raw) < 4 or not re.fullmatch(r"[0-9a-fA-F]{4}", raw):
                break
            chars.append(chr(int(raw, 16)))
            index += 6
            continue
        chars.append({"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f"}.get(esc, esc))
        index += 2
    return "".join(chars).strip()


def make_planner_reply_stream_handler(task_id: int, message_id: int, fallback: str) -> Callable[[dict[str, Any]], None]:
    state: dict[str, Any] = {"raw": "", "last_reply": "", "last_write": 0.0}

    def on_event(event: dict[str, Any]) -> None:
        delta = text_delta_from_stream_event(event)
        if delta:
            state["raw"] += delta
        reply = extract_partial_json_string_field(str(state["raw"]), "reply")
        if not reply or reply == state["last_reply"]:
            return
        now = time.monotonic()
        if len(reply) - len(str(state["last_reply"])) < 8 and now - float(state["last_write"] or 0) < 0.35:
            return
        state["last_reply"] = reply
        state["last_write"] = now
        update_message_content(message_id, reply or fallback)
        publish_task_event(
            task_id,
            "assistant_reply",
            {"message_id": message_id, "content": reply or fallback},
            snapshot=True,
        )

    return on_event


async def call_chat_planner(
    *,
    model: str,
    prompt: str,
    config: ClientConfig,
    uploaded: list[tuple[Path, str]] | None = None,
    image_contexts: list[dict[str, Any]] | None = None,
    previous_response_id: str | None = None,
    on_stream_event: Callable[[dict[str, Any]], None] | None = None,
    planner_endpoint: str = "responses",
) -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
    chat_lines: list[str] = [prompt]
    contexts = image_contexts or []
    for idx, (path, mime_type) in enumerate(uploaded or [], start=1):
        context = contexts[idx - 1] if idx - 1 < len(contexts) else {}
        ref = context.get("ref") or f"reference:{idx}"
        if context.get("source") == "upload":
            reference_text = f"Reference image {idx}: ref={ref}; 这是用户本轮上传的参考图，没有对应生图提示词。下一张 input_image 就是这个 ref 对应的图片。"
            chat_reference_text = f"Reference image {idx}: ref={ref}; 这是用户本轮上传的参考图，没有对应生图提示词。chat/completions 兼容模式只传文字说明，不附带图片本体。"
        else:
            hint = context.get("hint") or "无对应历史提示词"
            reference_text = f"Reference image {idx}: ref={ref}; 该参考图对应的一张图片生图提示词/说明={hint}。下一张 input_image 就是这个 ref 对应的图片。"
            chat_reference_text = f"Reference image {idx}: ref={ref}; 该参考图对应的一张图片生图提示词/说明={hint}。chat/completions 兼容模式只传文字说明，不附带图片本体。"
        content.append({"type": "input_text", "text": reference_text})
        content.append({"type": "input_image", "image_url": data_url_for_file(path, mime_type)})
        chat_lines.append(chat_reference_text)

    if planner_endpoint == "chat_completions":
        payload = {"model": model, "messages": [{"role": "user", "content": "\n\n".join(chat_lines)}]}
        return await post_chat_completions(
            payload,
            base_url=config.base_url,
            api_key=config.api_key,
            timeout=CHAT_PLANNER_TIMEOUT_SECONDS,
            max_attempts=CHAT_PLANNER_MAX_ATTEMPTS,
            stream=on_stream_event is not None,
            on_event=on_stream_event,
        )

    payload = responses_payload_for_planner(model=model, content=content, previous_response_id=previous_response_id)
    if on_stream_event is not None:
        return await post_json_stream(
            "responses",
            payload,
            base_url=config.base_url,
            api_key=config.api_key,
            timeout=CHAT_PLANNER_TIMEOUT_SECONDS,
            max_attempts=CHAT_PLANNER_MAX_ATTEMPTS,
            on_event=on_stream_event,
        )
    return await post_json(
        "responses",
        payload,
        base_url=config.base_url,
        api_key=config.api_key,
        timeout=CHAT_PLANNER_TIMEOUT_SECONDS,
        max_attempts=CHAT_PLANNER_MAX_ATTEMPTS,
    )


def active_task_count() -> int:
    with db.connect() as conn:
        row = conn.execute(
            "select count(*) as count from tasks where status in ('queued', 'running')"
        ).fetchone()
    return int(row["count"])


def resolve_provider_stage_text(
    stage: str | Callable[[dict[str, Any]], str] | None,
    provider: dict[str, Any],
    fallback: str,
) -> str:
    if callable(stage):
        text = str(stage(provider) or "").strip()
        return text or fallback
    text = str(stage or "").strip()
    return text or fallback


async def acquire_image_provider_slot(
    task_id: int,
    waiting_stage: str | Callable[[dict[str, Any]], str] | None = None,
    running_stage: str | Callable[[dict[str, Any]], str] | None = None,
    exclude_provider_ids: set[int] | None = None,
) -> dict[str, Any]:
    pool = load_image_provider_pool()
    if not pool:
        raise HTTPException(status_code=400, detail="当前没有可用的生图提供商，请先配置 provider 池。")

    excluded_ids = {int(value) for value in (exclude_provider_ids or set())}
    pool_lock = ensure_provider_pool_lock()
    async with pool_lock:
        states = [ensure_provider_pool_state(provider, index) for index, provider in enumerate(pool)]
        usable_states = [
            state
            for state in states
            if int(state["provider"]["id"]) not in excluded_ids and not provider_is_temporarily_unavailable(state)
        ]
        if not usable_states:
            raise HTTPException(status_code=503, detail=all_providers_unavailable_detail(states, []))
        state = min(
            usable_states,
            key=lambda item: (
                int(item["running_count"]),
                int(item["assigned_count"]),
                int(item["order"]),
            ),
        )
        provider = dict(state["provider"])
        waiting = int(state["running_count"]) >= MAX_CONCURRENT_TASKS
        state["assigned_count"] = int(state["assigned_count"]) + 1

    waiting_text = resolve_provider_stage_text(waiting_stage, provider, f"已分配生图提供商：{provider['name']}，等待空闲通道")
    update_task_stage(
        task_id,
        waiting_text if waiting else resolve_provider_stage_text(running_stage or waiting_stage, provider, f"已分配生图提供商：{provider['name']}"),
        provider=provider,
    )

    acquired = False
    running_incremented = False
    try:
        await state["semaphore"].acquire()
        acquired = True
        async with pool_lock:
            state["running_count"] = int(state["running_count"]) + 1
            running_incremented = True
    except BaseException:
        if acquired:
            state["semaphore"].release()
        async with pool_lock:
            if running_incremented:
                state["running_count"] = max(0, int(state["running_count"]) - 1)
            state["assigned_count"] = max(0, int(state["assigned_count"]) - 1)
        publish_task_snapshot(task_id)
        raise

    update_task_stage(
        task_id,
        resolve_provider_stage_text(running_stage, provider, f"正在使用生图提供商：{provider['name']}"),
        provider=provider,
    )
    return {"provider": provider, "state": state}


async def release_image_provider_slot(task_id: int, lease: dict[str, Any] | None) -> None:
    if not lease:
        return
    provider = lease.get("provider") if isinstance(lease, dict) else None
    state = lease.get("state") if isinstance(lease, dict) else None
    if not provider or not state:
        return
    state["semaphore"].release()
    pool_lock = ensure_provider_pool_lock()
    async with pool_lock:
        state["running_count"] = max(0, int(state["running_count"]) - 1)
        state["assigned_count"] = max(0, int(state["assigned_count"]) - 1)
    publish_task_snapshot(task_id)


def update_task_stage(
    task_id: int,
    stage: str,
    *,
    provider: dict[str, Any] | None = None,
    progress: int | None = None,
) -> None:
    values: dict[str, Any] = {"stage": stage}
    if progress is not None:
        values["progress"] = progress
    if provider:
        values["image_provider_id"] = int(provider["id"])
        values["image_provider_name"] = str(provider["name"])
    db.update_task(task_id, **values)
    checkpoint_updates: dict[str, Any] = {
        "stage": stage,
        "last_status": "running",
    }
    if progress is not None:
        checkpoint_updates["progress"] = progress
    if provider:
        checkpoint_updates["image_provider_id"] = int(provider["id"])
        checkpoint_updates["image_provider_name"] = str(provider["name"])
    merge_task_checkpoint_state(task_id, **checkpoint_updates)
    publish_task_snapshot(task_id)


async def execute_with_provider_failover(
    task_id: int,
    execute: Callable[[dict[str, Any], ClientConfig], Any],
    *,
    waiting_stage: str | Callable[[dict[str, Any]], str] | None = None,
    running_stage: str | Callable[[dict[str, Any]], str] | None = None,
    retry_stage: Callable[[dict[str, Any], int], str] | None = None,
    switch_stage: Callable[[dict[str, Any]], str] | None = None,
) -> tuple[Any, dict[str, Any], list[dict[str, Any]]]:
    attempted_provider_ids: set[int] = set()
    provider_attempts: list[dict[str, Any]] = []

    while True:
        try:
            lease = await acquire_image_provider_slot(
                task_id,
                waiting_stage=waiting_stage,
                running_stage=running_stage,
                exclude_provider_ids=attempted_provider_ids,
            )
        except HTTPException as exc:
            if int(exc.status_code or 0) == 503:
                detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
                detail["provider_attempts"] = copy.deepcopy(provider_attempts)
                raise HTTPException(status_code=503, detail=detail) from exc
            raise

        provider = lease["provider"]
        state = lease["state"]
        provider_config = provider_client_config(provider)
        same_provider_attempts = PROVIDER_UNAVAILABLE_RETRY_COUNT + 1

        try:
            for attempt_index in range(1, same_provider_attempts + 1):
                if attempt_index > 1:
                    retry_text = retry_stage(provider, attempt_index) if retry_stage else f"{provider['name']} 暂时不可用，正在重试第 {attempt_index}/{same_provider_attempts} 次"
                    update_task_stage(task_id, retry_text, provider=provider)
                try:
                    result = await execute(provider, provider_config)
                    clear_provider_unavailable_state(state)
                    if provider_attempts:
                        provider_attempts.append(
                            {
                                "provider_id": int(provider["id"]),
                                "provider_name": str(provider["name"]),
                                "action": "success",
                                "attempt": attempt_index,
                            }
                        )
                    return result, provider, provider_attempts
                except HTTPException as exc:
                    if not is_provider_unavailable_error(exc):
                        if provider_attempts:
                            detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
                            detail["provider_attempts"] = copy.deepcopy(provider_attempts)
                            detail["image_provider"] = {"id": provider["id"], "name": provider["name"]}
                            raise HTTPException(status_code=exc.status_code, detail=detail) from exc
                        raise
                    action = "retrying_same_provider" if attempt_index < same_provider_attempts else "provider_unavailable"
                    provider_attempts.append(provider_attempt_entry(provider, exc.detail, action=action, attempt=attempt_index))
                    if attempt_index < same_provider_attempts:
                        continue
                    mark_provider_unavailable(state, exc.detail)
                    attempted_provider_ids.add(int(provider["id"]))
                    switch_text = switch_stage(provider) if switch_stage else f"生图提供商 {provider['name']} 暂不可用，正在切换下一个最佳提供商"
                    update_task_stage(task_id, switch_text, provider=provider)
                    break
        finally:
            await release_image_provider_slot(task_id, lease)


def ensure_conversation_message_allowed(
    conn: sqlite3.Connection,
    conversation_id: int,
    expected_mode: str,
) -> sqlite3.Row:
    conversation = conn.execute(
        """
        select c.*,
            (
                select t.mode from tasks t
                where t.conversation_id = c.id
                order by t.id desc
                limit 1
            ) as latest_task_mode
        from conversations c
        where c.id = ?
        """,
        (conversation_id,),
    ).fetchone()
    if not conversation:
        raise HTTPException(status_code=404, detail="conversation not found")

    current_mode = resolved_conversation_mode(conversation)
    if current_mode != expected_mode and (
        normalize_conversation_mode(conversation["mode"]) or normalize_conversation_mode(conversation["latest_task_mode"])
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"当前会话属于{conversation_mode_label(current_mode)}模式，不能直接切换到{conversation_mode_label(expected_mode)}模式继续发送。",
                "status_code": 409,
                "suggestion": "请新建一个对应模式的新对话，或回到原模式继续当前会话。",
            },
        )

    active_task = conn.execute(
        """
        select id, mode, status, stage
        from tasks
        where conversation_id = ?
          and status in ('queued', 'running')
        order by id desc
        limit 1
        """,
        (conversation_id,),
    ).fetchone()
    if active_task:
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"当前会话仍有{conversation_mode_label(active_task['mode'])}任务{task_status_label(active_task['status'])}，请先停止该任务或新建对话后再继续发送。",
                "status_code": 409,
                "suggestion": "同一会话在任务排队或运行时不能继续发送新消息；你可以先停止该任务，或者点击新对话继续。",
            },
        )

    if normalize_conversation_mode(conversation["mode"]) != expected_mode:
        conn.execute(
            "update conversations set mode = ?, updated_at = ? where id = ?",
            (expected_mode, db.now_iso(), conversation_id),
        )
        conversation = conn.execute(
            """
            select c.*,
                (
                    select t.mode from tasks t
                    where t.conversation_id = c.id
                    order by t.id desc
                    limit 1
                ) as latest_task_mode
            from conversations c
            where c.id = ?
            """,
            (conversation_id,),
        ).fetchone()
    return conversation


def create_direct_mode_user_message(
    *,
    conversation_id: int,
    prompt: str,
    uploads: list[tuple[Path, str]] | None = None,
    meta_updates: dict[str, Any] | None = None,
) -> int:
    stamp = db.now_iso()
    meta = {
        "uploads": [str(path) for path, _mime in (uploads or [])],
    }
    if isinstance(meta_updates, dict):
        meta.update(compact_checkpoint_payload(meta_updates))
    with db.connect() as conn:
        cursor = conn.execute(
            """
            insert into messages (conversation_id, role, content, meta_json, created_at, updated_at)
            values (?, ?, ?, ?, ?, ?)
            """,
            (
                conversation_id,
                "user",
                prompt,
                db.json_dumps(meta),
                stamp,
                stamp,
            ),
        )
        message_id = int(cursor.lastrowid)
        conn.execute(
            "update conversations set updated_at = ? where id = ?",
            (stamp, conversation_id),
        )
    return message_id


def ensure_conversation_task_retry_allowed(conn: sqlite3.Connection, conversation_id: int, retry_source_task_id: int) -> None:
    active_task = conn.execute(
        """
        select id, mode, status
        from tasks
        where conversation_id = ?
          and id != ?
          and status in ('queued', 'running')
        order by id desc
        limit 1
        """,
        (conversation_id, retry_source_task_id),
    ).fetchone()
    if active_task:
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"当前会话仍有{conversation_mode_label(active_task['mode'])}任务{task_status_label(active_task['status'])}，请先停止该任务后再重试。",
                "status_code": 409,
                "suggestion": "同一会话同一时刻只能运行一个任务；你可以先停止当前任务，或稍后再重试。",
            },
        )


def ensure_task_slot() -> None:
    capacity = provider_pool_capacity()
    if active_task_count() >= capacity:
        raise HTTPException(
            status_code=429,
            detail={
                "message": f"当前生图提供商池最多支持 {capacity} 个任务运行或排队，请等待其中一个完成后再创建新任务。",
                "status_code": 429,
                "suggestion": "可以在任务卡片里停止不需要的任务，或等待任务完成。",
            },
        )


def conversation_has_active_task(conversation_id: int | None, *, exclude_task_id: int | None = None) -> bool:
    if not conversation_id:
        return False
    with db.connect() as conn:
        row = conn.execute(
            """
            select id
            from tasks
            where conversation_id = ?
              and status in ('queued', 'running')
              and (? is null or id != ?)
            order by id desc
            limit 1
            """,
            (conversation_id, exclude_task_id, exclude_task_id),
        ).fetchone()
    return row is not None


def schedule_existing_task(task: dict[str, Any]) -> None:
    task_id = int(task["id"])
    task = task_with_images(task_id)
    mode = str(task.get("mode") or "")
    params = copy.deepcopy(task.get("params") or {})
    checkpoint = task_checkpoint_dict(task)
    conversation_id = int(task["conversation_id"]) if task.get("conversation_id") else None
    user_message_id = int(task["user_message_id"]) if task.get("user_message_id") else None
    assistant_message_id = int(task["assistant_message_id"]) if task.get("assistant_message_id") else None
    if mode == "generate":
        request = GenerateRequest(**normalize_text_fields(params))
        completed_count = min(max(int(checkpoint.get("completed_count") or 0), 0), int(request.n))
        schedule_task(
            task_id,
            run_generate_task(
                task_id,
                request,
                compact_params(params),
                conversation_id=conversation_id,
                user_message_id=user_message_id,
                resume_checkpoint=checkpoint,
                restored_images=existing_task_output_images(task, completed_count=completed_count),
            ),
        )
        return
    if mode == "edit":
        input_images = [
            (Path(str(image.get("file_path") or "")), str(image.get("mime_type") or "image/png"))
            for image in task.get("images", [])
            if image.get("source") == "input" and Path(str(image.get("file_path") or "")).exists()
        ]
        if not input_images:
            raise HTTPException(status_code=400, detail="该编辑任务缺少可执行的输入图")
        saved_mask = next(
            (
                (Path(str(image.get("file_path") or "")), str(image.get("mime_type") or "image/png"))
                for image in task.get("images", [])
                if image.get("source") == "mask" and Path(str(image.get("file_path") or "")).exists()
            ),
            None,
        )
        schedule_task(
            task_id,
            run_edit_task(
                task_id,
                params,
                str(params.get("prompt") or task.get("prompt") or ""),
                input_images,
                saved_mask,
                conversation_id=conversation_id,
                user_message_id=user_message_id,
                resume_checkpoint=checkpoint,
                restored_images=existing_task_output_images(
                    task,
                    completed_count=min(max(int(checkpoint.get("completed_count") or 0), 0), clamp_image_count(params.get("n", 1))),
                ),
            ),
        )
        return
    if mode in {"chat", "storyboard"}:
        if not conversation_id or not user_message_id:
            raise HTTPException(status_code=400, detail="该会话任务缺少必要的会话信息")
        request_model = ChatRequest if mode == "chat" else StoryboardRequest
        request = request_model(**normalize_text_fields(params))
        with db.connect() as conn:
            conversation = ensure_conversation_message_allowed(conn, conversation_id, mode)
            conversation_title = str(conversation["title"] or "")
            context_limit = int(params.get("context_limit") if params.get("context_limit") is not None else conversation["context_limit"])
            context_limit = max(0, min(context_limit, 50))
        recent_messages = load_recent_messages(
            conversation_id,
            context_limit,
            exclude_message_ids=[user_message_id, assistant_message_id] if assistant_message_id else [user_message_id],
        )
        image_candidates = recover_task_image_candidates(
            task,
            params,
            conversation_id=conversation_id,
            label=f"{conversation_mode_label(mode)}任务参考图",
        )
        if mode == "chat":
            schedule_task(
                task_id,
                run_chat_task(
                    task_id,
                    conversation_id,
                    user_message_id,
                    request,
                    image_candidates,
                    None,
                    conversation_title,
                    recent_messages,
                    context_limit,
                    assistant_message_id=assistant_message_id,
                    resume_checkpoint=checkpoint,
                    restored_images=existing_task_output_images(
                        task,
                        completed_count=min(max(int(checkpoint.get("completed_count") or 0), 0), max(int(checkpoint.get("total_count") or 0), 1)),
                    ),
                ),
            )
            return
        if params.get("retry_of") or checkpoint.get("storyboard") or any(image.get("source") == "api" for image in task.get("images", [])):
            schedule_task(task_id, run_storyboard_retry_task(task_id, task, compact_params(params)))
            return
        schedule_task(
            task_id,
            run_storyboard_task(
                task_id,
                conversation_id,
                user_message_id,
                request,
                image_candidates,
                None,
                conversation_title,
                recent_messages,
                context_limit,
                compact_params(params),
            ),
        )
        return
    raise HTTPException(status_code=400, detail="暂不支持该任务模式的定时执行")


async def dispatch_scheduled_tasks_once() -> None:
    available_slots = max(provider_pool_capacity() - active_task_count(), 0)
    if available_slots <= 0:
        return
    dispatched_conversation_ids: set[int] = set()
    with db.connect() as conn:
        rows = conn.execute(
            """
            select *
            from tasks
            where status = 'scheduled'
              and (scheduled_for is null or scheduled_for = '' or scheduled_for <= ?)
            order by coalesce(scheduled_for, created_at) asc, id asc
            limit ?
            """,
            (db.now_iso(), max(available_slots * 3, 6)),
        ).fetchall()
    for row in rows:
        if available_slots <= 0:
            break
        task_id = int(row["id"])
        task = task_with_images(task_id)
        conversation_id = int(task["conversation_id"]) if task.get("conversation_id") else None
        if (
            conversation_id in dispatched_conversation_ids
            or conversation_has_active_task(conversation_id, exclude_task_id=task_id)
        ):
            waiting_stage = scheduled_task_stage(
                task.get("scheduled_for"),
                queue_position=task.get("queue_position"),
                queue_total=task.get("queue_total"),
                waiting_reason="等待同会话前序任务完成",
            )
            if str(task.get("stage") or "") != waiting_stage:
                db.update_task(task_id, stage=waiting_stage)
                merge_task_checkpoint_state(
                    task_id,
                    stage=waiting_stage,
                    last_status="scheduled",
                )
                publish_task_snapshot(task_id)
            continue
        try:
            db.update_task(task_id, stage="定时任务已到点，准备启动")
            merge_task_checkpoint_state(
                task_id,
                stage="定时任务已到点，准备启动",
                last_status="scheduled",
            )
            publish_task_snapshot(task_id)
            schedule_existing_task(task)
            if conversation_id:
                dispatched_conversation_ids.add(conversation_id)
            available_slots -= 1
        except HTTPException as exc:
            db.fail_task(task_id, compact_error_detail(exc.detail))
            merge_task_checkpoint_state(
                task_id,
                stage="失败",
                last_status="failed",
                last_error=exc.detail,
            )
            publish_task_snapshot(task_id)
            publish_task_event(task_id, "failed", {"task_id": task_id, "error": exc.detail}, snapshot=False)
        except Exception as exc:
            db.fail_task(task_id, str(exc))
            merge_task_checkpoint_state(
                task_id,
                stage="失败",
                last_status="failed",
                last_error=str(exc),
            )
            publish_task_snapshot(task_id)
            publish_task_event(task_id, "failed", {"task_id": task_id, "error": str(exc)}, snapshot=False)


async def scheduled_task_dispatch_loop() -> None:
    while True:
        try:
            await dispatch_scheduled_tasks_once()
        except Exception:
            pass
        await asyncio.sleep(SCHEDULER_POLL_INTERVAL_SECONDS)


def ensure_scheduled_task_loop() -> None:
    global TASK_SCHEDULER_LOOP
    if TASK_SCHEDULER_LOOP and not TASK_SCHEDULER_LOOP.done():
        return
    TASK_SCHEDULER_LOOP = asyncio.create_task(scheduled_task_dispatch_loop())


def schedule_task(task_id: int, coro: Any) -> None:
    scope = current_storage_scope()
    runtime_key = task_runtime_key(task_id, scope)

    async def runner() -> None:
        token = set_storage_scope(scope)
        try:
            await coro
        finally:
            reset_storage_scope(token)

    task = asyncio.create_task(runner())
    RUNNING_TASKS[runtime_key] = task

    def cleanup(done: asyncio.Task[Any]) -> None:
        is_current = RUNNING_TASKS.get(runtime_key) is done
        if is_current:
            RUNNING_TASKS.pop(runtime_key, None)
        if done.cancelled() and is_current:
            token = set_storage_scope(scope)
            try:
                db.cancel_task(task_id)
                merge_task_checkpoint_state(
                    task_id,
                    stage="已停止",
                    last_status="canceled",
                    last_error="用户已停止任务",
                )
            finally:
                reset_storage_scope(token)

    task.add_done_callback(cleanup)


async def run_with_slot(task_id: int, worker: Any) -> None:
    try:
        task = db.get_task(task_id)
        if task and task.get("cancel_requested"):
            db.cancel_task(task_id)
            merge_task_checkpoint_state(
                task_id,
                stage="已停止",
                last_status="canceled",
                last_error="用户已停止任务",
            )
            publish_task_snapshot(task_id)
            publish_task_event(task_id, "canceled", {"task_id": task_id}, snapshot=False)
            return
        db.update_task(task_id, status="running", progress=8, stage="任务已启动")
        merge_task_checkpoint_state(
            task_id,
            stage="任务已启动",
            progress=8,
            last_status="running",
            last_error=None,
        )
        publish_task_snapshot(task_id)
        await worker()
    except asyncio.CancelledError:
        db.cancel_task(task_id)
        merge_task_checkpoint_state(
            task_id,
            stage="已停止",
            last_status="canceled",
            last_error="用户已停止任务",
        )
        publish_task_snapshot(task_id)
        publish_task_event(task_id, "canceled", {"task_id": task_id}, snapshot=False)
        raise
    except HTTPException as exc:
        if is_all_providers_unavailable_exception(exc):
            retry_state = schedule_provider_pool_auto_retry(task_id, exc)
            if retry_state:
                publish_task_snapshot(task_id)
                publish_task_event(
                    task_id,
                    "retry_scheduled",
                    {
                        "task_id": task_id,
                        "reason": PROVIDER_POOL_AUTO_RETRY_REASON,
                        **retry_state,
                    },
                    snapshot=False,
                )
                return
        db.fail_task(task_id, compact_error_detail(exc.detail))
        merge_task_checkpoint_state(
            task_id,
            stage="失败",
            last_status="failed",
            last_error=exc.detail,
        )
        publish_task_snapshot(task_id)
        publish_task_event(task_id, "failed", {"task_id": task_id, "error": exc.detail}, snapshot=False)
    except Exception as exc:
        db.fail_task(task_id, str(exc))
        merge_task_checkpoint_state(
            task_id,
            stage="失败",
            last_status="failed",
            last_error=str(exc),
        )
        publish_task_snapshot(task_id)
        publish_task_event(task_id, "failed", {"task_id": task_id, "error": str(exc)}, snapshot=False)
    else:
        merge_task_checkpoint_state(
            task_id,
            stage="已完成",
            progress=100,
            can_resume=False,
            last_status="done",
            last_error=None,
            completed_at=db.now_iso(),
        )
        publish_task_snapshot(task_id)
        publish_task_event(task_id, "done", {"task_id": task_id}, snapshot=False)


def task_image_folder(task_id: int, title: str) -> str:
    return safe_storage_folder(title, db.now_iso())


def ensure_default_provider() -> None:
    with db.connect() as conn:
        row = conn.execute("select count(*) as count from providers").fetchone()
        if int(row["count"]) > 0:
            return
        stamp = db.now_iso()
        conn.execute(
            """
            insert into providers (name, base_url, api_key, created_at, updated_at)
            values (?, ?, ?, ?, ?)
            """,
            ("默认提供商", DEFAULT_API_BASE_URL, DEFAULT_API_KEY, stamp, stamp),
        )


@app.on_event("startup")
def startup() -> None:
    for scope in all_known_storage_scopes():
        token = set_storage_scope(scope)
        try:
            ensure_dirs(scope)
            db.init_db()
            ensure_default_provider()
            with db.connect() as conn:
                rows = conn.execute(
                    "select id, mode, checkpoint_json from tasks where status in ('queued', 'running')"
                ).fetchall()
                for row in rows:
                    checkpoint = {}
                    if isinstance(row["checkpoint_json"], str):
                        try:
                            parsed = json.loads(row["checkpoint_json"] or "{}")
                            if isinstance(parsed, dict):
                                checkpoint = parsed
                        except json.JSONDecodeError:
                            checkpoint = {}
                    stage = "服务重启后检测到未完成任务，正在自动恢复"
                    if isinstance(checkpoint.get("stage"), str) and checkpoint.get("stage"):
                        stage = f"服务重启后自动恢复：{checkpoint['stage']}"
                    conn.execute(
                        """
                        update tasks
                        set status = 'scheduled',
                            scheduled_for = ?,
                            stage = ?,
                            error = null,
                            updated_at = ?
                        where id = ?
                        """,
                        (db.now_iso(), stage, db.now_iso(), int(row["id"])),
                    )
        finally:
            reset_storage_scope(token)
    ensure_scheduled_task_loop()


@app.get(ACCESS_LOGIN_PATH, response_class=HTMLResponse, response_model=None)
def access_login_page(request: Request, next: str = "/"):
    if access_cookie_valid(request):
        return RedirectResponse(url=sanitized_next_path(next), status_code=303)
    return HTMLResponse(login_page_html(next))


@app.post(ACCESS_LOGIN_PATH, response_class=HTMLResponse, response_model=None)
async def access_login_submit(
    password: str = Form(...),
    next: str = Form(default="/"),
):
    target = sanitized_next_path(next)
    resolved_password = resolve_access_password(password)
    if not resolved_password:
        return HTMLResponse(login_page_html(target, ACCESS_ERROR_MESSAGE), status_code=401)
    response = RedirectResponse(url=target, status_code=303)
    response.set_cookie(
        ACCESS_COOKIE_NAME,
        access_cookie_token(resolved_password),
        httponly=True,
        samesite="lax",
        secure=False,
        path="/",
    )
    return response


@app.post("/auth/logout")
def access_logout():
    response = JSONResponse({"ok": True, "redirect_to": ACCESS_LOGIN_PATH})
    response.delete_cookie(
        ACCESS_COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="lax",
        secure=False,
    )
    return response


def normalize_access_user_password(value: str) -> str:
    normalized = normalize_access_password(value)
    if not ACCESS_PASSWORD_PATTERN.fullmatch(normalized):
        raise HTTPException(
            status_code=400,
            detail={
                "message": "访问密码必须是 8-32 位字母或数字。",
                "suggestion": "请使用不含空格、符号或中文字符的字母数字组合。",
            },
        )
    return normalized


def access_user_items() -> list[dict[str, Any]]:
    refresh_access_password_cache()
    registry = load_access_user_registry()
    managed = {item["password"]: item for item in registry["users"]}
    items: list[dict[str, Any]] = []
    for password in ACCESS_PASSWORDS:
        scope = access_storage_scope(password)
        managed_item = managed.get(password, {})
        items.append(
            {
                "password": password,
                "storage_scope": scope,
                "is_admin": password == DEFAULT_ACCESS_PASSWORD,
                "is_builtin": password in BASE_ACCESS_PASSWORDS,
                "is_managed": password in managed,
                "editable": password != DEFAULT_ACCESS_PASSWORD,
                "deletable": password != DEFAULT_ACCESS_PASSWORD,
                "created_at": managed_item.get("created_at"),
                "updated_at": managed_item.get("updated_at"),
            }
        )
    return items


def ensure_access_user_exists(password: str) -> None:
    refresh_access_password_cache()
    if password not in ACCESS_PASSWORDS:
        raise HTTPException(status_code=404, detail="用户不存在")


def write_access_user_change(*, old_password: str | None = None, new_password: str | None = None, delete: bool = False) -> None:
    registry = load_access_user_registry()
    users_by_password = {item["password"]: item for item in registry["users"]}
    disabled = set(registry["disabled_passwords"])
    stamp = db.now_iso()
    if old_password:
        if old_password == DEFAULT_ACCESS_PASSWORD:
            raise HTTPException(status_code=400, detail="主账号 hhs54666 不能修改或删除")
        if old_password in BASE_ACCESS_PASSWORDS:
            disabled.add(old_password)
        users_by_password.pop(old_password, None)
    if new_password and not delete:
        users_by_password[new_password] = {
            "password": new_password,
            "created_at": users_by_password.get(new_password, {}).get("created_at") or stamp,
            "updated_at": stamp,
        }
        disabled.discard(new_password)
        ensure_dirs(access_storage_scope(new_password))
    payload = {
        "users": sorted(users_by_password.values(), key=lambda item: item["password"]),
        "disabled_passwords": sorted(value for value in disabled if value != DEFAULT_ACCESS_PASSWORD),
    }
    save_access_user_registry(payload)
    refresh_access_password_cache()


@app.get("/api/access-users/me")
def get_current_access_user(request: Request) -> dict[str, Any]:
    password = request_access_password(request) or ""
    return {
        "password": password,
        "storage_scope": access_storage_scope(password) if password else "",
        "is_admin": password == DEFAULT_ACCESS_PASSWORD,
    }


@app.get("/api/access-users")
def list_access_users(request: Request) -> dict[str, Any]:
    require_access_user_admin(request)
    return {"items": access_user_items()}


@app.post("/api/access-users")
def create_access_user(request: Request, payload: AccessUserRequest) -> dict[str, Any]:
    require_access_user_admin(request)
    password = normalize_access_user_password(payload.password)
    refresh_access_password_cache()
    if password in ACCESS_PASSWORDS:
        raise HTTPException(status_code=409, detail="该用户已存在")
    write_access_user_change(new_password=password)
    return {"items": access_user_items()}


@app.put("/api/access-users/{password}")
def update_access_user(password: str, request: Request, payload: AccessUserRequest) -> dict[str, Any]:
    require_access_user_admin(request)
    old_password = normalize_access_user_password(password)
    new_password = normalize_access_user_password(payload.password)
    ensure_access_user_exists(old_password)
    if old_password == new_password:
        return {"items": access_user_items()}
    refresh_access_password_cache()
    if new_password in ACCESS_PASSWORDS:
        raise HTTPException(status_code=409, detail="新密码对应用户已存在")
    write_access_user_change(old_password=old_password, new_password=new_password)
    return {"items": access_user_items()}


@app.delete("/api/access-users/{password}")
def delete_access_user(password: str, request: Request) -> dict[str, Any]:
    require_access_user_admin(request)
    old_password = normalize_access_user_password(password)
    ensure_access_user_exists(old_password)
    write_access_user_change(old_password=old_password, delete=True)
    return {"items": access_user_items()}


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/settings")
def get_settings() -> dict[str, str]:
    with db.connect() as conn:
        rows = conn.execute("select key, value from settings").fetchall()
    values = {row["key"]: row["value"] for row in rows}
    return {
        "base_url": values.get("base_url", DEFAULT_API_BASE_URL),
        "api_key": values.get("api_key", DEFAULT_API_KEY),
    }


@app.put("/api/settings")
def put_settings(config: ClientConfig) -> dict[str, str]:
    stamp = db.now_iso()
    api_key = validate_api_key_text(config.api_key, field_label="默认 API Key") if config.api_key is not None else None
    with db.connect() as conn:
        if config.base_url is not None:
            conn.execute(
                "insert or replace into settings (key, value, updated_at) values (?, ?, ?)",
                ("base_url", config.base_url, stamp),
            )
        if api_key is not None:
            conn.execute(
                "insert or replace into settings (key, value, updated_at) values (?, ?, ?)",
                ("api_key", api_key, stamp),
            )
    return get_settings()


@app.get("/api/app-settings")
def get_app_settings() -> dict[str, Any]:
    with db.connect() as conn:
        row = conn.execute("select value from settings where key = ?", ("app_settings",)).fetchone()
    if not row:
        return {"value": {}}
    try:
        value = json.loads(row["value"])
    except json.JSONDecodeError:
        value = {}
    return {"value": value if isinstance(value, dict) else {}}


@app.put("/api/app-settings")
def put_app_settings(request: AppSettingsRequest) -> dict[str, Any]:
    stamp = db.now_iso()
    with db.connect() as conn:
        conn.execute(
            "insert or replace into settings (key, value, updated_at) values (?, ?, ?)",
            ("app_settings", db.json_dumps(request.value), stamp),
        )
    return get_app_settings()


@app.get("/api/style-locks")
def list_style_locks() -> dict[str, Any]:
    with db.connect() as conn:
        rows = conn.execute("select * from style_locks order by updated_at desc, id desc").fetchall()
    return {"items": [serialize_style_lock_row(row) for row in rows]}


@app.post("/api/style-locks")
def create_style_lock(request: StyleLockRequest) -> dict[str, Any]:
    stamp = db.now_iso()
    with db.connect() as conn:
        cursor = conn.execute(
            """
            insert into style_locks
                (name, subject_lock, composition_lock, color_tone_lock, lighting_lock, texture_lock, negative_lock, notes, created_at, updated_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalize_image_title(request.name, fallback="未命名风格锁"),
                normalize_free_text(request.subject_lock, 600),
                normalize_free_text(request.composition_lock, 600),
                normalize_free_text(request.color_tone_lock, 600),
                normalize_free_text(request.lighting_lock, 600),
                normalize_free_text(request.texture_lock, 600),
                normalize_free_text(request.negative_lock, 600),
                normalize_free_text(request.notes, 1000),
                stamp,
                stamp,
            ),
        )
        row = conn.execute("select * from style_locks where id = ?", (int(cursor.lastrowid),)).fetchone()
    return serialize_style_lock_row(row)


@app.put("/api/style-locks/{style_lock_id}")
def update_style_lock(style_lock_id: int, request: StyleLockRequest) -> dict[str, Any]:
    with db.connect() as conn:
        cursor = conn.execute(
            """
            update style_locks
            set name = ?, subject_lock = ?, composition_lock = ?, color_tone_lock = ?, lighting_lock = ?, texture_lock = ?, negative_lock = ?, notes = ?, updated_at = ?
            where id = ?
            """,
            (
                normalize_image_title(request.name, fallback="未命名风格锁"),
                normalize_free_text(request.subject_lock, 600),
                normalize_free_text(request.composition_lock, 600),
                normalize_free_text(request.color_tone_lock, 600),
                normalize_free_text(request.lighting_lock, 600),
                normalize_free_text(request.texture_lock, 600),
                normalize_free_text(request.negative_lock, 600),
                normalize_free_text(request.notes, 1000),
                db.now_iso(),
                style_lock_id,
            ),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="style lock not found")
        row = conn.execute("select * from style_locks where id = ?", (style_lock_id,)).fetchone()
    return serialize_style_lock_row(row)


@app.delete("/api/style-locks/{style_lock_id}")
def delete_style_lock(style_lock_id: int) -> dict[str, Any]:
    with db.connect() as conn:
        cursor = conn.execute("delete from style_locks where id = ?", (style_lock_id,))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="style lock not found")
    return {"ok": True}


@app.get("/api/character-profiles")
def list_character_profiles() -> dict[str, Any]:
    with db.connect() as conn:
        rows = conn.execute("select * from character_profiles order by updated_at desc, id desc").fetchall()
    return {"items": [serialize_character_profile_row(row) for row in rows]}


@app.post("/api/character-profiles")
def create_character_profile(request: CharacterProfileRequest) -> dict[str, Any]:
    stamp = db.now_iso()
    with db.connect() as conn:
        cursor = conn.execute(
            """
            insert into character_profiles
                (name, age, gender, appearance, wardrobe, personality, voice_style, signature_items, extra_prompt, notes, created_at, updated_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalize_image_title(request.name, fallback="未命名角色"),
                normalize_free_text(request.age, 120),
                normalize_free_text(request.gender, 120),
                normalize_free_text(request.appearance, 800),
                normalize_free_text(request.wardrobe, 800),
                normalize_free_text(request.personality, 600),
                normalize_free_text(request.voice_style, 400),
                normalize_free_text(request.signature_items, 400),
                normalize_free_text(request.extra_prompt, 800),
                normalize_free_text(request.notes, 1000),
                stamp,
                stamp,
            ),
        )
        row = conn.execute("select * from character_profiles where id = ?", (int(cursor.lastrowid),)).fetchone()
    return serialize_character_profile_row(row)


@app.put("/api/character-profiles/{profile_id}")
def update_character_profile(profile_id: int, request: CharacterProfileRequest) -> dict[str, Any]:
    with db.connect() as conn:
        cursor = conn.execute(
            """
            update character_profiles
            set name = ?, age = ?, gender = ?, appearance = ?, wardrobe = ?, personality = ?, voice_style = ?, signature_items = ?, extra_prompt = ?, notes = ?, updated_at = ?
            where id = ?
            """,
            (
                normalize_image_title(request.name, fallback="未命名角色"),
                normalize_free_text(request.age, 120),
                normalize_free_text(request.gender, 120),
                normalize_free_text(request.appearance, 800),
                normalize_free_text(request.wardrobe, 800),
                normalize_free_text(request.personality, 600),
                normalize_free_text(request.voice_style, 400),
                normalize_free_text(request.signature_items, 400),
                normalize_free_text(request.extra_prompt, 800),
                normalize_free_text(request.notes, 1000),
                db.now_iso(),
                profile_id,
            ),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="character profile not found")
        row = conn.execute("select * from character_profiles where id = ?", (profile_id,)).fetchone()
    return serialize_character_profile_row(row)


@app.delete("/api/character-profiles/{profile_id}")
def delete_character_profile(profile_id: int) -> dict[str, Any]:
    with db.connect() as conn:
        cursor = conn.execute("delete from character_profiles where id = ?", (profile_id,))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="character profile not found")
    return {"ok": True}


@app.get("/api/providers")
def list_providers() -> dict[str, Any]:
    items = load_provider_rows()
    pool = image_provider_pool_snapshot()
    pool_ids = {int(provider["id"]) for provider in pool["providers"]}
    pool_by_id = {int(provider["id"]): provider for provider in pool["providers"]}
    return {
        "items": [
            {
                **provider,
                "in_image_pool": int(provider["id"]) in pool_ids,
                "pool_assigned_tasks": pool_by_id.get(int(provider["id"]), {}).get("assigned_tasks", 0),
                "pool_running_tasks": pool_by_id.get(int(provider["id"]), {}).get("running_tasks", 0),
                "pool_idle_slots": pool_by_id.get(int(provider["id"]), {}).get("idle_slots", MAX_CONCURRENT_TASKS),
                "pool_available": pool_by_id.get(int(provider["id"]), {}).get("available", True),
                "pool_status": pool_by_id.get(int(provider["id"]), {}).get("status", "idle"),
                "pool_unavailable_seconds": pool_by_id.get(int(provider["id"]), {}).get("unavailable_seconds", 0),
                "pool_last_error": pool_by_id.get(int(provider["id"]), {}).get("last_error"),
            }
            for provider in items
        ],
        "image_provider_pool": pool,
    }


@app.post("/api/providers")
def create_provider(request: ProviderRequest) -> dict[str, Any]:
    stamp = db.now_iso()
    api_key = validate_api_key_text(request.api_key, field_label="提供商 API Key")
    with db.connect() as conn:
        cursor = conn.execute(
            """
            insert into providers (name, base_url, api_key, created_at, updated_at)
            values (?, ?, ?, ?, ?)
            """,
            (request.name.strip(), request.base_url.strip(), api_key, stamp, stamp),
        )
        provider_id = int(cursor.lastrowid)
        row = conn.execute("select * from providers where id = ?", (provider_id,)).fetchone()
    return db.row_to_dict(row)


@app.put("/api/providers/{provider_id}")
def update_provider(provider_id: int, request: ProviderRequest) -> dict[str, Any]:
    api_key = validate_api_key_text(request.api_key, field_label="提供商 API Key")
    with db.connect() as conn:
        cursor = conn.execute(
            """
            update providers
            set name = ?, base_url = ?, api_key = ?, updated_at = ?
            where id = ?
            """,
            (request.name.strip(), request.base_url.strip(), api_key, db.now_iso(), provider_id),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="provider not found")
        row = conn.execute("select * from providers where id = ?", (provider_id,)).fetchone()
    return db.row_to_dict(row)


@app.delete("/api/providers/{provider_id}")
def delete_provider(provider_id: int) -> dict[str, Any]:
    with db.connect() as conn:
        count = int(conn.execute("select count(*) as count from providers").fetchone()["count"])
        if count <= 1:
            raise HTTPException(status_code=400, detail="至少保留一个提供商")
        cursor = conn.execute("delete from providers where id = ?", (provider_id,))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="provider not found")
    return {"ok": True}


@app.get("/api/prompts")
def list_prompts(
    limit: int = 300,
    q: str = "",
    mode: str = "",
    favorite: int | None = None,
) -> dict[str, Any]:
    clauses: list[str] = []
    values: list[Any] = []
    if q.strip():
        clauses.append("content like ?")
        values.append(f"%{q.strip()}%")
    if mode.strip():
        clauses.append("mode = ?")
        values.append(mode.strip())
    if favorite is not None:
        clauses.append("favorite = ?")
        values.append(1 if int(favorite) else 0)
    where = f"where {' and '.join(clauses)}" if clauses else ""
    values.append(max(1, min(int(limit), 1000)))
    with db.connect() as conn:
        rows = conn.execute(
            f"select * from prompts {where} order by favorite desc, id desc limit ?",
            values,
        ).fetchall()
    return {"items": [db.row_to_dict(row) for row in rows]}


@app.post("/api/prompts")
def create_prompt(request: PromptRequest) -> dict[str, Any]:
    content = request.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="prompt content is required")
    prompt_id = db.add_prompt(content, source=request.source.strip() or "manual", mode=request.mode, favorite=request.favorite)
    with db.connect() as conn:
        row = conn.execute("select * from prompts where id = ?", (prompt_id,)).fetchone()
    return db.row_to_dict(row)


@app.put("/api/prompts/{prompt_id}")
def update_prompt(prompt_id: int, request: PromptRequest) -> dict[str, Any]:
    content = request.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="prompt content is required")
    with db.connect() as conn:
        cursor = conn.execute(
            """
            update prompts
            set content = ?, source = ?, mode = ?, favorite = ?, updated_at = ?
            where id = ?
            """,
            (content, request.source.strip() or "manual", request.mode, int(bool(request.favorite)), db.now_iso(), prompt_id),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="prompt not found")
        row = conn.execute("select * from prompts where id = ?", (prompt_id,)).fetchone()
    return db.row_to_dict(row)


@app.delete("/api/prompts/{prompt_id}")
def delete_prompt(prompt_id: int) -> dict[str, Any]:
    with db.connect() as conn:
        cursor = conn.execute("delete from prompts where id = ?", (prompt_id,))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="prompt not found")
    return {"ok": True}


@app.post("/api/images/generate")
async def generate_image(request: GenerateRequest) -> dict[str, Any]:
    db.add_prompt(request.prompt, source="auto", mode="generate")
    conversation_id: int | None = None
    if request.conversation_id:
        with db.connect() as conn:
            conversation = ensure_conversation_message_allowed(conn, int(request.conversation_id), "generate")
            conversation_id = int(conversation["id"])
    raw_variants = normalize_variant_plan([item.model_dump() for item in request.variant_plan])
    variant_entries = raw_variants or [
        {
            "name": "",
            "prompt_suffix": "",
            "quality": None,
            "size": None,
            "background": None,
            "output_format": None,
            "output_compression": None,
            "n": None,
            "image_title": "",
            "style_lock_id": None,
            "delay_seconds": 0,
        }
    ]
    schedule_at = normalize_schedule_at(request.schedule_at)
    should_schedule = bool(schedule_at or request.schedule_spacing_seconds > 0 or len(variant_entries) > 1)
    if not should_schedule:
        ensure_task_slot()

    batch_group = task_batch_group_id("generate", request.prompt) if should_schedule else None
    queue_total = len(variant_entries) if len(variant_entries) > 1 else None
    queue_label = normalize_image_title(request.batch_label or "批量变体任务", fallback="批量变体任务") if should_schedule else None
    scheduled_base = schedule_at or (db.now_iso() if should_schedule else None)
    created_tasks: list[dict[str, Any]] = []
    user_message_ids: list[int] = []

    for index, variant in enumerate(variant_entries, start=1):
        variant_name = normalize_image_title(variant.get("name") or "")
        scheduled_for = (
            add_seconds_to_iso(
                str(scheduled_base),
                int(request.schedule_spacing_seconds) * (index - 1) + int(variant.get("delay_seconds") or 0),
            )
            if scheduled_base
            else None
        )
        request_payload = request.model_dump()
        request_payload.update(
            {
                "image_title": normalize_image_title(variant.get("image_title") or request.image_title or variant_name or ""),
                "quality": variant.get("quality") or request.quality,
                "size": variant.get("size") or request.size,
                "background": variant.get("background") or request.background,
                "output_format": variant.get("output_format") or request.output_format,
                "output_compression": variant.get("output_compression") if variant.get("output_compression") is not None else request.output_compression,
                "n": int(variant.get("n") or request.n),
                "style_lock_id": int(variant.get("style_lock_id") or request.style_lock_id or 0) or None,
                "variant_plan": [],
                "schedule_at": None,
                "schedule_spacing_seconds": 0,
                "batch_label": None,
            }
        )
        task_request = GenerateRequest(**request_payload)
        user_message_id = create_direct_mode_user_message(
            conversation_id=conversation_id,
            prompt=request.prompt,
            meta_updates={
                "variant_name": variant_name,
                "variant_prompt_suffix": variant.get("prompt_suffix"),
                "style_lock_id": task_request.style_lock_id,
                "character_profile_ids": task_request.character_profile_ids,
                "scheduled_for": scheduled_for,
            },
        ) if conversation_id else None
        payload = compact_params(
            {
                "endpoint": "/v1/responses",
                "tool": "image_generation",
                "model": task_request.model,
                "image_model": task_request.image_model,
                "prompt": request.prompt,
                "image_title": normalize_image_title(task_request.image_title or ""),
                "size": task_request.size,
                "quality": task_request.quality,
                "n": task_request.n,
                "background": task_request.background,
                "output_format": task_request.output_format,
                "output_compression": task_request.output_compression,
                "moderation": task_request.moderation,
                "action": task_request.action,
                "partial_images": task_request.partial_images,
                "conversation_id": conversation_id,
                "style_lock_id": task_request.style_lock_id,
                "character_profile_ids": task_request.character_profile_ids,
                "variant_name": variant_name,
                "variant_prompt_suffix": normalize_free_text(variant.get("prompt_suffix"), 1200),
                "batch_label": queue_label,
            }
        )
        task_id = db.create_task(
            "generate",
            request.prompt,
            payload,
            status="scheduled" if should_schedule else "queued",
            conversation_id=conversation_id,
            user_message_id=user_message_id,
            scheduled_for=scheduled_for,
            queue_group=batch_group,
            queue_position=index if queue_total else None,
            queue_total=queue_total,
            queue_label=queue_label,
            variant_name=variant_name or None,
        )
        if should_schedule:
            db.update_task(
                task_id,
                stage=scheduled_task_stage(scheduled_for, queue_position=index if queue_total else None, queue_total=queue_total),
            )
            publish_task_snapshot(task_id)
        else:
            schedule_task(task_id, run_generate_task(task_id, task_request, payload, conversation_id=conversation_id, user_message_id=user_message_id))
        user_message_ids.append(int(user_message_id or 0))
        created_tasks.append(db.get_task(task_id) or task_with_images(task_id))

    if should_schedule:
        return {"tasks": created_tasks, "user_message_ids": [item for item in user_message_ids if item]}
    return {"task": created_tasks[0], "user_message_id": user_message_ids[0] if user_message_ids else None}


async def run_generate_task(
    task_id: int,
    request: GenerateRequest,
    payload: dict[str, Any],
    *,
    conversation_id: int | None = None,
    user_message_id: int | None = None,
    resume_checkpoint: dict[str, Any] | None = None,
    restored_images: list[dict[str, Any]] | None = None,
) -> None:
    async def worker() -> None:
        style_lock = load_style_lock(request.style_lock_id)
        character_profiles = load_character_profiles(request.character_profile_ids)
        variant_prompt_suffix = normalize_free_text(payload.get("variant_prompt_suffix"), 1200)
        effective_prompt = apply_locked_prompt(
            request.prompt,
            character_profiles=character_profiles,
            style_lock=style_lock,
            variant_prompt_suffix=variant_prompt_suffix,
        )
        conversation_title = conversation_title_for_naming(conversation_id, fallback=request.prompt[:20] or f"task-{task_id}")
        checkpoint = resume_checkpoint if isinstance(resume_checkpoint, dict) else {}
        base_title = str(checkpoint.get("base_title") or "").strip() or build_direct_mode_base_title(
            request.image_title,
            conversation_id=conversation_id,
            prompt=request.prompt,
            created_at=db.now_iso(),
        )
        bucket = str(checkpoint.get("bucket") or "").strip() or task_image_folder(task_id, conversation_title or request.prompt[:48] or f"task-{task_id}")
        responses: list[dict[str, Any]] = []
        saved_images: list[dict[str, Any]] = list(restored_images or [])
        provider_attempts: list[dict[str, Any]] = []
        last_provider: dict[str, Any] | None = None
        completed_count = min(max(int(checkpoint.get("completed_count") or len(saved_images)), 0), int(request.n))
        resume_requested = bool(checkpoint.get("can_resume") or checkpoint.get("manual_retry_requested_at"))
        persist_task_checkpoint(
            task_id,
            mode="generate",
            step="prepared",
            progress=12,
            stage=f"准备继续生成第 {completed_count + 1}/{request.n} 张" if completed_count or resume_requested else "准备开始生成图片",
            can_resume=completed_count > 0 or resume_requested,
            base_title=base_title,
            bucket=bucket,
            completed_count=completed_count,
            total_count=request.n,
        )
        if completed_count >= request.n and saved_images:
            db.update_task(task_id, progress=96, stage="已恢复全部已完成结果，正在整理任务结果")
        for index in range(completed_count, request.n):
            try:
                response, provider, attempt_log = await execute_with_provider_failover(
                    task_id,
                    lambda _provider, provider_config: call_responses_image_generation(
                        model=request.model,
                        prompt=effective_prompt,
                        image_model=request.image_model,
                        size=request.size,
                        quality=request.quality,
                        output_format=request.output_format,
                        background=request.background,
                        output_compression=request.output_compression,
                        moderation=request.moderation,
                        action=request.action,
                        partial_images=request.partial_images,
                        config=provider_config,
                        on_stable_retry=lambda quality: update_timeout_retry_stage(task_id, quality),
                        on_stream_event=lambda event: handle_image_stream_event(task_id, event),
                    ),
                    waiting_stage=lambda item, index=index: f"第 {index + 1}/{request.n} 张已分配到 {item['name']}，等待空闲通道",
                    running_stage=lambda item, index=index: f"正在使用 {item['name']} 生成第 {index + 1}/{request.n} 张",
                    retry_stage=lambda item, attempt, index=index: f"{item['name']} 暂不可用，正在重试第 {attempt}/{PROVIDER_UNAVAILABLE_RETRY_COUNT + 1} 次并继续生成第 {index + 1}/{request.n} 张",
                    switch_stage=lambda item, index=index: f"{item['name']} 连续不可用，正在切换下一个最佳提供商继续生成第 {index + 1}/{request.n} 张",
                )
                last_provider = provider
                if attempt_log:
                    provider_attempts.extend(attempt_log)
                responses.append(sanitize_response(response))
                image_items = extract_images_from_responses(response, request.output_format, folder=bucket)
                if not image_items:
                    raise HTTPException(
                        status_code=502,
                        detail={
                            "message": "Responses API 已返回，但没有找到 image_generation_call.result 图片数据。",
                            "endpoint": "responses",
                            "upstream": sanitize_response(response),
                            "suggestion": "请确认当前模型组合支持 image_generation 工具，或更换外层模型/图片工具模型后重试。",
                            "provider_attempts": provider_attempts,
                        },
                    )
                for item_offset, item in enumerate(image_items, start=1):
                    sequence_index = len(saved_images) + 1
                    resolved_title = build_sequenced_title(base_title, sequence_index, max(int(request.n), 1))
                    renamed = rename_output_image(item, resolved_title, fallback_stem=f"task-{task_id}")
                    saved_images.append(
                        public_task_image(
                            renamed,
                            task_id=task_id,
                            title=resolved_title,
                            bucket=bucket,
                            conversation_id=conversation_id,
                            message_id=user_message_id,
                        )
                    )
            except HTTPException as exc:
                provider_attempts = merge_provider_attempt_logs(provider_attempts, exc.detail)
                persist_task_checkpoint(
                    task_id,
                    mode="generate",
                    step="image_waiting",
                    progress=min(25 + int(index / max(request.n, 1) * 60), 90),
                    stage=f"第 {index + 1}/{request.n} 张生成失败，可从这里继续",
                    can_resume=True,
                    base_title=base_title,
                    bucket=bucket,
                    completed_count=len(saved_images),
                    total_count=request.n,
                    current_image_index=index + 1,
                    provider_attempts=provider_attempts,
                    last_error=exc.detail,
                )
                raise
            except Exception as exc:
                persist_task_checkpoint(
                    task_id,
                    mode="generate",
                    step="image_waiting",
                    progress=min(25 + int(index / max(request.n, 1) * 60), 90),
                    stage=f"第 {index + 1}/{request.n} 张生成失败，可从这里继续",
                    can_resume=True,
                    base_title=base_title,
                    bucket=bucket,
                    completed_count=len(saved_images),
                    total_count=request.n,
                    current_image_index=index + 1,
                    provider_attempts=provider_attempts,
                    last_error=str(exc),
                )
                raise
            progress = min(25 + int((index + 1) / max(request.n, 1) * 60), 90)
            persist_task_checkpoint(
                task_id,
                mode="generate",
                step="image_saved",
                progress=progress,
                stage=f"已通过 {provider['name']} 保存第 {index + 1}/{request.n} 张结果",
                can_resume=(index + 1) < request.n,
                base_title=base_title,
                bucket=bucket,
                completed_count=len(saved_images),
                total_count=request.n,
                provider_attempts=provider_attempts,
            )
        if not saved_images:
            raise HTTPException(
                status_code=502,
                detail={
                    "message": "Responses API 已返回，但没有找到 image_generation_call.result 图片数据。",
                    "endpoint": "responses",
                    "upstream": responses,
                    "suggestion": "请确认当前模型组合支持 image_generation 工具，或更换外层模型/图片工具模型后重试。",
                    "provider_attempts": provider_attempts,
                },
            )
        persist_task_checkpoint(
            task_id,
            mode="generate",
            step="finalizing",
            progress=96,
            stage="正在整理任务结果",
            can_resume=False,
            base_title=base_title,
            bucket=bucket,
            completed_count=len(saved_images),
            total_count=request.n,
            provider_attempts=provider_attempts,
        )
        raw = {
            "endpoint": "/v1/responses",
            "tool": "image_generation",
            "image_prompt": effective_prompt,
            "style_lock": style_lock,
            "character_profiles": character_profiles,
            "variant_name": payload.get("variant_name"),
            "image_provider": {"id": last_provider["id"], "name": last_provider["name"]} if last_provider else None,
            "provider_attempts": provider_attempts,
            "responses": responses,
            "images": saved_images,
        }
        db.finish_task(task_id, raw)
    await run_with_slot(task_id, worker)


@app.post("/api/images/edit")
async def edit_image(
    params_json: str = Form(...),
    images: list[UploadFile] = File(...),
    mask: UploadFile | None = File(default=None),
) -> dict[str, Any]:
    params = normalize_text_fields(parse_params(params_json), keys=("prompt", "image_title"))
    prompt = str(params.get("prompt") or "")
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")
    if not images:
        raise HTTPException(status_code=400, detail="images is required")
    upload_selection_modes = normalize_edit_upload_selection_modes(params.get("upload_selection_modes"), len(images))
    params["upload_selection_modes"] = upload_selection_modes
    db.add_prompt(prompt, source="auto", mode="edit")
    raw_variants = normalize_variant_plan(params.get("variant_plan"))
    variant_entries = raw_variants or [
        {
            "name": "",
            "prompt_suffix": "",
            "quality": None,
            "size": None,
            "background": None,
            "output_format": None,
            "output_compression": None,
            "n": None,
            "image_title": "",
            "style_lock_id": None,
            "delay_seconds": 0,
        }
    ]
    schedule_at = normalize_schedule_at(params.get("schedule_at"))
    schedule_spacing_seconds = max(0, int(params.get("schedule_spacing_seconds") or 0))
    should_schedule = bool(schedule_at or schedule_spacing_seconds > 0 or len(variant_entries) > 1)
    if not should_schedule:
        ensure_task_slot()

    saved_images = [await save_upload(upload) for upload in images]
    saved_mask: tuple[Path, str] | None = None
    if mask is not None:
        mask_path, mask_mime = await save_upload(mask)
        saved_mask = (mask_path, mask_mime)
    conversation_id = params.get("conversation_id")
    if conversation_id is not None:
        try:
            conversation_id = int(conversation_id)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="conversation_id is invalid")
    if conversation_id:
        with db.connect() as conn:
            conversation = ensure_conversation_message_allowed(conn, int(conversation_id), "edit")
            conversation_id = int(conversation["id"])
    uploads_for_message = [*saved_images, *([saved_mask] if saved_mask else [])]
    batch_group = task_batch_group_id("edit", prompt) if should_schedule else None
    queue_total = len(variant_entries) if len(variant_entries) > 1 else None
    queue_label = normalize_image_title(params.get("batch_label") or "批量改图任务", fallback="批量改图任务") if should_schedule else None
    scheduled_base = schedule_at or (db.now_iso() if should_schedule else None)
    created_tasks: list[dict[str, Any]] = []
    user_message_ids: list[int] = []

    for index, variant in enumerate(variant_entries, start=1):
        variant_name = normalize_image_title(variant.get("name") or "")
        scheduled_for = (
            add_seconds_to_iso(
                str(scheduled_base),
                schedule_spacing_seconds * (index - 1) + int(variant.get("delay_seconds") or 0),
            )
            if scheduled_base
            else None
        )
        task_params = compact_params(
            {
                "endpoint": "/v1/responses",
                "tool": "image_generation",
                "model": params.get("model", "gpt-5.4"),
                "image_model": params.get("image_model", "gpt-image-2"),
                "prompt": prompt,
                "image_title": normalize_image_title(variant.get("image_title") or params.get("image_title") or variant_name or ""),
                "size": variant.get("size") or params.get("size", "2560x1440"),
                "quality": variant.get("quality") or params.get("quality", "high"),
                "n": clamp_image_count(variant.get("n") or params.get("n", 1)),
                "background": variant.get("background") or params.get("background", "auto"),
                "output_format": variant.get("output_format") or params.get("output_format", "png"),
                "output_compression": variant.get("output_compression") if variant.get("output_compression") is not None else params.get("output_compression"),
                "moderation": params.get("moderation", "auto"),
                "input_fidelity": params.get("input_fidelity", "auto"),
                "action": "edit",
                "partial_images": params.get("partial_images"),
                "upload_selection_modes": upload_selection_modes,
                "conversation_id": conversation_id,
                "style_lock_id": int(variant.get("style_lock_id") or params.get("style_lock_id") or 0) or None,
                "character_profile_ids": normalize_profile_id_list(params.get("character_profile_ids")),
                "variant_name": variant_name,
                "variant_prompt_suffix": normalize_free_text(variant.get("prompt_suffix"), 1200),
                "batch_label": queue_label,
            }
        )
        user_message_id = create_direct_mode_user_message(
            conversation_id=conversation_id,
            prompt=prompt,
            uploads=uploads_for_message,
            meta_updates={
                "variant_name": variant_name,
                "variant_prompt_suffix": variant.get("prompt_suffix"),
                "style_lock_id": task_params.get("style_lock_id"),
                "character_profile_ids": task_params.get("character_profile_ids"),
                "scheduled_for": scheduled_for,
            },
        ) if conversation_id else None
        task_id = db.create_task(
            "edit",
            prompt,
            task_params,
            status="scheduled" if should_schedule else "queued",
            conversation_id=conversation_id,
            user_message_id=user_message_id,
            scheduled_for=scheduled_for,
            queue_group=batch_group,
            queue_position=index if queue_total else None,
            queue_total=queue_total,
            queue_label=queue_label,
            variant_name=variant_name or None,
        )
        for item in saved_images:
            public_input_image(item, source="input", title=prompt, task_id=task_id, conversation_id=conversation_id, message_id=user_message_id)
        if saved_mask:
            public_input_image(saved_mask, source="mask", title=f"{prompt} mask", task_id=task_id, conversation_id=conversation_id, message_id=user_message_id)
        if should_schedule:
            db.update_task(
                task_id,
                stage=scheduled_task_stage(scheduled_for, queue_position=index if queue_total else None, queue_total=queue_total),
            )
            publish_task_snapshot(task_id)
        else:
            schedule_task(task_id, run_edit_task(task_id, task_params, prompt, saved_images, saved_mask, conversation_id=conversation_id, user_message_id=user_message_id))
        user_message_ids.append(int(user_message_id or 0))
        created_tasks.append(db.get_task(task_id) or task_with_images(task_id))

    if should_schedule:
        return {"tasks": created_tasks, "user_message_ids": [item for item in user_message_ids if item]}
    return {"task": created_tasks[0], "user_message_id": user_message_ids[0] if user_message_ids else None}


async def run_edit_task(
    task_id: int,
    params: dict[str, Any],
    prompt: str,
    saved_images: list[tuple[Path, str]],
    saved_mask: tuple[Path, str] | None,
    *,
    conversation_id: int | None = None,
    user_message_id: int | None = None,
    resume_checkpoint: dict[str, Any] | None = None,
    restored_images: list[dict[str, Any]] | None = None,
) -> None:
    async def worker() -> None:
        style_lock = load_style_lock(params.get("style_lock_id"))
        character_profiles = load_character_profiles(params.get("character_profile_ids"))
        variant_prompt_suffix = normalize_free_text(params.get("variant_prompt_suffix"), 1200)
        effective_prompt = apply_locked_prompt(
            prompt,
            character_profiles=character_profiles,
            style_lock=style_lock,
            variant_prompt_suffix=variant_prompt_suffix,
        )
        conversation_title = conversation_title_for_naming(conversation_id, fallback=prompt[:20] or f"task-{task_id}")
        checkpoint = resume_checkpoint if isinstance(resume_checkpoint, dict) else {}
        base_title = str(checkpoint.get("base_title") or "").strip() or build_direct_mode_base_title(
            str(params.get("image_title") or ""),
            conversation_id=conversation_id,
            prompt=prompt,
            created_at=db.now_iso(),
        )
        bucket = str(checkpoint.get("bucket") or "").strip() or task_image_folder(task_id, conversation_title or prompt[:48] or f"task-{task_id}")
        output_format = str(params.get("output_format", "png"))
        responses: list[dict[str, Any]] = []
        saved_output_images: list[dict[str, Any]] = list(restored_images or [])
        count = clamp_image_count(params.get("n", 1))
        provider_attempts: list[dict[str, Any]] = []
        last_provider: dict[str, Any] | None = None
        edit_inputs, input_image_notes, reference_candidates, target_candidates = build_edit_input_bundle(
            saved_images,
            params.get("upload_selection_modes") if isinstance(params.get("upload_selection_modes"), list) else None,
        )
        completed_count = min(max(int(checkpoint.get("completed_count") or len(saved_output_images)), 0), count)
        resume_requested = bool(checkpoint.get("can_resume") or checkpoint.get("manual_retry_requested_at"))
        persist_task_checkpoint(
            task_id,
            mode="edit",
            step="prepared",
            progress=12,
            stage=f"准备继续编辑第 {completed_count + 1}/{count} 张" if completed_count or resume_requested else "准备开始编辑图片",
            can_resume=completed_count > 0 or resume_requested,
            base_title=base_title,
            bucket=bucket,
            completed_count=completed_count,
            total_count=count,
        )
        if completed_count >= count and saved_output_images:
            db.update_task(task_id, progress=96, stage="已恢复全部已完成编辑结果，正在整理任务结果")
        for index in range(completed_count, count):
            try:
                response, provider, attempt_log = await execute_with_provider_failover(
                    task_id,
                    lambda _provider, client_config: call_responses_image_generation(
                        model=str(params.get("model", "gpt-5.4")),
                        prompt=effective_prompt,
                        image_model=str(params.get("image_model", "gpt-image-2")),
                        size=str(params.get("size", "2560x1440")),
                        quality=str(params.get("quality", "high")),
                        output_format=output_format,
                        background=params.get("background", "auto"),
                        output_compression=params.get("output_compression"),
                        moderation=params.get("moderation", "auto"),
                        action="edit",
                        partial_images=params.get("partial_images"),
                        config=client_config,
                        uploaded=edit_inputs,
                        mask=saved_mask,
                        input_fidelity=str(params.get("input_fidelity", "auto")),
                        input_image_notes=input_image_notes,
                        on_stable_retry=lambda quality: update_timeout_retry_stage(task_id, quality),
                        on_stream_event=lambda event: handle_image_stream_event(task_id, event),
                    ),
                    waiting_stage=lambda item, index=index: f"第 {index + 1}/{count} 张已分配到 {item['name']}，等待空闲通道",
                    running_stage=lambda item, index=index: f"正在使用 {item['name']} 编辑第 {index + 1}/{count} 张",
                    retry_stage=lambda item, attempt, index=index: f"{item['name']} 暂不可用，正在重试第 {attempt}/{PROVIDER_UNAVAILABLE_RETRY_COUNT + 1} 次并继续编辑第 {index + 1}/{count} 张",
                    switch_stage=lambda item, index=index: f"{item['name']} 连续不可用，正在切换下一个最佳提供商继续编辑第 {index + 1}/{count} 张",
                )
                last_provider = provider
                if attempt_log:
                    provider_attempts.extend(attempt_log)
                responses.append(sanitize_response(response))
                image_items = extract_images_from_responses(response, output_format, folder=bucket)
                if not image_items:
                    raise HTTPException(
                        status_code=502,
                        detail={
                            "message": "Responses API 已返回，但没有找到 image_generation_call.result 图片数据。",
                            "endpoint": "responses",
                            "upstream": sanitize_response(response),
                            "suggestion": "请确认当前模型组合支持 image_generation 工具，或更换外层模型/图片工具模型后重试。",
                            "provider_attempts": provider_attempts,
                        },
                    )
                for item in image_items:
                    sequence_index = len(saved_output_images) + 1
                    resolved_title = build_sequenced_title(base_title, sequence_index, max(count, 1))
                    renamed = rename_output_image(item, resolved_title, fallback_stem=f"task-{task_id}")
                    saved_output_images.append(
                        public_task_image(
                            renamed,
                            task_id=task_id,
                            title=resolved_title,
                            bucket=bucket,
                            conversation_id=conversation_id,
                            message_id=user_message_id,
                        )
                    )
            except HTTPException as exc:
                provider_attempts = merge_provider_attempt_logs(provider_attempts, exc.detail)
                persist_task_checkpoint(
                    task_id,
                    mode="edit",
                    step="image_waiting",
                    progress=min(25 + int(index / max(count, 1) * 60), 90),
                    stage=f"第 {index + 1}/{count} 张编辑失败，可从这里继续",
                    can_resume=True,
                    base_title=base_title,
                    bucket=bucket,
                    completed_count=len(saved_output_images),
                    total_count=count,
                    current_image_index=index + 1,
                    provider_attempts=provider_attempts,
                    last_error=exc.detail,
                )
                raise
            except Exception as exc:
                persist_task_checkpoint(
                    task_id,
                    mode="edit",
                    step="image_waiting",
                    progress=min(25 + int(index / max(count, 1) * 60), 90),
                    stage=f"第 {index + 1}/{count} 张编辑失败，可从这里继续",
                    can_resume=True,
                    base_title=base_title,
                    bucket=bucket,
                    completed_count=len(saved_output_images),
                    total_count=count,
                    current_image_index=index + 1,
                    provider_attempts=provider_attempts,
                    last_error=str(exc),
                )
                raise
            progress = min(25 + int((index + 1) / max(count, 1) * 60), 90)
            persist_task_checkpoint(
                task_id,
                mode="edit",
                step="image_saved",
                progress=progress,
                stage=f"已通过 {provider['name']} 保存第 {index + 1}/{count} 张编辑结果",
                can_resume=(index + 1) < count,
                base_title=base_title,
                bucket=bucket,
                completed_count=len(saved_output_images),
                total_count=count,
                provider_attempts=provider_attempts,
            )
        if not saved_output_images:
            raise HTTPException(
                status_code=502,
                detail={
                    "message": "Responses API 已返回，但没有找到 image_generation_call.result 图片数据。",
                    "endpoint": "responses",
                    "upstream": responses,
                    "suggestion": "请确认当前模型组合支持 image_generation 工具，或更换外层模型/图片工具模型后重试。",
                    "provider_attempts": provider_attempts,
                },
            )
        persist_task_checkpoint(
            task_id,
            mode="edit",
            step="finalizing",
            progress=96,
            stage="正在整理任务结果",
            can_resume=False,
            base_title=base_title,
            bucket=bucket,
            completed_count=len(saved_output_images),
            total_count=count,
            provider_attempts=provider_attempts,
        )
        raw = {
            "endpoint": "/v1/responses",
            "tool": "image_generation",
            "image_prompt": effective_prompt,
            "style_lock": style_lock,
            "character_profiles": character_profiles,
            "variant_name": params.get("variant_name"),
            "image_provider": {"id": last_provider["id"], "name": last_provider["name"]} if last_provider else None,
            "provider_attempts": provider_attempts,
            "selected_reference_image_refs": candidate_refs(reference_candidates),
            "edit_target_image_refs": candidate_refs(target_candidates),
            "input_image_notes": input_image_notes,
            "responses": responses,
            "images": saved_output_images,
        }
        db.finish_task(task_id, raw)
    await run_with_slot(task_id, worker)


@app.get("/api/tasks")
def list_tasks(limit: int = 30) -> dict[str, Any]:
    with db.connect() as conn:
        tasks = [
            summarize_task(row, include_response=False)
            for row in conn.execute(
                "select * from tasks order by id desc limit ?",
                (limit,),
            ).fetchall()
        ]
    pool = image_provider_pool_snapshot()
    return {
        "items": tasks,
        "max_concurrent": pool["total_capacity"],
        "active_count": active_task_count(),
        "image_provider_pool": pool,
    }


@app.get("/api/tasks/{task_id}")
def get_task(task_id: int) -> dict[str, Any]:
    return {"task": task_with_images(task_id)}


@app.get("/api/tasks/{task_id}/events")
async def task_events(task_id: int, request: Request) -> StreamingResponse:
    if not db.get_task(task_id):
        raise HTTPException(status_code=404, detail="task not found")

    runtime_key = task_runtime_key(task_id)
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
    TASK_EVENT_SUBSCRIBERS.setdefault(runtime_key, set()).add(queue)

    async def stream() -> Any:
        try:
            yield sse_format("connected", {"task_id": task_id})
            for payload in TASK_EVENT_SNAPSHOTS.get(runtime_key, {}).values():
                yield sse_format(str(payload["event"]), payload["data"])
                if payload["event"] in {"done", "failed", "canceled"}:
                    return
            publish_task_snapshot(task_id)
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield sse_format(str(payload["event"]), payload["data"])
                if payload["event"] in {"done", "failed", "canceled"}:
                    break
        finally:
            TASK_EVENT_SUBSCRIBERS.get(runtime_key, set()).discard(queue)
            if not TASK_EVENT_SUBSCRIBERS.get(runtime_key):
                TASK_EVENT_SUBSCRIBERS.pop(runtime_key, None)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: int) -> dict[str, Any]:
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    cancel_running_task(task_id)
    with db.connect() as conn:
        image_rows = conn.execute("select * from images where task_id = ?", (task_id,)).fetchall()
        image_ids = [int(row["id"]) for row in image_rows]
        media_paths = deletable_media_paths(image_rows, image_ids)
        conn.execute("delete from images where task_id = ?", (task_id,))
        conn.execute("delete from tasks where id = ?", (task_id,))
    safe_delete_media_files(media_paths)
    return {"ok": True}


@app.post("/api/tasks/{task_id}/cancel")
def cancel_task(task_id: int) -> dict[str, Any]:
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    if task["status"] in {"done", "failed", "canceled"}:
        return {"task": task_with_images(task_id)}
    running = RUNNING_TASKS.get(task_runtime_key(task_id))
    if running:
        running.cancel()
    db.cancel_task(task_id)
    merge_task_checkpoint_state(
        task_id,
        stage="已停止",
        last_status="canceled",
        last_error="用户已停止任务",
    )
    publish_task_snapshot(task_id)
    publish_task_event(task_id, "canceled", {"task_id": task_id}, snapshot=False)
    return {"task": task_with_images(task_id)}


@app.post("/api/conversations")
def create_conversation(request: ConversationCreate) -> dict[str, Any]:
    stamp = db.now_iso()
    mode = normalize_conversation_mode(request.mode)
    title = request.title.strip() or "新的生图对话"
    with db.connect() as conn:
        cursor = conn.execute(
            """
            insert into conversations (title, mode, context_limit, created_at, updated_at)
            values (?, ?, ?, ?, ?)
            """,
            (title, mode, request.context_limit, stamp, stamp),
        )
        conversation_id = int(cursor.lastrowid)
        row = conn.execute("select * from conversations where id = ?", (conversation_id,)).fetchone()
    return serialize_conversation_row(row)


@app.get("/api/conversations")
def list_conversations() -> dict[str, Any]:
    with db.connect() as conn:
        rows = conn.execute(
            """
            select c.*,
                (select count(*) from messages m where m.conversation_id = c.id) as message_count,
                (select count(*) from images i where i.conversation_id = c.id and i.source = 'api') as image_count,
                (
                    select t.mode from tasks t
                    where t.conversation_id = c.id
                    order by t.id desc
                    limit 1
                ) as latest_task_mode,
                (
                    select t.status from tasks t
                    where t.conversation_id = c.id
                    order by t.id desc
                    limit 1
                ) as latest_task_status,
                (
                    select t.progress from tasks t
                    where t.conversation_id = c.id
                    order by t.id desc
                    limit 1
                ) as latest_task_progress,
                (
                    select t.stage from tasks t
                    where t.conversation_id = c.id
                    order by t.id desc
                    limit 1
                ) as latest_task_stage,
                (
                    select t.id from tasks t
                    where t.conversation_id = c.id
                    order by t.id desc
                    limit 1
                ) as latest_task_id
            from conversations c
            order by c.updated_at desc
            """
        ).fetchall()
    return {"items": [serialize_conversation_row(row, latest_task_mode=row["latest_task_mode"]) for row in rows]}


@app.put("/api/conversations/{conversation_id}")
def update_conversation(conversation_id: int, request: ConversationUpdate) -> dict[str, Any]:
    updates: list[str] = []
    values: list[Any] = []
    if request.title is not None:
        title = request.title.strip() or "未命名对话"
        updates.append("title = ?")
        values.append(title)
    if request.context_limit is not None:
        updates.append("context_limit = ?")
        values.append(request.context_limit)
    if not updates:
        return get_conversation(conversation_id)["conversation"]
    updates.append("updated_at = ?")
    values.append(db.now_iso())
    values.append(conversation_id)
    with db.connect() as conn:
        cursor = conn.execute(
            f"update conversations set {', '.join(updates)} where id = ?",
            values,
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="conversation not found")
        row = conn.execute("select * from conversations where id = ?", (conversation_id,)).fetchone()
    return serialize_conversation_row(row)


@app.delete("/api/conversations/{conversation_id}")
def delete_conversation(conversation_id: int) -> dict[str, Any]:
    with db.connect() as conn:
        conversation = conn.execute("select * from conversations where id = ?", (conversation_id,)).fetchone()
        if not conversation:
            raise HTTPException(status_code=404, detail="conversation not found")
        task_rows = conn.execute("select id from tasks where conversation_id = ?", (conversation_id,)).fetchall()
        task_ids = [int(row["id"]) for row in task_rows]
        for task_id in task_ids:
            cancel_running_task(task_id)
        image_rows = conn.execute(
            """
            select * from images
            where conversation_id = ?
               or task_id in (select id from tasks where conversation_id = ?)
            """,
            (conversation_id, conversation_id),
        ).fetchall()
        image_ids = [int(row["id"]) for row in image_rows]
        media_paths = deletable_media_paths(image_rows, image_ids)
        conn.execute(
            """
            delete from images
            where conversation_id = ?
               or task_id in (select id from tasks where conversation_id = ?)
            """,
            (conversation_id, conversation_id),
        )
        conn.execute("delete from messages where conversation_id = ?", (conversation_id,))
        conn.execute("delete from tasks where conversation_id = ?", (conversation_id,))
        conn.execute("delete from conversations where id = ?", (conversation_id,))
    safe_delete_media_files(media_paths)
    return {"ok": True}


@app.get("/api/conversations/{conversation_id}")
def get_conversation(conversation_id: int) -> dict[str, Any]:
    with db.connect() as conn:
        conversation = conn.execute(
            "select * from conversations where id = ?",
            (conversation_id,),
        ).fetchone()
        if not conversation:
            raise HTTPException(status_code=404, detail="conversation not found")
        messages = [
            db.row_to_dict(row)
            for row in conn.execute(
                "select * from messages where conversation_id = ? order by id asc",
                (conversation_id,),
            ).fetchall()
        ]
        images = [
            serialize_image_record(row, conn)
            for row in conn.execute(
                "select * from images where conversation_id = ? and source = 'api' order by id asc",
                (conversation_id,),
            ).fetchall()
        ]
        message_upload_rows = [
            db.row_to_dict(row)
            for row in conn.execute(
                """
                select i.*,
                       m.content as message_content,
                       t.prompt as task_prompt,
                       t.mode as task_mode
                from images i
                left join messages m on m.id = i.message_id
                left join tasks t on t.id = i.task_id
                where i.conversation_id = ? and i.source != 'api'
                order by i.id asc
                """,
                (conversation_id,),
            ).fetchall()
        ]
        tasks = [
            summarize_task(row, include_response=False)
            for row in conn.execute(
                "select * from tasks where conversation_id = ? order by id asc",
                (conversation_id,),
            ).fetchall()
        ]
    message_upload_map: dict[tuple[int, str], dict[str, Any]] = {}
    message_upload_rows_by_message: dict[int, list[dict[str, Any]]] = {}
    for item in message_upload_rows:
        message_id = item.get("message_id")
        file_path = str(item.get("file_path") or "").strip()
        if not message_id or not file_path:
            continue
        message_upload_map[(int(message_id), file_path)] = item
        message_upload_rows_by_message.setdefault(int(message_id), []).append(item)
    for message in messages:
        uploaded_images = []
        try:
            meta = json.loads(message.get("meta_json") or "{}")
        except json.JSONDecodeError:
            meta = {}
        message_id = int(message.get("id") or 0)
        seen_upload_paths: set[str] = set()
        for path_value in meta.get("uploads", []) if isinstance(meta, dict) else []:
            normalized_path = str(path_value or "").strip()
            item = public_message_upload_image(message_upload_map.get((message_id, normalized_path), {}))
            if item:
                seen_upload_paths.add(str(item.get("file_path") or "").strip())
                uploaded_images.append(item)
                continue
            fallback = public_upload_image(normalized_path)
            if fallback:
                fallback["source"] = "input"
                seen_upload_paths.add(str(fallback.get("file_path") or "").strip())
                uploaded_images.append(fallback)
        for row in message_upload_rows_by_message.get(message_id, []):
            if row.get("source") == "input_reference":
                continue
            file_path = str(row.get("file_path") or "").strip()
            if not file_path or file_path in seen_upload_paths:
                continue
            item = public_message_upload_image(row)
            if item:
                seen_upload_paths.add(file_path)
                uploaded_images.append(item)
        reference_ids = meta.get("reference_image_ids", []) if isinstance(meta, dict) else []
        for image in load_selected_reference_images(reference_ids, limit=3, conversation_id=conversation_id):
            serialized_reference = serialize_image_record(image)
            uploaded_images.append(
                {
                    "id": serialized_reference["id"],
                    "url": serialized_reference["thumb_url"] or serialized_reference["public_url"],
                    "public_url": serialized_reference["public_url"],
                    "thumb_url": serialized_reference["thumb_url"],
                    "medium_url": serialized_reference["medium_url"],
                    "file_path": serialized_reference["file_path"],
                    "filename": serialized_reference["filename"],
                    "mime_type": serialized_reference["mime_type"],
                    "source": "input_reference",
                    "origin_source": image.get("source") or "api",
                    "title": serialized_reference.get("title"),
                    "task_mode": image.get("task_mode"),
                    "prompt_text": image.get("task_prompt") or image.get("message_content") or serialized_reference.get("title"),
                    "width": serialized_reference.get("width"),
                    "height": serialized_reference.get("height"),
                    "byte_size": serialized_reference.get("byte_size"),
                }
            )
        message["uploaded_images"] = uploaded_images
    task_map = {int(task["id"]): task for task in tasks}
    images = enrich_images_with_prompt(images, None)
    for image in images:
        task = task_map.get(int(image["task_id"])) if image.get("task_id") else None
        if task:
            enrich_images_with_prompt([image], task)
    latest_task_mode = tasks[-1]["mode"] if tasks else None
    return {"conversation": serialize_conversation_row(conversation, latest_task_mode=latest_task_mode), "messages": messages, "images": images, "tasks": tasks}


@app.put("/api/messages/{message_id}")
def update_message(message_id: int, request: MessageUpdate) -> dict[str, Any]:
    stamp = db.now_iso()
    with db.connect() as conn:
        row = conn.execute("select * from messages where id = ?", (message_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="message not found")
        conn.execute(
            "update messages set content = ?, updated_at = ? where id = ?",
            (request.content, stamp, message_id),
        )
        conn.execute(
            "update conversations set updated_at = ? where id = ?",
            (stamp, row["conversation_id"]),
        )
        updated = conn.execute("select * from messages where id = ?", (message_id,)).fetchone()
    return db.row_to_dict(updated)


@app.post("/api/storyboards/{conversation_id}/messages")
async def storyboard_message(
    conversation_id: int,
    params_json: str = Form(...),
    images: list[UploadFile] | None = File(default=None),
) -> dict[str, Any]:
    ensure_task_slot()
    params = StoryboardRequest(**normalize_text_fields(parse_params(params_json)))
    if len(images or []) > 3:
        raise HTTPException(status_code=400, detail="分镜模式最多上传 3 张参考图")
    with db.connect() as conn:
        conversation = ensure_conversation_message_allowed(conn, conversation_id, "storyboard")
        previous_response_id = conversation["previous_response_id"]
        conversation_title = conversation["title"]
        context_limit = max(0, min(int(params.context_limit), 50))
    uploaded = [await save_upload(upload) for upload in images or []]
    selected_reference_images = require_selected_reference_images(
        params.reference_image_ids,
        limit=max(0, 3 - len(uploaded)),
        conversation_id=conversation_id,
        label="分镜参考图",
    )
    selected_reference_uploads = [
        (Path(item["file_path"]), item.get("mime_type") or "image/png")
        for item in selected_reference_images
    ]
    image_candidates = [
        *build_uploaded_image_candidates(uploaded, params.upload_reference_roles, params.upload_selection_modes),
        *build_selected_image_candidates(
            selected_reference_images,
            params.reference_image_roles,
            params.reference_image_selection_modes,
            start_order=len(uploaded) + 1,
        ),
    ]
    db.add_prompt(params.prompt, source="auto", mode="storyboard")

    with db.connect() as conn:
        recent_messages = load_recent_messages(conversation_id, context_limit)
        cursor = conn.execute(
            """
            insert into messages (conversation_id, role, content, meta_json, created_at)
            values (?, ?, ?, ?, ?)
            """,
            (
                conversation_id,
                "user",
                params.prompt,
                db.json_dumps(
                    {
                        "uploads": [str(path) for path, _ in uploaded],
                        "reference_image_ids": [item["id"] for item in selected_reference_images],
                        "reference_image_roles": params.reference_image_roles,
                        "reference_image_selection_modes": params.reference_image_selection_modes,
                        "upload_reference_roles": params.upload_reference_roles,
                        "upload_selection_modes": params.upload_selection_modes,
                        "context_limit": context_limit,
                        "mode": "storyboard",
                        "style_lock_id": params.style_lock_id,
                        "character_profile_ids": params.character_profile_ids,
                    }
                ),
                db.now_iso(),
            ),
        )
        user_message_id = int(cursor.lastrowid)
        conn.execute(
            "update conversations set context_limit = ?, updated_at = ? where id = ?",
            (context_limit, db.now_iso(), conversation_id),
        )

    task_params = compact_params(
        {
            "endpoint": "/v1/responses",
            "tool": "image_generation",
            "model": params.model,
            "planner_model": params.planner_model,
            "planner_endpoint": params.planner_endpoint,
            "image_model": params.image_model,
            "prompt": params.prompt,
            "size": params.size,
            "quality": params.quality,
            "background": params.background,
            "output_format": params.output_format,
            "output_compression": params.output_compression,
            "moderation": params.moderation,
            "input_fidelity": params.input_fidelity,
            "partial_images": params.partial_images,
            "context_limit": context_limit,
            "shot_limit": params.shot_limit,
            "reference_image_ids": [item["id"] for item in selected_reference_images],
            "reference_image_roles": params.reference_image_roles,
            "reference_image_selection_modes": params.reference_image_selection_modes,
            "upload_reference_roles": params.upload_reference_roles,
            "upload_selection_modes": params.upload_selection_modes,
            "style_lock_id": params.style_lock_id,
            "character_profile_ids": params.character_profile_ids,
            "seed_images": serialize_seed_images(image_candidates),
            "planner_config": params.planner_config.model_dump() if params.planner_config else None,
        }
    )
    task_id = db.create_task(
        "storyboard",
        params.prompt,
        task_params,
        conversation_id=conversation_id,
        user_message_id=user_message_id,
    )
    for item in uploaded:
        public_input_image(item, source="input", title=params.prompt, task_id=task_id, conversation_id=conversation_id, message_id=user_message_id)
    for item in selected_reference_uploads:
        public_input_image(item, source="input_reference", title=params.prompt, task_id=task_id, conversation_id=conversation_id, message_id=user_message_id)
    schedule_task(
        task_id,
        run_storyboard_task(
            task_id,
            conversation_id,
            user_message_id,
            params,
            image_candidates,
            previous_response_id,
            conversation_title,
            recent_messages,
            context_limit,
            task_params,
        ),
    )
    return {"task": db.get_task(task_id), "user_message_id": user_message_id}


async def run_storyboard_task(
    task_id: int,
    conversation_id: int,
    user_message_id: int,
    params: StoryboardRequest,
    image_candidates: list[dict[str, Any]],
    previous_response_id: str | None,
    conversation_title: str,
    recent_messages: list[dict[str, Any]],
    context_limit: int,
    task_payload: dict[str, Any],
) -> None:
    async def worker() -> None:
        style_lock = load_style_lock(params.style_lock_id)
        character_profiles = load_character_profiles(params.character_profile_ids)
        db.update_task(task_id, progress=12, stage="AI 正在规划连续分镜")
        planner_reference_images = [
            (item["path"], item.get("mime_type") or "image/png")
            for item in image_candidates
        ]
        planner_prompt = build_storyboard_planner_prompt(
            recent_messages,
            params.prompt,
            image_candidates,
            params.shot_limit,
            attach_reference_images=params.planner_endpoint != "chat_completions",
            character_profiles=character_profiles,
            style_lock=style_lock,
        )
        with db.connect() as conn:
            cursor = conn.execute(
                """
                insert into messages (conversation_id, role, content, meta_json, created_at)
                values (?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    "assistant",
                    "AI 正在规划连续分镜...",
                    db.json_dumps({"mode": "storyboard", "planner_status": "streaming", "context_limit": context_limit}),
                    db.now_iso(),
                ),
            )
            assistant_message_id = int(cursor.lastrowid)
        db.update_task(task_id, assistant_message_id=assistant_message_id)
        persist_task_checkpoint(
            task_id,
            mode="storyboard",
            step="planner_started",
            progress=12,
            stage="AI 正在规划连续分镜",
            can_resume=False,
            assistant_message_id=assistant_message_id,
            context_limit=context_limit,
        )
        publish_task_event(
            task_id,
            "assistant_start",
            {
                "message": {
                    "id": assistant_message_id,
                    "conversation_id": conversation_id,
                    "role": "assistant",
                    "content": "AI 正在规划连续分镜...",
                    "meta": {"mode": "storyboard", "planner_status": "streaming", "context_limit": context_limit},
                    "image_status": "",
                    "images": [],
                    "uploaded_images": [],
                }
            },
            snapshot=True,
        )
        planner_response = await call_chat_planner(
            model=params.planner_model or params.model,
            prompt=planner_prompt,
            config=params.planner_config or params.config,
            uploaded=planner_reference_images,
            image_contexts=image_candidates,
            previous_response_id=None,
            on_stream_event=make_planner_reply_stream_handler(task_id, assistant_message_id, "AI 正在规划连续分镜..."),
            planner_endpoint=params.planner_endpoint,
        )
        planner_text = extract_text_from_responses(planner_response)
        plan = parse_storyboard_plan(planner_text, params.shot_limit)
        planner_response_id = planner_response.get("id")
        storyboard_state = {
            "character_summary": plan.get("character_summary", ""),
            "scene_summary": plan.get("scene_summary", ""),
            "shots": plan.get("shots", []),
        }
        update_storyboard_task_state(task_id, task_payload, storyboard_state)
        raw_for_meta_storyboard = copy.deepcopy(storyboard_state)
        db.update_task(
            task_id,
            progress=24,
            stage="AI 已规划镜头，准备逐张生成" if plan["should_generate"] else "AI 已判断需要继续完善分镜",
        )

        raw_for_meta: dict[str, Any] = {
            "endpoint": "/v1/responses",
            "mode": "storyboard",
            "planner": sanitize_response(planner_response),
            "plan": plan,
            "context_limit": context_limit,
            "image_candidates": sanitize_reference_candidates(image_candidates),
            "style_lock": style_lock,
            "character_profiles": character_profiles,
        }
        raw_for_meta["storyboard"] = raw_for_meta_storyboard

        with db.connect() as conn:
            conn.execute(
                "update conversations set previous_response_id = ?, updated_at = ? where id = ?",
                (planner_response_id or previous_response_id, db.now_iso(), conversation_id),
            )
        update_message_content(assistant_message_id, plan["reply"] or "我理解了。", planner_response_id)
        update_message_meta(assistant_message_id, {**raw_for_meta, "planner_status": "done"}, planner_response_id)
        persist_task_checkpoint(
            task_id,
            mode="storyboard",
            step="planner_done",
            progress=24,
            stage="AI 已规划镜头，准备逐张生成" if plan["should_generate"] else "AI 已判断需要继续完善分镜",
            can_resume=bool(plan["should_generate"]),
            assistant_message_id=assistant_message_id,
            planner_response_id=planner_response_id,
            storyboard=storyboard_state,
            raw_for_meta=raw_for_meta,
            completed_count=0,
            total_count=len(plan["shots"]),
        )
        publish_task_snapshot(task_id)
        publish_task_event(
            task_id,
            "assistant_plan",
            {
                "message_id": assistant_message_id,
                "conversation_id": conversation_id,
                "meta": {**raw_for_meta, "planner_status": "done"},
            },
            snapshot=True,
        )
        publish_task_event(
            task_id,
            "assistant_reply",
            {"message_id": assistant_message_id, "content": plan["reply"] or "我理解了。"},
            snapshot=True,
        )

        if not plan["should_generate"]:
            db.finish_task(
                task_id,
                {
                    "user_message_id": user_message_id,
                    "assistant_message_id": assistant_message_id,
                    "text": plan["reply"],
                    "images": [],
                    "fallback": False,
                    "raw": raw_for_meta,
                },
            )
            return

        shots = plan["shots"]
        total = len(shots)
        bucket = task_image_folder(task_id, f"分镜-{conversation_title}")
        output_format = params.output_format
        previous_image: tuple[Path, str] | None = None
        saved_images: list[dict[str, Any]] = []
        shot_results: list[dict[str, Any]] = []
        provider_attempts: list[dict[str, Any]] = []
        last_provider: dict[str, Any] | None = None
        for index, shot in enumerate(shots, start=1):
            shot_name = normalize_shot_name(str(shot.get("name") or f"镜头{index}"), index)
            shot["name"] = shot_name
            shot["status"] = "running"
            update_storyboard_task_state(task_id, task_payload, storyboard_state)
            progress = min(30 + int((index - 1) / max(total, 1) * 62), 88)
            db.update_task(task_id, progress=progress, stage=f"准备生成镜头 {index}/{total}：{shot_name}")
            persist_task_checkpoint(
                task_id,
                mode="storyboard",
                step="image_waiting",
                progress=progress,
                stage=f"准备生成镜头 {index}/{total}：{shot_name}",
                can_resume=len(saved_images) > 0,
                assistant_message_id=assistant_message_id,
                planner_response_id=planner_response_id,
                storyboard=storyboard_state,
                raw_for_meta=raw_for_meta,
                completed_count=len(saved_images),
                total_count=total,
                current_shot_index=index,
            )
            publish_task_snapshot(task_id)
            continuity_prompt = "\n".join(
                part
                for part in [
                    f"人物一致性概述：{plan.get('character_summary')}",
                    f"场景一致性概述：{plan.get('scene_summary')}",
                    f"镜头顺序：第 {index}/{total} 镜头，文件名/标题：{shot_name}",
                    f"连续性要求：{shot.get('continuity')}",
                    "必须保持人物身份、服装、发型、道具、空间方位、光线方向和画面质感连续。",
                    "每次只输出这一镜头的一张首帧画面，不要拼图，不要多格漫画。",
                    str(shot.get("planner_prompt") or shot.get("prompt") or ""),
                ]
                if str(part or "").strip()
            )
            continuity_prompt = apply_locked_prompt(
                continuity_prompt,
                character_profiles=character_profiles,
                style_lock=style_lock,
            )
            action, edit_inputs, input_image_notes, shot_reference_candidates, shot_target_candidates = resolve_storyboard_shot_inputs(
                previous_image,
                image_candidates,
                shot,
                index=index,
            )
            shot["action"] = action
            shot["used_reference_image_refs"] = candidate_refs(shot_reference_candidates)
            shot["used_reference_image_ids"] = candidate_ids(shot_reference_candidates)
            shot["edit_target_image_refs"] = candidate_refs(shot_target_candidates)
            shot["edit_target_image_ids"] = candidate_ids(shot_target_candidates)
            try:
                response, provider, attempt_log = await execute_with_provider_failover(
                    task_id,
                    lambda _provider, provider_config: call_responses_image_generation(
                        model=params.model,
                        prompt=continuity_prompt,
                        image_model=params.image_model,
                        size=params.size,
                        quality=params.quality,
                        output_format=output_format,
                        background=params.background,
                        output_compression=params.output_compression,
                        moderation=params.moderation,
                        action=action,
                        partial_images=params.partial_images,
                        config=provider_config,
                        uploaded=edit_inputs,
                        input_fidelity=params.input_fidelity,
                        input_image_notes=input_image_notes,
                        previous_response_id=None,
                        on_stable_retry=lambda quality: update_timeout_retry_stage(task_id, quality),
                        on_stream_event=lambda event, shot_index=index, name=shot_name: handle_storyboard_stream_event(task_id, shot_index, total, name, event),
                    ),
                    waiting_stage=lambda item, index=index, total=total, shot_name=shot_name: f"镜头 {index}/{total} 已分配到 {item['name']}，等待空闲通道：{shot_name}",
                    running_stage=lambda item, index=index, total=total, shot_name=shot_name: f"正在使用 {item['name']} 生成镜头 {index}/{total}：{shot_name}",
                    retry_stage=lambda item, attempt, index=index, total=total, shot_name=shot_name: f"{item['name']} 暂不可用，正在重试第 {attempt}/{PROVIDER_UNAVAILABLE_RETRY_COUNT + 1} 次：镜头 {index}/{total} {shot_name}",
                    switch_stage=lambda item, index=index, total=total, shot_name=shot_name: f"{item['name']} 连续不可用，正在切换下一个最佳提供商继续生成镜头 {index}/{total}：{shot_name}",
                )
                last_provider = provider
                if attempt_log:
                    provider_attempts.extend(attempt_log)
                image_items = extract_images_from_responses(response, output_format, folder=bucket)
                if not image_items:
                    raise HTTPException(
                        status_code=502,
                        detail={
                            "message": "Responses API 已返回，但没有找到 image_generation_call.result 图片数据。",
                            "endpoint": "responses",
                            "upstream": sanitize_response(response),
                            "suggestion": "请确认当前模型组合支持 image_generation 工具，或更换外层模型/图片工具模型后重试。",
                            "provider_attempts": provider_attempts,
                        },
                    )
                renamed = rename_output_image(image_items[0], shot_name)
                image_record = public_task_image(
                    renamed,
                    conversation_id=conversation_id,
                    message_id=assistant_message_id,
                    task_id=task_id,
                    title=shot_name,
                    bucket=bucket,
                )
                raw_for_meta["image_provider"] = {"id": provider["id"], "name": provider["name"]}
                raw_for_meta["provider_attempts"] = provider_attempts
                saved_images.append(image_record)
                previous_image = (renamed[0], renamed[2])
                shot["status"] = "done"
                shot["image_id"] = image_record["id"]
                shot["url"] = image_record["url"]
                shot["execution_prompt"] = continuity_prompt
                image_record["prompt_text"] = continuity_prompt
                shot_results.append(
                    {
                        "shot": shot,
                        "action": action,
                        "response": sanitize_response(response),
                        "image": image_record,
                        "provider": {"id": provider["id"], "name": provider["name"]},
                    }
                )
                db.add_prompt(continuity_prompt, source="auto", mode="storyboard")
                update_storyboard_task_state(task_id, task_payload, storyboard_state)
                db.update_task(
                    task_id,
                    progress=min(32 + int(index / max(total, 1) * 60), 92),
                    stage=f"已通过 {provider['name']} 保存镜头 {index}/{total}：{shot_name}",
                )
                persist_task_checkpoint(
                    task_id,
                    mode="storyboard",
                    step="image_saved",
                    progress=min(32 + int(index / max(total, 1) * 60), 92),
                    stage=f"已通过 {provider['name']} 保存镜头 {index}/{total}：{shot_name}",
                    can_resume=index < total,
                    assistant_message_id=assistant_message_id,
                    planner_response_id=planner_response_id,
                    storyboard=storyboard_state,
                    raw_for_meta=raw_for_meta,
                    completed_count=len(saved_images),
                    total_count=total,
                    current_shot_index=index,
                )
                publish_storyboard_image_saved(
                    task_id,
                    conversation_id=conversation_id,
                    message_id=assistant_message_id,
                    image=image_record,
                    shot=shot,
                    index=index,
                    total=total,
                )
            except HTTPException as exc:
                shot["status"] = "failed"
                shot["error"] = exc.detail
                update_storyboard_task_state(task_id, task_payload, storyboard_state)
                publish_task_snapshot(task_id)
                raw_for_meta["image_status"] = "failed"
                raw_for_meta["image_error"] = exc.detail
                raw_for_meta["provider_attempts"] = provider_attempts
                update_message_meta(assistant_message_id, raw_for_meta, planner_response_id)
                persist_task_checkpoint(
                    task_id,
                    mode="storyboard",
                    step="image_waiting",
                    progress=min(30 + int((index - 1) / max(total, 1) * 62), 88),
                    stage=f"镜头 {index}/{total} 生成失败，可按当前进度重试",
                    can_resume=len(saved_images) > 0,
                    assistant_message_id=assistant_message_id,
                    planner_response_id=planner_response_id,
                    storyboard=storyboard_state,
                    raw_for_meta=raw_for_meta,
                    completed_count=len(saved_images),
                    total_count=total,
                    current_shot_index=index,
                )
                raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
            except Exception as exc:
                shot["status"] = "failed"
                shot["error"] = str(exc)
                update_storyboard_task_state(task_id, task_payload, storyboard_state)
                publish_task_snapshot(task_id)
                raw_for_meta["image_status"] = "failed"
                raw_for_meta["image_error"] = str(exc)
                raw_for_meta["provider_attempts"] = provider_attempts
                update_message_meta(assistant_message_id, raw_for_meta, planner_response_id)
                persist_task_checkpoint(
                    task_id,
                    mode="storyboard",
                    step="image_waiting",
                    progress=min(30 + int((index - 1) / max(total, 1) * 62), 88),
                    stage=f"镜头 {index}/{total} 生成失败，可按当前进度重试",
                    can_resume=len(saved_images) > 0,
                    assistant_message_id=assistant_message_id,
                    planner_response_id=planner_response_id,
                    storyboard=storyboard_state,
                    raw_for_meta=raw_for_meta,
                    completed_count=len(saved_images),
                    total_count=total,
                    current_shot_index=index,
                )
                raise

        raw_for_meta["image_status"] = "done"
        raw_for_meta["storyboard"] = storyboard_state
        raw_for_meta["provider_attempts"] = provider_attempts
        raw_for_meta["shot_results"] = shot_results
        if last_provider:
            raw_for_meta["image_provider"] = {"id": last_provider["id"], "name": last_provider["name"]}
        update_message_meta(assistant_message_id, raw_for_meta, planner_response_id)
        db.update_task(task_id, progress=96, stage="正在整理分镜结果")
        persist_task_checkpoint(
            task_id,
            mode="storyboard",
            step="finalizing",
            progress=96,
            stage="正在整理分镜结果",
            can_resume=False,
            assistant_message_id=assistant_message_id,
            planner_response_id=planner_response_id,
            storyboard=storyboard_state,
            raw_for_meta=raw_for_meta,
            completed_count=len(saved_images),
            total_count=total,
        )
        db.finish_task(
            task_id,
            {
                "user_message_id": user_message_id,
                "assistant_message_id": assistant_message_id,
                "text": plan["reply"],
                "images": saved_images,
                "fallback": False,
                "raw": raw_for_meta,
            },
        )

    await run_with_slot(task_id, worker)


@app.post("/api/tasks/{task_id}/retry")
async def retry_task(task_id: int) -> dict[str, Any]:
    ensure_task_slot()
    old_task = task_with_images(task_id)
    checkpoint = task_checkpoint_dict(old_task)
    if old_task.get("status") not in {"failed", "canceled"}:
        raise HTTPException(status_code=400, detail="当前仅支持重试失败或已停止的任务")
    conversation_id = old_task.get("conversation_id")
    if conversation_id:
        with db.connect() as conn:
            ensure_conversation_task_retry_allowed(conn, int(conversation_id), int(old_task["id"]))
    mode = str(old_task.get("mode") or "")
    params = copy.deepcopy(old_task.get("params") or {})
    if mode == "generate":
        GenerateRequest(**normalize_text_fields(params))
    elif mode == "edit":
        prompt = str(params.get("prompt") or old_task.get("prompt") or "").strip()
        if not prompt:
            raise HTTPException(status_code=400, detail="该编辑任务缺少可重试的原始提示词")
        input_image_exists = any(
            image.get("source") == "input" and Path(str(image.get("file_path") or "")).exists()
            for image in old_task.get("images", [])
        )
        if not input_image_exists:
            raise HTTPException(status_code=400, detail="该编辑任务缺少可重试的原始输入图")
    elif mode == "chat":
        prompt = str(params.get("prompt") or old_task.get("prompt") or "").strip()
        if not prompt:
            raise HTTPException(status_code=400, detail="该对话任务缺少可重试的原始提示词")
    elif mode == "storyboard":
        storyboard = params.get("storyboard") if isinstance(params.get("storyboard"), dict) else {}
        shots = storyboard.get("shots") if isinstance(storyboard.get("shots"), list) else []
        checkpoint_step = str(checkpoint.get("step") or "").strip().lower()
        if not shots and checkpoint_step not in {"planner_started", "planner_done"}:
            raise HTTPException(status_code=400, detail="该分镜任务没有可重试的镜头状态")
    else:
        raise HTTPException(status_code=400, detail="当前仅支持重试普通生图、编辑、对话和分镜连续生图任务")
    previous_state = {
        "status": old_task.get("status"),
        "progress": old_task.get("progress"),
        "stage": old_task.get("stage"),
        "error": old_task.get("error"),
        "response_json": old_task.get("response_json"),
        "cancel_requested": old_task.get("cancel_requested"),
        "scheduled_for": old_task.get("scheduled_for"),
        "image_provider_id": old_task.get("image_provider_id"),
        "image_provider_name": old_task.get("image_provider_name"),
        "checkpoint_json": old_task.get("checkpoint_json"),
    }
    retried_task = requeue_task_for_manual_retry(old_task, checkpoint)
    try:
        schedule_existing_task(retried_task)
    except HTTPException:
        db.update_task(task_id, **previous_state)
        raise
    except Exception:
        db.update_task(task_id, **previous_state)
        raise
    return {"task": task_with_images(task_id)}


async def run_storyboard_retry_task(task_id: int, old_task: dict[str, Any], payload: dict[str, Any]) -> None:
    async def worker() -> None:
        storyboard = payload.get("storyboard") if isinstance(payload.get("storyboard"), dict) else {}
        shots = storyboard.get("shots") if isinstance(storyboard.get("shots"), list) else []
        total = len(shots)
        output_format = str(payload.get("output_format", "png"))
        client_config = ClientConfig(**payload.get("config", {})) if isinstance(payload.get("config"), dict) else ClientConfig()
        style_lock = load_style_lock(payload.get("style_lock_id"))
        character_profiles = load_character_profiles(payload.get("character_profile_ids"))
        same_task_resume = int(old_task.get("id") or 0) == task_id
        seed_candidates = load_seed_images_from_payload(payload, strict=True, label="分镜任务参考图快照")
        if not seed_candidates:
            seed_candidates = load_seed_images_from_task_images(old_task.get("images", []))
        old_images = [image for image in old_task.get("images", []) if image.get("source") == "api"]
        by_id = {int(image["id"]): image for image in old_images if image.get("id")}
        by_title = {str(image.get("title") or ""): image for image in old_images}
        done_count = 0
        previous_image: tuple[Path, str] | None = None
        saved_images: list[dict[str, Any]] = []
        conversation_id = old_task.get("conversation_id")
        assistant_message_id = old_task.get("assistant_message_id")

        def image_for_shot(shot: dict[str, Any]) -> dict[str, Any] | None:
            try:
                image_id = int(shot.get("image_id") or 0)
            except (TypeError, ValueError):
                image_id = 0
            if image_id and image_id in by_id:
                return by_id[image_id]
            return by_title.get(str(shot.get("name") or ""))

        for shot in shots:
            if not isinstance(shot, dict) or shot.get("status") != "done":
                break
            done_count += 1
            image = image_for_shot(shot)
            if image and image.get("file_path") and Path(image["file_path"]).exists():
                public_url = image.get("public_url") or image.get("url") or ""
                if same_task_resume:
                    shot["image_id"] = image.get("id")
                    shot["url"] = public_url
                    saved_images.append(copy.deepcopy(image))
                else:
                    copied_image_id = db.add_image(
                        source="api",
                        file_path=Path(image["file_path"]),
                        public_url=public_url,
                        mime_type=image.get("mime_type") or "image/png",
                        title=str(shot.get("name") or image.get("title") or ""),
                        bucket=image.get("bucket"),
                        task_id=task_id,
                        conversation_id=conversation_id,
                        message_id=assistant_message_id,
                    )
                    shot["image_id"] = copied_image_id
                    shot["url"] = public_url
                    copied_image = {
                        **image,
                        "id": copied_image_id,
                        "task_id": task_id,
                        "conversation_id": conversation_id,
                        "message_id": assistant_message_id,
                        "public_url": public_url,
                        "url": public_url,
                    }
                    saved_images.append(copied_image)
                previous_image = (Path(image["file_path"]), image.get("mime_type") or "image/png")

        bucket = task_image_folder(task_id, f"重试分镜-{old_task.get('prompt') or task_id}")
        db.update_task(task_id, progress=12, stage=f"准备从第 {done_count + 1}/{total} 个镜头继续")
        update_storyboard_task_state(task_id, payload, storyboard)
        persist_task_checkpoint(
            task_id,
            mode="storyboard",
            step="image_waiting",
            progress=12,
            stage=f"准备从第 {done_count + 1}/{total} 个镜头继续",
            can_resume=done_count > 0,
            assistant_message_id=assistant_message_id,
            storyboard=storyboard,
            completed_count=len(saved_images),
            total_count=total,
            bucket=bucket,
            retry_of=old_task.get("id"),
        )
        publish_task_snapshot(task_id)
        provider_attempts: list[dict[str, Any]] = []
        last_provider: dict[str, Any] | None = None
        for index, shot in enumerate(shots, start=1):
            if not isinstance(shot, dict) or shot.get("status") == "done":
                continue
            shot_name = normalize_shot_name(str(shot.get("name") or f"镜头{index}"), index)
            shot["name"] = shot_name
            shot["status"] = "running"
            update_storyboard_task_state(task_id, payload, storyboard)
            db.update_task(task_id, progress=min(25 + int((index - 1) / max(total, 1) * 65), 88), stage=f"准备重试镜头 {index}/{total}：{shot_name}")
            publish_task_snapshot(task_id)
            action, edit_inputs, input_image_notes, shot_reference_candidates, shot_target_candidates = resolve_storyboard_shot_inputs(
                previous_image,
                seed_candidates,
                shot,
                index=index,
            )
            shot["action"] = action
            shot["used_reference_image_refs"] = candidate_refs(shot_reference_candidates)
            shot["used_reference_image_ids"] = candidate_ids(shot_reference_candidates)
            shot["edit_target_image_refs"] = candidate_refs(shot_target_candidates)
            shot["edit_target_image_ids"] = candidate_ids(shot_target_candidates)
            try:
                if previous_image is None and index > 1:
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "message": "无法继续分镜：没有找到上一镜头输出图作为 edit 输入。",
                            "suggestion": "请从原对话重新提交分镜需求，或选择一张参考图后重试。",
                        },
                    )
                persist_task_checkpoint(
                    task_id,
                    mode="storyboard",
                    step="image_running",
                    progress=min(25 + int((index - 1) / max(total, 1) * 65), 88),
                    stage=f"准备重试镜头 {index}/{total}：{shot_name}",
                    can_resume=len(saved_images) > 0,
                    assistant_message_id=assistant_message_id,
                    storyboard=storyboard,
                    completed_count=len(saved_images),
                    total_count=total,
                    current_shot_index=index,
                    bucket=bucket,
                    retry_of=old_task.get("id"),
                    provider_attempts=provider_attempts,
                )
                response, provider, attempt_log = await execute_with_provider_failover(
                    task_id,
                    lambda _provider, client_config: call_responses_image_generation(
                        model=str(payload.get("model", "gpt-5.4")),
                        prompt=apply_locked_prompt(
                            str(shot.get("execution_prompt") or shot.get("planner_prompt") or shot.get("prompt") or old_task.get("prompt") or ""),
                            character_profiles=character_profiles,
                            style_lock=style_lock,
                        ),
                        image_model=str(payload.get("image_model", "gpt-image-2")),
                        size=str(payload.get("size", "2560x1440")),
                        quality=str(payload.get("quality", "high")),
                        output_format=output_format,
                        background=payload.get("background", "auto"),
                        output_compression=payload.get("output_compression"),
                        moderation=payload.get("moderation", "auto"),
                        action=action,
                        partial_images=payload.get("partial_images"),
                        config=client_config,
                        uploaded=edit_inputs,
                        input_fidelity=str(payload.get("input_fidelity", "high")),
                        input_image_notes=input_image_notes,
                        previous_response_id=None,
                        on_stable_retry=lambda quality: update_timeout_retry_stage(task_id, quality),
                        on_stream_event=lambda event, shot_index=index, name=shot_name: handle_storyboard_stream_event(task_id, shot_index, total, name, event),
                    ),
                    waiting_stage=lambda item, index=index, total=total, shot_name=shot_name: f"重试镜头 {index}/{total} 已分配到 {item['name']}，等待空闲通道：{shot_name}",
                    running_stage=lambda item, index=index, total=total, shot_name=shot_name: f"正在使用 {item['name']} 重试镜头 {index}/{total}：{shot_name}",
                    retry_stage=lambda item, attempt, index=index, total=total, shot_name=shot_name: f"{item['name']} 暂不可用，正在重试第 {attempt}/{PROVIDER_UNAVAILABLE_RETRY_COUNT + 1} 次：镜头 {index}/{total} {shot_name}",
                    switch_stage=lambda item, index=index, total=total, shot_name=shot_name: f"{item['name']} 连续不可用，正在切换下一个最佳提供商继续重试镜头 {index}/{total}：{shot_name}",
                )
                last_provider = provider
                if attempt_log:
                    provider_attempts.extend(attempt_log)
                image_items = extract_images_from_responses(response, output_format, folder=bucket)
                if not image_items:
                    raise HTTPException(
                        status_code=502,
                        detail={
                            "message": "Responses API 已返回，但没有找到 image_generation_call.result 图片数据。",
                            "provider_attempts": provider_attempts,
                        },
                    )
                renamed = rename_output_image(image_items[0], shot_name)
                image_record = public_task_image(
                    renamed,
                    conversation_id=old_task.get("conversation_id"),
                    message_id=old_task.get("assistant_message_id"),
                    task_id=task_id,
                    title=shot_name,
                    bucket=bucket,
                )
                saved_images.append(image_record)
                previous_image = (renamed[0], renamed[2])
                shot["status"] = "done"
                shot["image_id"] = image_record["id"]
                shot["url"] = image_record["url"]
                image_record["prompt_text"] = str(shot.get("execution_prompt") or shot.get("planner_prompt") or shot.get("prompt") or old_task.get("prompt") or "")
                update_storyboard_task_state(task_id, payload, storyboard)
                db.update_task(task_id, progress=min(30 + int(index / max(total, 1) * 62), 92), stage=f"已通过 {provider['name']} 重试保存镜头 {index}/{total}：{shot_name}")
                persist_task_checkpoint(
                    task_id,
                    mode="storyboard",
                    step="image_saved",
                    progress=min(30 + int(index / max(total, 1) * 62), 92),
                    stage=f"已通过 {provider['name']} 重试保存镜头 {index}/{total}：{shot_name}",
                    can_resume=index < total,
                    assistant_message_id=assistant_message_id,
                    storyboard=storyboard,
                    completed_count=len(saved_images),
                    total_count=total,
                    current_shot_index=index,
                    bucket=bucket,
                    retry_of=old_task.get("id"),
                    provider_attempts=provider_attempts,
                )
                publish_storyboard_image_saved(
                    task_id,
                    conversation_id=old_task.get("conversation_id"),
                    message_id=old_task.get("assistant_message_id"),
                    image=image_record,
                    shot=shot,
                    index=index,
                    total=total,
                )
            except HTTPException as exc:
                shot["status"] = "failed"
                shot["error"] = exc.detail
                update_storyboard_task_state(task_id, payload, storyboard)
                persist_task_checkpoint(
                    task_id,
                    mode="storyboard",
                    step="image_waiting",
                    progress=min(25 + int((index - 1) / max(total, 1) * 65), 88),
                    stage=f"重试镜头 {index}/{total} 失败，可按当前进度继续",
                    can_resume=len(saved_images) > 0,
                    assistant_message_id=assistant_message_id,
                    storyboard=storyboard,
                    completed_count=len(saved_images),
                    total_count=total,
                    current_shot_index=index,
                    bucket=bucket,
                    retry_of=old_task.get("id"),
                    provider_attempts=provider_attempts,
                )
                publish_task_snapshot(task_id)
                raise
            except Exception as exc:
                shot["status"] = "failed"
                shot["error"] = str(exc)
                update_storyboard_task_state(task_id, payload, storyboard)
                persist_task_checkpoint(
                    task_id,
                    mode="storyboard",
                    step="image_waiting",
                    progress=min(25 + int((index - 1) / max(total, 1) * 65), 88),
                    stage=f"重试镜头 {index}/{total} 失败，可按当前进度继续",
                    can_resume=len(saved_images) > 0,
                    assistant_message_id=assistant_message_id,
                    storyboard=storyboard,
                    completed_count=len(saved_images),
                    total_count=total,
                    current_shot_index=index,
                    bucket=bucket,
                    retry_of=old_task.get("id"),
                    provider_attempts=provider_attempts,
                )
                publish_task_snapshot(task_id)
                raise
        persist_task_checkpoint(
            task_id,
            mode="storyboard",
            step="finalizing",
            progress=96,
            stage="正在整理分镜重试结果",
            can_resume=False,
            assistant_message_id=assistant_message_id,
            storyboard=storyboard,
            completed_count=len(saved_images),
            total_count=total,
            bucket=bucket,
            retry_of=old_task.get("id"),
            provider_attempts=provider_attempts,
        )
        db.finish_task(
            task_id,
            {
                "retry_of": old_task.get("id"),
                "images": saved_images,
                "raw": {
                    "storyboard": storyboard,
                    "image_provider": {"id": last_provider["id"], "name": last_provider["name"]} if last_provider else None,
                    "provider_attempts": provider_attempts,
                },
            },
        )

    await run_with_slot(task_id, worker)


def gallery_group_key(conversation_id: int | None, task_id: int | None) -> str:
    if conversation_id:
        return f"conversation-{conversation_id}"
    if task_id:
        return f"task-{task_id}"
    return "group-unknown"


def gallery_group_title(row: dict[str, Any]) -> str:
    return str(row.get("conversation_title") or row.get("task_prompt") or row.get("title") or "独立生成")


def gallery_group_mode(row: dict[str, Any]) -> str:
    return str(row.get("conversation_mode") or row.get("task_mode") or "").strip()


def safe_archive_name(value: str | None, fallback: str = "批量下载") -> str:
    text = normalize_image_title(value, fallback=fallback)
    text = INVALID_FILENAME_CHARS.sub("_", text).strip(" ._")
    return text[:80] or fallback


def unique_archive_path(used: set[str], folder: str, filename: str) -> str:
    clean_folder = safe_archive_name(folder, "批量下载")
    clean_filename = INVALID_FILENAME_CHARS.sub("_", filename or "image.png").strip(" ._") or "image.png"
    stem = Path(clean_filename).stem or "image"
    suffix = Path(clean_filename).suffix or ".png"
    candidate = f"{clean_folder}/{clean_filename}"
    index = 2
    while candidate in used:
        candidate = f"{clean_folder}/{stem}-{index}{suffix}"
        index += 1
    used.add(candidate)
    return candidate


def image_archive_filename(image: dict[str, Any], index: int) -> str:
    title = normalize_image_title(image.get("title"), fallback="")
    if not title:
        title = normalize_image_title(Path(str(image.get("file_path") or "")).stem, fallback=f"图片-{index}")
    suffix = Path(str(image.get("file_path") or "")).suffix
    if not suffix:
        mime = str(image.get("mime_type") or "").lower()
        suffix = ".jpg" if "jpeg" in mime or "jpg" in mime else ".webp" if "webp" in mime else ".png"
    return f"{title}{suffix}"


def selected_download_images(
    conn: sqlite3.Connection,
    *,
    conversation_id: int | None = None,
    task_id: int | None = None,
    tag: str = "",
    favorite: int | None = None,
    mode: str = "",
) -> list[dict[str, Any]]:
    clauses = ["i.source not in ('mask', 'input_reference')"]
    values: list[Any] = []
    if conversation_id is not None:
        clauses.append("i.conversation_id = ?")
        values.append(conversation_id)
    if task_id is not None:
        clauses.append("i.task_id = ?")
        values.append(task_id)
    if favorite is not None:
        clauses.append("i.favorite = ?")
        values.append(1 if int(favorite) else 0)
    if mode.strip():
        clauses.append("coalesce(c.mode, t.mode, '') = ?")
        values.append(mode.strip())
    where = " and ".join(clauses)
    rows = conn.execute(
        f"""
        select i.*,
               c.title as conversation_title,
               c.mode as conversation_mode,
               m.content as message_content,
               t.prompt as task_prompt,
               t.mode as task_mode
        from images i
        left join conversations c on c.id = i.conversation_id
        left join messages m on m.id = i.message_id
        left join tasks t on t.id = i.task_id
        where {where}
        order by datetime(i.created_at) asc, i.id asc
        """,
        values,
    ).fetchall()
    images = [serialize_image_record(row, conn) for row in rows]
    filter_tag = normalize_image_tags([tag])[0] if normalize_image_tags([tag]) else ""
    if filter_tag:
        images = [image for image in images if filter_tag in parse_image_tags(image.get("tags"))]
    task_ids = [int(image["task_id"]) for image in images if image.get("task_id")]
    task_map: dict[int, dict[str, Any]] = {}
    if task_ids:
        placeholders = ",".join("?" for _ in task_ids)
        task_rows = conn.execute(f"select * from tasks where id in ({placeholders})", task_ids).fetchall()
        task_map = {int(row["id"]): summarize_task(row, include_response=False) for row in task_rows}
    for image in images:
        task = task_map.get(int(image["task_id"])) if image.get("task_id") else None
        if task:
            enrich_images_with_prompt([image], task)
    return images


def load_gallery_group_items(
    conn: sqlite3.Connection,
    *,
    conversation_id: int | None = None,
    task_id: int | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    if conversation_id is None and task_id is None:
        return []
    where_clause = "i.conversation_id = ?" if conversation_id is not None else "i.conversation_id is null and i.task_id = ?"
    where_value = conversation_id if conversation_id is not None else task_id
    order_clause = "order by datetime(i.created_at) desc, i.id desc" if limit is not None else "order by datetime(i.created_at) asc, i.id asc"
    limit_clause = "limit ?" if limit is not None else ""
    values: list[Any] = [where_value]
    if limit is not None:
        values.append(limit)
    rows = conn.execute(
        f"""
        select i.*,
               c.title as conversation_title,
               c.mode as conversation_mode,
               m.content as message_content,
               t.prompt as task_prompt,
               t.mode as task_mode
        from images i
        left join conversations c on c.id = i.conversation_id
        left join messages m on m.id = i.message_id
        left join tasks t on t.id = i.task_id
        where i.source not in ('mask', 'input_reference')
          and {where_clause}
        {order_clause}
        {limit_clause}
        """,
        values,
    ).fetchall()
    ordered_rows = list(reversed(rows)) if limit is not None else list(rows)
    items = [serialize_image_record(row, conn) for row in ordered_rows]
    task_ids = [int(item["task_id"]) for item in items if item.get("task_id")]
    task_map: dict[int, dict[str, Any]] = {}
    if task_ids:
        placeholders = ",".join("?" for _ in task_ids)
        task_rows = conn.execute(f"select * from tasks where id in ({placeholders})", task_ids).fetchall()
        task_map = {int(row["id"]): summarize_task(row, include_response=False) for row in task_rows}
    for item in items:
        task = task_map.get(int(item["task_id"])) if item.get("task_id") else None
        if task:
            enrich_images_with_prompt([item], task)
    return items


def build_gallery_group_payload(
    conn: sqlite3.Connection,
    row: sqlite3.Row | dict[str, Any],
    *,
    preview_limit: int = 6,
    include_items: bool = False,
) -> dict[str, Any]:
    item = db.row_to_dict(row) if isinstance(row, sqlite3.Row) else dict(row)
    conversation_id = int(item["conversation_id"]) if item.get("conversation_id") is not None else None
    task_id = int(item["task_id"]) if item.get("task_id") is not None else None
    preview_items = load_gallery_group_items(
        conn,
        conversation_id=conversation_id,
        task_id=task_id,
        limit=max(1, min(preview_limit, 12)),
    )
    group = {
        "key": gallery_group_key(conversation_id, task_id),
        "conversation_id": conversation_id,
        "task_id": task_id,
        "title": gallery_group_title(item),
        "mode": gallery_group_mode(item),
        "latest_time": item.get("latest_created_at"),
        "time": item.get("latest_created_at"),
        "total_count": int(item.get("total_count") or len(preview_items)),
        "preview_items": preview_items,
        "has_more": int(item.get("total_count") or len(preview_items)) > len(preview_items),
        "detail_loaded": False,
    }
    if include_items:
        items = load_gallery_group_items(conn, conversation_id=conversation_id, task_id=task_id, limit=None)
        group["items"] = items
        group["detail_loaded"] = True
        group["has_more"] = len(items) > len(preview_items)
    return group


@app.get("/api/gallery")
def gallery(group_limit: int = 40, preview_limit: int = 6) -> dict[str, Any]:
    with db.connect() as conn:
        rows = conn.execute(
            """
            select
                grouped.conversation_id,
                grouped.task_id,
                grouped.total_count,
                grouped.latest_created_at,
                c.title as conversation_title,
                c.mode as conversation_mode,
                t.prompt as task_prompt,
                t.mode as task_mode
            from (
                select
                    i.conversation_id as conversation_id,
                    case when i.conversation_id is null then i.task_id else null end as task_id,
                    count(*) as total_count,
                    max(i.created_at) as latest_created_at
                from images i
                where i.source not in ('mask', 'input_reference')
                group by i.conversation_id, case when i.conversation_id is null then i.task_id else null end
            ) grouped
            left join conversations c on c.id = grouped.conversation_id
            left join tasks t on t.id = grouped.task_id
            order by datetime(grouped.latest_created_at) desc
            limit ?
            """,
            (max(1, min(group_limit, 120)),),
        ).fetchall()
        groups = [build_gallery_group_payload(conn, row, preview_limit=preview_limit, include_items=False) for row in rows]
    return {"items": groups}


@app.get("/api/gallery/group")
def gallery_group_detail(conversation_id: int | None = None, task_id: int | None = None, preview_limit: int = 6) -> dict[str, Any]:
    if conversation_id is None and task_id is None:
        raise HTTPException(status_code=400, detail="conversation_id 或 task_id 至少需要一个")
    with db.connect() as conn:
        if conversation_id is not None:
            row = conn.execute(
                """
                select
                    c.id as conversation_id,
                    null as task_id,
                    c.title as conversation_title,
                    c.mode as conversation_mode,
                    null as task_prompt,
                    null as task_mode,
                    (
                        select count(*) from images i
                        where i.conversation_id = c.id and i.source not in ('mask', 'input_reference')
                    ) as total_count,
                    (
                        select max(i.created_at) from images i
                        where i.conversation_id = c.id and i.source not in ('mask', 'input_reference')
                    ) as latest_created_at
                from conversations c
                where c.id = ?
                """,
                (conversation_id,),
            ).fetchone()
        else:
            row = conn.execute(
                """
                select
                    null as conversation_id,
                    t.id as task_id,
                    null as conversation_title,
                    null as conversation_mode,
                    t.prompt as task_prompt,
                    t.mode as task_mode,
                    (
                        select count(*) from images i
                        where i.conversation_id is null and i.task_id = t.id and i.source not in ('mask', 'input_reference')
                    ) as total_count,
                    (
                        select max(i.created_at) from images i
                        where i.conversation_id is null and i.task_id = t.id and i.source not in ('mask', 'input_reference')
                    ) as latest_created_at
                from tasks t
                where t.id = ?
                """,
                (task_id,),
            ).fetchone()
        if not row or not row["total_count"]:
            raise HTTPException(status_code=404, detail="gallery group not found")
        group = build_gallery_group_payload(conn, row, preview_limit=preview_limit, include_items=True)
    return {"group": group}


@app.put("/api/images/{image_id}/metadata")
def update_image_metadata(image_id: int, request: ImageMetadataRequest) -> dict[str, Any]:
    tags = normalize_image_tags(request.tags) if request.tags is not None else None
    assignments: list[str] = []
    values: list[Any] = []
    if request.favorite is not None:
        assignments.append("favorite = ?")
        values.append(1 if int(request.favorite) else 0)
    if tags is not None:
        assignments.append("tags = ?")
        values.append(db.json_dumps(tags))
    if not assignments:
        with db.connect() as conn:
            row = conn.execute("select * from images where id = ?", (image_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="image not found")
            return {"image": serialize_image_record(row, conn)}
    values.append(image_id)
    with db.connect() as conn:
        cursor = conn.execute(f"update images set {', '.join(assignments)} where id = ?", values)
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="image not found")
        row = conn.execute(
            """
            select i.*,
                   c.title as conversation_title,
                   c.mode as conversation_mode,
                   m.content as message_content,
                   t.prompt as task_prompt,
                   t.mode as task_mode
            from images i
            left join conversations c on c.id = i.conversation_id
            left join messages m on m.id = i.message_id
            left join tasks t on t.id = i.task_id
            where i.id = ?
            """,
            (image_id,),
        ).fetchone()
        image = serialize_image_record(row, conn)
    return {"image": image}


@app.get("/api/images/download")
def download_images_zip(
    conversation_id: int | None = None,
    task_id: int | None = None,
    tag: str = "",
    favorite: int | None = None,
    mode: str = "",
    folder_name: str = "批量下载",
) -> Response:
    folder = safe_archive_name(folder_name, "批量下载")
    with db.connect() as conn:
        images = selected_download_images(
            conn,
            conversation_id=conversation_id,
            task_id=task_id,
            tag=tag,
            favorite=favorite,
            mode=mode,
        )
    files = []
    for image in images:
        path = Path(str(image.get("file_path") or ""))
        if path.is_file():
            files.append((image, path))
    if not files:
        raise HTTPException(status_code=404, detail="没有找到可下载的图片")
    buffer = io.BytesIO()
    used_names: set[str] = set()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index, (image, path) in enumerate(files, start=1):
            archive_name = unique_archive_path(used_names, folder, image_archive_filename(image, index))
            archive.write(path, archive_name)
        manifest = {
            "folder": folder,
            "total": len(files),
            "filters": {
                "conversation_id": conversation_id,
                "task_id": task_id,
                "tag": tag,
                "favorite": favorite,
                "mode": mode,
            },
            "images": [
                {
                    "id": image.get("id"),
                    "title": image.get("title"),
                    "tags": image.get("tags"),
                    "favorite": image.get("favorite"),
                    "task_id": image.get("task_id"),
                    "conversation_id": image.get("conversation_id"),
                    "filename": path.name,
                }
                for image, path in files
            ],
        }
        archive.writestr(f"{folder}/manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    buffer.seek(0)
    filename = f"{folder}.zip"
    quoted = quote(filename)
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename=\"download.zip\"; filename*=UTF-8''{quoted}",
            "Cache-Control": "no-store",
        },
    )


@app.post("/api/conversations/{conversation_id}/messages")
async def chat_message(
    conversation_id: int,
    params_json: str = Form(...),
    images: list[UploadFile] | None = File(default=None),
) -> dict[str, Any]:
    ensure_task_slot()
    params = ChatRequest(**normalize_text_fields(parse_params(params_json)))
    if len(images or []) > 3:
        raise HTTPException(status_code=400, detail="对话模式最多上传 3 张参考图")
    with db.connect() as conn:
        conversation = ensure_conversation_message_allowed(conn, conversation_id, "chat")
        previous_response_id = conversation["previous_response_id"]
        conversation_title = conversation["title"]
        context_limit = params.context_limit if params.context_limit is not None else conversation["context_limit"]
        context_limit = max(0, min(int(context_limit), 50))
    uploaded = [await save_upload(upload) for upload in images or []]
    selected_reference_images = require_selected_reference_images(
        params.reference_image_ids,
        limit=max(0, 3 - len(uploaded)),
        conversation_id=conversation_id,
        label="对话参考图",
    )
    selected_reference_uploads = [
        (Path(item["file_path"]), item.get("mime_type") or "image/png")
        for item in selected_reference_images
    ]
    image_candidates = [
        *build_uploaded_image_candidates(uploaded, params.upload_reference_roles, params.upload_selection_modes),
        *build_selected_image_candidates(
            selected_reference_images,
            params.reference_image_roles,
            params.reference_image_selection_modes,
            start_order=len(uploaded) + 1,
        ),
    ]
    db.add_prompt(params.prompt, source="auto", mode="chat")

    with db.connect() as conn:
        recent_messages = load_recent_messages(conversation_id, context_limit)
        cursor = conn.execute(
            """
            insert into messages (conversation_id, role, content, meta_json, created_at)
            values (?, ?, ?, ?, ?)
            """,
            (
                conversation_id,
                "user",
                params.prompt,
                db.json_dumps(
                    {
                        "uploads": [str(path) for path, _ in uploaded],
                        "reference_image_ids": [item["id"] for item in selected_reference_images],
                        "reference_image_roles": params.reference_image_roles,
                        "reference_image_selection_modes": params.reference_image_selection_modes,
                        "upload_reference_roles": params.upload_reference_roles,
                        "upload_selection_modes": params.upload_selection_modes,
                        "context_limit": context_limit,
                        "style_lock_id": params.style_lock_id,
                        "character_profile_ids": params.character_profile_ids,
                    }
                ),
                db.now_iso(),
            ),
        )
        user_message_id = int(cursor.lastrowid)
        conn.execute(
            "update conversations set context_limit = ?, updated_at = ? where id = ?",
            (context_limit, db.now_iso(), conversation_id),
        )

    task_params = compact_params(
        {
            "endpoint": "/v1/responses",
            "tool": "image_generation",
            "model": params.model,
            "planner_model": params.planner_model,
            "planner_endpoint": params.planner_endpoint,
            "image_model": params.image_model,
            "prompt": params.prompt,
            "size": params.size,
            "quality": params.quality,
            "background": params.background,
            "output_format": params.output_format,
            "output_compression": params.output_compression,
            "moderation": params.moderation,
            "action": params.action,
            "input_fidelity": params.input_fidelity,
            "partial_images": params.partial_images,
            "context_limit": context_limit,
            "reference_image_ids": [item["id"] for item in selected_reference_images],
            "reference_image_roles": params.reference_image_roles,
            "reference_image_selection_modes": params.reference_image_selection_modes,
            "upload_reference_roles": params.upload_reference_roles,
            "upload_selection_modes": params.upload_selection_modes,
            "style_lock_id": params.style_lock_id,
            "character_profile_ids": params.character_profile_ids,
            "seed_images": serialize_seed_images(image_candidates),
            "planner_config": params.planner_config.model_dump() if params.planner_config else None,
        }
    )
    task_id = db.create_task(
        "chat",
        params.prompt,
        task_params,
        conversation_id=conversation_id,
        user_message_id=user_message_id,
    )
    for item in uploaded:
        public_input_image(item, source="input", title=params.prompt, task_id=task_id, conversation_id=conversation_id, message_id=user_message_id)
    for item in selected_reference_uploads:
        public_input_image(item, source="input_reference", title=params.prompt, task_id=task_id, conversation_id=conversation_id, message_id=user_message_id)
    schedule_task(
        task_id,
        run_chat_task(
            task_id,
            conversation_id,
            user_message_id,
            params,
            image_candidates,
            previous_response_id,
            conversation_title,
            recent_messages,
            context_limit,
        ),
    )
    return {"task": db.get_task(task_id), "user_message_id": user_message_id}


async def run_chat_task(
    task_id: int,
    conversation_id: int,
    user_message_id: int,
    params: ChatRequest,
    image_candidates: list[dict[str, Any]],
    previous_response_id: str | None,
    conversation_title: str,
    recent_messages: list[dict[str, Any]],
    context_limit: int,
    assistant_message_id: int | None = None,
    resume_checkpoint: dict[str, Any] | None = None,
    restored_images: list[dict[str, Any]] | None = None,
) -> None:
    async def worker() -> None:
        style_lock = load_style_lock(params.style_lock_id)
        character_profiles = load_character_profiles(params.character_profile_ids)
        checkpoint = resume_checkpoint if isinstance(resume_checkpoint, dict) else {}
        checkpoint_step = str(checkpoint.get("step") or "").strip().lower()
        resume_from_planner = checkpoint_step in {"planner_done", "image_waiting", "image_running", "image_saved", "finalizing"}
        planner_reference_images = [
            (item["path"], item.get("mime_type") or "image/png")
            for item in image_candidates
        ]
        active_assistant_message_id = int(checkpoint.get("assistant_message_id") or assistant_message_id or 0) or None
        if active_assistant_message_id is None:
            with db.connect() as conn:
                cursor = conn.execute(
                    """
                    insert into messages (conversation_id, role, content, meta_json, created_at)
                    values (?, ?, ?, ?, ?)
                    """,
                    (
                        conversation_id,
                        "assistant",
                        "AI 正在理解你的需求...",
                        db.json_dumps({"planner_status": "streaming", "context_limit": context_limit}),
                        db.now_iso(),
                    ),
                )
                active_assistant_message_id = int(cursor.lastrowid)
        else:
            update_message_meta(
                active_assistant_message_id,
                {
                    "planner_status": "streaming" if not resume_from_planner else "done",
                    "context_limit": context_limit,
                },
            )
            if not resume_from_planner:
                update_message_content(active_assistant_message_id, "AI 正在理解你的需求...")

        db.update_task(task_id, assistant_message_id=active_assistant_message_id)
        publish_task_event(
            task_id,
            "assistant_start",
            {
                "message": {
                    "id": active_assistant_message_id,
                    "conversation_id": conversation_id,
                    "role": "assistant",
                    "content": "AI 正在理解你的需求..." if not resume_from_planner else str(checkpoint.get("reply") or "AI 正在继续上次进度..."),
                    "meta": {"planner_status": "streaming" if not resume_from_planner else "done", "context_limit": context_limit},
                    "image_status": "",
                    "images": [],
                    "uploaded_images": [],
                }
            },
            snapshot=True,
        )

        plan = copy.deepcopy(checkpoint.get("plan")) if isinstance(checkpoint.get("plan"), dict) else None
        raw_for_meta = copy.deepcopy(checkpoint.get("raw_for_meta")) if isinstance(checkpoint.get("raw_for_meta"), dict) else {}
        planner_response_id = str(checkpoint.get("planner_response_id") or "").strip() or None
        provider_attempts: list[dict[str, Any]] = copy.deepcopy(checkpoint.get("provider_attempts")) if isinstance(checkpoint.get("provider_attempts"), list) else []

        if not resume_from_planner or not plan:
            persist_task_checkpoint(
                task_id,
                mode="chat",
                step="planner_started",
                progress=12,
                stage="AI 正在理解意图",
                can_resume=False,
                assistant_message_id=active_assistant_message_id,
                context_limit=context_limit,
            )
            planner_prompt = build_chat_planner_prompt(
                recent_messages,
                params.prompt,
                bool(image_candidates),
                image_candidates=image_candidates,
                attach_reference_images=params.planner_endpoint != "chat_completions",
                character_profiles=character_profiles,
                style_lock=style_lock,
            )
            planner_response = await call_chat_planner(
                model=params.planner_model or params.model,
                prompt=planner_prompt,
                config=params.planner_config or params.config,
                uploaded=planner_reference_images,
                image_contexts=image_candidates,
                previous_response_id=None,
                on_stream_event=make_planner_reply_stream_handler(task_id, active_assistant_message_id, "AI 正在理解你的需求..."),
                planner_endpoint=params.planner_endpoint,
            )
            planner_text = extract_text_from_responses(planner_response)
            plan = parse_planner_json(planner_text)
            planner_response_id = planner_response.get("id")
            raw_for_meta = {
                "endpoint": "/v1/responses",
                "planner": sanitize_response(planner_response),
                "plan": plan,
                "context_limit": context_limit,
                "image_candidates": sanitize_reference_candidates(image_candidates),
                "style_lock": style_lock,
                "character_profiles": character_profiles,
            }
            with db.connect() as conn:
                conn.execute(
                    """
                    update conversations
                    set previous_response_id = ?, updated_at = ?
                    where id = ?
                    """,
                    (planner_response_id or previous_response_id, db.now_iso(), conversation_id),
                )
            update_message_content(active_assistant_message_id, plan["reply"] or "我理解了。", planner_response_id)
            update_message_meta(active_assistant_message_id, {**raw_for_meta, "planner_status": "done"}, planner_response_id)
            publish_task_event(
                task_id,
                "assistant_reply",
                {"message_id": active_assistant_message_id, "content": plan["reply"] or "我理解了。"},
                snapshot=True,
            )
        else:
            raw_for_meta.setdefault("endpoint", "/v1/responses")
            raw_for_meta.setdefault("plan", plan)
            raw_for_meta.setdefault("context_limit", context_limit)
            raw_for_meta.setdefault("image_candidates", sanitize_reference_candidates(image_candidates))
            update_message_meta(active_assistant_message_id, {**raw_for_meta, "planner_status": "done"}, planner_response_id)
            publish_task_event(
                task_id,
                "assistant_reply",
                {"message_id": active_assistant_message_id, "content": str(plan.get("reply") or checkpoint.get("reply") or "我理解了。")},
                snapshot=True,
            )

        requested_reference_candidates = resolve_selected_candidates(
            image_candidates,
            plan["reference_image_ids"],
            plan["reference_image_refs"],
            fallback_to_all=bool(image_candidates) and plan["action"] != "edit",
        )
        user_target_candidates = explicit_edit_target_candidates(image_candidates)
        target_candidates = resolve_selected_candidates(
            image_candidates,
            plan["edit_target_image_ids"],
            plan["edit_target_image_refs"],
        )
        if not target_candidates and plan["action"] == "edit" and user_target_candidates:
            target_candidates = list(user_target_candidates)
        if not target_candidates and user_target_candidates and params.action != "generate":
            target_candidates = list(user_target_candidates)

        db.update_task(
            task_id,
            progress=34,
            stage="AI 已完成意图判断，准备生图" if plan["should_generate"] else "AI 已判断无需生图",
        )

        if not plan["should_generate"]:
            persist_task_checkpoint(
                task_id,
                mode="chat",
                step="planner_done",
                progress=34,
                stage="AI 已判断无需生图",
                can_resume=False,
                assistant_message_id=active_assistant_message_id,
                planner_response_id=planner_response_id,
                plan=plan,
                raw_for_meta=raw_for_meta,
            )
            raw_for_task = {
                "user_message_id": user_message_id,
                "assistant_message_id": active_assistant_message_id,
                "text": plan["reply"],
                "images": [],
                "fallback": False,
                "raw": raw_for_meta,
            }
            db.finish_task(task_id, raw_for_task)
            return

        base_image_prompt = str(checkpoint.get("image_prompt") or raw_for_meta.get("image_prompt") or plan.get("image_prompt") or params.prompt).strip() or params.prompt
        image_prompt = apply_locked_prompt(
            base_image_prompt,
            character_profiles=character_profiles,
            style_lock=style_lock,
        )
        action = str(checkpoint.get("resolved_action") or raw_for_meta.get("resolved_action") or plan["action"] or params.action).strip().lower()
        if action not in {"generate", "edit", "auto"}:
            action = params.action
        if target_candidates:
            action = "edit"
        if action == "auto":
            if target_candidates:
                action = "edit"
            elif params.action == "edit" and image_candidates:
                action = "edit"
            else:
                action = "generate"
        if action == "edit":
            if not target_candidates:
                if len(requested_reference_candidates) == 1:
                    target_candidates = list(requested_reference_candidates)
                elif not requested_reference_candidates and len(image_candidates) == 1:
                    target_candidates = list(image_candidates)
                else:
                    plan["should_generate"] = False
                    plan["reply"] = "我已经知道你想继续改图，但当前无法安全判断要直接修改哪一张。请明确勾选要改的那几张图；如果其余图片只是风格或角色参考，请保留它们为辅助参考。"
                    plan["reason"] = "edit requested without unambiguous direct edit targets"
            if not plan["should_generate"]:
                db.update_task(task_id, progress=36, stage="需要你确认具体要修改的图片")
                update_message_content(active_assistant_message_id, plan["reply"] or "我理解了。", planner_response_id)
                update_message_meta(active_assistant_message_id, {**raw_for_meta, "plan": plan, "planner_status": "done"}, planner_response_id)
                publish_task_event(
                    task_id,
                    "assistant_reply",
                    {"message_id": active_assistant_message_id, "content": plan["reply"] or "我理解了。"},
                    snapshot=True,
                )
                persist_task_checkpoint(
                    task_id,
                    mode="chat",
                    step="planner_done",
                    progress=36,
                    stage="需要你确认具体要修改的图片",
                    can_resume=False,
                    assistant_message_id=active_assistant_message_id,
                    planner_response_id=planner_response_id,
                    plan=plan,
                    raw_for_meta={**raw_for_meta, "plan": plan},
                )
                db.finish_task(
                    task_id,
                    {
                        "user_message_id": user_message_id,
                        "assistant_message_id": active_assistant_message_id,
                        "text": plan["reply"],
                        "images": [],
                        "fallback": False,
                        "raw": {**raw_for_meta, "plan": plan},
                    },
                )
                return

        used_reference_candidates = merge_candidate_lists(
            target_candidates if action == "edit" else [],
            requested_reference_candidates,
        )
        auxiliary_reference_candidates = [
            item
            for item in used_reference_candidates
            if candidate_identity(item) not in {candidate_identity(target) for target in target_candidates}
        ]
        edit_inputs, input_image_notes = build_candidate_input_bundle(
            target_candidates if action == "edit" else [],
            auxiliary_reference_candidates if action == "edit" else used_reference_candidates,
        )
        raw_for_meta["selected_reference_image_refs"] = candidate_refs(used_reference_candidates)
        raw_for_meta["selected_reference_image_ids"] = candidate_ids(used_reference_candidates)
        raw_for_meta["edit_target_image_refs"] = candidate_refs(target_candidates)
        raw_for_meta["edit_target_image_ids"] = candidate_ids(target_candidates)
        raw_for_meta["resolved_action"] = action
        raw_for_meta["tool"] = "image_generation"
        image_name = normalize_image_title(checkpoint.get("image_name") or raw_for_meta.get("image_name") or plan.get("image_name") or "")
        if not image_name:
            image_name = build_direct_mode_base_title(
                "",
                conversation_id=conversation_id,
                prompt=image_prompt,
                created_at=db.now_iso(),
            )
        raw_for_meta["image_name"] = image_name
        raw_for_meta["image_prompt"] = image_prompt
        images_out: list[dict[str, Any]] = list(restored_images or [])
        total_images_hint = max(int(checkpoint.get("total_count") or len(images_out) or 1), 1)

        persist_task_checkpoint(
            task_id,
            mode="chat",
            step="image_waiting",
            progress=52,
            stage=f"AI 已准备按 {action} 继续生成图片",
            can_resume=True,
            assistant_message_id=active_assistant_message_id,
            planner_response_id=planner_response_id,
            plan=plan,
            raw_for_meta=raw_for_meta,
            image_prompt=image_prompt,
            image_name=image_name,
            resolved_action=action,
            completed_count=len(images_out),
            total_count=total_images_hint,
            provider_attempts=provider_attempts,
        )
        update_message_meta(active_assistant_message_id, {**raw_for_meta, "image_status": "waiting"}, planner_response_id)

        if len(images_out) >= total_images_hint and images_out:
            persist_task_checkpoint(
                task_id,
                mode="chat",
                step="finalizing",
                progress=96,
                stage="已恢复已完成图片，正在整理对话结果",
                can_resume=False,
                assistant_message_id=active_assistant_message_id,
                planner_response_id=planner_response_id,
                plan=plan,
                raw_for_meta=raw_for_meta,
                completed_count=len(images_out),
                total_count=total_images_hint,
                provider_attempts=provider_attempts,
            )
            raw_for_meta["image_status"] = "done"
            raw_for_task = {
                "user_message_id": user_message_id,
                "assistant_message_id": active_assistant_message_id,
                "text": plan["reply"],
                "images": images_out,
                "fallback": False,
                "raw": raw_for_meta,
            }
            db.finish_task(task_id, raw_for_task)
            return

        try:
            image_response, provider, attempt_log = await execute_with_provider_failover(
                task_id,
                lambda _provider, provider_config: call_responses_image_generation(
                    model=params.model,
                    prompt=image_prompt,
                    image_model=params.image_model,
                    size=params.size,
                    quality=params.quality,
                    output_format=params.output_format,
                    background=params.background,
                    output_compression=params.output_compression,
                    moderation=params.moderation,
                    action=action,
                    partial_images=params.partial_images,
                    config=provider_config,
                    uploaded=edit_inputs,
                    input_fidelity=params.input_fidelity,
                    input_image_notes=input_image_notes if edit_inputs else None,
                    previous_response_id=None,
                    on_stable_retry=lambda quality: update_timeout_retry_stage(task_id, quality),
                    on_stream_event=lambda event: handle_image_stream_event(task_id, event),
                ),
                waiting_stage=lambda item: f"AI 决定执行 {action}，已分配 {item['name']}，等待空闲通道",
                running_stage=lambda item: f"AI 决定执行 {action}，正在使用 {item['name']} 生成图片",
                retry_stage=lambda item, attempt: f"{item['name']} 暂不可用，正在重试第 {attempt}/{PROVIDER_UNAVAILABLE_RETRY_COUNT + 1} 次并继续生成图片",
                switch_stage=lambda item: f"{item['name']} 连续不可用，正在切换下一个最佳提供商继续生成图片",
            )
            if attempt_log:
                provider_attempts.extend(attempt_log)
            raw_for_meta["image_provider"] = {"id": provider["id"], "name": provider["name"]}
            raw_for_meta["provider_attempts"] = provider_attempts
            update_message_meta(
                active_assistant_message_id,
                {
                    **raw_for_meta,
                    "image_status": "running",
                    "image_stage": f"AI 决定执行 {action}，正在使用 {provider['name']} 生成图片",
                },
                planner_response_id,
            )
            persist_task_checkpoint(
                task_id,
                mode="chat",
                step="image_running",
                progress=72,
                stage=f"正在通过 {provider['name']} 生成图片",
                can_resume=True,
                assistant_message_id=active_assistant_message_id,
                planner_response_id=planner_response_id,
                plan=plan,
                raw_for_meta=raw_for_meta,
                image_prompt=image_prompt,
                image_name=image_name,
                resolved_action=action,
                completed_count=len(images_out),
                total_count=total_images_hint,
                provider_attempts=provider_attempts,
            )
            bucket = str(checkpoint.get("bucket") or "").strip() or task_image_folder(task_id, conversation_title)
            image_items = extract_images_from_responses(image_response, params.output_format, folder=bucket)
            raw_for_meta["image_response"] = sanitize_response(image_response)
            if not image_items:
                raise HTTPException(
                    status_code=502,
                    detail={
                        "message": "Responses API 已返回，但没有找到 image_generation_call.result 图片数据。",
                        "endpoint": "responses",
                        "upstream": sanitize_response(image_response),
                        "suggestion": "请确认当前模型组合支持 image_generation 工具，或更换外层模型/图片工具模型后重试。",
                        "provider_attempts": provider_attempts,
                    },
                )
        except HTTPException as exc:
            raw_for_meta["image_status"] = "failed"
            raw_for_meta["image_error"] = exc.detail
            raw_for_meta["provider_attempts"] = provider_attempts
            update_message_meta(
                active_assistant_message_id,
                raw_for_meta,
                planner_response_id or previous_response_id,
            )
            persist_task_checkpoint(
                task_id,
                mode="chat",
                step="image_waiting",
                progress=52,
                stage="本次生图失败，可按当前进度重试",
                can_resume=True,
                assistant_message_id=active_assistant_message_id,
                planner_response_id=planner_response_id,
                plan=plan,
                raw_for_meta=raw_for_meta,
                image_prompt=image_prompt,
                image_name=image_name,
                resolved_action=action,
                completed_count=len(images_out),
                total_count=max(total_images_hint, len(images_out), 1),
                provider_attempts=provider_attempts,
            )
            with db.connect() as conn:
                conn.execute(
                    "update conversations set previous_response_id = ?, updated_at = ? where id = ?",
                    (planner_response_id or previous_response_id, db.now_iso(), conversation_id),
                )
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

        total_images = max(len(image_items), len(images_out), 1)
        for index, item in enumerate(image_items[len(images_out) :], start=len(images_out) + 1):
            resolved_title = build_sequenced_title(image_name, index, total_images)
            renamed = rename_output_image(item, resolved_title, fallback_stem=f"task-{task_id}")
            images_out.append(
                public_task_image(
                    renamed,
                    conversation_id=conversation_id,
                    message_id=active_assistant_message_id,
                    task_id=task_id,
                    title=resolved_title,
                    bucket=bucket,
                )
            )
            persist_task_checkpoint(
                task_id,
                mode="chat",
                step="image_saved",
                progress=min(84 + int(len(images_out) / max(total_images, 1) * 8), 92),
                stage=f"已保存第 {len(images_out)}/{total_images} 张图片",
                can_resume=len(images_out) < total_images,
                assistant_message_id=active_assistant_message_id,
                planner_response_id=planner_response_id,
                plan=plan,
                raw_for_meta=raw_for_meta,
                image_prompt=image_prompt,
                image_name=image_name,
                resolved_action=action,
                completed_count=len(images_out),
                total_count=total_images,
                provider_attempts=provider_attempts,
                bucket=bucket,
            )

        raw_for_meta["image_status"] = "done"
        raw_for_meta["provider_attempts"] = provider_attempts
        persist_task_checkpoint(
            task_id,
            mode="chat",
            step="finalizing",
            progress=96,
            stage="正在写入对话历史",
            can_resume=False,
            assistant_message_id=active_assistant_message_id,
            planner_response_id=planner_response_id,
            plan=plan,
            raw_for_meta=raw_for_meta,
            image_prompt=image_prompt,
            image_name=image_name,
            resolved_action=action,
            completed_count=len(images_out),
            total_count=total_images,
            provider_attempts=provider_attempts,
        )
        with db.connect() as conn:
            conn.execute(
                "update messages set meta_json = ?, response_id = ?, updated_at = ? where id = ?",
                (
                    db.json_dumps(raw_for_meta),
                    image_response.get("id") or planner_response_id,
                    db.now_iso(),
                    active_assistant_message_id,
                ),
            )
            conn.execute(
                "update conversations set previous_response_id = ?, updated_at = ? where id = ?",
                (image_response.get("id") or planner_response_id or previous_response_id, db.now_iso(), conversation_id),
            )

        raw_for_task = {
            "user_message_id": user_message_id,
            "assistant_message_id": active_assistant_message_id,
            "text": plan["reply"],
            "images": images_out,
            "fallback": False,
            "raw": raw_for_meta,
        }
        db.update_task(task_id, assistant_message_id=active_assistant_message_id, progress=96, stage="正在写入对话历史")
        db.finish_task(task_id, raw_for_task)
    await run_with_slot(task_id, worker)



def resolve_tenant_media_file(kind: str, relative_path: str) -> Path:
    if kind == "uploads":
        root = current_upload_dir()
    elif kind == "outputs":
        root = current_output_dir()
    else:
        raise HTTPException(status_code=404, detail="media not found")
    resolved_root = root.resolve()
    target = (root / relative_path).resolve()
    if target != resolved_root and resolved_root not in target.parents:
        raise HTTPException(status_code=404, detail="media not found")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="media not found")
    return target


@app.get("/media/{kind}/{relative_path:path}")
def serve_media(kind: str, relative_path: str):
    target = resolve_tenant_media_file(kind, relative_path)
    accel_prefix = get_env("MEDIA_ACCEL_REDIRECT_PREFIX", "").strip().rstrip("/")
    if accel_prefix:
        response = Response(media_type=guess_mime(target))
        response.headers["X-Accel-Redirect"] = f"{accel_prefix}/media/{kind}/{relative_path}"
    else:
        response = FileResponse(target)
    response.headers["Cache-Control"] = IMMUTABLE_PRIVATE_CACHE_CONTROL
    add_vary_cookie(response)
    return response


@app.get("/favicon.svg", include_in_schema=False)
def serve_favicon_svg():
    icon_path = FRONTEND_DIST / "favicon.svg"
    if icon_path.exists():
        return FileResponse(icon_path, media_type="image/svg+xml")
    raise HTTPException(status_code=404, detail="favicon not found")


@app.get("/favicon.ico", include_in_schema=False)
def serve_favicon_ico():
    icon_path = FRONTEND_DIST / "favicon.ico"
    if icon_path.exists():
        return FileResponse(icon_path, media_type="image/x-icon")
    return serve_favicon_svg()


if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")


@app.get("/{path:path}", response_class=HTMLResponse, response_model=None)
def frontend(path: str):
    index_path = FRONTEND_DIST / "index.html"
    if index_path.exists():
        response = FileResponse(index_path)
        response.headers["Cache-Control"] = NO_STORE_CACHE_CONTROL
        return response
    return HTMLResponse(
        """
        <html><body style="font-family:sans-serif;padding:32px">
        <h1>GPT Image Studio API is running</h1>
        <p>Frontend has not been built yet. Run <code>bash scripts/install_ubuntu.sh</code>.</p>
        </body></html>
        """
    )
