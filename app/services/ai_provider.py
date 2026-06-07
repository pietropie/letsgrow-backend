"""
Camada de abstração sobre provedores de LLM/embeddings.

Objetivo: permitir trocar de provedor (Gemini ↔ Claude ↔ OpenAI) ou de modelo
em runtime — via app.models.ai_config.AIConfig, editável pelo painel admin em
/admin/ai-panel — sem precisar alterar código nem fazer redeploy.

Cada provedor exige sua própria chave de API configurada no ambiente:
    GOOGLE_API_KEY     → provider "gemini"
    ANTHROPIC_API_KEY  → provider "anthropic" (somente chat)
    OPENAI_API_KEY     → provider "openai"
    DEEPSEEK_API_KEY   → provider "deepseek" (somente chat)
    ZAI_API_KEY        → provider "zai" (Z.ai / GLM — somente chat)

DeepSeek e Z.ai não têm SDK LangChain dedicado, mas ambos expõem uma API REST
compatível com a da OpenAI (chat completions) — então os tratamos com o mesmo
client `ChatOpenAI`, apenas trocando `base_url` e `api_key`:
    DeepSeek → base_url = https://api.deepseek.com        (ex.: model="deepseek-chat")
    Z.ai     → base_url = https://api.z.ai/api/paas/v4/   (ex.: model="glm-5")
Nenhum dos dois oferece API de embeddings de uso geral compatível — por isso
não entram em EMBEDDING_PROVIDERS (mesma situação da Anthropic).

Se a chave do provedor selecionado não estiver configurada, o erro só aparece
na primeira chamada (lazy import + lazy client). Isso é intencional: assim o
backend sobe normalmente mesmo que uma das chaves esteja ausente — só falha
quando alguém de fato tentar usar aquele provedor.
"""
from app.config import settings

CHAT_PROVIDERS = ("gemini", "anthropic", "openai", "deepseek", "zai")
EMBEDDING_PROVIDERS = ("gemini", "openai")  # demais provedores não têm API de embeddings utilizável aqui


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

    if provider == "deepseek":
        # API compatível com OpenAI — mesmo client, só muda base_url + chave.
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=model,
            api_key=settings.DEEPSEEK_API_KEY,
            base_url="https://api.deepseek.com",
            temperature=temperature,
        )

    if provider == "zai":
        # Z.ai (GLM) também expõe API compatível com OpenAI.
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=model,
            api_key=settings.ZAI_API_KEY,
            base_url="https://api.z.ai/api/paas/v4/",
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

    if provider in ("anthropic", "deepseek", "zai"):
        raise ValueError(
            f"{provider!r} não oferece API de embeddings utilizável aqui — "
            "escolha 'gemini' ou 'openai' como provedor de embedding "
            "(o provedor de chat pode continuar sendo outro, ver AIConfig.provider vs. embedding_provider)."
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
