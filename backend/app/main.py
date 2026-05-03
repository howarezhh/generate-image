import asyncio
import copy
import hashlib
import hmac
import html
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import database as db
from .config import (
    DEFAULT_API_BASE_URL,
    DEFAULT_API_KEY,
    CHAT_PLANNER_MAX_ATTEMPTS,
    CHAT_PLANNER_TIMEOUT_SECONDS,
    ENABLE_IMAGE_STABLE_RETRY,
    FRONTEND_DIST,
    IMAGE_REQUEST_MAX_ATTEMPTS,
    IMAGE_REQUEST_TIMEOUT_SECONDS,
    IMAGE_STABLE_RETRY_QUALITY,
    MAX_CONCURRENT_TASKS,
    OUTPUT_DIR,
    ROOT_DIR,
    UPLOAD_DIR,
    ensure_dirs,
)
from .openai_compat import (
    data_url_for_file,
    extract_images_from_responses,
    extract_text_from_responses,
    guess_mime,
    post_chat_completions,
    post_json,
    post_json_stream,
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

RUNNING_TASKS: dict[int, asyncio.Task[Any]] = {}
TASK_EVENT_SUBSCRIBERS: dict[int, set[asyncio.Queue[dict[str, Any]]]] = {}
TASK_EVENT_SNAPSHOTS: dict[int, dict[str, dict[str, Any]]] = {}
IMAGE_PROVIDER_POOL_LOCK: asyncio.Lock | None = None
IMAGE_PROVIDER_POOL_STATE: dict[int, dict[str, Any]] = {}
ACCESS_COOKIE_NAME = "studio_access"
ACCESS_PASSWORD = "hhs54666"
ACCESS_PASSWORD_PATTERN = re.compile(r"^[A-Za-z0-9]{8}$")
ACCESS_ERROR_MESSAGE = "密码错误，请联系管理员，管理员QQ为3286385052。"
ACCESS_LOGIN_PATH = "/auth/login"
ACCESS_ALLOWED_PATHS = {ACCESS_LOGIN_PATH, "/favicon.ico"}
ACCESS_COOKIE_TOKEN = hashlib.sha256(f"gpt-image-studio:{ACCESS_PASSWORD.lower()}".encode("utf-8")).hexdigest()


class ClientConfig(BaseModel):
    base_url: str | None = None
    api_key: str | None = None


class ProviderRequest(BaseModel):
    name: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    api_key: str = ""


class AppSettingsRequest(BaseModel):
    value: dict[str, Any] = Field(default_factory=dict)


class PromptRequest(BaseModel):
    content: str = Field(min_length=1)
    source: str = "manual"
    mode: str | None = None
    favorite: int = 0


class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=1)
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
    upload_reference_roles: list[str] = Field(default_factory=list)
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
    shot_limit: int = Field(default=6, ge=1, le=12)
    reference_image_ids: list[int] = Field(default_factory=list)
    reference_image_roles: dict[str, str] = Field(default_factory=dict)
    upload_reference_roles: list[str] = Field(default_factory=list)
    config: ClientConfig = Field(default_factory=ClientConfig)
    planner_config: ClientConfig | None = None


REFERENCE_ROLE_LABELS = {
    "character": "角色锚点",
    "scene": "场景锚点",
    "wardrobe_prop": "服装道具锚点",
    "style": "风格锚点",
}
DEFAULT_REFERENCE_ROLE_ORDER = ("character", "scene", "wardrobe_prop")
REFERENCE_ROLE_PRIORITY = {role: index for index, role in enumerate(("character", "scene", "wardrobe_prop", "style"))}
CONVERSATION_MODES = {"chat", "storyboard", "generate", "edit"}


def normalize_access_password(value: str) -> str:
    return str(value or "").strip().lower()


def validate_access_password(value: str) -> bool:
    normalized = normalize_access_password(value)
    if not ACCESS_PASSWORD_PATTERN.fullmatch(normalized):
        return False
    return hmac.compare_digest(normalized, ACCESS_PASSWORD.lower())


def access_cookie_valid(request: Request) -> bool:
    token = str(request.cookies.get(ACCESS_COOKIE_NAME) or "")
    return hmac.compare_digest(token, ACCESS_COOKIE_TOKEN)


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
    <p>访问当前项目之前，需要先完成统一密码验证。密码仅支持 8 位数字或英文字母，英文字母不区分大小写。</p>
    {error_block}
    <form method="post" action="{ACCESS_LOGIN_PATH}">
      <input type="hidden" name="next" value="{safe_next}" />
      <label>
        <span>访问密码</span>
        <input
          type="password"
          name="password"
          maxlength="8"
          minlength="8"
          pattern="[A-Za-z0-9]{{8}}"
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
        return await call_next(request)
    if access_cookie_valid(request):
        return await call_next(request)
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
    image_id = db.add_image(
        source="api",
        file_path=file_path,
        public_url=public_url,
        mime_type=mime_type,
        title=title,
        bucket=bucket,
        task_id=task_id,
        conversation_id=conversation_id,
        message_id=message_id,
    )
    return {
        "id": image_id,
        "url": public_url,
        "public_url": public_url,
        "source": "api",
        "task_id": task_id,
        "conversation_id": conversation_id,
        "message_id": message_id,
        "mime_type": mime_type,
        "filename": file_path.name,
        "title": title,
        "bucket": bucket,
    }


def public_upload_image(path_value: str) -> dict[str, Any] | None:
    path = Path(path_value)
    if not path.exists():
        return None
    public_url = None
    try:
        relative = path.relative_to(UPLOAD_DIR)
        public_url = f"/media/uploads/{relative.as_posix()}"
    except ValueError:
        try:
            relative = path.relative_to(OUTPUT_DIR)
            public_url = f"/media/outputs/{relative.as_posix()}"
        except ValueError:
            public_url = f"/media/uploads/{path.name}"
    return {
        "url": public_url,
        "public_url": public_url,
        "file_path": str(path),
        "filename": path.name,
        "mime_type": guess_mime(path),
    }


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
    image_id = db.add_image(
        source=source,
        file_path=path,
        public_url=public["public_url"],
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
        "queued": "排队中",
        "running": "运行中",
        "done": "已完成",
        "failed": "失败",
        "canceled": "已停止",
    }.get(str(status or ""), "处理中")


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
    state = IMAGE_PROVIDER_POOL_STATE.get(provider_id)
    if state is None:
        state = {
            "provider": dict(provider),
            "order": order_index,
            "semaphore": asyncio.Semaphore(MAX_CONCURRENT_TASKS),
            "assigned_count": 0,
            "running_count": 0,
        }
        IMAGE_PROVIDER_POOL_STATE[provider_id] = state
        return state
    state["provider"] = dict(provider)
    state["order"] = order_index
    return state


