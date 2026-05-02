import asyncio
import base64
import json
import mimetypes
import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
from fastapi import HTTPException, UploadFile

from .config import DEFAULT_API_BASE_URL, DEFAULT_API_KEY, OUTPUT_DIR, UPLOAD_DIR


def normalize_base_url(base_url: str | None) -> str:
    url = (base_url or DEFAULT_API_BASE_URL or "https://api.openai.com").strip()
    url = url.rstrip("/")
    if not url.endswith("/v1"):
        url = f"{url}/v1"
    return url


def resolve_api_key(api_key: str | None) -> str:
    key = (api_key or DEFAULT_API_KEY or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="API key is required")
    return key


def headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


async def post_json(
    endpoint: str,
    payload: dict[str, Any],
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    timeout: float = 240.0,
    max_attempts: int = 2,
) -> dict[str, Any]:
    url = urljoin(normalize_base_url(base_url) + "/", endpoint.lstrip("/"))
    key = resolve_api_key(api_key)
    last_network_error: httpx.RequestError | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, headers=headers(key), json=payload)
        except httpx.RequestError as exc:
            last_network_error = exc
            if attempt < max_attempts:
                await asyncio.sleep(backoff_seconds(attempt, None))
                continue
            raise HTTPException(status_code=502, detail=network_error_detail(endpoint, url, exc)) from exc
        if should_retry_response(response) and attempt < max_attempts:
            await asyncio.sleep(backoff_seconds(attempt, response))
            continue
        break
    else:
        assert last_network_error is not None
        raise HTTPException(status_code=502, detail=network_error_detail(endpoint, url, last_network_error))
    return parse_response(response, endpoint=endpoint, url=url, payload=payload)


async def post_json_stream(
    endpoint: str,
    payload: dict[str, Any],
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    timeout: float = 240.0,
    on_event: Any | None = None,
) -> dict[str, Any]:
    url = urljoin(normalize_base_url(base_url) + "/", endpoint.lstrip("/"))
    key = resolve_api_key(api_key)
    stream_payload = {**payload, "stream": True}
    events: list[dict[str, Any]] = []
    output_items: list[dict[str, Any]] = []
    terminal_response: dict[str, Any] | None = None
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", url, headers=headers(key), json=stream_payload) as response:
                if response.status_code >= 400:
                    body = await response.aread()
                    parsed = httpx.Response(
                        response.status_code,
                        content=body,
                        headers=response.headers,
                        request=response.request,
                    )
                    return parse_response(parsed, endpoint=endpoint, url=url, payload=payload)
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw or raw == "[DONE]":
                        continue
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    events.append(event)
                    if on_event is not None:
                        on_event(event)
                    event_type = str(event.get("type") or "")
                    if event_type == "response.output_item.done":
                        item = event.get("item")
                        if isinstance(item, dict):
                            output_items.append(item)
                    elif event_type in {"response.completed", "response.failed"}:
                        response_obj = event.get("response")
                        if isinstance(response_obj, dict):
                            terminal_response = response_obj
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=network_error_detail(endpoint, url, exc)) from exc
    result = terminal_response or {"id": None, "output": []}
    if output_items:
        existing = result.get("output")
        if not isinstance(existing, list) or not any(
            isinstance(item, dict) and item.get("type") == "image_generation_call" and item.get("result")
            for item in existing
        ):
            result["output"] = output_items
    result["_stream_events"] = summarize_stream_events(events)
    validate_responses_result(result, endpoint=endpoint, url=url, payload=payload)
    return result


async def post_multipart(
    endpoint: str,
    data: dict[str, Any],
    file_fields: list[tuple[str, Path, str]],
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    timeout: float = 300.0,
    max_attempts: int = 2,
) -> dict[str, Any]:
    url = urljoin(normalize_base_url(base_url) + "/", endpoint.lstrip("/"))
    key = resolve_api_key(api_key)
    files = []
    handles = []
    try:
        for field_name, path, mime_type in file_fields:
            handle = path.open("rb")
            handles.append(handle)
            files.append((field_name, (path.name, handle, mime_type)))
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, headers=headers(key), data=data, files=files)
        except httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail=network_error_detail(endpoint, url, exc)) from exc
        return parse_response(response, endpoint=endpoint, url=url, payload=data)
    finally:
        for handle in handles:
            handle.close()


def should_retry_response(response: httpx.Response) -> bool:
    return response.status_code == 429 or 500 <= response.status_code <= 599


def backoff_seconds(attempt: int, response: httpx.Response | None) -> float:
    if response is not None:
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                return min(float(retry_after), 30.0)
            except ValueError:
                pass
    return min(2.0**attempt, 10.0)


def network_error_detail(endpoint: str, url: str, exc: httpx.RequestError) -> dict[str, Any]:
    return {
        "message": "请求生图接口失败，未收到有效响应。",
        "endpoint": endpoint,
        "url": url,
        "error_type": exc.__class__.__name__,
        "error": str(exc),
        "suggestion": "请检查接口地址是否包含正确的 /v1 路径、服务器是否能访问该地址，以及密钥是否有效。",
    }


