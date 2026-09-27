"""Story backup round trips, validation and atomic failure handling."""

import asyncio
import copy
from pathlib import Path

import pytest

from astrbot_plugin_interlude.backup import _fingerprint, export_story, restore_story, validate_backup
from astrbot_plugin_interlude.models import EventType, StoryEvent, now_iso
from astrbot_plugin_interlude.storage import Database


def run(coro):
    return asyncio.run(coro)


async def populated(tmp_path: Path):
    db = Database(tmp_path / "backup.db")
    await db.open()
    await db.ensure_story("one", "character", {"id": "character", "name": "Old"}, {"world_description": "old"})
    pid = await db.ensure_participant("one", "test", "u1", "test:private:u1", "User")
    await db.append_event(StoryEvent("one", EventType.USER_MESSAGE, "hello", pid))
    await db.execute("INSERT INTO schedules(id,story_id,kind,content,start_at,end_at) VALUES(?,?,?,?,?,?)",
                     ("schedule", "one", "granular", "class", now_iso(), now_iso()))
    await db.execute("INSERT INTO intents(id,story_id,participant_id,type,content,due_at,created_at) VALUES(?,?,?,?,?,?,?)",
                     ("intent", "one", pid, "reminder", "do this", now_iso(), now_iso()))
    await db.execute("INSERT INTO runtime_jobs(id,story_id,kind,due_at) VALUES(?,?,?,?)",
                     ("job", "one", "retry_event", now_iso()))
    await db.execute("INSERT INTO deliveries(id,story_id,participant_id,umo,content,status,generation_id,kind,created_at) "
                     "VALUES(?,?,?,?,?,?,?,?,?)",
                     ("delivery", "one", pid, "test:private:u1", "pending text", "pending", None, "reply", now_iso()))
    return db, pid


def test_round_trip_replaces_only_selected_story_and_cancels_pending(tmp_path):
    async def scenario():
        db, pid = await populated(tmp_path)
        await db.ensure_story("two", "other", {"id": "other", "name": "Other"}, {})
        backup = await export_story(db, "one")
        assert (await validate_backup(db, backup))["counts"]["story_entries"] == 1
        await db.clear_story("one", purge=True)
        summary = await restore_story(db, backup, {"id": "character", "name": "Current"},
                                      {"world_description": "current"})
        assert summary["story_id"] == "one"
        assert (await db.one("SELECT content FROM story_entries WHERE story_id='one'"))["content"] == "hello"
        assert (await db.one("SELECT id FROM participants WHERE id=?", (pid,))) is not None
        assert (await db.one("SELECT status FROM intents WHERE id='intent'"))["status"] == "cancelled"
        assert (await db.one("SELECT status FROM runtime_jobs WHERE id='job'"))["status"] == "cancelled"
        assert (await db.one("SELECT status FROM deliveries WHERE id='delivery'"))["status"] == "uncertain"
        assert (await db.one("SELECT id FROM stories WHERE id='two'")) is not None
        assert (await db.one("SELECT data_json FROM characters WHERE id='character'"))["data_json"].find('Current') > 0
        assert (await db.one("SELECT world_json FROM stories WHERE id='one'"))["world_json"].find('current') > 0
        await db.close()
    run(scenario())


def test_restore_overwrites_existing_story_without_touching_another(tmp_path):
    async def scenario():
        db, _ = await populated(tmp_path)
        await db.ensure_story("two", "other", {"id": "other", "name": "Other"}, {})
        backup = await export_story(db, "one")
        await db.append_event(StoryEvent("one", EventType.USER_MESSAGE, "after backup"))
        await db.append_event(StoryEvent("two", EventType.USER_MESSAGE, "other story"))
        await restore_story(db, backup, {"id": "character", "name": "Current"}, {})
        assert [row["content"] for row in await db.all(
            "SELECT content FROM story_entries WHERE story_id='one' ORDER BY occurred_at"
        )] == ["hello"]
        assert (await db.one("SELECT content FROM story_entries WHERE story_id='two'"))["content"] == "other story"
        await db.close()
    run(scenario())


def test_backup_rejects_corruption_and_cross_story_rows(tmp_path):
    async def scenario():
        db, _ = await populated(tmp_path)
        backup = await export_story(db, "one")
        tampered = copy.deepcopy(backup)
        tampered["tables"]["story_entries"][0]["content"] = "changed"
        with pytest.raises(ValueError, match="checksum"):
            await validate_backup(db, tampered)
        tampered["sha256"] = _fingerprint({k: v for k, v in tampered.items() if k != "sha256"})
        tampered["tables"]["story_entries"][0]["story_id"] = "two"
        tampered["sha256"] = _fingerprint({k: v for k, v in tampered.items() if k != "sha256"})
        with pytest.raises(ValueError, match="another story"):
            await validate_backup(db, tampered)
        await db.close()
    run(scenario())


def test_restore_failure_rolls_back_existing_story(tmp_path):
    async def scenario():
        db, _ = await populated(tmp_path)
        backup = await export_story(db, "one")
        await db.ensure_story("two", "other", {"id": "other", "name": "Other"}, {})
        await db.append_event(StoryEvent("two", EventType.USER_MESSAGE, "other story"))
        collision = await db.one("SELECT id FROM story_entries WHERE story_id='two'")
        broken = copy.deepcopy(backup)
        broken["tables"]["story_entries"][0]["id"] = collision["id"]
        broken["sha256"] = _fingerprint({k: v for k, v in broken.items() if k != "sha256"})
        with pytest.raises(Exception):
            await restore_story(db, broken, {"id": "character", "name": "New"}, {})
        assert (await db.one("SELECT content FROM story_entries WHERE story_id='one'"))["content"] == "hello"
        assert (await db.one("SELECT content FROM story_entries WHERE story_id='two'"))["content"] == "other story"
        await db.close()
    run(scenario())
