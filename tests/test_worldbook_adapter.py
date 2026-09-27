"""WorldBook bridge respects session identity and narrative-only scope."""

import asyncio
from types import SimpleNamespace

from astrbot_plugin_interlude.models import EventType, StoryEvent
from astrbot_plugin_interlude.worldbook_adapter import WorldbookAdapter


def test_worldbook_uses_loaded_instance_and_sender_context():
    class Worldbook:
        def __init__(self):
            self.calls = []

        async def on_llm_request(self, event, req):
            self.calls.append((event.message_str, event.unified_msg_origin,
                               event.get_sender_id(), event.get_sender_name(),
                               event.get_group_id(), event.is_admin()))
            req.system_prompt += "\n## [rule]\nworld"

    plugin = Worldbook()
    context = SimpleNamespace(get_all_stars=lambda: [
        SimpleNamespace(name="astrbot_plugin_worldbook", activated=True, star_cls=plugin),
    ])
    adapter = WorldbookAdapter(context)
    participant = {"umo": "qq:group:123", "platform_user_id": "7", "display_name": "Alice"}
    event = StoryEvent("story", EventType.GROUP_MESSAGE, "keyword", "p1",
                       metadata={"group_id": "123", "is_admin": True})
    result = asyncio.run(adapter.apply(event, participant, "original"))
    assert result == "original\n## [rule]\nworld"
    assert plugin.calls == [("keyword", "qq:group:123", "7", "Alice", "123", True)]
    assert asyncio.run(adapter.apply(StoryEvent("story", EventType.AUTO_ADVANCE, "keyword"),
                                     participant, "original")) == "original"
    assert len(plugin.calls) == 1


def test_missing_or_disabled_worldbook_keeps_original_prompt():
    event = StoryEvent("story", EventType.USER_MESSAGE, "keyword")
    participant = {"umo": "qq:private:7", "platform_user_id": "7"}
    for stars in ([], [SimpleNamespace(name="astrbot_plugin_worldbook",
                                       activated=False, star_cls=None)]):
        context = SimpleNamespace(get_all_stars=lambda: stars)
        assert asyncio.run(WorldbookAdapter(context).apply(event, participant, "original")) == "original"
