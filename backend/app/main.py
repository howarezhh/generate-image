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
    extract_images_from_responses,
    extract_text_from_responses,
    post_json,
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


class ClientConfig(BaseModel):
    base_url: str | None = None
    api_key: str | None = None


class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=1)
    model: str = "gpt-5.4"
    image_model: str = "gpt-image-2"
    size: str = "1024x1024"
    quality: str = "auto"
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


class ConversationUpdate(BaseModel):
    title: str | None = None
    context_limit: int | None = Field(default=None, ge=0, le=50)


class MessageUpdate(BaseModel):
    content: str = Field(min_length=1)


class ChatRequest(BaseModel):
    prompt: str = Field(min_length=1)
    model: str = "gpt-5.4"
    image_model: str = "gpt-image-2"
    action: str = "auto"
    size: str = "1024x1024"
    quality: str = "auto"
    background: str = "auto"
    output_format: str = "png"
    output_compression: int | None = Field(default=None, ge=0, le=100)
    moderation: str = "auto"
    input_fidelity: str = "auto"
    partial_images: int = Field(default=0, ge=0, le=3)
    context_limit: int = Field(default=10, ge=0, le=50)
    config: ClientConfig = Field(default_factory=ClientConfig)


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
        "mime_type": mime_type,
        "filename": file_path.name,
        "title": title,
        "bucket": bucket,
    }


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


def compact_error_detail(detail: Any) -> str:
    return json.dumps(detail, ensure_ascii=False, indent=2) if not isinstance(detail, str) else detail


def build_context_prompt(history: list[dict[str, Any]], prompt: str) -> str:
    if not history:
        return prompt
    lines = ["以下是最近的对话上下文，请结合上下文理解当前生图需求："]
    for item in history:
        role = "用户" if item.get("role") == "user" else "助手"
        content = str(item.get("content") or "").strip()
        if content:
            lines.append(f"{role}: {content}")
    lines.append(f"当前用户需求: {prompt}")
    return "\n".join(lines)


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