def image_provider_pool_snapshot() -> dict[str, Any]:
    pool = load_image_provider_pool()
    providers: list[dict[str, Any]] = []
    for index, provider in enumerate(pool):
        state = IMAGE_PROVIDER_POOL_STATE.get(int(provider["id"]))
        assigned = int(state["assigned_count"]) if state else 0
        running = int(state["running_count"]) if state else 0
        providers.append(
            {
                "id": provider["id"],
                "name": provider["name"],
                "base_url": provider["base_url"],
                "assigned_tasks": assigned,
                "running_tasks": running,
                "idle_slots": max(0, MAX_CONCURRENT_TASKS - running),
                "order": index,
            }
        )
    total = len(providers)
    used = sum(1 for provider in providers if provider["assigned_tasks"] > 0)
    return {
        "total_providers": total,
        "used_providers": used,
        "idle_providers": max(0, total - used),
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


def reference_candidate_hint(candidate: dict[str, Any]) -> str:
    hint = str(candidate.get("hint") or "").strip()
    if hint:
        return hint
    return "无额外说明"


def build_reference_input_note(candidate: dict[str, Any], index: int) -> str:
    role = str(candidate.get("role") or "style")
    role_label = reference_role_label(role)
    hint = reference_candidate_hint(candidate)
    source = "用户本轮上传" if candidate.get("source") == "upload" else "用户显式选择的历史参考"
    return (
        f"Input image {index}: {role_label}. "
        f"把这张图当作固定锚点，优先保留与该角色对应的身份/场景/服装/风格信息；"
        f"来源={source}；已知说明={hint}。"
    )


def serialize_seed_images(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for candidate in candidates:
        path = candidate.get("path")
        if not isinstance(path, Path):
            continue
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


def summarize_task(row: Any) -> dict[str, Any]:
    item = db.row_to_dict(row)
    raw_response_json = item.get("response_json")
    if isinstance(item.get("params_json"), str):
        try:
            item["params"] = json.loads(item["params_json"])
        except json.JSONDecodeError:
            item["params"] = {}
    if isinstance(raw_response_json, str):
        try:
            item["response"] = json.loads(raw_response_json)
        except json.JSONDecodeError:
            item["response"] = None
        if len(raw_response_json) > 2000:
            item["response_json"] = f"[response omitted, {len(raw_response_json)} chars]"
    if isinstance(item.get("error"), str) and item["error"]:
        try:
            item["error_detail"] = json.loads(item["error"])
        except json.JSONDecodeError:
            item["error_detail"] = item["error"]
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
            db.row_to_dict(image)
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
    payload = {"event": event, "data": data}
    if snapshot:
        TASK_EVENT_SNAPSHOTS.setdefault(task_id, {})[event] = payload
    dead: list[asyncio.Queue[dict[str, Any]]] = []
    for queue in TASK_EVENT_SUBSCRIBERS.get(task_id, set()):
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            dead.append(queue)
    for queue in dead:
        TASK_EVENT_SUBSCRIBERS.get(task_id, set()).discard(queue)


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
    item["prompt_text"] = prompt_text_for_task(item)
    return item


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
    running = RUNNING_TASKS.get(task_id)
    if running:
        running.cancel()


def safe_delete_media_files(paths: list[str]) -> None:
    roots = [UPLOAD_DIR.resolve(), OUTPUT_DIR.resolve()]
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
            path = str(row["file_path"])
            if ids:
                count = conn.execute(
                    f"select count(*) as count from images where file_path = ? and id not in ({placeholders})",
                    [path, *ids],
                ).fetchone()["count"]
            else:
                count = conn.execute(
                    "select count(*) as count from images where file_path = ?",
                    (path,),
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
) -> str:
    context = build_context_prompt(history, prompt)
    image_candidates = image_candidates or []
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
7. 若使用参考图，reference_image_refs 必须填写使用到的 ref；reference_image_ids 只填写已选历史生成图的 image_id。用户本轮上传的参考图没有 image_id，也没有对应生图提示词，不要编造。
8. image_prompt 必须把用户意图改写成适合 image_generation 的单张图片提示词，并明确保留不应变化的主体、构图、风格或参考图特征。

{image_note}

请只输出 JSON，不要 Markdown，不要代码块。格式：
{{
  "reply": "给用户看的中文回复。若要生图，说明你将如何生成/修改；若不要生图，提出下一步问题或建议。",
  "should_generate": true 或 false,
  "action": "generate" 或 "edit" 或 "auto",
  "image_prompt": "should_generate 为 true 时填写一张图片的最终生图提示词；它只能对应一张图片，不能包含解释、流程、多图列表或其它信息；否则为空字符串",
  "reference_image_refs": [],
  "reference_image_ids": [],
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
            "image_prompt": "",
            "reference_image_refs": [],
            "reference_image_ids": [],
            "reason": "planner returned non-json text",
        }
    reference_refs = parsed.get("reference_image_refs") or []
    if not isinstance(reference_refs, list):
        reference_refs = []
    reference_ids = parsed.get("reference_image_ids") or []
    if not isinstance(reference_ids, list):
        reference_ids = []
    return {
        "reply": str(parsed.get("reply") or "").strip() or "我理解了。",
        "should_generate": bool(parsed.get("should_generate")),
        "action": str(parsed.get("action") or "auto").strip(),
        "image_prompt": str(parsed.get("image_prompt") or "").strip(),
        "reference_image_refs": [str(value).strip() for value in reference_refs if str(value).strip()],
        "reference_image_ids": [int(value) for value in reference_ids if str(value).isdigit()],
        "reason": str(parsed.get("reason") or "").strip(),
    }


def build_storyboard_planner_prompt(
    history: list[dict[str, Any]],
    prompt: str,
    image_candidates: list[dict[str, Any]] | None,
    shot_limit: int,
    attach_reference_images: bool = True,
) -> str:
    context = build_context_prompt(history, prompt)
    image_candidates = image_candidates or []
    if image_candidates:
        lines = ["用户本轮已经明确指定了以下角色/场景参考图；第一镜头必须优先使用这些锚点规划。"]
        for index, image in enumerate(image_candidates, start=1):
            lines.append(
                f"- 候选{index}: ref={image['ref']}, source={image.get('source')}, "
                f"role={reference_role_label(str(image.get('role') or 'style'))}, "
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
8. 第 1 镜头可基于文本或用户显式参考图；第 2 个及后续镜头的 prompt 必须写明“以上一镜头输出画面作为参考继续编辑”，并说明从上一镜头到当前首帧发生了什么连续变化。
9. 你需要为每个镜头生成中文名字，名字要短、能作为文件名，必须包含镜头顺序含义，但不要包含文件扩展名。
10. 最多输出 {shot_limit} 个镜头；如果用户没有指定数量，优先 3-5 个镜头。

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
      "prompt": "这一镜头的一张首帧图片生图提示词，包含人物/场景/构图/动作/连续性/禁止变化项；只能对应这一张图片",
      "continuity": "这一镜头与上一镜头的衔接关系；第一镜头说明开场状态"
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
    for index, item in enumerate(raw_shots[: max(1, min(int(shot_limit), 12))], start=1):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or f"{index:02d}-镜头{index}").strip()
        prompt = str(item.get("prompt") or "").strip()
        if not prompt:
            continue
        shots.append(
            {
                "order": int(item.get("order") or index),
                "name": normalize_shot_name(name, index),
                "prompt": prompt,
                "planner_prompt": prompt,
                "execution_prompt": "",
                "continuity": str(item.get("continuity") or "").strip(),
                "status": "pending",
            }
        )
    should_generate = bool(parsed.get("should_generate")) and bool(shots)
    return {
        "reply": str(parsed.get("reply") or "").strip() or ("我会按分镜顺序生成连续画面。" if should_generate else base["reply"]),
        "should_generate": should_generate,
        "character_summary": str(parsed.get("character_summary") or "").strip(),
        "scene_summary": str(parsed.get("scene_summary") or "").strip(),
        "shots": shots,
        "reason": str(parsed.get("reason") or "").strip(),
    }


def normalize_shot_name(name: str, order: int) -> str:
    clean = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "-", name.strip()).strip("-")
    if not clean:
        clean = f"镜头{order}"
    if not re.match(r"^\d{2}[-_]", clean):
        clean = f"{order:02d}-{clean}"
    return clean[:48]


def rename_output_image(item: tuple[Path, str, str], name: str) -> tuple[Path, str, str]:
    path, _public_url, mime_type = item
    stem = normalize_shot_name(name, 1)
    suffix = path.suffix or ".png"
    target = path.with_name(f"{stem}{suffix}")
    counter = 2
    while target.exists() and target != path:
        target = path.with_name(f"{stem}-{counter}{suffix}")
        counter += 1
    if target != path:
        path.rename(target)
    public_url = "/media/outputs/" + target.relative_to(OUTPUT_DIR).as_posix()
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
    *,
    start_order: int = 1,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for index, (path, mime_type) in enumerate(uploaded, start=1):
        ordinal = start_order + index - 1
        role = normalize_reference_role(upload_roles[index - 1] if upload_roles and index - 1 < len(upload_roles) else None, ordinal)
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
                "hint": f"本轮用户上传的第 {index} 张图片，没有历史生图提示词",
            }
        )
    return candidates


