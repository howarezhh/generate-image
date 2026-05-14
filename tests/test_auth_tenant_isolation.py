import json

from backend.app import config, database as db
from backend.app import main

from conftest import login_cookie, write_png


def test_login_required_and_tenant_data_isolation(client, isolated_storage):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/auth/login")

    response = client.get("/api/health")
    assert response.status_code == 401

    admin_cookie = login_cookie(client, "hhs54666")
    tenant_cookie = login_cookie(client, "hhs666666")

    response = client.put(
        "/api/settings",
        json={"base_url": "https://admin.example/v1", "api_key": "sk-admin"},
        cookies=admin_cookie,
    )
    assert response.status_code == 200
    response = client.put(
        "/api/settings",
        json={"base_url": "https://tenant.example/v1", "api_key": "sk-tenant"},
        cookies=tenant_cookie,
    )
    assert response.status_code == 200

    assert client.get("/api/settings", cookies=admin_cookie).json()["base_url"] == "https://admin.example/v1"
    assert client.get("/api/settings", cookies=tenant_cookie).json()["base_url"] == "https://tenant.example/v1"

    admin_scope = ""
    tenant_scope = main.access_storage_scope("hhs666666")
    admin_token = config.set_storage_scope(admin_scope)
    try:
        admin_task_id = db.create_task("generate", "admin task", {"prompt": "admin"}, status="queued")
        admin_image_path = write_png(config.current_output_dir() / "admin.png")
        db.add_image(
            source="api",
            file_path=admin_image_path,
            public_url="/media/outputs/admin.png",
            mime_type="image/png",
            task_id=admin_task_id,
            title="admin image",
        )
    finally:
        config.reset_storage_scope(admin_token)

    tenant_token = config.set_storage_scope(tenant_scope)
    try:
        tenant_task_id = db.create_task("generate", "tenant task", {"prompt": "tenant"}, status="queued")
        tenant_image_path = write_png(config.current_output_dir() / "tenant.png")
        db.add_image(
            source="api",
            file_path=tenant_image_path,
            public_url="/media/outputs/tenant.png",
            mime_type="image/png",
            task_id=tenant_task_id,
            title="tenant image",
        )
    finally:
        config.reset_storage_scope(tenant_token)

    admin_tasks = client.get("/api/tasks", cookies=admin_cookie).json()["items"]
    tenant_tasks = client.get("/api/tasks", cookies=tenant_cookie).json()["items"]
    assert {item["prompt"] for item in admin_tasks} == {"admin task"}
    assert {item["prompt"] for item in tenant_tasks} == {"tenant task"}

    admin_gallery = json.dumps(client.get("/api/gallery", cookies=admin_cookie).json(), ensure_ascii=False)
    tenant_gallery = json.dumps(client.get("/api/gallery", cookies=tenant_cookie).json(), ensure_ascii=False)
    assert "admin image" in admin_gallery
    assert "tenant image" not in admin_gallery
    assert "tenant image" in tenant_gallery
    assert "admin image" not in tenant_gallery

    media_response = client.get("/media/outputs/tenant.png", cookies=tenant_cookie)
    assert media_response.status_code == 200
    assert "Cookie" in media_response.headers.get("Vary", "")
    assert media_response.headers.get("Cache-Control", "").startswith("private")

    assert (isolated_storage / "app.db").exists()
    assert (isolated_storage / "tenants" / tenant_scope / "app.db").exists()