def build_responses_input(
    *,
    prompt: str,
    uploaded: list[tuple[Path, str]] | None = None,
    mask: tuple[Path, str] | None = None,
    input_fidelity: str | None = None,
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
        content.append(
            {
                "type": "input_text",
                "text": f"Input image {idx}: primary reference image. Preserve its identity/layout unless the prompt explicitly changes it.",
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
    previous_response_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "input": build_responses_input(
            prompt=prompt,
            uploaded=uploaded,
            mask=mask,
            input_fidelity=input_fidelity,
        ),
        "tools": [
            build_image_generation_tool(
                image_model=image_model,
                size=size,
                quality=quality,
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
    return await post_json(
        "responses",
        payload,
        base_url=config.base_url,
        api_key=config.api_key,
        timeout=300.0,
    )


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
        }
    )
    task_id = db.create_task("generate", request.prompt, payload)
    title = request.prompt[:48] or f"task-{task_id}"
    bucket = safe_storage_folder(title, db.now_iso())
    try:
        responses: list[dict[str, Any]] = []
        image_items = []
        for _ in range(request.n):
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
                config=request.config,
            )
            responses.append(sanitize_response(response))
            image_items.extend(extract_images_from_responses(response, request.output_format, folder=bucket))
        if not image_items:
            raise HTTPException(
                status_code=502,
                detail={
                    "message": "Responses API 已返回，但没有找到 image_generation_call.result 图片数据。",
                    "endpoint": "responses",
                    "upstream": responses,
                    "suggestion": "请确认当前模型组合支持 image_generation 工具，或更换外层模型/图片工具模型后重试。",
                },
            )
        images = [public_task_image(item, task_id=task_id, title=title, bucket=bucket) for item in image_items]
        raw = {"endpoint": "/v1/responses", "tool": "image_generation", "responses": responses}
        db.finish_task(task_id, raw)
        return {"task_id": task_id, "images": images, "raw": raw}
    except HTTPException as exc:
        db.fail_task(task_id, compact_error_detail(exc.detail))
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
            "endpoint": "/v1/responses",
            "tool": "image_generation",
            "model": params.get("model", "gpt-5.4"),
            "image_model": params.get("image_model", "gpt-image-2"),
            "prompt": prompt,
            "size": params.get("size", "1024x1024"),
            "quality": params.get("quality", "auto"),
            "n": int(params.get("n", 1)),
            "background": params.get("background", "auto"),
            "output_format": params.get("output_format", "png"),
            "output_compression": params.get("output_compression"),
            "moderation": params.get("moderation", "auto"),
            "input_fidelity": params.get("input_fidelity", "auto"),
            "action": "edit",
            "partial_images": params.get("partial_images"),
        }
    )
    task_id = db.create_task("edit", prompt, payload)
    title = prompt[:48] or f"task-{task_id}"
    bucket = safe_storage_folder(title, db.now_iso())
    try:
        output_format = str(params.get("output_format", "png"))
        responses: list[dict[str, Any]] = []
        image_items = []
        client_config = ClientConfig(**params.get("config", {}))
        for _ in range(int(params.get("n", 1))):
            response = await call_responses_image_generation(
                model=str(params.get("model", "gpt-5.4")),
                prompt=prompt,
                image_model=str(params.get("image_model", "gpt-image-2")),
                size=str(params.get("size", "1024x1024")),
                quality=str(params.get("quality", "auto")),
                output_format=output_format,
                background=params.get("background", "auto"),
                output_compression=params.get("output_compression"),
                moderation=params.get("moderation", "auto"),
                action="edit",
                partial_images=params.get("partial_images"),
                config=client_config,
                uploaded=saved_images,
                mask=(mask_path, mask_mime) if mask is not None else None,
                input_fidelity=str(params.get("input_fidelity", "auto")),
            )
            responses.append(sanitize_response(response))
            image_items.extend(extract_images_from_responses(response, output_format, folder=bucket))
        if not image_items:
            raise HTTPException(
                status_code=502,
                detail={
                    "message": "Responses API 已返回，但没有找到 image_generation_call.result 图片数据。",
                    "endpoint": "responses",
                    "upstream": responses,
                    "suggestion": "请确认当前模型组合支持 image_generation 工具，或更换外层模型/图片工具模型后重试。",
                },
            )
        images_out = [public_task_image(item, task_id=task_id, title=title, bucket=bucket) for item in image_items]
        raw = {"endpoint": "/v1/responses", "tool": "image_generation", "responses": responses}
        db.finish_task(task_id, raw)
        return {"task_id": task_id, "images": images_out, "raw": raw}
    except HTTPException as exc:
        db.fail_task(task_id, compact_error_detail(exc.detail))
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
            insert into conversations (title, context_limit, created_at, updated_at)
            values (?, ?, ?, ?)
            """,
            (request.title, request.context_limit, stamp, stamp),
        )
        conversation_id = int(cursor.lastrowid)
    return {"id": conversation_id, "title": request.title, "context_limit": request.context_limit}


@app.get("/api/conversations")
def list_conversations() -> dict[str, Any]:
    with db.connect() as conn:
        rows = conn.execute(
            """
            select c.*,
                (select count(*) from messages m where m.conversation_id = c.id) as message_count,
                (select count(*) from images i where i.conversation_id = c.id) as image_count
            from conversations c
            order by c.updated_at desc
            """
        ).fetchall()
    return {"items": [db.row_to_dict(row) for row in rows]}


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
    return db.row_to_dict(row)


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
            order by i.id desc
            limit ?
            """,
            (limit,),
        ).fetchall()
    return {"items": [db.row_to_dict(row) for row in rows]}


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
        conversation_title = conversation["title"]
        context_limit = params.context_limit if params.context_limit is not None else conversation["context_limit"]
        context_limit = max(0, min(int(context_limit), 50))
        recent_messages = [
            db.row_to_dict(row)
            for row in conn.execute(
                """
                select id, role, content, created_at
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
                db.json_dumps({"uploads": [str(path) for path, _ in uploaded], "context_limit": context_limit}),
                db.now_iso(),
            ),
        )
        user_message_id = int(cursor.lastrowid)
        conn.execute(
            "update conversations set context_limit = ?, updated_at = ? where id = ?",
            (context_limit, db.now_iso(), conversation_id),
        )

    context_prompt = build_context_prompt(recent_messages, params.prompt)
    response = await call_responses_image_generation(
        model=params.model,
        prompt=context_prompt,
        image_model=params.image_model,
        size=params.size,
        quality=params.quality,
        output_format=params.output_format,
        background=params.background,
        output_compression=params.output_compression,
        moderation=params.moderation,
        action=params.action,
        partial_images=params.partial_images,
        config=params.config,
        uploaded=uploaded,
        input_fidelity=params.input_fidelity,
        previous_response_id=previous_response_id,
    )
    text = extract_text_from_responses(response)
    bucket = safe_storage_folder(conversation_title, db.now_iso())
    image_items = extract_images_from_responses(response, params.output_format, folder=bucket)
    raw_for_meta = {
        "endpoint": "/v1/responses",
        "tool": "image_generation",
        "response": sanitize_response(response),
        "context_limit": context_limit,
    }
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
        public_task_image(
            item,
            conversation_id=conversation_id,
            message_id=assistant_message_id,
            title=conversation_title,
            bucket=bucket,
        )
        for item in image_items
    ]
    return {
        "user_message_id": user_message_id,
        "assistant_message_id": assistant_message_id,
        "text": text,
        "images": images_out,
        "fallback": False,
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
