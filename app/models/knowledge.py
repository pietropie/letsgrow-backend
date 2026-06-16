import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)

    embedding: Mapped[Any] = mapped_column(Vector(768), nullable=True)

    source_file: Mapped[str] = mapped_column(String(255), nullable=False)
    source_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    # concept | technique | problem | strain | insumo | user_experience

    phase_relevance: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # e.g. ["all"] or ["veg", "flower"]
    tags: Mapped[list | None] = mapped_column(JSON, nullable=True)
    chunk_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AIConversation(Base):
    __tablename__ = "ai_conversations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    grow_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("grows.id", ondelete="SET NULL"), nullable=True
    )
    plant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plants.id", ondelete="SET NULL"), nullable=True
    )

    title: Mapped[str | None] = mapped_column(String(120), nullable=True)
    messages: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # [{"role": "user"|"assistant", "content": "...", "timestamp": "..."}]

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user = relationship("User", back_populates="conversations")
    grow = relationship("Grow", back_populates="conversations")
