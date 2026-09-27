"""Behavioral tests for persistence, revision checks, and delivery truth."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

from astrbot_plugin_interlude.models import (
    EventType,
    NarrativeResult,
    StoryEvent,
    now_iso,
)
from astrbot_plugin_interlude.narrative import NarrativeEngine, parse_narrative
from astrbot_plugin_interlude.runtime import (
    DeliveryEngine,
    EventRouter,
    RuntimeScheduler,
)
from astrbot_plugin_interlude.services import (
    AlterService,
    MemoryService,
    ScheduleService,
)
from astrbot_plugin_interlude.storage import Database


def run(coro):
    return asyncio.run(coro)


async def wait_for_row(db, sql, args=()):
    for _ in range(40):
        row = await db.one(sql, args)
        if row:
            return row
        await asyncio.sleep(0.05)
    raise AssertionError(f"Timed out waiting for: {sql}")


async def setup(tmp_path, output=None, config=None):
    db = Database(tmp_path / "interlude.db")
    await db.open()
    await db.ensure_story("story", "character", {"name": "Mira"}, {})
    pid = await db.ensure_participant("story", "test", "u1", "test:private:u1", "User")
    sent = []

    async def generate(_provider, _system, _prompt, _umo):
        return json.dumps(output or {"interaction": {"seen": True, "reply": {"mode": "none"}}})

    async def send(_umo, text):
        sent.append(text)
        return True

    settings = {"typing_enabled": False, "auto_advance_enabled": False, "message_merge_window_seconds": 0.5,
                "proactive_enabled": True, "proactive_whitelist": "u1", "proactive_willingness_threshold": 0.8,
                "quiet_hours_start": 0, "quiet_hours_end": 0, "proactive_cooldown_minutes": 0}
    settings.update(config or {})
    engine = NarrativeEngine(db, settings, generate)
    delivery = DeliveryEngine(db, settings, send)
    router = EventRouter(db, engine, delivery, settings)
    scheduler = RuntimeScheduler(db, router, settings)
    return db, pid, sent, router, scheduler


def test_message_merge(tmp_path):
    async def scenario():
        db, pid, _sent, router, _scheduler = await setup(tmp_path)
        await router.ingest(StoryEvent("story", EventType.USER_MESSAGE, "在吗", pid))
        await router.ingest(StoryEvent("story", EventType.USER_MESSAGE, "有件事", pid))
        await wait_for_row(db, "SELECT id FROM story_entries WHERE event_type='USER_MESSAGE_BATCH'")
        rows = await db.all("SELECT * FROM story_entries WHERE event_type='USER_MESSAGE_BATCH'")
        assert len(rows) == 1
        assert rows[0]["content"] == "在吗\n有件事"
        assert len(await db.all("SELECT * FROM story_entries WHERE event_type='USER_MESSAGE'")) == 2
        await router.close()
        await db.close()
    run(scenario())


def test_narrative_current_time_uses_configured_timezone(tmp_path):
    async def scenario():
        db, pid, _sent, router, _scheduler = await setup(tmp_path, config={"timezone": "Asia/Shanghai"})
        context = await router.engine.builder.build(StoryEvent("story", EventType.USER_MESSAGE, "hello", pid))
        local_time = datetime.fromisoformat(context["current_time"])
        cursor = datetime.fromisoformat(context["story_cursor"])
        assert local_time.utcoffset() == timedelta(hours=8)
        assert cursor.utcoffset() == timedelta(0)
        assert abs((local_time.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds()) < 5
        await router.close()
        await db.close()
    run(scenario())


def test_reset_cancels_messages_waiting_to_merge(tmp_path):
    async def scenario():
        db, pid, _sent, router, _scheduler = await setup(tmp_path)
        await router.ingest(StoryEvent("story", EventType.USER_MESSAGE, "old message", pid))
        await router.debouncer.cancel_story("story")
        await db.clear_story("story", purge=False)
        await asyncio.sleep(0.55)
        assert not await db.all("SELECT * FROM story_entries WHERE story_id='story'")
        await router.close()
        await db.close()
    run(scenario())


def test_story_lock_serializes_generation(tmp_path):
    async def scenario():
        db, pid, _sent, router, _ = await setup(tmp_path)
        active = 0
        peak = 0

        async def generate(*_args):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.04)
            active -= 1
            return '{"interaction":{"reply":{"mode":"none"}}}'

        router.engine.generate = generate
        await asyncio.gather(
            router.route(StoryEvent("story", EventType.USER_MESSAGE, "a", pid)),
            router.route(StoryEvent("story", EventType.USER_MESSAGE, "b", pid)),
        )
        assert peak == 1
        await router.close()
        await db.close()
    run(scenario())


def test_delivery_failure_is_not_story_speech(tmp_path):
    async def scenario():
        db, pid, _, router, _ = await setup(tmp_path)

        async def failure(_umo, _text):
            return False

        router.delivery.send = failure
        count = await router.delivery.deliver("story", pid, "test:private:u1", ["unsent"], None, "reply")
        assert count == 0
        assert (await db.one("SELECT status FROM deliveries"))["status"] == "delivery_failed"
        assert not await db.all("SELECT * FROM story_entries WHERE event_type='CHARACTER_MESSAGE_SENT'")
        await router.close()
        await db.close()
    run(scenario())


def test_model_failure_retains_event_and_retries(tmp_path):
    async def scenario():
        db, pid, _, router, _ = await setup(tmp_path)

        async def invalid(*_args):
            return "not json"

        router.engine.generate = invalid
        event = StoryEvent("story", EventType.USER_MESSAGE, "remember this", pid)
        cursor = (await db.one("SELECT cursor FROM stories WHERE id='story'"))["cursor"]
        await router.ingest(event)
        await wait_for_row(db, "SELECT id FROM runtime_jobs WHERE story_id='story' AND status='pending'")
        assert (await db.one("SELECT cursor FROM stories WHERE id='story'"))["cursor"] == cursor
        assert await db.one("SELECT * FROM story_entries WHERE id=?", (event.id,))
        assert await db.one("SELECT * FROM runtime_jobs WHERE story_id='story' AND status='pending'")
        await router.close()
        await db.close()
    run(scenario())


def test_schedule_and_agency(tmp_path):
    async def scenario():
        from astrbot_plugin_interlude.services import AgencyService

        db, _, _, router, _ = await setup(tmp_path)
        schedule = ScheduleService(db)
        start = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        end = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        await schedule.add("story", "granular", "正在上课", start, end)
        assert len(await schedule.upcoming("story")) == 1
        agency = await AgencyService(db).evaluate("story")
        assert not agency["can_reply"]
        await router.close()
        await db.close()
    run(scenario())


def test_overlay_and_memory(tmp_path):
    async def scenario():
        db, pid, _, router, _ = await setup(tmp_path)
        await db.bump_generation("story", "g")
        result = NarrativeResult.model_validate({"overlay_candidates": [{"scope": "perspective", "content": "Values punctuality."}]})
        await db.commit_narrative("story", StoryEvent("story", EventType.SYSTEM_EVENT, "test", pid), result, "g", now_iso())
        assert (await db.one("SELECT content FROM overlays"))["content"] == "Values punctuality."
        fact_id = await MemoryService(db).add("story", "Exam tomorrow", participant_id=pid)
        assert any(row["id"] == fact_id for row in await MemoryService(db).list("story", pid))
        await router.close()
        await db.close()
    run(scenario())


def test_alter_decay(tmp_path):
    async def scenario():
        db, _, _, router, _ = await setup(tmp_path)
        alter = AlterService(db, {"alter_enabled": True})
        await alter.apply("story", 5)
        before = (await db.one("SELECT accumulated_alter FROM alter_states"))["accumulated_alter"]
        old = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
        await db.execute("UPDATE alter_states SET last_updated=?", (old,))
        await alter.apply("story", 0)
        after = (await db.one("SELECT accumulated_alter FROM alter_states"))["accumulated_alter"]
        assert 0 < after < before
        await router.close()
        await db.close()
    run(scenario())


def test_immediate_reply_and_delivery_truth(tmp_path):
    async def scenario():
        output = {"story": [{"content": "She finished washing."}],
                  "interaction": {"seen": True, "reply": {"mode": "immediate", "messages": ["刚洗完澡<sep/>在吹头发"]}}}
        db, pid, sent, router, _ = await setup(tmp_path, output)
        await router.ingest(StoryEvent("story", EventType.USER_MESSAGE, "在干嘛", pid))
        await wait_for_row(db, "SELECT id FROM story_entries WHERE event_type='CHARACTER_MESSAGE_SENT' ORDER BY occurred_at DESC LIMIT 1")
        for _ in range(40):
            if len(sent) == 2:
                break
            await asyncio.sleep(0.05)
        assert sent == ["刚洗完澡", "在吹头发"]
        assert len(await db.all("SELECT * FROM story_entries WHERE event_type='CHARACTER_MESSAGE_SENT'")) == 2
        await router.close()
        await db.close()
    run(scenario())


def test_delayed_restore_and_generation_cancel(tmp_path):
    async def scenario():
        due = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        output = {"interaction": {"seen": True, "reply": {"mode": "delayed", "messages": ["刚洗完澡"], "send_at": due}}}
        db, pid, sent, router, _scheduler = await setup(tmp_path, output)
        await router.ingest(StoryEvent("story", EventType.USER_MESSAGE, "在干嘛", pid))
        await wait_for_row(db, "SELECT id FROM intents WHERE type='delayed_reply'")
        assert sent == []
        await router.close()
        await db.close()
        db2, _, sent2, router2, scheduler2 = await setup(tmp_path)
        await scheduler2.tick()
        assert sent2 == ["刚洗完澡"]
        await router2.close()
        await db2.close()
    run(scenario())


def test_new_message_cancels_old_during_typing(tmp_path):
    async def scenario():
        db, pid, sent, router, _ = await setup(tmp_path, config={"typing_enabled": True, "base_typing_ms": 250, "per_character_ms": 0, "typing_jitter_ratio": 0})
        generation = "old"
        await db.bump_generation("story", generation)
        task = asyncio.create_task(router.delivery.deliver("story", pid, "test:private:u1", ["old"], generation, "reply"))
        await asyncio.sleep(0.05)
        await router.ingest(StoryEvent("story", EventType.USER_MESSAGE, "算了没事", pid))
        await task
        assert sent == []
        await router.close()
        await db.close()
    run(scenario())


def test_invalid_json_repair_and_no_reply(tmp_path):
    assert parse_narrative('```json\n{"interaction":{"reply":{"mode":"none"}}}\n```').interaction.reply.mode == "none"
    async def scenario():
        db, pid, sent, router, _ = await setup(tmp_path)
        await router.ingest(StoryEvent("story", EventType.USER_MESSAGE, "hi", pid))
        await wait_for_row(db, "SELECT story_id FROM scenes WHERE story_id='story'")
        assert sent == []
        await router.close()
        await db.close()
    run(scenario())


def test_proactive_gate_and_persistence(tmp_path):
    async def scenario():
        db, pid, sent, router, scheduler = await setup(tmp_path)
        due = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        await db.execute("INSERT INTO intents(id,story_id,participant_id,type,content,due_at,willingness,created_at) VALUES(?,?,?,?,?,?,?,?)",
                         ("intent", "story", pid, "proactive", "早点睡", due, 0.9, now_iso()))
        await scheduler.tick()
        assert sent == ["早点睡"]
        assert (await db.one("SELECT status FROM intents WHERE id='intent'"))["status"] == "completed"
        await router.close()
        await db.close()
    run(scenario())


def test_relationship_limit_migration_and_participants(tmp_path):
    async def scenario():
        db, pid, _, router, _ = await setup(tmp_path)
        second = await db.ensure_participant("story", "test", "u2", "test:private:u2", "Other")
        assert second != pid
        await db.bump_generation("story", "g")
        event = StoryEvent("story", EventType.USER_MESSAGE, "hello", pid)
        result = NarrativeResult.model_validate({"relationship_changes": [{"participant_id": pid, "dimension": "trust", "delta": 0.9}]})
        await db.commit_narrative("story", event, result, "g", now_iso())
        row = await db.one("SELECT data_json FROM relationships WHERE participant_id=?", (pid,))
        assert json.loads(row["data_json"])["trust"] == 0.55
        assert await db.one("SELECT * FROM relationships WHERE participant_id=?", (second,)) is None
        assert (await db.one("SELECT value FROM metadata WHERE key='schema_version'"))["value"] == "4"
        await router.close()
        await db.close()
    run(scenario())


def test_proactive_quiet_hours_block(tmp_path):
    async def scenario():
        db, pid, sent, router, scheduler = await setup(tmp_path, config={"quiet_hours_start": 0, "quiet_hours_end": 24})
        await db.execute("INSERT INTO intents(id,story_id,participant_id,type,content,due_at,willingness,created_at) VALUES(?,?,?,?,?,?,?,?)",
                         ("quiet", "story", pid, "proactive", "hello", now_iso(), 1.0, now_iso()))
        await scheduler.tick()
        assert sent == []
        assert (await db.one("SELECT status FROM intents WHERE id='quiet'"))["status"] == "cancelled"
        await router.close()
        await db.close()
    run(scenario())


def test_migration_reopen(tmp_path):
    async def scenario():
        db, _, _, router, _ = await setup(tmp_path)
        await router.close()
        await db.close()
        reopened = Database(tmp_path / "interlude.db")
        await reopened.open()
        assert (await reopened.one("SELECT COUNT(*) AS n FROM stories"))["n"] == 1
        assert (await reopened.one("SELECT value FROM metadata WHERE key='schema_version'"))["value"] == "4"
        await reopened.close()
    run(scenario())


def test_context_compression(tmp_path):
    async def scenario():
        db, pid, _, router, _ = await setup(tmp_path, config={"compression_enabled": True, "context_max_chars": 2000})
        for index in range(20):
            await db.append_event(StoryEvent("story", EventType.SYSTEM_EVENT, f"old-{index}-" + "x" * 200, pid))
        calls = []

        async def generate(_provider, system, _prompt, _umo):
            calls.append(system)
            if "压缩剧情" in system:
                return "Earlier events were routine."
            return '{"interaction":{"reply":{"mode":"none"}}}'

        router.engine.generate = generate
        event = StoryEvent("story", EventType.SYSTEM_EVENT, "now", pid)
        result = await router.engine.run(event, "LIFE_ADVANCE")
        assert result.interaction.reply.mode == "none"
        assert any("压缩剧情" in call for call in calls)
        await db.commit_narrative("story", event, result, "", now_iso())
        assert (await db.one("SELECT summary FROM scenes WHERE story_id='story'"))["summary"]
        await router.close()
        await db.close()
    run(scenario())
