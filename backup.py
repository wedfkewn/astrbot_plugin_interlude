"""Validated per-story JSON backups and transactional restoration."""

from __future__ import annotations

import hashlib
import json
import asyncio
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

from .models import new_id, now_iso

BACKUP_FORMAT = "astrbot_plugin_interlude/story"
BACKUP_VERSION = 1
MAX_BACKUP_BYTES = 20 * 1024 * 1024
LOCAL_BACKUP_ID = re.compile(r"[0-9a-f]{32}\Z")
STORY_TABLES = (
    "stories", "participants", "story_entries", "scenes", "facts", "intents",
    "schedules", "overlays", "perspectives", "relationships", "alter_states",
    "agency_states", "deliveries", "runtime_jobs",
)


def _fingerprint(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _atomic_json(path: Path, value: dict) -> None:
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".backup-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _save_local_backup(directory: Path, payload: dict) -> dict:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup_id = new_id()
    metadata = {"id": backup_id, "story_id": payload["story_id"],
                "exported_at": payload["exported_at"],
                "events": len(payload["tables"]["story_entries"]), "sha256": payload["sha256"]}
    data_path = directory / f"{backup_id}.json"
    try:
        _atomic_json(data_path, payload)
        _atomic_json(directory / f"{backup_id}.meta.json", metadata)
    except Exception:
        data_path.unlink(missing_ok=True)
        raise
    return {key: metadata[key] for key in ("id", "story_id", "exported_at", "events")}


async def save_local_backup(directory: Path, payload: dict) -> dict:
    return await asyncio.to_thread(_save_local_backup, directory, payload)


def _list_local_backups(directory: Path) -> list[dict]:
    if not directory.is_dir():
        return []
    backups = []
    for path in directory.glob("*.meta.json"):
        backup_id = path.name.removesuffix(".meta.json")
        if not LOCAL_BACKUP_ID.fullmatch(backup_id):
            continue
        try:
            if path.stat().st_size > 4096 or not (directory / f"{backup_id}.json").is_file():
                continue
            metadata = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(metadata, dict) or metadata.get("id") != backup_id or not isinstance(metadata.get("story_id"), str):
                continue
            datetime.fromisoformat(metadata["exported_at"])
            backups.append({key: metadata[key] for key in ("id", "story_id", "exported_at", "events")})
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return sorted(backups, key=lambda item: (item["exported_at"], item["id"]), reverse=True)


async def list_local_backups(directory: Path) -> list[dict]:
    return await asyncio.to_thread(_list_local_backups, directory)


def _load_local_backup(directory: Path, backup_id: str) -> dict:
    if not LOCAL_BACKUP_ID.fullmatch(backup_id):
        raise ValueError("invalid backup id")
    path = directory / f"{backup_id}.json"
    metadata_path = directory / f"{backup_id}.meta.json"
    if not path.is_file() or not metadata_path.is_file():
        raise ValueError("backup not found")
    if path.stat().st_size > MAX_BACKUP_BYTES or metadata_path.stat().st_size > 4096:
        raise ValueError("backup exceeds size limit")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("backup file unreadable") from exc
    if not isinstance(metadata, dict) or not isinstance(payload, dict):
        raise ValueError("backup file invalid")
    if metadata.get("id") != backup_id or any(payload.get(key) != metadata.get(key)
                                                   for key in ("story_id", "exported_at", "sha256")):
        raise ValueError("backup metadata mismatch")
    return payload


async def load_local_backup(directory: Path, backup_id: str) -> dict:
    return await asyncio.to_thread(_load_local_backup, directory, backup_id)


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
