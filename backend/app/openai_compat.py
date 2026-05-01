import base64
import mimetypes
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
) -> dict[str, Any]:
    url = urljoin(normalize_base_url(base_url) + "/", endpoint.lstrip("/"))
    key = resolve_api_key(api_key)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, headers=headers(key), json=payload)
    return parse_response(response)


async def post_multipart(
    endpoint: str,
    data: dict[str, Any],
    file_fields: list[tuple[str, Path, str]],
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    timeout: float = 300.0,
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
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, headers=headers(key), data=data, files=files)
        return parse_response(response)
    finally:
        for handle in handles:
            handle.close()


def parse_response(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        body = {"raw": response.text}
    if response.status_code >= 400:
        message = body.get("error", body)
        raise HTTPException(status_code=response.status_code, detail=message)
    return body


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


def decode_and_save_image(b64_data: str, *, preferred_format: str = "png") -> tuple[Path, str, str]:
    raw = base64.b64decode(b64_data)
    fmt = (preferred_format or "png").lower()
    mime_type = {
        "jpeg": "image/jpeg",
        "jpg": "image/jpeg",
        "webp": "image/webp",
        "png": "image/png",
    }.get(fmt, "image/png")
    suffix = extension_for_mime(mime_type)
    target = OUTPUT_DIR / f"{unique_name()}{suffix}"
    target.write_bytes(raw)
    public_url = f"/media/outputs/{target.name}"
    return target, public_url, mime_type


def extract_images_from_image_api(response: dict[str, Any], output_format: str) -> list[tuple[Path, str, str]]:
    images = []
    for item in response.get("data", []):
        b64_data = item.get("b64_json")
        if b64_data:
            images.append(decode_and_save_image(b64_data, preferred_format=output_format))
    return images


def extract_images_from_responses(response: dict[str, Any], output_format: str) -> list[tuple[Path, str, str]]:
    images = []
    for item in response.get("output", []):
        if item.get("type") == "image_generation_call" and item.get("result"):
            images.append(decode_and_save_image(item["result"], preferred_format=output_format))
        for part in item.get("content", []):
            image_data = part.get("image_base64") or part.get("b64_json")
            if image_data:
                images.append(decode_and_save_image(image_data, preferred_format=output_format))
    for item in response.get("data", []):
        image_data = item.get("b64_json") or item.get("image_base64")
        if image_data:
            images.append(decode_and_save_image(image_data, preferred_format=output_format))
    return images


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
