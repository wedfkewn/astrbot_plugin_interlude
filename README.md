# Interlude for AstrBot

Interlude is a persistent character runtime for AstrBot 4.24.x–4.x and Python 3.12+. It stores a canonical story separately from AstrBot conversation history. Private messages enter a per-story event router, are merged within a short window, and go through a structured narrative decision. A reply may be immediate, delayed, or absent. The SQLite database survives AstrBot restarts.

> Current release: **1.0.0**. The requested first three development phases are implemented and locally tested with a fake Provider and platform sender. A live AstrBot installation is still needed for platform smoke testing. Fourth-phase features such as embeddings, semantic retrieval, MCP/Agent tool integration, multi-character operation, and export/import are outside this release.

## Architecture

`AstrBot event → MessageDebouncer → EventRouter → ContextBuilder → NarrativeEngine → atomic SQLite commit → DeliveryEngine`. `RuntimeScheduler` polls durable intents and schedule boundaries. Incoming user messages are persisted immediately. Only successful platform sends create `CHARACTER_MESSAGE_SENT` entries. Every segment checks the current generation before sending; a newer user message invalidates unsent parts. The engine uses the current AstrBot chat provider unless `narrative_provider` is specified.

Each private session has its own story by default. Set `shared_story` to place participants into the same story while keeping separate participant and relationship records. A per-story `asyncio.Lock` serializes narrative writes. The SQLite schema is versioned in `metadata.schema_version`.

Group messages can be enabled separately. Ordinary messages become background story events; mentions and replies to the bot enter narrative judgment and must pass the speaking threshold. Images use AstrBot's vision-capable Provider for a transient observation. Memory facts, six-dimensional participant relationships, overlays, relevant perspectives, atmosphere decay, and an agency window provide long-term continuity without changing the original character canon.

## Install

Copy this directory to `AstrBot/data/plugins/astrbot_plugin_interlude`, install `requirements.txt`, then reload the plugin. In the AstrBot WebUI, configure the character, select a chat provider, and set `enabled=true`. Plugin Pages are available in AstrBot 4.24.1 and later; core private-message functionality can run on 4.24.0. Data is stored at `data/plugin_data/astrbot_plugin_interlude/interlude.db` using AstrBot's data path.

## Configuration

The WebUI `_conf_schema.json` groups fields by General, Character, Narrative, Memory, Schedule, Alter, Delivery, Proactive, and Debug prefixes. The most important fields are `character_name`, `character_profile`, `story_id`, `shared_story`, `message_merge_window_seconds`, and `narrative_provider`. A blank provider ID uses the current conversation provider. Proactive contact is off by default and requires `proactive_enabled`, a comma-separated platform user ID in `proactive_whitelist`, sufficient `willingness`, no quiet hours, cooldown clearance, and daily quota. The model cannot bypass these checks.

The scheduler reads pending intents and runtime jobs from SQLite after a restart. It handles delayed replies, proactive contact, follow-up, reminders, schedule boundaries, and narrative intents. `auto_advance_enabled` advances a stale story only at the configured interval and never calls the model every minute. Unsuccessful model output leaves the input event persisted and does not advance the story cursor. Schedule preplanning persists stable, contextual, and granular blocks. Context reads only the upcoming 12 hours.

## Commands and Pages

Administrator commands: `/interlude status`, `timeline`, `context`, `script`, `intents`, `advance`, `pause`, `resume`, `doctor`, `memory`, `memory add <content>`, `memory delete <id> CONFIRM`, `schedule`, `schedule refresh`, `overlay`, `overlay clear CONFIRM`, `reset CONFIRM`, and `purge CONFIRM`. Underscore-style command aliases are also available. Dashboard, Story, Memory, and Schedule Pages provide story selection, refresh, filtering, and detail cards. Destructive actions use typed confirmation and a short-lived one-time challenge.

## Privacy and delivery

The database contains raw user messages and generated facts. Protect and back up the file as private data. Image bytes are not stored; the provider's image observation is transient, while extracted textual facts may be retained. Ordinary logs do not include full prompts, user messages, or secrets. A delivery failure is recorded as `delivery_failed` and never appears as a sent character message. A crash during an in-flight send marks it `uncertain` rather than automatically resending. Some platform adapters may not support proactive sending; AstrBot `send_message` returning false or raising an exception is treated as failure.

## FAQ

- **No reply?** The narrative engine may choose `none`; check `/interlude status`, provider setup, and `/interlude doctor`.
- **Messages arrive late?** Check debounce, typing delay, and pending intents. New input invalidates old unsent replies.
- **No proactive contact?** It is disabled by default. Confirm whitelist, quiet hours, willingness, cooldown, and daily limit.
- **Where is the story?** Use the Story Page or `/interlude timeline`; it is not AstrBot's conversation history.

## Development

Install the plugin requirements plus `pytest` and `ruff` into a Python 3.12 environment. From the workspace root, run `python -m pytest astrbot_plugin_interlude/tests -q` and `python -m ruff check astrbot_plugin_interlude`. The tests use a fake provider and platform sender and cover persistence, delivery, cancellation, migration, schedule boundaries, memory, relationships, and a restart scenario. A live AstrBot 4.24+ smoke test is still required before production use. The plugin uses AstrBot's existing LLM and message APIs; it does not implement external search, weather, or other tools itself.

## License and design reference

This code is licensed under MIT. This project is inspired by the architectural ideas of HDS Interlude / MomoiCore: https://gitee.com/MomoiCore/hds-interlude . It is a clean-room Python implementation; no HDS source code was copied or translated.
