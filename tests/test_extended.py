"""Cross-feature and upgrade acceptance scenarios."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

from astrbot_plugin_interlude.models import EventType, StoryEvent
from astrbot_plugin_interlude.narrative import NarrativeEngine
from astrbot_plugin_interlude.runtime import (
    DeliveryEngine,
    EventRouter,
    RuntimeScheduler,
)
from astrbot_plugin_interlude.services import (
    AgencyService,
    SchedulePlanner,
    ScheduleService,
)
from astrbot_plugin_interlude.storage import MIGRATIONS, Database


def run(coro):
    return asyncio.run(coro)


async def runtime(tmp_path, generate, config=None):
    db = Database(tmp_path / "story.db")
    await db.open()
    await db.ensure_story("s", "c", {"name": "Mira"}, {})
    pid = await db.ensure_participant("s", "test", "u", "test:private:u", "User")
    sent = []

    async def send(_umo, text):
        sent.append(text)
        return True

    settings = {"typing_enabled": False, "auto_advance_enabled": False,
                "proactive_enabled": True, "proactive_whitelist": "u",
                "quiet_hours_start": 0, "quiet_hours_end": 0,
                "proactive_cooldown_minutes": 0, "message_merge_window_seconds": 0.5}
    settings.update(config or {})
    router = EventRouter(db, NarrativeEngine(db, settings, generate),
                         DeliveryEngine(db, settings, send), settings)
    return db, pid, sent, router, RuntimeScheduler(db, router, settings)


def test_upgrade_from_schema_one(tmp_path):
    async def scenario():
        import aiosqlite

        path = tmp_path / "story.db"
        async with aiosqlite.connect(path) as raw:
            await raw.executescript(MIGRATIONS[0])
            await raw.execute("INSERT INTO metadata VALUES('schema_version','1')")
            await raw.commit()
        db = Database(path)
        await db.open()
        assert (await db.one("SELECT value FROM metadata WHERE key='schema_version'"))["value"] == "4"
        assert await db.one("SELECT name FROM sqlite_master WHERE name='perspectives'")
        await db.close()
    run(scenario())


def test_group_willingness_and_state(tmp_path):
    async def scenario():
        async def generate(*_args):
            return json.dumps({"interaction": {"willingness_to_speak": 0.2,
                          "reply": {"mode": "immediate", "messages": ["hello"]}},
                          "state_update": {"location": "library", "activity": "reading"}})

        db, pid, sent, router, _ = await runtime(tmp_path, generate,
                                                  {"group_speak_threshold": 0.7})
        await router.route(StoryEvent("s", EventType.GROUP_MESSAGE, "chat", pid,
                                      metadata={"is_group": True, "speaker": "User", "mentioned": True}))
        assert sent == []
        state = json.loads((await db.one("SELECT state_json FROM stories WHERE id='s'"))["state_json"])
        assert state["location"] == "library"
        await router.close()
        await db.close()
    run(scenario())


def test_image_observation_stays_transient(tmp_path):
    async def scenario():
        prompts = []

        async def generate(_provider, _system, prompt, _umo):
            prompts.append(prompt)
            return '{"interaction":{"reply":{"mode":"none"}}}'

        db, pid, _, router, _ = await runtime(tmp_path, generate)
        event = StoryEvent("s", EventType.USER_IMAGE, "[图片]", pid)
        await router.ingest(event, debounce=False)
        event.content += " 观察：海边夕阳"
        await router.route(event)
        assert "海边夕阳" in prompts[-1]
        persisted = await db.one("SELECT content FROM story_entries WHERE id=?", (event.id,))
        assert persisted["content"] == "[图片]"
        await router.close()
        await db.close()
    run(scenario())


def test_schedule_planner_persists_and_deduplicates_stable(tmp_path):
    async def scenario():
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        plan = {"blocks": [{"kind": "stable", "content": "class",
                            "start_at": future.isoformat(),
                            "end_at": (future + timedelta(hours=1)).isoformat()}]}

        async def generate(*_args):
            return json.dumps(plan)

        db, _, _, router, _ = await runtime(tmp_path, generate)
        planner = SchedulePlanner(db, {}, generate)
        assert await planner.refresh("s") == 1
        assert await planner.refresh("s") == 1
        assert (await db.one("SELECT COUNT(*) AS n FROM schedules"))["n"] == 1
        await router.close()
        await db.close()
    run(scenario())


def test_delivery_recovery_and_reset(tmp_path):
    async def scenario():
        async def generate(*_args):
            return '{"interaction":{"reply":{"mode":"none"}}}'

        db, pid, _, router, _ = await runtime(tmp_path, generate)
        delivery = await db.record_delivery("s", pid, "test:private:u", "maybe", None, "reply")
        assert await db.mark_uncertain_deliveries() == 1
        assert (await db.one("SELECT status FROM deliveries WHERE id=?", (delivery,)))["status"] == "uncertain"
        await db.clear_story("s", purge=False)
        assert (await db.one("SELECT COUNT(*) AS n FROM deliveries"))["n"] == 0
        assert await db.one("SELECT * FROM participants WHERE id=?", (pid,))
        await db.clear_story("s", purge=True)
        assert not await db.one("SELECT * FROM stories WHERE id='s'")
        await router.close()
        await db.close()
    run(scenario())


def test_schedule_boundary_survives_restart_and_routes(tmp_path):
    async def scenario():
        async def generate(*_args):
            return '{"story":[{"content":"The scheduled activity changed."}],"interaction":{"reply":{"mode":"none"}}}'

        db, _, _, router, _ = await runtime(tmp_path, generate)
        due = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        end = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        schedule_id = await ScheduleService(db).add("s", "granular", "class ended", due, end)
        assert (await db.one("SELECT COUNT(*) AS n FROM runtime_jobs WHERE story_id='s'"))["n"] == 2
        await router.close()
        await db.close()

        reopened = Database(tmp_path / "story.db")
        await reopened.open()
        settings = {"typing_enabled": False, "auto_advance_enabled": False}
        new_router = EventRouter(reopened, NarrativeEngine(reopened, settings, generate),
                                 DeliveryEngine(reopened, settings, lambda *_: None), settings)
        await RuntimeScheduler(reopened, new_router, settings).tick()
        assert (await reopened.one("SELECT COUNT(*) AS n FROM runtime_jobs WHERE status='completed'"))["n"] == 2
        assert (await reopened.one("SELECT COUNT(*) AS n FROM story_entries WHERE event_type='SCHEDULE_BOUNDARY'"))["n"] == 2
        assert await reopened.one("SELECT id FROM schedules WHERE id=?", (schedule_id,))
        await new_router.close()
        await reopened.close()
    run(scenario())


def test_segmented_intent_retries_only_unsent_part(tmp_path):
    async def scenario():
        async def generate(*_args):
            return '{"interaction":{"reply":{"mode":"none"}}}'

        db, pid, _, router, scheduler = await runtime(
            tmp_path, generate, {"delivery_retry_attempts": 2, "delivery_retry_minutes": 1}
        )
        outcomes = [True, False, True]
        sent = []

        async def send(_umo, content):
            sent.append(content)
            return outcomes.pop(0)

        router.delivery.send = send
        due = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
        await db.execute(
            "INSERT INTO intents(id,story_id,participant_id,type,content,due_at,status,willingness,reason,created_at) "
            "VALUES(?,?,?,?,?,?,'pending',?,?,?)",
            ("i", "s", pid, "proactive", "first<sep/>second", due, 1, "", due),
        )
        await scheduler.tick()
        assert sent == ["first", "second"]
        assert (await db.one("SELECT status FROM intents WHERE id='i'"))["status"] == "pending"
        await db.execute("UPDATE intents SET due_at=? WHERE id='i'", (due,))
        await scheduler.tick()
        assert sent == ["first", "second", "second"]
        assert (await db.one("SELECT status FROM intents WHERE id='i'"))["status"] == "completed"
        assert (await db.one("SELECT COUNT(*) AS n FROM story_entries WHERE event_type='CHARACTER_MESSAGE_SENT'"))["n"] == 2
        await router.close()
        await db.close()
    run(scenario())


def test_final_life_scenario_across_restart(tmp_path):
    async def scenario():
        due = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()

        async def generate(_provider, _system, prompt, _umo):
            context = json.loads(prompt).get("context", {})
            content = context.get("current_event", {}).get("content", "")
            if "在干嘛" in content:
                return json.dumps({"story": [{"content": "She finished bathing."}],
                    "state_update": {"location": "home", "activity": "resting"},
                    "interaction": {"reply": {"mode": "delayed", "messages": ["刚洗完澡"], "send_at": due}}})
            if "哈哈" in content:
                return json.dumps({"facts": [{"content": "User has an exam tomorrow", "scope": "commitments"}],
                    "relationship_changes": [{"participant_id": pid, "dimension": "comfort", "delta": 0.02}],
                    "overlay_candidates": [{"scope": "relationship", "content": "They now joke more easily."}],
                    "intents": [{"type": "proactive", "content": "你明天不是还有考试吗，早点睡。",
                                 "due_at": due, "willingness": 0.95}],
                    "interaction": {"reply": {"mode": "none"}}})
            return '{"interaction":{"reply":{"mode":"none"}}}'

        db, pid, sent, router, scheduler = await runtime(tmp_path, generate)
        await router.ingest(StoryEvent("s", EventType.USER_MESSAGE, "在干嘛", pid))
        for _ in range(40):
            if await db.one("SELECT id FROM intents WHERE type='delayed_reply'"):
                break
            await asyncio.sleep(0.05)
        assert sent == []
        await scheduler.tick()
        assert sent == ["刚洗完澡"]
        await router.ingest(StoryEvent("s", EventType.USER_MESSAGE, "哈哈", pid))
        for _ in range(40):
            if await db.one("SELECT id FROM intents WHERE type='proactive'"):
                break
            await asyncio.sleep(0.05)
        await scheduler.tick()
        assert sent[-1] == "你明天不是还有考试吗，早点睡。"
        assert (await db.one("SELECT COUNT(*) AS n FROM story_entries WHERE event_type='CHARACTER_MESSAGE_SENT'"))["n"] == 2
        await router.close()
        await db.close()
        reopened = Database(tmp_path / "story.db")
        await reopened.open()
        assert await reopened.one("SELECT * FROM facts WHERE story_id='s'")
        assert await reopened.one("SELECT * FROM relationships WHERE participant_id=?", (pid,))
        assert await reopened.one("SELECT * FROM overlays WHERE story_id='s'")
        assert (await reopened.one("SELECT COUNT(*) AS n FROM intents WHERE status='completed'"))["n"] == 2
        assert json.loads((await reopened.one("SELECT state_json FROM stories WHERE id='s'"))["state_json"])["location"] == "home"
        assert (await AgencyService(reopened).evaluate("s"))["can_reply"]
        await reopened.close()
    run(scenario())
