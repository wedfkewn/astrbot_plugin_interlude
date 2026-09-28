"""Event routing, debounce, generation cancellation, delivery, and durable scheduling."""

from __future__ import annotations

import asyncio
import json
import random
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .models import EventType, IntentCandidate, StoryEvent, new_id, now_iso
from .narrative import NarrativeEngine
from .services import AgencyService, AlterService
from .storage import Database

Send = Callable[[str, str], Awaitable[bool]]
Log = Callable[[str, str], None]


class MessageDebouncer:
    def __init__(self, window: float, submit: Callable[[StoryEvent], Awaitable[None]],
                 persist: Callable[[StoryEvent], Awaitable[None]] | None = None):
        self.window = max(0.5, min(10.0, window))
        self.submit = submit
        self.persist = persist
        self.pending: dict[tuple[str, str], list[StoryEvent]] = defaultdict(list)
        self.tasks: dict[tuple[str, str], asyncio.Task] = {}
        self.active: set[asyncio.Task] = set()

    def add(self, event: StoryEvent) -> None:
        key = (event.story_id, event.participant_id or "")
        self.pending[key].append(event)
        old = self.tasks.get(key)
        if old:
            old.cancel()
        self.tasks[key] = asyncio.create_task(self._flush(key))

    async def _flush(self, key: tuple[str, str]) -> None:
        try:
            await asyncio.sleep(self.window)
            events = self.pending.pop(key, [])
            if not events:
                return
            if self.tasks.get(key) is asyncio.current_task():
                self.tasks.pop(key, None)
            self.active.add(asyncio.current_task())
            merged = StoryEvent(
                story_id=key[0], participant_id=events[0].participant_id,
                event_type=EventType.USER_MESSAGE_BATCH if len(events) > 1 else events[0].event_type,
                content="\n".join(e.content for e in events),
                metadata={**events[-1].metadata, "source_event_ids": [e.id for e in events],
                          "generation_id": events[-1].metadata.get("generation_id")},
            )
            if len(events) > 1 and self.persist:
                await self.persist(merged)
            await self.submit(merged)
        except asyncio.CancelledError:
            return
        finally:
            self.active.discard(asyncio.current_task())
            if self.tasks.get(key) is asyncio.current_task():
                self.tasks.pop(key, None)

    async def close(self) -> None:
        tasks = list({*self.tasks.values(), *self.active})
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()

    async def cancel_story(self, story_id: str) -> None:
        """Discard messages still waiting in the merge window before a story reset."""
        matching = [key for key in self.pending if key[0] == story_id]
        tasks = [self.tasks.pop(key) for key in matching if key in self.tasks]
        for key in matching:
            self.pending.pop(key, None)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


class DeliveryEngine:
    def __init__(self, db: Database, config: dict, send: Send, log: Log | None = None):
        self.db, self.config, self.send = db, config, send
        self.random = random.Random()
        self.log = log or (lambda _phase, _detail: None)

    def split(self, messages: list[str]) -> list[str]:
        return [part.strip() for message in messages for part in message.split("<sep/>") if part.strip()]

    async def deliver(self, story_id: str, participant_id: str | None, umo: str,
                      messages: list[str], generation_id: str | None, kind: str,
                      intent_id: str | None = None) -> int:
        sent = 0
        for part in self.split(messages):
            if generation_id and not await self.db.generation_valid(story_id, generation_id):
                break
            if self.config.get("typing_enabled", True):
                millis = int(self.config.get("base_typing_ms", 300)) + len(part) * int(self.config.get("per_character_ms", 30))
                jitter = float(self.config.get("typing_jitter_ratio", 0.3))
                delay = min(float(self.config.get("max_typing_delay", 5)), max(0, millis * self.random.uniform(1-jitter, 1+jitter) / 1000))
                await asyncio.sleep(delay)
            if generation_id and not await self.db.generation_valid(story_id, generation_id):
                break
            delivery_id = await self.db.record_delivery(story_id, participant_id, umo, part, generation_id, kind, intent_id)
            try:
                success = await self.send(umo, part)
            except Exception as exc:  # noqa: BLE001 - platform adapters may raise arbitrary errors
                await self.db.finish_delivery(delivery_id, False, type(exc).__name__)
                self.log("DELIVERY", f"failed: {type(exc).__name__}")
                break
            if success:
                await self.db.complete_delivery(delivery_id, story_id, participant_id, part)
                self.log("DELIVERY", "sent and committed")
                sent += 1
            else:
                await self.db.finish_delivery(delivery_id, False, "platform unavailable")
                self.log("DELIVERY", "platform unavailable")
                break
        return sent


