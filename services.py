"""Memory, schedule, atmosphere, and availability services."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field

from .models import new_id, now_iso
from .storage import Database


class MemoryService:
    def __init__(self, db: Database):
        self.db = db

    async def add(self, story_id: str, content: str, scope: str = "long_term_facts",
                  participant_id: str | None = None, importance: float = 0.5) -> str:
        fact_id = new_id()
        now = now_iso()
        await self.db.execute(
            "INSERT INTO facts(id,story_id,participant_id,scope,content,importance,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (fact_id, story_id, participant_id, scope, content, max(0, min(1, importance)), now, now),
        )
        return fact_id

    async def list(self, story_id: str, participant_id: str | None = None):
        return await self.db.all(
            "SELECT id,scope,content,importance,status FROM facts WHERE story_id=? AND status='active' "
            "AND (participant_id IS NULL OR participant_id=?) ORDER BY importance DESC,created_at DESC LIMIT 100",
            (story_id, participant_id),
        )

    async def change_status(self, story_id: str, fact_id: str, status: str) -> bool:
        if status not in {"active", "completed", "superseded"}:
            raise ValueError("Invalid fact status")
        row = await self.db.one("SELECT id FROM facts WHERE story_id=? AND id=?", (story_id, fact_id))
        if not row:
            return False
        await self.db.execute(
            "UPDATE facts SET status=?,updated_at=?,completed_at=? WHERE id=? AND story_id=?",
            (status, now_iso(), now_iso() if status == "completed" else None, fact_id, story_id),
        )
        return True


class ScheduleService:
    def __init__(self, db: Database):
        self.db = db

    async def add(self, story_id: str, kind: str, content: str, start_at: str, end_at: str) -> str:
        if kind not in {"stable", "contextual", "granular"}:
            raise ValueError("Invalid schedule kind")
        if datetime.fromisoformat(start_at) >= datetime.fromisoformat(end_at):
            raise ValueError("Schedule end must be after start")
        schedule_id = new_id()
        async with self.db.commit_lock:
            db = self.db._db
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    "INSERT INTO schedules(id,story_id,kind,content,start_at,end_at) VALUES(?,?,?,?,?,?)",
                    (schedule_id, story_id, kind, content, start_at, end_at),
                )
                for boundary, due_at in (("start", start_at), ("end", end_at)):
                    await db.execute(
                        "INSERT OR IGNORE INTO runtime_jobs(id,story_id,kind,due_at,payload_json) VALUES(?,?,?,?,?)",
                        (f"schedule:{schedule_id}:{boundary}", story_id, "schedule_boundary", due_at,
                         json.dumps({"schedule_id": schedule_id, "boundary": boundary, "content": content}, ensure_ascii=False)),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return schedule_id

    async def upcoming(self, story_id: str, hours: int = 12):
        until = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
        return await self.db.all(
            "SELECT * FROM schedules WHERE story_id=? AND status='active' AND end_at>=? AND start_at<=? ORDER BY start_at",
            (story_id, now_iso(), until),
        )


class PlannedBlock(BaseModel):
    kind: str
    content: str = Field(min_length=1, max_length=500)
    start_at: datetime
    end_at: datetime


class SchedulePlan(BaseModel):
    blocks: list[PlannedBlock] = Field(max_length=60)


class SchedulePlanner:
    """Refresh a persisted rolling plan, separate from each narrative turn."""

    def __init__(self, db: Database, config: dict,
                 generate: Callable[[str, str, str, str | None], Awaitable[str]]):
        self.db, self.config, self.generate = db, config, generate

    async def refresh(self, story_id: str) -> int:
        story = await self.db.one("SELECT character_id,world_json FROM stories WHERE id=?", (story_id,))
        if not story:
            raise ValueError("Unknown story")
        character = await self.db.one("SELECT data_json FROM characters WHERE id=?", (story["character_id"],))
        participant = await self.db.one("SELECT umo FROM participants WHERE story_id=? ORDER BY last_interaction_at DESC LIMIT 1", (story_id,))
        if not participant:
            return 0
        recent = await self.db.recent_events(story_id, 20)
        prompt = json.dumps({
            "now": now_iso(), "horizon_hours": 48,
            "character": json.loads(character["data_json"]),
            "world": json.loads(story["world_json"]),
            "recent_story": [{"type": r["event_type"], "content": r["content"]} for r in recent],
            "schema": SchedulePlan.model_json_schema(),
        }, ensure_ascii=False)
        raw = await self.generate(str(self.config.get("schedule_provider", "")),
                                  "Generate a plausible 48-hour plan as JSON. Blocks are stable, contextual or granular. Existing story events take precedence. Return only JSON.",
                                  prompt, participant["umo"])
        plan = SchedulePlan.model_validate_json(raw)
        now = datetime.now(timezone.utc)
        blocks = [block for block in plan.blocks
                  if block.kind in {"stable", "contextual", "granular"}
                  and block.start_at.tzinfo and block.end_at.tzinfo
                  and block.start_at < block.end_at
                  and block.end_at > now
                  and block.start_at < now + timedelta(hours=49)]
        async with self.db.commit_lock:
            db = self.db._db
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute("UPDATE schedules SET status='superseded' WHERE story_id=? AND kind IN ('contextual','granular') AND status='active' AND start_at>=?", (story_id, now.isoformat()))
                for block in blocks:
                    start_at = block.start_at.astimezone(timezone.utc).isoformat()
                    end_at = block.end_at.astimezone(timezone.utc).isoformat()
                    if block.kind == "stable":
                        async with db.execute(
                            "SELECT id FROM schedules WHERE story_id=? AND kind='stable' AND content=? AND start_at=? AND end_at=? AND status='active'",
                            (story_id, block.content, start_at, end_at),
                        ) as cursor:
                            if await cursor.fetchone():
                                continue
                    schedule_id = new_id()
                    await db.execute(
                        "INSERT INTO schedules(id,story_id,kind,content,start_at,end_at) VALUES(?,?,?,?,?,?)",
                        (schedule_id, story_id, block.kind, block.content, start_at, end_at),
                    )
                    for boundary, due_at in (("start", start_at), ("end", end_at)):
                        await db.execute(
                            "INSERT OR IGNORE INTO runtime_jobs(id,story_id,kind,due_at,payload_json) VALUES(?,?,?,?,?)",
                            (f"schedule:{schedule_id}:{boundary}", story_id, "schedule_boundary", due_at,
                             json.dumps({"schedule_id": schedule_id, "boundary": boundary,
                                         "content": block.content}, ensure_ascii=False)),
                        )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return len(blocks)


class AlterService:
    def __init__(self, db: Database, config: dict):
        self.db, self.config = db, config

    async def apply(self, story_id: str, alter: int) -> None:
        if not self.config.get("alter_enabled", False):
            return
        row = await self.db.one("SELECT * FROM alter_states WHERE story_id=?", (story_id,))
        previous = float(row["accumulated_alter"]) if row else 0.0
        if row:
            elapsed = max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(row["last_updated"])).total_seconds())
            density = max(0.1, float(self.config.get("density_factor", 1.0)))
            previous *= 0.5 ** (elapsed * density / 21600)
        boost = float(self.config.get("same_direction_boost", 1.15)) if previous * alter > 0 else float(self.config.get("opposite_decay", 0.8))
        max_intensity = max(1.0, float(self.config.get("max_intensity", 20)))
        value = max(-max_intensity, min(max_intensity, previous + alter * boost))
        await self.db.execute(
            "INSERT INTO alter_states(story_id,accumulated_alter,direction,weight,intensity,last_updated) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(story_id) DO UPDATE SET accumulated_alter=excluded.accumulated_alter,direction=excluded.direction,weight=excluded.weight,intensity=excluded.intensity,last_updated=excluded.last_updated",
            (story_id, value, (value > 0) - (value < 0),
             max(float(self.config.get("min_weight", 0.0)), min(1.0, abs(value)/10)),
             min(1.0, abs(value)/max_intensity), now_iso()),
        )

    async def summarize(self, story_id: str,
                        generate: Callable[[str, str, str, str | None], Awaitable[str]]) -> None:
        if not self.config.get("alter_enabled", False):
            return
        row = await self.db.one("SELECT accumulated_alter,summary FROM alter_states WHERE story_id=?", (story_id,))
        if not row or abs(row["accumulated_alter"]) < float(self.config.get("alter_summary_threshold", 6)):
            return
        key = f"alter_summary_at:{story_id}"
        last = await self.db.one("SELECT value FROM metadata WHERE key=?", (key,))
        if last and datetime.fromisoformat(last["value"]) > datetime.now(timezone.utc)-timedelta(hours=1):
            return
        participant = await self.db.one("SELECT umo FROM participants WHERE story_id=? ORDER BY last_interaction_at DESC LIMIT 1", (story_id,))
        prompt = json.dumps({"alter": row["accumulated_alter"], "previous": row["summary"]}, ensure_ascii=False)
        summary = await generate(str(self.config.get("alter_provider", "")),
                                 "简述角色当前临时氛围。正数更谨慎严肃，负数更轻松活跃。不要改写永久人设，只输出一句话。",
                                 prompt, participant["umo"] if participant else None)
        if summary.strip():
            await self.db.execute("UPDATE alter_states SET summary=? WHERE story_id=?", (summary.strip()[:300], story_id))
            await self.db.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)", (key, now_iso()))


class AgencyService:
    def __init__(self, db: Database):
        self.db = db

    async def evaluate(self, story_id: str) -> dict:
        now = now_iso()
        story = await self.db.one("SELECT state_json FROM stories WHERE id=?", (story_id,))
        state = json.loads(story["state_json"]) if story else {}
        rows = await self.db.all("SELECT content FROM schedules WHERE story_id=? AND status='active' AND start_at<=? AND end_at>=?", (story_id, now, now))
        activity = " / ".join([str(state.get("activity", "")), *(row["content"] for row in rows)])
        busy = any(term in activity.lower() for term in (
            "sleep", "class", "driving", "work", "shower", "bath",
            "睡", "上课", "开车", "工作", "洗澡", "洗浴",
        ))
        has_device = bool(state.get("device_available", True))
        private = str(state.get("privacy", "")).lower() not in {"none", "public", "crowded", "无", "不方便"}
        available = not busy and has_device
        value = {"available": available, "can_see_message": available,
                 "can_reply": available and private, "proactive_allowed": available and private,
                 "reason": activity if busy else ("device unavailable" if not has_device else "available")}
        await self.db.execute("INSERT OR REPLACE INTO agency_states VALUES(?,?,?)", (story_id, json.dumps(value, ensure_ascii=False), now))
        return value
