import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import database as db
from .config import (
    DEFAULT_API_BASE_URL,
    DEFAULT_API_KEY,
    FRONTEND_DIST,
    OUTPUT_DIR,
    ROOT_DIR,
    UPLOAD_DIR,
    ensure_dirs,
)
from .openai_compat import (
    data_url_for_file,
    extract_images_from_image_api,
    extract_images_from_responses,
    extract_text_from_responses,
    guess_mime,
    post_json,
    post_multipart,
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


class ClientConfig(BaseModel):
    base_url: str | None = None
    api_key: str | None = None


class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=1)
    model: str = "gpt-image-1"
    size: str = "1024x1024"
    quality: str = "auto"
    n: int = Field(default=1, ge=1, le=10)
    background: str = "auto"
    output_format: str = "png"
    output_compression: int | None = Field(default=None, ge=0, le=100)
    moderation: str = "auto"
    config: ClientConfig = Field(default_factory=ClientConfig)


class ConversationCreate(BaseModel):
    title: str = "新的生图对话"


class ChatRequest(BaseModel):
    prompt: str = Field(min_length=1)
    model: str = "gpt-4.1-mini"
    image_model: str = "gpt-image-1"
    action: str = "auto"
    size: str = "1024x1024"
    quality: str = "auto"
    background: str = "auto"
    output_format: str = "png"
    input_fidelity: str = "auto"
    partial_images: int = Field(default=0, ge=0, le=3)
    config: ClientConfig = Field(default_factory=ClientConfig)


def public_task_image(item: tuple[Path, str, str], *, task_id: int | None = None, conversation_id: int | None = None, message_id: int | None = None) -> dict[str, str]:
    file_path, public_url, mime_type = item
    db.add_image(
        source="api",
        file_path=file_path,
        public_url=public_url,
        mime_type=mime_type,
        task_id=task_id,
        conversation_id=conversation_id,
        message_id=message_id,
    )
    return {"url": public_url, "mime_type": mime_type, "filename": file_path.name}


def compact_params(data: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in data.items()
        if value is not None and value != "" and value != "default"
    }


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
    response_json = item.get("response_json")
    if isinstance(response_json, str) and len(response_json) > 2000:
        item["response_json"] = f"[response omitted, {len(response_json)} chars]"
    return item


@app.on_event("startup")
def startup() -> None:
    ensure_dirs()
    db.init_db()


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


@app.post("/api/images/generate")
async def generate_image(request: GenerateRequest) -> dict[str, Any]:
    payload = compact_params(
        {
            "model": request.model,
            "prompt": request.prompt,
            "size": request.size,
            "quality": request.quality,
            "n": request.n,
            "background": request.background,
            "output_format": request.output_format,
            "output_compression": request.output_compression,
            "moderation": request.moderation,
        }
    )
    task_id = db.create_task("generate", request.prompt, payload)
    try:
        response = await post_json(
            "images/generations",
            payload,
            base_url=request.config.base_url,
            api_key=request.config.api_key,
        )
        images = [
            public_task_image(item, task_id=task_id)
            for item in extract_images_from_image_api(response, request.output_format)
        ]
        raw = sanitize_response(response)
        db.finish_task(task_id, raw)
        return {"task_id": task_id, "images": images, "raw": raw}
    except HTTPException as exc:
        db.fail_task(task_id, json.dumps(exc.detail, ensure_ascii=False))
        raise
    except Exception as exc:
        db.fail_task(task_id, str(exc))
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/images/edit")
async def edit_image(
    params_json: str = Form(...),
    images: list[UploadFile] = File(...),
    mask: UploadFile | None = File(default=None),
) -> dict[str, Any]:
    params = normalize_text_fields(parse_params(params_json))
    prompt = str(params.get("prompt") or "")
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")

    saved_images = [await save_upload(upload) for upload in images]
    file_fields = [("image", path, mime_type) for path, mime_type in saved_images]
    if mask is not None:
        mask_path, mask_mime = await save_upload(mask)
        file_fields.append(("mask", mask_path, mask_mime))

    payload = compact_params(
        {
            "model": params.get("model", "gpt-image-1"),
            "prompt": prompt,
            "size": params.get("size", "1024x1024"),
            "quality": params.get("quality", "auto"),
            "n": int(params.get("n", 1)),
            "background": params.get("background", "auto"),
            "output_format": params.get("output_format", "png"),
            "output_compression": params.get("output_compression"),
            "moderation": params.get("moderation", "auto"),
        }
    )
    task_id = db.create_task("edit", prompt, payload)
    try:
        response = await post_multipart(
            "images/edits",
            payload,
            file_fields,
            base_url=params.get("config", {}).get("base_url"),
            api_key=params.get("config", {}).get("api_key"),
        )
        output_format = str(params.get("output_format", "png"))
        image_items = extract_images_from_image_api(response, output_format)
        images_out = [public_task_image(item, task_id=task_id) for item in image_items]
        raw = sanitize_response(response)
        db.finish_task(task_id, raw)
        return {"task_id": task_id, "images": images_out, "raw": raw}
    except HTTPException as exc:
        db.fail_task(task_id, json.dumps(exc.detail, ensure_ascii=False))
        raise
    except Exception as exc:
        db.fail_task(task_id, str(exc))
        raise HTTPException(status_code=500, detail=str(exc)) from exc


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
    return {"items": tasks}


