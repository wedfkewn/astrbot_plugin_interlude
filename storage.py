"""SQLite canonical state, migrations, and atomic narrative commits."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from .models import NarrativeResult, StoryEvent, new_id, now_iso

MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS stories (
      id TEXT PRIMARY KEY, character_id TEXT NOT NULL, world_json TEXT NOT NULL DEFAULT '{}',
      cursor TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
      generation_id TEXT NOT NULL DEFAULT '', paused INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS characters (id TEXT PRIMARY KEY, data_json TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS participants (
      id TEXT PRIMARY KEY, story_id TEXT NOT NULL, platform TEXT NOT NULL,
      platform_user_id TEXT NOT NULL, umo TEXT NOT NULL, display_name TEXT NOT NULL DEFAULT '',
      data_json TEXT NOT NULL DEFAULT '{}', enabled INTEGER NOT NULL DEFAULT 1,
      last_interaction_at TEXT, UNIQUE(story_id, umo, platform_user_id));
    CREATE TABLE IF NOT EXISTS relationships (
      story_id TEXT NOT NULL, participant_id TEXT NOT NULL, data_json TEXT NOT NULL,
      summary TEXT NOT NULL DEFAULT '', PRIMARY KEY(story_id, participant_id));
    CREATE TABLE IF NOT EXISTS story_entries (
      id TEXT PRIMARY KEY, story_id TEXT NOT NULL, participant_id TEXT,
      event_type TEXT NOT NULL, content TEXT NOT NULL, occurred_at TEXT NOT NULL,
      metadata_json TEXT NOT NULL DEFAULT '{}');
    CREATE INDEX IF NOT EXISTS idx_entries_story_time ON story_entries(story_id, occurred_at);
    CREATE TABLE IF NOT EXISTS scenes (
      story_id TEXT PRIMARY KEY, summary TEXT NOT NULL DEFAULT '', started_at TEXT NOT NULL,
      last_activity_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS facts (
      id TEXT PRIMARY KEY, story_id TEXT NOT NULL, participant_id TEXT, scope TEXT NOT NULL,
      content TEXT NOT NULL, importance REAL NOT NULL, status TEXT NOT NULL DEFAULT 'active',
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL, completed_at TEXT,
      source_event_id TEXT, embedding TEXT);
    CREATE TABLE IF NOT EXISTS intents (
      id TEXT PRIMARY KEY, story_id TEXT NOT NULL, participant_id TEXT, type TEXT NOT NULL,
      content TEXT NOT NULL, due_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
      willingness REAL NOT NULL DEFAULT 0.5, reason TEXT NOT NULL DEFAULT '',
      created_at TEXT NOT NULL, generation_id TEXT);
    CREATE INDEX IF NOT EXISTS idx_intents_due ON intents(status, due_at);
    CREATE TABLE IF NOT EXISTS schedules (
      id TEXT PRIMARY KEY, story_id TEXT NOT NULL, kind TEXT NOT NULL, content TEXT NOT NULL,
      start_at TEXT NOT NULL, end_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active');
    CREATE TABLE IF NOT EXISTS overlays (
      id TEXT PRIMARY KEY, story_id TEXT NOT NULL, participant_id TEXT, scope TEXT NOT NULL,
      content TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS alter_states (
      story_id TEXT PRIMARY KEY, accumulated_alter REAL NOT NULL DEFAULT 0,
      direction INTEGER NOT NULL DEFAULT 0, weight REAL NOT NULL DEFAULT 0,
      intensity REAL NOT NULL DEFAULT 0, summary TEXT NOT NULL DEFAULT '',
      last_updated TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS agency_states (
      story_id TEXT PRIMARY KEY, data_json TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS deliveries (
      id TEXT PRIMARY KEY, story_id TEXT NOT NULL, participant_id TEXT, umo TEXT NOT NULL,
      content TEXT NOT NULL, status TEXT NOT NULL, generation_id TEXT,
      kind TEXT NOT NULL, created_at TEXT NOT NULL, sent_at TEXT, error TEXT);
    CREATE TABLE IF NOT EXISTS runtime_jobs (
      id TEXT PRIMARY KEY, story_id TEXT NOT NULL, kind TEXT NOT NULL, due_at TEXT NOT NULL,
      payload_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'pending');
    """,
    """
    ALTER TABLE deliveries ADD COLUMN attempts INTEGER NOT NULL DEFAULT 1;
    CREATE INDEX IF NOT EXISTS idx_deliveries_story_sent ON deliveries(story_id, status, sent_at);
    CREATE INDEX IF NOT EXISTS idx_facts_story_scope ON facts(story_id, status, scope);
    CREATE INDEX IF NOT EXISTS idx_schedules_window ON schedules(story_id, status, start_at, end_at);
    """,
    """
    ALTER TABLE stories ADD COLUMN state_json TEXT NOT NULL DEFAULT '{}';
    CREATE TABLE IF NOT EXISTS perspectives (
      id TEXT PRIMARY KEY, story_id TEXT NOT NULL, participant_id TEXT,
      content TEXT NOT NULL, created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active');
    CREATE INDEX IF NOT EXISTS idx_perspectives_story ON perspectives(story_id,status,participant_id);
    """,
    """
    ALTER TABLE deliveries ADD COLUMN intent_id TEXT;
    ALTER TABLE intents ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
    CREATE INDEX IF NOT EXISTS idx_deliveries_intent ON deliveries(intent_id,status);
    """,
]


