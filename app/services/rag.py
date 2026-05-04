from datetime import date, datetime

from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.grow import Grow
from app.models.knowledge import AIConversation, KnowledgeChunk
from app.models.plant import Plant
from app.models.sensor import SensorDevice, SensorReading
from app.rag.prompts import build_system_prompt, build_grow_context
from app.rag.retriever import retrieve_chunks

_llm: ChatGoogleGenerativeAI | None = None
_embeddings: GoogleGenerativeAIEmbeddings | None = None


def get_llm() -> ChatGoogleGenerativeAI:
    global _llm
    if _llm is None:
        _llm = ChatGoogleGenerativeAI(
            model=settings.GEMINI_MODEL,
            google_api_key=settings.GOOGLE_API_KEY,
            temperature=0.3,
        )
    return _llm


def get_embeddings() -> GoogleGenerativeAIEmbeddings:
    global _embeddings
    if _embeddings is None:
        _embeddings = GoogleGenerativeAIEmbeddings(
            model=settings.EMBEDDING_MODEL,
            google_api_key=settings.GOOGLE_API_KEY,
        )
    return _embeddings


async def chat(
    db: AsyncSession,
    conversation: AIConversation,
    user_message: str,
    grow: Grow | None = None,
) -> str:
    # Build grow context summary (compact — saves tokens)
    grow_ctx = await build_grow_context(db, grow) if grow else ""

    # Retrieve relevant knowledge chunks
    chunks = await retrieve_chunks(db, user_message, grow, top_k=4)
    rag_context = "\n\n---\n\n".join(c.content for c in chunks)

    system_prompt = build_system_prompt(grow_ctx, rag_context)

    # Build message history (last 6 messages to save tokens)
    history = conversation.messages[-6:] if conversation.messages else []
    messages = [("system", system_prompt)]
    for msg in history:
        messages.append((msg["role"], msg["content"]))
    messages.append(("human", user_message))

    llm = get_llm()
    response = await llm.ainvoke(messages)
    return response.content
