"""SQLAlchemy Core table definitions for WhatsBot-Lite.

These ``Table`` objects are the single source of truth for the database
schema. They are NOT mapped ORM classes — there is no ``DeclarativeBase``, no
``Session``, no identity map. Repositories import the tables and build
``select()/insert()/update()`` against them directly, preserving the
explicit-SQL feel of the original ``sqlite3`` code while letting SQLAlchemy
handle dialect differences. The Alembic baseline revision is generated from
this metadata.

Timestamps are stored as Unix epoch floats for backwards compatibility with
existing data (column type ``Float``). A future migration could move them to
``TIMESTAMP``; that is intentionally out of scope.
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
)

metadata = MetaData()


config = Table(
    "config",
    metadata,
    Column("key", Text, primary_key=True),
    Column("value", Text, nullable=False),
)


contacts = Table(
    "contacts",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("phone", Text, nullable=False, unique=True),
    Column("name", Text, nullable=False, server_default=""),
    Column("email", Text, nullable=False, server_default=""),
    Column("profession", Text, nullable=False, server_default=""),
    Column("company", Text, nullable=False, server_default=""),
    Column("address", Text, nullable=False, server_default=""),
    Column("ai_enabled", Integer, nullable=False, server_default="1"),
    # When the AI was last switched off (epoch). NULL = unknown/never. The
    # automatic re-enabling counts its wait from here too, so a conversation
    # switched off by hand is not turned back on because its last message is old.
    Column("ai_disabled_at", Float),
    Column("is_group", Integer, nullable=False, server_default="0"),
    Column("group_name", Text, nullable=False, server_default=""),
    Column("is_archived", Integer, nullable=False, server_default="0"),
    Column("archived_by_app", Integer, nullable=False, server_default="0"),
    Column("is_pinned", Integer, nullable=False, server_default="0"),
    Column("can_send", Integer, nullable=False, server_default="1"),
    Column("unread_count", Integer, nullable=False, server_default="0"),
    Column("unread_ai_count", Integer, nullable=False, server_default="0"),
    Column("has_unread_mention", Integer, nullable=False, server_default="0"),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
)
Index("idx_contacts_updated", contacts.c.updated_at)
Index("idx_contacts_archived", contacts.c.is_archived)


observations = Table(
    "observations",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("contact_id", Integer, ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False),
    Column("text", Text, nullable=False),
    Column("created_at", Float, nullable=False),
)
Index("idx_obs_contact", observations.c.contact_id)


messages = Table(
    "messages",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("contact_id", Integer, ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False),
    Column("role", Text, nullable=False),
    Column("content", Text, nullable=False, server_default=""),
    Column("ts", Float, nullable=False),
    Column("media_type", Text),
    Column("media_path", Text),
    Column("status", Text),
    Column("msg_id", Text),
    Column("revoked", Integer, nullable=False, server_default="0"),
    Column("reactions", Text),  # JSON: {emoji: [reactor, ...]}
    Column("reply_to_msg_id", Text),  # GOWA msg_id of the quoted message (reply)
    # NULL = normal. 'discarded' = file not kept (auto-download policy);
    # 'removed' = file deleted later by the file manager.
    Column("media_state", Text),
    # Name the sender gave the file ("Teste.mp4"), for the file manager and for
    # downloading it under that name. NULL = none known: WhatsApp only carries a
    # name for documents, and rows from before this column are read from `content`.
    Column("media_name", Text),
)
Index("idx_msg_contact_ts", messages.c.contact_id, messages.c.ts)
Index("idx_msg_id", messages.c.msg_id)
Index("idx_msg_media_path", messages.c.media_path)


usage = Table(
    "usage",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("contact_id", Integer, ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False),
    Column("call_type", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("prompt_tokens", Integer, nullable=False, server_default="0"),
    Column("completion_tokens", Integer, nullable=False, server_default="0"),
    Column("total_tokens", Integer, nullable=False, server_default="0"),
    Column("cost_usd", Float, nullable=False, server_default="0.0"),
    Column("ts", Float, nullable=False),
    # NULL = not measured (rows from before cache tracking); 0 = measured, no cache hit.
    Column("cached_tokens", Integer, nullable=True),
    Column("saved_usd", Float, nullable=True),
)
Index("idx_usage_contact_ts", usage.c.contact_id, usage.c.ts)
Index("idx_usage_ts", usage.c.ts)


tags = Table(
    "tags",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", Text, nullable=False, unique=True),
    Column("color", Text, nullable=False),
)


contact_tags = Table(
    "contact_tags",
    metadata,
    Column("contact_id", Integer, ForeignKey("contacts.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", Integer, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)
Index("idx_ct_tag", contact_tags.c.tag_id)


unread_msg_ids = Table(
    "unread_msg_ids",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("contact_id", Integer, ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False),
    Column("msg_id", Text, nullable=False),
)
Index("idx_unread_contact", unread_msg_ids.c.contact_id)


executions = Table(
    "executions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("phone", Text, nullable=False),
    Column("trigger_type", Text, nullable=False, server_default="webhook"),
    Column("status", Text, nullable=False, server_default="running"),
    Column("started_at", Float, nullable=False),
    Column("completed_at", Float),
    Column("error", Text),
    # Which DB-driven agent handled this execution + aggregate cost/usage.
    # Populated only when the AI engine (ai_engine_enabled) is active; null/0
    # otherwise so the legacy path is unaffected.
    Column("agent_key", Text),
    Column("total_tokens", Integer, nullable=False, server_default="0"),
    Column("total_cost_usd", Float, nullable=False, server_default="0.0"),
)
Index("idx_exec_started", executions.c.started_at)


execution_steps = Table(
    "execution_steps",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("execution_id", Integer, ForeignKey("executions.id", ondelete="CASCADE"), nullable=False),
    Column("step_type", Text, nullable=False),
    Column("status", Text, nullable=False, server_default="ok"),
    Column("data", Text),
    Column("ts", Float, nullable=False),
)
Index("idx_step_exec", execution_steps.c.execution_id)


plugins = Table(
    "plugins",
    metadata,
    Column("id", Text, primary_key=True),
    Column("version", Text, nullable=False, server_default=""),
    Column("enabled", Integer, nullable=False, server_default="0"),
    Column("installed_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Column("load_error", Text),
    # JSON array of pip specs already installed for this plugin's manifest
    # ``dependencies`` (cache marker: the loader skips pip on boot when this
    # equals the manifest's declared deps).
    Column("installed_deps", Text, nullable=False, server_default="[]"),
)


plugin_migrations = Table(
    "plugin_migrations",
    metadata,
    Column("plugin_id", Text, primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("applied_at", Float, nullable=False),
)


tool_overrides = Table(
    "tool_overrides",
    metadata,
    Column("name", Text, primary_key=True),
    Column("plugin_id", Text),
    Column("enabled", Integer, nullable=False, server_default="1"),
    Column("description", Text),
    Column("display_label", Text),
    Column("updated_at", Float, nullable=False),
)
Index("idx_tool_overrides_plugin", tool_overrides.c.plugin_id)


# --------------------------------------------------------------------------- #
# Built-in Chat / plugin creator
# --------------------------------------------------------------------------- #

chat_projects = Table(
    "chat_projects",
    metadata,
    Column("id", Text, primary_key=True),
    # ``system`` is the built-in WhatsBot-Lite help project; ``plugin`` owns a
    # durable development workspace.  plugin_id stays stable across updates.
    Column("kind", Text, nullable=False, server_default="plugin"),
    Column("plugin_id", Text),
    Column("name", Text, nullable=False),
    Column("workspace_path", Text, nullable=False, server_default=""),
    Column("sort_order", Integer, nullable=False, server_default="0"),
    # Soft deletion hides only the Chat project. Its workspace and any
    # installed plugin remain untouched and can be restored later.
    Column("deleted_at", Float),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
)
Index("idx_chat_projects_updated", chat_projects.c.updated_at)


chat_conversations = Table(
    "chat_conversations",
    metadata,
    Column("id", Text, primary_key=True),
    Column("project_id", Text, ForeignKey("chat_projects.id", ondelete="CASCADE"), nullable=False),
    Column("title", Text, nullable=False, server_default="Nova conversa"),
    Column("model", Text, nullable=False, server_default=""),
    Column("reasoning", Text, nullable=False, server_default=""),
    Column("summary", Text, nullable=False, server_default=""),
    Column("compacted_through_id", Integer),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
)
Index("idx_chat_conversations_project", chat_conversations.c.project_id, chat_conversations.c.updated_at)


chat_messages = Table(
    "chat_messages",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("conversation_id", Text, ForeignKey("chat_conversations.id", ondelete="CASCADE"), nullable=False),
    Column("role", Text, nullable=False),
    Column("content", Text, nullable=False, server_default=""),
    # message | action | system.  Action metadata holds the real tool event.
    Column("kind", Text, nullable=False, server_default="message"),
    Column("metadata", Text, nullable=False, server_default="{}"),
    Column("created_at", Float, nullable=False),
)
Index("idx_chat_messages_conversation", chat_messages.c.conversation_id, chat_messages.c.id)


# --------------------------------------------------------------------------- #
# AI engine — config-in-DB + code-in-DB (prefix ``ai_``)
# --------------------------------------------------------------------------- #
# These tables move the agent's prompt/model/tools out of code and into the DB,
# so behaviour can change without a deploy. All JSON-shaped values are stored as
# JSON-encoded TEXT (portable across SQLite/Postgres), mirroring ``config``.

ai_agents = Table(
    "ai_agents",
    metadata,
    # ``agent_key`` is identity — never rename (breaks executions.agent_key).
    Column("agent_key", Text, primary_key=True),
    Column("display_name", Text, nullable=False, server_default=""),
    Column("prompt_key", Text, nullable=False, server_default=""),
    # JSON object: {model, temperature, top_p, max_tokens, ...}
    Column("model_config", Text, nullable=False, server_default="{}"),
    # JSON array of tool names, or null/"all" meaning every registered tool.
    Column("tool_names", Text),
    Column("enabled", Integer, nullable=False, server_default="1"),
    Column("version", Integer, nullable=False, server_default="1"),
    Column("updated_at", Float, nullable=False),
)


ai_prompts = Table(
    "ai_prompts",
    metadata,
    # ``prompt_key`` is identity — referenced by ai_agents.prompt_key.
    Column("prompt_key", Text, primary_key=True),
    # Template body with ``{placeholder}`` slots resolved from ai_variables.
    Column("body", Text, nullable=False, server_default=""),
    Column("version", Integer, nullable=False, server_default="1"),
    Column("updated_at", Float, nullable=False),
)


ai_variables = Table(
    "ai_variables",
    metadata,
    # Global values referenceable by prompts via ``{name}``.
    Column("name", Text, primary_key=True),
    Column("value", Text, nullable=False, server_default=""),
    Column("category", Text, nullable=False, server_default=""),
    Column("updated_at", Float, nullable=False),
)


ai_tools = Table(
    "ai_tools",
    metadata,
    # ``name`` is identity (== schema function name; == usage.call_type).
    Column("name", Text, primary_key=True),
    Column("description", Text, nullable=False, server_default=""),
    # Python source materialised to storages/ai_tools/<name>.py and imported.
    Column("code", Text, nullable=False, server_default=""),
    # JSON array of pip specs (e.g. ["httpx>=0.27,<0.28"]).
    Column("dependencies", Text, nullable=False, server_default="[]"),
    Column("enabled", Integer, nullable=False, server_default="1"),
    # pending | installing | ok | failed — gates registration (fail-closed).
    Column("install_status", Text, nullable=False, server_default="pending"),
    Column("install_error", Text),
    # JSON array — the dependency specs last successfully installed (cache
    # marker: pip is skipped on boot when this equals ``dependencies``).
    Column("installed_deps", Text, nullable=False, server_default="[]"),
    Column("version", Integer, nullable=False, server_default="1"),
    Column("updated_at", Float, nullable=False),
)


# History tables — one snapshot row per save (rollback + change trail). The
# ``snapshot`` column holds the full JSON-encoded row as it was after the save.
ai_agents_history = Table(
    "ai_agents_history",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("agent_key", Text, nullable=False),
    Column("version", Integer, nullable=False),
    Column("snapshot", Text, nullable=False),
    Column("created_at", Float, nullable=False),
)
Index("idx_ai_agents_hist", ai_agents_history.c.agent_key, ai_agents_history.c.version)


ai_prompts_history = Table(
    "ai_prompts_history",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("prompt_key", Text, nullable=False),
    Column("version", Integer, nullable=False),
    Column("snapshot", Text, nullable=False),
    Column("created_at", Float, nullable=False),
)
Index("idx_ai_prompts_hist", ai_prompts_history.c.prompt_key, ai_prompts_history.c.version)


ai_tools_history = Table(
    "ai_tools_history",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", Text, nullable=False),
    Column("version", Integer, nullable=False),
    Column("snapshot", Text, nullable=False),
    Column("created_at", Float, nullable=False),
)
Index("idx_ai_tools_hist", ai_tools_history.c.name, ai_tools_history.c.version)


# Set of core table names — used by the SQLite → Postgres migration helper to
# distinguish what belongs to the app vs. plugin-owned tables.
CORE_TABLES = frozenset(t.name for t in metadata.sorted_tables)
