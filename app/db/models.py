"""
FruitcakeAI v5 — SQLAlchemy ORM models
Ported from v4, simplified, + persona/scope fields for v5 agent context.
Phase 4: Memory, Task, DeviceToken models added.
"""

import json
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func, text

from app.db.session import Base
from app.config import settings

# pgvector is only available when connected to PostgreSQL
_USE_PGVECTOR = settings.database_url.startswith("postgresql")
if _USE_PGVECTOR:
    from pgvector.sqlalchemy import Vector


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, index=True, nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    full_name = Column(String(255))

    # Role: admin | parent | restricted | guest
    role = Column(String(50), default=settings.default_user_role, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)

    # v5: persona + access scopes (stored as JSON strings)
    persona = Column(String(100), default="family_assistant")
    chat_routing_preference = Column(
        String(20),
        default="auto",
        server_default="auto",
        nullable=False,
    )
    _library_scopes = Column("library_scopes", Text, default='["family_docs"]')
    _calendar_access = Column("calendar_access", Text, default='[]')

    # Phase 4: active hours for task scheduling
    # Resolution order: task fields → user fields → heartbeat.yaml defaults
    active_hours_start = Column(String(5))   # "HH:MM" in user's local tz, e.g. "07:00"
    active_hours_end = Column(String(5))     # "HH:MM"
    active_hours_tz = Column(String(50))     # IANA tz string, e.g. "America/Chicago"

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    last_login = Column(DateTime(timezone=True))

    # Relationships
    documents = relationship("Document", back_populates="owner", cascade="all, delete-orphan")
    chat_sessions = relationship("ChatSession", back_populates="user", cascade="all, delete-orphan")
    audit_logs = relationship("AuditLog", back_populates="user", cascade="all, delete-orphan")
    tasks = relationship("Task", back_populates="user", cascade="all, delete-orphan")
    device_tokens = relationship("DeviceToken", back_populates="user", cascade="all, delete-orphan")
    memories = relationship("Memory", back_populates="user", cascade="all, delete-orphan")
    memory_proposals = relationship("MemoryProposal", back_populates="user", foreign_keys="MemoryProposal.user_id", cascade="all, delete-orphan")
    resolved_memory_proposals = relationship(
        "MemoryProposal",
        back_populates="resolver",
        foreign_keys="MemoryProposal.resolved_by_user_id",
    )
    memory_entities = relationship("MemoryEntity", back_populates="user", cascade="all, delete-orphan")
    memory_relations = relationship("MemoryRelation", back_populates="user", cascade="all, delete-orphan")
    memory_observations = relationship("MemoryObservation", back_populates="user", cascade="all, delete-orphan")
    webhook_configs = relationship("WebhookConfig", back_populates="user", cascade="all, delete-orphan")
    rss_sources = relationship("RSSSource", back_populates="user", cascade="all, delete-orphan")
    rss_user_state = relationship("RSSUserState", back_populates="user", uselist=False, cascade="all, delete-orphan")
    rss_source_candidates = relationship(
        "RSSSourceCandidate",
        back_populates="user",
        cascade="all, delete-orphan",
        foreign_keys="RSSSourceCandidate.user_id",
    )
    reviewed_rss_source_candidates = relationship(
        "RSSSourceCandidate",
        back_populates="reviewer",
        foreign_keys="RSSSourceCandidate.reviewed_by",
    )
    personal_skills = relationship(
        "Skill",
        back_populates="personal_user",
        foreign_keys="Skill.personal_user_id",
    )
    installed_skills = relationship(
        "Skill",
        back_populates="installer",
        foreign_keys="Skill.installed_by",
    )

    @property
    def library_scopes(self) -> list[str]:
        return json.loads(self._library_scopes or "[]")

    @library_scopes.setter
    def library_scopes(self, value: list[str]):
        self._library_scopes = json.dumps(value)

    @property
    def calendar_access(self) -> list[str]:
        return json.loads(self._calendar_access or "[]")

    @calendar_access.setter
    def calendar_access(self, value: list[str]):
        self._calendar_access = json.dumps(value)

    def __repr__(self):
        return f"<User(username='{self.username}', role='{self.role}')>"


