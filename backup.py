"""Validated per-story JSON backups and transactional restoration."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from .models import new_id, now_iso

BACKUP_FORMAT = "astrbot_plugin_interlude/story"
BACKUP_VERSION = 1
MAX_BACKUP_BYTES = 20 * 1024 * 1024
STORY_TABLES = (
    "stories", "participants", "story_entries", "scenes", "facts", "intents",
    "schedules", "overlays", "perspectives", "relationships", "alter_states",
    "agency_states", "deliveries", "runtime_jobs",
)


def _fingerprint(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


async def _columns(db, table: str) -> tuple[str, ...]:
    return tuple(row["name"] for row in await db.all(f"PRAGMA table_info({table})"))


async def export_story(db, story_id: str) -> dict:
    async with db.commit_lock:
        await db._db.execute("BEGIN IMMEDIATE")
        try:
            rows = {table: [dict(row) for row in await db.all(
                f"SELECT * FROM {table} WHERE {'id' if table == 'stories' else 'story_id'}=?", (story_id,)
            )] for table in STORY_TABLES}
            schema = await db.one("SELECT value FROM metadata WHERE key='schema_version'")
            await db._db.commit()
        except Exception:
            await db._db.rollback()
            raise
    if len(rows["stories"]) != 1:
        raise ValueError("story not found")
    payload = {"format": BACKUP_FORMAT, "version": BACKUP_VERSION, "schema_version": int(schema["value"]),
               "story_id": story_id, "exported_at": now_iso(), "tables": rows}
    payload["sha256"] = _fingerprint(payload)
    if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > MAX_BACKUP_BYTES:
        raise ValueError("backup exceeds 20 MiB")
    return payload


async def validate_backup(db, payload: dict) -> dict:
    if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > MAX_BACKUP_BYTES:
        raise ValueError("backup exceeds 20 MiB")
    if not isinstance(payload, dict) or payload.get("format") != BACKUP_FORMAT or payload.get("version") != BACKUP_VERSION:
        raise ValueError("unsupported backup format")
    schema = await db.one("SELECT value FROM metadata WHERE key='schema_version'")
    if payload.get("schema_version") != int(schema["value"]):
        raise ValueError("incompatible database schema")
    story_id = payload.get("story_id")
    if not isinstance(story_id, str) or not story_id or len(story_id) > 200:
        raise ValueError("invalid story id")
    try:
        datetime.fromisoformat(payload["exported_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid export timestamp") from exc
    digest = payload.get("sha256")
    if not isinstance(digest, str) or digest != _fingerprint({k: v for k, v in payload.items() if k != "sha256"}):
        raise ValueError("backup checksum mismatch")
    tables = payload.get("tables")
    if not isinstance(tables, dict) or set(tables) != set(STORY_TABLES):
        raise ValueError("incomplete backup tables")
    if any(not isinstance(rows, list) or len(rows) > 100_000 for rows in tables.values()):
        raise ValueError("invalid backup rows")
    if len(tables["stories"]) != 1:
        raise ValueError("backup must contain one story")
    for table, rows in tables.items():
        columns = set(await _columns(db, table))
        if not columns:
            raise ValueError("missing database table")
        identifiers = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != columns:
                raise ValueError(f"invalid {table} columns")
            if row["id" if table == "stories" else "story_id"] != story_id:
                raise ValueError("backup contains another story")
            if "id" in row:
                if not isinstance(row["id"], str) or row["id"] in identifiers:
                    raise ValueError(f"duplicate {table} id")
                identifiers.add(row["id"])
    participant_ids = {row["id"] for row in tables["participants"]}
    for table in STORY_TABLES:
        for row in tables[table]:
            if row.get("participant_id") is not None and row["participant_id"] not in participant_ids:
                raise ValueError(f"orphan participant in {table}")
    try:
        datetime.fromisoformat(tables["stories"][0]["cursor"])
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid story cursor") from exc
    return {"story_id": story_id, "exported_at": payload["exported_at"],
            "counts": {table: len(rows) for table, rows in tables.items()}}


async def restore_story(db, payload: dict, character: dict, world: dict) -> dict:
    summary = await validate_backup(db, payload)
    story_id = summary["story_id"]
    async with db.commit_lock:
        await db._db.execute("BEGIN IMMEDIATE")
        try:
            for table in reversed(STORY_TABLES):
                await db._db.execute(
                    f"DELETE FROM {table} WHERE {'id' if table == 'stories' else 'story_id'}=?", (story_id,)
                )
            await db._db.execute(
                "INSERT INTO characters(id,data_json) VALUES(?,?) "
                "ON CONFLICT(id) DO UPDATE SET data_json=excluded.data_json",
                (character["id"], json.dumps(character, ensure_ascii=False)),
            )
            for table in STORY_TABLES:
                columns = await _columns(db, table)
                placeholders = ",".join("?" for _ in columns)
                names = ",".join(columns)
                for original in payload["tables"][table]:
                    row = dict(original)
                    if table == "stories":
                        row.update(character_id=character["id"], world_json=json.dumps(world, ensure_ascii=False),
                                   generation_id=new_id())
                    elif table == "intents" and row["status"] in {"pending", "processing"}:
                        row["status"] = "cancelled"
                    elif table == "runtime_jobs" and row["status"] in {"pending", "processing"}:
                        row["status"] = "cancelled"
                    elif table == "deliveries" and row["status"] in {"pending", "processing"}:
                        row["status"] = "uncertain"
                    await db._db.execute(
                        f"INSERT INTO {table}({names}) VALUES({placeholders})",
                        tuple(row[column] for column in columns),
                    )
            await db._db.commit()
        except Exception:
            await db._db.rollback()
            raise
    return summary
