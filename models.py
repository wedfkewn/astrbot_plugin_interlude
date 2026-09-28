"""Typed canonical story and narrative interchange models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return uuid4().hex


class EventType(StrEnum):
    USER_MESSAGE = "USER_MESSAGE"
    USER_MESSAGE_BATCH = "USER_MESSAGE_BATCH"
    USER_IMAGE = "USER_IMAGE"
    GROUP_MESSAGE = "GROUP_MESSAGE"
    CHARACTER_MESSAGE_SENT = "CHARACTER_MESSAGE_SENT"
    AUTO_ADVANCE = "AUTO_ADVANCE"
    INTENT_DUE = "INTENT_DUE"
    SCHEDULE_BOUNDARY = "SCHEDULE_BOUNDARY"
    FOLLOW_UP = "FOLLOW_UP"
    SYSTEM_EVENT = "SYSTEM_EVENT"


@dataclass(slots=True)
class Character:
    id: str
    name: str
    profile: str = ""
    personality: str = ""
    background: str = ""
    speaking_style: str = ""
    habits: str = ""
    preferences: str = ""
    boundaries: str = ""
    default_schedule: str = ""
    timezone: str = "Asia/Shanghai"


@dataclass(slots=True)
class World:
    world_description: str = ""
    locations: list[str] = field(default_factory=list)
    supporting_characters: list[str] = field(default_factory=list)
    stable_rules: list[str] = field(default_factory=list)
    current_world_state: str = ""


@dataclass(slots=True)
class Participant:
    id: str
    story_id: str
    platform: str
    platform_user_id: str
    unified_msg_origin: str
    display_name: str = ""
    background: str = ""
    character_view: str = ""
    initial_relationship: str = ""
    current_relationship: str = ""
    last_interaction_at: str | None = None
    enabled: bool = True


@dataclass(slots=True)
class StoryEvent:
    story_id: str
    event_type: EventType
    content: str
    participant_id: str | None = None
    id: str = field(default_factory=new_id)
    created_at: str = field(default_factory=now_iso)
    metadata: dict = field(default_factory=dict)


@dataclass(slots=True)
class ActiveScene:
    story_id: str
    scene_summary: str = ""
    started_at: str = field(default_factory=now_iso)
    last_activity_at: str = field(default_factory=now_iso)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StoryBeat(StrictModel):
    content: str = Field(min_length=1, max_length=2000)
    occurred_at: str | None = None


class ReplyDecision(StrictModel):
    mode: Literal["immediate", "delayed", "none"] = "none"
    messages: list[str] = Field(default_factory=list, max_length=8)
    send_at: str | None = None


class Interaction(StrictModel):
    seen: bool = True
    reply: ReplyDecision = Field(default_factory=ReplyDecision)
    willingness_to_speak: float = Field(default=1, ge=0, le=1)


class FactCandidate(StrictModel):
    content: str = Field(min_length=1, max_length=1000)
    scope: Literal["recent_facts", "long_term_facts", "open_loops", "completed_events",
                   "commitments", "relationship_memories", "world_facts"] = "recent_facts"
    importance: float = Field(default=0.5, ge=0, le=1)
    participant_id: str | None = None


class IntentCandidate(StrictModel):
    type: Literal["delayed_reply", "proactive", "follow_up", "reminder", "life_advance"]
    content: str = Field(min_length=1, max_length=2000)
    due_at: str
    participant_id: str | None = None
    willingness: float = Field(default=0.5, ge=0, le=1)
    reason: str = ""

    @field_validator("due_at")
    @classmethod
    def valid_due_at(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("due_at must include timezone")
        return parsed.astimezone(timezone.utc).isoformat()


class RelationshipChange(StrictModel):
    participant_id: str
    dimension: Literal["closeness", "trust", "comfort", "tension", "dependency", "familiarity"]
    delta: float = Field(ge=-1, le=1)
    summary: str = ""


class OverlayCandidate(StrictModel):
    scope: Literal["character", "relationship", "world", "perspective"]
    content: str = Field(min_length=1, max_length=1000)
    participant_id: str | None = None


class StateUpdate(StrictModel):
    location: str = Field(default="", max_length=200)
    activity: str = Field(default="", max_length=300)
    emotion: str = Field(default="", max_length=40)
    emotion_intensity: int = Field(default=0, ge=0, le=5)
    emotion_reason: str = Field(default="", max_length=200)
    privacy: str = Field(default="", max_length=200)
    device_available: bool = True
    nearby_people: list[str] = Field(default_factory=list, max_length=12)
    world_state: str = Field(default="", max_length=1000)
    scene_summary: str = Field(default="", max_length=2000)


class NarrativeResult(StrictModel):
    story: list[StoryBeat] = Field(default_factory=list, max_length=12)
    interaction: Interaction = Field(default_factory=Interaction)
    facts: list[FactCandidate] = Field(default_factory=list, max_length=12)
    relationship_changes: list[RelationshipChange] = Field(default_factory=list, max_length=12)
    overlay_candidates: list[OverlayCandidate] = Field(default_factory=list, max_length=8)
    intents: list[IntentCandidate] = Field(default_factory=list, max_length=12)
    aftermath: list[StoryBeat] = Field(default_factory=list, max_length=8)
    alter: int = Field(default=0, ge=-5, le=5)
    state_update: StateUpdate | None = None


RELATION_DIMENSIONS = (
    "closeness", "trust", "comfort", "tension", "dependency", "familiarity"
)