class Document(Base):
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True, index=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    filename = Column(String(255), nullable=False)
    original_filename = Column(String(255), nullable=False)
    file_path = Column(Text, nullable=False)
    file_size_bytes = Column(Integer, nullable=False)
    file_hash = Column(String(64), index=True)

    mime_type = Column(String(100))
    title = Column(String(500))
    content = Column(Text)
    summary = Column(Text)
    content_type = Column(String(50), nullable=True)
    extraction_method = Column(String(50), nullable=True)
    extracted_text_length = Column(Integer, nullable=True)
    chunk_count = Column(Integer, nullable=True)

    # v5: scope controls visibility — personal | family | shared
    scope = Column(String(50), default="personal", nullable=False)
    tags = Column(Text)  # JSON array

    processing_status = Column(String(50), default="pending")
    error_message = Column(Text)
    processing_started_at = Column(DateTime(timezone=True), nullable=True)
    processing_completed_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    owner = relationship("User", back_populates="documents")
    chunks = relationship("DocumentChunk", back_populates="document", cascade="all, delete-orphan")
    ingest_job = relationship(
        "DocumentIngestJob",
        back_populates="document",
        uselist=False,
        cascade="all, delete-orphan",
    )

    def __repr__(self):
        return f"<Document(filename='{self.filename}', scope='{self.scope}')>"


class DocumentIngestJob(Base):
    __tablename__ = "document_ingest_jobs"
    __table_args__ = (
        UniqueConstraint("document_id", name="uq_document_ingest_jobs_document_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    document_id = Column(Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)

    status = Column(String(30), nullable=False, default="queued", index=True)
    attempt_count = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=3)
    last_error = Column(Text)

    queued_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    started_at = Column(DateTime(timezone=True))
    finished_at = Column(DateTime(timezone=True))
    next_attempt_at = Column(DateTime(timezone=True))
    claimed_at = Column(DateTime(timezone=True))

    document = relationship("Document", back_populates="ingest_job")

    def __repr__(self):
        return (
            f"<DocumentIngestJob(document_id={self.document_id}, "
            f"status='{self.status}', attempts={self.attempt_count})>"
        )


class DocumentChunk(Base):
    __tablename__ = "document_chunks"

    id = Column(Integer, primary_key=True, index=True)
    document_id = Column(Integer, ForeignKey("documents.id"), nullable=False)

    chunk_index = Column(Integer, nullable=False)
    content = Column(Text, nullable=False)
    content_length = Column(Integer, nullable=False)
    start_char = Column(Integer)
    end_char = Column(Integer)
    page_number = Column(Integer)

    if _USE_PGVECTOR:
        embedding = Column(Vector(settings.embedding_dimension))

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    document = relationship("Document", back_populates="chunks")

    def __repr__(self):
        return f"<DocumentChunk(doc_id={self.document_id}, idx={self.chunk_index})>"


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    title = Column(String(255))
    is_active = Column(Boolean, default=True)

    # Snapshot the persona/model at session creation time
    persona = Column(String(100), default="family_assistant")
    llm_model = Column(String(100))

    # Phase 4: task sessions are hidden from the chat UI session list
    is_task_session = Column(Boolean, default=False, nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    user = relationship("User", back_populates="chat_sessions")
    messages = relationship("ChatMessage", back_populates="session", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<ChatSession(id={self.id})>"


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("chat_sessions.id"), nullable=False)

    role = Column(String(20), nullable=False)  # user | assistant | tool
    content = Column(Text, nullable=False)
    tool_calls = Column(Text)   # JSON — tool calls made by the assistant
    tool_results = Column(Text) # JSON — tool results returned

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    session = relationship("ChatSession", back_populates="messages")

    def __repr__(self):
        return f"<ChatMessage(session_id={self.session_id}, role='{self.role}')>"


class AuditLog(Base):
    """Every agent tool call is logged here for admin review."""
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    tool = Column(String(100), nullable=False)
    arguments = Column(Text)  # JSON
    result_summary = Column(Text)
    session_id = Column(Integer, ForeignKey("chat_sessions.id", ondelete="SET NULL"))

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="audit_logs")

    def __repr__(self):
        return f"<AuditLog(user_id={self.user_id}, tool='{self.tool}')>"


class LLMUsageEvent(Base):
    """Per-call LLM usage accounting for chats, tasks, planners, and tools."""
    __tablename__ = "llm_usage_events"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    session_id = Column(Integer, ForeignKey("chat_sessions.id", ondelete="SET NULL"), index=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="SET NULL"), index=True)
    task_run_id = Column(Integer, ForeignKey("task_runs.id", ondelete="SET NULL"), index=True)

    source = Column(String(50), nullable=False, index=True)
    stage = Column(String(80), nullable=True, index=True)
    model = Column(String(120), nullable=False)
    provider = Column(String(50), nullable=True)

    prompt_tokens = Column(Integer, nullable=False, default=0)
    completion_tokens = Column(Integer, nullable=False, default=0)
    total_tokens = Column(Integer, nullable=False, default=0)
    estimated_cost_usd = Column(Float, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)

    def __repr__(self):
        return f"<LLMUsageEvent(user_id={self.user_id}, source='{self.source}', model='{self.model}')>"


