import asyncio
import json

from fastapi import HTTPException

from backend.app import database as db
from backend.app import main


def test_run_with_slot_state_machine_for_all_modes():
    for mode in ("generate", "edit", "chat", "storyboard"):
        task_id = db.create_task(mode, f"{mode} prompt", {"prompt": mode}, status="queued")

        async def succeed(task_id=task_id):
            db.finish_task(task_id, {"ok": True})

        asyncio.run(main.run_with_slot(task_id, succeed))
        task = db.get_task(task_id)
        checkpoint = json.loads(task["checkpoint_json"])
        assert task["status"] == "done"
        assert task["progress"] == 100
        assert checkpoint["last_status"] == "done"
        assert checkpoint["can_resume"] is False


def test_run_with_slot_failure_and_cancel_update_checkpoint():
    async def fail():
        raise HTTPException(status_code=400, detail={"message": "bad request"})

    failed_id = db.create_task("generate", "fail", {"prompt": "fail"}, status="queued")
    asyncio.run(main.run_with_slot(failed_id, fail))
    failed = db.get_task(failed_id)
    failed_checkpoint = json.loads(failed["checkpoint_json"])
    assert failed["status"] == "failed"
    assert failed_checkpoint["last_status"] == "failed"
    assert failed_checkpoint["last_error"]["message"] == "bad request"

    canceled_id = db.create_task("generate", "cancel", {"prompt": "cancel"}, status="queued")
    db.update_task(canceled_id, cancel_requested=1)
    asyncio.run(main.run_with_slot(canceled_id, fail))
    canceled = db.get_task(canceled_id)
    canceled_checkpoint = json.loads(canceled["checkpoint_json"])
    assert canceled["status"] == "canceled"
    assert canceled_checkpoint["last_status"] == "canceled"


def test_manual_retry_reuses_original_task_id():
    task_id = db.create_task("generate", "retry", {"prompt": "retry"}, status="queued")
    main.persist_task_checkpoint(
        task_id,
        mode="generate",
        step="image_saved",
        can_resume=True,
        completed_count=1,
        total_count=2,
    )
    db.fail_task(task_id, "temporary failure")
    task = main.task_with_images(task_id)
    checkpoint = main.task_checkpoint_dict(task)

    retried = main.requeue_task_for_manual_retry(task, checkpoint)

    assert retried["id"] == task_id
    assert retried["status"] == "scheduled"
    assert "继续" in retried["stage"]
    assert db.get_task(task_id)["error"] is None


def test_same_conversation_scheduled_tasks_dispatch_one_at_a_time(monkeypatch):
    with db.connect() as conn:
        cursor = conn.execute(
            """
            insert into conversations (title, mode, context_limit, created_at, updated_at)
            values (?, ?, ?, ?, ?)
            """,
            ("serial", "generate", 10, db.now_iso(), db.now_iso()),
        )
        conversation_id = int(cursor.lastrowid)
    first = db.create_task("generate", "first", {"prompt": "first"}, status="scheduled", conversation_id=conversation_id)
    second = db.create_task("generate", "second", {"prompt": "second"}, status="scheduled", conversation_id=conversation_id)
    dispatched: list[int] = []

    def fake_schedule(task):
        dispatched.append(int(task["id"]))

    monkeypatch.setattr(main, "schedule_existing_task", fake_schedule)
    monkeypatch.setattr(main, "provider_pool_capacity", lambda: 10)

    asyncio.run(main.dispatch_scheduled_tasks_once())

    assert dispatched == [first]
    first_task = db.get_task(first)
    second_task = db.get_task(second)
    assert "准备启动" in first_task["stage"]
    assert second_task["status"] == "scheduled"
    assert "等待同会话前序任务完成" in second_task["stage"]
