import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import DATABASE_PATH, ensure_dirs


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    ensure_dirs()
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            create table if not exists settings (
                key text primary key,
                value text not null,
                updated_at text not null
            );

            create table if not exists providers (
                id integer primary key autoincrement,
                name text not null,
                base_url text not null,
                api_key text not null,
                created_at text not null,
                updated_at text not null
            );

            create table if not exists tasks (
                id integer primary key autoincrement,
                mode text not null,
                prompt text not null,
                status text not null,
                progress integer not null default 0,
                stage text,
                params_json text not null,
                response_json text,
                error text,
                conversation_id integer,
                user_message_id integer,
                assistant_message_id integer,
                cancel_requested integer not null default 0,
                created_at text not null,
                updated_at text not null
            );

            create table if not exists images (
                id integer primary key autoincrement,
                task_id integer,
                conversation_id integer,
                message_id integer,
                title text,
                bucket text,
                source text not null,
                file_path text not null,
                public_url text not null,
                mime_type text not null,
                created_at text not null
            );

            create table if not exists conversations (
                id integer primary key autoincrement,
                title text not null,
                context_limit integer not null default 10,
                previous_response_id text,
                created_at text not null,
                updated_at text not null
            );

            create table if not exists messages (
                id integer primary key autoincrement,
                conversation_id integer not null,
                role text not null,
                content text not null,
                response_id text,
                meta_json text not null,
                created_at text not null,
                updated_at text
            );

            create table if not exists prompts (
                id integer primary key autoincrement,
                content text not null,
                source text not null default 'manual',
                mode text,
                created_at text not null,
                updated_at text not null
            );
            """
        )
        ensure_column(conn, "images", "title", "text")
        ensure_column(conn, "images", "bucket", "text")
        ensure_column(conn, "conversations", "context_limit", "integer not null default 10")
        ensure_column(conn, "messages", "updated_at", "text")
        ensure_column(conn, "tasks", "progress", "integer not null default 0")
        ensure_column(conn, "tasks", "stage", "text")
        ensure_column(conn, "tasks", "conversation_id", "integer")
        ensure_column(conn, "tasks", "user_message_id", "integer")
        ensure_column(conn, "tasks", "assistant_message_id", "integer")
        ensure_column(conn, "tasks", "cancel_requested", "integer not null default 0")
        ensure_column(conn, "prompts", "source", "text not null default 'manual'")
        ensure_column(conn, "prompts", "mode", "text")


def add_prompt(content: str, *, source: str = "manual", mode: str | None = None) -> int | None:
    text = content.strip()
    if not text:
        return None
    stamp = now_iso()
    with connect() as conn:
        cursor = conn.execute(
            """
            insert into prompts (content, source, mode, created_at, updated_at)
            values (?, ?, ?, ?, ?)
            """,
            (text, source, mode, stamp, stamp),
        )
        return int(cursor.lastrowid)


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"pragma table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"alter table {table} add column {column} {definition}")


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def create_task(
    mode: str,
    prompt: str,
    params: dict[str, Any],
    *,
    status: str = "queued",
    conversation_id: int | None = None,
    user_message_id: int | None = None,
    assistant_message_id: int | None = None,
) -> int:
    stamp = now_iso()
    with connect() as conn:
        cursor = conn.execute(
            """
            insert into tasks
                (mode, prompt, status, progress, stage, params_json, conversation_id, user_message_id, assistant_message_id, created_at, updated_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mode,
                prompt,
                status,
                0 if status == "queued" else 5,
                "排队中" if status == "queued" else "准备开始",
                json_dumps(params),
                conversation_id,
                user_message_id,
                assistant_message_id,
                stamp,
                stamp,
            ),
        )
        return int(cursor.lastrowid)


def finish_task(task_id: int, response: Any) -> None:
    with connect() as conn:
        conn.execute(
            """
            update tasks
            set status = ?, progress = ?, stage = ?, response_json = ?, updated_at = ?
            where id = ?
            """,
            ("done", 100, "已完成", json_dumps(response), now_iso(), task_id),
        )


def fail_task(task_id: int, error: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            update tasks
            set status = ?, stage = ?, error = ?, updated_at = ?
            where id = ?
            """,
            ("failed", "失败", error, now_iso(), task_id),
        )


def update_task(task_id: int, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = now_iso()
    assignments = ", ".join(f"{key} = ?" for key in fields)
    values = list(fields.values())
    values.append(task_id)
    with connect() as conn:
        conn.execute(f"update tasks set {assignments} where id = ?", values)


def cancel_task(task_id: int, reason: str = "用户已停止任务") -> None:
    update_task(task_id, status="canceled", stage="已停止", error=reason, cancel_requested=1)


def get_task(task_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("select * from tasks where id = ?", (task_id,)).fetchone()
    return row_to_dict(row) if row else None


def add_image(
    *,
    source: str,
    file_path: Path,
    public_url: str,
    mime_type: str,
    title: str | None = None,
    bucket: str | None = None,
    task_id: int | None = None,
    conversation_id: int | None = None,
    message_id: int | None = None,
) -> int:
    with connect() as conn:
        cursor = conn.execute(
            """
            insert into images
                (task_id, conversation_id, message_id, title, bucket, source, file_path, public_url, mime_type, created_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                conversation_id,
                message_id,
                title,
                bucket,
                source,
                str(file_path),
                public_url,
                mime_type,
                now_iso(),
            ),
        )
        return int(cursor.lastrowid)
