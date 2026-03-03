"""
FruitcakeAI v5 — SQLAlchemy ORM models
Ported from v4, simplified, + persona/scope fields for v5 agent context.
"""

import json
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

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

    # Role: admin | parent | child | guest
    role = Column(String(50), default=settings.default_user_role, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)

    # v5: persona + access scopes (stored as JSON strings)
    persona = Column(String(100), default="family_assistant")
    _library_scopes = Column("library_scopes", Text, default='["family_docs"]')
    _calendar_access = Column("calendar_access", Text, default='[]')

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    last_login = Column(DateTime(timezone=True))

    # Relationships
    documents = relationship("Document", back_populates="owner", cascade="all, delete-orphan")
    chat_sessions = relationship("ChatSession", back_populates="user", cascade="all, delete-orphan")
    audit_logs = relationship("AuditLog", back_populates="user", cascade="all, delete-orphan")

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

    # v5: scope controls visibility — personal | family | shared
    scope = Column(String(50), default="personal", nullable=False)
    tags = Column(Text)  # JSON array

    processing_status = Column(String(50), default="pending")
    error_message = Column(Text)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    owner = relationship("User", back_populates="documents")
    chunks = relationship("DocumentChunk", back_populates="document", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Document(filename='{self.filename}', scope='{self.scope}')>"


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
