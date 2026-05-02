import asyncio
import json
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
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
TASK_SEMAPHORE: asyncio.Semaphore | None = None


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
    reference_image_ids: list[int] = Field(default_factory=list)
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
    for image in images:
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


def build_chat_planner_prompt(
    history: list[dict[str, Any]],
    prompt: str,
    has_images: bool,
    image_candidates: list[dict[str, Any]] | None = None,
) -> str:
    context = build_context_prompt(history, prompt)
    image_candidates = image_candidates or []
    if image_candidates:
        source_note = "用户本轮已经明确指定了参考图片。" if has_images else "用户本轮没有指定参考图片。"
        lines = [f"{source_note} 已指定参考图如下；如果你决定执行图片修改，应使用这些参考图，不要自行选择其它历史图片："]
        for index, image in enumerate(image_candidates, start=1):
            lines.append(
                f"- 候选{index}: ref={image['ref']}, source={image.get('source')}, "
                f"image_id={image.get('id')}, message_id={image.get('message_id')}, "
                f"task_id={image.get('task_id')}, 提示词/说明={image.get('hint') or '无'}"
            )
        lines.append("候选顺序与随请求附带给你的参考图片顺序一致。")
        image_note = "\n".join(lines)
    else:
        image_note = "本轮用户没有上传或选择参考图片。"
    return f"""
你是一个中文生图对话助手。你的职责不是每次都生图，而是先理解用户意图，再决定是否需要调用生图。

判断规则：
1. 如果用户只是在聊天、询问能力、补充偏好、没有明确画面/修改目标，不要生图，应继续提问或确认需求。
2. 如果用户明确要求生成、重画、修改、继续改图，或上下文已经足够且用户表达了开始/按这个来/生成吧，应生图。
3. 如果用户表达“继续改、修改、调整、保留、换成、参考某张图”等改图意图，但本轮没有指定参考图，不要自行从历史图片里猜测，应要求用户选择或上传参考图。
4. 如果用户在已有图片基础上提出修改意见，应把修改意见融合为新的高质量生图提示词，强调保留不应改变的部分。
5. 生成提示词要完整、具体、适合直接传给 image_generation 工具。
6. 如果用户要改图但你无法从候选中判断应修改哪张图，不要生图，should_generate=false，并请用户明确选择哪张。

{image_note}

请只输出 JSON，不要 Markdown，不要代码块。格式：
{{
  "reply": "给用户看的中文回复。若要生图，说明你将如何生成/修改；若不要生图，提出下一步问题或建议。",
  "should_generate": true 或 false,
  "action": "generate" 或 "edit" 或 "auto",
  "image_prompt": "should_generate 为 true 时填写最终生图提示词，否则为空字符串",
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


def build_uploaded_image_candidates(uploaded: list[tuple[Path, str]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for index, (path, mime_type) in enumerate(uploaded, start=1):
        candidates.append(
            {
                "ref": f"upload:{index}",
                "source": "upload",
                "id": None,
                "message_id": None,
                "task_id": None,
                "path": path,
                "mime_type": mime_type,
                "hint": f"本轮用户上传的第 {index} 张图片",
            }
        )
    return candidates


def build_selected_image_candidates(selected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for item in selected:
        path = Path(item["file_path"])
        if not path.exists():
            continue
        candidates.append(
            {
                "ref": f"image:{item['id']}",
                "source": "selected",
                "id": item["id"],
                "message_id": item.get("message_id"),
                "task_id": item.get("task_id"),
                "path": path,
                "mime_type": item.get("mime_type") or "image/png",
                "hint": item.get("task_prompt") or item.get("message_content") or item.get("title") or "用户指定的历史图片",
            }
        )
    return candidates


def load_selected_reference_images(image_ids: list[int], limit: int = 3) -> list[dict[str, Any]]:
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
    with db.connect() as conn:
        rows = conn.execute(
            f"""
            select i.*,
                   m.content as message_content,
                   t.prompt as task_prompt
            from images i
            left join messages m on m.id = i.message_id
            left join tasks t on t.id = i.task_id
            where i.id in ({placeholders}) and i.source = 'api'
            """,
            clean_ids,
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


async def call_chat_planner(
    *,
    model: str,
    prompt: str,
    config: ClientConfig,
    uploaded: list[tuple[Path, str]] | None = None,
    previous_response_id: str | None = None,
) -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
    for idx, (path, mime_type) in enumerate(uploaded or [], start=1):
        content.append({"type": "input_text", "text": f"Reference image {idx}: user supplied this image for possible edit context."})
        content.append({"type": "input_image", "image_url": data_url_for_file(path, mime_type)})
    payload = responses_payload_for_planner(model=model, content=content, previous_response_id=previous_response_id)
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


def ensure_task_slot() -> None:
    if active_task_count() >= MAX_CONCURRENT_TASKS:
        raise HTTPException(
            status_code=429,
            detail={
                "message": f"当前已有 {MAX_CONCURRENT_TASKS} 个生图任务在运行或排队，请等待其中一个完成后再创建新任务。",
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
    global TASK_SEMAPHORE
    if TASK_SEMAPHORE is None:
        TASK_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
    try:
        db.update_task(task_id, status="queued", progress=3, stage="等待生图通道")
        async with TASK_SEMAPHORE:
            task = db.get_task(task_id)
            if task and task.get("cancel_requested"):
                db.cancel_task(task_id)
                return
            db.update_task(task_id, status="running", progress=8, stage="任务已启动")
            await worker()
    except asyncio.CancelledError:
        db.cancel_task(task_id)
        raise
    except HTTPException as exc:
        db.fail_task(task_id, compact_error_detail(exc.detail))
    except Exception as exc:
        db.fail_task(task_id, str(exc))


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
    global TASK_SEMAPHORE
    ensure_dirs()
    db.init_db()
    ensure_default_provider()
    TASK_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
    with db.connect() as conn:
        conn.execute(
            """
            update tasks
            set status = 'failed', stage = '服务重启后任务已中断', error = ?, updated_at = ?
            where status in ('queued', 'running')
            """,
            ("服务重启后，内存中的后台任务已中断，请重新创建任务。", db.now_iso()),
        )


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
    ensure_default_provider()
    with db.connect() as conn:
        rows = conn.execute("select * from providers order by id asc").fetchall()
    return {"items": [db.row_to_dict(row) for row in rows]}


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
    schedule_task(task_id, run_generate_task(task_id, request, payload))
    return {"task": db.get_task(task_id)}


async def run_generate_task(task_id: int, request: GenerateRequest, payload: dict[str, Any]) -> None:
    async def worker() -> None:
        title = request.prompt[:48] or f"task-{task_id}"
        bucket = task_image_folder(task_id, title)
        responses: list[dict[str, Any]] = []
        saved_images: list[dict[str, Any]] = []
        for index in range(request.n):
            db.update_task(
                task_id,
                progress=min(15 + int(index / max(request.n, 1) * 70), 85),
                stage=f"正在生成第 {index + 1}/{request.n} 张",
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
                config=request.config,
                on_stable_retry=lambda quality: update_timeout_retry_stage(task_id, quality),
                on_stream_event=lambda event: handle_image_stream_event(task_id, event),
            )
            responses.append(sanitize_response(response))
            image_items = extract_images_from_responses(response, request.output_format, folder=bucket)
            saved_images.extend(
                public_task_image(item, task_id=task_id, title=title, bucket=bucket)
                for item in image_items
            )
            db.update_task(
                task_id,
                progress=min(25 + int((index + 1) / max(request.n, 1) * 60), 90),
                stage=f"已保存第 {index + 1}/{request.n} 张结果",
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
        raw = {"endpoint": "/v1/responses", "tool": "image_generation", "responses": responses, "images": saved_images}
        db.finish_task(task_id, raw)
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

    payload = compact_params(
        {
            "endpoint": "/v1/responses",
            "tool": "image_generation",
            "model": params.get("model", "gpt-5.4"),
            "image_model": params.get("image_model", "gpt-image-2"),
            "prompt": prompt,
            "size": params.get("size", "1024x1024"),
            "quality": params.get("quality", "auto"),
            "n": clamp_image_count(params.get("n", 1)),
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
    for item in saved_images:
        public_input_image(item, source="input", title=prompt, task_id=task_id)
    if saved_mask:
        public_input_image(saved_mask, source="mask", title=f"{prompt} mask", task_id=task_id)
    schedule_task(task_id, run_edit_task(task_id, params, prompt, saved_images, saved_mask))
    return {"task": db.get_task(task_id)}


async def run_edit_task(
    task_id: int,
    params: dict[str, Any],
    prompt: str,
    saved_images: list[tuple[Path, str]],
    saved_mask: tuple[Path, str] | None,
) -> None:
    async def worker() -> None:
        title = prompt[:48] or f"task-{task_id}"
        bucket = task_image_folder(task_id, title)
        output_format = str(params.get("output_format", "png"))
        responses: list[dict[str, Any]] = []
        saved_output_images: list[dict[str, Any]] = []
        client_config = ClientConfig(**params.get("config", {}))
        count = clamp_image_count(params.get("n", 1))
        for index in range(count):
            db.update_task(
                task_id,
                progress=min(15 + int(index / max(count, 1) * 70), 85),
                stage=f"正在编辑第 {index + 1}/{count} 张",
            )
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
                mask=saved_mask,
                input_fidelity=str(params.get("input_fidelity", "auto")),
                on_stable_retry=lambda quality: update_timeout_retry_stage(task_id, quality),
                on_stream_event=lambda event: handle_image_stream_event(task_id, event),
            )
            responses.append(sanitize_response(response))
            image_items = extract_images_from_responses(response, output_format, folder=bucket)
            saved_output_images.extend(
                public_task_image(item, task_id=task_id, title=title, bucket=bucket)
                for item in image_items
            )
            db.update_task(
                task_id,
                progress=min(25 + int((index + 1) / max(count, 1) * 60), 90),
                stage=f"已保存第 {index + 1}/{count} 张编辑结果",
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
        raw = {"endpoint": "/v1/responses", "tool": "image_generation", "responses": responses, "images": saved_output_images}
        db.finish_task(task_id, raw)
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
    return {"items": tasks, "max_concurrent": MAX_CONCURRENT_TASKS, "active_count": active_task_count()}


@app.get("/api/tasks/{task_id}")
def get_task(task_id: int) -> dict[str, Any]:
    return {"task": task_with_images(task_id)}


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
    return {"task": task_with_images(task_id)}


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
                (select count(*) from images i where i.conversation_id = c.id and i.source = 'api') as image_count,
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
        for image in load_selected_reference_images(reference_ids, limit=3):
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
    return {"conversation": db.row_to_dict(conversation), "messages": messages, "images": images, "tasks": tasks}


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
            where i.source = 'api'
            order by i.id desc
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
    uploaded = [await save_upload(upload) for upload in images or []]
    selected_reference_images = load_selected_reference_images(params.reference_image_ids, limit=max(0, 3 - len(uploaded)))
    selected_reference_uploads = [
        (Path(item["file_path"]), item.get("mime_type") or "image/png")
        for item in selected_reference_images
    ]
    db.add_prompt(params.prompt, source="auto", mode="chat")

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
                db.json_dumps(
                    {
                        "uploads": [str(path) for path, _ in uploaded],
                        "reference_image_ids": [item["id"] for item in selected_reference_images],
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
            uploaded,
            selected_reference_images,
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
    uploaded: list[tuple[Path, str]],
    selected_reference_images: list[dict[str, Any]],
    previous_response_id: str | None,
    conversation_title: str,
    recent_messages: list[dict[str, Any]],
    context_limit: int,
) -> None:
    async def worker() -> None:
        db.update_task(task_id, progress=12, stage="AI 正在理解意图")
        image_candidates = [
            *build_uploaded_image_candidates(uploaded),
            *build_selected_image_candidates(selected_reference_images),
        ]
        planner_reference_images = [
            (item["path"], item.get("mime_type") or "image/png")
            for item in image_candidates
        ]
        planner_prompt = build_chat_planner_prompt(
            recent_messages,
            params.prompt,
            bool(image_candidates),
            image_candidates=image_candidates,
        )
        planner_response = await call_chat_planner(
            model=params.model,
            prompt=planner_prompt,
            config=params.config,
            uploaded=planner_reference_images,
            previous_response_id=previous_response_id,
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
            "image_candidates": [
                {
                    key: value
                    for key, value in item.items()
                    if key not in {"path"}
                }
                for item in image_candidates
            ],
        }

        with db.connect() as conn:
            cursor = conn.execute(
                """
                insert into messages (conversation_id, role, content, response_id, meta_json, created_at)
                values (?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    "assistant",
                    plan["reply"] or "我理解了。",
                    planner_response_id,
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
                (planner_response_id or previous_response_id, db.now_iso(), conversation_id),
            )

        db.update_task(task_id, assistant_message_id=assistant_message_id)

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
        raw_for_meta["selected_reference_image_refs"] = [item.get("ref") for item in image_candidates]
        raw_for_meta["selected_reference_image_ids"] = [item.get("id") for item in image_candidates if item.get("id")]
        raw_for_meta["resolved_action"] = action
        db.update_task(task_id, progress=48, stage=f"AI 决定执行 {action}，正在生成图片")
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
            config=params.config,
            uploaded=edit_inputs,
            input_fidelity=params.input_fidelity,
            previous_response_id=planner_response_id or previous_response_id,
            on_stable_retry=lambda quality: update_timeout_retry_stage(task_id, quality),
            on_stream_event=lambda event: handle_image_stream_event(task_id, event),
        )
        db.update_task(task_id, progress=84, stage="正在提取和保存图片")
        bucket = task_image_folder(task_id, conversation_title)
        image_items = extract_images_from_responses(image_response, params.output_format, folder=bucket)
        raw_for_meta["tool"] = "image_generation"
        raw_for_meta["image_prompt"] = image_prompt
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
