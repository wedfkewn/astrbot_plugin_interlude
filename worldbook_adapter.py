"""Optional bridge to the loaded astrbot_plugin_worldbook instance."""

from __future__ import annotations

from types import SimpleNamespace

from astrbot.api import logger

from .models import EventType, StoryEvent


class _WorldbookEvent:
    """The message fields consumed by WorldBook's request hook and wildcards."""

    def __init__(self, event: StoryEvent, participant: dict):
        self.message_str = event.content
        self.unified_msg_origin = participant["umo"]
        self._sender_id = str(participant["platform_user_id"])
        self._sender_name = participant.get("display_name") or self._sender_id
        self._group_id = str(event.metadata.get("group_id") or "")
        self._admin = bool(event.metadata.get("is_admin", False))

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_sender_name(self) -> str:
        return self._sender_name

    def get_group_id(self) -> str:
        return self._group_id

    def is_admin(self) -> bool:
        return self._admin


class WorldbookAdapter:
    def __init__(self, context):
        self.context = context

    async def apply(self, event: StoryEvent, participant: dict | None, system: str) -> str:
        if not participant or event.event_type not in {
            EventType.USER_MESSAGE, EventType.USER_MESSAGE_BATCH,
            EventType.USER_IMAGE, EventType.GROUP_MESSAGE,
        }:
            return system
        for plugin in self.context.get_all_stars():
            if plugin.name != "astrbot_plugin_worldbook" or not plugin.activated:
                continue
            instance = plugin.star_cls
            if instance is None or not callable(getattr(instance, "on_llm_request", None)):
                return system
            request = SimpleNamespace(system_prompt=system)
            try:
                await instance.on_llm_request(_WorldbookEvent(event, participant), request)
            except Exception as exc:  # noqa: BLE001 - optional plugin must not stop narrative generation
                logger.warning(f"[WORLDBOOK] Rule evaluation failed: {type(exc).__name__}")
                return system
            return request.system_prompt
        return system