def build_selected_image_candidates(
    selected: list[dict[str, Any]],
    reference_roles: dict[str, str] | None = None,
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
                "hint": item.get("task_prompt") or item.get("message_content") or item.get("title") or "用户指定的历史图片",
            }
        )
    return candidates


def load_selected_reference_images(image_ids: list[int], limit: int = 3, conversation_id: int | None = None) -> list[dict[str, Any]]:
    clean_ids: list[int] = []
    for value in image_ids:
        try:
            image_id = int(value)
        except (TypeError, ValueError):
            continue
        if image_id > 0 and image_id not in clean_ids:
            clean_ids.append(image_id)
        if len(clean_ids) >= limit:
            break
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
                   t.prompt as task_prompt
            from images i
            left join messages m on m.id = i.message_id
            left join tasks t on t.id = i.task_id
            where i.id in ({placeholders}) and i.source = 'api' {conversation_clause}
            """,
            query_values,
        ).fetchall()
    by_id = {int(row["id"]): db.row_to_dict(row) for row in rows}
    return [by_id[image_id] for image_id in clean_ids if image_id in by_id and Path(by_id[image_id]["file_path"]).exists()]


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
            (conversation_id, max(1, min(limit, 12))),
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
    selected: list[tuple[Path, str]] = []
    wanted = set(reference_ids)
    wanted_refs = set(reference_refs)
    for item in candidates:
        item_id = item.get("id")
        item_ref = str(item.get("ref") or "")
        if item_ref in wanted_refs or (item_id is not None and int(item_id) in wanted):
            selected.append((item["path"], item.get("mime_type") or "image/png"))
    return selected


def storyboard_anchor_candidates(candidates: list[dict[str, Any]], limit: int = 2) -> list[dict[str, Any]]:
    anchors = [item for item in candidates if isinstance(item.get("path"), Path)]
    anchors.sort(
        key=lambda item: (
            REFERENCE_ROLE_PRIORITY.get(str(item.get("role") or "style"), len(REFERENCE_ROLE_PRIORITY)),
            str(item.get("ref") or ""),
        )
    )
    return anchors[: max(0, limit)]


def build_storyboard_generation_inputs(
    previous_image: tuple[Path, str] | None,
    seed_candidates: list[dict[str, Any]],
) -> tuple[list[tuple[Path, str]], list[str]]:
    uploads: list[tuple[Path, str]] = []
    notes: list[str] = []
    seen_paths: set[str] = set()
    if previous_image is not None:
        previous_path, previous_mime = previous_image
        uploads.append((previous_path, previous_mime))
        notes.append(
            "Input image 1: 上一镜头输出画面。必须把它作为连续编辑基底，优先保留人物身份、构图关系、空间方位、光线方向和镜头语义连续性。"
        )
        seen_paths.add(str(previous_path.resolve()))
    for candidate in storyboard_anchor_candidates(seed_candidates):
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


def load_seed_images_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_items = payload.get("seed_images") if isinstance(payload.get("seed_images"), list) else []
    candidates: list[dict[str, Any]] = []
    for index, item in enumerate(raw_items, start=1):
        if not isinstance(item, dict):
            continue
        path_value = str(item.get("file_path") or "").strip()
        if not path_value:
            continue
        path = Path(path_value)
        if not path.exists():
            continue
        role = normalize_reference_role(item.get("role"), index)
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
            }
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
    if input_fidelity and input_fidelity != "auto":
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
    db.update_task(
        task_id,
        progress=52,
        stage=f"上游网关超时，已自动切换到{quality}清晰度稳定重试",
    )


def handle_image_stream_event(task_id: int, event: dict[str, Any]) -> None:
    event_type = str(event.get("type") or "")
    if event_type.endswith(".in_progress") or event_type == "response.in_progress":
        db.update_task(task_id, progress=45, stage="上游已开始处理图像请求")
    elif event_type == "response.image_generation_call.partial_image":
        index = event.get("partial_image_index")
        label = f"上游返回局部预览 {index}" if index is not None else "上游返回局部预览"
        db.update_task(task_id, progress=68, stage=label)
    elif event_type == "response.output_item.done":
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        if item.get("type") == "image_generation_call":
            db.update_task(task_id, progress=88, stage="上游已返回最终图片")
    elif event_type == "response.completed":
        db.update_task(task_id, progress=92, stage="上游响应完成，正在保存图片")


def handle_storyboard_stream_event(task_id: int, shot_index: int, total: int, shot_name: str, event: dict[str, Any]) -> None:
    event_type = str(event.get("type") or "")
    prefix = f"镜头 {shot_index}/{total}：{shot_name}"
    if event_type.endswith(".in_progress") or event_type == "response.in_progress":
        db.update_task(task_id, stage=f"{prefix} 上游已开始处理")
    elif event_type == "response.image_generation_call.partial_image":
        db.update_task(task_id, stage=f"{prefix} 上游返回局部预览")
    elif event_type == "response.output_item.done":
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        if item.get("type") == "image_generation_call":
            db.update_task(task_id, stage=f"{prefix} 上游已返回最终图片")
    elif event_type == "response.completed":
        db.update_task(task_id, stage=f"{prefix} 上游响应完成，正在保存图片")


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


async def acquire_image_provider_slot(task_id: int, waiting_stage: str | None = None, running_stage: str | None = None) -> dict[str, Any]:
    pool = load_image_provider_pool()
    if not pool:
        raise HTTPException(status_code=400, detail="当前没有可用的生图提供商，请先配置 provider 池。")

    pool_lock = ensure_provider_pool_lock()
    async with pool_lock:
        states = [ensure_provider_pool_state(provider, index) for index, provider in enumerate(pool)]
        state = min(
            states,
            key=lambda item: (
                int(item["running_count"]),
                int(item["assigned_count"]),
                int(item["order"]),
            ),
        )
        provider = dict(state["provider"])
        waiting = int(state["running_count"]) >= MAX_CONCURRENT_TASKS
        state["assigned_count"] = int(state["assigned_count"]) + 1

    waiting_text = waiting_stage or f"已分配生图提供商：{provider['name']}，等待空闲通道"
    db.update_task(
        task_id,
        image_provider_id=int(provider["id"]),
        image_provider_name=str(provider["name"]),
        stage=waiting_text if waiting else str(running_stage or waiting_stage or f"已分配生图提供商：{provider['name']}"),
    )
    publish_task_snapshot(task_id)

    await state["semaphore"].acquire()
    async with pool_lock:
        state["running_count"] = int(state["running_count"]) + 1

    db.update_task(
        task_id,
        image_provider_id=int(provider["id"]),
        image_provider_name=str(provider["name"]),
        stage=running_stage or f"正在使用生图提供商：{provider['name']}",
    )
    publish_task_snapshot(task_id)
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
) -> int:
    stamp = db.now_iso()
    meta = {
        "uploads": [str(path) for path, _mime in (uploads or [])],
    }
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


def schedule_task(task_id: int, coro: Any) -> None:
    task = asyncio.create_task(coro)
    RUNNING_TASKS[task_id] = task

    def cleanup(done: asyncio.Task[Any]) -> None:
        RUNNING_TASKS.pop(task_id, None)
        if done.cancelled():
            db.cancel_task(task_id)

    task.add_done_callback(cleanup)


async def run_with_slot(task_id: int, worker: Any) -> None:
    try:
        task = db.get_task(task_id)
        if task and task.get("cancel_requested"):
            db.cancel_task(task_id)
            publish_task_snapshot(task_id)
            publish_task_event(task_id, "canceled", {"task_id": task_id}, snapshot=False)
            return
        db.update_task(task_id, status="running", progress=8, stage="任务已启动")
        publish_task_snapshot(task_id)
        await worker()
    except asyncio.CancelledError:
        db.cancel_task(task_id)
        publish_task_snapshot(task_id)
        publish_task_event(task_id, "canceled", {"task_id": task_id}, snapshot=False)
        raise
    except HTTPException as exc:
        db.fail_task(task_id, compact_error_detail(exc.detail))
        publish_task_snapshot(task_id)
        publish_task_event(task_id, "failed", {"task_id": task_id, "error": exc.detail}, snapshot=False)
    except Exception as exc:
        db.fail_task(task_id, str(exc))
        publish_task_snapshot(task_id)
        publish_task_event(task_id, "failed", {"task_id": task_id, "error": str(exc)}, snapshot=False)
    else:
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
    ensure_dirs()
    db.init_db()
    ensure_default_provider()
    with db.connect() as conn:
        conn.execute(
            """
            update tasks
            set status = 'failed', stage = '服务重启后任务已中断', error = ?, updated_at = ?
            where status in ('queued', 'running')
            """,
            ("服务重启后，内存中的后台任务已中断，请重新创建任务。", db.now_iso()),
        )


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
    if not validate_access_password(password):
        return HTMLResponse(login_page_html(target, ACCESS_ERROR_MESSAGE), status_code=401)
    response = RedirectResponse(url=target, status_code=303)
    response.set_cookie(
        ACCESS_COOKIE_NAME,
        ACCESS_COOKIE_TOKEN,
        httponly=True,
        samesite="lax",
        secure=False,
        path="/",
    )
    return response


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
    with db.connect() as conn:
        if config.base_url is not None:
            conn.execute(
                "insert or replace into settings (key, value, updated_at) values (?, ?, ?)",
                ("base_url", config.base_url, stamp),
            )
        if config.api_key is not None:
            conn.execute(
                "insert or replace into settings (key, value, updated_at) values (?, ?, ?)",
                ("api_key", config.api_key, stamp),
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
            }
            for provider in items
        ],
        "image_provider_pool": pool,
    }


@app.post("/api/providers")
def create_provider(request: ProviderRequest) -> dict[str, Any]:
    stamp = db.now_iso()
    with db.connect() as conn:
        cursor = conn.execute(
            """
            insert into providers (name, base_url, api_key, created_at, updated_at)
            values (?, ?, ?, ?, ?)
            """,
            (request.name.strip(), request.base_url.strip(), request.api_key.strip(), stamp, stamp),
        )
        provider_id = int(cursor.lastrowid)
        row = conn.execute("select * from providers where id = ?", (provider_id,)).fetchone()
    return db.row_to_dict(row)


@app.put("/api/providers/{provider_id}")
def update_provider(provider_id: int, request: ProviderRequest) -> dict[str, Any]:
    with db.connect() as conn:
        cursor = conn.execute(
            """
            update providers
            set name = ?, base_url = ?, api_key = ?, updated_at = ?
            where id = ?
            """,
            (request.name.strip(), request.base_url.strip(), request.api_key.strip(), db.now_iso(), provider_id),
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
    ensure_task_slot()
    db.add_prompt(request.prompt, source="auto", mode="generate")
    conversation_id: int | None = None
    user_message_id: int | None = None
    if request.conversation_id:
        with db.connect() as conn:
            conversation = ensure_conversation_message_allowed(conn, int(request.conversation_id), "generate")
            conversation_id = int(conversation["id"])
        user_message_id = create_direct_mode_user_message(
            conversation_id=conversation_id,
            prompt=request.prompt,
        )
    payload = compact_params(
        {
            "endpoint": "/v1/responses",
            "tool": "image_generation",
            "model": request.model,
            "image_model": request.image_model,
            "prompt": request.prompt,
            "size": request.size,
            "quality": request.quality,
            "n": request.n,
            "background": request.background,
            "output_format": request.output_format,
            "output_compression": request.output_compression,
            "moderation": request.moderation,
            "action": request.action,
            "partial_images": request.partial_images,
            "conversation_id": conversation_id,
        }
    )
    task_id = db.create_task("generate", request.prompt, payload, conversation_id=conversation_id, user_message_id=user_message_id)
    schedule_task(task_id, run_generate_task(task_id, request, payload, conversation_id=conversation_id, user_message_id=user_message_id))
    return {"task": db.get_task(task_id), "user_message_id": user_message_id}


async def run_generate_task(task_id: int, request: GenerateRequest, payload: dict[str, Any], *, conversation_id: int | None = None, user_message_id: int | None = None) -> None:
    async def worker() -> None:
        title = request.prompt[:48] or f"task-{task_id}"
        bucket = task_image_folder(task_id, title)
        responses: list[dict[str, Any]] = []
        saved_images: list[dict[str, Any]] = []
        lease = await acquire_image_provider_slot(task_id)
        provider = lease["provider"]
        provider_config = provider_client_config(provider)
        try:
            for index in range(request.n):
                db.update_task(
                    task_id,
                    progress=min(15 + int(index / max(request.n, 1) * 70), 85),
                    stage=f"正在使用 {provider['name']} 生成第 {index + 1}/{request.n} 张",
                )
                response = await call_responses_image_generation(
                    model=request.model,
                    prompt=request.prompt,
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
                )
                responses.append(sanitize_response(response))
                image_items = extract_images_from_responses(response, request.output_format, folder=bucket)
                saved_images.extend(
                    public_task_image(item, task_id=task_id, title=title, bucket=bucket, conversation_id=conversation_id, message_id=user_message_id)
                    for item in image_items
                )
                db.update_task(
                    task_id,
                    progress=min(25 + int((index + 1) / max(request.n, 1) * 60), 90),
                    stage=f"已通过 {provider['name']} 保存第 {index + 1}/{request.n} 张结果",
                )
            if not saved_images:
                raise HTTPException(
                    status_code=502,
                    detail={
                        "message": "Responses API 已返回，但没有找到 image_generation_call.result 图片数据。",
                        "endpoint": "responses",
                        "upstream": responses,
                        "suggestion": "请确认当前模型组合支持 image_generation 工具，或更换外层模型/图片工具模型后重试。",
                    },
                )
            db.update_task(task_id, progress=96, stage="正在整理任务结果")
            raw = {
                "endpoint": "/v1/responses",
                "tool": "image_generation",
                "image_provider": {"id": provider["id"], "name": provider["name"]},
                "responses": responses,
                "images": saved_images,
            }
            db.finish_task(task_id, raw)
        finally:
            await release_image_provider_slot(task_id, lease)
    await run_with_slot(task_id, worker)


@app.post("/api/images/edit")
async def edit_image(
    params_json: str = Form(...),
    images: list[UploadFile] = File(...),
    mask: UploadFile | None = File(default=None),
) -> dict[str, Any]:
    ensure_task_slot()
    params = normalize_text_fields(parse_params(params_json))
    prompt = str(params.get("prompt") or "")
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")
    db.add_prompt(prompt, source="auto", mode="edit")

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
    user_message_id = create_direct_mode_user_message(
        conversation_id=conversation_id,
        prompt=prompt,
        uploads=uploads_for_message,
    ) if conversation_id else None

    payload = compact_params(
        {
            "endpoint": "/v1/responses",
            "tool": "image_generation",
            "model": params.get("model", "gpt-5.4"),
            "image_model": params.get("image_model", "gpt-image-2"),
            "prompt": prompt,
            "size": params.get("size", "2560x1440"),
            "quality": params.get("quality", "high"),
            "n": clamp_image_count(params.get("n", 1)),
            "background": params.get("background", "auto"),
            "output_format": params.get("output_format", "png"),
            "output_compression": params.get("output_compression"),
            "moderation": params.get("moderation", "auto"),
            "input_fidelity": params.get("input_fidelity", "auto"),
            "action": "edit",
            "partial_images": params.get("partial_images"),
            "conversation_id": conversation_id,
        }
    )
    task_id = db.create_task("edit", prompt, payload, conversation_id=conversation_id, user_message_id=user_message_id)
    for item in saved_images:
        public_input_image(item, source="input", title=prompt, task_id=task_id, conversation_id=conversation_id, message_id=user_message_id)
    if saved_mask:
        public_input_image(saved_mask, source="mask", title=f"{prompt} mask", task_id=task_id, conversation_id=conversation_id, message_id=user_message_id)
    schedule_task(task_id, run_edit_task(task_id, params, prompt, saved_images, saved_mask, conversation_id=conversation_id, user_message_id=user_message_id))
    return {"task": db.get_task(task_id), "user_message_id": user_message_id}


async def run_edit_task(
    task_id: int,
    params: dict[str, Any],
    prompt: str,
    saved_images: list[tuple[Path, str]],
    saved_mask: tuple[Path, str] | None,
    *,
    conversation_id: int | None = None,
    user_message_id: int | None = None,
) -> None:
    async def worker() -> None:
        title = prompt[:48] or f"task-{task_id}"
        bucket = task_image_folder(task_id, title)
        output_format = str(params.get("output_format", "png"))
        responses: list[dict[str, Any]] = []
        saved_output_images: list[dict[str, Any]] = []
        count = clamp_image_count(params.get("n", 1))
        lease = await acquire_image_provider_slot(task_id)
        provider = lease["provider"]
        client_config = provider_client_config(provider)
        try:
            for index in range(count):
                db.update_task(
                    task_id,
                    progress=min(15 + int(index / max(count, 1) * 70), 85),
                    stage=f"正在使用 {provider['name']} 编辑第 {index + 1}/{count} 张",
                )
                response = await call_responses_image_generation(
                    model=str(params.get("model", "gpt-5.4")),
                    prompt=prompt,
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
                    uploaded=saved_images,
                    mask=saved_mask,
                    input_fidelity=str(params.get("input_fidelity", "auto")),
                    on_stable_retry=lambda quality: update_timeout_retry_stage(task_id, quality),
                    on_stream_event=lambda event: handle_image_stream_event(task_id, event),
                )
                responses.append(sanitize_response(response))
                image_items = extract_images_from_responses(response, output_format, folder=bucket)
                saved_output_images.extend(
                    public_task_image(item, task_id=task_id, title=title, bucket=bucket, conversation_id=conversation_id, message_id=user_message_id)
                    for item in image_items
                )
                db.update_task(
                    task_id,
                    progress=min(25 + int((index + 1) / max(count, 1) * 60), 90),
                    stage=f"已通过 {provider['name']} 保存第 {index + 1}/{count} 张编辑结果",
                )
            if not saved_output_images:
                raise HTTPException(
                    status_code=502,
                    detail={
                        "message": "Responses API 已返回，但没有找到 image_generation_call.result 图片数据。",
                        "endpoint": "responses",
                        "upstream": responses,
                        "suggestion": "请确认当前模型组合支持 image_generation 工具，或更换外层模型/图片工具模型后重试。",
                    },
                )
            db.update_task(task_id, progress=96, stage="正在整理任务结果")
            raw = {
                "endpoint": "/v1/responses",
                "tool": "image_generation",
                "image_provider": {"id": provider["id"], "name": provider["name"]},
                "responses": responses,
                "images": saved_output_images,
            }
            db.finish_task(task_id, raw)
        finally:
            await release_image_provider_slot(task_id, lease)
    await run_with_slot(task_id, worker)


@app.get("/api/tasks")
def list_tasks(limit: int = 30) -> dict[str, Any]:
    with db.connect() as conn:
        tasks = [
            summarize_task(row)
            for row in conn.execute(
                "select * from tasks order by id desc limit ?",
                (limit,),
            ).fetchall()
        ]
        for task in tasks:
            images = [
                db.row_to_dict(image)
                for image in conn.execute(
                    "select * from images where task_id = ? order by id asc",
                    (task["id"],),
                ).fetchall()
            ]
            task["images"] = enrich_images_with_prompt(images, task)
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

    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
    TASK_EVENT_SUBSCRIBERS.setdefault(task_id, set()).add(queue)

    async def stream() -> Any:
        try:
            yield sse_format("connected", {"task_id": task_id})
            for payload in TASK_EVENT_SNAPSHOTS.get(task_id, {}).values():
                yield sse_format(str(payload["event"]), payload["data"])
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
            TASK_EVENT_SUBSCRIBERS.get(task_id, set()).discard(queue)
            if not TASK_EVENT_SUBSCRIBERS.get(task_id):
                TASK_EVENT_SUBSCRIBERS.pop(task_id, None)

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
    running = RUNNING_TASKS.get(task_id)
    if running:
        running.cancel()
    db.cancel_task(task_id)
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
            db.row_to_dict(row)
            for row in conn.execute(
                "select * from images where conversation_id = ? and source = 'api' order by id asc",
                (conversation_id,),
            ).fetchall()
        ]
        tasks = [
            summarize_task(row)
            for row in conn.execute(
                "select * from tasks where conversation_id = ? order by id asc",
                (conversation_id,),
            ).fetchall()
        ]
    for message in messages:
        uploaded_images = []
        try:
            meta = json.loads(message.get("meta_json") or "{}")
        except json.JSONDecodeError:
            meta = {}
        for path_value in meta.get("uploads", []) if isinstance(meta, dict) else []:
            item = public_upload_image(str(path_value))
            if item:
                uploaded_images.append(item)
        reference_ids = meta.get("reference_image_ids", []) if isinstance(meta, dict) else []
        for image in load_selected_reference_images(reference_ids, limit=3, conversation_id=conversation_id):
            uploaded_images.append(
                {
                    "id": image["id"],
                    "url": image["public_url"],
                    "public_url": image["public_url"],
                    "file_path": image["file_path"],
                    "filename": Path(image["file_path"]).name,
                    "mime_type": image["mime_type"],
                    "source": "input_reference",
                    "prompt_text": image.get("task_prompt") or image.get("message_content") or image.get("title"),
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
    selected_reference_images = load_selected_reference_images(params.reference_image_ids, limit=max(0, 3 - len(uploaded)), conversation_id=conversation_id)
    selected_reference_uploads = [
        (Path(item["file_path"]), item.get("mime_type") or "image/png")
        for item in selected_reference_images
    ]
    image_candidates = [
        *build_uploaded_image_candidates(uploaded, params.upload_reference_roles),
        *build_selected_image_candidates(
            selected_reference_images,
            params.reference_image_roles,
            start_order=len(uploaded) + 1,
        ),
    ]
    db.add_prompt(params.prompt, source="auto", mode="storyboard")

    with db.connect() as conn:
        recent_messages = [
            db.row_to_dict(row)
            for row in conn.execute(
                """
                select id, role, content, meta_json, created_at
                from messages
                where conversation_id = ?
                order by id desc
                limit ?
                """,
                (conversation_id, context_limit),
            ).fetchall()
        ]
        recent_messages = list(reversed(recent_messages))
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
                        "upload_reference_roles": params.upload_reference_roles,
                        "context_limit": context_limit,
                        "mode": "storyboard",
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
            "upload_reference_roles": params.upload_reference_roles,
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
        }

        with db.connect() as conn:
            conn.execute(
                "update conversations set previous_response_id = ?, updated_at = ? where id = ?",
                (planner_response_id or previous_response_id, db.now_iso(), conversation_id),
            )
        update_message_content(assistant_message_id, plan["reply"] or "我理解了。", planner_response_id)
        update_message_meta(assistant_message_id, {**raw_for_meta, "planner_status": "done"}, planner_response_id)
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
        lease = await acquire_image_provider_slot(task_id)
        provider = lease["provider"]
        provider_config = provider_client_config(provider)
        raw_for_meta["image_provider"] = {"id": provider["id"], "name": provider["name"]}
        try:
            for index, shot in enumerate(shots, start=1):
                shot_name = normalize_shot_name(str(shot.get("name") or f"镜头{index}"), index)
                shot["name"] = shot_name
                shot["status"] = "running"
                update_storyboard_task_state(task_id, task_payload, storyboard_state)
                progress = min(30 + int((index - 1) / max(total, 1) * 62), 88)
                db.update_task(task_id, progress=progress, stage=f"正在使用 {provider['name']} 生成镜头 {index}/{total}：{shot_name}")
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
                edit_inputs, input_image_notes = build_storyboard_generation_inputs(previous_image, image_candidates)
                action = "edit" if edit_inputs else "generate"
                try:
                    response = await call_responses_image_generation(
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
                    )
                    image_items = extract_images_from_responses(response, output_format, folder=bucket)
                    if not image_items:
                        raise HTTPException(
                            status_code=502,
                            detail={
                                "message": "Responses API 已返回，但没有找到 image_generation_call.result 图片数据。",
                                "endpoint": "responses",
                                "upstream": sanitize_response(response),
                                "suggestion": "请确认当前模型组合支持 image_generation 工具，或更换外层模型/图片工具模型后重试。",
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
                        }
                    )
                    db.add_prompt(continuity_prompt, source="auto", mode="storyboard")
                    update_storyboard_task_state(task_id, task_payload, storyboard_state)
                    db.update_task(
                        task_id,
                        progress=min(32 + int(index / max(total, 1) * 60), 92),
                        stage=f"已通过 {provider['name']} 保存镜头 {index}/{total}：{shot_name}",
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
                    raw_for_meta["image_status"] = "failed"
                    raw_for_meta["image_error"] = exc.detail
                    update_message_meta(assistant_message_id, raw_for_meta, planner_response_id)
                    raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
                except Exception as exc:
                    shot["status"] = "failed"
                    shot["error"] = str(exc)
                    update_storyboard_task_state(task_id, task_payload, storyboard_state)
                    raw_for_meta["image_status"] = "failed"
                    raw_for_meta["image_error"] = str(exc)
                    update_message_meta(assistant_message_id, raw_for_meta, planner_response_id)
                    raise
        finally:
            await release_image_provider_slot(task_id, lease)

        raw_for_meta["image_status"] = "done"
        raw_for_meta["storyboard"] = storyboard_state
        raw_for_meta["shot_results"] = shot_results
        update_message_meta(assistant_message_id, raw_for_meta, planner_response_id)
        db.update_task(task_id, progress=96, stage="正在整理分镜结果")
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
    if old_task.get("status") not in {"failed", "canceled"}:
        raise HTTPException(status_code=400, detail="当前仅支持重试失败或已停止的任务")
    conversation_id = old_task.get("conversation_id")
    if conversation_id:
        with db.connect() as conn:
            ensure_conversation_task_retry_allowed(conn, int(conversation_id), int(old_task["id"]))
    mode = str(old_task.get("mode") or "")
    if mode == "generate":
        params = copy.deepcopy(old_task.get("params") or {})
        request = GenerateRequest(**normalize_text_fields(params))
        retry_payload = compact_params({**params, "retry_of": task_id})
        retry_id = db.create_task(
            "generate",
            f"重试：{old_task.get('prompt') or '普通生图任务'}",
            retry_payload,
            conversation_id=old_task.get("conversation_id"),
            user_message_id=old_task.get("user_message_id"),
        )
        schedule_task(
            retry_id,
            run_generate_task(
                retry_id,
                request,
                retry_payload,
                conversation_id=old_task.get("conversation_id"),
                user_message_id=old_task.get("user_message_id"),
            ),
        )
        return {"task": db.get_task(retry_id)}
    if mode == "edit":
        params = copy.deepcopy(old_task.get("params") or {})
        prompt = str(params.get("prompt") or old_task.get("prompt") or "").strip()
        if not prompt:
            raise HTTPException(status_code=400, detail="该编辑任务缺少可重试的原始提示词")
        input_images: list[tuple[Path, str]] = []
        saved_mask: tuple[Path, str] | None = None
        for image in old_task.get("images", []):
            if image.get("source") not in {"input", "mask"}:
                continue
            path = Path(str(image.get("file_path") or ""))
            if not path.exists():
                continue
            item = (path, str(image.get("mime_type") or "image/png"))
            if image.get("source") == "mask":
                saved_mask = item
            else:
                input_images.append(item)
        if not input_images:
            raise HTTPException(status_code=400, detail="该编辑任务缺少可重试的原始输入图")
        retry_payload = compact_params({**params, "retry_of": task_id})
        retry_id = db.create_task(
            "edit",
            f"重试：{prompt}",
            retry_payload,
            conversation_id=old_task.get("conversation_id"),
            user_message_id=old_task.get("user_message_id"),
        )
        for item in input_images:
            public_input_image(item, source="input", title=prompt, task_id=retry_id, conversation_id=old_task.get("conversation_id"), message_id=old_task.get("user_message_id"))
        if saved_mask:
            public_input_image(saved_mask, source="mask", title=f"{prompt} mask", task_id=retry_id, conversation_id=old_task.get("conversation_id"), message_id=old_task.get("user_message_id"))
        schedule_task(
            retry_id,
            run_edit_task(
                retry_id,
                params,
                prompt,
                input_images,
                saved_mask,
                conversation_id=old_task.get("conversation_id"),
                user_message_id=old_task.get("user_message_id"),
            ),
        )
        return {"task": db.get_task(retry_id)}
    if mode != "storyboard":
        raise HTTPException(status_code=400, detail="当前仅支持重试普通生图、编辑和分镜连续生图任务")
    params = copy.deepcopy(old_task.get("params") or {})
    storyboard = params.get("storyboard") if isinstance(params.get("storyboard"), dict) else {}
    shots = storyboard.get("shots") if isinstance(storyboard.get("shots"), list) else []
    if not shots:
        raise HTTPException(status_code=400, detail="该分镜任务没有可重试的镜头状态")
    for shot in shots:
        if isinstance(shot, dict) and shot.get("status") in {"running", "failed"}:
            shot["status"] = "pending"
            shot.pop("error", None)
    retry_payload = compact_params({**params, "retry_of": task_id})
    retry_id = db.create_task(
        "storyboard",
        f"重试：{old_task.get('prompt') or '分镜任务'}",
        retry_payload,
        conversation_id=old_task.get("conversation_id"),
        user_message_id=old_task.get("user_message_id"),
        assistant_message_id=old_task.get("assistant_message_id"),
    )
    schedule_task(retry_id, run_storyboard_retry_task(retry_id, old_task, retry_payload))
    return {"task": db.get_task(retry_id)}


async def run_storyboard_retry_task(task_id: int, old_task: dict[str, Any], payload: dict[str, Any]) -> None:
    async def worker() -> None:
        storyboard = payload.get("storyboard") if isinstance(payload.get("storyboard"), dict) else {}
        shots = storyboard.get("shots") if isinstance(storyboard.get("shots"), list) else []
        total = len(shots)
        output_format = str(payload.get("output_format", "png"))
        client_config = ClientConfig(**payload.get("config", {})) if isinstance(payload.get("config"), dict) else ClientConfig()
        seed_candidates = load_seed_images_from_payload(payload)
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
        lease = await acquire_image_provider_slot(task_id)
        provider = lease["provider"]
        client_config = provider_client_config(provider)
        try:
            for index, shot in enumerate(shots, start=1):
                if not isinstance(shot, dict) or shot.get("status") == "done":
                    continue
                shot_name = normalize_shot_name(str(shot.get("name") or f"镜头{index}"), index)
                shot["name"] = shot_name
                shot["status"] = "running"
                update_storyboard_task_state(task_id, payload, storyboard)
                db.update_task(task_id, progress=min(25 + int((index - 1) / max(total, 1) * 65), 88), stage=f"正在使用 {provider['name']} 重试镜头 {index}/{total}：{shot_name}")
                edit_inputs, input_image_notes = build_storyboard_generation_inputs(previous_image, seed_candidates)
                action = "edit" if edit_inputs else "generate"
                try:
                    if previous_image is None and index > 1:
                        raise HTTPException(
                            status_code=400,
                            detail={
                                "message": "无法继续分镜：没有找到上一镜头输出图作为 edit 输入。",
                                "suggestion": "请从原对话重新提交分镜需求，或选择一张参考图后重试。",
                            },
                        )
                    response = await call_responses_image_generation(
                        model=str(payload.get("model", "gpt-5.4")),
                        prompt=str(shot.get("execution_prompt") or shot.get("planner_prompt") or shot.get("prompt") or old_task.get("prompt") or ""),
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
                    )
                    image_items = extract_images_from_responses(response, output_format, folder=bucket)
                    if not image_items:
                        raise HTTPException(status_code=502, detail="Responses API 已返回，但没有找到 image_generation_call.result 图片数据。")
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
                    raise
                except Exception as exc:
                    shot["status"] = "failed"
                    shot["error"] = str(exc)
                    update_storyboard_task_state(task_id, payload, storyboard)
                    raise
            db.finish_task(
                task_id,
                {
                    "retry_of": old_task.get("id"),
                    "images": saved_images,
                    "raw": {
                        "storyboard": storyboard,
                        "image_provider": {"id": provider["id"], "name": provider["name"]},
                    },
                },
            )
        finally:
            await release_image_provider_slot(task_id, lease)

    await run_with_slot(task_id, worker)


@app.get("/api/gallery")
def gallery(limit: int = 200) -> dict[str, Any]:
    with db.connect() as conn:
        rows = conn.execute(
            """
            select i.*,
                c.title as conversation_title,
                c.context_limit as conversation_context_limit,
                m.content as message_content,
                t.prompt as task_prompt,
                t.mode as task_mode
            from images i
            left join conversations c on c.id = i.conversation_id
            left join messages m on m.id = i.message_id
            left join tasks t on t.id = i.task_id
            where i.source != 'mask'
            order by datetime(i.created_at) desc, i.id desc
            limit ?
            """,
            (limit,),
        ).fetchall()
    items = [db.row_to_dict(row) for row in rows]
    task_ids = [item["task_id"] for item in items if item.get("task_id")]
    task_map: dict[int, dict[str, Any]] = {}
    if task_ids:
        placeholders = ",".join("?" for _ in task_ids)
        with db.connect() as conn:
            task_rows = conn.execute(f"select * from tasks where id in ({placeholders})", task_ids).fetchall()
        task_map = {int(row["id"]): summarize_task(row) for row in task_rows}
    for item in items:
        task = task_map.get(int(item["task_id"])) if item.get("task_id") else None
        if task:
            enrich_images_with_prompt([item], task)
    return {"items": items}


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
    selected_reference_images = load_selected_reference_images(params.reference_image_ids, limit=max(0, 3 - len(uploaded)), conversation_id=conversation_id)
    selected_reference_uploads = [
        (Path(item["file_path"]), item.get("mime_type") or "image/png")
        for item in selected_reference_images
    ]
    image_candidates = [
        *build_uploaded_image_candidates(uploaded, params.upload_reference_roles),
        *build_selected_image_candidates(
            selected_reference_images,
            params.reference_image_roles,
            start_order=len(uploaded) + 1,
        ),
    ]
    db.add_prompt(params.prompt, source="auto", mode="chat")

    with db.connect() as conn:
        recent_messages = [
            db.row_to_dict(row)
            for row in conn.execute(
                """
                select id, role, content, meta_json, created_at
                from messages
                where conversation_id = ?
                order by id desc
                limit ?
                """,
                (conversation_id, context_limit),
            ).fetchall()
        ]
        recent_messages = list(reversed(recent_messages))
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
                        "upload_reference_roles": params.upload_reference_roles,
                        "context_limit": context_limit,
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
            "partial_images": params.partial_images,
            "context_limit": context_limit,
            "reference_image_ids": [item["id"] for item in selected_reference_images],
            "reference_image_roles": params.reference_image_roles,
            "upload_reference_roles": params.upload_reference_roles,
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
) -> None:
    async def worker() -> None:
        db.update_task(task_id, progress=12, stage="AI 正在理解意图")
        planner_reference_images = [
            (item["path"], item.get("mime_type") or "image/png")
            for item in image_candidates
        ]
        planner_prompt = build_chat_planner_prompt(
            recent_messages,
            params.prompt,
            bool(image_candidates),
            image_candidates=image_candidates,
            attach_reference_images=params.planner_endpoint != "chat_completions",
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
                    "AI 正在理解你的需求...",
                    db.json_dumps({"planner_status": "streaming", "context_limit": context_limit}),
                    db.now_iso(),
                ),
            )
            assistant_message_id = int(cursor.lastrowid)
        db.update_task(task_id, assistant_message_id=assistant_message_id)
        publish_task_event(
            task_id,
            "assistant_start",
            {
                "message": {
                    "id": assistant_message_id,
                    "conversation_id": conversation_id,
                    "role": "assistant",
                    "content": "AI 正在理解你的需求...",
                    "meta": {"planner_status": "streaming", "context_limit": context_limit},
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
            on_stream_event=make_planner_reply_stream_handler(task_id, assistant_message_id, "AI 正在理解你的需求..."),
            planner_endpoint=params.planner_endpoint,
        )
        planner_text = extract_text_from_responses(planner_response)
        plan = parse_planner_json(planner_text)
        reference_uploads = [
            (item["path"], item.get("mime_type") or "image/png")
            for item in image_candidates
        ]
        if plan["should_generate"] and plan["action"] == "edit" and not reference_uploads:
            plan["should_generate"] = False
            plan["reply"] = "我需要你先上传或选择要修改的参考图。最多可以选择 3 张，然后我再按你的意见编辑。"
            plan["reason"] = "planner requested edit but user did not provide selected reference images"
        planner_response_id = planner_response.get("id")
        db.update_task(
            task_id,
            progress=34,
            stage="AI 已完成意图判断，准备生图" if plan["should_generate"] else "AI 已判断无需生图",
        )

        raw_for_meta: dict[str, Any] = {
            "endpoint": "/v1/responses",
            "planner": sanitize_response(planner_response),
            "plan": plan,
            "context_limit": context_limit,
            "image_candidates": sanitize_reference_candidates(image_candidates),
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
        update_message_content(assistant_message_id, plan["reply"] or "我理解了。", planner_response_id)
        update_message_meta(assistant_message_id, {**raw_for_meta, "planner_status": "done"}, planner_response_id)
        publish_task_event(
            task_id,
            "assistant_reply",
            {"message_id": assistant_message_id, "content": plan["reply"] or "我理解了。"},
            snapshot=True,
        )

        if not plan["should_generate"]:
            raw_for_task = {
                "user_message_id": user_message_id,
                "assistant_message_id": assistant_message_id,
                "text": plan["reply"],
                "images": [],
                "fallback": False,
                "raw": raw_for_meta,
            }
            db.finish_task(task_id, raw_for_task)
            return

        image_prompt = plan["image_prompt"] or params.prompt
        action = plan["action"] if plan["action"] in {"generate", "edit", "auto"} else params.action
        if reference_uploads:
            action = "edit"
        edit_inputs = reference_uploads
        input_image_notes = [build_reference_input_note(item, index) for index, item in enumerate(image_candidates, start=1)]
        raw_for_meta["selected_reference_image_refs"] = [item.get("ref") for item in image_candidates]
        raw_for_meta["selected_reference_image_ids"] = [item.get("id") for item in image_candidates if item.get("id")]
        raw_for_meta["resolved_action"] = action
        raw_for_meta["tool"] = "image_generation"
        raw_for_meta["image_prompt"] = image_prompt
        update_message_meta(assistant_message_id, {**raw_for_meta, "image_status": "waiting"}, planner_response_id)
        lease = await acquire_image_provider_slot(
            task_id,
            waiting_stage=f"AI 决定执行 {action}，已分配生图提供商，等待空闲通道",
            running_stage=f"AI 决定执行 {action}，正在使用生图提供商生成图片",
        )
        provider = lease["provider"]
        provider_config = provider_client_config(provider)
        raw_for_meta["image_provider"] = {"id": provider["id"], "name": provider["name"]}
        update_message_meta(
            assistant_message_id,
            {
                **raw_for_meta,
                "image_status": "running",
                "image_stage": f"AI 决定执行 {action}，正在使用 {provider['name']} 生成图片",
            },
            planner_response_id,
        )
        try:
            image_response = await call_responses_image_generation(
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
            )
            db.update_task(task_id, progress=84, stage=f"正在通过 {provider['name']} 提取和保存图片")
            bucket = task_image_folder(task_id, conversation_title)
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
                    },
                )
        except HTTPException as exc:
            raw_for_meta["image_status"] = "failed"
            raw_for_meta["image_error"] = exc.detail
            update_message_meta(
                assistant_message_id,
                raw_for_meta,
                planner_response_id or previous_response_id,
            )
            with db.connect() as conn:
                conn.execute(
                    "update conversations set previous_response_id = ?, updated_at = ? where id = ?",
                    (planner_response_id or previous_response_id, db.now_iso(), conversation_id),
                )
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        finally:
            await release_image_provider_slot(task_id, lease)

        images_out = [
            public_task_image(
                item,
                conversation_id=conversation_id,
                message_id=assistant_message_id,
                task_id=task_id,
                title=conversation_title,
                bucket=bucket,
            )
            for item in image_items
        ]
        raw_for_meta["image_status"] = "done"
        with db.connect() as conn:
            conn.execute(
                "update messages set meta_json = ?, response_id = ?, updated_at = ? where id = ?",
                (
                    db.json_dumps(raw_for_meta),
                    image_response.get("id") or planner_response_id,
                    db.now_iso(),
                    assistant_message_id,
                ),
            )
            conn.execute(
                "update conversations set previous_response_id = ?, updated_at = ? where id = ?",
                (image_response.get("id") or planner_response_id or previous_response_id, db.now_iso(), conversation_id),
            )

        raw_for_task = {
            "user_message_id": user_message_id,
            "assistant_message_id": assistant_message_id,
            "text": plan["reply"],
            "images": images_out,
            "fallback": False,
            "raw": raw_for_meta,
        }
        db.update_task(task_id, assistant_message_id=assistant_message_id, progress=96, stage="正在写入对话历史")
        db.finish_task(task_id, raw_for_task)
    await run_with_slot(task_id, worker)



app.mount("/media/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
app.mount("/media/outputs", StaticFiles(directory=OUTPUT_DIR), name="outputs")


if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")


@app.get("/{path:path}", response_class=HTMLResponse, response_model=None)
def frontend(path: str):
    index_path = FRONTEND_DIST / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return HTMLResponse(
        """
        <html><body style="font-family:sans-serif;padding:32px">
        <h1>GPT Image Studio API is running</h1>
        <p>Frontend has not been built yet. Run <code>bash scripts/install_ubuntu.sh</code>.</p>
        </body></html>
        """
    )