# ---------------------------------------------------------------------------
# Phase 4 models
# ---------------------------------------------------------------------------

class Memory(Base):
    """
    Per-user persistent memory. The agent writes memories via the
    create_memory tool; MemoryService retrieves them in 3 tiers before
    each task/heartbeat run.

    Memory is immutable — never edited, only deactivated + replaced.
    This preserves a full audit trail of what the agent has learned.
    """
    __tablename__ = "memories"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    # "episodic" | "semantic" | "procedural"
    memory_type = Column(String(20), nullable=False)

    content = Column(Text, nullable=False)

    # pgvector embedding for Tier 3 semantic retrieval
    if _USE_PGVECTOR:
        embedding = Column(Vector(settings.embedding_dimension))

    # Importance 0.0–1.0. Set at write-time or by explicit update flows.
    importance = Column(Float, default=0.5, nullable=False)

    # Tracks how many times this memory has been retrieved.
    # Used for usage analytics and optional pruning heuristics.
    access_count = Column(Integer, default=0, nullable=False)
    last_accessed_at = Column(DateTime(timezone=True), nullable=True)

    # Optional JSON array of string tags for filtering/grouping
    tags = Column(Text, default="[]")

    # Soft-delete: deactivate instead of DELETE to preserve audit trail
    is_active = Column(Boolean, default=True, nullable=False)

    # For episodic memories that should expire (e.g. "family visiting this weekend")
    expires_at = Column(DateTime(timezone=True))

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    user = relationship("User", back_populates="memories")

    @property
    def tags_list(self) -> list[str]:
        return json.loads(self.tags or "[]")

    @tags_list.setter
    def tags_list(self, value: list[str]):
        self.tags = json.dumps(value)

    def __repr__(self):
        return f"<Memory(user_id={self.user_id}, type='{self.memory_type}', importance={self.importance})>"


