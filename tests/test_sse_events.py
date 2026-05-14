from backend.app import database as db
from backend.app import main

from conftest import login_cookie


def test_task_sse_replays_task_level_events(client):
    cookie = login_cookie(client)
    task_id = db.create_task("chat", "sse", {"prompt": "sse"}, status="running")
    expected_events = [
        "assistant_start",
        "assistant_reply",
        "assistant_plan",
        "task_update",
        "storyboard_image",
        "done",
    ]
    for event_name in expected_events:
        main.publish_task_event(task_id, event_name, {"task_id": task_id, "event_name": event_name}, snapshot=True)

    with client.stream("GET", f"/api/tasks/{task_id}/events", cookies=cookie) as response:
        assert response.status_code == 200
        body = ""
        for line in response.iter_lines():
            body += f"{line}\n"
            if "event: done" in body:
                break

    for event_name in expected_events:
        assert f"event: {event_name}" in body
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["Cache-Control"] == "no-cache"