@app.post("/api/conversations")
def create_conversation(request: ConversationCreate) -> dict[str, Any]:
    stamp = db.now_iso()
    with db.connect() as conn:
        cursor = conn.execute(
            """
            insert into conversations (title, created_at, updated_at)
            values (?, ?, ?)
            """,
            (request.title, stamp, stamp),
        )
        conversation_id = int(cursor.lastrowid)
    return {"id": conversation_id, "title": request.title}


@app.get("/api/conversations")
def list_conversations() -> dict[str, Any]:
    with db.connect() as conn:
        rows = conn.execute("select * from conversations order by updated_at desc").fetchall()
    return {"items": [db.row_to_dict(row) for row in rows]}


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
                "select * from images where conversation_id = ? order by id asc",
                (conversation_id,),
            ).fetchall()
        ]
    return {"conversation": db.row_to_dict(conversation), "messages": messages, "images": images}


@app.post("/api/conversations/{conversation_id}/messages")
async def chat_message(
    conversation_id: int,
    params_json: str = Form(...),
    images: list[UploadFile] | None = File(default=None),
) -> dict[str, Any]:
    params = ChatRequest(**normalize_text_fields(parse_params(params_json)))
    uploaded = [await save_upload(upload) for upload in images or []]

    with db.connect() as conn:
        conversation = conn.execute(
            "select * from conversations where id = ?",
            (conversation_id,),
        ).fetchone()
        if not conversation:
            raise HTTPException(status_code=404, detail="conversation not found")
        previous_response_id = conversation["previous_response_id"]
        cursor = conn.execute(
            """
            insert into messages (conversation_id, role, content, meta_json, created_at)
            values (?, ?, ?, ?, ?)
            """,
            (
                conversation_id,
                "user",
                params.prompt,
                db.json_dumps({"uploads": [str(path) for path, _ in uploaded]}),
                db.now_iso(),
            ),
        )
        user_message_id = int(cursor.lastrowid)

    content: list[dict[str, Any]] = [{"type": "input_text", "text": params.prompt}]
    for path, mime_type in uploaded:
        content.append({"type": "input_image", "image_url": data_url_for_file(path, mime_type)})

    tool: dict[str, Any] = compact_params(
        {
            "type": "image_generation",
            "model": params.image_model,
            "size": params.size,
            "quality": params.quality,
            "background": params.background,
            "output_format": params.output_format,
            "input_fidelity": params.input_fidelity,
            "partial_images": params.partial_images or None,
        }
    )
    if params.action != "auto":
        tool["action"] = params.action

    payload: dict[str, Any] = {
        "model": params.model,
        "input": [{"role": "user", "content": content}],
        "tools": [tool],
    }
    if previous_response_id:
        payload["previous_response_id"] = previous_response_id

    used_fallback = False
    try:
        response = await post_json(
            "responses",
            payload,
            base_url=params.config.base_url,
            api_key=params.config.api_key,
            timeout=300.0,
        )
        text = extract_text_from_responses(response)
        image_items = extract_images_from_responses(response, params.output_format)
        raw_for_meta = sanitize_response(response)
    except HTTPException as exc:
        used_fallback = True
        fallback_payload = compact_params(
            {
                "model": params.image_model,
                "prompt": params.prompt,
                "size": params.size,
                "quality": params.quality,
                "background": params.background,
                "output_format": params.output_format,
            }
        )
        if uploaded:
            response = await post_multipart(
                "images/edits",
                fallback_payload,
                [("image", path, mime_type) for path, mime_type in uploaded],
                base_url=params.config.base_url,
                api_key=params.config.api_key,
                timeout=300.0,
            )
        else:
            response = await post_json(
                "images/generations",
                fallback_payload,
                base_url=params.config.base_url,
                api_key=params.config.api_key,
                timeout=300.0,
            )
        text = "Responses 对话生图不可用，已自动使用 Images API 完成本次生成。"
        image_items = extract_images_from_image_api(response, params.output_format)
        raw_for_meta = {
            "fallback": "images_api",
            "responses_error": exc.detail,
            "response": sanitize_response(response),
        }

    response_id = response.get("id")
    with db.connect() as conn:
        cursor = conn.execute(
            """
            insert into messages (conversation_id, role, content, response_id, meta_json, created_at)
            values (?, ?, ?, ?, ?, ?)
            """,
            (
                conversation_id,
                "assistant",
                text or "已生成图片。",
                response_id,
                db.json_dumps(raw_for_meta),
                db.now_iso(),
            ),
        )
        assistant_message_id = int(cursor.lastrowid)
        conn.execute(
            """
            update conversations
            set previous_response_id = ?, updated_at = ?
            where id = ?
            """,
            (response_id or previous_response_id, db.now_iso(), conversation_id),
        )

    images_out = [
        public_task_image(item, conversation_id=conversation_id, message_id=assistant_message_id)
        for item in image_items
    ]
    return {
        "user_message_id": user_message_id,
        "assistant_message_id": assistant_message_id,
        "text": text,
        "images": images_out,
        "fallback": used_fallback,
        "raw": raw_for_meta,
    }


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