class MemoryProposal(Base):
    __tablename__ = "memory_proposals"

    id = Column(Integer, primary_key=True, index=True)
    proposal_key = Column(String(64), nullable=False, unique=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    proposal_type = Column(String(50), nullable=False, index=True)
    source_type = Column(String(50), nullable=False, index=True)
    status = Column(String(20), nullable=False, default="pending", index=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True)
    task_run_id = Column(Integer, ForeignKey("task_runs.id", ondelete="SET NULL"), nullable=True, index=True)
    content = Column(Text, nullable=False)
    proposal_json = Column(Text, default="{}", nullable=False)
    confidence = Column(Float, default=0.5, nullable=False)
    reason = Column(Text)
    approved_memory_id = Column(Integer, ForeignKey("memories.id", ondelete="SET NULL"), nullable=True, index=True)
    resolved_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    resolved_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    user = relationship("User", back_populates="memory_proposals", foreign_keys=[user_id])
    resolver = relationship("User", back_populates="resolved_memory_proposals", foreign_keys=[resolved_by_user_id])
    task = relationship("Task", foreign_keys=[task_id])
    task_run = relationship("TaskRun", foreign_keys=[task_run_id])
    approved_memory = relationship("Memory", foreign_keys=[approved_memory_id])

    @property
    def proposal_payload(self) -> dict:
        try:
            value = json.loads(self.proposal_json or "{}")
        except Exception:
            return {}
        return value if isinstance(value, dict) else {}

    @proposal_payload.setter
    def proposal_payload(self, value: dict):
        self.proposal_json = json.dumps(value or {})

    def __repr__(self):
        return f"<MemoryProposal(user_id={self.user_id}, type='{self.proposal_type}', status='{self.status}')>"


class MemoryEntity(Base):
    __tablename__ = "memory_entities"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    normalized_name = Column(String(255), nullable=False, index=True)
    entity_type = Column(String(100), nullable=False, default="unknown")
    aliases = Column(Text, default="[]", nullable=False)
    confidence = Column(Float, default=0.5, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    user = relationship("User", back_populates="memory_entities")
    outgoing_relations = relationship(
        "MemoryRelation",
        back_populates="from_entity",
        cascade="all, delete-orphan",
        foreign_keys="MemoryRelation.from_entity_id",
    )
    incoming_relations = relationship(
        "MemoryRelation",
        back_populates="to_entity",
        cascade="all, delete-orphan",
        foreign_keys="MemoryRelation.to_entity_id",
    )
    observations = relationship("MemoryObservation", back_populates="entity", cascade="all, delete-orphan")

    @property
    def aliases_list(self) -> list[str]:
        return json.loads(self.aliases or "[]")

    @aliases_list.setter
    def aliases_list(self, value: list[str]):
        self.aliases = json.dumps(value)


class MemoryRelation(Base):
    __tablename__ = "memory_relations"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    from_entity_id = Column(Integer, ForeignKey("memory_entities.id"), nullable=False, index=True)
    to_entity_id = Column(Integer, ForeignKey("memory_entities.id"), nullable=False, index=True)
    relation_type = Column(String(100), nullable=False)
    confidence = Column(Float, default=0.5, nullable=False)
    source_memory_id = Column(Integer, ForeignKey("memories.id"), nullable=True, index=True)
    source_session_id = Column(Integer, ForeignKey("chat_sessions.id"), nullable=True, index=True)
    source_task_id = Column(Integer, ForeignKey("tasks.id"), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="memory_relations")
    from_entity = relationship("MemoryEntity", back_populates="outgoing_relations", foreign_keys=[from_entity_id])
    to_entity = relationship("MemoryEntity", back_populates="incoming_relations", foreign_keys=[to_entity_id])
    source_memory = relationship("Memory", foreign_keys=[source_memory_id])
    source_session = relationship("ChatSession", foreign_keys=[source_session_id])
    source_task = relationship("Task", foreign_keys=[source_task_id])


class MemoryObservation(Base):
    __tablename__ = "memory_observations"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    entity_id = Column(Integer, ForeignKey("memory_entities.id"), nullable=False, index=True)
    content = Column(Text, nullable=True)
    observed_at = Column(DateTime(timezone=True), nullable=True)
    confidence = Column(Float, default=0.5, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    source_memory_id = Column(Integer, ForeignKey("memories.id"), nullable=True, index=True)
    source_session_id = Column(Integer, ForeignKey("chat_sessions.id"), nullable=True, index=True)
    source_task_id = Column(Integer, ForeignKey("tasks.id"), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="memory_observations")
    entity = relationship("MemoryEntity", back_populates="observations")
    source_memory = relationship("Memory", foreign_keys=[source_memory_id])
    source_session = relationship("ChatSession", foreign_keys=[source_session_id])
    source_task = relationship("Task", foreign_keys=[source_task_id])


class Task(Base):
    """
    Scheduled autonomous task. The agent executes the instruction in an
    isolated session at next_run_at, then pushes the result if deliver=True.
    """
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    title = Column(String(255), nullable=False)
    instruction = Column(Text, nullable=False)  # natural language prompt for the agent
    # Optional per-task persona override/resolution target.
    persona = Column(String(100), nullable=True)
    # Optional task execution profile (default, rss_newspaper)
    profile = Column(String(50), nullable=True)

    # "one_shot" | "recurring"
    task_type = Column(String(20), nullable=False, default="one_shot")

    # "pending" | "running" | "completed" | "failed" | "cancelled" | "waiting_approval"
    status = Column(String(30), default="pending", nullable=False)

    # Schedule expression. One of:
    #   "every:30m"   — interval shorthand (supports s, m, h, d)
    #   cron expr     — "0 7 * * *"
    #   ISO timestamp — "2026-03-10T09:00:00"  (one_shot)
    schedule = Column(String(100))

    # Push notification when the agent finishes
    deliver = Column(Boolean, default=True, nullable=False)

    # If True, the runner pauses before APPROVAL_REQUIRED_TOOLS and pushes an
    # approval request to the user's device before proceeding.
    requires_approval = Column(Boolean, default=False, nullable=False)

    # Set to True by PATCH /tasks/{id} {"approved": true} so the runner knows
    # to skip the approval gate on the next execution without overloading error.
    pre_approved = Column(Boolean, default=False, nullable=False)
    current_step_index = Column(Integer)
    has_plan = Column(Boolean, default=False, nullable=False)
    plan_version = Column(Integer, default=1, nullable=False)

    # Captured last agent output text
    result = Column(Text)
    error = Column(Text)

    # Per-task active hours (overrides user-level, which overrides heartbeat.yaml)
    active_hours_start = Column(String(5))
    active_hours_end = Column(String(5))
    active_hours_tz = Column(String(50))

    # Exponential retry tracking
    retry_count = Column(Integer, default=0, nullable=False)
    next_retry_at = Column(DateTime(timezone=True))

    # FK to the isolated ChatSession created for the last run.
    # Used by /admin/task-runs to join AuditLog entries.
    last_session_id = Column(Integer, ForeignKey("chat_sessions.id", ondelete="SET NULL"), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    last_run_at = Column(DateTime(timezone=True))
    next_run_at = Column(DateTime(timezone=True))

    user = relationship("User", back_populates="tasks")
    steps = relationship(
        "TaskStep",
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="TaskStep.step_index",
    )
    runs = relationship(
        "TaskRun",
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="TaskRun.started_at.desc()",
    )

    def __repr__(self):
        return f"<Task(id={self.id}, title='{self.title}', status='{self.status}')>"


class DeviceToken(Base):
    """APNs device token for a user. Used by APNsPusher to deliver notifications."""
    __tablename__ = "device_tokens"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    token = Column(String(255), unique=True, nullable=False, index=True)

    # "sandbox" | "production"
    environment = Column(String(20), default="sandbox", nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="device_tokens")

    def __repr__(self):
        return f"<DeviceToken(user_id={self.user_id}, env='{self.environment}')>"


class TaskStep(Base):
    """Step in a planned task graph. Executed sequentially by TaskRunner."""
    __tablename__ = "task_steps"
    __table_args__ = (
        UniqueConstraint("task_id", "step_index", name="uq_task_steps_task_id_step_index"),
    )

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    step_index = Column(Integer, nullable=False, index=True)

    title = Column(String(255), nullable=False)
    instruction = Column(Text, nullable=False)

    # "pending" | "running" | "waiting_approval" | "succeeded" | "failed" | "skipped"
    status = Column(String(30), default="pending", nullable=False, index=True)
    requires_approval = Column(Boolean, default=False, nullable=False)

    tool_allowlist = Column(Text, default="[]", nullable=False)
    tool_blocklist = Column(Text, default="[]", nullable=False)

    output_summary = Column(Text)
    result = Column(Text)
    error = Column(Text)
    waiting_approval_tool = Column(String(100))

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    task = relationship("Task", back_populates="steps")

    @property
    def tool_allowlist_items(self) -> list[str]:
        return json.loads(self.tool_allowlist or "[]")

    @tool_allowlist_items.setter
    def tool_allowlist_items(self, value: list[str]):
        self.tool_allowlist = json.dumps(value)

    @property
    def tool_blocklist_items(self) -> list[str]:
        return json.loads(self.tool_blocklist or "[]")

    @tool_blocklist_items.setter
    def tool_blocklist_items(self, value: list[str]):
        self.tool_blocklist = json.dumps(value)

    def __repr__(self):
        return f"<TaskStep(task_id={self.task_id}, idx={self.step_index}, status='{self.status}')>"


class TaskRun(Base):
    """Execution record for one task run attempt."""
    __tablename__ = "task_runs"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    session_id = Column(Integer, ForeignKey("chat_sessions.id", ondelete="SET NULL"))

    started_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    finished_at = Column(DateTime(timezone=True))

    # "running" | "completed" | "failed" | "waiting_approval" | "cancelled"
    status = Column(String(30), nullable=False, default="running")
    error = Column(Text)
    summary = Column(Text)

    task = relationship("Task", back_populates="runs")
    artifacts = relationship(
        "TaskRunArtifact",
        back_populates="task_run",
        cascade="all, delete-orphan",
        order_by="TaskRunArtifact.created_at.desc()",
    )

    def __repr__(self):
        return f"<TaskRun(task_id={self.task_id}, status='{self.status}')>"


class TaskRunArtifact(Base):
    """Structured artifacts emitted by a task run (dataset, reports, outputs)."""
    __tablename__ = "task_run_artifacts"

    id = Column(Integer, primary_key=True, index=True)
    task_run_id = Column(Integer, ForeignKey("task_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    artifact_type = Column(String(50), nullable=False, index=True)
    content_json = Column(Text)
    content_text = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    task_run = relationship("TaskRun", back_populates="artifacts")

    def __repr__(self):
        return f"<TaskRunArtifact(task_run_id={self.task_run_id}, type='{self.artifact_type}')>"


# ---------------------------------------------------------------------------
# Phase 5 models
# ---------------------------------------------------------------------------

class WebhookConfig(Base):
    """Inbound webhook configuration for external triggers (GitHub/Zapier/IFTTT)."""
    __tablename__ = "webhook_configs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    name = Column(String(255), nullable=False)
    webhook_key = Column(String(255), unique=True, nullable=False, index=True)
    instruction = Column(Text, nullable=False)
    active = Column(Boolean, default=True, nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="webhook_configs")

    def __repr__(self):
        return f"<WebhookConfig(id={self.id}, user_id={self.user_id}, active={self.active})>"


class Skill(Base):
    """Admin-installed prompt extension. Content is frozen at install time."""
    __tablename__ = "skills"
    __table_args__ = (
        Index(
            "uq_skills_active_shared_slug",
            "slug",
            unique=True,
            sqlite_where=text("personal_user_id IS NULL AND is_active = 1"),
            postgresql_where=text("personal_user_id IS NULL AND is_active = true"),
        ),
        Index(
            "uq_skills_active_personal_user_slug",
            "personal_user_id",
            "slug",
            unique=True,
            sqlite_where=text("personal_user_id IS NOT NULL AND is_active = 1"),
            postgresql_where=text("personal_user_id IS NOT NULL AND is_active = true"),
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    slug = Column(String(100), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=False)
    system_prompt_addition = Column(Text, nullable=False)
    _allowed_tool_additions = Column("allowed_tool_additions", Text, default="[]", nullable=False)
    scope = Column(String(20), default="shared", nullable=False, index=True)
    personal_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    installed_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    source_url = Column(Text)
    content_hash = Column(String(64), nullable=False)
    description_embedding = Column(Text)
    is_active = Column(Boolean, default=True, nullable=False, index=True)
    is_pinned = Column(Boolean, default=False, nullable=False, index=True)
    supersedes_skill_id = Column(Integer, ForeignKey("skills.id", ondelete="SET NULL"), nullable=True, index=True)
    installed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    personal_user = relationship("User", back_populates="personal_skills", foreign_keys=[personal_user_id])
    installer = relationship("User", back_populates="installed_skills", foreign_keys=[installed_by])
    supersedes_skill = relationship("Skill", remote_side=[id], uselist=False)

    @property
    def allowed_tool_additions(self) -> list[str]:
        return json.loads(self._allowed_tool_additions or "[]")

    @allowed_tool_additions.setter
    def allowed_tool_additions(self, value: list[str]):
        self._allowed_tool_additions = json.dumps(value or [])

    @property
    def description_embedding_vector(self) -> list[float] | None:
        if not self.description_embedding:
            return None
        try:
            return json.loads(self.description_embedding)
        except Exception:
            return None

    @description_embedding_vector.setter
    def description_embedding_vector(self, value: list[float] | None):
        self.description_embedding = json.dumps(value) if value is not None else None

    def __repr__(self):
        return f"<Skill(slug='{self.slug}', scope='{self.scope}', active={self.is_active})>"


class RSSSource(Base):
    """Curated RSS/Atom feed source. Global when user_id is null."""
    __tablename__ = "rss_sources"
    __table_args__ = (
        UniqueConstraint("user_id", "url_canonical", name="uq_rss_sources_user_url"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)

    name = Column(String(255), nullable=False)
    url = Column(Text, nullable=False)
    url_canonical = Column(Text, nullable=False, index=True)
    category = Column(String(100), default="news", nullable=False)
    active = Column(Boolean, default=True, nullable=False, index=True)
    trust_level = Column(String(30), default="manual", nullable=False)
    update_interval_minutes = Column(Integer, default=60, nullable=False)
    last_ok_at = Column(DateTime(timezone=True))
    last_error = Column(Text)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    user = relationship("User", back_populates="rss_sources")
    items = relationship("RSSItem", back_populates="source", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<RSSSource(id={self.id}, user_id={self.user_id}, name='{self.name}')>"


class RSSSourceCandidate(Base):
    """Discovered candidate feed awaiting moderation."""
    __tablename__ = "rss_source_candidates"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    seed_url = Column(Text, nullable=False)
    url = Column(Text, nullable=False)
    url_canonical = Column(Text, nullable=False, index=True)
    title_hint = Column(String(255))
    domain = Column(String(255), nullable=False, index=True)
    discovered_via = Column(String(100), default="discover_rss_sources", nullable=False)
    status = Column(String(30), default="pending", nullable=False, index=True)
    reason = Column(Text)
    reviewed_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    reviewed_at = Column(DateTime(timezone=True))

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", foreign_keys=[user_id], back_populates="rss_source_candidates")
    reviewer = relationship(
        "User",
        foreign_keys=[reviewed_by],
        back_populates="reviewed_rss_source_candidates",
    )

    def __repr__(self):
        return f"<RSSSourceCandidate(id={self.id}, user_id={self.user_id}, status='{self.status}')>"


class RSSItem(Base):
    """Cached RSS/Atom item for fast recall/search and headline history."""
    __tablename__ = "rss_items"
    __table_args__ = (
        UniqueConstraint("source_id", "item_uid", name="uq_rss_items_source_uid"),
    )

    id = Column(Integer, primary_key=True, index=True)
    source_id = Column(Integer, ForeignKey("rss_sources.id", ondelete="CASCADE"), nullable=False, index=True)
    item_uid = Column(String(128), nullable=False, index=True)

    title = Column(String(1000), nullable=False)
    link = Column(Text)
    summary = Column(Text)
    published_at = Column(DateTime(timezone=True), index=True)
    first_seen_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_seen_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    fetched_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)

    source = relationship("RSSSource", back_populates="items")

    def __repr__(self):
        return f"<RSSItem(source_id={self.source_id}, title='{self.title[:40]}')>"


class RSSPublishedItem(Base):
    """Per-task publication history for RSS Newspaper freshness control."""
    __tablename__ = "rss_published_items"
    __table_args__ = (
        UniqueConstraint("task_run_id", "rss_item_id", name="uq_rss_published_items_run_item"),
    )

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    task_run_id = Column(Integer, ForeignKey("task_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    rss_item_id = Column(Integer, ForeignKey("rss_items.id", ondelete="CASCADE"), nullable=False, index=True)
    url_canonical = Column(Text, nullable=False, index=True)
    published_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)

    def __repr__(self):
        return f"<RSSPublishedItem(task_id={self.task_id}, rss_item_id={self.rss_item_id})>"


class RSSUserState(Base):
    """Per-user RSS cursor state for incremental listing flows."""
    __tablename__ = "rss_user_state"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    last_list_recent_cursor_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="rss_user_state")

    def __repr__(self):
        return f"<RSSUserState(user_id={self.user_id}, cursor={self.last_list_recent_cursor_at})>"