class Database:
    """Single async connection; per-story locks live in the router."""

    def __init__(self, path: Path):
        self.path = path
        self.conn: aiosqlite.Connection | None = None
        self.commit_lock = asyncio.Lock()

    async def open(self) -> None:
        await asyncio.to_thread(self.path.parent.mkdir, parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA foreign_keys=ON")
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.migrate()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()
            self.conn = None

    async def migrate(self) -> None:
        db = self._db
        await db.execute("CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        row = await self.one("SELECT value FROM metadata WHERE key='schema_version'")
        version = int(row["value"]) if row else 0
        for index, script in enumerate(MIGRATIONS[version:], start=version + 1):
            # Apply the schema and its version marker together. A crash cannot
            # leave an ALTER applied while the marker still points to the old version.
            try:
                await db.executescript(
                    "BEGIN IMMEDIATE;\n" + script +
                    f"\nINSERT OR REPLACE INTO metadata VALUES ('schema_version', '{index}');\nCOMMIT;"
                )
            except Exception:
                await db.rollback()
                raise

    @property
    def _db(self) -> aiosqlite.Connection:
        if self.conn is None:
            raise RuntimeError("Database is not open")
        return self.conn

    async def one(self, sql: str, args: tuple = ()) -> aiosqlite.Row | None:
        async with self._db.execute(sql, args) as cursor:
            return await cursor.fetchone()

    async def all(self, sql: str, args: tuple = ()) -> list[aiosqlite.Row]:
        async with self._db.execute(sql, args) as cursor:
            return await cursor.fetchall()

    async def execute(self, sql: str, args: tuple = ()) -> None:
        async with self.commit_lock:
            await self._db.execute(sql, args)
            await self._db.commit()

    async def ensure_story(self, story_id: str, character_id: str, character: dict, world: dict) -> None:
        async with self.commit_lock:
            await self._db.execute(
                "INSERT INTO characters(id,data_json) VALUES(?,?) "
                "ON CONFLICT(id) DO UPDATE SET data_json=excluded.data_json",
                (character_id, json.dumps(character, ensure_ascii=False)),
            )
            await self._db.execute(
                "INSERT INTO stories(id, character_id, world_json, cursor) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET character_id=excluded.character_id,world_json=excluded.world_json",
                (story_id, character_id, json.dumps(world, ensure_ascii=False), now_iso()),
            )
            await self._db.commit()

    async def ensure_participant(self, story_id: str, platform: str, user_id: str, umo: str, name: str) -> str:
        row = await self.one(
            "SELECT id FROM participants WHERE story_id=? AND umo=? AND platform_user_id=?",
            (story_id, umo, user_id),
        )
        if row:
            return row["id"]
        participant_id = new_id()
        await self.execute(
            "INSERT OR IGNORE INTO participants(id,story_id,platform,platform_user_id,umo,display_name) VALUES(?,?,?,?,?,?)",
            (participant_id, story_id, platform, user_id, umo, name),
        )
        row = await self.one(
            "SELECT id FROM participants WHERE story_id=? AND umo=? AND platform_user_id=?",
            (story_id, umo, user_id),
        )
        return row["id"]

    async def append_event(self, event: StoryEvent) -> None:
        await self.execute(
            "INSERT OR IGNORE INTO story_entries VALUES(?,?,?,?,?,?,?)",
            (event.id, event.story_id, event.participant_id, event.event_type.value,
             event.content, event.created_at, json.dumps(event.metadata, ensure_ascii=False)),
        )

    async def ingest_event(self, event: StoryEvent, generation_id: str | None, *, shared: bool = False) -> None:
        """Persist input; shared stories retain other participants' pending turns."""
        async with self.commit_lock:
            db = self._db
            await db.execute("BEGIN IMMEDIATE")
            try:
                if shared:
                    row = await self.one("SELECT generation_id FROM stories WHERE id=?", (event.story_id,))
                    if not row:
                        raise ValueError("story not found")
                    generation_id = row["generation_id"] or new_id()
                event.metadata["generation_id"] = generation_id
                await db.execute(
                    "INSERT OR IGNORE INTO story_entries VALUES(?,?,?,?,?,?,?)",
                    (event.id, event.story_id, event.participant_id, event.event_type.value,
                     event.content, event.created_at, json.dumps(event.metadata, ensure_ascii=False)),
                )
                if shared:
                    if row["generation_id"]:
                        await db.execute("UPDATE stories SET revision=revision+1 WHERE id=?", (event.story_id,))
                    else:
                        await db.execute("UPDATE stories SET revision=revision+1,generation_id=? WHERE id=?",
                                         (generation_id, event.story_id))
                else:
                    await db.execute(
                        "UPDATE stories SET revision=revision+1,generation_id=? WHERE id=?",
                        (generation_id, event.story_id),
                    )
                if event.participant_id:
                    await db.execute("UPDATE participants SET last_interaction_at=? WHERE id=?",
                                     (event.created_at, event.participant_id))
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def bump_generation(self, story_id: str, generation_id: str) -> int:
        await self.execute(
            "UPDATE stories SET revision=revision+1, generation_id=? WHERE id=?",
            (generation_id, story_id),
        )
        row = await self.one("SELECT revision FROM stories WHERE id=?", (story_id,))
        return int(row["revision"])

    async def generation_valid(self, story_id: str, generation_id: str) -> bool:
        row = await self.one("SELECT generation_id FROM stories WHERE id=?", (story_id,))
        return bool(row and row["generation_id"] == generation_id)

    async def commit_narrative(self, story_id: str, event: StoryEvent, result: NarrativeResult,
                               generation_id: str, cursor: str) -> None:
        """Commit only actual life events. Planned messages remain intents/deliveries."""
        async with self.commit_lock:
            await self._commit_narrative(story_id, event, result, generation_id, cursor)

    async def _commit_narrative(self, story_id: str, event: StoryEvent, result: NarrativeResult,
                                generation_id: str, cursor: str) -> None:
        db = self._db
        await db.execute("BEGIN IMMEDIATE")
        try:
            row = await self.one("SELECT generation_id FROM stories WHERE id=?", (story_id,))
            if not row or row["generation_id"] != generation_id:
                raise RuntimeError("stale generation")
            previous_cursor = await self.one("SELECT cursor FROM stories WHERE id=?", (story_id,))
            earliest = datetime.fromisoformat(previous_cursor["cursor"])
            latest = datetime.fromisoformat(cursor)
            for beat in [*result.story, *result.aftermath]:
                occurred = latest
                if beat.occurred_at:
                    try:
                        candidate = datetime.fromisoformat(beat.occurred_at)
                        if candidate.tzinfo is not None:
                            occurred = max(earliest, min(latest, candidate.astimezone(timezone.utc)))
                    except ValueError:
                        pass
                await db.execute(
                    "INSERT INTO story_entries VALUES(?,?,?,?,?,?,?)",
                    (new_id(), story_id, None, "LIFE_EVENT", beat.content,
                     occurred.isoformat(), "{}"),
                )
            for fact in result.facts:
                if fact.participant_id and fact.participant_id != event.participant_id:
                    continue
                await db.execute(
                    "INSERT INTO facts(id,story_id,participant_id,scope,content,importance,created_at,updated_at,source_event_id) VALUES(?,?,?,?,?,?,?,?,?)",
                    (new_id(), story_id, fact.participant_id or event.participant_id,
                     fact.scope, fact.content, fact.importance, cursor, cursor, event.id),
                )
            for intent in result.intents:
                if intent.participant_id:
                    target = await self.one(
                        "SELECT id FROM participants WHERE id=? AND story_id=? AND enabled=1",
                        (intent.participant_id, story_id),
                    )
                    if not target:
                        continue
                await db.execute(
                    "INSERT INTO intents(id,story_id,participant_id,type,content,due_at,willingness,reason,created_at,generation_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (new_id(), story_id, intent.participant_id or event.participant_id,
                     intent.type, intent.content, intent.due_at, intent.willingness,
                     intent.reason, cursor, generation_id if intent.type == "delayed_reply" else None),
                )
            for overlay in result.overlay_candidates:
                if overlay.participant_id:
                    target = await self.one(
                        "SELECT id FROM participants WHERE id=? AND story_id=? AND enabled=1",
                        (overlay.participant_id, story_id),
                    )
                    if not target:
                        continue
                await db.execute(
                    "INSERT INTO overlays VALUES(?,?,?,?,?,?,?)",
                    (new_id(), story_id, overlay.participant_id, overlay.scope,
                     overlay.content, "active", cursor),
                )
                if overlay.scope == "perspective":
                    await db.execute(
                        "INSERT INTO perspectives VALUES(?,?,?,?,?,'active')",
                        (new_id(), story_id, overlay.participant_id, overlay.content, cursor),
                    )
            for change in result.relationship_changes:
                if change.participant_id != event.participant_id:
                    continue
                existing = await self.one(
                    "SELECT data_json FROM relationships WHERE story_id=? AND participant_id=?",
                    (story_id, change.participant_id),
                )
                values = json.loads(existing["data_json"]) if existing else {}
                current = float(values.get(change.dimension, 0.5))
                values[change.dimension] = max(0.0, min(1.0, current + max(-0.05, min(0.05, change.delta))))
                await db.execute(
                    "INSERT OR REPLACE INTO relationships VALUES(?,?,?,?)",
                    (story_id, change.participant_id, json.dumps(values), change.summary),
                )
            existing_state = await self.one("SELECT state_json FROM stories WHERE id=?", (story_id,))
            state = json.loads(existing_state["state_json"])
            patch = result.state_update.model_dump(exclude_unset=True) if result.state_update else {}
            if "emotion" in patch and patch["emotion"] != state.get("emotion"):
                patch.setdefault("emotion_intensity", 0)
                patch.setdefault("emotion_reason", "")
            state.update(patch)
            await db.execute("UPDATE stories SET cursor=?,state_json=? WHERE id=?",
                             (cursor, json.dumps(state, ensure_ascii=False), story_id))
            await db.execute(
                "INSERT INTO scenes(story_id,summary,started_at,last_activity_at) VALUES(?,?,?,?) "
                "ON CONFLICT(story_id) DO UPDATE SET last_activity_at=excluded.last_activity_at,"
                "summary=CASE WHEN excluded.summary<>'' THEN excluded.summary ELSE scenes.summary END",
                (story_id, result.state_update.scene_summary if result.state_update else "", event.created_at, cursor),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    async def due_intents(self, at: str) -> list[aiosqlite.Row]:
        return await self.all("SELECT * FROM intents WHERE status='pending' AND due_at<=? ORDER BY due_at LIMIT 50", (at,))

    async def recent_events(self, story_id: str, limit: int = 50) -> list[aiosqlite.Row]:
        rows = await self.all("SELECT * FROM story_entries WHERE story_id=? ORDER BY occurred_at DESC LIMIT ?", (story_id, limit))
        return list(reversed(rows))

    async def mark_intent(self, intent_id: str, status: str) -> None:
        await self.execute("UPDATE intents SET status=? WHERE id=?", (status, intent_id))

    async def record_delivery(self, story_id: str, participant_id: str | None, umo: str,
                              content: str, generation_id: str | None, kind: str,
                              intent_id: str | None = None) -> str:
        delivery_id = new_id()
        await self.execute(
            "INSERT INTO deliveries(id,story_id,participant_id,umo,content,status,generation_id,kind,created_at,intent_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (delivery_id, story_id, participant_id, umo, content, "pending", generation_id, kind, now_iso(), intent_id),
        )
        return delivery_id

    async def finish_delivery(self, delivery_id: str, success: bool, error: str = "") -> None:
        await self.execute(
            "UPDATE deliveries SET status=?,sent_at=?,error=? WHERE id=?",
            ("sent" if success else "delivery_failed", now_iso() if success else None, error[:300], delivery_id),
        )

    async def complete_delivery(self, delivery_id: str, story_id: str,
                                participant_id: str | None, content: str) -> None:
        """Record a confirmed send and canonical speech in one transaction."""
        async with self.commit_lock:
            db = self._db
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    "UPDATE deliveries SET status='sent',sent_at=?,error=NULL WHERE id=? AND status='pending'",
                    (now_iso(), delivery_id),
                )
                await db.execute(
                    "INSERT INTO story_entries VALUES(?,?,?,?,?,?,?)",
                    (new_id(), story_id, participant_id, "CHARACTER_MESSAGE_SENT",
                     content, now_iso(), json.dumps({"delivery_id": delivery_id})),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def mark_uncertain_deliveries(self) -> int:
        """A crash may leave a send in flight; never assume it succeeded or resend it."""
        async with self.commit_lock:
            cursor = await self._db.execute(
                "UPDATE deliveries SET status='uncertain',error='interrupted during send' WHERE status='pending'"
            )
            await self._db.commit()
            return cursor.rowcount

    async def recover_processing(self) -> None:
        """Reconcile jobs interrupted by shutdown without duplicating uncertain sends."""
        async with self.commit_lock:
            db = self._db
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute("SELECT id,content FROM intents WHERE status='processing'") as cursor:
                    intents = await cursor.fetchall()
                for intent in intents:
                    async with db.execute(
                        "SELECT status FROM deliveries WHERE intent_id=?", (intent["id"],)
                    ) as cursor:
                        deliveries = await cursor.fetchall()
                    statuses = {item["status"] for item in deliveries}
                    # A partly sent segmented message must resume from its first
                    # unsent segment. An in-flight send has unknown outcome and
                    # cannot safely be retried automatically.
                    expected = len([part for part in intent["content"].split("<sep/>") if part.strip()])
                    sent = sum(item["status"] == "sent" for item in deliveries)
                    status = ("cancelled" if "uncertain" in statuses else
                              "completed" if expected and sent == expected else "pending")
                    await db.execute("UPDATE intents SET status=? WHERE id=?", (status, intent["id"]))
                await db.execute("UPDATE runtime_jobs SET status='pending' WHERE status='processing'")
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def latest_sent_at(self, participant_id: str) -> str | None:
        row = await self.one("SELECT MAX(sent_at) AS value FROM deliveries WHERE participant_id=? AND status='sent'", (participant_id,))
        return row["value"] if row else None

    async def sent_count(self, participant_id: str, since: str) -> int:
        row = await self.one("SELECT COUNT(*) AS value FROM deliveries WHERE participant_id=? AND kind='proactive' AND status='sent' AND sent_at>=?", (participant_id, since))
        return int(row["value"]) if row else 0

    async def story_sent_count(self, story_id: str, since: str) -> int:
        row = await self.one(
            "SELECT COUNT(*) AS value FROM deliveries WHERE story_id=? AND kind='proactive' AND status='sent' AND sent_at>=?",
            (story_id, since),
        )
        return int(row["value"]) if row else 0

    async def clear_story(self, story_id: str, *, purge: bool) -> None:
        """Administrator reset/purge is atomic and invalidates queued generations."""
        tables = ("story_entries", "facts", "intents", "schedules", "overlays",
                  "perspectives", "deliveries", "runtime_jobs", "relationships",
                  "scenes", "alter_states", "agency_states")
        async with self.commit_lock:
            db = self._db
            await db.execute("BEGIN IMMEDIATE")
            try:
                for table in tables:
                    await db.execute(f"DELETE FROM {table} WHERE story_id=?", (story_id,))
                if purge:
                    await db.execute("DELETE FROM participants WHERE story_id=?", (story_id,))
                    await db.execute("DELETE FROM stories WHERE id=?", (story_id,))
                else:
                    await db.execute(
                        "UPDATE stories SET cursor=?,revision=revision+1,generation_id=?,state_json='{}',paused=0 WHERE id=?",
                        (now_iso(), new_id(), story_id),
                    )
                    await db.execute(
                        "UPDATE participants SET last_interaction_at=NULL WHERE story_id=?",
                        (story_id,),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
