import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("IMAGE_API_BASE_URL", "https://api.asxs.top/v1")
os.environ.setdefault("IMAGE_API_KEY", "sk-test")
os.environ.setdefault("PORT", "8010")

from backend.app import config, database as db  # noqa: E402
from backend.app import main  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path, monkeypatch):
    storage_dir = tmp_path / "storage"
    monkeypatch.setattr(config, "STORAGE_DIR", storage_dir)
    monkeypatch.setattr(config, "DATABASE_PATH", storage_dir / "app.db")
    monkeypatch.setattr(config, "UPLOAD_DIR", storage_dir / "uploads")
    monkeypatch.setattr(config, "OUTPUT_DIR", storage_dir / "outputs")
    monkeypatch.setattr(config, "TENANT_STORAGE_ROOT", storage_dir / "tenants")
    monkeypatch.setattr(main, "TENANT_STORAGE_ROOT", storage_dir / "tenants")
    monkeypatch.setattr(main, "IMAGE_PROVIDER_POOL_LOCK", None)
    main.IMAGE_PROVIDER_POOL_STATE.clear()
    main.TASK_EVENT_SUBSCRIBERS.clear()
    main.TASK_EVENT_SNAPSHOTS.clear()
    main.RUNNING_TASKS.clear()
    token = config.set_storage_scope("")
    try:
        config.ensure_dirs()
        db.init_db()
        main.refresh_access_password_cache()
        main.ensure_default_provider()
        yield storage_dir
    finally:
        config.reset_storage_scope(token)
        if main.TASK_SCHEDULER_LOOP and not main.TASK_SCHEDULER_LOOP.done():
            main.TASK_SCHEDULER_LOOP.cancel()
        main.TASK_SCHEDULER_LOOP = None
        main.RUNNING_TASKS.clear()
        main.IMAGE_PROVIDER_POOL_STATE.clear()


@pytest.fixture
def client():
    with TestClient(main.app) as test_client:
        yield test_client


def login_cookie(client: TestClient, password: str = "hhs54666") -> dict[str, str]:
    response = client.post(
        "/auth/login",
        data={"password": password, "next": "/"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    token = response.cookies.get(main.ACCESS_COOKIE_NAME)
    assert token
    return {main.ACCESS_COOKIE_NAME: token}


def write_png(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
            "1f15c4890000000a49444154789c6360000002000100ffff030000060005"
            "57bfab0000000049454e44ae426082"
        )
    )
    return path


def create_provider(name: str, base_url: str | None = None, api_key: str = "sk-test") -> int:
    stamp = db.now_iso()
    with db.connect() as conn:
        cursor = conn.execute(
            """
            insert into providers (name, base_url, api_key, created_at, updated_at)
            values (?, ?, ?, ?, ?)
            """,
            (name, base_url or f"https://{name}.example/v1", api_key, stamp, stamp),
        )
        return int(cursor.lastrowid)


def reset_providers(*names: str) -> list[int]:
    with db.connect() as conn:
        conn.execute("delete from providers")
        conn.execute("delete from settings where key = 'app_settings'")
    main.IMAGE_PROVIDER_POOL_STATE.clear()
    return [create_provider(name) for name in names]