class EventRouter:
    def __init__(self, db: Database, engine: NarrativeEngine, delivery: DeliveryEngine,
                 config: dict, log: Log | None = None):
        self.db, self.engine, self.delivery, self.config = db, engine, delivery, config
        self.log = log or (lambda _phase, _detail: None)
        self.locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.debouncer = MessageDebouncer(float(config.get("message_merge_window_seconds", 2)), self.route, db.append_event)
        self.alter = AlterService(db, config)
        self.last_alter_error: str | None = None

    async def ingest(self, event: StoryEvent, *, debounce: bool = True) -> None:
        """Persist input immediately; only isolated stories supersede earlier replies."""
        shared = bool(self.config.get("shared_story", False))
        generation_id = None if shared else new_id()
        await self.db.ingest_event(event, generation_id, shared=shared)
        self.log("EVENT", f"{event.event_type.value} persisted")
        if debounce:
            self.debouncer.add(event)

    async def route(self, event: StoryEvent, *, retry_on_failure: bool = True) -> bool:
        lock = self.locks[event.story_id]
        async with lock:
            story = await self.db.one("SELECT paused,generation_id FROM stories WHERE id=?", (event.story_id,))
            if not story or story["paused"]:
                return False
            generation_id = story["generation_id"]
            expected_generation = event.metadata.get("generation_id")
            if expected_generation and expected_generation != generation_id:
                return False
            if event.event_type == EventType.PROACTIVE_CHECK:
                participant = await self.db.one("SELECT last_interaction_at FROM participants WHERE id=?", (event.participant_id,))
                if not participant or participant["last_interaction_at"] != event.metadata.get("last_user_at"):
                    return False
            await self.db.append_event(event)
            mode = {EventType.FOLLOW_UP: "FOLLOW_UP", EventType.INTENT_DUE: "INTENT_DUE",
                    EventType.AUTO_ADVANCE: "LIFE_ADVANCE",
                    EventType.PROACTIVE_CHECK: "PROACTIVE_CHECK",
                    EventType.SCHEDULE_BOUNDARY: "LIFE_ADVANCE"}.get(event.event_type, "USER_EVENT")
            try:
                self.log("CONTEXT", f"building {mode}")
                result = await self.engine.run(event, mode)
                self.log("NARRATIVE", f"validated; beats={len(result.story)} intents={len(result.intents)}")
                reply = result.interaction.reply
                if event.event_type == EventType.PROACTIVE_CHECK:
                    result.interaction.reply.mode = "none"
                    result.interaction.reply.messages = []
                    result.intents = [intent for intent in result.intents
                                      if intent.type == "proactive" and
                                      (not intent.participant_id or intent.participant_id == event.participant_id)][:1]
                    for intent in result.intents:
                        intent.reason = "静默触发 · " + (intent.reason.strip() or "结合最近对话与故事状态决定联系")
                if result.interaction.seen and reply.mode == "delayed" and reply.messages and event.participant_id:
                    try:
                        due = datetime.fromisoformat(reply.send_at).astimezone(timezone.utc).isoformat() if reply.send_at else ""
                    except (ValueError, TypeError):
                        due = ""
                    result.intents.append(IntentCandidate(
                        type="delayed_reply", content="<sep/>".join(reply.messages),
                        due_at=due or (datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat(),
                        participant_id=event.participant_id, willingness=1,
                    ))
                if self.config.get("follow_up_enabled", False) and event.event_type in (
                    EventType.USER_MESSAGE, EventType.USER_MESSAGE_BATCH
                ):
                    result.intents.append(IntentCandidate(
                        type="follow_up", content="Review the recent conversation and continue living; doing nothing is allowed.",
                        due_at=(datetime.now(timezone.utc)+timedelta(
                            minutes=max(1, int(self.config.get("follow_up_minutes", 10)))
                        )).isoformat(), participant_id=event.participant_id,
                    ))
                await self.db.commit_narrative(event.story_id, event, result, generation_id, now_iso())
                self.log("STATE", "narrative committed")
            except Exception:  # noqa: BLE001 - provider failures become durable retries
                if event.event_type == EventType.PROACTIVE_CHECK:
                    participant = await self.db.one("SELECT last_interaction_at FROM participants WHERE id=?", (event.participant_id,))
                    if not participant or participant["last_interaction_at"] != event.metadata.get("last_user_at"):
                        return False
                if expected_generation and not await self.db.generation_valid(event.story_id, expected_generation):
                    self.log("NARRATIVE", "stale generation discarded")
                    return False
                if retry_on_failure:
                    due = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
                    persisted = await self.db.one("SELECT content FROM story_entries WHERE id=?", (event.id,))
                    await self.db.execute(
                        "INSERT OR REPLACE INTO runtime_jobs(id,story_id,kind,due_at,payload_json,status) VALUES(?,?,?,?,?,'pending')",
                        (event.id, event.story_id, "retry_event", due,
                         json.dumps({"event_type": event.event_type.value, "content": persisted["content"] if persisted else event.content,
                                     "participant_id": event.participant_id, "created_at": event.created_at,
                                     "metadata": event.metadata}, ensure_ascii=False)),
                    )
                self.log("NARRATIVE", "failed; retry queued")
                return False
            try:
                await self.alter.apply(event.story_id, result.alter)
                await self.alter.summarize(event.story_id, self.engine.generate)
            except Exception as exc:  # noqa: BLE001 - core narrative has already committed
                self.last_alter_error = type(exc).__name__
            if event.metadata.get("is_group") and result.interaction.willingness_to_speak < float(
                self.config.get("group_speak_threshold", 0.7)
            ):
                return True
            if not result.interaction.seen or reply.mode == "none" or not reply.messages:
                return True
            participant = await self.db.one("SELECT umo FROM participants WHERE id=?", (event.participant_id,)) if event.participant_id else None
            if not participant:
                return True
            if reply.mode == "delayed":
                return True
        await self.delivery.deliver(event.story_id, event.participant_id, participant["umo"], reply.messages, generation_id, "reply")
        return True

    async def close(self) -> None:
        await self.debouncer.close()


class RuntimeScheduler:
    """Database is the authoritative job queue, including across restarts."""

    def __init__(self, db: Database, router: EventRouter, config: dict,
                 refresh_schedule: Callable[[str], Awaitable[int]] | None = None,
                 log: Log | None = None):
        self.db, self.router, self.config = db, router, config
        self.refresh_schedule = refresh_schedule
        self.log = log or (lambda _phase, _detail: None)
        self.task: asyncio.Task | None = None
        self.last_error: str | None = None

    def start(self) -> None:
        self.task = asyncio.create_task(self._run())

    async def close(self) -> None:
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def _run(self) -> None:
        try:
            await self._recover_inbound()
        except Exception as exc:  # noqa: BLE001 - polling must still start
            self.last_error = type(exc).__name__
        while True:
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001 - keep scheduler alive after one failed job
                self.last_error = type(exc).__name__
            await asyncio.sleep(max(1, int(self.config.get("scheduler_poll_seconds", 10))))

    async def _recover_inbound(self) -> None:
        """Replay the newest unprocessed input after a restart or interrupted generation."""
        rows = await self.db.all(
            "SELECT e.* FROM story_entries e JOIN stories s ON s.id=e.story_id "
            "WHERE e.event_type IN ('USER_MESSAGE','USER_MESSAGE_BATCH','USER_IMAGE') AND e.occurred_at>s.cursor "
            "ORDER BY e.occurred_at DESC",
        )
        seen: set[str] = set()
        for row in rows:
            if row["story_id"] in seen:
                continue
            seen.add(row["story_id"])
            await self.router.route(StoryEvent(
                row["story_id"], EventType(row["event_type"]), row["content"],
                row["participant_id"], id=row["id"], created_at=row["occurred_at"],
                metadata=json.loads(row["metadata_json"]),
            ))

    async def tick(self) -> None:
        for intent in await self.db.due_intents(now_iso()):
            self.log("SCHEDULER", f"due intent {intent['type']}")
            await self._process_intent(intent)
        for job in await self.db.all("SELECT * FROM runtime_jobs WHERE status='pending' AND due_at<=? ORDER BY due_at LIMIT 50", (now_iso(),)):
            payload = json.loads(job["payload_json"])
            await self.db.execute("UPDATE runtime_jobs SET status='processing' WHERE id=?", (job["id"],))
            try:
                if job["kind"] == "schedule_boundary":
                    schedule = await self.db.one("SELECT status FROM schedules WHERE id=?", (payload["schedule_id"],))
                    if not schedule or schedule["status"] != "active":
                        await self.db.execute("UPDATE runtime_jobs SET status='cancelled' WHERE id=?", (job["id"],))
                        continue
                    success = await self.router.route(StoryEvent(
                        job["story_id"], EventType.SCHEDULE_BOUNDARY,
                        f"Schedule {payload['boundary']}: {payload['content']}",
                    ), retry_on_failure=False)
                    if success:
                        await self.db.execute("UPDATE runtime_jobs SET status='completed' WHERE id=?", (job["id"],))
                    else:
                        await self.db.execute("UPDATE runtime_jobs SET status='pending',due_at=? WHERE id=?",
                                              ((datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat(), job["id"]))
                    continue
                success = await self.router.route(StoryEvent(
                    job["story_id"], EventType(payload["event_type"]), payload["content"],
                    payload["participant_id"], id=job["id"], created_at=payload["created_at"],
                    metadata=payload.get("metadata", {}),
                ))
                if success:
                    await self.db.execute("UPDATE runtime_jobs SET status='completed' WHERE id=?", (job["id"],))
                elif payload.get("metadata", {}).get("generation_id") and not await self.db.generation_valid(
                    job["story_id"], payload["metadata"]["generation_id"]
                ):
                    await self.db.execute("UPDATE runtime_jobs SET status='cancelled' WHERE id=?", (job["id"],))
                else:
                    await self.db.execute("UPDATE runtime_jobs SET status='pending',due_at=? WHERE id=? AND status='processing'",
                                          ((datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat(), job["id"]))
            except Exception:  # noqa: BLE001 - retry on a later poll
                await self.db.execute("UPDATE runtime_jobs SET status='pending',due_at=? WHERE id=?",
                                      ((datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat(), job["id"]))
        await self._consider_idle_chats()
        if self.config.get("auto_advance_enabled", True):
            cutoff = (datetime.now(timezone.utc) - timedelta(minutes=int(self.config.get("auto_advance_minutes", 60)))).isoformat()
            for story in await self.db.all("SELECT id FROM stories WHERE cursor<? AND paused=0", (cutoff,)):
                await self.router.route(StoryEvent(story["id"], EventType.AUTO_ADVANCE, "Time has passed."))
        if self.config.get("schedule_enabled", False) and self.refresh_schedule:
            for story in await self.db.all("SELECT id FROM stories WHERE paused=0"):
                row = await self.db.one(
                    "SELECT MAX(end_at) AS until FROM schedules WHERE story_id=? AND status='active'",
                    (story["id"],),
                )
                if row["until"] and row["until"] > (datetime.now(timezone.utc)+timedelta(hours=12)).isoformat():
                    continue
                key = f"schedule_attempt:{story['id']}"
                attempted = await self.db.one("SELECT value FROM metadata WHERE key=?", (key,))
                if attempted and datetime.fromisoformat(attempted["value"]) > datetime.now(timezone.utc)-timedelta(hours=1):
                    continue
                await self.db.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)", (key, now_iso()))
                try:
                    await self.refresh_schedule(story["id"])
                except Exception as exc:  # noqa: BLE001 - next maintenance window retries
                    self.last_error = type(exc).__name__

    async def _consider_idle_chats(self) -> None:
        if not (self.config.get("enabled", True) and self.config.get("proactive_enabled", False)
                and self.config.get("proactive_idle_enabled", False)):
            return
        whitelist = {x.strip() for x in str(self.config.get("proactive_whitelist", "")).split(",") if x.strip()}
        if not whitelist:
            return
        cutoff = (datetime.now(timezone.utc) - timedelta(
            minutes=max(30, int(self.config.get("proactive_idle_minutes", 1200))))).isoformat()
        user_ids = sorted(whitelist)
        placeholders = ",".join("?" for _ in user_ids)
        participants = await self.db.all(
            "SELECT p.id,p.story_id,p.platform_user_id,p.last_interaction_at FROM participants p "
            "JOIN stories s ON s.id=p.story_id WHERE p.enabled=1 AND s.paused=0 "
            f"AND p.platform_user_id IN ({placeholders}) "
            "AND p.last_interaction_at IS NOT NULL AND p.last_interaction_at<=? "
            "AND NOT EXISTS (SELECT 1 FROM story_entries e WHERE e.participant_id=p.id "
            "AND e.event_type='PROACTIVE_CHECK' AND e.occurred_at>=p.last_interaction_at) "
            "ORDER BY p.last_interaction_at LIMIT 20", (*user_ids, cutoff))
        for participant in participants:
            await self.router.route(StoryEvent(
                participant["story_id"], EventType.PROACTIVE_CHECK,
                "距离这位用户上次发消息已经过了一段时间。请判断是否有自然的主动联系缘由。",
                participant["id"], metadata={"last_user_at": participant["last_interaction_at"]},
            ))

    async def _process_intent(self, intent) -> None:
        await self.db.mark_intent(intent["id"], "processing")
        await self.db.execute("UPDATE intents SET attempts=attempts+1 WHERE id=?", (intent["id"],))
        try:
            participant = await self.db.one("SELECT * FROM participants WHERE id=?", (intent["participant_id"],)) if intent["participant_id"] else None
            if intent["type"] == "delayed_reply":
                valid = await self.db.generation_valid(intent["story_id"], intent["generation_id"])
                if valid and participant:
                    await self._deliver_intent(intent, participant["umo"], "reply", intent["generation_id"])
                else:
                    await self.db.mark_intent(intent["id"], "cancelled")
            elif intent["type"] == "proactive":
                already_started = await self.db.one(
                    "SELECT 1 FROM deliveries WHERE intent_id=? AND status='sent' LIMIT 1", (intent["id"],)
                )
                allowed = bool(participant) and (bool(already_started) or await self._proactive_allowed(intent, participant))
                if allowed:
                    await self._deliver_intent(intent, participant["umo"], "proactive", None)
                else:
                    await self.db.mark_intent(intent["id"], "cancelled")
            else:
                event_type = EventType.FOLLOW_UP if intent["type"] == "follow_up" else EventType.INTENT_DUE
                succeeded = await self.router.route(
                    StoryEvent(intent["story_id"], event_type, intent["content"], intent["participant_id"], id=intent["id"]),
                    retry_on_failure=False,
                )
                if succeeded:
                    await self.db.mark_intent(intent["id"], "completed")
                else:
                    await self.db.execute(
                        "UPDATE intents SET status='pending',due_at=? WHERE id=?",
                        ((datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat(), intent["id"]),
                    )
        except Exception:  # noqa: BLE001 - durable intent remains retryable
            await self.db.execute(
                "UPDATE intents SET status='pending',due_at=? WHERE id=?",
                ((datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat(), intent["id"]),
            )

    async def _deliver_intent(self, intent, umo: str, kind: str, generation_id: str | None) -> None:
        parts = self.router.delivery.split([intent["content"]])
        records = await self.db.all(
            "SELECT content,status FROM deliveries WHERE intent_id=? ORDER BY created_at,id",
            (intent["id"],),
        )
        if any(record["status"] == "uncertain" for record in records):
            await self.db.mark_intent(intent["id"], "cancelled")
            return
        sent_parts = [record["content"] for record in records if record["status"] == "sent"]
        if sent_parts != parts[:len(sent_parts)]:
            await self.db.mark_intent(intent["id"], "cancelled")
            return
        if len(sent_parts) < len(parts):
            await self.router.delivery.deliver(
                intent["story_id"], intent["participant_id"], umo,
                parts[len(sent_parts):], generation_id, kind, intent["id"],
            )
        sent = await self.db.one(
            "SELECT COUNT(*) AS n FROM deliveries WHERE intent_id=? AND status='sent'", (intent["id"],),
        )
        if sent["n"] == len(parts):
            await self.db.mark_intent(intent["id"], "completed")
            return
        maximum = max(0, int(self.config.get("delivery_retry_attempts", 0)))
        if intent["attempts"] < maximum:
            delay = max(1, int(self.config.get("delivery_retry_minutes", 2)))
            await self.db.execute(
                "UPDATE intents SET status='pending',due_at=? WHERE id=?",
                ((datetime.now(timezone.utc)+timedelta(minutes=delay)).isoformat(), intent["id"]),
            )
        else:
            await self.db.mark_intent(intent["id"], "cancelled")

    async def _proactive_allowed(self, intent, participant) -> bool:
        if not self.config.get("proactive_enabled", False) or not participant or not participant["enabled"]:
            return False
        if intent["reason"].startswith("静默触发 · ") and not self.config.get("proactive_idle_enabled", False):
            return False
        whitelist = {x.strip() for x in str(self.config.get("proactive_whitelist", "")).split(",") if x.strip()}
        if participant["platform_user_id"] not in whitelist:
            return False
        if intent["willingness"] < float(self.config.get("proactive_willingness_threshold", 0.8)):
            return False
        if intent["reason"].startswith("静默触发 · ") and participant["last_interaction_at"] and participant["last_interaction_at"] > intent["created_at"]:
            return False
        agency = await AgencyService(self.db).evaluate(intent["story_id"])
        if not agency["proactive_allowed"]:
            return False
        try:
            zone = ZoneInfo(str(self.config.get("timezone", "Asia/Shanghai")))
        except KeyError:
            zone = timezone.utc
        now = datetime.now(zone)
        start = max(0, min(23, int(self.config.get("quiet_hours_start", 23))))
        end = max(0, min(24, int(self.config.get("quiet_hours_end", 8))))
        quiet = start <= now.hour or now.hour < end if start > end else start <= now.hour < end
        if quiet:
            return False
        latest = await self.db.latest_sent_at(participant["id"])
        if latest and datetime.fromisoformat(latest) > datetime.now(timezone.utc) - timedelta(minutes=int(self.config.get("proactive_cooldown_minutes", 60))):
            return False
        since = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        per_user = int(self.config.get("proactive_per_user_daily_limit", self.config.get("proactive_daily_limit", 2)))
        story_limit = int(self.config.get("proactive_story_daily_limit", 5))
        return (await self.db.sent_count(participant["id"], since) < per_user
                and await self.db.story_sent_count(intent["story_id"], since) < story_limit)