def parse_response(
    response: httpx.Response,
    *,
    endpoint: str,
    url: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        text = response.text
        body = {"raw": text, "content_type": response.headers.get("content-type", "")}
        if looks_like_html(text):
            body["html_error"] = summarize_html_error(text)
    if response.status_code >= 400:
        upstream = body.get("error", body) if isinstance(body, dict) else body
        raise HTTPException(
            status_code=response.status_code,
            detail={
                "message": readable_error_message(upstream, response.status_code),
                "status_code": response.status_code,
                "endpoint": endpoint,
                "url": url,
                "upstream": upstream,
                "request": summarize_payload(payload or {}),
                "suggestion": suggestion_for_status(response.status_code, endpoint, upstream),
            },
        )
    validate_responses_result(body, endpoint=endpoint, url=url, payload=payload)
    return body


def validate_responses_result(
    body: Any,
    *,
    endpoint: str,
    url: str,
    payload: dict[str, Any] | None = None,
) -> None:
    if endpoint.strip("/") != "responses" or not isinstance(body, dict):
        return
    response_error = body.get("error")
    status = str(body.get("status") or "")
    if not response_error and status not in {"failed", "incomplete"}:
        return
    message = readable_response_failure_message(body)
    code = ""
    if isinstance(response_error, dict):
        code = str(response_error.get("code") or "")
    raise HTTPException(
        status_code=400 if code in {"moderation_blocked", "content_policy_violation"} else 502,
        detail={
            "message": message,
            "status_code": 400 if code in {"moderation_blocked", "content_policy_violation"} else 502,
            "endpoint": endpoint,
            "url": url,
            "upstream": sanitize_response(body),
            "request": summarize_payload(payload or {}),
            "suggestion": suggestion_for_response_failure(code, message),
        },
    )


def readable_response_failure_message(body: dict[str, Any]) -> str:
    response_error = body.get("error")
    if isinstance(response_error, dict):
        code = response_error.get("code")
        message = response_error.get("message")
        if code and message:
            return f"{code}: {message}"
        if message:
            return str(message)
    if body.get("status"):
        return f"Responses API 返回失败状态：{body.get('status')}"
    return "Responses API 返回失败状态。"


def suggestion_for_response_failure(code: str, message: str) -> str:
    text = f"{code} {message}".lower()
    if "moderation" in text or "safety" in text or "sexual" in text:
        return "上游安全系统拒绝了本次生图请求。请调整提示词，避免露骨性内容、未成年人、强暗示姿势或过度色情化描述；可以改成时尚写真、优雅造型、电影感人像等更安全的表达。"
    if "model" in text:
        return "接口可能不支持当前模型或图片工具，请更换 Responses 模型或图片工具模型后重试。"
    return "上游返回失败状态，请复制完整原因并结合接口地址、模型和提示词排查。"


def readable_error_message(upstream: Any, status_code: int) -> str:
    if isinstance(upstream, dict):
        html_error = upstream.get("html_error")
        if isinstance(html_error, str) and html_error.strip():
            return html_error.strip()
        for key in ("message", "detail", "raw", "error"):
            value = upstream.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    if isinstance(upstream, str) and upstream.strip():
        return upstream.strip()
    return f"生图接口返回 HTTP {status_code}"


def suggestion_for_status(status_code: int, endpoint: str, upstream: Any) -> str:
    text = readable_error_message(upstream, status_code).lower()
    if status_code in {524, 522, 520} or "timeout" in text:
        return "上游接口超时或网关异常。项目会按配置自动重试；如果仍失败，请降低分辨率/清晰度、把并发降到 1，或改用不经过短超时网关的提供商。"
    if status_code == 404:
        return f"当前接口没有找到 /{endpoint}。如果你使用中转服务，请确认它支持该 OpenAI 兼容路径，或换成它文档里的 base_url。"
    if status_code == 429:
        return "上游接口限流。项目会按配置自动重试；如果仍失败，请稍等几十秒再重试，或降低并发/减少连续生图次数。"
    if status_code in {401, 403}:
        return "密钥校验失败或没有该模型权限，请检查前端保存的密钥和模型名称。"
    if "model" in text:
        return "接口可能不支持当前模型，请在模型设置里换一个可用模型后重试。"
    return "请复制完整失败原因，结合接口地址、模型和上游返回内容排查。"


def looks_like_html(text: str) -> bool:
    snippet = text[:512].lower()
    return "<!doctype html" in snippet or "<html" in snippet or "<head" in snippet


def summarize_html_error(text: str) -> str:
    title_match = re.search(r"<title[^>]*>(.*?)</title>", text, flags=re.IGNORECASE | re.DOTALL)
    h1_match = re.search(r"<h1[^>]*>(.*?)</h1>", text, flags=re.IGNORECASE | re.DOTALL)
    title = clean_html_text(title_match.group(1)) if title_match else ""
    heading = clean_html_text(h1_match.group(1)) if h1_match else ""
    message = title or heading or "上游接口返回了 HTML 错误页"
    if heading and heading not in message:
        message = f"{message}：{heading}"
    return message


def clean_html_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def summarize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key, value in payload.items():
        if key == "prompt" and isinstance(value, str):
            summary[key] = value[:240]
        elif key == "input":
            summary[key] = "[conversation input omitted]"
        elif key in {"image", "mask", "file"}:
            summary[key] = "[file omitted]"
        else:
            summary[key] = value
    return summary


async def save_upload(upload: UploadFile) -> tuple[Path, str]:
    suffix = Path(upload.filename or "upload.bin").suffix
    if not suffix:
        suffix = mimetypes.guess_extension(upload.content_type or "") or ".bin"
    target = UPLOAD_DIR / f"{unique_name()}{suffix}"
    content = await upload.read()
    target.write_bytes(content)
    return target, upload.content_type or guess_mime(target)


def unique_name() -> str:
    import uuid

    return uuid.uuid4().hex


def guess_mime(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def extension_for_mime(mime_type: str) -> str:
    if mime_type == "image/jpeg":
        return ".jpg"
    if mime_type == "image/webp":
        return ".webp"
    return ".png"


def data_url_for_file(path: Path, mime_type: str | None = None) -> str:
    mime = mime_type or guess_mime(path)
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def decode_and_save_image(
    b64_data: str,
    *,
    preferred_format: str = "png",
    folder: str | None = None,
) -> tuple[Path, str, str]:
    raw = base64.b64decode(b64_data)
    fmt = (preferred_format or "png").lower()
    mime_type = {
        "jpeg": "image/jpeg",
        "jpg": "image/jpeg",
        "webp": "image/webp",
        "png": "image/png",
    }.get(fmt, "image/png")
    suffix = extension_for_mime(mime_type)
    base = OUTPUT_DIR / folder if folder else OUTPUT_DIR
    base.mkdir(parents=True, exist_ok=True)
    target = base / f"{unique_name()}{suffix}"
    target.write_bytes(raw)
    public_url = "/media/outputs/" + target.relative_to(OUTPUT_DIR).as_posix()
    return target, public_url, mime_type


def extract_images_from_image_api(
    response: dict[str, Any],
    output_format: str,
    *,
    folder: str | None = None,
) -> list[tuple[Path, str, str]]:
    images = []
    for item in response.get("data", []):
        b64_data = item.get("b64_json")
        if b64_data:
            images.append(decode_and_save_image(b64_data, preferred_format=output_format, folder=folder))
    return images


def extract_images_from_responses(
    response: dict[str, Any],
    output_format: str,
    *,
    folder: str | None = None,
) -> list[tuple[Path, str, str]]:
    images = []
    for item in response.get("output", []):
        if item.get("type") == "image_generation_call" and item.get("result"):
            images.append(decode_and_save_image(item["result"], preferred_format=output_format, folder=folder))
        for part in item.get("content", []):
            image_data = part.get("image_base64") or part.get("b64_json")
            if image_data:
                images.append(decode_and_save_image(image_data, preferred_format=output_format, folder=folder))
    for item in response.get("data", []):
        image_data = item.get("b64_json") or item.get("image_base64")
        if image_data:
            images.append(decode_and_save_image(image_data, preferred_format=output_format, folder=folder))
    return images


def safe_storage_folder(title: str | None, created_at: str | None = None) -> str:
    raw_title = (title or "untitled").strip()[:48] or "untitled"
    safe_title = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "-", raw_title).strip("-") or "untitled"
    stamp = (created_at or "").replace(":", "").replace(".", "").replace("+", "-")
    if not stamp:
        from datetime import datetime

        stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    return f"{safe_title}/{stamp[:32]}"


def extract_text_from_responses(response: dict[str, Any]) -> str:
    chunks: list[str] = []
    if response.get("output_text"):
        chunks.append(str(response["output_text"]))
    for item in response.get("output", []):
        if item.get("type") == "message":
            for part in item.get("content", []):
                if part.get("type") in {"output_text", "text"} and part.get("text"):
                    chunks.append(part["text"])
    return "\n".join(chunks).strip()


def sanitize_response(value: Any) -> Any:
    if isinstance(value, list):
        return [sanitize_response(item) for item in value]
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            if key in {"b64_json", "result", "image_base64"} and isinstance(item, str) and len(item) > 128:
                sanitized[key] = f"[base64 image omitted, {len(item)} chars]"
            else:
                sanitized[key] = sanitize_response(item)
        return sanitized
    return value


def summarize_stream_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for event in events:
        item = {
            "type": event.get("type"),
            "sequence_number": event.get("sequence_number"),
        }
        if event.get("type") == "response.image_generation_call.partial_image":
            item["partial_image_index"] = event.get("partial_image_index")
            item["partial_image_b64"] = "[partial image omitted]"
        summary.append({key: value for key, value in item.items() if value is not None})
    return summary
