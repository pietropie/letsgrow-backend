"""
Camada de abstração sobre provedores de LLM/embeddings.

Objetivo: permitir trocar de provedor (Gemini ↔ Claude ↔ OpenAI) ou de modelo
em runtime — via app.models.ai_config.AIConfig, editável pelo painel admin em
/admin/ai-panel — sem precisar alterar código nem fazer redeploy.

Cada provedor exige sua própria chave de API configurada no ambiente:
    GOOGLE_API_KEY     → provider "gemini"
    ANTHROPIC_API_KEY  → provider "anthropic" (somente chat)
    OPENAI_API_KEY     → provider "openai"

Se a chave do provedor selecionado não estiver configurada, o erro só aparece
na primeira chamada (lazy import + lazy client). Isso é intencional: assim o
backend sobe normalmente mesmo que uma das três chaves esteja ausente — só
falha quando alguém de fato tentar usar aquele provedor.
"""
from app.config import settings

CHAT_PROVIDERS = ("gemini", "anthropic", "openai")
EMBEDDING_PROVIDERS = ("gemini", "openai")  # Anthropic não tem API de embeddings


def build_llm(provider: str, model: str, temperature: float = 0.3):
    """Instancia o client de chat (LangChain) para o provedor informado."""
    provider = (provider or "gemini").lower()

    if provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=model,
            google_api_key=settings.GOOGLE_API_KEY,
            temperature=temperature,
        )

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=model,
            api_key=settings.ANTHROPIC_API_KEY,
            temperature=temperature,
        )

    if provider == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=model,
            api_key=settings.OPENAI_API_KEY,
            temperature=temperature,
        )

    raise ValueError(
        f"Provedor de chat não suportado: {provider!r}. Use um de: {CHAT_PROVIDERS}"
    )


def build_embeddings(provider: str, model: str):
    """Instancia o client de embeddings (LangChain) para o provedor informado."""
    provider = (provider or "gemini").lower()

    if provider == "gemini":
        from langchain_google_genai import GoogleGenerativeAIEmbeddings

        return GoogleGenerativeAIEmbeddings(model=model, google_api_key=settings.GOOGLE_API_KEY)

    if provider == "openai":
        from langchain_openai import OpenAIEmbeddings

        return OpenAIEmbeddings(model=model, api_key=settings.OPENAI_API_KEY)

    if provider == "anthropic":
        raise ValueError(
            "Anthropic não oferece API de embeddings — escolha 'gemini' ou "
            "'openai' como provedor de embedding."
        )

    raise ValueError(
        f"Provedor de embedding não suportado: {provider!r}. Use um de: {EMBEDDING_PROVIDERS}"
    )


# ─── Normalização de chamadas de embedding entre provedores ──────────────────
#
# Cada provedor lida com dimensionalidade de vetor de um jeito diferente:
#   - gemini-embedding-001 aceita `output_dimensionality` por chamada (é o que
#     usamos para forçar 768 dims e casar com a coluna pgvector existente)
#   - text-embedding-004 (descontinuado) e a maioria dos modelos não aceita
#     esse parâmetro — passar gera erro
#   - OpenAI text-embedding-3-* aceita `dimensions` no construtor/chamada
#
# As funções abaixo escondem essa diferença do resto do código (indexer,
# retriever) — quem chama só passa o provider/model/dimensions do AIConfig
# atual e recebe o vetor já no tamanho certo (ou no tamanho nativo do modelo,
# quando o provedor não suporta redução).

def _gemini_supports_output_dimensionality(model: str) -> bool:
    return "gemini-embedding" in (model or "")


async def embed_documents(embeddings, texts: list[str], *, provider: str, model: str, dimensions: int) -> list[list[float]]:
    provider = (provider or "gemini").lower()
    if provider == "gemini" and _gemini_supports_output_dimensionality(model):
        return await embeddings.aembed_documents(texts, output_dimensionality=dimensions)
    if provider == "openai":
        return await embeddings.aembed_documents(texts, dimensions=dimensions)
    return await embeddings.aembed_documents(texts)


async def embed_query(embeddings, text: str, *, provider: str, model: str, dimensions: int) -> list[float]:
    provider = (provider or "gemini").lower()
    if provider == "gemini" and _gemini_supports_output_dimensionality(model):
        return await embeddings.aembed_query(text, output_dimensionality=dimensions)
    if provider == "openai":
        return await embeddings.aembed_query(text, dimensions=dimensions)
    return await embeddings.aembed_query(text)
