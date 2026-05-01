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

            create table if not exists tasks (
                id integer primary key autoincrement,
                mode text not null,
                prompt text not null,
                status text not null,
                params_json text not null,
                response_json text,
                error text,
                created_at text not null,
                updated_at text not null
            );

            create table if not exists images (
                id integer primary key autoincrement,
                task_id integer,
                conversation_id integer,
                message_id integer,
                source text not null,
                file_path text not null,
                public_url text not null,
                mime_type text not null,
                created_at text not null
            );

            create table if not exists conversations (
                id integer primary key autoincrement,
                title text not null,
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
                created_at text not null
            );
            """
        )


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def create_task(mode: str, prompt: str, params: dict[str, Any]) -> int:
    stamp = now_iso()
    with connect() as conn:
        cursor = conn.execute(
            """
            insert into tasks (mode, prompt, status, params_json, created_at, updated_at)
            values (?, ?, ?, ?, ?, ?)
            """,
            (mode, prompt, "running", json_dumps(params), stamp, stamp),
        )
        return int(cursor.lastrowid)


def finish_task(task_id: int, response: Any) -> None:
    with connect() as conn:
        conn.execute(
            """
            update tasks
            set status = ?, response_json = ?, updated_at = ?
            where id = ?
            """,
            ("done", json_dumps(response), now_iso(), task_id),
        )


def fail_task(task_id: int, error: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            update tasks
            set status = ?, error = ?, updated_at = ?
            where id = ?
            """,
            ("failed", error, now_iso(), task_id),
        )


def add_image(
    *,
    source: str,
    file_path: Path,
    public_url: str,
    mime_type: str,
    task_id: int | None = None,
    conversation_id: int | None = None,
    message_id: int | None = None,
) -> int:
    with connect() as conn:
        cursor = conn.execute(
            """
            insert into images
                (task_id, conversation_id, message_id, source, file_path, public_url, mime_type, created_at)
            values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                conversation_id,
                message_id,
                source,
                str(file_path),
                public_url,
                mime_type,
                now_iso(),
            ),
        )
        return int(cursor.lastrowid)

