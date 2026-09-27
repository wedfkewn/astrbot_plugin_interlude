"""AstrBot adapters and administrator entry points."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

try:
    from astrbot.api.web import json_response, request
except ImportError:  # AstrBot 4.24.x uses Quart-backed plugin routes.
    from quart import jsonify as json_response
    from quart import request

from .models import EventType, StoryEvent
from .narrative import NarrativeEngine
from .runtime import DeliveryEngine, EventRouter, RuntimeScheduler
from .services import MemoryService, SchedulePlanner, ScheduleService
from .storage import Database
from .worldbook_adapter import WorldbookAdapter

PLUGIN_NAME = "astrbot_plugin_interlude"


class Interlude(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.db = Database(Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME / "interlude.db")
        self.engine = NarrativeEngine(self.db, config, self._generate, WorldbookAdapter(context))
        self.delivery = DeliveryEngine(self.db, config, self._send, self._log)
        self.router = EventRouter(self.db, self.engine, self.delivery, config, self._log)
        self.schedule_planner = SchedulePlanner(self.db, config, self._generate)
        self.scheduler = RuntimeScheduler(self.db, self.router, config, self.schedule_planner.refresh, self._log)
        self.memory = MemoryService(self.db)
        self.schedule = ScheduleService(self.db)
        self.start_lock = asyncio.Lock()
        self.started = False
        self.page_challenges: dict[str, tuple[str, str, str, str | None, float]] = {}
        if hasattr(context, "register_web_api"):
            context.register_web_api(f"/{PLUGIN_NAME}/snapshot", self.page_snapshot, ["GET"], "Interlude snapshot")
            context.register_web_api(f"/{PLUGIN_NAME}/stories", self.page_stories, ["GET"], "Interlude stories")
            context.register_web_api(f"/{PLUGIN_NAME}/challenge", self.page_challenge, ["POST"], "Interlude action challenge")
            context.register_web_api(f"/{PLUGIN_NAME}/action", self.page_action, ["POST"], "Interlude confirmed action")

    def _log(self, phase: str, detail: str) -> None:
        if self.config.get("debug_logging", False) or phase in {"EVENT", "DELIVERY", "SCHEDULER"}:
            logger.info(f"[{phase}] {detail}")

    async def _start(self) -> None:
        async with self.start_lock:
            if self.started:
                return
            await self.db.open()
            uncertain = await self.db.mark_uncertain_deliveries()
            await self.db.recover_processing()
            self.scheduler.start()
            self.started = True
            logger.info(f"[SCHEDULER] Interlude started; uncertain deliveries: {uncertain}")

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        await self._start()

    async def terminate(self):
        if self.started:
            await self.scheduler.close()
            await self.router.close()
            await self.db.close()
            self.started = False

    async def _generate(self, provider_id: str, system: str, prompt: str, umo: str | None) -> str:
        if not provider_id:
            provider_id = await self.context.get_current_chat_provider_id(umo=umo)
        response = await self.context.llm_generate(chat_provider_id=provider_id, system_prompt=system, prompt=prompt)
        return response.completion_text if response else ""

    async def _send(self, umo: str, text: str) -> bool:
        result = await self.context.send_message(umo, MessageChain().message(text))
        return result is True

    async def _observe_images(self, event: AstrMessageEvent, images: list) -> str:
        urls = []
        for image in images[:3]:
            source = image.url or image.file or ""
            if source.startswith(("https://", "http://")):
                urls.append(source)
            else:
                try:
                    urls.append("data:image/jpeg;base64," + await image.convert_to_base64())
                except Exception as exc:  # noqa: BLE001 - one unsupported image must not drop the message
                    logger.warning(f"[EVENT] Image conversion failed: {type(exc).__name__}")
        if not urls:
            return "用户发送了图片，但当前平台无法提供可读取的图像。"
        provider = str(self.config.get("vision_provider", "")) or await self.context.get_current_chat_provider_id(umo=event.unified_msg_origin)
        try:
            response = await self.context.llm_generate(
                chat_provider_id=provider,
                prompt="客观简述用户图片中可见的内容，不猜测身份、不虚构细节。仅供本轮叙事观察。",
                image_urls=urls,
            )
            return (response.completion_text or "").strip()[:1500] if response else "图片内容未识别。"
        except Exception as exc:  # noqa: BLE001 - fall back to an honest observation
            logger.warning(f"[EVENT] Vision observation failed: {type(exc).__name__}")
            return "用户发送了图片，当前无法识别内容。"

    def _story_id(self, umo: str) -> str:
        configured = str(self.config.get("story_id", "default"))
        if self.config.get("shared_story", False):
            return configured
        return configured + ":" + hashlib.sha256(umo.encode()).hexdigest()[:16]

    async def _ensure_story(self, umo: str, user_id: str, name: str) -> tuple[str, str]:
        await self._start()
        story_id = self._story_id(umo)
        character = {
            "id": str(self.config.get("character_id", "interlude")),
            "name": str(self.config.get("character_name", "角色")),
            "profile": str(self.config.get("character_profile", "")),
            "personality": str(self.config.get("character_personality", "")),
            "background": str(self.config.get("character_background", "")),
            "speaking_style": str(self.config.get("character_speaking_style", "")),
            "habits": str(self.config.get("character_habits", "")),
            "preferences": str(self.config.get("character_preferences", "")),
            "boundaries": str(self.config.get("character_boundaries", "")),
            "default_schedule": str(self.config.get("character_default_schedule", "")),
            "timezone": str(self.config.get("timezone", "Asia/Shanghai")),
        }
        await self.db.ensure_story(story_id, character["id"], character, {"world_description": self.config.get("world_description", "")})
        participant_id = await self.db.ensure_participant(story_id, umo.split(":", 1)[0], user_id, umo, name)
        return story_id, participant_id

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def on_private(self, event: AstrMessageEvent):
        if not self.config.get("enabled", False) or (event.message_str or "").startswith("/interlude"):
            return
        images = [part for part in event.get_messages() if isinstance(part, Comp.Image)]
        if not (event.message_str or "").strip() and not images:
            return
        try:
            story_id, participant_id = await self._ensure_story(event.unified_msg_origin, str(event.get_sender_id()), event.get_sender_name() or "")
            if images:
                story_event = StoryEvent(story_id, EventType.USER_IMAGE,
                                         (event.message_str or "") + " [图片]", participant_id,
                                         metadata={"image_count": len(images), "is_admin": event.is_admin()})
                await self.router.ingest(story_event, debounce=False)
                observation = await self._observe_images(event, images)
                story_event.content += "\n本轮图片观察：" + observation
                await self.router.route(story_event)
            else:
                await self.router.ingest(StoryEvent(
                    story_id, EventType.USER_MESSAGE, event.message_str or "", participant_id,
                    metadata={"is_admin": event.is_admin()},
                ))
            event.stop_event()
        except Exception as exc:  # noqa: BLE001 - do not break AstrBot's message dispatcher
            logger.error(f"[EVENT] Interlude ingest failed: {type(exc).__name__}")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group(self, event: AstrMessageEvent):
        if not self.config.get("enabled", False) or not self.config.get("group_enabled", False):
            return
        if (event.message_str or "").startswith("/interlude"):
            return
        parts = event.get_messages()
        own_id = str(event.get_self_id())
        mentioned = any(isinstance(part, Comp.At) and str(getattr(part, "qq", "")) == own_id for part in parts)
        reply_to_bot = any(isinstance(part, Comp.Reply) and str(getattr(part, "sender_id", "")) == own_id for part in parts)
        images = [part for part in parts if isinstance(part, Comp.Image)]
        if not (event.message_str or "").strip() and not images:
            return
        try:
            story_id, participant_id = await self._ensure_story(event.unified_msg_origin, str(event.get_sender_id()), event.get_sender_name() or "")
            recent = await self.db.all(
                "SELECT COUNT(*) AS n FROM story_entries WHERE story_id=? AND event_type='GROUP_MESSAGE' AND occurred_at>=?",
                (story_id, (datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat()),
            )
            metadata = {"is_group": True, "speaker": event.get_sender_name() or str(event.get_sender_id()),
                        "group_id": event.get_group_id(), "is_admin": event.is_admin(),
                        "mentioned": mentioned, "reply_to_bot": reply_to_bot,
                        "conversation_density": recent[0]["n"] if recent else 0}
            story_event = StoryEvent(story_id, EventType.GROUP_MESSAGE,
                                     (event.message_str or "") + (" [图片]" if images else ""),
                                     participant_id, metadata=metadata)
            if mentioned or reply_to_bot:
                await self.router.ingest(story_event, debounce=False)
                if images:
                    story_event.content += "\n本轮图片观察：" + await self._observe_images(event, images)
                await self.router.route(story_event)
                event.stop_event()
            else:
                await self.db.append_event(story_event)
        except Exception as exc:  # noqa: BLE001 - group listener must not break other plugins
            logger.error(f"[EVENT] Group observation failed: {type(exc).__name__}")

    async def page_snapshot(self):
        await self._start()
        query = request.query if hasattr(request, "query") else request.args
        story_id = query.get("story_id", str(self.config.get("story_id", "default")))
        if not self.config.get("shared_story", False) and ":" not in story_id:
            return json_response({"error": "story_id required"})
        story = await self.db.one("SELECT id,cursor,revision,paused,state_json FROM stories WHERE id=?", (story_id,))
        if not story:
            return json_response({"error": "story not found"})
        entries = await self.db.recent_events(story_id, 50)
        facts = await self.db.all("SELECT id,scope,content,status FROM facts WHERE story_id=? ORDER BY created_at DESC LIMIT 50", (story_id,))
        intents = await self.db.all("SELECT id,type,content,due_at,status FROM intents WHERE story_id=? ORDER BY due_at LIMIT 50", (story_id,))
        schedules = await self.db.all("SELECT id,kind,content,start_at,end_at FROM schedules WHERE story_id=? ORDER BY start_at LIMIT 50", (story_id,))
        overlays = await self.db.all("SELECT id,scope,content,status FROM overlays WHERE story_id=? ORDER BY created_at DESC LIMIT 50", (story_id,))
        perspectives = await self.db.all("SELECT id,content,status FROM perspectives WHERE story_id=? ORDER BY created_at DESC LIMIT 50", (story_id,))
        relationships = await self.db.all("SELECT participant_id,summary,data_json FROM relationships WHERE story_id=?", (story_id,))
        jobs = await self.db.all(
            "SELECT id,kind,due_at,status FROM runtime_jobs WHERE story_id=? ORDER BY due_at LIMIT 50", (story_id,)
        )
        scene = await self.db.one("SELECT * FROM scenes WHERE story_id=?", (story_id,))
        return json_response({"story": dict(story), "scene": dict(scene) if scene else None,
                              "current_time": datetime.now(timezone.utc).isoformat(),
                              "scheduler": {"running": bool(self.scheduler.task and not self.scheduler.task.done()),
                                            "last_error": self.scheduler.last_error},
                              "entries": [dict(x) for x in entries], "facts": [dict(x) for x in facts],
                              "intents": [dict(x) for x in intents], "schedules": [dict(x) for x in schedules],
                              "jobs": [dict(x) for x in jobs],
                              "overlays": [dict(x) for x in overlays], "perspectives": [dict(x) for x in perspectives],
                              "relationships": [dict(x) for x in relationships]})

    async def page_stories(self):
        await self._start()
        rows = await self.db.all("SELECT id,cursor,paused FROM stories ORDER BY id LIMIT 100")
        return json_response({"stories": [dict(x) for x in rows]})

    async def page_challenge(self):
        await self._start()
        payload = await request.get_json(silent=True) if hasattr(request, "get_json") else await request.json(default={})
        if not isinstance(payload, dict):
            return json_response({"error": "invalid payload"})
        action = str(payload.get("action", ""))
        story_id = str(payload.get("story_id", ""))
        item_id = str(payload.get("item_id", ""))
        if action not in {"memory_delete", "overlay_clear", "story_reset", "story_purge"} or not story_id:
            return json_response({"error": "invalid action"})
        if not await self.db.one("SELECT id FROM stories WHERE id=?", (story_id,)):
            return json_response({"error": "story not found"})
        token = secrets.token_urlsafe(24)
        self.page_challenges[token] = (action, story_id, item_id, getattr(request, "username", None), time.monotonic()+120)
        return json_response({"token": token, "expires_seconds": 120})

    async def page_action(self):
        await self._start()
        payload = await request.get_json(silent=True) if hasattr(request, "get_json") else await request.json(default={})
        if not isinstance(payload, dict) or payload.get("confirmation") != "CONFIRM":
            return json_response({"error": "confirmation required"})
        token = str(payload.get("token", ""))
        challenge = self.page_challenges.pop(token, None)
        if not challenge or challenge[4] < time.monotonic() or challenge[3] != getattr(request, "username", None):
            return json_response({"error": "challenge expired"})
        action, story_id, item_id, _, _ = challenge
        if action == "memory_delete":
            if not await self.memory.change_status(story_id, item_id, "superseded"):
                return json_response({"error": "fact not found"})
        elif action == "overlay_clear":
            await self.db.execute("UPDATE overlays SET status='superseded' WHERE story_id=?", (story_id,))
            await self.db.execute("UPDATE perspectives SET status='superseded' WHERE story_id=?", (story_id,))
        else:
            await self.db.clear_story(story_id, purge=action == "story_purge")
        return json_response({"ok": True})

    @filter.command_group("interlude")
    def interlude():
        pass

    @interlude.command("status")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def status(self, event: AstrMessageEvent):
        story_id, _ = await self._ensure_story(event.unified_msg_origin, str(event.get_sender_id()), event.get_sender_name() or "")
        row = await self.db.one("SELECT cursor,revision,paused FROM stories WHERE id=?", (story_id,))
        yield event.plain_result(f"Interlude {story_id}: cursor={row['cursor']} revision={row['revision']} paused={bool(row['paused'])}")

    @interlude.command("timeline")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def timeline(self, event: AstrMessageEvent):
        story_id, _ = await self._ensure_story(event.unified_msg_origin, str(event.get_sender_id()), event.get_sender_name() or "")
        rows = await self.db.recent_events(story_id, 10)
        yield event.plain_result("\n".join(f"{r['occurred_at']} {r['event_type']}: {r['content'][:100]}" for r in rows) or "暂无事件")

    @interlude.command("intents")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def intents(self, event: AstrMessageEvent):
        story_id, _ = await self._ensure_story(event.unified_msg_origin, str(event.get_sender_id()), event.get_sender_name() or "")
        rows = await self.db.all("SELECT type,due_at,status,content FROM intents WHERE story_id=? ORDER BY due_at LIMIT 20", (story_id,))
        yield event.plain_result("\n".join(f"{r['due_at']} {r['type']} [{r['status']}] {r['content'][:80]}" for r in rows) or "暂无 Intent")

    @interlude.command("advance")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def advance(self, event: AstrMessageEvent):
        story_id, _ = await self._ensure_story(event.unified_msg_origin, str(event.get_sender_id()), event.get_sender_name() or "")
        await self.router.route(StoryEvent(story_id, EventType.AUTO_ADVANCE, "Manual advance"))
        yield event.plain_result("已尝试推进故事")

    @interlude.command("pause")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def pause(self, event: AstrMessageEvent):
        story_id, _ = await self._ensure_story(event.unified_msg_origin, str(event.get_sender_id()), event.get_sender_name() or "")
        await self.db.execute("UPDATE stories SET paused=1 WHERE id=?", (story_id,))
        yield event.plain_result("已暂停")

    @interlude.command("resume")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def resume(self, event: AstrMessageEvent):
        story_id, _ = await self._ensure_story(event.unified_msg_origin, str(event.get_sender_id()), event.get_sender_name() or "")
        await self.db.execute("UPDATE stories SET paused=0 WHERE id=?", (story_id,))
        yield event.plain_result("已恢复")

    @interlude.command("doctor")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def doctor(self, event: AstrMessageEvent):
        await self._start()
        row = await self.db.one("SELECT value FROM metadata WHERE key='schema_version'")
        failures = await self.db.one("SELECT COUNT(*) AS n FROM deliveries WHERE status IN ('delivery_failed','uncertain')")
        retries = await self.db.one("SELECT COUNT(*) AS n FROM runtime_jobs WHERE status='pending'")
        yield event.plain_result(
            f"DB schema={row['value']}; scheduler={'running' if self.scheduler.task and not self.scheduler.task.done() else 'stopped'}; "
            f"delivery issues={failures['n']}; retries={retries['n']}; last scheduler error={self.scheduler.last_error or 'none'}"
        )

    @interlude.command("context")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def show_context(self, event: AstrMessageEvent):
        story_id, pid = await self._ensure_story(event.unified_msg_origin, str(event.get_sender_id()), event.get_sender_name() or "")
        value = await self.engine.builder.build(StoryEvent(story_id, EventType.SYSTEM_EVENT, "context preview", pid))
        yield event.plain_result(json.dumps(value, ensure_ascii=False, indent=2)[:3500])

    @interlude.command("memory")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def show_memory(self, event: AstrMessageEvent, action: str = "", content: GreedyStr = GreedyStr):
        story_id, pid = await self._ensure_story(event.unified_msg_origin, str(event.get_sender_id()), event.get_sender_name() or "")
        if action == "add":
            if not content.strip():
                yield event.plain_result("用法：/interlude memory add <内容>")
                return
            fact_id = await self.memory.add(story_id, content, participant_id=pid)
            yield event.plain_result(f"已添加记忆 {fact_id[:8]}")
            return
        if action == "delete":
            pieces = content.split()
            if len(pieces) != 2 or pieces[1] != "CONFIRM":
                yield event.plain_result("用法：/interlude memory delete <完整记忆 ID> CONFIRM")
                return
            found = await self.memory.change_status(story_id, pieces[0], "superseded")
            yield event.plain_result("记忆已归档" if found else "未找到该记忆")
            return
        rows = await self.memory.list(story_id, pid)
        yield event.plain_result("\n".join(f"{r['id'][:8]} [{r['scope']}] {r['content'][:100]}" for r in rows[:20]) or "暂无记忆")

    @interlude.command("memory_add")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def memory_add(self, event: AstrMessageEvent, content: str):
        story_id, pid = await self._ensure_story(event.unified_msg_origin, str(event.get_sender_id()), event.get_sender_name() or "")
        fact_id = await self.memory.add(story_id, content, participant_id=pid)
        yield event.plain_result(f"已添加记忆 {fact_id[:8]}")

    @interlude.command("schedule")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def show_schedule(self, event: AstrMessageEvent, action: str = ""):
        story_id, _ = await self._ensure_story(event.unified_msg_origin, str(event.get_sender_id()), event.get_sender_name() or "")
        if action == "refresh":
            count = await self.schedule_planner.refresh(story_id)
            yield event.plain_result(f"已更新 {count} 条日程")
            return
        rows = await self.schedule.upcoming(story_id)
        yield event.plain_result("\n".join(f"{r['start_at']}–{r['end_at']} {r['content']}" for r in rows) or "近期无日程")

    @interlude.command("schedule_refresh")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def schedule_refresh(self, event: AstrMessageEvent):
        story_id, _ = await self._ensure_story(event.unified_msg_origin, str(event.get_sender_id()), event.get_sender_name() or "")
        count = await self.schedule_planner.refresh(story_id)
        yield event.plain_result(f"已更新 {count} 条日程")

    @interlude.command("overlay")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def show_overlay(self, event: AstrMessageEvent, action: str = "", confirmation: str = ""):
        story_id, _ = await self._ensure_story(event.unified_msg_origin, str(event.get_sender_id()), event.get_sender_name() or "")
        if action == "clear":
            if confirmation != "CONFIRM":
                yield event.plain_result("再次输入 /interlude overlay clear CONFIRM")
                return
            await self.db.execute("UPDATE overlays SET status='superseded' WHERE story_id=?", (story_id,))
            await self.db.execute("UPDATE perspectives SET status='superseded' WHERE story_id=?", (story_id,))
            yield event.plain_result("Overlay 已清除")
            return
        rows = await self.db.all("SELECT id,scope,content FROM overlays WHERE story_id=? AND status='active' ORDER BY created_at DESC LIMIT 20", (story_id,))
        yield event.plain_result("\n".join(f"{r['id'][:8]} [{r['scope']}] {r['content'][:100]}" for r in rows) or "暂无 Overlay")

    @interlude.command("overlay_clear")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def overlay_clear(self, event: AstrMessageEvent, confirmation: str = ""):
        if confirmation != "CONFIRM":
            yield event.plain_result("此操作会清除当前故事的演化层。再次输入 /interlude overlay_clear CONFIRM")
            return
        story_id, _ = await self._ensure_story(event.unified_msg_origin, str(event.get_sender_id()), event.get_sender_name() or "")
        await self.db.execute("UPDATE overlays SET status='superseded' WHERE story_id=?", (story_id,))
        await self.db.execute("UPDATE perspectives SET status='superseded' WHERE story_id=?", (story_id,))
        yield event.plain_result("Overlay 已清除")

    @interlude.command("script")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def script(self, event: AstrMessageEvent):
        story_id, _ = await self._ensure_story(event.unified_msg_origin, str(event.get_sender_id()), event.get_sender_name() or "")
        rows = await self.db.recent_events(story_id, 30)
        yield event.plain_result("\n".join(f"[{r['occurred_at']}] {r['event_type']}\n{r['content']}" for r in rows)[:3500] or "暂无剧情")

    @interlude.command("memory_delete")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def memory_delete(self, event: AstrMessageEvent, fact_id: str, confirmation: str = ""):
        if confirmation != "CONFIRM":
            yield event.plain_result(f"确认删除记忆请再次输入 /interlude memory_delete {fact_id} CONFIRM")
            return
        story_id, _ = await self._ensure_story(event.unified_msg_origin, str(event.get_sender_id()), event.get_sender_name() or "")
        found = await self.memory.change_status(story_id, fact_id, "superseded")
        yield event.plain_result("记忆已归档" if found else "未找到该记忆")

    @interlude.command("reset")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def reset(self, event: AstrMessageEvent, confirmation: str = ""):
        if confirmation != "CONFIRM":
            yield event.plain_result("此操作会清空当前故事状态但保留角色与参与者。再次输入 /interlude reset CONFIRM")
            return
        story_id, _ = await self._ensure_story(event.unified_msg_origin, str(event.get_sender_id()), event.get_sender_name() or "")
        await self.db.clear_story(story_id, purge=False)
        yield event.plain_result("故事状态已重置")

    @interlude.command("purge")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def purge(self, event: AstrMessageEvent, confirmation: str = ""):
        if confirmation != "CONFIRM":
            yield event.plain_result("此操作会永久删除当前故事。再次输入 /interlude purge CONFIRM")
            return
        story_id, _ = await self._ensure_story(event.unified_msg_origin, str(event.get_sender_id()), event.get_sender_name() or "")
        await self.db.clear_story(story_id, purge=True)
        yield event.plain_result("故事已删除")
