import asyncio
import json

import pytest
from fastapi import HTTPException

from backend.app import database as db
from backend.app import main

from conftest import reset_providers


def unavailable(status_code: int, message: str = "upstream timeout") -> HTTPException:
    return HTTPException(status_code=status_code, detail={"message": message, "code": "timeout"})


@pytest.mark.parametrize("status_code", [429, 502, 503, 504])
def test_provider_failover_retries_same_provider_then_switches(status_code):
    reset_providers("p1", "p2")
    task_id = db.create_task("generate", "failover", {"prompt": "x"}, status="running")
    calls: list[str] = []

    async def execute(provider, _config):
        calls.append(provider["name"])
        if provider["name"] == "p1":
            raise unavailable(status_code)
        return {"ok": True, "provider": provider["name"]}

    result, provider, attempts = asyncio.run(main.execute_with_provider_failover(task_id, execute))

    assert result["provider"] == "p2"
    assert provider["name"] == "p2"
    assert calls == ["p1", "p1", "p2"]
    assert [item["action"] for item in attempts] == [
        "retrying_same_provider",
        "provider_unavailable",
        "success",
    ]
    task = db.get_task(task_id)
    checkpoint = json.loads(task["checkpoint_json"])
    assert checkpoint["image_provider_name"] == "p2"


def test_provider_pool_prefers_idle_provider():
    reset_providers("busy", "idle")
    task_id = db.create_task("generate", "idle", {"prompt": "x"}, status="running")
    pool = main.load_image_provider_pool()
    busy_state = main.ensure_provider_pool_state(pool[0], 0)
    busy_state["running_count"] = 1
    chosen: list[str] = []

    async def execute(provider, _config):
        chosen.append(provider["name"])
        return {"ok": True}

    asyncio.run(main.execute_with_provider_failover(task_id, execute))

    assert chosen == ["idle"]


def test_single_provider_concurrency_limit(monkeypatch):
    reset_providers("only")
    monkeypatch.setattr(main, "MAX_CONCURRENT_TASKS", 1)
    main.IMAGE_PROVIDER_POOL_STATE.clear()
    task_a = db.create_task("generate", "a", {"prompt": "a"}, status="running")
    task_b = db.create_task("generate", "b", {"prompt": "b"}, status="running")
    active = 0
    max_active = 0

    async def execute(_provider, _config):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return {"ok": True}

    async def run_two():
        await asyncio.gather(
            main.execute_with_provider_failover(task_a, execute),
            main.execute_with_provider_failover(task_b, execute),
        )

    asyncio.run(run_two())

    assert max_active == 1


def test_all_providers_unavailable_schedules_provider_pool_retry():
    reset_providers("p1")
    task_id = db.create_task("generate", "retry", {"prompt": "x"}, status="running")
    detail = {
        "message": "当前生图池中的所有提供商都暂时不可用，无法继续自动切换生图线路。",
        "providers": [{"id": 1, "name": "p1"}],
    }
    exc = HTTPException(status_code=503, detail=detail)

    retry_state = main.schedule_provider_pool_auto_retry(task_id, exc)

    task = db.get_task(task_id)
    checkpoint = json.loads(task["checkpoint_json"])
    assert retry_state["delay_seconds"] == 1800
    assert task["status"] == "scheduled"
    assert task["scheduled_for"]
    assert checkpoint["auto_retry"]["scheduled_count"] == 1
    assert checkpoint["auto_retry"]["retry_delays_seconds"] == [1800, 3600, 7200, 18000]
