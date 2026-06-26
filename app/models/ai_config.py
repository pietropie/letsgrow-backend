import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class AIConfig(Base, TimestampMixin):
    """
    Configuração ativa do(s) provedor(es) de IA, editável em runtime via painel
    admin (/admin/ai-panel) sem precisar de redeploy.

    Tabela de linha única: sempre lemos a primeira linha (a mais antiga). Isso
    evita ambiguidade — se quiserem múltiplos perfis no futuro, dá pra evoluir
    para uma FK "config ativa" em vez de remodelar do zero.

    `provider` define qual client é instanciado (ver app/services/ai_provider.py):
      - "gemini"    → Google Generative AI (chat + embeddings)
      - "anthropic" → Claude (somente chat — Anthropic não tem API de embeddings)
      - "openai"    → OpenAI (chat + embeddings)

    Trocar o `chat_model`/`provider` é seguro a qualquer momento (efeito imediato
    após o cache de ~60s expirar). Já trocar o `embedding_model`/provider de
    embeddings exige reindexar toda a wiki (scripts/index_brain.py --clear),
    pois vetores de modelos/dimensões diferentes não são comparáveis entre si.
    """

    __tablename__ = "ai_config"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    provider: Mapped[str] = mapped_column(String(20), default="gemini", nullable=False)
    chat_model: Mapped[str] = mapped_column(String(100), nullable=False)
    temperature: Mapped[float] = mapped_column(Float, default=0.3, nullable=False)

    embedding_provider: Mapped[str] = mapped_column(String(20), default="gemini", nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(100), nullable=False)
    embedding_dimensions: Mapped[int] = mapped_column(Integer, default=768, nullable=False)

    # Quem fez a última alteração (e-mail/identificador informado no painel) —
    # útil para auditoria simples em caso de incidentes.
    updated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Mensagem personalizada exibida quando o guard bloqueia uma tentativa de
    # prompt injection. Se NULL, usa o texto padrão definido em guard.py.
    injection_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Agendamento de push diário ────────────────────────────────────────────
    daily_push_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    daily_push_hour: Mapped[int] = mapped_column(Integer, default=9, nullable=False)
    daily_push_minute: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    daily_push_last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # JSON string: {"users_processed": N, "pushed": N, "errors": N, ...}
    daily_push_last_stats: Mapped[str | None] = mapped_column(Text, nullable=True)
