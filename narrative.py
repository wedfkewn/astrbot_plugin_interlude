"""Context construction, strict model parsing, and narrative generation."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError

from .models import NarrativeResult, StateUpdate, StoryEvent, now_iso
from .services import AgencyService
from .storage import Database

Generate = Callable[[str, str, str, str | None], Awaitable[str]]


def parse_narrative(text: str) -> NarrativeResult:
    """Validate JSON, accepting only a fenced/embedded JSON object as repair."""
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.IGNORECASE)
    try:
        return NarrativeResult.model_validate_json(candidate)
    except (ValidationError, ValueError):
        decoder = json.JSONDecoder()
        start = candidate.find("{")
        if start >= 0:
            parsed, _ = decoder.raw_decode(candidate[start:])
            return NarrativeResult.model_validate(parsed)
        raise


class ContextBuilder:
    def __init__(self, db: Database, config: dict):
        self.db = db
        self.config = config

    async def build(self, event: StoryEvent) -> dict:
        story = await self.db.one("SELECT * FROM stories WHERE id=?", (event.story_id,))
        scene = await self.db.one("SELECT * FROM scenes WHERE story_id=?", (event.story_id,))
        participant = await self.db.one("SELECT * FROM participants WHERE id=?", (event.participant_id,)) if event.participant_id else None
        relationship = await self.db.one("SELECT * FROM relationships WHERE story_id=? AND participant_id=?", (event.story_id, event.participant_id)) if event.participant_id else None
        facts = await self.db.all("SELECT scope,content,importance FROM facts WHERE story_id=? AND status='active' AND (participant_id IS NULL OR participant_id=?) ORDER BY importance DESC,created_at DESC LIMIT 30", (event.story_id, event.participant_id))
        intents = await self.db.all("SELECT type,content,due_at FROM intents WHERE story_id=? AND status='pending' AND (participant_id IS NULL OR participant_id=?) ORDER BY due_at LIMIT 20", (event.story_id, event.participant_id))
        overlays = await self.db.all("SELECT scope,content FROM overlays WHERE story_id=? AND status='active' AND (participant_id IS NULL OR participant_id=?) ORDER BY created_at DESC LIMIT 15", (event.story_id, event.participant_id))
        perspective_rows = await self.db.all(
            "SELECT content FROM perspectives WHERE story_id=? AND status='active' AND (participant_id IS NULL OR participant_id=?) ORDER BY created_at DESC LIMIT 30",
            (event.story_id, event.participant_id),
        )
        fragments = {event.content[index:index+2] for index in range(max(0, len(event.content)-1))
                     if not event.content[index:index+2].isspace()}
        perspectives = [row["content"] for row in perspective_rows
                        if fragments and any(fragment in row["content"] for fragment in fragments)][:5]
        alter = await self.db.one("SELECT accumulated_alter,summary FROM alter_states WHERE story_id=?", (event.story_id,))
        participants = await self.db.all("SELECT id,display_name,platform_user_id FROM participants WHERE story_id=? AND enabled=1 LIMIT 20", (event.story_id,))
        last_bot = await self.db.one("SELECT content,occurred_at FROM story_entries WHERE story_id=? AND event_type='CHARACTER_MESSAGE_SENT' ORDER BY occurred_at DESC LIMIT 1", (event.story_id,))
        schedules = await self.db.all("SELECT kind,content,start_at,end_at FROM schedules WHERE story_id=? AND status='active' AND end_at>=? AND start_at<=? ORDER BY start_at LIMIT 20", (event.story_id, now_iso(), (datetime.now(timezone.utc)+timedelta(hours=12)).isoformat()))
        events = await self.db.recent_events(event.story_id, max(50, int(self.config.get("recent_event_min_count", 50))))
        protected_after = (datetime.now(timezone.utc) - timedelta(minutes=int(self.config.get("recent_message_protect_minutes", 60)))).isoformat()
        protected_rows = await self.db.all(
            "SELECT * FROM story_entries WHERE story_id=? AND occurred_at>=? "
            "AND event_type IN ('USER_MESSAGE','USER_MESSAGE_BATCH','CHARACTER_MESSAGE_SENT') "
            "ORDER BY occurred_at",
            (event.story_id, protected_after),
        )
        events = sorted({row["id"]: row for row in [*events, *protected_rows]}.values(),
                        key=lambda row: (row["occurred_at"], row["id"]))
        batch_sources = {
            source_id
            for row in events if row["event_type"] == "USER_MESSAGE_BATCH"
            for source_id in json.loads(row["metadata_json"]).get("source_event_ids", [])
        }
        events = [row for row in events if row["id"] not in batch_sources]
        max_chars = max(2000, int(self.config.get("context_max_chars", 14000)))
        selected = []
        used = 0
        for row in reversed(events):
            item = {"type": row["event_type"], "content": row["content"], "at": row["occurred_at"]}
            if row["event_type"] == "GROUP_MESSAGE":
                meta = json.loads(row["metadata_json"])
                item.update({"speaker": meta.get("speaker"), "mentioned": meta.get("mentioned", False),
                             "reply_to_bot": meta.get("reply_to_bot", False)})
            size = len(row["content"])
            protected = row["occurred_at"] >= protected_after and row["event_type"] in ("USER_MESSAGE", "USER_MESSAGE_BATCH", "CHARACTER_MESSAGE_SENT")
            if used + size <= max_chars or protected:
                selected.append(item)
                used += size
        selected.reverse()
        try:
            local_time = datetime.now(ZoneInfo(str(self.config.get("timezone", "Asia/Shanghai")))).isoformat()
        except ZoneInfoNotFoundError:
            local_time = now_iso()
        return {
            "current_time": local_time, "story_cursor": story["cursor"],
            "current_state": json.loads(story["state_json"]),
            "scene_summary": scene["summary"] if scene else "",
            "participant": dict(participant) if participant else None,
            "relationship": json.loads(relationship["data_json"]) if relationship else {},
            "relationship_summary": relationship["summary"] if relationship else "",
            "facts": [dict(r) for r in facts], "intents": [dict(r) for r in intents],
            "overlays": [dict(r) for r in overlays], "schedule": [dict(r) for r in schedules],
            "perspectives": perspectives, "alter": dict(alter) if alter else None,
            "participants": [dict(r) for r in participants],
            "last_bot_message": dict(last_bot) if last_bot else None,
            "agency": await AgencyService(self.db).evaluate(event.story_id),
            "recent_events": selected, "current_event": {
                "type": event.event_type.value, "content": event.content,
                "participant_id": event.participant_id, "metadata": event.metadata,
            },
            "omitted_event_count": len(events) - len(selected),
        }


class NarrativeEngine:
    def __init__(self, db: Database, config: dict, generate: Generate, worldbook=None):
        self.db = db
        self.config = config
        self.generate = generate
        self.worldbook = worldbook
        self.builder = ContextBuilder(db, config)

    async def run(self, event: StoryEvent, mode: str) -> NarrativeResult:
        context = await self.builder.build(event)
        original_scene_summary = context["scene_summary"]
        provider_umo = context["participant"]["umo"] if context["participant"] else None
        if provider_umo is None:
            row = await self.db.one("SELECT umo FROM participants WHERE story_id=? ORDER BY last_interaction_at DESC LIMIT 1", (event.story_id,))
            provider_umo = row["umo"] if row else None
        if self.config.get("compression_enabled", False) and context["omitted_event_count"]:
            old = context["scene_summary"]
            history = await self.db.recent_events(event.story_id, 200)
            selected_ids = {(x["type"], x["content"], x["at"]) for x in context["recent_events"]}
            omitted = [{"type": x["event_type"], "content": x["content"], "at": x["occurred_at"]} for x in history
                       if (x["event_type"], x["content"], x["occurred_at"]) not in selected_ids]
            budget = 0
            bounded = []
            for item in reversed(omitted):
                size = len(item["content"])
                if budget + size > 12000:
                    break
                bounded.append(item)
                budget += size
            omitted = list(reversed(bounded))
            summary_prompt = json.dumps({"previous_summary": old, "omitted_events": omitted}, ensure_ascii=False)
            try:
                summary = await self.generate(str(self.config.get("compression_provider", "")),
                                              "压缩剧情，保留因果、承诺、关系变化、未解决问题、情绪余波和未来事项。只输出摘要。",
                                              summary_prompt, provider_umo)
                if summary.strip():
                    context["scene_summary"] = summary.strip()[:4000]
            except Exception as exc:  # noqa: BLE001 - optional compression must not block a turn
                context["compression_error"] = type(exc).__name__
        character_row = await self.db.one("SELECT data_json FROM characters WHERE id=(SELECT character_id FROM stories WHERE id=?)", (event.story_id,))
        story = await self.db.one("SELECT world_json FROM stories WHERE id=?", (event.story_id,))
        system = (
            "你是持续生活叙事引擎。静态角色设定：" + character_row["data_json"] +
            "\n世界设定：" + story["world_json"] +
            "\n只输出符合给定 JSON Schema 的 JSON。故事条目仅写已经发生的生活事件；"
            "角色拟发送内容只能写在 interaction.reply.messages 或 intents，未实际发送不得写成已说过。"
            "补写 story_cursor 到 current_time 中有意义的变化。允许不回复、不主动联系。"
            "事实与关系变化要保守，不能替用户或角色编造已发生的发送。"
            "群聊仅在自然合适且意愿足够时发言；图片观察只是本轮所见，不要保存图片数据。"
            "Agency 表示现实条件；如设备不在身边、正在忙或缺乏隐私，可以延迟或不回复。"
        )
        if self.worldbook and self.config.get("worldbook_enabled", False):
            system = await self.worldbook.apply(event, context["participant"], system)
        prompt = json.dumps({"mode": mode, "context": context, "schema": NarrativeResult.model_json_schema()}, ensure_ascii=False)
        original_prompt = prompt
        provider = str(self.config.get("narrative_provider", ""))
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                raw = await self.generate(provider, system, prompt, provider_umo)
                if not raw.strip():
                    raise ValueError("empty_response")
                result = parse_narrative(raw)
                if context["scene_summary"] != original_scene_summary:
                    if result.state_update is None:
                        result.state_update = StateUpdate(scene_summary=context["scene_summary"])
                    elif not result.state_update.scene_summary:
                        result.state_update.scene_summary = context["scene_summary"]
                return result
            except (ValueError, ValidationError, json.JSONDecodeError) as exc:
                last_error = exc
                prompt = json.dumps({"repair": "Return a complete object matching the schema, no markdown", "invalid_output": raw if 'raw' in locals() else "", "schema": NarrativeResult.model_json_schema()}, ensure_ascii=False)
            except Exception as exc:  # noqa: BLE001 - provider errors and timeouts are retried
                last_error = exc
                prompt = original_prompt
        raise ValueError("Narrative validation failed") from last_error
